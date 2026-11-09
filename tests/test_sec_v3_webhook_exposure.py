"""The single public route exception keeps every other remote route closed."""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings, UnsafeConfiguration
from agentpal.platform.router import WEBHOOK_PATH
from test_platform_fixtures import build_world, sign
from test_sec_v3_tls import certificate


def public_app(tmp_path, **overrides):
    world = build_world()
    cert, key = certificate(tmp_path)
    values = dict(auth_mode="required", public_webhook_enabled=True,
                  public_webhook_hosts=["hooks.example.test"], tls_certfile=cert, tls_keyfile=key,
                  rate_limit_db_path=str(tmp_path / "limits.db"))
    values.update(overrides)
    settings = Settings(**values)
    app = create_app(service=world.risk, settings=settings, platform=world.deps,
                     registry=TokenRegistry())
    return world, app


def remote(app, scheme="https", host="hooks.example.test", peer="203.0.113.4"):
    return TestClient(app, base_url=f"{scheme}://{host}", client=(peer, 50000))


def test_public_post_preserves_bytes_and_signature(tmp_path):
    w, app = public_app(tmp_path)
    raw = b'{"id":"WH-public",   "order":"ORDER-public"}'
    with remote(app) as client:
        result = client.post(WEBHOOK_PATH, content=raw,
                             headers={"x-fake-signature": sign(raw), "Authorization": "Bearer ignored"})
    assert result.status_code == 200
    import hashlib
    assert w.sink.events[0].raw_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("method,path", [("GET", WEBHOOK_PATH), ("PUT", WEBHOOK_PATH),
    ("OPTIONS", WEBHOOK_PATH), ("POST", WEBHOOK_PATH + "/"), ("POST", WEBHOOK_PATH + "/x"),
    ("POST", WEBHOOK_PATH.replace("paypal", "%70aypal")), ("GET", "/v1/health"),
    ("GET", "/health"), ("GET", "/docs"), ("POST", "/assess"), ("GET", "/v1/agents")])
def test_only_exact_remote_post_is_admitted(tmp_path, method, path):
    _, app = public_app(tmp_path)
    with remote(app) as client:
        assert client.request(method, path, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("scheme,host,status,error", [
    ("http", "hooks.example.test", 403, "webhook_https_required"),
    ("https", "evil.test", 400, "forbidden_host"),
])
def test_scheme_host_and_forwarding_spoofs_are_refused(tmp_path, scheme, host, status, error):
    w, app = public_app(tmp_path)
    raw = b'{"id":"WH-forwarding", "order":"ORDER-forwarding"}'
    with remote(app, scheme=scheme, host=host) as client:
        result = client.post(WEBHOOK_PATH, content=raw, headers={"x-fake-signature": sign(raw),
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1;proto=https"})
    assert result.status_code == status
    assert result.json() == {"error": error}
    assert w.sink.events == []


def test_validly_signed_remote_http_event_is_refused_by_https_gate(tmp_path):
    w, app = public_app(tmp_path)
    raw = b'{"id":"WH-http", "order":"ORDER-http"}'
    headers = {"x-fake-signature": sign(raw)}
    # This is an acceptable event: only its transport is insecure.
    assert w.provider.verify_webhook(headers, raw).verified
    with remote(app, scheme="http") as client:
        result = client.post(WEBHOOK_PATH, content=raw, headers=headers)
    assert result.status_code == 403
    assert result.json() == {"error": "webhook_https_required"}
    assert w.sink.events == []


def test_public_is_off_by_default_and_requires_dependencies(tmp_path):
    w = build_world()
    with remote(w.app) as client:
        assert client.post(WEBHOOK_PATH).status_code == 403
    cert, key = certificate(tmp_path)
    settings = Settings(auth_mode="required", public_webhook_enabled=True,
        public_webhook_hosts=["hooks.example.test"], tls_certfile=cert, tls_keyfile=key,
        rate_limit_db_path=str(tmp_path / "limits.db"))
    with pytest.raises(UnsafeConfiguration):
        create_app(service=w.risk, settings=settings)
    w.deps.event_sink = None
    with pytest.raises(UnsafeConfiguration):
        create_app(service=w.risk, settings=settings, platform=w.deps)


def test_bearer_cannot_replace_signature_and_bad_event_never_reaches_sink(tmp_path):
    w, app = public_app(tmp_path)
    _, key = w.register()
    with remote(app) as client:
        response = client.post(WEBHOOK_PATH, content=b'{}',
                               headers={"Authorization": "Bearer " + key})
    assert response.status_code == 400
    assert w.sink.events == []


@pytest.mark.parametrize("global_limit,peer_limit,second_peer", [(1, 20, "203.0.113.5"),
                                                                  (20, 1, "203.0.113.4")])
def test_shared_limits_precede_any_body_read(tmp_path, global_limit, peer_limit, second_peer):
    _, app = public_app(tmp_path, webhook_rate_limit_global_per_minute=global_limit,
                       webhook_rate_limit_per_peer_per_minute=peer_limit)
    with remote(app) as client:
        assert client.post(WEBHOOK_PATH, content=b'{}').status_code == 400
    sent = []
    async def no_body():
        raise AssertionError("limited body was read")
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "https", "path": WEBHOOK_PATH,
        "raw_path": WEBHOOK_PATH.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"hooks.example.test")], "client": (second_peer, 1),
        "server": ("hooks.example.test", 443)}
    asyncio.run(app(scope, no_body, send))
    assert sent[0]["status"] == 429


