"""Owner decisions 4, 6 and 9: multi-request episodes (seed AgentPal-Bench, dev split)
replayed through the real API with a controlled server clock.

Every seed episode must pass rules-only; this guards approval-hold expiry, reservation TTL,
attempted spend, unverified outcomes, structuring, replay and near-duplicates together.
The seed items are NOT independent (written by the rule author): this is a regression
check, not a measurement of generalization.
"""

from __future__ import annotations

import pytest

from agentpal import benchmark
from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import PACKAGE_ROOT, Settings

SEED = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v1"
_, EPISODES = benchmark.load(SEED)


@pytest.mark.parametrize("episode", EPISODES, ids=[e["id"] for e in EPISODES])
def test_seed_episode_passes_rules_only(episode):
    results = benchmark.replay_episode(episode, [HeuristicAnalyzer()],
                                       Settings(db_path=":memory:"))
    failed = [r for r in results if not r["passed"]]
    assert not failed, failed


def test_decision_6_episodes_are_present():
    ids = {e["id"] for e in EPISODES}
    assert {"ep-approval-hold-expiry", "ep-unapproved-reservation-ttl",
            "ep-attempted-payment-keeps-hold", "ep-unverified-outcome-not-completed"} <= ids
