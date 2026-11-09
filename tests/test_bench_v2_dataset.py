"""AgentPal-Bench v2 DEV set: structure, reproducibility, coverage and honesty markers.

These items were written by an AI helper agent with access to the rules; they are dev
items (independent: false) with PROPOSED labels for annotators, not ground truth.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from agentpal import benchmark
from agentpal.config import PACKAGE_ROOT
from agentpal.schemas import PaymentRequest

V2 = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v2"
CASES, EPISODES = benchmark.load(V2)
AUTHOR = "helper agent (AI-generated) - NOT independent"
ACTIONS = {"allow_for_review", "require_review", "block"}
REQUIRED_RISK_TYPES = {
    "legit_routine", "legit_hard_negative", "purpose_mismatch", "impersonation_lookalike",
    "near_name", "fabricated_evidence", "injection", "social_engineering", "budget_limit",
    "stale_expired", "unexpected_recurrence", "missing_information", "duplicate",
    "near_duplicate", "structuring", "replay", "approval_window", "attempted_vs_completed",
    "unverified_outcome",
}


def all_units():
    for c in CASES:
        yield c["id"], c, c
    for e in EPISODES:
        for i, s in enumerate(e["steps"]):
            yield f"{e['id']}#{i}", e, s


def test_generator_reproduces_committed_files_byte_for_byte(tmp_path):
    gen = V2 / "generate_dev.py"
    check = subprocess.run([sys.executable, str(gen), "--check"], capture_output=True,
                           text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    subprocess.run([sys.executable, str(gen), "--out", str(tmp_path)], check=True,
                   capture_output=True)
    for name in ("cases.jsonl", "episodes.jsonl"):
        assert (tmp_path / name).read_bytes() == (V2 / name).read_bytes()


def test_sizes_meet_the_dev_targets():
    assert len(CASES) >= 120
    assert len(EPISODES) >= 25


def test_every_item_is_marked_dev_not_independent_with_provenance():
    for item in CASES + EPISODES:
        assert item["split"] == "dev"
        assert item["independent"] is False
        assert item["author"] == AUTHOR
        assert item["provenance"]["generator"].endswith("generate_dev.py")
        assert "NOT ground truth" in item["provenance"]["label_status"]
        assert item["label_status"] == "proposal_for_annotators_not_ground_truth"
        assert item["group_id"].startswith("v2g-")
        # no `label` key: the stock harness must not treat proposals as ground truth
        assert "label" not in item


def test_every_unit_has_a_consistent_proposed_label_and_action():
    for uid, _, unit in all_units():
        assert unit["proposed_label"] in (0, 1), uid
        assert unit["proposed_expected_action"] in ACTIONS, uid
        assert (unit["proposed_label"] == 0) == (
            unit["proposed_expected_action"] == "allow_for_review"), uid
        assert unit["proposed_policy_clause"].startswith("P-"), uid
        assert len(unit["proposed_rationale"]) >= 10, uid


def test_policy_clauses_cited_by_items_exist():
    clauses = json.loads((V2 / "annotation" / "policy_clauses.json").read_text("utf-8"))
    for uid, _, unit in all_units():
        assert unit["proposed_policy_clause"] in clauses, uid


def test_every_request_validates_against_the_public_schema():
    for _uid, _, unit in all_units():
        PaymentRequest.model_validate(unit["request"])


def test_ids_are_unique_and_request_ids_only_repeat_for_deliberate_replays():
    ids = [i["id"] for i in CASES + EPISODES]
    assert len(ids) == len(set(ids))
    case_rids = [c["request"]["request_id"] for c in CASES]
    assert len(case_rids) == len(set(case_rids))
    owners: dict[str, set[str]] = {}
    for e in EPISODES:
        for s in e["steps"]:
            owners.setdefault(s["request"]["request_id"], set()).add(e["id"])
    assert all(len(v) == 1 for v in owners.values()), "request ids cross episodes"
    assert not set(case_rids) & set(owners)
    repeated = {e["id"] for e in EPISODES
                if len({s["request"]["request_id"] for s in e["steps"]}) < len(e["steps"])}
    assert repeated and all(e_id in {e["id"] for e in EPISODES if e["risk_type"] == "replay"}
                            for e_id in repeated)


def test_groups_never_cross_splits_and_variations_share_groups():
    groups = Counter(c["group_id"] for c in CASES)
    assert max(groups.values()) >= 3            # variations of one scenario stay together
    benchmark.load(V2)                           # raises if a group crossed splits


def test_coverage_of_risk_types_wording_slices_and_languages():
    risk_types = {unit.get("risk_type", item["risk_type"]) for _, item, unit in all_units()}
    assert REQUIRED_RISK_TYPES <= risk_types, REQUIRED_RISK_TYPES - risk_types
    wordings = Counter(c["wording"] for c in CASES)
    assert {"plain", "paraphrased", "non_english", "obfuscated"} <= set(wordings)
    assert min(wordings[w] for w in ("paraphrased", "non_english", "obfuscated")) >= 10
    langs = {unit.get("language", item["language"]) for _, item, unit in all_units()} - {"en"}
    assert len(langs) >= 3, langs
    # injections exist in every wording slice
    inj = {c["wording"] for c in CASES if c["risk_type"] == "injection"}
    assert inj == {"plain", "paraphrased", "non_english", "obfuscated"}


def test_hard_negatives_are_at_least_30_percent_of_legitimate_units():
    legit = [(item, unit) for _, item, unit in all_units() if unit["proposed_label"] == 0]
    hard = [1 for item, unit in legit
            if unit.get("risk_type", item["risk_type"]) == "legit_hard_negative"
            or item.get("proposed_difficulty") == "hard"]
    assert len(hard) / len(legit) >= 0.30


def test_episode_mechanics_cover_the_multi_request_attacks():
    mech = {m for e in EPISODES for m in e["mechanics"]}
    assert {"approval_window", "approval_terms_binding", "attempted_spend",
            "unverified_outcome", "replay", "merchant_window", "rolling_window",
            "reservation_ttl", "owner_sharing", "duplicate_window",
            "zero_decimal_currency", "multi_currency"} <= mech


def test_episode_spend_policies_are_operator_configurations():
    for e in EPISODES:
        sp = e["spend_policy"]
        assert sp["configuration"] == "operator" and "default_agent" not in sp
        agents = set(sp["agents"])
        for s in e["steps"]:
            assert s["request"]["agent_id"] in agents, e["id"]


def test_extended_ops_are_flagged_as_not_v1_harness_compatible():
    for e in EPISODES:
        ext = any(k in o for s in e["steps"] for o in s.get("then", [])
                  for k in ("advance_seconds", "target_step", "terms_override"))
        assert e["harness_v1_compatible"] is (not ext), e["id"]
        for s in e["steps"]:
            for o in s.get("then", []):
                assert o["proposed_op_outcome"] in ("accepted", "refused")


def test_generator_source_has_no_invisible_characters():
    """Zero-width and homoglyph characters must be written as escapes so reviewers can
    see them."""
    import unicodedata
    src = (V2 / "generate_dev.py").read_text("utf-8")
    assert not [ch for ch in src if unicodedata.category(ch) == "Cf"]


@pytest.mark.parametrize("path", sorted((PACKAGE_ROOT / "benchmarks").rglob("*.jsonl")),
                         ids=lambda p: str(p.relative_to(PACKAGE_ROOT)))
def test_no_locked_items_are_committed_anywhere_under_benchmarks(path: Path):
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            assert not (isinstance(row, dict) and row.get("split") == "locked"), path
