from __future__ import annotations

from ..config import Settings
from ..integrity import load_manifest
from .base import (
    AnalyzerInput,
    AnalyzerUnavailable,
    MalformedModelOutput,
    RiskAnalyzer,
    parse_model_output,
)
from .heuristic import HeuristicAnalyzer
from .llm import LlamaCppAnalyzer, OllamaAnalyzer
from .ml import MLAnalyzer

__all__ = [
    "AnalyzerInput", "AnalyzerUnavailable", "MalformedModelOutput", "RiskAnalyzer",
    "parse_model_output", "HeuristicAnalyzer", "LlamaCppAnalyzer", "OllamaAnalyzer",
    "MLAnalyzer", "build_analyzer_chain",
]


def build_analyzer_chain(settings: Settings) -> list[RiskAnalyzer]:
    """Analyzers in priority order. The heuristic fallback is always appended."""
    chain: list[RiskAnalyzer] = []
    manifest = load_manifest(settings.model_manifest_path)
    strict = settings.require_model_integrity
    for name in settings.analyzer_chain:
        if name == "llamacpp":
            chain.append(LlamaCppAnalyzer(
                settings.llamacpp_model_path, expected_sha256=manifest.digest_for("llamacpp"),
                require_integrity=strict, staging_dir=settings.model_staging_dir,
                require_read_only_storage=settings.require_read_only_model_storage,
                # a fine-tuned model is served exactly as it was trained (no rule findings,
                # thinking disabled). The Settings field is owned by config.py; until it
                # exists the historical behaviour (rule findings shown) is kept.
                include_rule_findings=getattr(settings, "llamacpp_include_rule_findings",
                                              True),
                disable_thinking=True))
        elif name == "ollama":
            chain.append(OllamaAnalyzer(settings.ollama_url, settings.ollama_model,
                                        settings.model_timeout_seconds,
                                        expected_sha256=manifest.digest_for("ollama"),
                                        require_integrity=strict))
        elif name == "ml":
            chain.append(MLAnalyzer(settings.ml_model_path,
                                    expected_sha256=manifest.digest_for("ml"),
                                    require_integrity=strict))
        elif name == "heuristic":
            chain.append(HeuristicAnalyzer())
        else:
            raise ValueError(f"unknown analyzer backend: {name!r}")
    if not any(isinstance(a, HeuristicAnalyzer) for a in chain):
        chain.append(HeuristicAnalyzer())
    return chain
