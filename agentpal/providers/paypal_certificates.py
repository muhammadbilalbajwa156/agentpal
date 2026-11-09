"""PayPal signing-certificate path validation. No AIA or other network fetching.

Trust anchors: certifi's pinned dependency bundle only. Downloaded, bundled and
operator-supplied certificates are UNTRUSTED intermediates, never trust anchors.
PolicyBuilder also checks CA constraints, chain signatures, EKU and the signing SAN.
https://cryptography.io/en/42.0.8/x509/verification/
"""

from datetime import datetime
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.verification import PolicyBuilder, Store

BUNDLED_INTERMEDIATES = Path(__file__).with_name("certs") / "paypal_sandbox_intermediates.pem"
MAX_CHAIN_BYTES = 256 * 1024
MAX_CHAIN_CERTS = 16


def trust_store() -> Store:
    return Store(x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes()))


def _load_intermediates(path: Path) -> list[x509.Certificate]:
    with path.open("rb") as stream:
        data = stream.read(MAX_CHAIN_BYTES + 1)
    if len(data) > MAX_CHAIN_BYTES:
        raise ValueError("intermediate bundle too large")
    certs = x509.load_pem_x509_certificates(data)
    if len(certs) > MAX_CHAIN_CERTS:
        raise ValueError("too many intermediates")
    return certs


def validate_signing_chain(pem: bytes, *, identity: str, now: datetime,
                           intermediates_file: str | None = None,
                           roots: Store | None = None) -> x509.Certificate:
    certs = x509.load_pem_x509_certificates(pem)
    if not certs or len(certs) > MAX_CHAIN_CERTS:
        raise ValueError("invalid certificate chain")
    leaf, intermediates = certs[0], certs[1:]
    intermediates += _load_intermediates(BUNDLED_INTERMEDIATES)
    if intermediates_file:
        intermediates += _load_intermediates(Path(intermediates_file))
    # Rebuilt at delivery time: cached certificates never bypass chain expiry checks.
    verifier = (PolicyBuilder().store(roots if roots is not None else trust_store()).time(now)
                .max_chain_depth(8).build_server_verifier(x509.DNSName(identity)))
    verifier.verify(leaf, intermediates)
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if identity not in [name.lower() for name in names.get_values_for_type(x509.DNSName)]:
        raise ValueError("signing identity SAN must match the literal policy pin")
    key = leaf.public_key()
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ValueError("invalid signing key")
    if not leaf.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
        raise ValueError("certificate cannot sign")
    return leaf
