"""ATHENA-89 Phase 7 (DC14 item 1b) -- src/orchestrator/tv_handler.py's
fallback room -> Apple TV entity mapping (used only when the admin API's
Room TV Config is unreachable) is now configured via HA_TV_ENTITIES
instead of a hardcoded FALLBACK_ROOM_TO_TV dict. Covers both accepted
input forms (JSON array, comma-separated triples), the empty/unset
default, and the invalid-input error path.
"""
from __future__ import annotations

import asyncio
import sys
from unittest import mock

import pytest

sys.path.insert(0, "src")

import orchestrator.tv_handler as tv_handler  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(tv_handler, "_fallback_room_to_tv_cache", None)
    monkeypatch.setattr(tv_handler, "_fallback_room_to_tv_warned", False)
    yield


def _set_config(monkeypatch, raw: str):
    monkeypatch.setattr(tv_handler, "get_config", lambda: mock.MagicMock(ha_tv_entities=raw))


def test_empty_unset_yields_empty_dict_and_logs_once(monkeypatch):
    _set_config(monkeypatch, "")
    calls = []
    monkeypatch.setattr(tv_handler.logger, "info", lambda event, **kw: calls.append(event))

    first = tv_handler._get_fallback_room_to_tv()
    second = tv_handler._get_fallback_room_to_tv()

    assert first == {}
    assert second == {}
    assert calls.count("ha_tv_entities_unset_no_fallback_tv_configured") == 1


def test_json_array_form_parses_room_media_player_remote():
    raw = (
        '[{"room": "living_room", "media_player_entity_id": "media_player.lr_tv", '
        '"remote_entity_id": "remote.lr_tv"}]'
    )
    result = tv_handler._parse_ha_tv_entities(raw)
    assert result == {"living_room": ("media_player.lr_tv", "remote.lr_tv")}


def test_json_array_form_remote_entity_id_optional():
    raw = '[{"room": "Office", "media_player_entity_id": "media_player.office_tv"}]'
    result = tv_handler._parse_ha_tv_entities(raw)
    assert result == {"office": ("media_player.office_tv", "")}


def test_comma_list_form_with_remote():
    raw = "living_room:media_player.lr_tv:remote.lr_tv,office:media_player.office_tv:remote.office_tv"
    result = tv_handler._parse_ha_tv_entities(raw)
    assert result == {
        "living_room": ("media_player.lr_tv", "remote.lr_tv"),
        "office": ("media_player.office_tv", "remote.office_tv"),
    }


def test_comma_list_form_without_remote():
    raw = "living_room:media_player.lr_tv"
    result = tv_handler._parse_ha_tv_entities(raw)
    assert result == {"living_room": ("media_player.lr_tv", "")}


def test_comma_list_entry_missing_media_player_is_skipped_and_logged():
    calls = []
    with mock.patch.object(tv_handler.logger, "error", lambda event, **kw: calls.append({"event": event, **kw})):
        result = tv_handler._parse_ha_tv_entities("just_a_room_name")
    assert result == {}
    assert calls and calls[0]["event"] == "ha_tv_entities_invalid_entry"


def test_invalid_json_yields_empty_dict_and_logs_error():
    calls = []
    with mock.patch.object(tv_handler.logger, "error", lambda event, **kw: calls.append({"event": event, **kw})):
        result = tv_handler._parse_ha_tv_entities("[not valid json")
    assert result == {}
    assert calls and calls[0]["event"] == "ha_tv_entities_invalid_json"


def test_get_tv_configs_falls_back_to_empty_dict_when_admin_api_and_ha_tv_entities_both_unset(monkeypatch):
    """get_tv_configs()'s own except-and-fall-back-to-hardcoded-values
    branch (unchanged by this phase) now falls back to
    _get_fallback_room_to_tv(), which is {} when HA_TV_ENTITIES is unset --
    every call site already treats an empty tv_configs dict as "no TV
    entity configured" (handle_launch etc., unchanged by this phase)."""
    _set_config(monkeypatch, "")
    monkeypatch.setattr(tv_handler, "_tv_config_cache", {})
    monkeypatch.setattr(tv_handler, "_tv_config_cache_time", 0)

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
        result = asyncio.run(tv_handler.get_tv_configs())

    assert result == {}
