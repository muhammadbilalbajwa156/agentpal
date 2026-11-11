"""Agent-only MCP contract against the real platform; no provider credentials needed."""
import asyncio
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from agentpal.mcp_server import MCPServer
from test_platform_fixtures import ALL_PAY
from test_platform_workspace import bearer, owner, payment, world


def message(method, params=None, ident=1):
    value = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        value["params"] = params
    return value


def initialize(server):
    return rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})


def rpc(server, method, params=None):
    return asyncio.run(server.handle(message(method, params)))


def call(server, name, arguments=None):
    return rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})


def data(reply):
    return json.loads(reply["result"]["content"][0]["text"])


@pytest.fixture
def platform(tmp_path):
    with world(tmp_path) as (c, deps):
        token = owner(deps)
        reg = c.post("/v1/agents", headers=bearer(token),
                     json={"name": "MCP agent", "capabilities": ALL_PAY}).json()
        key = reg["api_key"]
        assert c.post("/v1/wallets", headers=bearer(token), json={
            "agent_id": reg["agent"]["agent_id"], "currency": "USD",
            "monthly_budget": "100.00", "per_transaction_limit": "25.00"}).status_code == 201
        server = MCPServer(environ={"AGENTPAL_AGENT_KEY": key},
                           transport=httpx.ASGITransport(app=c.app))
        yield server, c, token, key
        asyncio.run(server.aclose())


def test_initialize_and_exact_agent_tool_set(platform):
    server, _, _, _ = platform
    result = initialize(server)["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "agentpal"
    assert result["capabilities"] == {"tools": {}}
    assert asyncio.run(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    assert rpc(server, "ping")["result"] == {}
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_wallet", "request_payment", "get_payment_request",
        "list_my_payment_requests", "execute_approved_payment"}
    for tool in tools:
        assert "human" in tool["description"].lower()
        assert "approv" in tool["description"].lower()
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False


def test_real_request_approval_execution_reads_and_retry(platform):
    server, c, token, _ = platform
    initialize(server)
    assert data(call(server, "get_wallet"))["wallets"][0]["is_real_money"] is False
    args = payment()
    created = data(call(server, "request_payment", args))
    assert created["status"] == "awaiting_approval"
    assert created["message"] == "A human must approve these exact terms in AgentPal before any money moves."
    rid = created["request_id"]
    assert data(call(server, "request_payment", dict(reversed(list(args.items())))))["request_id"] == rid
    refused = call(server, "execute_approved_payment", {"request_id": rid})["result"]
    assert refused["isError"] is True
    assert "approv" in refused["content"][0]["text"].lower()
    assert c.post(f"/v1/payment-requests/{rid}/approve", headers=bearer(token),
                  json={"terms_sha256": created["terms_sha256"]}).status_code == 200
    assert data(call(server, "execute_approved_payment", {"request_id": rid}))["status"] == "provider_pending"
    view = data(call(server, "get_payment_request", {"request_id": rid}))
    assert view["status"] == "provider_pending"
    assert view["terms"]["amount"] == "1.00"
    assert "verification" in view
    assert len(data(call(server, "list_my_payment_requests"))["payment_requests"]) == 1


@pytest.mark.parametrize("params", [None, [], {}, {"name": "approve"},
    {"name": "request_payment", "arguments": {"amount": 1.0}},
    {"name": "get_wallet", "arguments": {"url": "/approve"}},
    {"name": "get_payment_request", "arguments": {"request_id": "../approve"}},
    {"name": "request_payment", "arguments": {**payment(), "approve": True}},
    {"name": "request_payment", "arguments": {**payment(), "idempotency_key": "bad\nheader"}},
    {"name": "request_payment", "arguments": {**payment(), "currency": "usd"}},
    {"name": "get_wallet", "arguments": [], "extra": True}])
def test_bad_tool_params_do_not_call_api(platform, params):
    server, _, _, _ = platform
    initialize(server)
    assert rpc(server, "tools/call", params)["error"]["code"] == -32602


def test_jsonrpc_errors_and_notifications(platform):
    server, _, _, key = platform
    assert rpc(server, "tools/list")["error"]["code"] == -32600
    assert rpc(server, "initialize", {})["error"]["code"] == -32602
    initialize(server)
    assert rpc(server, "unknown-" + key)["error"]["code"] == -32601
    assert key not in json.dumps(rpc(server, "unknown-" + key))
    for bad in [[], None, {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": {}, "method": "ping"}]:
        assert asyncio.run(server.handle(bad))["error"]["code"] == -32600
    # A notification must never cause a payment or generate a JSON-RPC response.
    assert asyncio.run(server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {
        "name": "request_payment", "arguments": payment()}})) is None
    assert data(call(server, "list_my_payment_requests"))["payment_requests"] == []


def test_protocol_request_metadata_is_accepted_but_must_be_an_object(platform):
    server, _, _, _ = platform
    initialize(server)
    assert rpc(server, "ping", {"_meta": {"progressToken": "probe"}})["result"] == {}
    assert len(rpc(server, "tools/list", {"_meta": {}})["result"]["tools"]) == 5
    for method in ("ping", "tools/list"):
        assert rpc(server, method, {"_meta": []})["error"]["code"] == -32602


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 500, 302])
def test_api_errors_are_plain_tool_errors_and_never_follow_redirects(status):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(status, json={"error": "apk_synthetic_secret"},
                              headers={"Location": "https://other.invalid/"})
    server = MCPServer(environ={"AGENTPAL_AGENT_KEY": "apk_synthetic_secret"},
                       transport=httpx.MockTransport(respond))
    try:
        initialize(server)
        result = call(server, "get_wallet")["result"]
        assert result["isError"] is True
        assert "apk_" not in json.dumps(result)
        assert len(result["content"][0]["text"]) > 20
        assert len(seen) == 1
        assert seen[0].headers["Authorization"] == "Bearer apk_synthetic_secret"
        assert seen[0].extensions["timeout"]["read"] > 0
    finally:
        asyncio.run(server.aclose())


