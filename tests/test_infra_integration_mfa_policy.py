"""Lead integration (Mission 3): the operator's MFA-for-approval setting reaches the
real /v1 platform wiring (A3 setting -> A1/WS4 wiring)."""

import pytest

from agentpal.config import Settings
from agentpal.platform.wiring import build_platform


@pytest.mark.parametrize("required", [True, False])
def test_mfa_approval_policy_is_propagated(tmp_path, required):
    settings = Settings(environment="development", db_path=str(tmp_path / "risk.db"),
                        analyzer_chain=["heuristic"], require_mfa_for_approval=required)
    deps = build_platform(settings=settings, environ={})
    try:
        assert deps.require_mfa_for_approval is required
    finally:
        deps.close()
