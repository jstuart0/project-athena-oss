"""ATHENA-89 Phase 7 (DC14 item 1) -- src/orchestrator/music_handler.py's
fallback room -> Music Assistant media_player entity mapping (used only
when the admin API's room_audio_config table is unreachable) is now
configured via HA_MUSIC_PLAYERS instead of a hardcoded
FALLBACK_ROOM_TO_PLAYER dict. Covers both accepted input forms (JSON
object, comma-separated pairs), the empty/unset default, and the
invalid-input error path.
"""
from __future__ import annotations

import asyncio
import sys
from unittest import mock

import pytest

# Stub heavy deps before any orchestrator import -- MusicHandler.__init__
# lazily imports orchestrator.mode_permission (-> orchestrator.metrics ->
# prometheus_client) the first time it's actually constructed, which the
# regression test below at the end of this file does.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import orchestrator.music_handler as music_handler  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(music_handler, "_fallback_room_to_player_cache", None)
    monkeypatch.setattr(music_handler, "_fallback_room_to_player_warned", False)
    monkeypatch.setattr(music_handler, "_room_config_cache", {})
    monkeypatch.setattr(music_handler, "_room_config_cache_time", 0)
    yield


def _set_config(monkeypatch, raw: str):
    monkeypatch.setattr(music_handler, "get_config", lambda: mock.MagicMock(ha_music_players=raw))


def test_empty_unset_yields_empty_dict_and_logs_once(monkeypatch):
    _set_config(monkeypatch, "")
    calls = []
    monkeypatch.setattr(music_handler.logger, "info", lambda event, **kw: calls.append(event))

    first = music_handler._get_fallback_room_to_player()
    second = music_handler._get_fallback_room_to_player()

    assert first == {}
    assert second == {}
    assert calls.count("ha_music_players_unset_no_fallback_configured") == 1


def test_json_object_form():
    raw = '{"Living_Room": "media_player.lr", "office": "media_player.office_group"}'
    result = music_handler._parse_ha_music_players(raw)
    assert result == {"living_room": "media_player.lr", "office": "media_player.office_group"}


def test_comma_list_form():
    raw = "living_room:media_player.lr,office:media_player.office_group"
    result = music_handler._parse_ha_music_players(raw)
    assert result == {"living_room": "media_player.lr", "office": "media_player.office_group"}


def test_comma_list_entry_missing_colon_is_skipped_and_logged():
    calls = []
    with mock.patch.object(music_handler.logger, "error", lambda event, **kw: calls.append({"event": event, **kw})):
        result = music_handler._parse_ha_music_players("not_a_valid_entry")
    assert result == {}
    assert calls and calls[0]["event"] == "ha_music_players_invalid_entry"


def test_invalid_json_yields_empty_dict_and_logs_error():
    calls = []
    with mock.patch.object(music_handler.logger, "error", lambda event, **kw: calls.append({"event": event, **kw})):
        result = music_handler._parse_ha_music_players("{not valid json")
    assert result == {}
    assert calls and calls[0]["event"] == "ha_music_players_invalid_json"


def test_get_room_configs_falls_back_to_empty_dict_when_admin_api_and_ha_music_players_both_unset(monkeypatch):
    _set_config(monkeypatch, "")

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, *a, **kw):
            raise RuntimeError("admin API unreachable")

    with mock.patch("httpx.AsyncClient", _FakeAsyncClient):
        result = asyncio.run(music_handler.get_room_configs())

    assert result == {}
    assert music_handler.get_room_entity("office", result) is None


def test_player_check_succeeds_when_constructed_with_already_guarded_client():
    """Pass H regression: production (main.py's lifespan) passes the
    already-guarded ha_client into get_music_handler(), so MusicHandler's
    own ensure_permission_enforcing() call is a no-op wrapping an already-
    wrapped guard (idempotent). Before Pass H, MusicHandler kept a private
    self._ha_raw = ha_client reference and used raw .url/.headers on it --
    since that "raw" reference was actually the guard, .headers raised
    AttributeError (caught and silently turned into "no players found",
    breaking playback). Now the player check goes through the guard's
    get_states() read passthrough and must succeed."""
    from orchestrator import mode_permission

    inner = mock.MagicMock()
    inner.get_states = mock.AsyncMock(return_value=[
        {"entity_id": "media_player.mass_kitchen", "attributes": {"mass_player_type": "player"}},
    ])
    already_guarded = mode_permission.ensure_permission_enforcing(inner)
    assert already_guarded._athena_ha_guard is mode_permission._GUARD_SENTINEL

    handler = music_handler.MusicHandler(already_guarded)
    music_handler._ma_config_checked = False  # bypass the module-level once-per-session cache

    has_players = asyncio.run(music_handler.check_music_assistant_players(handler.ha))

    assert has_players is True
    inner.get_states.assert_awaited_once()
