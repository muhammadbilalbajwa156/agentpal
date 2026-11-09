"""Shared helpers for the AgentPal-Bench v2 annotation kit (standard library + optional
jsonschema). Used by make_packets.py, agreement.py, adjudicate.py and ../score.py."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "label_schema.json"
ACTIONS = ("allow_for_review", "require_review", "block")
STEP_SEP = "#"


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                         for r in rows), encoding="utf-8")


def load_bench(bench_dir: str | Path) -> tuple[list[dict], list[dict]]:
    d = Path(bench_dir)
    return read_jsonl(d / "cases.jsonl"), read_jsonl(d / "episodes.jsonl")


def units(bench_dir: str | Path) -> list[dict]:
    """Every labelable unit with its slicing metadata (never shown to annotators)."""
    cases, episodes = load_bench(bench_dir)
    out = []
    for c in cases:
        out.append({"unit_id": c["id"], "item_id": c["id"], "kind": "case",
                    "group_id": c["group_id"], "split": c["split"],
                    "independent": bool(c["independent"]),
                    "risk_type": c.get("risk_type", "unspecified"),
                    "wording": c.get("wording", "unspecified"),
                    "language": c.get("language", "unspecified"),
                    "proposed_label": c.get("proposed_label"),
                    "proposed_expected_action": c.get("proposed_expected_action")})
    for e in episodes:
        for i, s in enumerate(e["steps"]):
            out.append({"unit_id": f"{e['id']}{STEP_SEP}{i}", "item_id": e["id"],
                        "kind": "episode_step", "group_id": e["group_id"],
                        "split": e["split"], "independent": bool(e["independent"]),
                        "risk_type": s.get("risk_type", e.get("risk_type", "unspecified")),
                        "wording": s.get("wording", e.get("wording", "unspecified")),
                        "language": s.get("language", e.get("language", "unspecified")),
                        "proposed_label": s.get("proposed_label"),
                        "proposed_expected_action": s.get("proposed_expected_action")})
    return out


# ------------------------------------------------------------------ validation

def _minimal_validate(row: dict, schema: dict) -> list[str]:
    errs = []
    props = schema["properties"]
    for k in schema["required"]:
        if k not in row:
            errs.append(f"missing {k}")
    for k, v in row.items():
        if k not in props:
            errs.append(f"unknown field {k}")
            continue
        spec = props[k]
        if "enum" in spec and v not in spec["enum"]:
            errs.append(f"{k}: {v!r} not in {spec['enum']}")
        if spec.get("type") == "string":
            if not isinstance(v, str):
                errs.append(f"{k}: not a string")
            elif "pattern" in spec and not re.match(spec["pattern"], v):
                errs.append(f"{k}: does not match {spec['pattern']}")
            elif len(v) < spec.get("minLength", 0):
                errs.append(f"{k}: too short")
        if spec.get("type") == "number" and (not isinstance(v, (int, float))
                                             or isinstance(v, bool) or v < 0):
            errs.append(f"{k}: must be a non-negative number")
    if row.get("risky") == 0 and row.get("expected_action") != "allow_for_review":
        errs.append("risky=0 requires expected_action=allow_for_review")
    if row.get("risky") == 1 and row.get("expected_action") == "allow_for_review":
        errs.append("risky=1 requires require_review or block")
    return errs


def validate_row(row: dict) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - fallback when jsonschema is absent
        return _minimal_validate(row, schema)
    v = jsonschema.Draft202012Validator(schema)
    return sorted(e.message for e in v.iter_errors(row)) + [
        e for e in _minimal_validate(row, schema) if e.startswith("risky=")]


def load_annotations(paths: list[str | Path], strict: bool = True) -> list[dict]:
    """Read annotator rows from one or more JSONL files. Invalid rows raise (strict) so a
    malformed label can never be silently counted. One row per (annotator, item)."""
    rows, seen, errors = [], set(), []
    for p in paths:
        for n, row in enumerate(read_jsonl(p), 1):
            errs = validate_row(row)
            key = (row.get("annotator_id"), row.get("item_id"))
            if key in seen:
                errs.append("duplicate (annotator_id, item_id)")
            seen.add(key)
            if errs:
                errors.append(f"{p}:{n}: {'; '.join(errs)}")
            else:
                rows.append(row)
    if errors and strict:
        raise ValueError("invalid annotation rows:\n" + "\n".join(errors[:50]))
    return rows


def by_item(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append(r)
    return out


def strict_majority(values: list) -> object | None:
    """The value chosen by more than half of the raters, else None (a tie)."""
    if not values:
        return None
    value, count = Counter(values).most_common(1)[0]
    return value if count * 2 > len(values) else None
