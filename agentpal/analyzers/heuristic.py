"""Deterministic fallback analyzer. Needs no model, network or API key."""

from __future__ import annotations

from ..schemas import ModelAnalysis, ModelFinding, SignalKind
from .base import AnalyzerInput


class HeuristicAnalyzer:
    name = "heuristic"
    model_name = "deterministic-heuristic"
    model_version = "1.0"
    is_fallback = True
    reports_uncertainty = False

    def available(self) -> bool:
        return True

    def analyze(self, data: AnalyzerInput) -> ModelAnalysis:
        signals = data.rules.signals
        score = data.rules.score
        missing = [s.message for s in signals if s.kind == SignalKind.uncertainty]
        indicators = [s for s in signals if s.kind != SignalKind.uncertainty]
        # More missing information and fewer concrete indicators -> higher uncertainty.
        uncertainty = min(0.9, 0.2 + 0.15 * len(missing)) if not indicators else 0.3
        if data.rules.hard_violations:  # only deterministic violations block
            rec = "block"
        elif score >= 25:
            rec = "require_review"
        else:
            rec = "allow_for_review"
        findings = [
            ModelFinding(code=s.code, description=s.message[:500],
                         evidence=str(s.evidence)[:500])
            for s in sorted(signals, key=lambda s: -s.points)[:8]
        ]
        return ModelAnalysis(
            risk_score=score, findings=findings, missing_information=missing[:12],
            uncertainty=round(uncertainty, 2), recommendation=rec,
            summary="Deterministic fallback: mirrors rule findings; no AI model was used.",
        )
