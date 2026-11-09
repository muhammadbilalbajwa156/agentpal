"""Custodian lock for a v2 LOCKED split held OUTSIDE the engineering repository.

    python benchmarks/agentpal-bench-v2/custodian_lock.py --dir /custodian/agentpal-v2-locked \
        --note "guideline v1.0, kappa_risky=0.71"

Differences from `python -m agentpal bench-lock` (which it stays compatible with: the
`files` map is what agentpal.benchmark._verify_lock checks):
  * refuses a directory inside the git repository (locked items must never be committed);
  * locks EVERY file the evaluation reads: cases.jsonl, episodes.jsonl AND
    adjudicated_labels.jsonl, and refuses if the labels are missing (a v1 lock records only
    the files present at lock time, so a file added later is never verified);
  * refuses items that are not `split: "locked"` and `independent: true`;
  * never silently overwrites an existing LOCK.json: a re-lock needs --relock-reason, and
    every lock is appended to lock_history.jsonl (a v1 re-lock leaves no trace).

Only LOCK.json (and aggregate reports) may be copied into the repository afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCKED_FILES = ("cases.jsonl", "episodes.jsonl", "adjudicated_labels.jsonl")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE,
                         capture_output=True, text=True).stdout.strip()
    return Path(out).resolve() if out else HERE.parent.parent.resolve()


def lock(d: Path, note: str, relock_reason: str | None = None,
         repo_root: Path | None = None) -> dict:
    d = d.resolve()
    root = (repo_root or _repo_root()).resolve()
    if d == root or root in d.parents:
        raise SystemExit(f"refusing: {d} is inside the repository {root}")
    if not (d / "adjudicated_labels.jsonl").exists():
        raise SystemExit("refusing: adjudicated_labels.jsonl is required before locking")
    if not any((d / f).exists() for f in ("cases.jsonl", "episodes.jsonl")):
        raise SystemExit("refusing: no cases.jsonl or episodes.jsonl to lock")
    for f in ("cases.jsonl", "episodes.jsonl"):
        p = d / f
        for n, line in enumerate(p.read_text("utf-8").splitlines() if p.exists() else [], 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("split") != "locked" or item.get("independent") is not True:
                raise SystemExit(f"refusing: {f}:{n} is not split=locked/independent=true")
    if (d / "LOCK.json").exists() and not relock_reason:
        raise SystemExit("refusing: LOCK.json exists; re-locking needs --relock-reason and "
                         "invalidates every earlier locked result")
    record = {"locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "note": note, "files": {f: _sha(d / f) for f in LOCKED_FILES
                                      if (d / f).exists()},
              "relock_reason": relock_reason, "tool": "custodian_lock.py"}
    (d / "LOCK.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with (d / "lock_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="custodian lock for the v2 locked split")
    p.add_argument("--dir", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--relock-reason", default=None)
    args = p.parse_args(argv)
    print(json.dumps(lock(Path(args.dir), args.note, args.relock_reason), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
