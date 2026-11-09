"""Evidence store: content-addressed, write-once, verified on read, no path escape."""

from __future__ import annotations

import hashlib
import os

import pytest

from agentpal.config import PACKAGE_ROOT
from agentpal.tools import DEFAULT_EVIDENCE_ROOT, EvidenceError, EvidenceStore


def test_put_get_roundtrip_is_keyed_by_sha256(tmp_path):
    s = EvidenceStore(tmp_path)
    sha = s.put(b"hello")
    assert sha == hashlib.sha256(b"hello").hexdigest()
    assert s.get(sha) == b"hello" and s.has(sha) and s.verify(sha)
    assert (tmp_path / sha[:2] / sha).is_file()


def test_write_once_and_idempotent(tmp_path):
    s = EvidenceStore(tmp_path)
    a = s.put(b"same")
    b = s.put(b"same")
    assert a == b and len(list(tmp_path.rglob("*"))) == 2  # one dir + one object


def test_corruption_is_detected(tmp_path):
    s = EvidenceStore(tmp_path)
    sha = s.put(b"original")
    path = tmp_path / sha[:2] / sha
    os.chmod(path, 0o644)
    path.write_bytes(b"tampered")
    assert not s.verify(sha)
    with pytest.raises(EvidenceError, match="corrupted"):
        s.get(sha)
    with pytest.raises(EvidenceError, match="corrupted"):
        s.put(b"original")


@pytest.mark.parametrize("bad", ["../../etc/passwd", "A" * 64, "0" * 63, "", None,
                                 "0" * 64 + "/x"])
def test_ids_are_validated(tmp_path, bad):
    with pytest.raises(EvidenceError):
        EvidenceStore(tmp_path).get(bad)


def test_size_cap_and_type(tmp_path):
    s = EvidenceStore(tmp_path, max_object_bytes=10)
    with pytest.raises(EvidenceError, match="too large"):
        s.put(b"x" * 11)
    with pytest.raises(EvidenceError):
        s.put("text")
    with pytest.raises(EvidenceError, match="unknown"):
        s.get("0" * 64)


def test_default_root_is_under_the_gitignored_data_dir():
    assert DEFAULT_EVIDENCE_ROOT == PACKAGE_ROOT / "data" / "evidence"
    gitignore = (PACKAGE_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/*" in gitignore
