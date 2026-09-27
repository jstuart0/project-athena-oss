"""ATHENA-121 -- execute_simple_command() must propagate the real HA
outcome instead of returning a canned success string regardless of what HA
actually did.

httpx does not raise on a non-2xx response unless raise_for_status() is
called, so the pre-fix code posted to HA's service-call endpoint, ignored
the response entirely, and always returned "I've turned on/off the
<device>." -- even when HA returned 403/500 or the call timed out. These
tests exercise execute_simple_command() directly against a fake ha_client,
independent of the gateway route wiring (covered separately in
test_gateway_nonstream_continuity.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gateway.simple_commands import execute_simple_command  # noqa: E402


class _StatusClient:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        resp = mock.MagicMock()
        resp.status_code = self.status_code
        return resp


class _RaisingClient:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def post(self, url, headers=None, json=None):
        raise self._exc


@pytest.mark.asyncio
async def test_turn_on_2xx_returns_success_text():
    client = _StatusClient(200)
    result = await execute_simple_command(
        "turn_on", {"device": "kitchen"}, client, "http://ha.local:8123", "tok"
    )
    assert result == "I've turned on the kitchen."
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_turn_on_403_returns_none_not_canned_success():
    client = _StatusClient(403)
    result = await execute_simple_command(
        "turn_on", {"device": "office"}, client, "http://ha.local:8123", "tok"
    )
    assert result is None


@pytest.mark.asyncio
async def test_turn_on_500_returns_none_not_canned_success():
    client = _StatusClient(500)
    result = await execute_simple_command(
        "turn_on", {"device": "office"}, client, "http://ha.local:8123", "tok"
    )
    assert result is None


@pytest.mark.asyncio
async def test_turn_on_timeout_returns_none():
    client = _RaisingClient(httpx.ConnectTimeout("timed out"))
    result = await execute_simple_command(
        "turn_on", {"device": "office"}, client, "http://ha.local:8123", "tok"
    )
    assert result is None


@pytest.mark.asyncio
async def test_turn_off_2xx_returns_success_text():
    client = _StatusClient(204)
    result = await execute_simple_command(
        "turn_off", {"device": "office light"}, client, "http://ha.local:8123", "tok"
    )
    assert result == "I've turned off the office light."


@pytest.mark.asyncio
async def test_turn_off_403_returns_none_not_canned_success():
    """Reproduces the exact live report: HA 403s the turn_off call, but the
    pre-fix code still spoke 'I've turned off the office light.'"""
    client = _StatusClient(403)
    result = await execute_simple_command(
        "turn_off", {"device": "office light"}, client, "http://ha.local:8123", "tok"
    )
    assert result is None
    assert result != "I've turned off the office light."


@pytest.mark.asyncio
async def test_turn_off_500_returns_none_not_canned_success():
    client = _StatusClient(500)
    result = await execute_simple_command(
        "turn_off", {"device": "office light"}, client, "http://ha.local:8123", "tok"
    )
    assert result is None


@pytest.mark.asyncio
async def test_turn_off_timeout_returns_none():
    client = _RaisingClient(httpx.ReadTimeout("timed out"))
    result = await execute_simple_command(
        "turn_off", {"device": "office light"}, client, "http://ha.local:8123", "tok"
    )
    assert result is None


@pytest.mark.asyncio
async def test_turn_on_failure_log_never_includes_url_or_token(monkeypatch):
    """The warning log on a failed HA call must not leak the HA token or
    the full request URL (ATHENA-121 constraint)."""
    import gateway.simple_commands as sc

    calls = []
    monkeypatch.setattr(sc.logger, "warning", lambda event, **kw: calls.append((event, kw)))

    client = _StatusClient(403)
    await execute_simple_command(
        "turn_on", {"device": "office"}, client,
        "http://ha.local:8123", "super-secret-token"
    )

    assert calls, "expected a warning log on HA failure"
    event, kwargs = calls[0]
    assert event == "simple_command_ha_call_failed"
    assert kwargs["status_code"] == 403
    rendered = repr((event, kwargs))
    assert "super-secret-token" not in rendered
    assert "http://ha.local:8123" not in rendered
    assert "ha.local" not in rendered
