"""One opt-in live REST probe. Only its own session is ever deleted."""

import json
import os
import time

import pytest

from agentpal.tools.browser_contracts import MAX_SCREENSHOT_BYTES
from agentpal.tools.kernel_browser import (
    CAPTURE_JS, KernelBrowserProvider, _IO, _map_error,
)


def test_live_kernel_rest_execute_and_screenshot():
    if not os.environ.get("KERNEL_API_KEY", "").strip():
        pytest.skip("BLOCKED (credentials): KERNEL_API_KEY not set")
    provider = KernelBrowserProvider()
    # Reuse the exact SDK hardening that capture() installs, on this test thread.
    sdk = provider._sdk()
    own_session_id = None
    _IO.active, _IO.deadline = True, time.monotonic() + 25
    try:
        try:
            _IO.creating = True
            try:
                own_session_id = sdk.browsers.create(timeout_seconds=30, timeout=8).session_id
            finally:
                _IO.creating = False
            result = sdk.browsers.playwright.execute(
                own_session_id,
                code="const input = " + json.dumps({"url": "https://example.com/", "timeout_ms": 8000})
                     + ";\n" + CAPTURE_JS,
                timeout_sec=10, timeout=10)
            data = provider._parse(result, "https://example.com/")
            assert data["http_status"] == 200
            assert data["final_url"] == "https://example.com/"
            image = sdk.browsers.computer.capture_screenshot(own_session_id, timeout=5).read()
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
            assert len(image) <= MAX_SCREENSHOT_BYTES
        except AssertionError:
            raise
        except Exception as exc:
            error = _map_error(exc)
            if error.kind in ("unavailable", "timeout"):
                pytest.skip("BLOCKED (network): Kernel REST execute/control endpoint unreachable")
            pytest.fail("LIVE Kernel probe failed: " + error.kind, pytrace=False)
    finally:
        try:
            if own_session_id is not None:
                _IO.deadline = time.monotonic() + 5
                sdk.browsers.delete_by_id(own_session_id, timeout=5)
        except Exception:
            pytest.fail("BLOCKED (network): own-session deletion failed", pytrace=False)
        finally:
            _IO.active = False
