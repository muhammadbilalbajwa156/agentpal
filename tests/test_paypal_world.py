"""Scenario world exercises the real app and deterministic control layer."""
import asyncio
import json


def test_offline_world_runs_all_control_scenarios(tmp_path):
    from agentpal.demo.paypal_world import run_world
    result = asyncio.run(run_world(report_dir=tmp_path))
    assert result['mode'] == 'offline_mocked_paypal'
    assert result['is_real_money'] is False
    cases = {r['scenario']: r for r in result['scenarios']}
    assert cases['normal_purchase']['state'] == 'provider_confirmed'
    assert cases['owner_rejects_risky']['state'] == 'rejected'
    assert cases['buyer_never_approves']['state'] == 'provider_pending'
    assert cases['duplicates']['capture_posts'] == 1
    assert cases['forged_webhook']['http_status'] == 400
    assert cases['over_budget']['state'] == 'rejected'
    assert result['provider_calls']['create'] == 2
    assert result['provider_calls']['capture'] == 1
    assert result['verified_outcomes'] == 1
    assert len(result['reconciliation']['rows']) == 4
    artifact = json.loads((tmp_path / 'paypal-world.json').read_text())
    assert artifact == result
    assert 'WORLD-ORDER-' not in json.dumps(result)


def test_world_buyer_failure_never_claims_completion(tmp_path):
    from agentpal.demo.paypal_world import run_world
    # The offline mode drives its own buyer fixture; no browser or credential is needed.
    result = asyncio.run(run_world(report_dir=tmp_path))
    pending = next(r for r in result['scenarios'] if r['scenario'] == 'buyer_never_approves')
    assert pending['capture_http_status'] == 409
    assert result['live_webhooks'].startswith('UNVERIFIED')
