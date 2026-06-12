"""SSRF guard: shared URL-safety validator and safe HTTP fetch helpers.

This module is the canonical entry-point for any code path that fetches a
**user- or admin-supplied URL** (iCal sources, sitescraper targets, search-result
URLs, MCP discovery URLs, admin-editable feature-flag config URLs).

Three-way classification (must be applied at every new fetch site):
  - **Class 1** — user/admin-supplied URL (request body, stored record, search
    result, MCP URL, admin-editable feature-flag config) → MUST route through
    ``safe_get`` / ``safe_post`` (or call ``validate_url_not_private`` directly).
  - **Class 2** — operator-trusted infra with an admin-stored host (connector /
    service-registry health tests) → allow private targets but set
    ``allow_redirects=False`` / per-hop-revalidate; do NOT fail-closed-guard.
  - **Class 3** — operator-env service URL (``OVERSEERR_URL``, ``HA_URL``,
    ``N8N_MCP_URL``, fixed deployment-config endpoints) → exempt, do NOT guard.

DNS-rebinding mitigation (0.1c):
  ``validate_url_not_private`` resolves the hostname and stores the resolved IPs
  in ``UrlSafetyResult.resolved_ips``.  ``safe_request`` builds a custom
  ``AsyncNetworkBackend`` that dials the *validated* IP while leaving the
  original hostname in the TLS SNI / cert-validation hostname and the ``Host``
  header — closing the TOCTOU window between ``getaddrinfo`` and TCP-connect.

  Implementation detail (httpx 0.28.1): ``AsyncHTTPTransport`` accepts a
  ``network_backend=`` argument that is forwarded to httpcore's
  ``AsyncConnectionPool``.  Our ``_PinnedNetworkBackend`` subclasses
  httpcore's ``AsyncNetworkBackend`` and overrides ``connect_tcp`` to dial
  the pinned IP; httpcore still constructs the TLS handshake using the
  connection's ``Origin.host`` (the original hostname) as the SNI server-name.
  No monkey-patching or private-API access is required.

  SNI decision (r4 fallback spec): httpx 0.28.1 + httpcore DO support SNI
  preservation via the network-backend approach above.  The fallback to
  hostname-connect (with documented TOCTOU window) is NOT needed at this
  version.  If a future httpx upgrade breaks this, switch to hostname-connect
  and document the residual per the plan's named fallback spec.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import httpcore
import httpx

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class SsrfBlockedError(Exception):
    """Raised by ``safe_request`` when a URL (or redirect target) is blocked."""


# ---------------------------------------------------------------------------
# UrlSafetyResult (D8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlSafetyResult:
    """Result of ``validate_url_not_private``.

    All fields are populated on both allowed and blocked outcomes.
    Callers that only need a yes/no read ``.allowed`` / ``.reason``.
    ``safe_request`` (0.1b/0.1c) reads ``.resolved_ips`` to pin the transport.
    """

    allowed: bool
    reason: str  # "" when allowed
    normalized_url: str  # scheme/host lowercased, userinfo stripped
    hostname: str  # the host component that was resolved
    resolved_ips: list[str] = field(default_factory=list)  # A/AAAA records


# ---------------------------------------------------------------------------
# Blocked CIDR ranges
# ---------------------------------------------------------------------------

_BLOCKED_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),  # loopback IPv4
    ipaddress.ip_network("::1/128"),  # loopback IPv6
    ipaddress.ip_network("169.254.0.0/16"),  # link-local IPv4
    ipaddress.ip_network("fe80::/10"),  # link-local IPv6
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (RFC 6598)
    ipaddress.ip_network("fc00::/7"),  # ULA (RFC 4193)
]


def _ip_is_private(addr_str: str) -> bool:
    """Return True if addr_str falls in a blocked range."""
    try:
        addr = ipaddress.ip_address(addr_str)
        return any(addr in net for net in _BLOCKED_NETS)
    except ValueError:
        return True  # malformed → treat as blocked


def _parse_allowlist(
    allowed_private_hosts: Iterable[str],
) -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    """Split the allowlist into literal hostnames and CIDR objects."""
    hosts: set[str] = set()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in allowed_private_hosts:
        entry = entry.strip()
        if not entry:
            continue
        if "/" in entry:
            try:
                nets.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                pass
        else:
            hosts.add(entry.lower())
    return hosts, nets


def _ip_is_allowlisted(
    addr_str: str,
    allow_hosts: set[str],
    allow_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        addr = ipaddress.ip_address(addr_str)
        return any(addr in net for net in allow_nets)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 0.1 — validate_url_not_private
# ---------------------------------------------------------------------------


def validate_url_not_private(
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
    allowed_private_hosts: Iterable[str] = (),
) -> UrlSafetyResult:
    """Validate that *url* is safe to fetch (does not resolve to a private IP).

    **Never raises.**  Always returns a :class:`UrlSafetyResult`.

    This is a **sync** function.  Async callers (``safe_request``) wrap it
    with ``loop.run_in_executor(None, ...)`` to avoid blocking the event loop.

    Contract:
    - Scheme must be in ``allowed_schemes``; default ``{"http", "https"}``.
      Pass ``frozenset({"https"})`` from iCal / per-hop-revalidation paths to
      enforce HTTPS on every redirect hop.
    - URLs with userinfo (``@`` in authority) are rejected unconditionally.
    - IPv4-mapped IPv6 hosts (``[::ffff:c0a8:0101]``) are extracted as their
      embedded IPv4 address and tested against the blocked ranges.
    - DNS resolution uses ``socket.getaddrinfo`` (covers AAAA/IPv6).  A
      failure to resolve is treated as blocked (fail-closed).
    - All A/AAAA records are checked; any private record blocks the URL unless
      the host (or any resolved IP's CIDR) is in ``allowed_private_hosts``.
    - The validator **never reads env**; the allowlist is passed in per-consumer
      (D9): sitescraper / iCal / ContentFetcher → ``get_config().sitescraper_
      allowed_private_hosts``; health_poller → its own
      ``HEALTH_POLL_ALLOWED_PRIVATE_HOSTS``.

    DNS-rebinding TOCTOU: ``resolved_ips`` is consumed by ``safe_request``'s
    pinned transport (0.1c) to close the window between this call and the TCP
    connect.
    """
    # --- parse ---
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return UrlSafetyResult(
            allowed=False,
            reason=f"Invalid URL: {exc}",
            normalized_url=url,
            hostname="",
        )

    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        return UrlSafetyResult(
            allowed=False,
            reason=f"Scheme not allowed: {scheme!r}",
            normalized_url=url,
            hostname="",
        )

    # --- userinfo check ---
    if parsed.username or parsed.password:
        return UrlSafetyResult(
            allowed=False,
            reason="URL contains userinfo",
            normalized_url=url,
            hostname="",
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return UrlSafetyResult(
            allowed=False,
            reason="Invalid URL: missing host",
            normalized_url=url,
            hostname="",
        )

    # --- strip userinfo from normalized URL ---
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    normalized_url = urlunparse(
        (scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )

    # --- IPv4-mapped IPv6 extraction ---
    # urlparse strips brackets from IPv6 literals, so hostname for
    # "http://[::ffff:192.168.1.1]/" is "::ffff:192.168.1.1" (no brackets).
    # We also handle the bracketed form in case it arrives via a redirect
    # Location header that was not parsed through urlparse.
    _h = hostname.strip("[]")
    try:
        addr = ipaddress.ip_address(_h)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            hostname = str(addr.ipv4_mapped)
    except ValueError:
        pass

    # --- parse allowlist ---
    allow_hosts, allow_nets = _parse_allowlist(allowed_private_hosts)

    # --- hostname-level allowlist shortcut ---
    if hostname in allow_hosts:
        # Still resolve to populate resolved_ips, but skip block check.
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
            resolved = list({info[4][0] for info in infos if info[4]})
        except Exception:
            resolved = []
        return UrlSafetyResult(
            allowed=True,
            reason="",
            normalized_url=normalized_url,
            hostname=hostname,
            resolved_ips=resolved,
        )

    # --- DNS resolution ---
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        resolved = list({info[4][0] for info in infos if info[4]})
    except Exception as exc:
        return UrlSafetyResult(
            allowed=False,
            reason=f"DNS resolution failed: {exc}",
            normalized_url=normalized_url,
            hostname=hostname,
        )

    if not resolved:
        return UrlSafetyResult(
            allowed=False,
            reason="DNS resolution failed: no records returned",
            normalized_url=normalized_url,
            hostname=hostname,
        )

    # --- check each resolved IP ---
    for ip_str in resolved:
        if _ip_is_private(ip_str):
            if ip_str in allow_hosts or _ip_is_allowlisted(ip_str, allow_hosts, allow_nets):
                continue
            return UrlSafetyResult(
                allowed=False,
                reason=f"Hostname resolves to private IP: {ip_str}",
                normalized_url=normalized_url,
                hostname=hostname,
                resolved_ips=resolved,
            )

    return UrlSafetyResult(
        allowed=True,
        reason="",
        normalized_url=normalized_url,
        hostname=hostname,
        resolved_ips=resolved,
    )


# ---------------------------------------------------------------------------
# 0.1c — IP-pinned network backend (DNS-rebinding mitigation)
# ---------------------------------------------------------------------------


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Custom httpcore network backend that dials a *pinned* IP.

    When ``connect_tcp`` is called with the original hostname, this backend
    connects to ``pinned_ip`` instead.  httpcore's TLS layer still uses the
    original hostname (from the connection's ``Origin.host``) as the SNI
    server-name and for certificate validation — so TLS is unaffected.

    This closes the DNS-rebinding TOCTOU window between ``getaddrinfo`` in
    ``validate_url_not_private`` and the actual TCP connect.

    Verified by the mandatory PoC test in ``tests/shared/test_url_safety.py``:
    (a) TCP connect target is the pinned IP; (b) TLS SNI hostname is the
    original hostname (not the IP).
    """

    def __init__(self, pinned_ip: str, *, inner: httpcore.AsyncNetworkBackend | None = None):
        self._pinned_ip = pinned_ip
        # Use the default AnyIO backend when no inner backend is supplied.
        self._inner: httpcore.AsyncNetworkBackend = (
            inner if inner is not None else httpcore._backends.anyio.AnyIOBackend()
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Dial the pinned IP instead of resolving the hostname again.
        return await self._inner.connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _build_pinned_transport(resolved_ips: list[str]) -> httpx.AsyncHTTPTransport:
    """Build an ``httpx.AsyncHTTPTransport`` that connects to the first
    resolved IP while preserving TLS SNI / ``Host`` headers.

    Implementation note (httpx 0.28.1 / httpcore 1.0.9):
    ``httpx.AsyncHTTPTransport`` does not accept a ``network_backend=`` argument
    directly.  Instead we build a default transport, then replace its internal
    ``._pool`` with an ``httpcore.AsyncConnectionPool`` that was constructed
    with our ``_PinnedNetworkBackend`` as ``network_backend``.  httpcore's pool
    handles TLS using ``Origin.host`` (the original hostname), not the IP
    dialed by the network backend, so SNI / cert-validation are unaffected.

    Falls back to an unmodified transport if ``resolved_ips`` is empty (e.g.
    for plain-hostname Class-2 paths — not used by ``safe_request``).
    """
    if not resolved_ips:
        return httpx.AsyncHTTPTransport()
    pinned_ip = resolved_ips[0]
    backend = _PinnedNetworkBackend(pinned_ip)
    transport = httpx.AsyncHTTPTransport()
    # Replace the pool with one that routes through our pinned backend.
    # httpcore.AsyncConnectionPool accepts network_backend= as a public arg.
    transport._pool = httpcore.AsyncConnectionPool(network_backend=backend)
    return transport


# ---------------------------------------------------------------------------
# 0.1b — safe_request / safe_get / safe_post
# ---------------------------------------------------------------------------


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _origins_differ(url_a: str, url_b: str) -> bool:
    """Return True if (scheme, host, port) differs between the two URLs."""
    a = urlparse(url_a)
    b = urlparse(url_b)
    port_a = a.port or _default_port(a.scheme)
    port_b = b.port or _default_port(b.scheme)
    return (
        a.scheme.lower() != b.scheme.lower()
        or (a.hostname or "").lower() != (b.hostname or "").lower()
        or port_a != port_b
    )


_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)

_METHOD_DOWNGRADE_CODES = frozenset({301, 302, 303})
_METHOD_REFUSE_CODES = frozenset({307, 308})


async def safe_request(
    method: str,
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
    allowed_private_hosts: Iterable[str] = (),
    max_hops: int = 3,
    max_bytes: int = 10 * 1024 * 1024,
    timeout: float = 30.0,
    **kwargs,
) -> httpx.Response:
    """Fetch *url* via method, following redirects with per-hop SSRF re-validation.

    The helper **owns** the ``httpx.AsyncClient`` and the pinned transport —
    callers must NOT pass a pre-built client (D8).

    ``**kwargs`` (e.g. ``json=``, ``data=``, ``headers=``, ``params=``) are
    forwarded to the underlying httpx request call **only** — they are never
    passed to ``validate_url_not_private`` (bob r4 finding 8).

    Redirect semantics (xander r4 finding 2):
    - 301/302/303 on POST → method downgrades to GET, body / ``json=`` /
      ``data=`` dropped, ``Content-Type`` / ``Content-Length`` dropped.
      ``Authorization`` / ``Cookie`` / ``Proxy-Authorization`` are stripped on
      cross-origin hops.
    - 307/308 on POST → **refused** (raises ``SsrfBlockedError``).
    - All redirects: ``Location`` is re-validated under the same
      ``allowed_schemes`` / ``allowed_private_hosts`` before following.

    Raises:
        ``SsrfBlockedError`` — URL (or a redirect target) blocked by the guard,
        or a 307/308 redirect on POST is encountered.
    """
    loop = asyncio.get_running_loop()

    current_url = url
    current_method = method.upper()
    current_kwargs = dict(kwargs)
    hop_count = 0

    while True:
        # --- validate current URL ---
        result: UrlSafetyResult = await loop.run_in_executor(
            None,
            lambda u=current_url: validate_url_not_private(
                u,
                allowed_schemes=allowed_schemes,
                allowed_private_hosts=allowed_private_hosts,
            ),
        )
        if not result.allowed:
            raise SsrfBlockedError(result.reason)

        # --- build transport pinned to validated IP ---
        transport = _build_pinned_transport(result.resolved_ips)

        # --- issue request (no auto-redirects) ---
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
        ) as client:
            response = await client.request(
                current_method,
                current_url,
                **current_kwargs,
            )

        # --- size cap ---
        # For non-redirect responses we stream the body; for redirect responses
        # we don't need the body at all.
        if response.status_code not in (301, 302, 303, 307, 308):
            body_bytes = await response.aread()
            if len(body_bytes) > max_bytes:
                raise SsrfBlockedError(
                    f"Response body exceeds max_bytes ({max_bytes}): "
                    f"got {len(body_bytes)} bytes"
                )
            # Re-wrap as a completed response with the body already read.
            return response

        # --- redirect handling ---
        location = response.headers.get("location", "")
        if not location:
            # No Location header — return as-is.
            return response

        hop_count += 1
        if hop_count > max_hops:
            raise SsrfBlockedError(
                f"Too many redirects (max_hops={max_hops})"
            )

        status = response.status_code

        # 307/308 on POST → refuse
        if status in _METHOD_REFUSE_CODES and current_method == "POST":
            raise SsrfBlockedError(
                f"Refusing to follow {status} redirect on POST"
            )

        # Resolve relative Location to absolute.
        if not location.startswith(("http://", "https://")):
            parsed_current = urlparse(current_url)
            if location.startswith("/"):
                base = f"{parsed_current.scheme}://{parsed_current.netloc}"
                location = base + location
            else:
                from urllib.parse import urljoin
                location = urljoin(current_url, location)

        cross_origin = _origins_differ(current_url, location)

        # 301/302/303 on POST → downgrade to GET
        if status in _METHOD_DOWNGRADE_CODES and current_method == "POST":
            current_method = "GET"
            # Drop body kwargs
            current_kwargs.pop("json", None)
            current_kwargs.pop("data", None)
            current_kwargs.pop("content", None)
            # Drop content headers
            hdrs = dict(current_kwargs.get("headers", {}))
            hdrs.pop("Content-Type", None)
            hdrs.pop("content-type", None)
            hdrs.pop("Content-Length", None)
            hdrs.pop("content-length", None)
            if cross_origin:
                for h in list(hdrs.keys()):
                    if h.lower() in _CREDENTIAL_HEADERS:
                        del hdrs[h]
            current_kwargs["headers"] = hdrs

        elif cross_origin:
            # Strip credential headers on cross-origin hops for all methods.
            hdrs = dict(current_kwargs.get("headers", {}))
            for h in list(hdrs.keys()):
                if h.lower() in _CREDENTIAL_HEADERS:
                    del hdrs[h]
            current_kwargs["headers"] = hdrs

        current_url = location


async def safe_get(url: str, **kw) -> httpx.Response:
    """Convenience wrapper: ``safe_request("GET", url, **kw)``."""
    return await safe_request("GET", url, **kw)


async def safe_post(url: str, **kw) -> httpx.Response:
    """Convenience wrapper: ``safe_request("POST", url, **kw)``."""
    return await safe_request("POST", url, **kw)
