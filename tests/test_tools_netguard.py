"""SSRF guard: only public addresses on default ports over https; no obfuscated or
local-only hosts; every resolved address must be public. Offline (fake resolver)."""

from __future__ import annotations

import ipaddress

import pytest

from agentpal.tools.netguard import BlockedTarget, ip_is_public, vet_url


def resolver_for(*addrs):
    def resolve(host, port):
        return list(addrs)
    return resolve


PUBLIC = resolver_for("93.184.215.14")


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.0.1", "172.31.255.255",
    "192.168.1.1", "169.254.169.254", "169.254.0.1", "100.64.0.1", "0.0.0.0", "0.1.2.3",
    "224.0.0.1", "240.0.0.1", "255.255.255.255", "198.18.0.1", "192.0.0.8", "192.0.2.1",
    "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a00:1",
    "2002:7f00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "2001:db8::1",
])
def test_non_public_addresses_are_refused(ip):
    assert not ip_is_public(ipaddress.ip_address(ip))
    with pytest.raises(BlockedTarget, match="non-public"):
        vet_url("https://shop.example.com/", resolver=resolver_for(ip))


@pytest.mark.parametrize("ip", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_pass(ip):
    t = vet_url("https://shop.example.com/item?id=1", resolver=resolver_for(ip))
    assert t.addresses == (ip,) and t.host == "shop.example.com" and t.port == 443
    assert t.origin == "https://shop.example.com"


def test_one_private_answer_among_public_ones_is_refused():
    with pytest.raises(BlockedTarget, match="non-public"):
        vet_url("https://shop.example.com/", resolver=resolver_for("8.8.8.8", "10.0.0.5"))


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/meta-data/",
    "https://[::ffff:127.0.0.1]/", "https://10.1.2.3/",
])
def test_private_ip_literals_are_refused_without_dns(url):
    def no_dns(host, port):
        raise AssertionError("IP literals must not be resolved")
    with pytest.raises(BlockedTarget):
        vet_url(url, resolver=no_dns)


@pytest.mark.parametrize("url", [
    "https://2130706433/", "https://0x7f000001/", "https://0x7f.1/", "https://017.0.0.1/",
    "https://127.1/", "https://0177.0.0.1/",
])
def test_obfuscated_numeric_hosts_are_refused(url):
    with pytest.raises(BlockedTarget, match="numeric|non-canonical|invalid url"):
        vet_url(url, resolver=PUBLIC)


@pytest.mark.parametrize("url", [
    "https://localhost/", "https://LOCALHOST./", "https://foo.localhost/",
    "https://printer.local/", "https://metadata.google.internal/", "https://intranet/",
    "https://router.home.arpa/",
])
def test_local_only_names_are_refused(url):
    with pytest.raises(BlockedTarget, match="local-only"):
        vet_url(url, resolver=PUBLIC)


@pytest.mark.parametrize("url,match", [
    ("http://shop.example.com/", "scheme"), ("ftp://shop.example.com/", "scheme"),
    ("file:///etc/passwd", "scheme"), ("gopher://shop.example.com/", "scheme"),
    ("https://user:pw@shop.example.com/", "credentials"),
    ("https://shop.example.com:8443/", "port"), ("https://shop.example.com:22/", "port"),
    ("https://" + "a" * 2050 + ".com/", "too long"),
])
def test_scheme_port_userinfo_and_length(url, match):
    with pytest.raises(BlockedTarget, match=match):
        vet_url(url, resolver=PUBLIC)


def test_http_only_when_explicitly_allowed():
    t = vet_url("http://shop.example.com/", resolver=PUBLIC, allow_http=True)
    assert t.scheme == "http" and t.port == 80
    with pytest.raises(BlockedTarget, match="port"):
        vet_url("http://shop.example.com:443/", resolver=PUBLIC, allow_http=True)


def test_resolution_failure_and_empty_answer_are_refused():
    def fail(host, port):
        raise OSError("nxdomain")
    with pytest.raises(BlockedTarget, match="cannot resolve"):
        vet_url("https://nope.example.com/", resolver=fail)
    with pytest.raises(BlockedTarget, match="no address"):
        vet_url("https://nope.example.com/", resolver=resolver_for())


def test_idn_hosts_are_vetted_in_ascii_form():
    t = vet_url("https://bücher.example/", resolver=PUBLIC)
    assert t.host == "xn--bcher-kva.example"
