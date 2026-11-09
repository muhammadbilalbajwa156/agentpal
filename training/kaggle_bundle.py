"""Source bundle for Kaggle (the GitHub repository is private, and no credential may be put
in the handoff). Packs the TRACKED files of `agentpal/` at one exact commit, plus
BUNDLE_INFO.json (commit, per-file SHA-256). On Kaggle, `verify` re-hashes every file
before anything runs.

    # here (needs git):
    python -m training.kaggle_bundle create --out build/kaggle
    # on Kaggle, after unzipping the uploaded dataset:
    python -m training.kaggle_bundle verify --root /kaggle/working/agentpal

Only tracked files are included: no data/, artifacts/, build/, .env or model weights
(all git-ignored), so no secret or generated file can leak into the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

INFO = "BUNDLE_INFO.json"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def create(out_dir: Path, ref: str = "HEAD", repo: Path | None = None) -> Path:
    repo = repo or Path(_git("rev-parse", "--show-toplevel", cwd=Path(__file__).parent))
    commit = _git("rev-parse", ref, cwd=repo)
    # The project may be the repository root (public repo) or its agentpal/ folder (monorepo).
    project = Path(__file__).resolve().parents[1]
    sub = project.relative_to(Path(repo).resolve()).as_posix()
    sub = "" if sub == "." else sub
    if _git("status", "--porcelain", "--", sub or ".", cwd=repo) and ref == "HEAD":
        print("WARNING: agentpal/ has uncommitted changes; the bundle contains the COMMIT "
              f"{commit[:12]}, not the working tree", file=sys.stderr)
    tree = f"{commit}:{sub}" if sub else commit
    raw = subprocess.run(["git", "archive", "--format=zip", "--prefix=agentpal/", tree], cwd=repo,
                         check=True, capture_output=True).stdout
    files = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        for name in sorted(src.namelist()):
            if not name.endswith("/"):
                files[name] = hashlib.sha256(src.read(name)).hexdigest()
    info = {"commit": commit, "repository": Path(repo).name,
            "branch_hint": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
            "files": files}
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"agentpal-src-{commit[:12]}.zip"
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(
            out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr(f"agentpal/{INFO}", json.dumps(info, indent=1) + "\n")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out_dir / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n")
    print(f"bundle {out} ({len(files)} files) commit {commit} sha256 {digest}")
    return out


def verify(root: Path) -> dict:
    """`root` is the extracted agentpal/ directory. Raises on any missing or changed file."""
    root = root.resolve()  # `--root .` must work: Path(".").parent is "." again
    info = json.loads((root / INFO).read_text())
    bad = []
    for name, digest in info["files"].items():
        p = root.parent / name
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            bad.append(name)
    if bad:
        raise SystemExit(f"bundle verification FAILED for {len(bad)} file(s), e.g. {bad[:5]}")
    print(f"bundle OK: {len(info['files'])} files match commit {info['commit']}")
    return info


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--out", default="build/kaggle")
    c.add_argument("--ref", default="HEAD")
    v = sub.add_parser("verify")
    v.add_argument("--root", default=".")
    a = p.parse_args(argv)
    if a.cmd == "create":
        create(Path(a.out), a.ref)
    else:
        verify(Path(a.root))


if __name__ == "__main__":
    main()