def test_keys_are_redacted_even_from_success_and_transport_errors():
    key = "apk_synthetic_secret"
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"nested": [key, "apk_other_secret"]})
        raise httpx.ConnectError(key, request=request)
    server = MCPServer(environ={"AGENTPAL_AGENT_KEY": key}, transport=httpx.MockTransport(respond))
    try:
        initialize(server)
        assert "apk_" not in json.dumps(call(server, "get_wallet"))
        result = call(server, "request_payment", payment())["result"]
        assert result["isError"] is True
        assert key not in json.dumps(result)
        assert "unknown" in result["content"][0]["text"].lower()
    finally:
        asyncio.run(server.aclose())


def test_stable_hash_across_restarts_explicit_key_and_only_execute_route():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "awaiting_approval", "request_id": "request_1",
            "payer_action_url": "https://www.sandbox.paypal.com/x", "verification": None})
    for _ in range(2):
        server = MCPServer(environ={"AGENTPAL_AGENT_KEY": "apk_synthetic_secret",
                                   "AGENTPAL_BASE_URL": "http://127.0.0.1:8000/v1/"},
                           transport=httpx.MockTransport(respond))
        try:
            initialize(server)
            call(server, "request_payment", {**payment(), "product_query": "Printer paper",
                                            "business_justification": "Office supplies"})
        finally:
            asyncio.run(server.aclose())
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
    server = MCPServer(environ={"AGENTPAL_AGENT_KEY": "apk_synthetic_secret"},
                       transport=httpx.MockTransport(respond))
    try:
        initialize(server)
        call(server, "request_payment", {**payment(), "idempotency_key": "logical-purchase-1"})
        assert seen[-1].headers["Idempotency-Key"] == "logical-purchase-1"
        assert "idempotency_key" not in json.loads(seen[-1].content)
        view = data(call(server, "get_payment_request", {"request_id": "request_1"}))
        assert view["payer_action_url"] == "https://www.sandbox.paypal.com/x"
        seen.clear()
        call(server, "execute_approved_payment", {"request_id": "request_1"})
        assert [(r.method, r.url.path) for r in seen] == [("POST", "/v1/payment-requests/request_1/execute")]
    finally:
        asyncio.run(server.aclose())


