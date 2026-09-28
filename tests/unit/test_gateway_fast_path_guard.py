"""ATHENA-69 D17/P5 -- the gateway simple-command fast path is owner-only.

execute_simple_command() bypasses the orchestrator -- and therefore every
server-derived permission check -- to call Home Assistant directly for
turn_on/turn_off. These tests pin the fail-closed contract: the fast path
runs only when the mode service confirms owner mode within the timeout,
and only against a light entity. Any other outcome (guest, non-200,
timeout, connection error, unset MODE_SERVICE_URL) must skip the fast path
with zero HA calls, falling through to the orchestrator (already covered by
the call sites in gateway/main.py, unchanged here).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gateway.mode_gate as mode_gate  # noqa: E402
import gateway.simple_commands as sc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mode_gate(monkeypatch):
    """Every test starts with a cold, un-warned client and no
    MODE_SERVICE_URL -- the module-level lazy singleton and the
    warn-once flag must not leak state across tests."""
    monkeypatch.setattr(mode_gate, "_client", None)
    monkeypatch.setattr(mode_gate, "_warned_mode_service_unset", False)
    monkeypatch.delenv("MODE_SERVICE_URL", raising=False)
    yield


class _FakeModeServiceClient:
    """Stands in for mode_gate's lazy module-level httpx.AsyncClient."""

    def __init__(self, status_code=200, json_body=None, raise_exc=None):
        self.status_code = status_code
        self._json = {} if json_body is None else json_body
        self._raise = raise_exc
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        if self._raise is not None:
            raise self._raise
        resp = mock.MagicMock()
        resp.status_code = self.status_code
        resp.json = mock.MagicMock(return_value=self._json)
        return resp


class _RecordingHAClient:
    """Fake ha_client whose .post() records every call -- used to prove the
    gate short-circuits before any HA request is made."""

    def __init__(self):
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp


class TestFastPathAllowed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code,mode_value,raise_exc,url_set",
        [
            pytest.param(200, "owner", None, True, id="owner-200-true"),
            pytest.param(200, "guest", None, True, id="guest-200-false"),
            pytest.param(401, None, None, True, id="401-false"),
            pytest.param(None, None, httpx.ConnectTimeout("timed out"), True, id="timeout-false"),
            pytest.param(None, None, httpx.ConnectError("refused"), True, id="connect-error-false"),
            pytest.param(None, None, None, False, id="mode-service-url-unset-false"),
        ],
    )
    async def test_fast_path_allowed_only_for_owner(
        self, monkeypatch, status_code, mode_value, raise_exc, url_set
    ):
        expected = status_code == 200 and mode_value == "owner"
        if url_set:
            monkeypatch.setenv("MODE_SERVICE_URL", "http://mode-service:8022")

        fake = _FakeModeServiceClient(
            status_code=status_code or 0,
            json_body={"mode": mode_value} if mode_value else {},
            raise_exc=raise_exc,
        )
        monkeypatch.setattr(mode_gate, "_client", fake)

        assert await mode_gate.fast_path_allowed() is expected

    @pytest.mark.asyncio
    async def test_mode_service_url_unset_never_constructs_a_client(self, monkeypatch):
        """The unset branch must short-circuit before touching httpx at
        all -- no client construction, no network attempt."""
        called = mock.MagicMock()
        monkeypatch.setattr(mode_gate, "_get_client", called)

        assert await mode_gate.fast_path_allowed() is False
        called.assert_not_called()


class TestExecuteSimpleCommandGate:
    @pytest.mark.asyncio
    async def test_execute_simple_command_skips_when_not_allowed(self, monkeypatch):
        monkeypatch.setattr(sc, "fast_path_allowed", AsyncMock(return_value=False))
        client = _RecordingHAClient()

        result = await sc.execute_simple_command(
            "turn_on", {"device": "kitchen"}, client, "http://ha.local:8123", "tok"
        )

        assert result is None
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_execute_simple_command_refuses_non_light_entity(self, monkeypatch):
        monkeypatch.setattr(sc, "fast_path_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(sc, "_resolve_device_to_entity", lambda device: "lock.front_door")
        client = _RecordingHAClient()

        result = await sc.execute_simple_command(
            "turn_on", {"device": "front door"}, client, "http://ha.local:8123", "tok"
        )

        assert result is None
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_execute_simple_command_runs_for_light_when_owner(self, monkeypatch):
        """Positive control: a light entity with the gate open still takes
        the fast path (proves the gate isn't refusing everything)."""
        monkeypatch.setattr(sc, "fast_path_allowed", AsyncMock(return_value=True))
        client = _RecordingHAClient()

        result = await sc.execute_simple_command(
            "turn_on", {"device": "kitchen"}, client, "http://ha.local:8123", "tok"
        )

        assert result == "I've turned on the kitchen."
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_time_and_greeting_unaffected_by_gate(self, monkeypatch):
        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(sc, "fast_path_allowed", gate)

        time_result = await sc.execute_simple_command("time", {}, None, "http://ha.local:8123", "tok")
        greeting_result = await sc.execute_simple_command(
            "greeting", {"type": "greeting_hello"}, None, "http://ha.local:8123", "tok"
        )

        assert time_result is not None
        assert greeting_result is not None
        gate.assert_not_called()


class TestDeviceResolutionIsLightOnly:
    """Drift guard: every current phrasing (mapped or via the fallback
    normalizer) resolves to a light.* entity -- pinning the assumption the
    explicit lights-only check in execute_simple_command defends even
    though today's resolver never emits anything else."""

    @pytest.mark.parametrize(
        "phrase",
        [
            pytest.param("kitchen lights", id="kitchen-lights"),
            pytest.param("living room", id="living-room"),
            pytest.param("bedroom light", id="bedroom-light"),
            pytest.param("office", id="office"),
            pytest.param("hallway lights", id="hallway-lights"),
            pytest.param("all lights", id="all-lights"),
            pytest.param("thermostat", id="thermostat"),
            pytest.param("garage door", id="garage-door"),
        ],
    )
    def test_gateway_simple_command_resolver_is_light_only(self, phrase):
        entity_id = sc._resolve_device_to_entity(phrase)
        assert entity_id is not None
        assert entity_id.startswith("light."), f"{phrase!r} resolved to {entity_id!r}"

    def test_device_mappings_values_all_light(self):
        offenders = {k: v for k, v in sc.DEVICE_MAPPINGS.items() if not v.startswith("light.")}
        assert offenders == {}
