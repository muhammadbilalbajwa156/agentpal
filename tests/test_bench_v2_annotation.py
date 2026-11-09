"""AgentPal-Bench v2 annotation kit: label schema, blinded packets, kappa, adjudication.

All annotations below are synthetic test fixtures created in tmp directories; no
annotation of the benchmark exists yet (that needs independent people)."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from agentpal.config import PACKAGE_ROOT

V2 = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v2"
sys.path.insert(0, str(V2 / "annotation"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


annolib = _load("annolib", V2 / "annotation" / "annolib.py")
agreement = _load("bench_v2_agreement", V2 / "annotation" / "agreement.py")
adjudicate = _load("bench_v2_adjudicate", V2 / "annotation" / "adjudicate.py")
packets = _load("bench_v2_make_packets", V2 / "annotation" / "make_packets.py")

UNITS = annolib.units(V2)


def row(ann, item, risky, action, **kw):
    return {"annotator_id": ann, "item_id": item, "risky": risky, "expected_action": action,
            "rationale": "fixture rationale text", "policy_clause": "P-ALLOW",
            "difficulty": "easy", "time_spent_seconds": 30, "guideline_version": "test-1",
            **kw}


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ------------------------------------------------------------------ schema

def test_valid_row_passes_schema():
    assert annolib.validate_row(row("ann01", "v2-case-001", 0, "allow_for_review")) == []


@pytest.mark.parametrize("bad", [
    {"risky": 0, "expected_action": "block"},           # inconsistent with the guideline
    {"risky": 1, "expected_action": "allow_for_review"},
    {"risky": 2},
    {"expected_action": "approve"},
    {"rationale": "short"},
    {"policy_clause": "whatever"},
    {"time_spent_seconds": -1},
    {"unexpected": 1},
])
def test_invalid_rows_are_rejected(bad):
    assert annolib.validate_row({**row("ann01", "v2-case-001", 1, "require_review"), **bad})


def test_missing_required_field_and_duplicates_are_rejected(tmp_path):
    r = row("ann01", "v2-case-001", 0, "allow_for_review")
    r.pop("guideline_version")
    with pytest.raises(ValueError):
        annolib.load_annotations([write(tmp_path / "a.jsonl", [r])])
    good = row("ann01", "v2-case-001", 0, "allow_for_review")
    with pytest.raises(ValueError, match="duplicate"):
        annolib.load_annotations([write(tmp_path / "b.jsonl", [good, good])])


def test_schema_file_matches_the_kit():
    schema = json.loads((V2 / "annotation" / "label_schema.json").read_text("utf-8"))
    assert set(schema["required"]) >= {"annotator_id", "item_id", "risky", "expected_action",
                                       "rationale", "policy_clause", "difficulty",
                                       "time_spent_seconds"}


# ------------------------------------------------------------------ kappa

def test_fleiss_kappa_matches_the_textbook_example():
    # Fleiss (1971) / Wikipedia worked example: 10 subjects, 14 raters, 5 categories
    counts = [[0, 0, 0, 0, 14], [0, 2, 6, 4, 2], [0, 0, 3, 5, 6], [0, 3, 9, 2, 0],
              [2, 2, 8, 1, 1], [7, 7, 0, 0, 0], [3, 2, 6, 3, 0], [2, 5, 3, 2, 2],
              [6, 5, 2, 1, 0], [0, 2, 2, 3, 7]]
    ratings = [[c for c, n in enumerate(r) for _ in range(n)] for r in counts]
    assert agreement.fleiss_kappa(ratings, list(range(5))) == pytest.approx(0.20993, abs=1e-4)


def test_cohen_kappa_known_values_and_undefined_case():
    assert agreement.cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == pytest.approx(0.5)
    assert agreement.cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == pytest.approx(1.0)
    assert agreement.cohen_kappa([1, 1], [1, 1]) is None           # pe = 1: undefined
    assert agreement.fleiss_kappa([[0, 0, 0], [0, 0, 0]], [0, 1]) is None


def test_agreement_report_overall_per_risk_type_and_gate(tmp_path):
    ids = [u["unit_id"] for u in UNITS[:40]]
    props = {u["unit_id"]: u for u in UNITS}
    files = []
    for k, ann in enumerate(("ann01", "ann02", "ann03")):
        rows = []
        for j, i in enumerate(ids):
            lab = props[i]["proposed_label"]
            if k == 2 and j % 10 == 0:          # one annotator disagrees on 10% of items
                lab = 1 - lab
            rows.append(row(ann, i, lab, "require_review" if lab else "allow_for_review"))
        files.append(write(tmp_path / f"{ann}.jsonl", rows))
    rep = agreement.agreement_report(V2, files)
    assert rep["overall"]["items"] == 40
    assert 0.6 <= rep["overall"]["fleiss_kappa_risky"] < 1.0
    assert rep["gate_passed"]
    assert set(rep["per_risk_type"]) == {props[i]["risk_type"] for i in ids}
    assert set(rep["pairwise_cohen"]) == {"ann01|ann02", "ann01|ann03", "ann02|ann03"}
    assert rep["pairwise_cohen"]["ann01|ann02"]["risky"] == pytest.approx(1.0)


def test_agreement_rejects_labels_for_unknown_items(tmp_path):
    f = write(tmp_path / "a.jsonl", [row("ann01", "not-an-item", 0, "allow_for_review")])
    with pytest.raises(ValueError, match="unknown items"):
        agreement.agreement_report(V2, [f])


# ------------------------------------------------------------------ adjudication

def _three(tmp_path, votes: dict[str, list[tuple[int, str]]], flags=None):
    files = []
    for k, ann in enumerate(("ann01", "ann02", "ann03")):
        rows = [row(ann, item, *v[k], **({"flags": ["ambiguous"]}
                                         if flags and (item, ann) in flags else {}))
                for item, v in votes.items() if k < len(v)]
        files.append(write(tmp_path / f"{ann}.jsonl", rows))
    return files


A, R, B = "allow_for_review", "require_review", "block"


def test_adjudication_majority_ties_block_disagreement_and_insufficient(tmp_path):
    ids = [u["unit_id"] for u in UNITS[:6]]
    votes = {
        ids[0]: [(0, A), (0, A), (0, A)],                 # unanimous
        ids[1]: [(1, R), (1, R), (0, A)],                 # 2-1 majority
        ids[2]: [(1, B), (1, R), (1, R)],                 # block vs non-block -> adjudicator
        ids[3]: [(1, B), (1, R), (0, A)],                 # tie on action -> adjudicator
        ids[4]: [(0, A), (0, A)],                         # only two annotators
        ids[5]: [(1, R), (1, R), (1, R)],                 # flagged ambiguous -> adjudicator
    }
    files = _three(tmp_path, votes, flags={(ids[5], "ann02")})
    res = adjudicate.adjudicate(V2, files)
    final = {r["item_id"]: r for r in res["final"]}
    assert final[ids[0]]["expected_action"] == A and final[ids[0]]["method"] == "majority"
    assert final[ids[1]]["risky"] == 1 and final[ids[1]]["expected_action"] == R
    needs = {r["item_id"]: r["reasons"] for r in res["needs"]}
    assert "block vs non-block disagreement" in needs[ids[2]]
    assert "tie on expected_action" in needs[ids[3]]
    assert "flagged ambiguous by an annotator" in needs[ids[5]]
    assert [r["item_id"] for r in res["insufficient"]] == [ids[4]]
    # the adjudicator decides two items and marks one ambiguous
    adj = write(tmp_path / "adj.jsonl", [
        {"item_id": ids[2], "adjudicator_id": "senior01", "risky": 1, "expected_action": B,
         "rationale": "budget exceeded per clause P-BLOCK-BUDGET"},
        {"item_id": ids[3], "adjudicator_id": "senior01", "ambiguous": True,
         "rationale": "item text does not say whether the budget was exhausted"},
        {"item_id": ids[5], "adjudicator_id": "ann02", "risky": 1, "expected_action": R,
         "rationale": "the wording is unclear but review is the safe call"},
    ])
    res = adjudicate.adjudicate(V2, files, adj)
    final = {r["item_id"]: r for r in res["final"]}
    assert final[ids[2]]["method"] == "adjudicator" and final[ids[2]]["expected_action"] == B
    assert final[ids[5]]["adjudicator_was_annotator"] is True
    assert [r["item_id"] for r in res["ambiguous"]] == [ids[3]]
    assert res["needs"] == []
    assert all(r["n_annotators"] >= 3 for r in res["final"])


def test_adjudicator_rows_must_be_consistent(tmp_path):
    ids = [u["unit_id"] for u in UNITS[:1]]
    files = _three(tmp_path, {ids[0]: [(1, B), (1, R), (1, R)]})
    adj = write(tmp_path / "adj.jsonl", [{"item_id": ids[0], "adjudicator_id": "s1",
                                          "risky": 0, "expected_action": B,
                                          "rationale": "inconsistent on purpose"}])
    with pytest.raises(ValueError, match="inconsistent"):
        adjudicate.adjudicate(V2, files, adj)


def test_adjudicate_cli_refuses_fewer_than_three_annotators(tmp_path):
    f = write(tmp_path / "a.jsonl", [row("ann01", UNITS[0]["unit_id"], 0, A)])
    assert adjudicate.main(["--bench-dir", str(V2), "--annotations", str(f),
                            "--min-annotators", "2", "--out-dir", str(tmp_path)]) == 2


# ------------------------------------------------------------------ packets

def test_packets_are_blinded_and_shuffled_per_annotator(tmp_path):
    manifest = packets.make_packets(V2, ["ann01", "ann02", "ann03"], tmp_path, PACKAGE_ROOT)
    assert manifest["ann01"]["label_rows"] == len(UNITS)
    texts = {}
    for ann in ("ann01", "ann02", "ann03"):
        text = (tmp_path / ann / "units.jsonl").read_text("utf-8")
        texts[ann] = text
        for forbidden in ("proposed_", "risk_type", "group_id", "v2g-", "provenance",
                          "author", "wording", "summary", "mechanics", "label_status",
                          "harness_v1_compatible", "independent"):
            assert forbidden not in text, (ann, forbidden)
        tmpl = annolib.read_jsonl(tmp_path / ann / "labels_template.jsonl")
        assert {r["item_id"] for r in tmpl} == {u["unit_id"] for u in UNITS}
        ctx = json.loads((tmp_path / ann / "context.json").read_text("utf-8"))
        assert ctx["approved_merchants"] and ctx["policy_clauses"]
    order = {a: [json.loads(x)["item_id"] for x in t.splitlines()] for a, t in texts.items()}
    assert order["ann01"] != order["ann02"] and sorted(order["ann01"]) == sorted(order["ann02"])


def test_packets_describe_changed_terms_and_unverified_outcomes():
    units = {u["item_id"]: u for u in packets.blinded_units(V2)}
    ep14 = next(u for k, u in units.items() if "attempt-with-other-terms" in k)
    events = " ".join(ep14["steps"][0]["events_after_this_step"])
    assert "CHANGED terms" in events
    ep16 = next(u for k, u in units.items() if "unverified-outcome" in k)
    assert "NOT verified" in " ".join(ep16["steps"][0]["events_after_this_step"])
