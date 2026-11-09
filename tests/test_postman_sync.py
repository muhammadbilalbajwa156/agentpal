"""Offline Postman push contracts and credential boundaries; no account is required."""

from __future__ import annotations

import json
import secrets

import httpx
import pytest

from postman.sync import SyncBlocked, SyncError, main, sync_assets


@pytest.fixture
def assets():
    return ({"info": {"name": "test"}, "item": [], "variable": [
        {"key": "agent_a_key", "value": "", "type": "string"}]},
        {"name": "test", "values": [{"key": "base_url", "value": "http://127.0.0.1"}],
         "_postman_variable_scope": "environment"})


def test_missing_api_key_is_blocked_even_for_dry_run(assets, monkeypatch):
    monkeypatch.delenv("POSTMAN_API_KEY", raising=False)
    with pytest.raises(SyncBlocked, match="BLOCKED: POSTMAN_API_KEY is absent"):
        sync_assets(*assets)
    assert main(["--collection", "/does-not-exist"]) == 2


def test_default_is_dry_run_with_no_transport_calls(assets):
    def reject(request):
        pytest.fail("dry run sent a network request")
    key = secrets.token_urlsafe(32)
    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        result = sync_assets(*assets, api_key=key, workspace="space one", client=client)
    assert result["status"] == "DRY RUN"
    assert [x["method"] for x in result["actions"]] == ["POST", "POST"]
    assert all("workspace=space+one" in x["url"] for x in result["actions"])
    assert key not in json.dumps(result)


@pytest.mark.parametrize("update", [False, True])
def test_create_and_update_shapes_header_workspace_and_base(assets, update):
    key = secrets.token_urlsafe(32)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == key
        return httpx.Response(200, json={"server_reflection": key})

    ids = {"collection_id": "collection-1", "environment_id": "environment-2"} if update else {}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_assets(*assets, api_key=key, workspace="workspace-3", apply=True,
                             base_url="https://postman.example/api/", client=client, **ids)
    assert [r.method for r in calls] == ["PUT" if update else "POST"] * 2
    assert [r.url.path for r in calls] == (
        ["/api/collections/collection-1", "/api/environments/environment-2"] if update else
        ["/api/collections", "/api/environments"])
    assert all(dict(r.url.params) == ({} if update else {"workspace": "workspace-3"})
               for r in calls)
    assert json.loads(calls[0].content) == {"collection": assets[0]}
    assert json.loads(calls[1].content) == {"environment": {
        "name": assets[1]["name"], "values": assets[1]["values"]}}
    assert result["status"] == "SYNCED"
    assert key not in json.dumps(result)


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
def test_remote_errors_are_sanitized_and_redirects_not_followed(assets, status, capsys):
    key = secrets.token_urlsafe(32)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=key, headers={"Location": "https://other.example"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(SyncError, match=f"HTTP {status}") as exc:
            sync_assets(*assets, api_key=key, apply=True, client=client)
    assert len(calls) == 1
    assert key not in str(exc.value)
    assert key not in str(capsys.readouterr())


def test_transport_exception_cannot_echo_the_key(assets):
    key = secrets.token_urlsafe(32)

    def handler(request):
        raise httpx.ConnectError(key, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SyncError, match="details suppressed") as exc:
            sync_assets(*assets, api_key=key, apply=True, client=client)
    assert key not in str(exc.value)


@pytest.mark.parametrize("base", ["http://postman.example", "https://user:pass@postman.example",
                                  "https://postman.example?key=hidden", "ftp://127.0.0.1",
                                  "https://postman.example:bad", "https://postman.example#frag"])
def test_unsafe_base_is_refused_before_credentials_leave(assets, base):
    with pytest.raises(SyncBlocked, match="HTTPS"):
        sync_assets(*assets, api_key=secrets.token_urlsafe(32), base_url=base)


def test_loopback_http_can_use_mock_transport(assets):
    result = sync_assets(*assets, api_key=secrets.token_urlsafe(32),
                         base_url="http://127.0.0.1:8777")
    assert result["actions"][0]["url"] == "http://127.0.0.1:8777/collections"


@pytest.mark.parametrize("kind", ["populated", "secret_type", "any_type", "environment_override"])
def test_populated_secrets_and_environment_credentials_are_never_uploaded(assets, kind):
    if kind == "populated":
        assets[0]["variable"][0]["value"] = secrets.token_urlsafe(32)
    elif kind == "secret_type":   # rejected by the Postman API; never sent
        assets[0]["variable"][0]["type"] = "secret"
    elif kind == "any_type":
        assets[0]["variable"][0]["type"] = "any"
    else:
        assets[1]["values"].append({"key": "agent_a_key", "value": ""})
    def reject(request):
        pytest.fail("credential guard allowed a transport call")

    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(SyncBlocked, match="BLOCKED"):
            sync_assets(*assets, api_key=secrets.token_urlsafe(32), apply=True, client=client)


def test_remote_ids_cannot_inject_a_query_or_path(assets):
    with pytest.raises(SyncBlocked, match="invalid remote object ID"):
        sync_assets(*assets, api_key=secrets.token_urlsafe(32), collection_id="a/../b?x=1")


@pytest.mark.parametrize("field", ["base_url", "workspace", "collection_id", "environment_id"])
def test_misconfigured_target_cannot_print_key_in_dry_run(assets, field):
    key = secrets.token_urlsafe(32)
    value = f"https://postman.example/{key}" if field == "base_url" else key
    with pytest.raises(SyncBlocked, match="only in the authentication header") as exc:
        sync_assets(*assets, api_key=key, **{field: value})
    assert key not in str(exc.value)


def test_cli_previews_and_does_not_print_key(assets, monkeypatch, tmp_path, capsys):
    key = secrets.token_urlsafe(32)
    monkeypatch.setenv("POSTMAN_API_KEY", key)
    paths = [tmp_path / "collection.json", tmp_path / "environment.json"]
    for path, payload in zip(paths, assets, strict=True):
        path.write_text(json.dumps(payload))
    assert main(["--collection", str(paths[0]), "--environment", str(paths[1])]) == 0
    output = capsys.readouterr()
    assert '"status": "DRY RUN"' in output.out
    assert key not in output.out + output.err