@pytest.mark.parametrize("general,expected_cap", [(100, 100), (1_000_000, 512 * 1024)])
def test_public_streaming_body_cap(tmp_path, general, expected_cap):
    w, app = public_app(tmp_path, max_request_bytes=general)
    with remote(app) as client:
        response = client.post(WEBHOOK_PATH, content=iter([b"x" * expected_cap, b"x"]))
    assert response.status_code == 413
    assert w.sink.events == []


def test_initial_public_limiter_outage_fails_503(tmp_path):
    w, app = public_app(tmp_path, rate_limit_db_path=str(tmp_path / "missing" / "l.db"))
    with remote(app) as client:
        assert client.post(WEBHOOK_PATH, content=b'{}').status_code == 503
    assert w.sink.events == []


def test_duplicate_host_query_and_forwarded_proto_variants_stay_closed(tmp_path):
    _, app = public_app(tmp_path)
    with remote(app) as client:
        assert client.post(WEBHOOK_PATH, headers=[("host", "hooks.example.test"),
            ("host", "evil.test")]).status_code == 400
        assert client.post(WEBHOOK_PATH + "?route=other").status_code == 403
        response = client.post(WEBHOOK_PATH, headers={"X-Forwarded-Proto": "http"})
        assert response.status_code == 400  # socket HTTPS; still requires signature


@pytest.mark.parametrize("override", [{"rate_limit_backend": "memory"},
    {"rate_limit_db_path": ":memory:"}, {"public_webhook_hosts": ["*"]},
    {"public_webhook_hosts": []}, {"auth_mode": "disabled_insecure_dev"}])
def test_unsafe_public_settings_are_refused(tmp_path, override):
    with pytest.raises(UnsafeConfiguration):
        public_app(tmp_path, **override)


def test_remote_websocket_is_closed_with_policy_violation(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    _, app = public_app(tmp_path)
    with remote(app) as client:
        with pytest.raises(WebSocketDisconnect) as failure:
            with client.websocket_connect(WEBHOOK_PATH):
                pytest.fail("remote websocket admitted")
    assert failure.value.code == 1008


def test_signed_remote_root_path_is_denied_before_body_read(tmp_path):
    w, app = public_app(tmp_path)
    raw = b'{"id":"WH-root", "order":"ORDER-root"}'

    async def dispatch(root_path):
        sent, reads = [], 0
        async def receive():
            nonlocal reads
            reads += 1
            return {"type": "http.request", "body": raw, "more_body": False}
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": WEBHOOK_PATH,
            "raw_path": WEBHOOK_PATH.encode(), "query_string": b"", "root_path": root_path,
            "headers": [(b"host", b"hooks.example.test"),
                        (b"x-fake-signature", sign(raw).encode())],
            "client": ("203.0.113.4", 50000), "server": ("hooks.example.test", 443)}
        await app(scope, receive, send)
        return sent, reads

    accepted, _ = asyncio.run(dispatch(""))
    assert accepted[0]["status"] == 200 and len(w.sink.events) == 1
    w.sink.events.clear()
    rejected, reads = asyncio.run(dispatch("/mounted"))
    assert rejected[0]["status"] == 403
    assert json.loads(b"".join(message.get("body", b"") for message in rejected)) == {
        "error": "forbidden_non_loopback_client"}
    assert reads == 0
    assert w.sink.events == []


def test_signed_remote_duplicate_hosts_are_refused_by_host_gate(tmp_path):
    w, app = public_app(tmp_path)
    raw = b'{"id":"WH-duplicate-host", "order":"ORDER-duplicate-host"}'
    signature = sign(raw)
    assert w.provider.verify_webhook({"x-fake-signature": signature}, raw).verified
    with remote(app) as client:
        result = client.post(WEBHOOK_PATH, content=raw, headers=[
            ("host", "hooks.example.test"), ("host", "evil.test"),
            ("x-fake-signature", signature)])
    assert result.status_code == 400
    assert result.json() == {"error": "forbidden_host"}
    assert w.sink.events == []
