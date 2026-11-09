"""Probe boundary tests. Fixtures here are not APIMatic-generated SDKs."""

import io
import json
from types import SimpleNamespace

import pytest

from apimatic import generated_probe as probe


@pytest.mark.parametrize("base_url", [
    "https://api-m.paypal.com", "http://example.com:8000", "http://127.0.0.1",
    "http://user:password@127.0.0.1:8000", "http://127.0.0.1:8000/path",
    "http://127.0.0.1:8000?next=external", "http://127.0.0.1:8000#fragment",
])
def test_non_disposable_origin_refused_before_loading_any_sdk(monkeypatch, base_url):
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO(json.dumps({"base_url": base_url})))
    monkeypatch.setattr(probe, "find_client", lambda *a: pytest.fail("unsafe origin reached SDK"))
    with pytest.raises(RuntimeError, match="loopback"):
        probe.main()


def test_nested_generated_response_conversion():
    result = SimpleNamespace(body=SimpleNamespace(agents=[SimpleNamespace(agent_id="agt_test")]))
    assert probe.plain(result) == {"agents": [{"agent_id": "agt_test"}]}


def test_unknown_generated_surface_never_falls_back():
    with pytest.raises(RuntimeError, match="operation not found"):
        probe.operation(SimpleNamespace(), "getMe", "me")
    with pytest.raises(RuntimeError, match="scheme parameter not found"):
        probe.client_for(lambda base_url, retry_options: None,
                         "http://127.0.0.1:8000", "AgentApiKey", "test-only-key")
