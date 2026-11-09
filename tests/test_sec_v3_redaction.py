"""The ledger redactor removes AgentPal credentials (fix applied by the lead at integration)."""
import secrets



def test_store_redacts_standalone_platform_and_legacy_credentials():
    from agentpal.store import redact
    for prefix in ("apk_", "apo_", "apt_"):
        token = prefix + secrets.token_hex(12) + "_" + secrets.token_urlsafe(32)
        assert token not in redact("accidental paste: " + token)
