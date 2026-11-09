"""RFC 6238 TOTP primitive, using only the standard library.

The service fixes SHA-1, six digits, a 30-second step and a +/-1 step window.
Other RFC algorithms/digit widths are supported here for published vector checks.
"""
import hashlib
import hmac
import struct


def totp(secret: bytes, at: int, *, digits: int = 6, algorithm: str = "sha1") -> str:
    if algorithm not in ("sha1", "sha256", "sha512") or digits not in (6, 8) or at < 0:
        raise ValueError("invalid TOTP parameters")
    message = struct.pack(">Q", int(at) // 30)
    digest = hmac.new(secret, message, getattr(hashlib, algorithm)).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fff_ffff
    return str(number % (10 ** digits)).zfill(digits)


def match_counter(secret: bytes, code: object, at: int, last_counter: int) -> int | None:
    import re
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    # Adjacent counters can rarely collide in six digits. Never treat a previously
    # consumed value as a newer proof merely because the next counter matches too.
    if last_counter >= 0 and hmac.compare_digest(totp(secret, last_counter * 30), code):
        return None
    found = None
    for counter in range(max(0, at // 30 - 1), at // 30 + 2):
        matches = hmac.compare_digest(totp(secret, counter * 30), code)
        if matches and counter > last_counter:
            found = counter
    return found
