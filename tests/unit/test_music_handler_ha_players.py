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
