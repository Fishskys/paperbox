"""The inbound-URL safety gate (contract section 6).

Every rule here is a refusal that has to keep working: the gate is the only thing
between an agent-supplied URL and this server's own network. DNS is monkeypatched so
the tests describe the *policy* (which addresses are refused) rather than the
machine's connectivity.
"""

from __future__ import annotations

import socket

import pytest

from app.core.config import settings
from app.services import net_guard


def fake_dns(*addresses: str):
    """A ``getaddrinfo`` that answers with exactly ``addresses``."""

    def _resolver(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [
            (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 443))
            for address in addresses
        ]

    return _resolver


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    """Default state: nobody is whitelisted."""
    monkeypatch.setattr(settings, "ingest_allow_private_hosts", "")


# --------------------------------------------------------------------------- #
# schemes and shapes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/p.pdf",
        "file:///etc/passwd",
        "gopher://example.com/",
        "example.com/p.pdf",
        "",
    ],
)
def test_only_http_and_https_are_allowed(url: str) -> None:
    with pytest.raises(net_guard.URLBlocked):
        net_guard.check_url(url)


def test_a_public_address_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("93.184.216.34"))
    assert net_guard.check_url("https://example.com/paper.pdf") == "https://example.com/paper.pdf"


# --------------------------------------------------------------------------- #
# addresses that must never be reachable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/paper.pdf",          # loopback
        "http://127.1.2.3/paper.pdf",          # whole 127/8
        "http://10.0.0.5/paper.pdf",           # private
        "http://172.16.9.9/paper.pdf",         # private
        "http://192.168.31.53/paper.pdf",      # private (the NAS)
        "http://169.254.169.254/latest/meta-data/",  # link-local / cloud metadata
        "http://0.0.0.0/paper.pdf",            # unspecified
        "http://224.0.0.1/paper.pdf",          # multicast
        "http://[::1]/paper.pdf",              # IPv6 loopback
        "http://[fc00::1]/paper.pdf",          # IPv6 unique-local
        "http://[fe80::1]/paper.pdf",          # IPv6 link-local
        "http://localhost/paper.pdf",          # local name
    ],
)
def test_internal_targets_are_refused(url: str) -> None:
    with pytest.raises(net_guard.URLBlocked) as failure:
        net_guard.check_url(url)
    assert "INGEST_ALLOW_PRIVATE_HOSTS" in str(failure.value)


def test_a_name_with_one_private_record_is_refused(monkeypatch) -> None:
    """DNS round-robin must not be a way in: every address has to pass."""
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("93.184.216.34", "10.0.0.5"))
    with pytest.raises(net_guard.URLBlocked):
        net_guard.check_url("https://mixed.example.com/paper.pdf")


def test_an_unresolvable_host_is_refused(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(net_guard.URLBlocked):
        net_guard.check_url("https://nope.invalid/paper.pdf")


def test_address_classification_covers_the_special_ranges() -> None:
    import ipaddress

    for literal in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "0.0.0.0", "::1", "fc00::1"):
        assert net_guard.is_blocked_address(ipaddress.ip_address(literal)), literal
    for literal in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
        assert not net_guard.is_blocked_address(ipaddress.ip_address(literal)), literal


# --------------------------------------------------------------------------- #
# the escape hatch
# --------------------------------------------------------------------------- #
def test_the_allowlist_accepts_a_host_name(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ingest_allow_private_hosts", "nas.local")
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("192.168.31.53"))
    assert net_guard.check_url("http://nas.local/paper.pdf")


def test_the_allowlist_accepts_a_cidr(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ingest_allow_private_hosts", "192.168.31.0/24")
    assert net_guard.check_url("http://192.168.31.53/paper.pdf")


def test_the_allowlist_parses_names_and_networks() -> None:
    parsed = net_guard.allowlist(" nas.local , 192.168.31.0/24 , bad-cidr/33 , 10.0.0.7 ")
    assert "nas.local" in parsed.names
    assert "bad-cidr/33" in parsed.names  # unparsable entries are treated as names
    import ipaddress

    assert parsed.allows_ip(ipaddress.ip_address("10.0.0.7"))
    assert parsed.allows_ip(ipaddress.ip_address("192.168.31.5"))
    assert not parsed.allows_ip(ipaddress.ip_address("192.168.32.5"))


def test_a_whitelisted_public_target_still_works(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ingest_allow_private_hosts", "93.184.216.34")
    assert net_guard.check_url("http://93.184.216.34/paper.pdf")


# --------------------------------------------------------------------------- #
# redirects
# --------------------------------------------------------------------------- #
def test_a_redirect_into_the_lan_is_refused() -> None:
    """The first URL passing says nothing about where it points next."""
    with pytest.raises(net_guard.URLBlocked):
        net_guard.check_redirect("https://example.com/paper.pdf", "http://10.0.0.5/paper.pdf")


def test_a_relative_redirect_is_resolved_before_checking(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("93.184.216.34"))
    target = net_guard.check_redirect("https://example.com/a/paper.pdf", "../b/paper.pdf")
    assert target == "https://example.com/b/paper.pdf"


def test_a_redirect_to_a_scheme_we_do_not_fetch_is_refused() -> None:
    with pytest.raises(net_guard.URLBlocked):
        net_guard.check_redirect("https://example.com/paper.pdf", "file:///etc/passwd")


__all__ = ["fake_dns"]
