"""Model artifact integrity (P0.7d).

Expected SHA-256 digests live in a trusted *manifest* under ``config/`` (operator
configuration), separate from the untrusted ``artifacts/`` directory, so that replacing
an artifact does not also replace the digest it is checked against.

A matching digest proves only that the file is byte-identical to the file whose digest
the operator pinned. It does not establish that the model is safe, accurate, or from any
particular publisher.

Residual risk: llama.cpp loads GGUF files by path, so there is a window between hashing
and loading (TOCTOU). Keep artifacts on storage the service cannot write to. The JSON
ML artifact is parsed from the same bytes that were hashed (no window).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class IntegrityError(RuntimeError):
    pass


class ManifestEntry(BaseModel):
    sha256: str
    source: str = ""          # where the digest came from (e.g. publisher's LFS metadata)
    pinned_at: str = ""
    note: str = ""


class ModelManifest(BaseModel):
    manifest_version: int = 1
    models: dict[str, ManifestEntry] = Field(default_factory=dict)

    def digest_for(self, backend: str) -> str | None:
        entry = self.models.get(backend)
        return entry.sha256.lower() if entry else None


def load_manifest(path: str | Path | None) -> ModelManifest:
    """A missing manifest is an empty manifest (every required check then fails closed)."""
    if not path:
        return ModelManifest()
    p = Path(path)
    if not p.exists():
        return ModelManifest()
    return ModelManifest.model_validate(json.loads(p.read_text(encoding="utf-8")))


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _check(actual: str, expected: str, path: Path) -> None:
    expected = expected.strip().lower()
    if not _HEX64.match(expected):
        raise IntegrityError(f"integrity: configured digest for {path.name} is not a SHA-256")
    if not hmac.compare_digest(actual, expected):
        raise IntegrityError(f"integrity: digest mismatch for {path.name}")


def verify_file(path: str | Path, expected_sha256: str) -> bytes:
    """Read the whole file once, verify, and return the verified bytes."""
    p = Path(path)
    data = p.read_bytes()
    _check(hashlib.sha256(data).hexdigest(), expected_sha256, p)
    return data


def verify_large_file(path: str | Path, expected_sha256: str) -> None:
    """Streamed verification for large artifacts loaded by path (GGUF)."""
    p = Path(path)
    _check(sha256_file(p), expected_sha256, p)


def pin(manifest_path: str | Path, backend: str, artifact: str | Path,
        source: str = "operator", note: str = "") -> str:
    """Operator action: compute an artifact's digest and record it in the manifest."""
    digest = sha256_file(artifact)
    mp = Path(manifest_path)
    manifest = load_manifest(mp)
    manifest.models[backend] = ManifestEntry(
        sha256=digest, source=source, note=note,
        pinned_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return digest


# ------------------------------------------------- controlled loading (decision 7)

_WRITE_BITS = 0o222


def storage_is_read_only(path: str | Path) -> tuple[bool, str]:
    """Policy check on permission bits: neither the file nor its directory may be writable
    by anyone. (A process running as root bypasses permission bits, so root-run services
    must also use a read-only mount; see docs/MODEL_INTEGRITY.md.)"""
    p = Path(path)
    if p.stat().st_mode & _WRITE_BITS:
        return False, f"model file {p.name} is writable"
    if p.parent.stat().st_mode & _WRITE_BITS:
        return False, f"model directory {p.parent.name}/ is writable"
    return True, ""


def _secure_staging_dir(staging_dir: Path) -> None:
    if staging_dir.is_symlink():
        raise IntegrityError("integrity: staging directory must not be a symlink")
    if not staging_dir.exists():
        staging_dir.mkdir(mode=0o700, parents=True)
        staging_dir.chmod(0o700)
    st = staging_dir.stat()
    if not staging_dir.is_dir() or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise IntegrityError("integrity: staging directory is insecure (must be a private "
                             "0700 directory owned by the service user)")


def stage_verified_copy(source: str | Path, expected_sha256: str,
                        staging_dir: str | Path) -> Path:
    """Copy `source` into a private staging directory while hashing exactly the bytes
    written; publish the copy (mode 0400) only if that hash equals the pinned digest.
    The caller then loads the staged copy, so later changes to `source` cannot affect
    what is loaded (closes the hash-then-load window)."""
    expected = expected_sha256.strip().lower()
    if not _HEX64.match(expected):
        raise IntegrityError("integrity: configured digest is not a SHA-256")
    staging = Path(staging_dir)
    _secure_staging_dir(staging)
    target = staging / f"{expected}{Path(source).suffix}"
    if target.exists():
        if hmac.compare_digest(sha256_file(target), expected):
            return target
        target.unlink()
    tmp = staging / f".staging-{os.getpid()}-{expected[:16]}"
    h = hashlib.sha256()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            while block := src.read(1 << 20):
                h.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if not hmac.compare_digest(h.hexdigest(), expected):
            raise IntegrityError("integrity: staged copy does not match the pinned digest")
        os.chmod(tmp, 0o400)
        os.replace(tmp, target)
        return target
    finally:
        if tmp.exists():
            tmp.unlink()
