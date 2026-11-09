"""Unicode and domain canonicalization for security-sensitive comparisons (P0.6).

Policy
------
* ``canonical_text``: NFKC, then remove Default_Ignorable_Code_Point characters
  (zero-width, bidi controls, variation selectors…), casefold, and collapse whitespace.
  Used for every text detector and every name comparison.
* ``detection_text``: ``canonical_text`` plus folding of *non-ASCII* characters whose
  UTS #39 prototype is ASCII (Cyrillic 'о' → 'o'). ASCII is left alone, because the
  confusables table also maps ASCII (e.g. 'm' → 'rn'), which would break regexes.
* ``skeleton``: the UTS #39 confusable skeleton (NFD, remove ignorables, map, NFD),
  applied after ``canonical_text``. Used only to ask "could these two identifiers be
  confused?". Never displayed, and never proof of identity or of fraud.
* Domains: IDNs are stored as punycode (schema). ``registrable_domain`` uses the
  maintained Public Suffix List (``publicsuffixlist``, including the private section, so
  ``a.github.io`` and ``b.github.io`` are different registrable domains). Nothing is
  ever fetched from a merchant-controlled host.

Unicode data: vendored Unicode 18.0.0 files under ``agentpal/resources`` (see README).
"""

from __future__ import annotations

import gzip
import ipaddress
import unicodedata
from functools import lru_cache
from pathlib import Path

from publicsuffixlist import PublicSuffixList

_RES = Path(__file__).resolve().parent / "resources"
CONFUSABLES_FILE = _RES / "confusables-18.0.0.txt.gz"
IGNORABLE_FILE = _RES / "default_ignorable-18.0.0.txt"
CONFUSABLES_SHA256 = "6ed3ee967c9dfdf6677d563c9985182fbc50a2efb7d6059cd57b2e2ce18f5b92"


@lru_cache(maxsize=1)
def _ignorable() -> frozenset[int]:
    cps: set[int] = set()
    for line in IGNORABLE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lo, _, hi = line.partition("..")
        cps.update(range(int(lo, 16), int(hi or lo, 16) + 1))
    return frozenset(cps)


@lru_cache(maxsize=1)
def _confusables() -> dict[str, str]:
    table: dict[str, str] = {}
    with gzip.open(CONFUSABLES_FILE, "rt", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            src, dst, *_ = (part.strip() for part in line.split(";"))
            table[chr(int(src, 16))] = "".join(chr(int(cp, 16)) for cp in dst.split())
    return table


@lru_cache(maxsize=1)
def _psl() -> PublicSuffixList:
    return PublicSuffixList()  # bundled list; no network access


def strip_ignorable(s: str) -> str:
    ign = _ignorable()
    return "".join(ch for ch in s if ord(ch) not in ign)


def canonical_text(s: str) -> str:
    s = strip_ignorable(unicodedata.normalize("NFKC", s))
    return " ".join(s.casefold().split())


def detection_text(s: str) -> str:
    table = _confusables()
    out = []
    for ch in canonical_text(s):
        if ord(ch) > 127:
            proto = table.get(ch)
            if proto and proto.isascii():
                ch = proto
        out.append(ch)
    return "".join(out).casefold()


def skeleton(s: str) -> str:
    """UTS #39 skeleton of canonical_text(s)."""
    table = _confusables()
    x = strip_ignorable(unicodedata.normalize("NFD", canonical_text(s)))
    x = "".join(table.get(ch, ch) for ch in x)
    return unicodedata.normalize("NFD", x)


def name_key(name: str) -> str:
    """Comparison key for merchant names: confusable-insensitive, spacing-insensitive."""
    return "".join(skeleton(name).split())


def scripts(s: str) -> set[str]:
    """Approximate script set of the letters in s, from Unicode character names.
    Python has no Script property; this is sufficient to detect Latin/Cyrillic/Greek
    mixing but is an approximation (documented)."""
    found = set()
    for ch in unicodedata.normalize("NFKC", s):
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        found.add(name.split(" ", 1)[0] if name else "UNKNOWN")
    return found


def is_mixed_script(s: str) -> bool:
    return len(scripts(s)) > 1


# ------------------------------------------------------------------ domains

def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def unicode_host(host: str) -> str:
    """Punycode labels -> Unicode for confusable analysis (display/analysis only)."""
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(label)
    return ".".join(labels)


def registrable_domain(host: str) -> str | None:
    """eTLD+1 via the Public Suffix List (ICANN + private sections)."""
    host = host.lower().rstrip(".")
    if not host or is_ip(host):
        return None
    return _psl().privatesuffix(host) or host


def registrable_label(host: str) -> str:
    reg = registrable_domain(host) or host
    suffix = _psl().publicsuffix(reg) or ""
    label = reg[: -len(suffix) - 1] if suffix and reg.endswith("." + suffix) else reg
    return label.split(".")[-1]


def host_matches(host: str, domain: str, include_subdomains: bool) -> bool:
    """Exact host match, or (opt-in) a dot-boundary subdomain within the same registrable
    domain. Never a naive substring test."""
    host, domain = host.lower().rstrip("."), domain.lower().rstrip(".")
    if host == domain:
        return True
    return (include_subdomains and host.endswith("." + domain)
            and registrable_domain(host) == registrable_domain(domain))


def is_subdomain_of(host: str, domain: str) -> bool:
    host, domain = host.lower().rstrip("."), domain.lower().rstrip(".")
    return host != domain and host.endswith("." + domain)
