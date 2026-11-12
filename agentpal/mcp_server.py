"""Agent-only MCP over newline-delimited JSON-RPC 2.0 on stdio.

There is deliberately NO approve, capture, refund or payee-change tool. A human
approves exact terms in AgentPal; this adapter has only an agent credential and
delegates every permission, policy and execution decision to the sandbox API.
Configuration is environment-only. stdout carries protocol messages, never logs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any, TextIO

import httpx

PROTOCOL_VERSION = "2025-06-18"
APPROVAL_NOTICE = "A human must approve these exact terms in AgentPal before any money moves."
_KEY = re.compile(r"apk_[A-Za-z0-9_-]+")


def _string(**constraints):
    return {"type": "string", **constraints}


_PAYMENT_PROPERTIES = {
    "amount": _string(pattern=r"^[0-9]{1,10}(\.[0-9]{1,8})?$"),
    "currency": _string(pattern=r"^[A-Z]{3}$"),
    "merchant_name": _string(minLength=1, maxLength=200),
    "purpose": _string(minLength=1, maxLength=500),
    "merchant_domain": _string(maxLength=253),
    "product_query": _string(minLength=2, maxLength=200),
    "business_justification": _string(maxLength=2000),
    "idempotency_key": _string(minLength=8, maxLength=128, pattern=r"^[A-Za-z0-9_-]+$"),
}
_REQUEST_ID = {"request_id": _string(minLength=1, maxLength=128, pattern=r"^[A-Za-z0-9_-]+$")}


def _tool(name, description, properties=None, required=()):
    return {"name": name, "description": description + " " + APPROVAL_NOTICE,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False}}


TOOLS = [
    _tool("get_wallet", "Read this agent's virtual allowance; it holds no real money."),
    _tool("request_payment", "Request a sandbox payment for human review. Creating a request does not pay.",
          _PAYMENT_PROPERTIES, ("amount", "currency", "merchant_name", "purpose")),
    _tool("get_payment_request", "Read this agent's request status, exact terms and verification; includes a payer link only if the API supplies it. If `countered_by` is set, the human owner declined this request and replaced it with that request: read it, and after the owner's approval execute it unchanged if you accept. Agents cannot counter or approve.",
          _REQUEST_ID, ("request_id",)),
    _tool("list_my_payment_requests", "List only this agent's payment requests."),
    _tool("execute_approved_payment", "Submit only an already owner-approved request to the sandbox provider. The API enforces exact-term approval. Submission is not verified settlement; do not blindly retry uncertain execution.",
          _REQUEST_ID, ("request_id",)),
]
_TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}
_HTTP_ERRORS = {
    401: "The agent credential was refused. Ask its owner to check expiry or revocation.",
    403: "This agent is not permitted to perform that action. A human owner must grant the required permission; agents cannot approve payments.",
    404: "The request was not found among this agent's resources.",
    409: "The action conflicts with the current state. Execution requires a human's approval of the exact terms and an active wallet. Read the request before proceeding; do not blindly retry execution.",
    422: "The API refused the input. Check the amount, currency and required payment terms.",
    429: "The API rate limit was reached. Wait before trying again.",
}


class InvalidParams(Exception):
    pass


def _validate_arguments(name: str, arguments: Any) -> None:
    schema = _TOOL_BY_NAME[name]["inputSchema"]
    if (not isinstance(arguments, dict) or set(arguments) - schema["properties"].keys()
            or set(schema["required"]) - arguments.keys()):
        raise InvalidParams
    # All supported arguments are strings. Keep runtime validation in lockstep with
    # the small published schema subset rather than accepting model-supplied URLs,
    # extra authority fields, floats, or a path in place of a request identifier.
    for field, value in arguments.items():
        rule = schema["properties"][field]
        if (not isinstance(value, str) or len(value) < rule.get("minLength", 0)
                or len(value) > rule.get("maxLength", 1000)
                or ("pattern" in rule and not re.fullmatch(rule["pattern"], value))):
            raise InvalidParams


def _error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


class MCPServer:
    """One scoped agent per process. Transport injection is for in-process tests only."""

    def __init__(self, *, environ: Mapping[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        env = os.environ if environ is None else environ
        key = env.get("AGENTPAL_AGENT_KEY", "")
        if not re.fullmatch(r"apk_[A-Za-z0-9_-]+", key):
            raise ValueError("AGENTPAL_AGENT_KEY must contain an agent credential.")
        try:
            url = httpx.URL(env.get("AGENTPAL_BASE_URL", "http://127.0.0.1:8000"))
            if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                    or url.query or url.fragment or url.path not in {"", "/", "/v1", "/v1/"}):
                raise ValueError
        except (ValueError, httpx.InvalidURL):
            raise ValueError("AGENTPAL_BASE_URL must be an HTTP(S) origin, optionally ending in /v1.") from None
        self._http = httpx.AsyncClient(
            base_url=str(url.copy_with(path="/", query=None, fragment=None)),
            headers={"Authorization": "Bearer " + key}, timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False, trust_env=False, transport=transport)
        self._initialized = False

    async def aclose(self):
        await self._http.aclose()

    async def handle(self, message: Any) -> dict | None:
        """Process one decoded message; notifications never execute tools or respond."""
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or ("id" in message and (isinstance(message["id"], bool)
                    or not isinstance(message["id"], (str, int, type(None)))))):
            return _error(None, -32600, "Invalid Request")
        if "id" not in message:
            return None
        ident, method = message["id"], message["method"]
        try:
            result = await self._dispatch(method, message.get("params", {}))
            reply = {"jsonrpc": "2.0", "id": ident, "result": result}
        except InvalidParams:
            reply = _error(ident, -32602, "Invalid params or unknown tool")
        except LookupError:
            reply = _error(ident, -32601, "Method not found")
        except RuntimeError:
            reply = _error(ident, -32600, "Initialize the server first")
        except Exception:
            # No arbitrary exception text, request bodies or tracebacks cross stdio.
            reply = _error(ident, -32603, "Internal error; payment outcome may be unknown. Read the request before proceeding.")
        return json.loads(_KEY.sub("[REDACTED]", json.dumps(reply, ensure_ascii=True)))

    async def _dispatch(self, method, params):
        if (not isinstance(params, dict)
                or ("_meta" in params and not isinstance(params["_meta"], dict))):
            raise InvalidParams
        if method == "initialize":
            info = params.get("clientInfo")
            if (not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(info, dict) or not isinstance(info.get("name"), str)
                    or not isinstance(info.get("version"), str)):
                raise InvalidParams
            self._initialized = True
            return {"protocolVersion": PROTOCOL_VERSION, "serverInfo": {"name": "agentpal", "version": "0.1.0"},
                    "capabilities": {"tools": {}}}
        if method == "ping":
            if params.keys() - {"_meta"}:
                raise InvalidParams
            return {}
        if method not in {"tools/list", "tools/call"}:
            raise LookupError
        if not self._initialized:
            raise RuntimeError
        if method == "tools/list":
            if params.keys() - {"cursor", "_meta"} or params.get("cursor") is not None:
                raise InvalidParams
            return {"tools": TOOLS}
        name = params.get("name")
        if (not isinstance(name, str) or name not in _TOOL_BY_NAME
                or params.keys() - {"name", "arguments", "_meta"}):
            raise InvalidParams
        arguments = params.get("arguments", {})
        _validate_arguments(name, arguments)
        return await self._call(name, arguments)

    async def _call(self, name, arguments):
        body, headers = None, None
        method = "GET"
        if name == "request_payment":
            body = {k: v for k, v in arguments.items() if k != "idempotency_key"}
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            idem = arguments.get("idempotency_key") or "mcp-" + hashlib.sha256(canonical.encode()).hexdigest()
            headers = {"Idempotency-Key": idem}
            method, path = "POST", "/v1/payment-requests"
        elif name == "get_wallet":
            path = "/v1/me/wallet"
        elif name == "list_my_payment_requests":
            path = "/v1/payment-requests"
        else:
            # request_id was validated as a single URL-safe identifier above.
            path = "/v1/payment-requests/" + arguments["request_id"]
            if name == "execute_approved_payment":
                method, path = "POST", path + "/execute"
        try:
            response = await self._http.request(method, path, json=body, headers=headers)
        except httpx.RequestError:
            return self._tool_result("The API could not be reached. The payment outcome may be unknown. Retry creation only with identical arguments/key; read the request before any further execution.", error=True)
        if not 200 <= response.status_code < 300:
            explanation = _HTTP_ERRORS.get(response.status_code,
                "The API could not complete the action. Read the request status; do not blindly retry execution.")
            return self._tool_result(explanation, error=True)
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
        except ValueError:
            return self._tool_result("The API returned an unreadable result. The payment outcome may be unknown; read the request before proceeding.", error=True)
        if name == "request_payment":
            value["message"] = APPROVAL_NOTICE
        return self._tool_result(value)

    @staticmethod
    def _tool_result(value, *, error=False):
        return {"content": [{"type": "text", "text": json.dumps(value) if isinstance(value, dict) else value}],
                "isError": error}


async def serve_stdio(server: MCPServer, reader: TextIO, writer: TextIO):
    """EOF shuts down cleanly. One compact JSON message per line; no batch support."""
    while line := await asyncio.to_thread(reader.readline):
        try:
            # JSON-RPC uses JSON, which excludes NaN/Infinity despite Python's default.
            def invalid_constant(_value):
                raise ValueError
            message = json.loads(line, parse_constant=invalid_constant)
        except (ValueError, RecursionError):
            reply = _error(None, -32700, "Parse error")
        else:
            reply = await server.handle(message)
        if reply is not None:
            writer.write(json.dumps(reply, separators=(",", ":")) + "\n")
            writer.flush()


async def _main():
    try:
        server = MCPServer()
    except (ValueError, httpx.InvalidURL):
        print("AgentPal MCP configuration invalid. Set AGENTPAL_AGENT_KEY and an HTTP(S) AGENTPAL_BASE_URL.", file=sys.stderr)
        return 1
    try:
        await serve_stdio(server, sys.stdin, sys.stdout)
    finally:
        await server.aclose()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
