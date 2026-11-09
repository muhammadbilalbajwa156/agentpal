import json

import pytest

from agentpal.dataset import CATEGORIES, generate, write_dataset
from agentpal.evaluation import binary_metrics, evaluate, fit_baseline, to_inputs


@pytest.fixture(scope="module")
def dataset_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    write_dataset(out, seed=123)
    return out


def test_generation_is_reproducible(tmp_path):
    m1 = write_dataset(tmp_path / "a", seed=7)
    m2 = write_dataset(tmp_path / "b", seed=7)
    m3 = write_dataset(tmp_path / "c", seed=8)
    assert m1["sha256"] == m2["sha256"]
    assert m1["sha256"] != m3["sha256"]


def test_all_examples_validate_and_are_documented():
    examples = generate(seed=11, variations=2)
    assert {e.category for e in examples} == set(CATEGORIES)
    for e in examples:
        to_inputs(e.__dict__)  # schema-valid request + context
        assert e.label in (0, 1) and e.scenario and e.label_reason
        assert e.expected_action in ("allow_for_review", "require_review", "block")


def test_groups_never_cross_splits_and_no_label_leakage():
    examples = generate(seed=11, variations=3)
    split_of = {}
    for e in examples:
        assert split_of.setdefault(e.group_id, e.split) == e.split
    assert {e.split for e in examples} == {"train", "validation", "test"}
    for e in examples:
        blob = json.dumps({"request": e.request, "context": e.context}).lower()
        assert e.category not in blob
        assert "label" not in blob and "scenario" not in blob
        assert e.category not in e.request["request_id"]


def test_binary_metrics():
    m = binary_metrics([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
    assert m["confusion_matrix"] == {"tp": 2, "fp": 1, "tn": 1, "fn": 1}
    assert m["precision"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["false_positive_rate"] == 0.5


def test_train_and_evaluate_are_reproducible(dataset_dir, tmp_path):
    a1 = fit_baseline(dataset_dir, tmp_path / "m1.json")
    a2 = fit_baseline(dataset_dir, tmp_path / "m2.json")
    assert a1["coef"] == a2["coef"] and a1["threshold"] == a2["threshold"]
    r1 = evaluate(dataset_dir, tmp_path / "m1.json", tmp_path / "r1.json")
    r2 = evaluate(dataset_dir, tmp_path / "m1.json", tmp_path / "r2.json")
    for name in ("rules_only", "ml_classifier_only", "rules_plus_ml"):
        assert r1["approaches"][name]["binary"] == r2["approaches"][name]["binary"]
    saved = json.loads((tmp_path / "r1.json").read_text())
    assert saved["notice"].startswith("SYNTHETIC EVALUATION")
    assert saved["dataset"]["split"] == "test"
