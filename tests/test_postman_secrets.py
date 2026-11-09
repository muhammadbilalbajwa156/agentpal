"""Scan the owned Postman tree, excluding disposable dependencies/reports/caches."""

import json
from pathlib import Path
import re

import pytest


POSTMAN = Path(__file__).resolve().parents[1] / "postman"
TOKEN = re.compile(
    r"\b(?:apt_|apk_|apo_)[A-Za-z0-9_-]{20,}"
    r"|\bPMAK-[A-Za-z0-9-]{20,}"
    r"|\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
SENSITIVE = re.compile(r"(?:key|token|secret|password|authorization)", re.I)


def scan(path: Path) -> list[str]:
    failures = []
    for file in sorted(path.rglob("*")):
        if not file.is_file() or any(part in {"node_modules", "reports", "__pycache__"}
                                     for part in file.relative_to(path).parts):
            continue
        content = file.read_text(encoding="utf-8")
        if TOKEN.search(content):
            failures.append(f"credential-shaped literal: {file.relative_to(path)}")
        if file.name.endswith(".collection.json") or file.name == "environment.json":
            document = json.loads(content)
            variables = document.get("variable", document.get("values", []))
            for variable in variables:
                if SENSITIVE.search(variable["key"]):
                    if variable.get("value") != "" or variable.get("type") not in ("string", "secret"):
                        failures.append(f"nonempty or unmasked credential variable: {variable['key']}")
            for auth in re.findall(r'"value":\s*"Bearer ([^"\n]+)"', content):
                if not re.fullmatch(r"\{\{[a-z_]+\}\}", auth):
                    failures.append("literal bearer credential")
    return failures


def test_postman_tree_contains_no_credentials():
    assert scan(POSTMAN) == []


@pytest.mark.parametrize("kind", ["token", "variable", "bearer"])
def test_scanner_rejects_leaks(tmp_path, kind):
    document = {"variable": [{"key": "agent_a_key", "type": "string", "value": ""}]}
    if kind == "token":
        document["note"] = "apt_" + "x" * 40
    elif kind == "variable":
        document["variable"][0]["value"] = "accidental local value"
    else:
        document["header"] = [{"key": "Authorization", "value": "Bearer accidental-value"}]
    (tmp_path / "leak.collection.json").write_text(json.dumps(document))
    assert scan(tmp_path)
