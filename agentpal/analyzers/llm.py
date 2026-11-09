"""Generative open-weight model backends: in-process llama.cpp, or an Ollama server.

Default model: Qwen2.5-1.5B-Instruct (GGUF, Q4_K_M), Apache-2.0 licensed. The planned
fine-tuned replacement is Qwen3-1.7B (docs/RISK_MODEL_PLAN.md).
Both backends request schema-constrained JSON and re-validate it with
`parse_model_output` regardless, so a malformed reply is rejected, not trusted.

Inference contract for a fine-tuned Qwen3 model (must match training/train_lora.py):
  * thinking DISABLED: the prompt is rendered with the GGUF's own chat template and
    `enable_thinking=False`, so it ends with an empty `<think>\n\n</think>\n\n` block,
    exactly as in training. llama-cpp-python 0.3.36 `create_chat_completion` has no
    chat_template_kwargs, so `no_think_chat_handler` injects the flag at render time.
    Templates without the variable (Qwen2.5) render unchanged;
  * `include_rule_findings` must equal the value used to build the SFT data (False).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .. import integrity
from ..schemas import ModelAnalysis
from .base import AnalyzerInput, AnalyzerUnavailable, parse_model_output
from .prompting import build_messages, output_schema

_LLAMA_CACHE: dict[str, object] = {}
CHAT_TEMPLATE_METADATA_KEY = "tokenizer.chat_template"


def no_think_chat_handler(template: str, eos_token: str, bos_token: str,
                          stop_token_ids: list[int] | None = None):
    """A llama-cpp-python chat handler that renders `template` with enable_thinking=False
    (mirrors how llama_cpp.Llama builds its default handler from GGUF metadata)."""
    from llama_cpp import llama_chat_format as lcf  # optional dependency, imported lazily

    base = lcf.Jinja2ChatFormatter(template=template, eos_token=eos_token,
                                   bos_token=bos_token, add_generation_prompt=True,
                                   stop_token_ids=stop_token_ids)

    class _NoThink:
        def __call__(self, **kwargs: Any):
            kwargs["enable_thinking"] = False
            return base(**kwargs)

    return lcf.chat_formatter_to_chat_completion_handler(_NoThink())


def install_no_think_handler(llm: Any) -> bool:
    """Make `llm.create_chat_completion` render the GGUF-embedded template with thinking
    disabled. Returns False (and changes nothing) when the GGUF carries no template."""
    template = (getattr(llm, "metadata", None) or {}).get(CHAT_TEMPLATE_METADATA_KEY)
    if not template:
        return False
    eos_id, bos_id = llm.token_eos(), llm.token_bos()
    text = llm._model.token_get_text  # same source llama_cpp.Llama uses for its handler
    llm.chat_handler = no_think_chat_handler(
        template, eos_token=text(eos_id) if eos_id != -1 else "",
        bos_token=text(bos_id) if bos_id != -1 else "",
        stop_token_ids=[eos_id] if eos_id != -1 else None)
    return True


_VERIFIED: set[tuple] = set()
_LLAMA_LOCK = threading.Lock()


class LlamaCppAnalyzer:
    name = "llamacpp"
    is_fallback = False
    reports_uncertainty = True

    def __init__(self, model_path: str | Path, n_threads: int | None = None,
                 max_tokens: int = 384, expected_sha256: str | None = None,
                 require_integrity: bool = True, staging_dir: str | Path | None = None,
                 require_read_only_storage: bool = True,
                 include_rule_findings: bool = True, disable_thinking: bool = True):
        self.model_path = Path(model_path)
        # serving must match training: a model fine-tuned without rule findings is served
        # without them (training.sft_data.INCLUDE_RULE_FINDINGS)
        self.include_rule_findings = include_rule_findings
        self.disable_thinking = disable_thinking
        # decision 7: load only a verified private copy, from read-only source storage
        self.staging_dir = Path(staging_dir) if staging_dir else None
        self.require_read_only_storage = require_read_only_storage
        self.expected_sha256 = expected_sha256
        self.require_integrity = require_integrity
        self.unavailable_reason = ""
        self.model_name = self.model_path.stem
        # llama-cpp-python's default generation thread count is HALF the CPUs; on the 4-core
        # serving host all cores cut a typical assessment from ~10.3 s to ~6-7 s (measured
        # 2026-10-30), widening the margin to the model timeout.
        self.n_threads = n_threads or os.cpu_count() or 1
        self.max_tokens = max_tokens
        try:
            import llama_cpp  # noqa: F401
            self.model_version = f"llama-cpp-python {llama_cpp.__version__}"
        except ImportError:
            self.model_version = "llama-cpp-python not installed"

    def _verify(self) -> None:
        """Verify the GGUF against the trusted digest before it is ever loaded. Cached per
        process by file identity (path, size, mtime, inode); any change re-verifies."""
        if not self.expected_sha256:
            if self.require_integrity:
                raise AnalyzerUnavailable(
                    "integrity: no trusted digest configured for the GGUF model")
            return
        st = self.model_path.stat()
        key = (str(self.model_path), st.st_size, st.st_mtime_ns, st.st_ino,
               self.expected_sha256.lower())
        if key in _VERIFIED:
            return
        try:
            integrity.verify_large_file(self.model_path, self.expected_sha256)
        except integrity.IntegrityError as exc:
            raise AnalyzerUnavailable(str(exc)) from exc
        _VERIFIED.add(key)

    def available(self) -> bool:
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            self.unavailable_reason = "llama-cpp-python not installed"
            return False
        if not self.model_path.exists():
            self.unavailable_reason = "model file not found"
            return False
        if self.require_read_only_storage:
            ok, why = integrity.storage_is_read_only(self.model_path)
            if not ok:
                self.unavailable_reason = (f"integrity: model storage must be read-only "
                                           f"({why})")
                return False
        try:
            self._verify()
        except AnalyzerUnavailable as exc:
            self.unavailable_reason = str(exc)
            return False
        self.unavailable_reason = ""
        return True

    def _llm(self):
        if not self.available():
            raise AnalyzerUnavailable(self.unavailable_reason or "llama.cpp unavailable")
        key = str(self.model_path)
        with _LLAMA_LOCK:
            if key not in _LLAMA_CACHE:
                from llama_cpp import Llama

                load_path = self.model_path
                if self.expected_sha256 and self.staging_dir is not None:
                    try:  # hash-while-copy into private staging; load only that copy
                        load_path = integrity.stage_verified_copy(
                            self.model_path, self.expected_sha256, self.staging_dir)
                    except (integrity.IntegrityError, OSError) as exc:
                        raise AnalyzerUnavailable(f"integrity: staging failed: {exc}") from exc
                llm = Llama(model_path=str(load_path), n_ctx=4096,
                            n_threads=self.n_threads, n_threads_batch=self.n_threads,
                            seed=0, verbose=False)
                if self.disable_thinking:
                    install_no_think_handler(llm)
                _LLAMA_CACHE[key] = llm
            return _LLAMA_CACHE[key]

    def analyze(self, data: AnalyzerInput) -> ModelAnalysis:
        llm = self._llm()
        parts: list[str] = []
        with _LLAMA_LOCK:  # llama.cpp contexts are not thread-safe
            # Streaming lets us check the deadline between tokens and stop generation by
            # closing the generator (create_chat_completion has no stopping-criteria hook).
            stream = llm.create_chat_completion(
                messages=build_messages(
                    data, include_rule_findings=self.include_rule_findings),
                response_format={"type": "json_object", "schema": output_schema()},
                temperature=0.0,
                max_tokens=self.max_tokens,
                stream=True,
            )
            try:
                for chunk in stream:
                    if data.deadline is not None and time.monotonic() > data.deadline:
                        raise AnalyzerUnavailable("llama.cpp generation stopped at the deadline")
                    parts.append(chunk["choices"][0].get("delta", {}).get("content") or "")
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
        return parse_model_output("".join(parts))


class OllamaAnalyzer:
    name = "ollama"
    is_fallback = False
    reports_uncertainty = True

    def __init__(self, base_url: str, model: str, timeout: float = 30.0,
                 client: httpx.Client | None = None, expected_sha256: str | None = None,
                 require_integrity: bool = True):
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.model_version = "unknown"
        # Ollama owns its model files; we can only compare the digest the server REPORTS
        # with the pinned one. That detects accidental model changes, not a malicious
        # server (documented limitation).
        self.expected_sha256 = expected_sha256
        self.require_integrity = require_integrity
        self.unavailable_reason = ""
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def available(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/api/tags", timeout=2.0)
            r.raise_for_status()
            models = {m.get("name"): m for m in r.json().get("models", [])}
        except (httpx.HTTPError, ValueError):
            self.unavailable_reason = "ollama server unreachable"
            return False
        entry = models.get(self.model_name)
        if entry is None:
            self.unavailable_reason = "model not present on ollama server"
            return False
        reported = str(entry.get("digest", "")).lower().removeprefix("sha256:")
        if self.expected_sha256:
            if reported != self.expected_sha256.lower():
                self.unavailable_reason = "integrity: ollama-reported digest mismatch"
                return False
        elif self.require_integrity:
            self.unavailable_reason = "integrity: no trusted digest configured for ollama model"
            return False
        self.unavailable_reason = ""
        return True

    def analyze(self, data: AnalyzerInput) -> ModelAnalysis:
        try:
            r = self._client.post(
                f"{self.base_url}/api/chat",
                json={"model": self.model_name, "messages": build_messages(data),
                      "format": output_schema(), "stream": False,
                      "options": {"temperature": 0, "seed": 0}},
                timeout=self.timeout,
            )
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AnalyzerUnavailable(f"ollama request failed: {type(exc).__name__}") from exc
        return parse_model_output(body.get("message", {}).get("content", ""))
