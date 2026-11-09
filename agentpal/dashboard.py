"""Owner dashboard: a fixed set of static files served at ``/app/`` on the API's own origin.

Security model (lead, 2026-11-06):

* The files are read ONCE at startup into memory. A request is answered only if its path
  names one of those files exactly; anything else is a 404. There is no filesystem lookup
  at request time, so path traversal and symlink tricks cannot reach other files.
* GET and HEAD only. The static files carry no secrets; every piece of data the dashboard
  shows comes from the authenticated ``/v1`` API, called with the owner's own token.
* Same origin as the API, so no CORS is configured anywhere.
* A strict Content-Security-Policy: scripts, styles, images and API calls from this origin
  only; no framing, no plugins, no form posts elsewhere. ``nosniff``, no referrer.
"""

from __future__ import annotations

import secrets
from pathlib import Path

PREFIX = "/app"
SITE_DIR = Path(__file__).resolve().parents[1] / "frontend" / "site"
MAX_FILE_BYTES = 2 * 1024 * 1024   # vendored AG Grid / AG Charts bundles are ~1.4 MB
MAX_FILES = 64
# Exact download names map to committed artifacts, never to files in the site directory.
DOWNLOADS = {
    "openapi.json": Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json",
    "agentpal.postman_collection.json": Path(__file__).resolve().parents[1] / "postman" / "v1.collection.json",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}

# style-src-attr allows the handful of style="" attributes the design uses; scripts stay
# strictly same-origin (no inline script, no eval). index.html additionally gets a fresh
# per-response nonce so AG Grid / AG Charts can add their own <style> elements (they set the
# nonce we pass them); no other inline style element can load.
NONCE_PLACEHOLDER = b"__CSP_NONCE__"


def csp(nonce: str | None = None) -> str:
    style = "'self'" + (f" 'nonce-{nonce}'" if nonce else "")
    return ("default-src 'none'; script-src 'self'; style-src " + style + "; "
            "style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


CSP = csp()

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]


def load_site(site_dir: Path = SITE_DIR) -> dict[str, tuple[bytes, str]]:
    """Read the allow-listed files once. Only regular, non-symlink files directly in the
    folder with a known type are served; anything unexpected stops startup."""
    files: dict[str, tuple[bytes, str]] = {}
    if not site_dir.is_dir():
        return files
    for entry in sorted(site_dir.iterdir()):
        ctype = CONTENT_TYPES.get(entry.suffix.lower())
        if ctype is None or entry.is_symlink() or not entry.is_file():
            continue
        data = entry.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"dashboard file too large: {entry.name}")
        files[entry.name] = (data, ctype)
        if len(files) > MAX_FILES:
            raise ValueError("too many dashboard files")
    if site_dir == SITE_DIR:
        for name, path in DOWNLOADS.items():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"dashboard download unavailable: {name}")
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"dashboard file too large: {name}")
            files[name] = (data, "application/json")
        if len(files) > MAX_FILES:
            raise ValueError("too many dashboard files")
    return files


def matches(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class Dashboard:
    """ASGI app for ``/app`` and ``/app/<file>``. Never touches the filesystem per request."""

    def __init__(self, files: dict[str, tuple[bytes, str]]):
        self.files = dict(files)

    def __bool__(self) -> bool:
        return "index.html" in self.files

    async def __call__(self, scope, receive, send) -> None:
        method = scope.get("method")
        path = scope.get("path") or ""
        if method not in ("GET", "HEAD"):
            await self._send(send, 405, b'{"error":"method_not_allowed"}',
                             "application/json", method, [(b"allow", b"GET, HEAD")])
            return
        if path == PREFIX:
            await self._send(send, 308, b"", "text/plain; charset=utf-8", method,
                             [(b"location", b"/app/")])
            return
        name = path[len(PREFIX) + 1:] or "index.html"
        entry = self.files.get(name)
        if entry is None:
            await self._send(send, 404, b'{"error":"not_found"}', "application/json", method)
            return
        data, ctype = entry
        if name == "index.html":
            nonce = secrets.token_urlsafe(18)   # [A-Za-z0-9_-] only: safe in HTML and header
            await self._send(send, 200, data.replace(NONCE_PLACEHOLDER, nonce.encode()), ctype,
                             method, [(b"cache-control", b"no-store")], policy=csp(nonce))
            return
        await self._send(send, 200, data, ctype, method,
                         [(b"cache-control", b"public, max-age=300")])

    @staticmethod
    async def _send(send, status: int, body: bytes, ctype: str, method: str | None,
                    extra: list[tuple[bytes, bytes]] | None = None,
                    policy: str = CSP) -> None:
        headers = [(b"content-type", ctype.encode()),
                   (b"content-length", str(len(body)).encode()),
                   (b"content-security-policy", policy.encode()), *SECURITY_HEADERS,
                   *(extra or [])]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})
