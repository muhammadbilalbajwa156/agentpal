"""Generate the checked-in OpenAPI document from the FastAPI app.

    python -m agentpal.platform.openapi --write   # regenerate docs/api/openapi.json
    python -m agentpal.platform.openapi --check   # exit 1 if the file is out of date

The document is deterministic: keys sorted, two-space indent, no timestamps, and it is
built from a throw-away app with in-memory stores and inert dependencies, so it does not
depend on the environment, the database or any credential.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import PACKAGE_ROOT

DEFAULT_PATH = PACKAGE_ROOT / "docs" / "api" / "openapi.json"


class _NoOwners:
    def authenticate(self, bearer: str):
        from ..infra_contracts import OwnerAuthError
        raise OwnerAuthError("documentation app")


# pydantic < 2.14 emits this regex for Decimal-as-string fields; 2.14+ does not. It is
# documentation only (amounts are validated server-side), so it is removed to keep the
# published contract identical across pydantic versions (CI installs the latest).
_PYDANTIC_DECIMAL_PATTERN = r"^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$"


def _strip_version_dependent(node):
    if isinstance(node, dict):
        if node.get("pattern") == _PYDANTIC_DECIMAL_PATTERN:
            node = {k: v for k, v in node.items() if k != "pattern"}
        out = {k: _strip_version_dependent(v) for k, v in node.items()}
        if isinstance(out.get("anyOf"), list):
            # Once the pattern is gone, older pydantic leaves a duplicate {"type": "string"}
            # arm (Decimal | str) that 2.14+ emits only once: drop exact duplicates, in order.
            unique = []
            for arm in out["anyOf"]:
                if arm not in unique:
                    unique.append(arm)
            out["anyOf"] = unique
        return out
    if isinstance(node, list):
        return [_strip_version_dependent(v) for v in node]
    return node


def _document_platform_errors(doc: dict) -> None:
    """Document middleware/exception handlers invisible to FastAPI route inspection.

    Request schemas, parameters and route metadata remain entirely route/model-owned.
    This deliberately leaves legacy responses alone.
    """
    doc["components"]["schemas"]["V1ValidationEnvelope"] = {
        "type": "object", "additionalProperties": False, "required": ["error"],
        "properties": {
            "error": {"type": "string", "enum": ["invalid_request"]},
            "details": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["loc", "msg", "type"], "properties": {
                    "loc": {"type": "array", "items": {
                        "anyOf": [{"type": "string"}, {"type": "integer"}]}},
                    "msg": {"type": "string"}, "type": {"type": "string"}}}}}}

    def error(description: str) -> dict:
        return {"description": description, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/ErrorOut"}}}}

    for path, methods in doc["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            responses = operation["responses"]
            for code, description in {
                "400": "Invalid request or forbidden Host header",
                "403": "Forbidden credential, permission, or non-loopback client",
                "413": "Request body exceeds the configured byte limit",
                "429": "Rate limited", "500": "Internal error (no input reflected)",
            }.items():
                responses.setdefault(code, error(description))
            if "422" in responses:
                responses["422"]["content"] = {"application/json": {
                    "schema": {"$ref": "#/components/schemas/V1ValidationEnvelope"}}}
            responses["429"].setdefault("headers", {})["Retry-After"] = {
                "description": "Seconds until a request may be attempted again",
                "schema": {"type": "integer", "minimum": 1}}
            if operation.get("security"):
                responses.setdefault("401", error("Missing or invalid credential"))
                responses["401"].setdefault("headers", {})["WWW-Authenticate"] = {
                    "description": "Bearer authentication challenge",
                    "schema": {"type": "string"}}
            if operation["operationId"] == "createPaymentRequest":
                responses["200"].setdefault("headers", {})["Idempotent-Replayed"] = {
                    "description": "Stored response replayed for the same agent, key and body",
                    "schema": {"type": "string", "enum": ["true"]}}


def build_document() -> dict:
    from ..analyzers import HeuristicAnalyzer
    from ..api import create_app
    from ..auth import TokenRegistry
    from ..config import Settings, load_policy
    from ..service import RiskService
    from ..spending import SpendPolicy
    from ..store import AssessmentStore
    from ..wallet.store import WalletStore
    from .deps import PlatformDeps

    risk = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                       spend_policy=SpendPolicy(policy_version="openapi"))
    settings = Settings(environment="development", auth_mode="required",
                        allowed_hosts=["127.0.0.1", "localhost", "::1"], log_level="ERROR")
    app = create_app(service=risk, settings=settings, registry=TokenRegistry(),
                     platform=PlatformDeps(owner_authenticator=_NoOwners(),
                                           wallet_store=WalletStore(":memory:")))
    doc = _strip_version_dependent(app.openapi())
    _document_platform_errors(doc)
    doc["servers"] = [{"url": "http://127.0.0.1:8000",
                       "description": "local development (loopback only)"}]
    return doc


def render(doc: dict | None = None) -> str:
    return json.dumps(doc if doc is not None else build_document(), indent=2,
                      sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = render()
    if args.write:
        args.path.parent.mkdir(parents=True, exist_ok=True)
        args.path.write_text(text, encoding="utf-8")
        print(f"wrote {args.path}", file=sys.stderr)
        return 0
    current = args.path.read_text(encoding="utf-8") if args.path.exists() else ""
    if current != text:
        print(f"{args.path} is out of date: run python -m agentpal.platform.openapi --write",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
