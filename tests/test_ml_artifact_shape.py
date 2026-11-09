"""Lead finding: an ML artifact whose arrays are shorter or longer than the feature list
must never be silently truncated by zip(); it must be refused, and the metric helpers must
refuse mismatched label/prediction lists."""

from __future__ import annotations

import json

import pytest

from agentpal.analyzers import AnalyzerUnavailable, MLAnalyzer
from agentpal.evaluation import _per_category, action_confusion, binary_metrics
from agentpal.features import ML_FEATURE_NAMES


def _artifact(**override) -> dict:
    n = len(ML_FEATURE_NAMES)
    art = {"feature_names": list(ML_FEATURE_NAMES), "mean": [0.0] * n, "scale": [1.0] * n,
           "coef": [0.1] * n, "intercept": 0.0, "threshold": 0.5, "version": "test"}
    art.update(override)
    return art


def _write(tmp_path, art) -> MLAnalyzer:
    p = tmp_path / "m.json"
    p.write_text(json.dumps(art))
    return MLAnalyzer(p, require_integrity=False)


def test_well_formed_artifact_loads(tmp_path):
    m = _write(tmp_path, _artifact())
    assert m.available()
    p, contrib = m.predict_proba([0.0] * len(ML_FEATURE_NAMES))
    assert 0 < p < 1 and len(contrib) == len(ML_FEATURE_NAMES)


@pytest.mark.parametrize("field", ["mean", "scale", "coef"])
@pytest.mark.parametrize("delta", [-1, +1])
def test_artifact_array_length_mismatch_is_refused(tmp_path, field, delta):
    n = len(ML_FEATURE_NAMES) + delta
    fill = 1.0 if field == "scale" else 0.0
    m = _write(tmp_path, _artifact(**{field: [fill] * n}))
    assert not m.available()
    assert field in m.unavailable_reason
    with pytest.raises(AnalyzerUnavailable):
        m._load()


@pytest.mark.parametrize("bad", [None, "x", [0.0, "a"], {"a": 1}])
def test_artifact_non_numeric_arrays_are_refused(tmp_path, bad):
    m = _write(tmp_path, _artifact(coef=bad))
    assert not m.available()


def test_artifact_bad_intercept_or_threshold_is_refused(tmp_path):
    assert not _write(tmp_path, _artifact(intercept="1")).available()
    assert not _write(tmp_path, _artifact(threshold=1.5)).available()


def test_predict_proba_rejects_a_vector_of_the_wrong_length(tmp_path):
    m = _write(tmp_path, _artifact())
    with pytest.raises(ValueError):
        m.predict_proba([0.0] * (len(ML_FEATURE_NAMES) - 1))


def test_metric_helpers_refuse_mismatched_lengths():
    with pytest.raises(ValueError):
        binary_metrics([1, 0, 1], [1, 0])
    with pytest.raises(ValueError):
        action_confusion(["block"], ["block", "block"])
    with pytest.raises(ValueError):
        _per_category([{"category": "a", "label": 1}], [1, 0])
