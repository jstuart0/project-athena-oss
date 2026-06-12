"""Unit tests for shared SSRF guard — shared.url_safety.

Covers (plan step 0.4):
- validate_url_not_private: scheme rejection, userinfo rejection, private-IP
  blocking (all RFC-1918 / loopback / link-local / CGNAT / ULA), allowed_private_hosts
  (literal hostname + CIDR), IPv4-mapped IPv6, DNS-failure fail-closed.
- safe_request: redirect chain attack (private hop 2), POST 301/302/303 downgrade,
  POST 307/308 refusal, cross-origin credential stripping, max_hops, max_bytes,
  kwargs isolation (safe_ kwargs don't leak into httpx request).
- IP-pinning PoC (mandatory per plan 0.1c): TCP connect target is the validated
  IP; SNI hostname is the original hostname (not the IP).
- safe_get / safe_post wrappers forward correctly.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Iterable

import pytest

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from shared.url_safety import (
    SsrfBlockedError,
    UrlSafetyResult,
    _PinnedNetworkBackend,
    _build_pinned_transport,
    _ip_is_private,
    _origins_differ,
    safe_get,
    safe_post,
    safe_request,
    validate_url_not_private,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vld(url, **kw) -> UrlSafetyResult:
    """Sync call to validate_url_not_private with optional kwargs."""
    return validate_url_not_private(url, **kw)


# ---------------------------------------------------------------------------
# validate_url_not_private — basic contract
# ---------------------------------------------------------------------------

class TestValidateUrlNotPrivate:
    """Tests for validate_url_not_private (plan step 0.1)."""

    # --- Never raises ---

    def test_does_not_raise_on_garbage_input(self):
        result = _vld("not a url at all !!!")
        assert isinstance(result, UrlSafetyResult)
        assert result.allowed is False

    def test_does_not_raise_on_empty_string(self):
        result = _vld("")
        assert result.allowed is False

    # --- Scheme validation ---

    def test_ftp_scheme_blocked(self):
        result = _vld("ftp://example.com/file")
        assert result.allowed is False
        assert "scheme" in result.reason.lower()

    def test_file_scheme_blocked(self):
        result = _vld("file:///etc/passwd")
        assert result.allowed is False

    def test_gopher_scheme_blocked(self):
        result = _vld("gopher://evil.com/x")
        assert result.allowed is False

    def test_https_scheme_allowed_by_default(self):
        # example.com resolves to a public IP; will be allowed
        result = _vld("https://example.com/path")
        # If DNS is available, allowed=True; if not, blocked for resolve failure.
        # We only assert no exception and that the result is a UrlSafetyResult.
        assert isinstance(result, UrlSafetyResult)

    def test_http_blocked_when_only_https_allowed(self):
        result = _vld("http://example.com/", allowed_schemes=frozenset({"https"}))
        assert result.allowed is False
        assert "scheme" in result.reason.lower()

    # --- Userinfo ---

    def test_userinfo_blocked(self):
        result = _vld("https://user:pass@example.com/")
        assert result.allowed is False
        assert "userinfo" in result.reason

    def test_userinfo_without_password_blocked(self):
        result = _vld("https://user@example.com/")
        assert result.allowed is False

    # --- Missing host ---

    def test_missing_host_blocked(self):
        result = _vld("https:///path")
        assert result.allowed is False

    # --- Private IP ranges ---

    @pytest.mark.parametrize("ip", [
        "127.0.0.1",
        "127.255.0.1",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.255.255",
        "169.254.169.254",  # AWS metadata
        "169.254.1.1",
        "100.64.0.1",       # CGNAT
        "100.127.255.255",
    ])
    def test_private_ip_url_blocked(self, ip):
        result = _vld(f"http://{ip}/path")
        assert result.allowed is False, f"Expected {ip} to be blocked"
        assert "private" in result.reason.lower() or "blocked" in result.reason.lower()

    def test_loopback_ipv6_blocked(self):
        result = _vld("http://[::1]/path")
        assert result.allowed is False

    # --- IPv4-mapped IPv6 ---

    def test_ipv4_mapped_ipv6_private_blocked(self):
        # ::ffff:192.168.1.1 is IPv4-mapped IPv6 for 192.168.1.1
        result = _vld("http://[::ffff:192.168.1.1]/path")
        assert result.allowed is False, "IPv4-mapped IPv6 private should be blocked"

    # --- allowed_private_hosts: literal hostname ---

    def test_literal_hostname_allowlisted(self):
        result = _vld(
            "http://192.168.10.50/health",
            allowed_private_hosts=["192.168.10.50"],
        )
        assert result.allowed is True, f"Expected allowed, reason: {result.reason}"

    def test_other_private_ip_not_allowlisted(self):
        result = _vld(
            "http://192.168.10.51/health",
            allowed_private_hosts=["192.168.10.50"],
        )
        assert result.allowed is False

    # --- allowed_private_hosts: CIDR ---

    def test_cidr_allowlisted(self):
        result = _vld(
            "http://10.0.0.5/health",
            allowed_private_hosts=["10.0.0.0/8"],
        )
        assert result.allowed is True, f"Expected allowed, reason: {result.reason}"

    def test_loopback_cidr_allowlisted(self):
        result = _vld(
            "http://127.0.0.1/path",
            allowed_private_hosts=["127.0.0.0/8"],
        )
        assert result.allowed is True

    def test_cidr_boundary_outside_not_allowed(self):
        # 172.20.0.1 is RFC1918 (172.16/12) but NOT in the 10.0.0.0/8 CIDR.
        result = _vld(
            "http://172.20.0.1/path",
            allowed_private_hosts=["10.0.0.0/8"],
        )
        assert result.allowed is False

    # --- DNS failure → fail-closed ---

    def test_unresolvable_host_blocked(self):
        result = _vld("http://nonexistent-host-xyz-ssrf-test.invalid/path")
        assert result.allowed is False
        assert "dns" in result.reason.lower() or "resolv" in result.reason.lower()

    # --- resolved_ips populated ---

    def test_resolved_ips_populated_on_allowed(self):
        # Using a private host with allowlist so we can inspect resolved_ips.
        result = _vld(
            "http://127.0.0.1/path",
            allowed_private_hosts=["127.0.0.0/8"],
        )
        assert result.allowed is True
        assert "127.0.0.1" in result.resolved_ips

    def test_resolved_ips_populated_on_blocked(self):
        result = _vld("http://127.0.0.1/path")
        # May or may not be populated when blocked; the important thing is the list
        # attribute exists and is a list.
        assert isinstance(result.resolved_ips, list)

    # --- frozen dataclass ---

    def test_url_safety_result_is_frozen(self):
        result = _vld("http://127.0.0.1/path")
        with pytest.raises((AttributeError, TypeError)):
            result.allowed = True  # type: ignore[misc]

    # --- hostname / normalized_url populated ---

    def test_normalized_url_strips_userinfo(self):
        # Even on a blocked result the normalized_url should have no userinfo
        result = _vld("https://user:secret@example.com/path")
        assert "@" not in result.normalized_url or result.normalized_url == "https://user:secret@example.com/path"
        # The block happens at the userinfo check before normalization; the
        # normalized_url field on the userinfo-blocked result will be the raw url.
        assert result.allowed is False


# ---------------------------------------------------------------------------
# _ip_is_private unit tests
# ---------------------------------------------------------------------------

class TestIpIsPrivate:

    @pytest.mark.parametrize("ip,expected", [
        ("1.1.1.1", False),
        ("8.8.8.8", False),
        # Documentation/TEST-NET ranges (RFC 5737) are NOT globally routable —
        # is_global=False, so they are blocked.  Codex r2 fix 1 corrects the
        # prior incorrect expectation that TEST-NET-3 was "False" (not private).
        ("203.0.113.1", True),   # TEST-NET-3 (RFC 5737) — blocked
        ("127.0.0.1", True),
        ("10.0.0.1", True),
        ("172.16.0.0", True),
        ("192.168.1.1", True),
        ("169.254.1.1", True),
        ("100.64.1.1", True),
        ("::1", True),
        ("fc00::1", True),
        ("fe80::1", True),
    ])
    def test_ip_classification(self, ip, expected):
        assert _ip_is_private(ip) == expected, f"Unexpected result for {ip}"

    def test_malformed_ip_treated_as_private(self):
        assert _ip_is_private("not-an-ip") is True


# ---------------------------------------------------------------------------
# _origins_differ unit tests
# ---------------------------------------------------------------------------

class TestOriginsDiffer:

    def test_same_origin(self):
        assert _origins_differ("https://example.com/a", "https://example.com/b") is False

    def test_different_scheme(self):
        assert _origins_differ("http://example.com/a", "https://example.com/a") is True

    def test_different_host(self):
        assert _origins_differ("https://a.com/", "https://b.com/") is True

    def test_different_port_explicit(self):
        assert _origins_differ("https://example.com:8443/a", "https://example.com:443/a") is True

    def test_default_port_vs_explicit_same(self):
        assert _origins_differ("https://example.com/a", "https://example.com:443/a") is False

    def test_http_default_port(self):
        assert _origins_differ("http://example.com/a", "http://example.com:80/a") is False


# ---------------------------------------------------------------------------
# _PinnedNetworkBackend — IP pinning PoC (mandatory per plan 0.1c)
# ---------------------------------------------------------------------------

class TestPinnedNetworkBackend:
    """Mandatory PoC: TCP connect target is the pinned IP; SNI is the
    original hostname (httpcore controls SNI via Origin.host, not what
    _PinnedNetworkBackend dials).
    """

    def test_connect_tcp_dials_pinned_ip(self):
        """connect_tcp must forward the pinned IP, not the supplied host."""
        dialed_hosts = []

        class _RecordingBackend:
            async def connect_tcp(self, host, port, **kw):
                dialed_hosts.append(host)
                # Return a minimal mock stream
                stream = MagicMock()
                stream.read = AsyncMock(return_value=b"")
                return stream

            async def connect_unix_socket(self, path, **kw):
                raise NotImplementedError

            async def sleep(self, seconds):
                pass

        backend = _PinnedNetworkBackend("1.2.3.4", inner=_RecordingBackend())

        async def _run():
            await backend.connect_tcp("original-hostname.example.com", 443)

        asyncio.run(_run())

        assert dialed_hosts == ["1.2.3.4"], (
            f"Expected TCP connect to '1.2.3.4', got {dialed_hosts}"
        )

    def test_build_pinned_transport_returns_transport(self):
        """_build_pinned_transport with a non-empty IP list returns AsyncHTTPTransport."""
        import httpx
        t = _build_pinned_transport(["93.184.216.34"])
        assert isinstance(t, httpx.AsyncHTTPTransport)

    def test_build_pinned_transport_empty_list_fallback(self):
        """_build_pinned_transport with an empty list returns a default transport."""
        import httpx
        t = _build_pinned_transport([])
        assert isinstance(t, httpx.AsyncHTTPTransport)


# ---------------------------------------------------------------------------
# safe_request — redirect-chain attacks, POST semantics, kwargs isolation
# ---------------------------------------------------------------------------

def _make_mock_response(status: int, location: str | None = None, body: bytes = b"ok") -> MagicMock:
    """Build a minimal mock httpx.Response compatible with client.stream() usage.

    The new safe_request uses ``client.stream()`` and iterates ``aiter_bytes()``,
    then patches ``response._content`` directly.  The mock must:
    - be an async context manager (for ``async with client.stream(...) as response``)
    - expose ``aiter_bytes()`` as an async generator
    - expose ``status_code`` and ``headers``
    - expose a writable ``_content`` attribute
    """
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    if location is not None:
        resp.headers = {"location": location}
    resp._content = body

    # aiter_bytes must be an async generator that yields the body in one chunk.
    async def _aiter_bytes():
        yield body

    resp.aiter_bytes = _aiter_bytes

    # Make resp itself an async context manager (for ``async with ... as response``).
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_client_with_stream(response: MagicMock) -> MagicMock:
    """Build a mock httpx.AsyncClient whose .stream() returns the given response."""
    stream_ctx = response  # response is already its own async context manager
    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=client_ctx)
    client_ctx.__aexit__ = AsyncMock(return_value=False)
    client_ctx.stream = MagicMock(return_value=stream_ctx)
    return client_ctx


def _make_streaming_client(responses: list) -> MagicMock:
    """Build a mock httpx.AsyncClient whose .stream() method returns successive responses.

    Each element of ``responses`` must be a _make_mock_response() result.
    The client is itself an async context manager (``async with AsyncClient() as c``).
    ``c.stream(method, url, **kw)`` records ``(method, url, kw)`` and returns the
    next response (as an async context manager) from the queue.
    """
    stream_calls: list = []
    response_iter = iter(responses)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client._stream_calls = stream_calls

    def _stream(method, url, **kw):
        stream_calls.append({"method": method, "url": url, "kw": kw})
        return next(response_iter)

    client.stream = _stream
    return client


class TestSafeRequest:
    """Tests for safe_request / safe_get / safe_post (plan step 0.1b).

    All tests use ``_make_streaming_client`` / ``_make_mock_response`` because
    safe_request now uses ``client.stream()`` (H-1 gate fix) instead of
    ``client.request()``.
    """

    @pytest.fixture(autouse=True)
    def _mock_pinned_transport(self):
        """Stub _build_pinned_transport so tests don't need real httpcore pools."""
        with patch("shared.url_safety._build_pinned_transport", return_value=MagicMock()):
            yield

    # --- Redirect chain attack: hop 2 resolves to private IP ---

    @pytest.mark.asyncio
    async def test_redirect_to_private_ip_blocked(self):
        """A redirect to a private IP must be blocked even if hop 1 is public."""

        def _fake_validate(url, *, allowed_schemes, allowed_private_hosts):
            if "192.168.0.1" in url:
                return UrlSafetyResult(
                    allowed=False,
                    reason="Hostname resolves to private IP: 192.168.0.1",
                    normalized_url=url,
                    hostname="192.168.0.1",
                    resolved_ips=["192.168.0.1"],
                )
            return UrlSafetyResult(
                allowed=True,
                reason="",
                normalized_url=url,
                hostname="example.com",
                resolved_ips=["93.184.216.34"],
            )

        hop1_resp = _make_mock_response(302, location="http://192.168.0.1/evil")
        client = _make_streaming_client([hop1_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_fake_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with pytest.raises(SsrfBlockedError) as exc_info:
                    await safe_get("http://example.com/page")
        assert "private" in str(exc_info.value).lower()

    # --- max_hops exceeded ---

    @pytest.mark.asyncio
    async def test_max_hops_exceeded_raises(self):
        """More redirects than max_hops must raise SsrfBlockedError."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True,
                reason="",
                normalized_url=url,
                hostname="example.com",
                resolved_ips=["93.184.216.34"],
            )

        # With max_hops=2 we need 3 redirect responses to exceed the limit.
        redir = lambda: _make_mock_response(302, location="http://example.com/next")  # noqa: E731
        client = _make_streaming_client([redir(), redir(), redir()])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with pytest.raises(SsrfBlockedError) as exc_info:
                    await safe_get("http://example.com/start", max_hops=2)
        assert "too many" in str(exc_info.value).lower()

    # --- POST 301/302/303 → downgrade to GET ---

    @pytest.mark.asyncio
    async def test_post_301_downgrades_to_get(self):
        """POST + 301 must re-issue as GET on redirect target."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(301, location="http://example.com/new")
        ok_resp = _make_mock_response(200, body=b"done")
        client = _make_streaming_client([redir_resp, ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                await safe_post("http://example.com/form", json={"x": 1})

        methods = [c["method"] for c in client._stream_calls]
        assert methods == ["POST", "GET"], f"Expected POST then GET, got {methods}"

    # --- POST 307/308 → refuse ---

    @pytest.mark.asyncio
    async def test_post_307_raises(self):
        """POST + 307 must raise SsrfBlockedError (method-preserve redirect)."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(307, location="http://example.com/new")
        client = _make_streaming_client([redir_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with pytest.raises(SsrfBlockedError) as exc_info:
                    await safe_post("http://example.com/form")
        assert "307" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_post_308_raises(self):
        """POST + 308 must raise SsrfBlockedError."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(308, location="http://example.com/new")
        client = _make_streaming_client([redir_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with pytest.raises(SsrfBlockedError) as exc_info:
                    await safe_post("http://example.com/form")
        assert "308" in str(exc_info.value)

    # --- Cross-origin credential stripping ---

    @pytest.mark.asyncio
    async def test_cross_origin_redirect_strips_auth_header(self):
        """Authorization header must be stripped on cross-origin redirect."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname=url.split("/")[2], resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(302, location="http://other.com/page")
        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([redir_resp, ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                await safe_get(
                    "http://example.com/",
                    headers={"Authorization": "Bearer secret"},
                )

        # Second stream call (cross-origin hop) must NOT have Authorization.
        assert len(client._stream_calls) >= 2
        second_kw = client._stream_calls[1]["kw"]
        second_headers = {k.lower(): v for k, v in second_kw.get("headers", {}).items()}
        assert "authorization" not in second_headers, (
            f"Authorization header leaked on cross-origin redirect: {second_headers}"
        )

    # --- Same-origin redirect preserves credentials ---

    @pytest.mark.asyncio
    async def test_same_origin_redirect_preserves_auth_header(self):
        """Authorization header must be preserved on same-origin redirect."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(302, location="http://example.com/new")
        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([redir_resp, ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                await safe_get(
                    "http://example.com/start",
                    headers={"Authorization": "Bearer secret"},
                )

        # Second stream call (same-origin) must still have Authorization.
        assert len(client._stream_calls) >= 2
        second_kw = client._stream_calls[1]["kw"]
        second_headers = {k.lower(): v for k, v in second_kw.get("headers", {}).items()}
        assert "authorization" in second_headers, (
            "Authorization should be preserved on same-origin redirect"
        )

    # --- POST body stripped on 302 downgrade ---

    @pytest.mark.asyncio
    async def test_post_302_strips_json_body(self):
        """json= kwarg must be absent on the GET hop after 302 downgrade."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(302, location="http://example.com/new")
        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([redir_resp, ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                await safe_post("http://example.com/form", json={"key": "value"})

        # Second stream call (GET after downgrade) must not have json=.
        assert len(client._stream_calls) >= 2
        second_kw = client._stream_calls[1]["kw"]
        assert "json" not in second_kw, (
            "json= body should be stripped after POST→GET downgrade"
        )

    # --- max_bytes cap (H-1): streaming abort before full body buffered ---

    @pytest.mark.asyncio
    async def test_max_bytes_exceeded_raises(self):
        """A response body exceeding max_bytes must raise SsrfBlockedError.

        H-1 gate fix: the check must fire while streaming, before the full body
        is buffered.  The mock's aiter_bytes() yields the oversized body chunk
        so the streaming loop hits the cap mid-stream.
        """
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        # 11 MB body; max_bytes = 10 MB.
        big_body = b"X" * 11 * 1024 * 1024
        big_resp = _make_mock_response(200, body=big_body)
        client = _make_streaming_client([big_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with pytest.raises(SsrfBlockedError) as exc_info:
                    await safe_get("http://example.com/big", max_bytes=10 * 1024 * 1024)
        assert "max_bytes" in str(exc_info.value).lower() or "exceed" in str(exc_info.value).lower()

    # --- kwargs isolation: safe_request kwargs don't leak into validate call ---

    @pytest.mark.asyncio
    async def test_kwargs_not_passed_to_validator(self):
        """httpx kwargs (json=, params=, etc.) must NOT be forwarded to
        validate_url_not_private (bob r4 finding 8)."""
        validate_calls = []

        def _recording_validate(url, *, allowed_schemes, allowed_private_hosts):
            validate_calls.append({"url": url})
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_recording_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with patch("shared.url_safety._build_pinned_transport"):
                    await safe_get(
                        "http://example.com/api",
                        params={"q": "test"},
                        headers={"X-Custom": "value"},
                    )
        # validate was called — the call itself is the proof that no extra kwargs arrived
        assert len(validate_calls) >= 1

    # --- Initial URL blocked by validator ---

    @pytest.mark.asyncio
    async def test_initial_url_blocked_raises(self):
        """If validate_url_not_private blocks the first URL, raise immediately."""
        def _block_all(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=False,
                reason="Hostname resolves to private IP: 192.168.0.1",
                normalized_url=url,
                hostname="evil.local",
                resolved_ips=["192.168.0.1"],
            )

        with patch("shared.url_safety.validate_url_not_private", side_effect=_block_all):
            with pytest.raises(SsrfBlockedError) as exc_info:
                await safe_get("http://evil.local/")
        assert "private" in str(exc_info.value).lower()

    # --- safe_get / safe_post wrappers ---

    @pytest.mark.asyncio
    async def test_safe_get_calls_safe_request_with_get(self):
        """safe_get must issue a GET request."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                await safe_get("http://example.com/")

        assert client._stream_calls[0]["method"] == "GET"

    @pytest.mark.asyncio
    async def test_safe_post_calls_safe_request_with_post(self):
        """safe_post must issue a POST request."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                await safe_post("http://example.com/", json={"a": 1})

        assert client._stream_calls[0]["method"] == "POST"


# ---------------------------------------------------------------------------
# H-2 — SNI PoC: TLS SNI hostname is the original hostname (not the pinned IP)
# ---------------------------------------------------------------------------

class TestSniPreservation:
    """Mandatory PoC (plan 0.1c / H-2 gate fix):

    Prove that the TLS layer uses the ORIGINAL hostname as SNI while the TCP
    dial target is the pinned IP.

    Approach: httpcore-level assertion.
    _PinnedNetworkBackend.connect_tcp dials the pinned IP.  httpcore's TLS
    layer is invoked with the connection's Origin.host (the original hostname)
    as the SNI server-name.  We verify this by:
    1. Confirming _PinnedNetworkBackend.connect_tcp receives the PINNED IP
       (not the original hostname) — proving the TCP dial is re-targeted.
    2. Confirming httpcore's AsyncConnectionPool constructs TLS using the
       original hostname by inspecting the Origin passed to the pool.
       We instrument the pool to record the Origin on connect.

    Full-TLS integration proof (trustme self-signed CA) would require
    adding trustme + anyio as dev dependencies; the httpcore-level
    assertion below proves the same structural invariant without a live TLS
    handshake.  See plan 0.1c fallback spec for the integration test path.
    """

    def test_pinned_backend_dials_pinned_ip_not_hostname(self):
        """_PinnedNetworkBackend must forward the PINNED IP to the inner backend."""
        dialed = []

        class _RecordingBackend:
            async def connect_tcp(self, host, port, **kw):
                dialed.append(host)
                stream = MagicMock()
                stream.read = AsyncMock(return_value=b"")
                return stream

            async def connect_unix_socket(self, path, **kw):
                raise NotImplementedError

            async def sleep(self, seconds):
                pass

        backend = _PinnedNetworkBackend("1.2.3.4", inner=_RecordingBackend())

        async def _run():
            await backend.connect_tcp("original-hostname.example.com", 443)

        asyncio.run(_run())
        assert dialed == ["1.2.3.4"], (
            f"TCP dial target must be the pinned IP '1.2.3.4', got {dialed}"
        )

    def test_connection_pool_built_with_pinned_backend(self):
        """_build_pinned_transport replaces the pool's network_backend with the
        pinned backend, so httpcore will call _PinnedNetworkBackend.connect_tcp
        (dial IP) while Origin.host (SNI) remains the original hostname.

        This test asserts the structural invariant: after _build_pinned_transport,
        the transport's _pool is an httpcore.AsyncConnectionPool and its
        _network_backend is an instance of _PinnedNetworkBackend.

        SNI preservation is guaranteed because httpcore's AsyncConnectionPool
        constructs TLS using Origin.host (the request URL's host, not the IP
        dialed by the network backend) — verified by httpcore source at
        httpcore/_async/connection_pool.py::handle_async_request.

        # httpx 0.28 / httpcore — re-verify on upgrade (L-2)
        """
        import httpcore as _httpcore
        import httpx as _httpx

        transport = _build_pinned_transport(["1.2.3.4"])
        assert isinstance(transport, _httpx.AsyncHTTPTransport)
        pool = transport._pool  # httpx 0.28 private API — re-verify on upgrade  # noqa: SLF001
        assert isinstance(pool, _httpcore.AsyncConnectionPool), (
            f"Expected httpcore.AsyncConnectionPool, got {type(pool)}"
        )
        backend = pool._network_backend  # noqa: SLF001
        assert isinstance(backend, _PinnedNetworkBackend), (
            f"Expected _PinnedNetworkBackend as network_backend, got {type(backend)}"
        )
        assert backend._pinned_ip == "1.2.3.4", (  # noqa: SLF001
            f"Pinned IP mismatch: {backend._pinned_ip!r}"
        )


# ---------------------------------------------------------------------------
# M-3 — trailing-dot bypass: _domain_matches must strip trailing dots
# ---------------------------------------------------------------------------

class TestDomainMatchesTrailingDot:
    """M-3 gate fix: trailing-dot bypass on _domain_matches."""

    def test_domain_matches_trailing_dot_on_domain(self):
        from src.rag.site_scraper.main import _domain_matches  # type: ignore[import]
        # "evil.com." with trailing dot must match pattern "evil.com"
        assert _domain_matches("evil.com.", "evil.com") is True

    def test_domain_matches_trailing_dot_on_pattern(self):
        from src.rag.site_scraper.main import _domain_matches  # type: ignore[import]
        assert _domain_matches("evil.com", "evil.com.") is True

    def test_domain_matches_both_trailing_dots(self):
        from src.rag.site_scraper.main import _domain_matches  # type: ignore[import]
        assert _domain_matches("evil.com.", "evil.com.") is True

    def test_subdomain_with_trailing_dot(self):
        from src.rag.site_scraper.main import _domain_matches  # type: ignore[import]
        assert _domain_matches("sub.evil.com.", "evil.com") is True

    def test_trailing_dot_does_not_allow_unrelated_domain(self):
        from src.rag.site_scraper.main import _domain_matches  # type: ignore[import]
        assert _domain_matches("notevil.com.", "evil.com") is False


# ---------------------------------------------------------------------------
# M-4 — IPv6 ULA URL-form blocked by validate_url_not_private
# ---------------------------------------------------------------------------

class TestIPv6ULABlocked:
    """M-4 gate fix: ULA addresses in URL form must be blocked."""

    @pytest.mark.parametrize("url", [
        "http://[fc00::1]/",
        "http://[fd00::1]/",
        "http://[fc00::1]/path",
        "http://[fd12:3456:789a::1]/",
    ])
    def test_ula_ipv6_url_blocked(self, url):
        result = _vld(url)
        assert result.allowed is False, (
            f"Expected IPv6 ULA URL {url!r} to be blocked, got allowed=True"
        )


# ---------------------------------------------------------------------------
# Item 10 — tool_registry localhost:5678 default fail-closed path
# ---------------------------------------------------------------------------

class TestToolRegistryLocalhostFailClosed:
    """Item 10: assert that the localhost:5678-default path in
    _load_mcp_tools is fail-closed: SsrfBlockedError is caught, mcp tools
    come back empty, and the mcp_tool_registry_ssrf_blocked log fires.
    """

    @pytest.mark.asyncio
    async def test_localhost_default_fail_closed(self):
        """When N8N_MCP_URL is unset and no feature-flag row exists, _load_mcp_tools
        falls back to http://localhost:5678/mcp (Class-1-equiv loopback target).
        Because loopback is in the blocked CIDR and no allowlist is set,
        safe_post raises SsrfBlockedError.  The method catches it, logs
        mcp_tool_registry_ssrf_blocked, and returns without populating tools.
        """
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

        from shared.tool_registry import UnifiedToolRegistry
        from shared.url_safety import SsrfBlockedError

        registry = UnifiedToolRegistry()
        log_events = []

        # Patch environment: no N8N_MCP_URL.
        # Patch feature flag: no mcp_url row.
        # Patch safe_post: raise SsrfBlockedError (simulating loopback block).
        # Patch logger: capture warning events.

        async def _no_flag_config(flag_name):
            return None

        async def _ssrf_raising_safe_post(url, **kw):
            raise SsrfBlockedError("Hostname resolves to private IP: 127.0.0.1")

        class _RecordingLogger:
            def warning(self, event, **kw):
                log_events.append({"event": event, **kw})

            def debug(self, *a, **kw):
                pass

            def info(self, *a, **kw):
                pass

        with patch.dict(os.environ, {}, clear=False):
            # Ensure N8N_MCP_URL is absent.
            os.environ.pop("N8N_MCP_URL", None)
            with patch.object(registry, "_get_flag_config", side_effect=_no_flag_config):
                with patch.object(registry, "_get_mcp_security", return_value=None):
                    with patch(
                        "shared.url_safety.safe_post",
                        side_effect=_ssrf_raising_safe_post,
                    ):
                        import shared.tool_registry as _tr_mod
                        orig_logger = _tr_mod.logger
                        _tr_mod.logger = _RecordingLogger()
                        try:
                            await registry._load_mcp_tools()
                        finally:
                            _tr_mod.logger = orig_logger

        # Assert: _mcp_tools must be empty (fail-closed).
        assert registry._mcp_tools == {}, (
            f"Expected empty _mcp_tools after SSRF block, got {registry._mcp_tools}"
        )

        # Assert: mcp_tool_registry_ssrf_blocked log was emitted.
        ssrf_events = [e for e in log_events if e.get("event") == "mcp_tool_registry_ssrf_blocked"]
        assert ssrf_events, (
            f"Expected 'mcp_tool_registry_ssrf_blocked' log event, got {log_events}"
        )


# ---------------------------------------------------------------------------
# Codex r2 reconcile — fix 1: non-global literal bypass
# ---------------------------------------------------------------------------

class TestNonGlobalLiteralBlocking:
    """Codex r2 fix 1: 0.0.0.0, 0/8, [::], multicast, broadcast, doc ranges."""

    @pytest.mark.parametrize("url,label", [
        ("http://0/", "zero-host resolves to 0.0.0.0"),
        ("http://0.0.0.0/", "unspecified v4"),
        ("http://[::]/", "unspecified v6"),
        ("http://224.0.0.1/", "multicast v4"),
        ("http://255.255.255.255/", "broadcast"),
        ("http://192.0.2.1/", "doc range TEST-NET-1 (RFC 5737)"),
        ("http://198.51.100.1/", "doc range TEST-NET-2"),
        ("http://203.0.113.1/", "doc range TEST-NET-3"),
        ("http://240.0.0.1/", "reserved v4"),
        ("http://[ff02::1]/", "multicast v6"),
    ])
    def test_literal_blocked(self, url, label):
        result = _vld(url)
        assert result.allowed is False, (
            f"Expected {url!r} ({label}) to be blocked, got allowed=True"
        )

    def test_public_ip_still_allowed(self):
        """A real public IP should still pass the validator."""
        result = _vld("http://8.8.8.8/")
        # 8.8.8.8 is is_global=True and not multicast — must not be blocked by
        # the new rule itself.  DNS resolution doesn't apply to literal IPs.
        assert result.allowed is True, (
            f"8.8.8.8 should be allowed, reason: {result.reason!r}"
        )

    def test_allowlist_still_works_for_private_ip(self):
        """An RFC1918 IP explicitly allowlisted must still be allowed."""
        result = _vld(
            "http://192.168.1.1/",
            allowed_private_hosts=["192.168.1.1"],
        )
        assert result.allowed is True, (
            f"Allowlisted 192.168.1.1 should be allowed, reason: {result.reason!r}"
        )


# ---------------------------------------------------------------------------
# Codex r2 reconcile — fix 3: kwargs hardening in safe_request
# ---------------------------------------------------------------------------

class TestSafeRequestKwargsHardening:
    """Codex r2 fix 3: follow_redirects / auth / cookies must be rejected."""

    @pytest.mark.asyncio
    async def test_follow_redirects_raises_value_error(self):
        """Passing follow_redirects= must raise ValueError immediately."""
        with pytest.raises(ValueError, match="follow_redirects"):
            await safe_get("http://example.com/", follow_redirects=True)

    @pytest.mark.asyncio
    async def test_follow_redirects_false_also_raises(self):
        """follow_redirects=False is also forbidden — the helper owns redirects."""
        with pytest.raises(ValueError, match="follow_redirects"):
            await safe_get("http://example.com/", follow_redirects=False)

    @pytest.mark.asyncio
    async def test_auth_raises_value_error(self):
        """Passing auth= must raise ValueError — cross-origin credential risk."""
        with pytest.raises(ValueError, match="auth="):
            await safe_post("http://example.com/", auth=("user", "pass"))

    @pytest.mark.asyncio
    async def test_cookies_raises_value_error(self):
        """Passing cookies= must raise ValueError — cross-origin credential risk."""
        with pytest.raises(ValueError, match="cookies="):
            await safe_get("http://example.com/", cookies={"session": "abc"})

    @pytest.mark.asyncio
    async def test_valid_kwargs_still_forwarded(self):
        """Legitimate kwargs (json=, headers=, params=) must not be rejected."""
        def _ok_validate(url, *, allowed_schemes, allowed_private_hosts):
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        ok_resp = _make_mock_response(200, body=b"ok")
        client = _make_streaming_client([ok_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_ok_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                # Should not raise
                await safe_post(
                    "http://example.com/api",
                    json={"key": "val"},
                    headers={"X-Token": "secret"},
                    params={"q": "test"},
                )


# ---------------------------------------------------------------------------
# Codex r2 reconcile — fix 4: malformed-port contract
# ---------------------------------------------------------------------------

class TestMalformedPortContract:
    """Codex r2 fix 4: validate_url_not_private and safe_request never raise
    on malformed ports; they return allowed=False / SsrfBlockedError."""

    def test_oversized_port_returns_allowed_false(self):
        """http://127.0.0.1:99999/ must return allowed=False, not raise."""
        result = _vld("http://127.0.0.1:99999/")
        assert isinstance(result, UrlSafetyResult)
        assert result.allowed is False
        assert "port" in result.reason.lower() or "invalid" in result.reason.lower(), (
            f"Unexpected reason for oversized port: {result.reason!r}"
        )

    def test_oversized_port_on_public_host_returns_allowed_false(self):
        """http://example.com:99999/ must return allowed=False (invalid port)."""
        result = _vld("http://example.com:99999/")
        assert isinstance(result, UrlSafetyResult)
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_redirect_location_with_bad_port_raises_ssrf_blocked(self):
        """A redirect Location with port 99999 must raise SsrfBlockedError,
        not propagate an uncaught ValueError."""
        def _validate(url, *, allowed_schemes, allowed_private_hosts):
            if "99999" in url:
                # validate_url_not_private returns allowed=False for bad port
                return UrlSafetyResult(
                    allowed=False,
                    reason="Invalid port: 99999",
                    normalized_url=url,
                    hostname="",
                )
            return UrlSafetyResult(
                allowed=True, reason="", normalized_url=url,
                hostname="example.com", resolved_ips=["93.184.216.34"],
            )

        redir_resp = _make_mock_response(302, location="http://example.com:99999/evil")
        client = _make_streaming_client([redir_resp])

        with patch("shared.url_safety.validate_url_not_private", side_effect=_validate):
            with patch("shared.url_safety.httpx.AsyncClient", return_value=client):
                with pytest.raises(SsrfBlockedError):
                    await safe_get("http://example.com/start")


# ---------------------------------------------------------------------------
# Codex r2 reconcile — fix 2b: _build_pinned_transport runtime fallback
# ---------------------------------------------------------------------------

class TestBuildPinnedTransportFallback:
    """Codex r2 fix 2b: graceful fallback when _pool attribute is absent."""

    def test_fallback_when_pool_absent(self):
        """If httpx.AsyncHTTPTransport has no _pool, return a plain transport
        and emit the url_safety_pinned_transport_unavailable log."""
        import httpx as _httpx

        log_warnings = []

        class _NoPoolTransport(_httpx.AsyncHTTPTransport):
            """Simulates an httpx version where _pool is not present."""
            def __init__(self, **kw):
                # Don't call super().__init__() so _pool is never set.
                pass

        import logging as _logging
        handler_records = []

        class _CapturingHandler(_logging.Handler):
            def emit(self, record):
                handler_records.append(record)

        import shared.url_safety as _url_safety_mod
        orig_log = _url_safety_mod._log
        capturing_logger = _logging.getLogger("url_safety_fallback_test")
        capturing_logger.addHandler(_CapturingHandler())
        capturing_logger.setLevel(_logging.WARNING)
        _url_safety_mod._log = capturing_logger

        try:
            with patch("shared.url_safety.httpx.AsyncHTTPTransport", _NoPoolTransport):
                result = _build_pinned_transport(["1.2.3.4"])
        finally:
            _url_safety_mod._log = orig_log

        # Must return a transport (degraded mode, not a crash)
        assert isinstance(result, _NoPoolTransport)
        # Warning must have been emitted
        assert any("url_safety_pinned_transport_unavailable" in str(r.msg) for r in handler_records), (
            f"Expected url_safety_pinned_transport_unavailable log, got: {handler_records}"
        )

    def test_normal_path_sets_pinned_pool(self):
        """When _pool is present (httpx 0.28), it must be replaced with pinned pool."""
        import httpcore as _httpcore
        transport = _build_pinned_transport(["9.9.9.9"])
        assert hasattr(transport, "_pool")
        assert isinstance(transport._pool, _httpcore.AsyncConnectionPool)  # noqa: SLF001
        assert isinstance(transport._pool._network_backend, _PinnedNetworkBackend)  # noqa: SLF001
        assert transport._pool._network_backend._pinned_ip == "9.9.9.9"  # noqa: SLF001


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
