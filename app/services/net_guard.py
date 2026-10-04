"""Inbound-URL safety gate (contract section 6).

``paper_import(url=...)`` and the REST URL import share this guard: an agent (or a
human) can point paperbox at any address, so the server must not become a way to
read the metadata service or to probe the LAN. The rules are deliberately blunt:

* only ``http``/``https``;
* **every** address a host name resolves to has to pass (a name with one public and
  one private A-record is refused, so DNS round-robin cannot slip past);
* loopback, private, link-local (including the cloud metadata address),
  unspecified, multicast and reserved ranges are refused by default;
* redirects are re-checked **per hop**, because the first URL passing says nothing
  about where it points next;
* ``INGEST_ALLOW_PRIVATE_HOSTS`` (default empty) is the escape hatch, and it takes
  host names or CIDRs -- importing a PDF from the NAS is a deliberate act. A **name**
  in that list is trusted as a whole (its addresses are not checked afterwards: the
  operator vouched for the name, and an entry that stops working the moment DNS
  changes would be a trap); a **CIDR** entry whitelists addresses only.

Residual risk, stated plainly: the address is checked before the request and the
socket layer resolves again when it connects, so a hostile DNS server could answer
differently the second time (DNS rebinding). Closing that properly needs connecting
to the validated IP with the original SNI, which breaks certificate validation for
``https``; the per-hop check is the trade-off this project accepts, and the guard
still runs in the worker (not only at request time) so a rebind has to win twice.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from app.core.config import settings

#: Schemes an ingestion URL may use.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hosts that need no resolution and are always internal.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: Redirect statuses worth following (httpx's default set).
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: How many redirects a single download may follow.
MAX_REDIRECTS = 5


class URLBlocked(Exception):
    """The URL points somewhere this server refuses to fetch from."""

    def __init__(self, reason: str, *, url: str | None = None) -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"{reason}: {url}" if url else reason)


@dataclass(frozen=True, slots=True)
class AllowList:
    """``INGEST_ALLOW_PRIVATE_HOSTS`` parsed into names and networks."""

    names: frozenset[str]
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @property
    def empty(self) -> bool:
        return not self.names and not self.networks

    def allows_name(self, host: str) -> bool:
        return host.lower() in self.names

    def allows_ip(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(address in network for network in self.networks)


def allowlist(raw: str | None = None) -> AllowList:
    """Parse the escape hatch: comma-separated host names and/or CIDRs."""
    text = settings.ingest_allow_private_hosts if raw is None else raw
    names: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in (text or "").split(","):
        entry = item.strip()
        if not entry:
            continue
        if "/" in entry or _looks_like_address(entry):
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                names.add(entry.lower())
            continue
        names.add(entry.lower())
    return AllowList(names=frozenset(names), networks=tuple(networks))


def _looks_like_address(entry: str) -> bool:
    try:
        ipaddress.ip_address(entry)
    except ValueError:
        return False
    return True


def is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything that is not a public unicast address.

    Covers loopback, private (RFC 1918 / fc00::7), link-local (169.254/16, fe80::/10
    -- the cloud metadata service lives at 169.254.169.254), unspecified
    (``0.0.0.0``/``::``), multicast, reserved and shared space.
    """
    return not address.is_global or address.is_multicast


def resolve(host: str, port: int | None = None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every A/AAAA address for ``host`` (raises :class:`URLBlocked` when unknown)."""
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise URLBlocked(f"cannot resolve host {host!r} ({exc})") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        literal = info[4][0]
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:  # pragma: no cover - getaddrinfo only returns literals
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise URLBlocked(f"host {host!r} resolved to no usable address")
    return addresses


def check_url(url: str) -> str:
    """Validate one URL; returns it unchanged when it may be fetched.

    Raises :class:`URLBlocked` with a human-readable reason otherwise -- the reason
    is what the MCP error ``hint`` and the REST 400 body carry, so it names the
    variable to set (``INGEST_ALLOW_PRIVATE_HOSTS``) when that is the fix.
    """
    candidate = (url or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise URLBlocked("only http(s) URLs may be imported", url=candidate)
    host = parsed.hostname or ""
    if not host:
        raise URLBlocked("URL has no host", url=candidate)

    allowed = allowlist()
    if allowed.allows_name(host):
        # A whitelisted *name* means "this host is trusted, wherever it points":
        # checking its addresses afterwards would make the name entry useless (an
        # entry that only works while the IP never changes is a CIDR entry).
        return candidate
    if host.lower() in _LOCAL_NAMES and not allowed.allows_name(host):
        raise URLBlocked(
            f"{host} is a local address; add it to INGEST_ALLOW_PRIVATE_HOSTS to allow it",
            url=candidate,
        )
    if _looks_like_address(host):
        address = ipaddress.ip_address(host)
        if not allowed.allows_ip(address) and is_blocked_address(address):
            raise URLBlocked(
                f"{host} is not a public address; add it to INGEST_ALLOW_PRIVATE_HOSTS "
                "to allow it",
                url=candidate,
            )
        return candidate

    for address in resolve(host, parsed.port):
        if allowed.allows_ip(address):
            continue
        if is_blocked_address(address):
            raise URLBlocked(
                f"{host} resolves to the non-public address {address}; add the host "
                "or CIDR to INGEST_ALLOW_PRIVATE_HOSTS to allow it",
                url=candidate,
            )
    return candidate


def check_redirect(current_url: str, location: str) -> str:
    """Resolve and validate one redirect hop (relative locations included)."""
    target = urljoin(current_url, location)
    return check_url(target)


@contextmanager
def open_stream(url: str, *, timeout: float, max_redirects: int = MAX_REDIRECTS) -> Iterator:
    """Open a guarded streaming GET, re-checking every redirect hop.

    ``follow_redirects=False`` is the point: httpx would otherwise chase a public
    URL into ``http://169.254.169.254/`` without asking us. The response is yielded
    with the body still unread, so the caller keeps the streaming size guard.
    """
    import httpx

    check_url(url)
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        current = url
        for _ in range(max_redirects + 1):
            request = client.build_request("GET", current)
            response = client.send(request, stream=True)
            if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                location = response.headers["location"]
                response.close()
                current = check_redirect(current, location)
                continue
            try:
                yield response
            finally:
                response.close()
            return
        raise URLBlocked(f"too many redirects (limit {max_redirects})", url=url)
    finally:
        client.close()


__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_REDIRECTS",
    "REDIRECT_STATUSES",
    "AllowList",
    "URLBlocked",
    "allowlist",
    "check_redirect",
    "check_url",
    "is_blocked_address",
    "open_stream",
    "resolve",
]
