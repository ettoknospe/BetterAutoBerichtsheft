"""SSRF guard: validate that a user-supplied host points at a public server.

The WebUntis/IHK hosts come from user settings, so a malicious value could
aim the server's own HTTP client at localhost, LAN, or cloud-metadata
(169.254.169.254). We resolve the host and reject any address that is
loopback / link-local / private / reserved before the client connects.
"""

import ipaddress
import socket


class HostNotAllowed(Exception):
    """Raised when a host resolves to a non-public address (SSRF guard)."""


def _addr_is_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return not (
        a.is_private
        or a.is_loopback
        or a.is_link_local
        or a.is_reserved
        or a.is_multicast
        or a.is_unspecified
    )


def validate_external_host(host: str) -> None:
    """Raise HostNotAllowed unless every address `host` resolves to is public.

    `host` is a bare hostname (no scheme/port), e.g. 'le-bk-muenster.webuntis.com'.
    """
    if not host or not host.strip():
        raise HostNotAllowed("empty host")
    host = host.strip()

    # Reject an IP literal that is itself non-public without a DNS round-trip.
    try:
        ipaddress.ip_address(host)
        if not _addr_is_public(host):
            raise HostNotAllowed(f"host {host!r} is not a public address")
        return
    except ValueError:
        pass  # not a literal IP — resolve it below

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise HostNotAllowed(f"could not resolve host {host!r}: {e}")

    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise HostNotAllowed(f"host {host!r} did not resolve")
    for ip in resolved:
        if not _addr_is_public(ip):
            raise HostNotAllowed(f"host {host!r} resolves to non-public address {ip}")
