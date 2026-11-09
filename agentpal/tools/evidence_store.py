"""Content-addressed, write-once evidence store.

Retrieved bytes are kept under their SHA-256, so a `SourceEvidence.content_sha256` always
names exactly the bytes that were retrieved. Storing bytes proves only that they were
served; it does not make them true (contracts.SourceEvidence).

* default root: `<project>/data/evidence/` (git-ignored); tests pass `tmp_path`;
* layout: `<root>/<sha[:2]>/<sha>`; written atomically (temp file + rename);
* write-once: an existing object is re-verified, never overwritten;
* `get` re-hashes on read and refuses corrupted objects;
* ids are validated (64 lowercase hex), so a lookup can never escape the root.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from ..config import PACKAGE_ROOT

DEFAULT_EVIDENCE_ROOT = PACKAGE_ROOT / "data" / "evidence"
DEFAULT_MAX_OBJECT_BYTES = 10 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    pass


class EvidenceStore:
    def __init__(self, root: str | Path = DEFAULT_EVIDENCE_ROOT,
                 max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES):
        self.root = Path(root)
        self.max_object_bytes = max_object_bytes

    def _path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
            raise EvidenceError("invalid evidence id")
        return self.root / sha256[:2] / sha256

    def put(self, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise EvidenceError("evidence content must be bytes")
        if len(content) > self.max_object_bytes:
            raise EvidenceError("evidence object too large")
        sha = hashlib.sha256(content).hexdigest()
        path = self._path(sha)
        if path.exists():
            self.get(sha)  # verifies the stored copy
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o400)  # owner-only, like the financial databases (E2E-T4-1)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return sha

    def has(self, sha256: str) -> bool:
        return self._path(sha256).is_file()

    def get(self, sha256: str) -> bytes:
        path = self._path(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise EvidenceError("unknown evidence id") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise EvidenceError("stored evidence does not match its hash (corrupted)")
        return data

    def verify(self, sha256: str) -> bool:
        try:
            self.get(sha256)
        except EvidenceError:
            return False
        return True
