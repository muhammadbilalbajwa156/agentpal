"""Encrypted workspace material; keys never enter response objects or logs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from cryptography.fernet import Fernet, InvalidToken

from .errors import PlatformError


def private_file(path: Path, initial: bytes) -> bytes:
    """Create once, without following symlinks; readers reject loose permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(initial)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # publish complete bytes atomically; never replace
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError("private configuration file has unsafe permissions or size")
        return os.read(fd, 4096)
    finally:
        os.close(fd)


class SecretVault:
    def __init__(self, key: bytes | None):
        try:
            self._cipher = Fernet(key) if key else None
        except (ValueError, TypeError):
            raise ValueError("invalid AGENTPAL_SECRETS_KEY; expected a Fernet key") from None

    def require(self):
        if self._cipher is None:
            raise PlatformError(503, "secrets_key_required")
        return self._cipher

    def encrypt(self, owner_id: str, purpose: str, fields: dict) -> bytes:
        return self.require().encrypt(json.dumps([owner_id, purpose, fields], sort_keys=True).encode())

    def decrypt(self, owner_id: str, purpose: str, blob: bytes) -> dict:
        try:
            actual_owner, actual_purpose, fields = json.loads(self.require().decrypt(blob))
            if actual_owner != owner_id or actual_purpose != purpose or not isinstance(fields, dict):
                raise ValueError
            return fields
        except (InvalidToken, ValueError, TypeError):
            raise PlatformError(503, "secrets_unavailable") from None

    @classmethod
    def configured(cls, env, data_dir: Path | None, hosted: bool):
        raw = env.get("AGENTPAL_SECRETS_KEY")
        key = raw.encode() if raw else None
        if key is None and not hosted:
            key = (private_file(data_dir / "secrets.key", Fernet.generate_key())
                   if data_dir else Fernet.generate_key())
        return cls(key)
