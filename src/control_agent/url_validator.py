"""
SSRF callback-URL validator for the Control Agent.

Placed here (src/control_agent/) rather than src/shared/ because the Control
Agent runs as standalone Python files copied to the bare Mac Studio host.
Importing from src.shared.* would require PYTHONPATH to include src/, which
is not guaranteed in the bare-host distribution.

NOTE: a duplicate at src/shared/url_validator.py is acceptable when the
Control Agent's bare-host distribution is consolidated into a packaged module
(deferred — see audit dexter-review:11).
"""

import os
import socket
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse
from typing import Tuple, List

# Private, loopback, and link-local IP ranges to block (RFC-1918 + RFC-3927 + loopback)
_PRIVATE_NETS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("169.254.0.0/16"),   # link-local (AWS metadata endpoint etc.)
    ip_network("127.0.0.0/8"),      # loopback
    ip_network("::1/128"),          # IPv6 loopback
    ip_network("fc00::/7"),         # IPv6 unique-local (RFC-4193)
    ip_network("fe80::/10"),        # IPv6 link-local
]


def validate_callback_url(url: str, allowed_hosts: List[str]) -> Tuple[bool, str]:
    """Validate a callback URL against SSRF risk.

    Returns (is_valid, reason_if_not).

    Logic (allowlist-first per codex High 5):
    1. Reject non-http/https schemes.
    2. If the hostname is in allowed_hosts, accept unconditionally — the
       operator has explicitly opted in (e.g. "localhost", K8s service FQDN).
    3. If allowed_hosts is empty, reject immediately (fail-closed default).
    4. Otherwise resolve the hostname to an IP and reject private/loopback/
       link-local ranges.
    """
    if not url:
        return False, "URL is empty"

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Could not parse URL: {exc}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Scheme {parsed.scheme!r} is not allowed; only http and https are permitted"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL contains no hostname"

    # Allowlist-first: explicit operator opt-in skips the RFC-1918 check.
    if hostname in allowed_hosts:
        return True, ""

    # Fail-closed: empty allowlist means no callback target is trusted.
    if not allowed_hosts:
        return (
            False,
            "ALLOWED_CALLBACK_HOSTS is not configured (fail-closed default); "
            "set it to your admin-backend hostname to enable HuggingFace download callbacks",
        )

    # Resolve and reject private/loopback/link-local addresses.
    try:
        resolved_ip = ip_address(socket.gethostbyname(hostname))
    except Exception as exc:
        return False, f"Could not resolve hostname {hostname!r}: {exc}"

    for net in _PRIVATE_NETS:
        if resolved_ip in net:
            return False, (
                f"Resolved IP {resolved_ip} for hostname {hostname!r} "
                f"is in blocked network {net} (private/loopback/link-local)"
            )

    return True, ""
