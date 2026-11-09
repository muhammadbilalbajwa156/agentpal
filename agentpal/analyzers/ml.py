"""Small trained classifier (logistic regression on engineered features).

The artifact is plain JSON (coefficients + scaler), produced by `agentpal train`.
Using JSON instead of pickle means loading a model file can never execute code.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..features import ML_FEATURE_NAMES, ml_vector
from ..schemas import ModelAnalysis, ModelFinding
from .. import integrity
from .base import AnalyzerInput, AnalyzerUnavailable


def _finite(x: object) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


class MLAnalyzer:
    name = "ml"
    is_fallback = False
    reports_uncertainty = False  # LR probabilities are not epistemic uncertainty

    def __init__(self, model_path: str | Path, expected_sha256: str | None = None,
                 require_integrity: bool = True):
        self.model_path = Path(model_path)
        self.expected_sha256 = expected_sha256
        self.require_integrity = require_integrity
        self._model: dict | None = None
        self.model_name = "logistic-regression-baseline"
        self.model_version = "untrained"
        self.unavailable_reason = ""

    def _load(self) -> dict:
        if self._model is None:
            if not self.model_path.exists():
                raise AnalyzerUnavailable(f"ML artifact not found: {self.model_path.name}")
            if self.expected_sha256:
                try:  # parse exactly the bytes that were verified (no TOCTOU window)
                    raw = integrity.verify_file(self.model_path, self.expected_sha256)
                except integrity.IntegrityError as exc:
                    raise AnalyzerUnavailable(str(exc)) from exc
            elif self.require_integrity:
                raise AnalyzerUnavailable(
                    "integrity: no trusted digest configured for the ML artifact "
                    "(pin it with `python -m agentpal pin-model ml <path>`)")
            else:
                raw = self.model_path.read_bytes()
            try:
                model = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AnalyzerUnavailable("ML artifact is not valid JSON") from exc
            self._model = self._validated(model)
            model = self._model
            self.model_version = model.get("version", "unknown")
        return self._model

    @staticmethod
    def _validated(model: object) -> dict:
        """Refuse an artifact whose shape does not match the code: zip() would otherwise
        silently truncate mismatched arrays and score on a subset of features."""
        if not isinstance(model, dict):
            raise AnalyzerUnavailable("ML artifact is not a JSON object")
        if model.get("feature_names") != ML_FEATURE_NAMES:
            raise AnalyzerUnavailable("ML artifact feature set does not match code")
        n = len(ML_FEATURE_NAMES)
        for key in ("mean", "scale", "coef"):
            arr = model.get(key)
            if not isinstance(arr, list) or len(arr) != n:
                raise AnalyzerUnavailable(
                    f"ML artifact '{key}' must be a list of {n} numbers")
            if not all(_finite(x) for x in arr):
                raise AnalyzerUnavailable(f"ML artifact '{key}' contains a non-number")
        if not _finite(model.get("intercept")):
            raise AnalyzerUnavailable("ML artifact 'intercept' must be a number")
        threshold = model.get("threshold", 0.5)
        if not _finite(threshold) or not 0.0 < threshold < 1.0:
            raise AnalyzerUnavailable("ML artifact 'threshold' must be in (0, 1)")
        return model

    def available(self) -> bool:
        try:
            self._load()
            self.unavailable_reason = ""
            return True
        except AnalyzerUnavailable as exc:
            self.unavailable_reason = str(exc)
            return False

    def predict_proba(self, vector: list[float]) -> tuple[float, list[float]]:
        m = self._load()
        if len(vector) != len(m["coef"]):
            raise ValueError(f"feature vector has {len(vector)} values, expected "
                             f"{len(m['coef'])}")
        z = [(x - mu) / (sd or 1.0)
             for x, mu, sd in zip(vector, m["mean"], m["scale"], strict=True)]
        contributions = [c * x for c, x in zip(m["coef"], z, strict=True)]
        logit = m["intercept"] + sum(contributions)
        return 1.0 / (1.0 + math.exp(-logit)), contributions

    @staticmethod
    def probability_to_score(p: float, threshold: float, review_at: int = 25) -> int:
        """Piecewise-linear calibration: the validation-selected decision threshold maps
        to the policy's review boundary, so a probability is never read as a raw score."""
        if p < threshold:
            return int(round(review_at * p / threshold))
        return int(round(review_at + (100 - review_at) * (p - threshold) / (1 - threshold)))

    def analyze(self, data: AnalyzerInput) -> ModelAnalysis:
        m = self._load()
        p, contributions = self.predict_proba(ml_vector(data.features))
        threshold = m.get("threshold", 0.5)
        score = self.probability_to_score(p, threshold)
        top = sorted(zip(ML_FEATURE_NAMES, contributions, strict=True), key=lambda t: -t[1])
        findings = [
            ModelFinding(code=f"ML_{name.upper()}",
                         description=f"Feature '{name}' increased predicted risk.",
                         evidence=f"contribution={c:.2f}")
            for name, c in top[:5] if c > 0.25
        ]
        rec = "require_review" if p >= threshold else "allow_for_review"
        return ModelAnalysis(
            risk_score=score, findings=findings, missing_information=[],
            uncertainty=round(1 - abs(2 * p - 1), 3), recommendation=rec,
            summary=(f"Logistic-regression baseline trained on synthetic data: "
                     f"P(needs review)={p:.2f}. Prediction only; nothing was verified."),
        )
