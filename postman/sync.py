"""Push empty-secret artifacts to Postman; preview only unless --apply is supplied."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent
DEFAULT_BASE = "https://api.postman.com"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_CREDENTIALS = {"agent_a_key", "agent_b_key", "owner_key", "invalid_key"}


class SyncBlocked(ValueError):
    """An unsafe or incomplete request was refused before sending credentials."""


class SyncError(RuntimeError):
    """A sanitized remote error (remote bodies and headers must not be printed)."""


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname == "localhost"
        if parsed.hostname and not loopback:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        valid = (parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment
                 and (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)))
        _ = parsed.port  # reject malformed ports without reflecting the input
    except ValueError:
        valid = False
    if not valid:
        raise SyncBlocked("BLOCKED: Postman API base must use HTTPS (HTTP loopback allowed)")
    return value.rstrip("/")


def _empty_credentials(collection: dict, environment: dict) -> None:
    """Never upload locally populated collection credentials or environment overrides."""
    for variable in collection.get("variable", []):
        if variable.get("key") in _CREDENTIALS or variable.get("type") == "secret":
            # Postman collections only accept string/boolean/any/number variable types, so
            # credentials are empty 'string' variables (type 'secret' is rejected upstream).
            if variable.get("value", "") != "" or variable.get("type") != "string":
                raise SyncBlocked("BLOCKED: collection credentials must be empty string variables")
    for variable in environment.get("values", []):
        if variable.get("key") in _CREDENTIALS or variable.get("type") == "secret":
            raise SyncBlocked("BLOCKED: credentials belong only in collection variables")


def sync_assets(collection: dict, environment: dict, *, api_key: str | None = None,
                base_url: str = DEFAULT_BASE, workspace: str | None = None,
                collection_id: str | None = None, environment_id: str | None = None,
                apply: bool = False, client: httpx.Client | None = None) -> dict:
    """Preview or send two requests, optionally using an injected mock HTTP client.

    Supply remote IDs to replace existing objects with PUT; otherwise POST creates
    them. A failure can leave the first object synced: this is not a remote transaction.
    Response content is deliberately omitted because a service could echo credentials.
    """
    api_key = api_key if api_key is not None else os.environ.get("POSTMAN_API_KEY")
    if not api_key or not api_key.strip():
        raise SyncBlocked("BLOCKED: POSTMAN_API_KEY is absent; no Postman API requests sent")
    base_url = _base_url(base_url)
    _empty_credentials(collection, environment)
    actions = []
    payloads = []
    for kind, remote_id, data in (("collection", collection_id, collection),
                                  ("environment", environment_id, environment)):
        if remote_id is not None and not _IDENTIFIER.fullmatch(remote_id):
            raise SyncBlocked("BLOCKED: invalid remote object ID")
        url = f"{base_url}/{kind}s"
        if remote_id:
            url += f"/{remote_id}"
        elif workspace:
            url = str(httpx.URL(url, params={"workspace": workspace}))
        actions.append({"resource": kind, "method": "PUT" if remote_id else "POST", "url": url})
        # Export metadata is not part of the environment API's writable data.
        if kind == "environment":
            data = {key: data[key] for key in ("name", "values") if key in data}
        payloads.append({kind: data})
    if any(api_key in action["url"] for action in actions):
        raise SyncBlocked("BLOCKED: Postman API key must appear only in the authentication header")
    if not apply:
        return {"status": "DRY RUN", "actions": actions}

    owned_client = client is None
    client = client or httpx.Client(timeout=30.0, trust_env=False)
    try:
        for action, payload in zip(actions, payloads, strict=True):
            try:
                response = client.request(action["method"], action["url"], json=payload,
                                          headers={"X-API-Key": api_key},
                                          follow_redirects=False, timeout=30.0)
            except httpx.HTTPError:
                raise SyncError("Postman API transport failed; details suppressed") from None
            if not 200 <= response.status_code < 300:
                raise SyncError(f"Postman API {action['resource']} failed: "
                                f"HTTP {response.status_code}; response suppressed")
            action["status_code"] = response.status_code
    finally:
        if owned_client:
            client.close()
    return {"status": "SYNCED", "actions": actions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, default=ROOT / "legacy.collection.json")
    parser.add_argument("--environment", type=Path, default=ROOT / "environment.json")
    parser.add_argument("--base-url", default=os.environ.get("POSTMAN_API_BASE", DEFAULT_BASE))
    parser.add_argument("--workspace", default=os.environ.get("POSTMAN_WORKSPACE_ID"))
    parser.add_argument("--collection-id", default=os.environ.get("POSTMAN_COLLECTION_ID"))
    parser.add_argument("--environment-id", default=os.environ.get("POSTMAN_ENVIRONMENT_ID"))
    parser.add_argument("--apply", action="store_true", help="send requests (default: dry run)")
    args = parser.parse_args(argv)
    try:
        # Check before reading files so the documented missing-key result is reliable.
        if not os.environ.get("POSTMAN_API_KEY", "").strip():
            raise SyncBlocked("BLOCKED: POSTMAN_API_KEY is absent; no Postman API requests sent")
        collection = json.loads(args.collection.read_text(encoding="utf-8"))
        environment = json.loads(args.environment.read_text(encoding="utf-8"))
        result = sync_assets(collection, environment, base_url=args.base_url,
                             workspace=args.workspace, collection_id=args.collection_id,
                             environment_id=args.environment_id, apply=args.apply)
    except (SyncBlocked, SyncError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print("BLOCKED: cannot read valid Postman JSON artifacts", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
