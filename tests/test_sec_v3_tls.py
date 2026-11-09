"""TLS configuration and an actual local HTTPS handshake, without external services."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest

from agentpal.config import Settings, UnsafeConfiguration


def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
            .not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False).sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(certfile), str(keyfile)


def test_tls_pair_and_unreadable_material_fail_closed(tmp_path):
    with pytest.raises(UnsafeConfiguration):
        Settings(tls_certfile="missing").check_runtime()
    with pytest.raises(UnsafeConfiguration):
        Settings(tls_certfile="missing", tls_keyfile="missing").check_runtime()
    cert, key = certificate(tmp_path)
    settings = Settings(tls_certfile=cert, tls_keyfile=key)
    assert settings.tls_options()["ssl_certfile"] == cert
    assert settings.tls_options()["ssl_keyfile"] == key


def test_production_public_bind_without_tls_is_refused(tmp_path):
    settings = Settings(environment="production", auth_mode="required", api_tokens_file="tokens",
                        db_path=str(tmp_path / "risk.db"), allowed_hosts=["localhost"])
    with pytest.raises(UnsafeConfiguration, match="TLS"):
        settings.check_runtime(host="0.0.0.0", spend_policy_configuration="operator")


def test_cli_passes_tls_options_for_both_serving_paths(tmp_path, monkeypatch):
    from agentpal import __main__ as cli
    from agentpal.config import get_settings
    cert, key = certificate(tmp_path)
    monkeypatch.setenv("AGENTPAL_DB_PATH", str(tmp_path / "risk.db"))
    monkeypatch.setenv("AGENTPAL_ANALYZER_CHAIN", "heuristic")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    get_settings.cache_clear()
    try:
        for extra in ([], ["--platform"]):
            cli.main(["serve", "--ssl-certfile", cert, "--ssl-keyfile", key, *extra])
        assert len(calls) == 2
        assert all(call["ssl_certfile"] == cert and call["ssl_keyfile"] == key
                   and call["proxy_headers"] is False for call in calls)
    finally:
        get_settings.cache_clear()


def test_real_local_https_listener(tmp_path):
    cert, key = certificate(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "AGENTPAL_DB_PATH": str(tmp_path / "https.db"),
           "AGENTPAL_AUTH_MODE": "disabled_insecure_dev", "AGENTPAL_ANALYZER_CHAIN": "heuristic"}
    process = subprocess.Popen([sys.executable, "-m", "agentpal", "serve", "--port", str(port),
        "--ssl-certfile", cert, "--ssl-keyfile", key], env=env,
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with httpx.Client(verify=ssl.create_default_context(cafile=cert), trust_env=False, timeout=1) as client:
            for _ in range(100):
                assert process.poll() is None, "HTTPS server exited before readiness"
                try:
                    response = client.get(f"https://127.0.0.1:{port}/health")
                    break
                except httpx.TransportError:
                    time.sleep(0.05)
            else:
                pytest.fail("HTTPS server never became ready")
        assert response.status_code == 200
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1, trust_env=False)
    finally:
        process.terminate()
        process.communicate(timeout=10)
