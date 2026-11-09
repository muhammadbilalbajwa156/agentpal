"""Run only in the generated SDK's isolated venv. Uses its actual client and methods.

No server, handwritten SDK, ASGI transport or substitute HTTP client is imported here.
Discovery accommodates package naming derived from OpenAPI info.title. Unsupported
generator surface changes fail explicitly, never fall back to another SDK.
"""

import importlib
import inspect
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def normalized(name):
    return re.sub("[^a-z0-9]", "", name.lower())


def find_client(project):
    packages = [p for root in (project, project / "src") if root.exists()
                for p in root.iterdir() if p.is_dir() and (p / "__init__.py").exists()
                and p.name not in {"tests", "test"}]
    for package in packages:
        modules = [package.name, *[package.name + "." + p.stem
                                 for p in package.glob("*client.py")]]
        for module_name in modules:
            module = importlib.import_module(module_name)
            for name, cls in vars(module).items():
                if (inspect.isclass(cls) and name.endswith("Client") and "Async" not in name
                        and cls.__module__.startswith(package.name)):
                    params = inspect.signature(cls).parameters
                    if "base_url" in params and "retry_options" in params:
                        return cls
    raise RuntimeError("Generated v4 client with base_url/retry_options not found")


def client_for(cls, base_url, scheme, credential):
    params = inspect.signature(cls).parameters
    names = [name for name in params if normalized(name) == normalized(scheme)]
    if len(names) != 1:
        raise RuntimeError("Generated authentication scheme parameter not found")
    return cls(base_url=base_url, retry_options=0, **{names[0]: credential})


def operation(client, operation_id, tag):
    owners = [client]
    for name in dir(client):
        if normalized(name) in {normalized(tag), normalized(tag + "controller")}:
            owners.append(getattr(client, name))
    for owner in owners:
        for name in dir(owner):
            if normalized(name) == normalized(operation_id):
                method = getattr(owner, name)
                if callable(method):
                    return method
    raise RuntimeError("Generated operation not found: " + operation_id)


def plain(value):
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "body"):
        return plain(value.body)
    for name in ("model_dump", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            return plain(method())
    return plain(vars(value))


def main():
    payload = json.load(sys.stdin)
    url = urlsplit(payload["base_url"])
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.path or url.query or url.fragment):
        raise RuntimeError("Only an exact disposable loopback origin is permitted")
    cls = find_client(Path(sys.argv[1]))
    owner = client_for(cls, payload["base_url"], "OwnerToken", payload["owner_token"])
    agent = client_for(cls, payload["base_url"], "AgentApiKey", payload["agent_key"])
    health = plain(operation(owner, "v1Health", "meta")())
    assert health["is_real_money"] is False
    me = plain(operation(agent, "getMe", "me")())
    assert me["agent_id"] == payload["agent_id"]
    agents = plain(operation(owner, "listAgents", "agents")())
    assert any(a["agent_id"] == payload["agent_id"] for a in agents["agents"])
    for client in (owner, agent):
        close = getattr(client, "close", None)
        if callable(close):
            close()
    print(json.dumps({"generated_sdk_http_reads": 3}))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        main()
    except Exception:
        print("Generated SDK verification failed; details suppressed", file=sys.stderr)
        raise SystemExit(1) from None