@pytest.mark.parametrize("content", [b"not json", b"[]"])
def test_unreadable_api_response_is_tool_error(content):
    server = MCPServer(environ={"AGENTPAL_AGENT_KEY": "apk_synthetic_secret"},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    try:
        initialize(server)
        assert call(server, "get_wallet")["result"]["isError"] is True
    finally:
        asyncio.run(server.aclose())


@pytest.mark.parametrize("env", [{}, {"AGENTPAL_AGENT_KEY": "apo_owner_value"},
    {"AGENTPAL_AGENT_KEY": "apk_synthetic_secret", "AGENTPAL_BASE_URL": "ftp://example.test"},
    {"AGENTPAL_AGENT_KEY": "apk_synthetic_secret", "AGENTPAL_BASE_URL": "http://user:pass@example.test"}])
def test_environment_requires_agent_credential_and_http_origin(env):
    with pytest.raises(ValueError) as exc:
        MCPServer(environ=env)
    assert "apo_owner_value" not in str(exc.value)
    assert "pass" not in str(exc.value)


def test_stdio_subprocess_pipes_parse_errors_and_clean_stdout():
    messages = [message("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "pipe-test", "version": "1"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, message("tools/list"),
        message("ping"), message("tools/call", {"name": "approve", "arguments": {}})]
    proc = subprocess.run([sys.executable, "-m", "agentpal.mcp_server"],
        input="{invalid\n" + "\n".join(json.dumps(m) for m in messages) + "\n",
        text=True, capture_output=True, timeout=20,
        env={**os.environ, "AGENTPAL_AGENT_KEY": "apk_synthetic_secret"})
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == 5
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"]["serverInfo"]["name"] == "agentpal"
    assert len(replies[2]["result"]["tools"]) == 5
    assert replies[3]["result"] == {}
    assert replies[4]["error"]["code"] == -32602
    assert proc.stderr == ""
    assert "apk_" not in proc.stdout


def test_stdio_subprocess_through_local_http_to_real_platform(platform):
    _, client, _, key = platform
    class Bridge(BaseHTTPRequestHandler):
        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = client.request(self.command, self.path, content=body,
                headers={"Authorization": self.headers.get("Authorization", ""),
                         "Content-Type": "application/json",
                         "Idempotency-Key": self.headers.get("Idempotency-Key", "")})
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Bridge) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            messages = [message("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "stdio-http", "version": "1"}}),
                message("tools/call", {"name": "get_wallet"}),
                message("tools/call", {"name": "request_payment", "arguments": payment()}),
                message("tools/call", {"name": "request_payment", "arguments": payment()}),
                message("tools/call", {"name": "list_my_payment_requests"})]
            proc = subprocess.run([sys.executable, "-m", "agentpal.mcp_server"],
                input="\n".join(json.dumps(m) for m in messages) + "\n", text=True,
                capture_output=True, timeout=20, env={**os.environ, "AGENTPAL_AGENT_KEY": key,
                    "AGENTPAL_BASE_URL": f"http://127.0.0.1:{http.server_port}"})
            assert proc.returncode == 0, proc.stderr
            replies = [json.loads(line) for line in proc.stdout.splitlines()]
            assert len(replies) == 5
            assert data(replies[1])["wallets"]
            assert data(replies[2])["status"] == "awaiting_approval"
            assert data(replies[2])["request_id"] == data(replies[3])["request_id"]
            assert len(data(replies[4])["payment_requests"]) == 1
            assert key not in proc.stdout + proc.stderr
            assert proc.stderr == ""
        finally:
            http.shutdown()
            thread.join(timeout=5)
