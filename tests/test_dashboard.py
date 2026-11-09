"""Owner dashboard static hosting at /app/ (lead, 2026-11-06): exact allow-listed files,
GET/HEAD only, strict security headers, no credentials needed for the files themselves
and none granted by them."""
import asyncio

import pytest

from agentpal.dashboard import CSP, Dashboard, csp, load_site
from test_hosted_render import hosted, via_render  # noqa: F401  (fixture re-export)
from test_platform_fixtures import OWNER_A


def test_dashboard_is_served_remotely_through_render_without_a_token(hosted):  # noqa: F811
    _, c = hosted
    r = c.get("/app/", headers=via_render())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "AgentPal" in r.text
    nonce = r.text.split('name="csp-nonce" content="')[1].split('"')[0]
    assert len(nonce) >= 20 and "__CSP_NONCE__" not in r.text
    assert r.headers["content-security-policy"] == csp(nonce)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


def test_assets_have_their_own_types(hosted):  # noqa: F811
    _, c = hosted
    js = c.get("/app/core.js?v=1", headers=via_render())
    css = c.get("/app/style.css", headers=via_render())
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")


def test_bare_prefix_redirects_to_the_index(hosted):  # noqa: F811
    _, c = hosted
    r = c.get("/app", headers=via_render(), follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/app/"


@pytest.mark.parametrize("path", ["/app/missing.js", "/app/agentpal.db", "/app/a/core.js",
                                  "/app/README.md", "/app/preview.cjs", "/app/.env"])
def test_only_allow_listed_files_exist(hosted, path):  # noqa: F811
    _, c = hosted
    assert c.get(path, headers=via_render()).status_code == 404


def test_traversal_and_encoded_paths_are_refused(hosted):  # noqa: F811
    _, c = hosted
    for raw in ("/app/..%2fagentpal%2fapi.py", "/app/%2e%2e/%2e%2e/etc/passwd"):
        assert c.get(raw, headers=via_render()).status_code in (403, 404)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_and_head(hosted, method):  # noqa: F811
    _, c = hosted
    r = c.request(method, "/app/", headers=via_render())
    assert r.status_code == 405 and r.headers["allow"] == "GET, HEAD"


def test_head_has_no_body(hosted):  # noqa: F811
    _, c = hosted
    r = c.head("/app/", headers=via_render())
    assert r.status_code == 200 and r.content == b""


def test_wrong_host_is_still_refused(hosted):  # noqa: F811
    _, c = hosted
    r = c.get("/app/", headers={**via_render(), "Host": "evil.example"})
    assert r.status_code == 400


def test_plain_http_is_still_refused(hosted):  # noqa: F811
    _, c = hosted
    assert c.get("/app/", headers=via_render(proto="http")).status_code == 403


def test_dashboard_grants_no_api_access(hosted):  # noqa: F811
    _, c = hosted
    assert c.get("/app/", headers=via_render()).status_code == 200
    assert c.get("/v1/agents", headers=via_render()).status_code == 401
    ok = c.get("/v1/agents", headers={**via_render(), "Authorization": "Bearer " + OWNER_A})
    assert ok.status_code == 200


def test_load_site_skips_symlinks_and_unknown_types(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "secret.env").write_text("X=1")
    (tmp_path / "notes.md").write_text("x")
    outside = tmp_path.parent / "outside.js"
    outside.write_text("leak()")
    (tmp_path / "link.js").symlink_to(outside)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.js").write_text("x")
    assert sorted(load_site(tmp_path)) == ["index.html"]


def test_load_site_refuses_oversized_files(tmp_path):
    (tmp_path / "index.html").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        load_site(tmp_path)


def test_missing_site_disables_the_dashboard(tmp_path):
    assert not Dashboard(load_site(tmp_path / "nope"))


def test_files_are_read_once_not_per_request(tmp_path):
    (tmp_path / "index.html").write_text("v1")
    app = Dashboard(load_site(tmp_path))
    (tmp_path / "index.html").write_text("v2-changed-on-disk")
    sent = []

    async def send(msg):
        sent.append(msg)
    asyncio.run(app({"type": "http", "method": "GET", "path": "/app/"}, None, send))
    assert sent[1]["body"] == b"v1"


def test_v1_responses_are_never_cached_or_sniffed(hosted):  # noqa: F811
    _, c = hosted
    for r in (c.get("/v1/agents", headers={**via_render(), "Authorization": "Bearer " + OWNER_A}),
              c.get("/v1/agents", headers=via_render())):
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-type-options"] == "nosniff"


def test_index_nonce_is_fresh_per_response_and_assets_get_no_nonce(hosted):  # noqa: F811
    _, c = hosted
    a, b = (c.get("/app/", headers=via_render()) for _ in range(2))
    assert a.headers["content-security-policy"] != b.headers["content-security-policy"]
    js = c.get("/app/core.js", headers=via_render())
    assert js.headers["content-security-policy"] == CSP and "nonce" not in CSP


def test_vendored_ag_bundles_are_served(hosted):  # noqa: F811
    _, c = hosted
    for name in ("ag-grid-community.min.noStyle.js", "ag-charts-community.min.js",
                 "ag-grid.min.css", "ag-theme-quartz-no-font.min.css"):
        assert c.get("/app/" + name, headers=via_render()).status_code == 200, name


def test_paypal_return_page_is_static_and_script_free(hosted):  # noqa: F811
    _, c = hosted
    r = c.get("/app/paypal-return.html", headers=via_render())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<script" not in r.text and "Capture" in r.text
