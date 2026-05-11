"""Phase 3 CA startup-upsert tests.

Verifies that `_upsert_all_services` and `sync_registry_loop` in
src/control_agent/main.py behave correctly across the six required cases:

  1. Happy path — correct headers + body shape for every PROCESS_SERVICES entry
  2. Idempotency — calling _upsert_all_services twice produces identical POST
     calls (no double-insert side-effects)
  3. admin-backend down (ConnectError) — function logs warning + doesn't crash
  4. Missing SERVICE_API_KEY — sync_registry_loop skips with critical log
  5. Malformed CONTROL_AGENT_URL — _upsert_all_services skips with critical log
  6. Shape validation — POST params match the upsert route's Pydantic contract
     (name, endpoint_url, service_type, cache_ttl, timeout, rate_limit)

ATHENA-1 Phase 3 (Campaign 4).
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup — CA lives in src/control_agent; tests run from admin/backend
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_SRC = os.path.join(_REPO_ROOT, 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ---------------------------------------------------------------------------
# Import the CA module under test
# ---------------------------------------------------------------------------
# We must do this AFTER patching the module-level env reads so that
# PROCESS_SERVICES and constants load cleanly.  The CA does not read env vars
# at import time for the new functions, so a plain import is safe.
import control_agent.main as ca_main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_ADMIN_URL = "https://athena.example.com"
FAKE_SERVICE_KEY = "test-service-key-phase3"
FAKE_CA_URL = "http://192.168.10.108:8099"
FAKE_CA_HOST = "192.168.10.108"

EXPECTED_PARAM_KEYS = {"name", "endpoint_url", "service_type", "cache_ttl", "timeout", "rate_limit"}


def _make_mock_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = '{"action": "created"}'
    return resp


def _build_async_client_mock(response: Any) -> MagicMock:
    """Return a mock that behaves like `async with httpx.AsyncClient() as client:`."""
    client_mock = AsyncMock()
    if isinstance(response, Exception):
        client_mock.post = AsyncMock(side_effect=response)
    else:
        client_mock.post = AsyncMock(return_value=response)

    # Support `async with httpx.AsyncClient(...) as client:`
    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=client_mock)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)
    return ctx_mock, client_mock


# ---------------------------------------------------------------------------
# Test 1: Happy path — correct headers + body shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_all_services_happy_path():
    """Every PROCESS_SERVICES entry is POSTed with X-Service-Key and correct params."""
    ctx_mock, client_mock = _build_async_client_mock(_make_mock_response(200))

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock):
            count_ok, count_skip = await ca_main._upsert_all_services(
                FAKE_ADMIN_URL, FAKE_SERVICE_KEY
            )

    expected_service_count = len(ca_main.PROCESS_SERVICES)
    assert count_ok == expected_service_count
    assert count_skip == 0
    assert client_mock.post.call_count == expected_service_count

    # Verify every call used the correct URL, header, and param keys
    for c in client_mock.post.call_args_list:
        args, kwargs = c
        posted_url = args[0] if args else kwargs.get("url", "")
        assert posted_url == f"{FAKE_ADMIN_URL}/api/service-registry/services"

        headers = kwargs.get("headers", {})
        assert headers.get("X-Service-Key") == FAKE_SERVICE_KEY

        params = kwargs.get("params", {})
        assert EXPECTED_PARAM_KEYS == set(params.keys())
        assert params["endpoint_url"].startswith(f"http://{FAKE_CA_HOST}:")


# ---------------------------------------------------------------------------
# Test 2: Idempotency — calling twice produces identical POST calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_all_services_idempotent():
    """Calling _upsert_all_services twice sends the same params both times."""
    ctx_mock, client_mock = _build_async_client_mock(_make_mock_response(200))

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock):
            first_ok, first_skip = await ca_main._upsert_all_services(
                FAKE_ADMIN_URL, FAKE_SERVICE_KEY
            )

    # Reset mock and run again
    ctx_mock2, client_mock2 = _build_async_client_mock(_make_mock_response(200))

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock2):
            second_ok, second_skip = await ca_main._upsert_all_services(
                FAKE_ADMIN_URL, FAKE_SERVICE_KEY
            )

    assert first_ok == second_ok
    assert first_skip == second_skip
    assert client_mock.post.call_count == client_mock2.post.call_count

    # Params of each corresponding call must be identical
    for c1, c2 in zip(
        client_mock.post.call_args_list,
        client_mock2.post.call_args_list,
    ):
        assert c1.kwargs.get("params") == c2.kwargs.get("params")


# ---------------------------------------------------------------------------
# Test 3: admin-backend down (ConnectError) — no crash, warns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_all_services_connect_error():
    """ConnectError is caught; function returns count_skip == len(PROCESS_SERVICES)."""
    import httpx

    ctx_mock, client_mock = _build_async_client_mock(
        httpx.ConnectError("Connection refused")
    )

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock):
            count_ok, count_skip = await ca_main._upsert_all_services(
                FAKE_ADMIN_URL, FAKE_SERVICE_KEY
            )

    assert count_ok == 0
    assert count_skip == len(ca_main.PROCESS_SERVICES)
    # Must not raise — function returned normally


# ---------------------------------------------------------------------------
# Test 4: Missing SERVICE_API_KEY — sync_registry_loop skips
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_registry_loop_missing_service_key(caplog):
    """sync_registry_loop exits immediately and logs critical when SERVICE_API_KEY is unset."""
    import logging

    env_overrides = {
        "ADMIN_API_URL": FAKE_ADMIN_URL,
        "SERVICE_API_KEY": "",  # explicitly empty
    }

    with patch.dict(os.environ, env_overrides, clear=False):
        with patch("control_agent.main._upsert_all_services") as mock_upsert:
            with caplog.at_level(logging.CRITICAL):
                await ca_main.sync_registry_loop()

    mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Malformed CONTROL_AGENT_URL — _upsert_all_services skips
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_all_services_malformed_ca_url():
    """_upsert_all_services returns (0, total) when CONTROL_AGENT_URL has no hostname."""
    total = len(ca_main.PROCESS_SERVICES)

    # Empty URL
    with patch.dict(os.environ, {"CONTROL_AGENT_URL": ""}):
        count_ok, count_skip = await ca_main._upsert_all_services(
            FAKE_ADMIN_URL, FAKE_SERVICE_KEY
        )
    assert count_ok == 0
    assert count_skip == total

    # URL with no hostname (e.g., just a bare path)
    with patch.dict(os.environ, {"CONTROL_AGENT_URL": "/no-hostname"}):
        count_ok2, count_skip2 = await ca_main._upsert_all_services(
            FAKE_ADMIN_URL, FAKE_SERVICE_KEY
        )
    assert count_ok2 == 0
    assert count_skip2 == total


# ---------------------------------------------------------------------------
# Test 6: Shape validation — POST params match the upsert route's Pydantic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_payload_shape_matches_route_contract():
    """POST params contain exactly the fields the service_registry.py POST route accepts."""
    ctx_mock, client_mock = _build_async_client_mock(_make_mock_response(201))

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock):
            await ca_main._upsert_all_services(FAKE_ADMIN_URL, FAKE_SERVICE_KEY)

    # Spot-check the first call
    first_call = client_mock.post.call_args_list[0]
    params = first_call.kwargs.get("params", {})

    # Fields accepted by service_registry.py::register_service query params:
    # name, endpoint_url, display_name (optional), service_type, cache_ttl, timeout, rate_limit
    assert "name" in params
    assert "endpoint_url" in params
    assert "service_type" in params
    assert "cache_ttl" in params
    assert "timeout" in params
    assert "rate_limit" in params

    # Verify types match what the route's defaults expect
    assert isinstance(params["name"], str)
    assert isinstance(params["endpoint_url"], str)
    assert isinstance(params["service_type"], str)
    assert isinstance(params["cache_ttl"], int)
    assert isinstance(params["timeout"], int)
    assert isinstance(params["rate_limit"], int)

    # service_type must be one of the expected values
    assert params["service_type"] in ("rag", "core")

    # endpoint_url must be an http URL with host and port
    from urllib.parse import urlparse
    parsed = urlparse(params["endpoint_url"])
    assert parsed.scheme == "http"
    assert parsed.hostname == FAKE_CA_HOST
    assert parsed.port is not None


# ---------------------------------------------------------------------------
# Test 7 (ATHENA-49): 429 mid-loop aborts cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_all_services_429_aborts_cleanly():
    """429 mid-loop: stops posting, counts correctly, logs registry_upsert_rate_limited."""
    import structlog.testing

    total = len(ca_main.PROCESS_SERVICES)
    # 429 fires on the 3rd call (index 2); first two are successes.
    N = 2

    def _make_resp(status_code: int, text: str = '{"action": "created"}') -> MagicMock:
        r = MagicMock()
        r.status_code = status_code
        r.text = text
        return r

    side_effects = [_make_resp(200)] * N + [_make_resp(429, '{"detail": "rate limited"}')]

    client_mock = AsyncMock()
    client_mock.post = AsyncMock(side_effect=side_effects)

    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=client_mock)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock):
            with structlog.testing.capture_logs() as captured:
                count_ok, count_skip = await ca_main._upsert_all_services(
                    FAKE_ADMIN_URL, FAKE_SERVICE_KEY
                )

    assert count_ok == N
    assert count_skip == total - N
    assert client_mock.post.call_count == N + 1  # no further POSTs after 429

    # The log line must carry remaining_skipped == (total - N - 1) services after
    # the one that actually got the 429.
    expected_remaining = total - N - 1
    rl_events = [e for e in captured if e.get("event") == "registry_upsert_rate_limited"]
    assert rl_events, "expected 'registry_upsert_rate_limited' log event"
    assert rl_events[0]["log_level"] == "warning"
    assert rl_events[0]["remaining_skipped"] == expected_remaining


# ---------------------------------------------------------------------------
# Test 8 (ATHENA-49): sync_registry_loop iterates again after 429 abort
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_registry_loop_retries_after_429():
    """sync_registry_loop calls _upsert_all_services again on the next iteration."""
    env_overrides = {
        "ADMIN_API_URL": FAKE_ADMIN_URL,
        "SERVICE_API_KEY": FAKE_SERVICE_KEY,
    }

    call_results = [(2, len(ca_main.PROCESS_SERVICES) - 2), asyncio.CancelledError()]

    async def _side_effect(*args, **kwargs):
        result = call_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    with patch.dict(os.environ, env_overrides, clear=False):
        with patch.object(ca_main, "REGISTRY_SYNC_INTERVAL", 0):
            with patch("control_agent.main._upsert_all_services", side_effect=_side_effect) as mock_upsert:
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    with pytest.raises(asyncio.CancelledError):
                        await ca_main.sync_registry_loop()

    assert mock_upsert.call_count >= 2


# ---------------------------------------------------------------------------
# Test 9 (ATHENA-49, optional): 429 on final service → remaining_skipped == 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_429_on_last_service_remaining_skipped_zero():
    """429 on the very last service means remaining_skipped == 0 (off-by-one check)."""
    import structlog.testing

    total = len(ca_main.PROCESS_SERVICES)

    def _make_resp(status_code: int, text: str = '{"action": "created"}') -> MagicMock:
        r = MagicMock()
        r.status_code = status_code
        r.text = text
        return r

    # All succeed except the final one
    side_effects = [_make_resp(200)] * (total - 1) + [_make_resp(429, '{"detail": "rate limited"}')]

    client_mock = AsyncMock()
    client_mock.post = AsyncMock(side_effect=side_effects)

    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=client_mock)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch.dict(os.environ, {"CONTROL_AGENT_URL": FAKE_CA_URL}):
        with patch("control_agent.main.httpx.AsyncClient", return_value=ctx_mock):
            with structlog.testing.capture_logs() as captured:
                count_ok, count_skip = await ca_main._upsert_all_services(
                    FAKE_ADMIN_URL, FAKE_SERVICE_KEY
                )

    assert count_ok == total - 1
    assert count_skip == 1  # only the service that got the 429
    assert client_mock.post.call_count == total  # every service was attempted

    # remaining_skipped must be 0 — no services were skipped beyond the one that got 429
    rl_events = [e for e in captured if e.get("event") == "registry_upsert_rate_limited"]
    assert rl_events, "expected registry_upsert_rate_limited log event"
    assert rl_events[0]["remaining_skipped"] == 0
