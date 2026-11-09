"""P0.7b regression: the request-size limit is enforced while streaming.

Before the fix, the middleware awaited `request.body()`, buffering an entire chunked
body (no Content-Length) before checking its size.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import BodySizeLimit, create_app
from agentpal.config import Settings, load_policy
from agentpal.service import RiskService
from agentpal.store import AssessmentStore

from conftest import VALID

LIMIT = 4096


def client():
    s = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()])
    return TestClient(create_app(service=s, settings=Settings(max_request_bytes=LIMIT)))


def test_middleware_stops_reading_an_endless_stream():
    """Unit-level: an infinite body is abandoned shortly after the limit is crossed."""
    calls = {"receive": 0, "app": 0}
    sent = []

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    async def send(msg):
        sent.append(msg)

    async def app(scope, receive, send):
        calls["app"] += 1

    mw = BodySizeLimit(app, max_bytes=LIMIT)
    scope = {"type": "http", "method": "POST", "headers": [], "path": "/assess"}
    asyncio.run(mw(scope, receive, send))
    assert calls["app"] == 0
    assert calls["receive"] <= LIMIT // 1024 + 1
    assert sent[0]["status"] == 413


def test_declared_oversize_rejected_without_reading_body():
    calls = {"receive": 0}
    sent = []

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    async def app(scope, receive, send):  # pragma: no cover - must not be reached
        raise AssertionError("app called")

    scope = {"type": "http", "method": "POST", "path": "/assess",
             "headers": [(b"content-length", str(10 ** 10).encode())]}
    asyncio.run(BodySizeLimit(app, max_bytes=LIMIT)(scope, receive, send))
    assert calls["receive"] == 0 and sent[0]["status"] == 413


def test_oversize_with_content_length():
    with client() as c:
        r = c.post("/assess", content=b"x" * (LIMIT + 1),
                   headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_oversize_chunked_without_content_length():
    def body():
        for _ in range(64):
            yield b"x" * 1024

    with client() as c:
        r = c.post("/assess", content=body(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_exactly_at_limit_is_not_rejected_for_size():
    payload = json.dumps(VALID).encode()
    padded = payload[:-1] + b" " * (LIMIT - len(payload)) + b"}"
    assert len(padded) == LIMIT
    with client() as c:
        r = c.post("/assess", content=padded, headers={"content-type": "application/json"})
    assert r.status_code == 200


@pytest.mark.parametrize("headers,body,status", [
    ({"content-length": "abc"}, b"{}", 400),            # malformed Content-Length
    ({"content-type": "application/json"}, b"{not json", 422),
    ({"content-type": "text/plain"}, b"hello", 422),
])
def test_malformed_payloads(headers, body, status):
    with client() as c:
        r = c.post("/assess", content=body, headers=headers)
    assert r.status_code == status


def test_real_server_rejects_chunked_oversize_stream():
    """End to end against uvicorn with a raw chunked HTTP/1.1 request."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "AGENTPAL_DB_PATH": ":memory:", "AGENTPAL_ANALYZER_CHAIN": "heuristic",
           "AGENTPAL_MAX_REQUEST_BYTES": str(LIMIT), "AGENTPAL_LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentpal.api:create_app", "--factory",
         "--port", str(port), "--log-level", "warning"],
        env=env, cwd=os.path.dirname(os.path.dirname(__file__)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"POST /assess HTTP/1.1\r\nHost: x\r\nContent-Type: application/json"
                         b"\r\nTransfer-Encoding: chunked\r\n\r\n")
            chunk = b"x" * 1024
            sent = 0
            try:
                for _ in range(2048):  # up to 2 MiB; the server must answer long before
                    sock.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            sock.settimeout(10)
            response = sock.recv(4096)
        assert response.startswith(b"HTTP/1.1 413"), response[:80]
    finally:
        proc.terminate()
        proc.wait(10)
