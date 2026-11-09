"""Offline SDK/MockTransport regressions: no credentials or browser credits."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from kernel import Kernel

from agentpal.tools.browser_contracts import (
    MAX_HTML_BYTES, MAX_SCREENSHOT_BYTES, MAX_TEXT_CHARS, BrowserError, BrowserProvider,
)
from agentpal.tools.kernel_browser import CAPTURE_JS, KernelBrowserProvider

URL = "https://shop.example/product"
PNG = b"\x89PNG\r\n\x1a\n" + b"offline-image"
KEY = hashlib.sha256(b"kernel-offline-test").hexdigest()


def public_dns(host, port):
    return ["93.184.216.34"]


def observed(**changes):
    data = dict(final_url=URL, redirect_chain=[URL], http_status=200, title="Product",
                html="<html>price 12.00</html>", html_truncated=False,
                visible_text="price 12.00")
    data.update(changes)
    return {"success": True, "result": data}


class Backend:
    def __init__(self):
        self.calls = []
        self.result = observed()
        self.errors = {}
        self.handlers = {}
        self.deleted = threading.Event()

    def __call__(self, request):
        path = request.url.path
        step = ("create" if path == "/browsers" else
                "delete" if request.method == "DELETE" else
                "execute" if path.endswith("/execute") else "screenshot")
        self.calls.append((step, request))
        if step in self.handlers:
            return self.handlers[step](request)
        if step in self.errors:
            error = self.errors[step]
            if isinstance(error, Exception):
                raise error
            return httpx.Response(error, text=KEY)
        if step == "create":
            return httpx.Response(201, json={"session_id": "own_session"})
        if step == "execute":
            return httpx.Response(200, json=self.result)
        if step == "screenshot":
            return httpx.Response(200, content=PNG)
        self.deleted.set()
        return httpx.Response(204)

    @property
    def steps(self):
        return [s for s, _ in self.calls]


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def provider(backend):
    with httpx.Client(transport=httpx.MockTransport(backend)) as http:
        yield KernelBrowserProvider(client=Kernel(api_key=KEY, http_client=http),
                                    resolver=public_dns)


def failure(provider, kind, url=URL, **kwargs):
    with pytest.raises(BrowserError) as error:
        provider.capture(url, **kwargs)
    assert error.value.kind == kind
    assert KEY not in str(error.value)
    assert KEY not in "".join(traceback.format_exception(error.value))
    return error.value


def test_contract_success_hashes_utc_and_own_session_cleanup(provider, backend):
    assert isinstance(provider, BrowserProvider) and provider.configured()
    result = provider.capture(URL)
    assert result.provider == "kernel" and result.session_id == "own_session"
    assert result.requested_url == result.final_url == URL
    assert result.redirect_chain == (URL,)
    assert result.html == b"<html>price 12.00</html>"
    assert result.html_sha256 == hashlib.sha256(result.html).hexdigest()
    assert result.screenshot_png == PNG
    assert result.screenshot_sha256 == hashlib.sha256(PNG).hexdigest()
    assert result.captured_at.utcoffset().total_seconds() == 0
    assert result.replay_url is None
    assert backend.steps == ["create", "execute", "screenshot", "delete"]
    assert backend.calls[-1][1].url.path == "/browsers/own_session"
    create = json.loads(backend.calls[0][1].content)
    assert 10 <= create["timeout_seconds"] <= 60
    assert "start_url" not in create and "profile" not in create
    execute = json.loads(backend.calls[1][1].content)
    assert 1 <= execute["timeout_sec"] <= 30
    assert "domcontentloaded" in execute["code"]
    assert all(r.extensions["timeout"]["read"] <= 30 for _, r in backend.calls)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/", "http://10.0.0.1/", "https://localhost/",
    "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd",
    "javascript:process.exit()", "https://user:pass@shop.example/",
    "https://[::1]/", "https://2130706433/", "https://0x7f.1/",
    "https://shop.example:8443/", "https://shop.example\\@localhost/",
    "https://shop.example/\nsecret", "https://shop.example/" + KEY,
])
def test_blocked_targets_make_zero_kernel_calls(provider, backend, url):
    failure(provider, "blocked_target", url)
    assert backend.calls == []


@pytest.mark.parametrize("addresses", [["10.0.0.2"], ["93.184.216.34", "127.0.0.1"], []])
def test_dns_refusal_makes_zero_kernel_calls(provider, backend, addresses):
    provider._resolver = lambda host, port: addresses
    failure(provider, "blocked_target")
    assert backend.calls == []


def test_dns_errors_are_sanitized(provider, backend):
    def broken(host, port):
        raise OSError(KEY)
    provider._resolver = broken
    failure(provider, "blocked_target")
    assert backend.calls == []


def test_url_is_json_data_and_template_never_changes(provider, backend):
    malicious = 'https://x.example/";process.exit()//'
    backend.result = observed(final_url=malicious, redirect_chain=[malicious])
    provider.capture(malicious)
    code = json.loads(backend.calls[1][1].content)["code"]
    declaration, body = code.split("\n", 1)
    assert declaration.startswith("const input = ") and declaration.endswith(";")
    assert json.loads(declaration[len("const input = "):-1])["url"] == malicious
    assert body == CAPTURE_JS and malicious not in body
    assert not any(word in body for word in (
        ".click(", ".fill(", ".type(", ".press(", "eval(", "new Function"))


@pytest.mark.parametrize("step", ["create", "execute", "screenshot", "delete"])
@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (402, "quota"),
                                      (429, "quota"), (500, "unavailable"), (302, "unavailable")])
def test_error_mapping_and_no_sdk_retry(provider, backend, step, status, kind):
    backend.errors[step] = status
    failure(provider, kind)
    assert backend.steps.count(step) == 1
    assert backend.steps.count("delete") == (0 if step == "create" else 1)


@pytest.mark.parametrize("step", ["create", "execute", "screenshot"])
@pytest.mark.parametrize("error,kind", [(httpx.ReadTimeout(KEY), "timeout"),
                                      (httpx.ConnectError(KEY), "unavailable")])
def test_transport_errors_cleanup_and_sanitization(provider, backend, step, error, kind, caplog):
    caplog.set_level(logging.DEBUG)
    backend.errors[step] = error
    failure(provider, kind)
    assert backend.steps.count("delete") == (0 if step == "create" else 1)
    assert KEY not in caplog.text and KEY not in repr(provider)


@pytest.mark.parametrize("result", [None, [], {}, {"success": "true", "result": {}},
    {"success": True, "result": []}, observed(title=7), observed(html=None),
    observed(html_truncated="false"), observed(http_status=True), observed(http_status=999),
    observed(redirect_chain="bad"), observed(redirect_chain=[URL]*30),
    observed(final_url="file:///etc/passwd"), observed(redirect_chain=["https://127.0.0.1/", URL]),
    {"success": False, "error": KEY}, observed(title=KEY), observed(html=KEY),
])
def test_malformed_results_deleted(provider, backend, result):
    backend.result = result
    failure(provider, "invalid_response")
    assert backend.steps == ["create", "execute", "delete"]


def test_execute_timeout_result(provider, backend):
    backend.result = {"success": False, "error": "TimeoutError: " + KEY}
    failure(provider, "timeout")
    assert backend.steps == ["create", "execute", "delete"]


def test_bounds_and_hashes_on_exact_stored_bytes(provider, backend):
    backend.result = observed(html="€" * (MAX_HTML_BYTES//3 + 20),
                              visible_text="x" * (MAX_TEXT_CHARS+20), title="t"*400)
    result = provider.capture(URL)
    assert len(result.html) <= MAX_HTML_BYTES and result.html_truncated
    assert len(result.visible_text) == MAX_TEXT_CHARS and len(result.title) == 300
    assert result.html_sha256 == hashlib.sha256(result.html).hexdigest()


def test_remote_truncation_flag_preserved(provider, backend):
    backend.result = observed(html_truncated=True)
    assert provider.capture(URL).html_truncated


@pytest.mark.parametrize("size", [0, MAX_SCREENSHOT_BYTES+1])
def test_screenshot_invalid_or_oversized_is_not_partial_evidence(provider, backend, size):
    backend.handlers["screenshot"] = lambda r: httpx.Response(200, content=b"x"*size)
    failure(provider, "invalid_response")
    assert backend.steps[-1] == "delete"


def test_exact_screenshot_limit_is_allowed(provider, backend):
    image = PNG + b"x"*(MAX_SCREENSHOT_BYTES-len(PNG))
    backend.handlers["screenshot"] = lambda r: httpx.Response(200, content=image)
    assert len(provider.capture(URL).screenshot_png) == MAX_SCREENSHOT_BYTES


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def test_screenshot_stream_cut_off_before_full_download(provider, backend):
    stream = Stream([PNG, b"x"*MAX_SCREENSHOT_BYTES, b"must not be read"])
    backend.handlers["screenshot"] = lambda r: httpx.Response(200, stream=stream)
    failure(provider, "invalid_response")
    assert stream.reads == 2 and stream.closed
    assert backend.steps[-1] == "delete"


def test_http_error_body_never_read(provider, backend):
    stream = Stream([KEY.encode()] * 100)
    backend.handlers["execute"] = lambda r: httpx.Response(401, stream=stream)
    failure(provider, "auth")
    assert stream.reads == 0 and stream.closed
    assert backend.steps[-1] == "delete"


@pytest.mark.parametrize("step", ["create", "execute", "screenshot", "delete", "dns"])
def test_overall_deadline_and_eventual_cleanup(provider, backend, step):
    entered, release = threading.Event(), threading.Event()
    def delayed(request):
        entered.set()
        release.wait(3)
        backend.handlers.pop(step, None)
        response = backend(request)
        backend.calls.pop()
        return response
    if step == "dns":
        def slow_dns(host, port):
            entered.set()
            release.wait(3)
            return public_dns(host, port)
        provider._resolver = slow_dns
    else:
        backend.handlers[step] = delayed
    start = time.monotonic()
    try:
        failure(provider, "timeout", timeout_s=0.08)
        assert time.monotonic()-start < 0.5 and entered.is_set()
    finally:
        release.set()
    if step == "dns":
        time.sleep(0.03)
        assert backend.calls == []
    else:
        assert backend.deleted.wait(2)
        assert backend.steps.count("delete") == 1


def test_process_concurrency_limit_across_instances_and_timeout_while_queued():
    lock, entered, release = threading.Lock(), threading.Event(), threading.Event()
    counts = dict(active=0, maximum=0, created=0, deleted=0)
    class Browsers:
        def create(self, **kwargs):
            with lock:
                counts["active"] += 1
                counts["created"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
                number = counts["created"]
                if counts["active"] == 2:
                    entered.set()
            return SimpleNamespace(session_id=f"own_{number}")
        def delete_by_id(self, sid, **kwargs):
            with lock:
                counts["active"] -= 1
                counts["deleted"] += 1
    browsers = Browsers()
    def execute(*args, **kwargs):
        release.wait(3)
        return SimpleNamespace(**observed())
    browsers.playwright = SimpleNamespace(execute=execute)
    browsers.computer = SimpleNamespace(capture_screenshot=lambda *a, **k: PNG)
    providers = [KernelBrowserProvider(client=SimpleNamespace(browsers=browsers),
                                      resolver=public_dns) for _ in range(3)]
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(p.capture, URL, timeout_s=2) for p in providers[:2]]
        try:
            assert entered.wait(1)
            failure(providers[2], "timeout", timeout_s=0.04)
            assert counts["created"] == counts["maximum"] == 2
        finally:
            release.set()
        for f in futures:
            assert f.result().provider == "kernel"
    assert counts["active"] == 0 and counts["deleted"] == 2


def test_not_configured_and_env_override(monkeypatch):
    monkeypatch.delenv("KERNEL_API_KEY", raising=False)
    empty = KernelBrowserProvider(resolver=public_dns)
    assert not empty.configured()
    failure(empty, "not_configured")
    monkeypatch.setenv("KERNEL_API_KEY", KEY)
    assert KernelBrowserProvider().configured()
    assert not KernelBrowserProvider(api_key="").configured()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeouts_do_not_create(provider, backend, timeout):
    failure(provider, "timeout", timeout_s=timeout)
    assert backend.calls == []


def test_execute_boolean_is_not_coerced_by_sdk(provider, backend):
    backend.result = {"success": "true", "result": observed()["result"]}
    failure(provider, "invalid_response")
    assert backend.steps == ["create", "execute", "delete"]


def test_json_stream_is_bounded_before_parse(provider, backend):
    from agentpal.tools.kernel_browser import _MAX_JSON_BYTES
    stream = Stream([b"x" * (_MAX_JSON_BYTES+1), b"not read"])
    backend.handlers["execute"] = lambda r: httpx.Response(200, stream=stream)
    failure(provider, "invalid_response")
    assert stream.reads == 1 and stream.closed
    assert backend.steps[-1] == "delete"


def test_compressed_screenshot_is_bounded_after_decoding(provider, backend):
    import gzip
    compressed = gzip.compress(b"x" * (MAX_SCREENSHOT_BYTES+1))
    backend.handlers["screenshot"] = lambda r: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Stream([compressed]))
    failure(provider, "invalid_response")
    assert backend.steps[-1] == "delete"


def test_debug_logging_does_not_reveal_direct_vm_jwt(provider, backend, caplog):
    caplog.set_level(logging.DEBUG)
    jwt = hashlib.sha256(b"ephemeral-session-token").hexdigest()
    backend.handlers["create"] = lambda r: httpx.Response(201, json={
        "session_id": "own_session", "base_url": "https://browser.onkernel.com:8443",
        "cdp_ws_url": "wss://browser.onkernel.com:8443/devtools?jwt=" + jwt})
    provider.capture(URL)
    assert jwt not in caplog.text and KEY not in caplog.text
    assert backend.calls[-1][1].url.path == "/browsers/own_session"


def test_timeout_worker_holds_credit_slot_until_delete_finishes(provider, backend):
    provider = KernelBrowserProvider(client=provider._client, resolver=public_dns,
                                     max_concurrent_sessions=1)
    entered, release = threading.Event(), threading.Event()
    def delayed_delete(request):
        entered.set()
        release.wait(3)
        backend.deleted.set()
        return httpx.Response(204)
    backend.handlers["delete"] = delayed_delete
    try:
        failure(provider, "timeout", timeout_s=0.04)
        assert entered.wait(1)
        failure(provider, "timeout", timeout_s=0.04)
        assert backend.steps.count("create") == 1
    finally:
        release.set()
    assert backend.deleted.wait(1)


def test_credential_only_in_sdk_auth_header_not_url_code_or_capture(provider, backend, caplog):
    caplog.set_level(logging.DEBUG)
    backend.result.update(stdout=KEY, stderr=KEY)
    capture = provider.capture(URL)
    for _, request in backend.calls:
        assert request.headers["authorization"] == "Bearer " + KEY
        assert KEY not in str(request.url) and KEY.encode() not in request.content
    assert KEY not in repr(capture) and KEY not in caplog.text


def test_sdk_http_redirect_cannot_forward_key_even_with_redirects_enabled(provider, backend):
    provider._client._client.follow_redirects = True
    backend.handlers["execute"] = lambda r: httpx.Response(
        307, headers={"Location": "https://attacker.example/steal"})
    failure(provider, "unavailable")
    assert backend.steps == ["create", "execute", "delete"]
    assert all(request.url.host == "api.onkernel.com" for _, request in backend.calls)


@pytest.mark.parametrize("limit", [0, 3, True, -1])
def test_invalid_concurrency_configuration_refused(limit):
    with pytest.raises(ValueError):
        KernelBrowserProvider(max_concurrent_sessions=limit)


def test_primary_failure_is_preserved_when_cleanup_also_fails(provider, backend):
    backend.errors.update(execute=401, delete=500)
    failure(provider, "auth")
    assert backend.steps == ["create", "execute", "delete"]


def test_session_id_reflecting_key_never_used_in_a_request_path(provider, backend):
    backend.handlers["create"] = lambda r: httpx.Response(201, json={"session_id": KEY})
    failure(provider, "invalid_response")
    assert backend.steps == ["create"]
    assert all(KEY not in str(request.url) for _, request in backend.calls)


@pytest.mark.parametrize("message", ["Execution timed out after 10 seconds", "TimeoutError: navigation"])
def test_execute_deadline_error_envelopes(provider, backend, message):
    backend.result = {"success": False, "error": message + KEY}
    failure(provider, "timeout")
    assert backend.steps == ["create", "execute", "delete"]


@pytest.mark.parametrize("timeout", [None, "3", True, 10**400])
def test_malformed_timeout_still_raises_browser_error_without_allocating(provider, backend, timeout):
    failure(provider, "timeout", timeout_s=timeout)
    assert backend.calls == []
