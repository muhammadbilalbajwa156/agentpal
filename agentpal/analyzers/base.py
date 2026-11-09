"""Provider-independent interface for AI risk analysis backends."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from ..features import Features
from ..rules import RuleResult
from ..schemas import ModelAnalysis, PaymentRequest


class AnalyzerUnavailable(RuntimeError):
    """Backend cannot run (not installed, not reachable, no model file...)."""


class MalformedModelOutput(ValueError):
    """Backend ran but its output did not satisfy the ModelAnalysis schema."""


@dataclass(frozen=True)
class AnalyzerInput:
    request: PaymentRequest
    features: Features
    rules: RuleResult
    # time.monotonic() deadline; cooperative backends (llama.cpp) stop generating after it
    deadline: float | None = None


@runtime_checkable
class RiskAnalyzer(Protocol):
    name: str          # backend identifier, e.g. "llamacpp"
    model_name: str    # actual model in use
    model_version: str
    is_fallback: bool  # True for non-generative deterministic fallbacks
    reports_uncertainty: bool  # whether `uncertainty` is meaningful enough to act on

    def available(self) -> bool: ...

    def analyze(self, data: AnalyzerInput) -> ModelAnalysis: ...


# Claims a model cannot make from the data we give it. Findings asserting them
# are dropped so model output never presents unverifiable facts as verified.
_UNVERIFIABLE_CLAIM = re.compile(
    r"\b(verified|confirmed (as )?(legitimate|safe|genuine)|is (a )?(legitimate|trusted|"
    r"reputable) (merchant|company|business)|known (scam|fraudster)|good reputation|"
    r"positive reviews|bbb|trustpilot)\b",
    re.IGNORECASE,
)


# Hard bounds on what a backend may return before anything is parsed. A schema-valid
# ModelAnalysis serializes to well under 16 KB (12 findings x 3 x 500 chars, 1000-char
# summary); anything larger is a runaway generation, not an answer.
MAX_RAW_OUTPUT_CHARS = 32_000
MAX_THINK_CHARS = 8_000
MAX_MISSING_INFORMATION_CHARS = 500
_THINK = re.compile(r"\A\s*<think>(.*?)</think>\s*", re.DOTALL)
_FENCED = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.DOTALL)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise MalformedModelOutput("duplicate key in model output")
    return dict(pairs)


def _reject_constant(name: str) -> None:
    raise MalformedModelOutput(f"non-finite number {name} in model output")


def _strip_think(text: str) -> str:
    """Qwen3-style reasoning models may emit one leading `<think>...</think>` block (empty
    when thinking is disabled). Strip exactly one bounded block; anything else that mentions
    think tags is rejected rather than guessed at."""
    m = _THINK.match(text)
    if m:
        if len(m.group(1)) > MAX_THINK_CHARS:
            raise MalformedModelOutput("reasoning block exceeds the size limit")
        text = text[m.end():]
    if "<think>" in text or "</think>" in text:
        raise MalformedModelOutput("unbalanced or repeated reasoning block")
    return text


def parse_model_output(raw: str) -> ModelAnalysis:
    """Extract and schema-validate a ModelAnalysis from raw model text.

    Accepted shape, after surrounding whitespace: an optional single leading
    `<think>...</think>` block, then EXACTLY one JSON object, optionally inside one
    ```json fence. Prose before or after the object, several objects, duplicate keys,
    NaN/Infinity, oversized output and schema violations are all rejected with
    MalformedModelOutput, which the orchestrator turns into a degraded, rules-only
    assessment (never an allow)."""
    if not isinstance(raw, str) or not raw.strip():
        raise MalformedModelOutput("empty model output")
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise MalformedModelOutput(f"model output exceeds {MAX_RAW_OUTPUT_CHARS} characters")
    text = _strip_think(raw.strip()).strip()
    fenced = _FENCED.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        raise MalformedModelOutput("model output is not a single JSON object")
    # strict=False only admits raw control characters (e.g. a newline) INSIDE string
    # values: llama-cpp-python 0.3.36's JSON-schema grammar lets them through, they carry
    # no meaning, and every schema check below still applies (ML_AUDIT ML-14).
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys,
                               parse_constant=_reject_constant, strict=False)
    try:
        payload, end = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise MalformedModelOutput(f"invalid JSON: {exc.msg}") from exc
    except RecursionError as exc:
        raise MalformedModelOutput("model output is nested too deeply") from exc
    if text[end:].strip():
        raise MalformedModelOutput("unexpected text after the JSON object")
    if not isinstance(payload, dict):
        raise MalformedModelOutput("model output is not a JSON object")
    missing = payload.get("missing_information")
    if isinstance(missing, list) and any(
            not isinstance(m, str) or len(m) > MAX_MISSING_INFORMATION_CHARS for m in missing):
        raise MalformedModelOutput("missing_information items must be short strings")
    try:
        # strict: "90" is not a score and true is not an uncertainty (no silent coercion)
        analysis = ModelAnalysis.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise MalformedModelOutput(f"schema violation: {exc.error_count()} error(s)") from exc
    analysis.findings = [
        f for f in analysis.findings
        if not _UNVERIFIABLE_CLAIM.search(f"{f.description} {f.evidence}")
    ]
    return analysis
