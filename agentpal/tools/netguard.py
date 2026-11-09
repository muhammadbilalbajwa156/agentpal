"""SSRF guard for outbound fetches.

`vet_url` accepts a URL only if:
* the scheme is https (http only when the caller explicitly allows it);
* there is no userinfo (`user:pass@`), the port is the scheme's default, length <= 2048;
* the host is a name or a canonical IP literal (no decimal/octal/hex obfuscation such as
  `2130706433` or `0x7f.1`), and not a local-only name (`localhost`, `*.local`,
  `*.internal`, ...);
* EVERY address the host resolves to is publicly routable: private, loopback, link-local
  (incl. cloud metadata 169.254.169.254), CGNAT, multicast, reserved, unspecified,
  benchmarking, ULA, site-local, IPv4-mapped/NAT64/6to4/Teredo forms of those are refused.

The fetch tool connects to one of the vetted addresses (IP pinning, see fetch.py), and
calls `vet_url` again for every redirect hop.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

Resolver = Callable[[str, int], Sequence[str]]

DEFAULT_PORTS = {"https": 443, "http": 80}
MAX_URL_LENGTH = 2048
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet",
                   ".corp", ".localdomain")
_EXTRA_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/4",
    "240.0.0.0/4", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
    "fc00::/7", "fec0::/10", "ff00::/8")]
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")


class BlockedTarget(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VettedTarget:
    url: httpx.URL
    scheme: str
    host: str          # ASCII (IDNA) host, no trailing dot
    port: int
    addresses: tuple[str, ...]

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]).split("%", 1)[0] for info in infos})


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    if any(ip.version == n.version and ip in n for n in _EXTRA_BLOCKED):
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return ip.is_global


def _canonical_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def vet_url(url: str | httpx.URL, *, resolver: Resolver = system_resolver,
            allow_http: bool = False) -> VettedTarget:
    if len(str(url)) > MAX_URL_LENGTH:
        raise BlockedTarget("url too long")
    try:
        u = httpx.URL(str(url))
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise BlockedTarget(f"invalid url ({type(exc).__name__})") from exc
    scheme = u.scheme.lower()
    if scheme not in DEFAULT_PORTS or (scheme == "http" and not allow_http):
        raise BlockedTarget(f"scheme {scheme or '?'} not allowed")
    if u.userinfo:
        raise BlockedTarget("credentials in url are not allowed")
    port = u.port or DEFAULT_PORTS[scheme]
    if port != DEFAULT_PORTS[scheme]:
        raise BlockedTarget(f"port {port} not allowed")
    try:
        host = u.raw_host.decode("ascii").lower().rstrip(".")
    except UnicodeDecodeError as exc:
        raise BlockedTarget("non-ascii host") from exc
    if not host:
        raise BlockedTarget("missing host")
    literal = _canonical_ip(host)
    if literal is None and all(_NUMERIC_LABEL.fullmatch(p) for p in host.split(".")):
        # all-numeric / hex labels are IP obfuscations (2130706433, 0x7f.1, 017.0.0.1)
        raise BlockedTarget("non-canonical numeric host")
    if literal is None and (host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES)
                            or "." not in host):
        raise BlockedTarget("local-only host name")
    if literal is not None:
        if isinstance(literal, ipaddress.IPv4Address) and str(literal) != host:
            raise BlockedTarget("non-canonical ip literal")
        addresses = [str(literal)]
    else:
        try:
            addresses = list(resolver(host, port))
        except (OSError, UnicodeError) as exc:
            raise BlockedTarget(f"cannot resolve host ({type(exc).__name__})") from exc
    if not addresses:
        raise BlockedTarget("host resolves to no address")
    for a in addresses:
        ip = _canonical_ip(a)
        if ip is None or not ip_is_public(ip):
            raise BlockedTarget("host resolves to a non-public address")
    return VettedTarget(url=u, scheme=scheme, host=host, port=port,
                        addresses=tuple(addresses))
