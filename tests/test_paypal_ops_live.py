"""LIVE SANDBOX read tests. No mutating operations; clean skip without credentials."""
from datetime import datetime, timedelta, timezone
import os

import pytest

from agentpal.providers.config import load_config_from_env
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider
from agentpal.providers.paypal_reporting import PayPalReporting
from agentpal.tools.paypal_webhook_setup import WebhookSetup

pytestmark = pytest.mark.skipif(not (os.environ.get('PAYPAL_SANDBOX_CLIENT_ID') and
    (os.environ.get('PAYPAL_SANDBOX_CLIENT_SECRET') or os.environ.get('PAYPAL_SANDBOX_SECRET'))),
    reason='LIVE PayPal Sandbox: credentials absent')


def test_live_reporting_and_webhook_list_are_real_sandbox_reads():
    provider = PayPalSandboxProvider(load_config_from_env())
    try:
        reader = PayPalReporting(provider)
        now = datetime.now(timezone.utc)
        rows = reader.transactions(now - timedelta(days=1), now)
        assert isinstance(rows, list)
        assert all(isinstance(row, dict) for row in rows)
        assert isinstance(WebhookSetup(reader).list(), list)
        assert provider.environment == 'sandbox'
    finally:
        provider.http_client.close()
