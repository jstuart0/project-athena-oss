"""What the user gets when the area registry is off, slow, stale or refused."""
from __future__ import annotations

import asyncio
import json

import pytest

from ._room_chat_harness import Chat
from .test_room_light_area_tier import _isolated, _state  # noqa: F401

from gateway import speaker_room as sr  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

STUDY = {f"light.study_lamp_{i}" for i in range(3)}
STUDY_DEVICE = "a" * 32


def with_satellite_map(chat, mapping):
    chat.rig.cfg.ha_satellite_room_map = json.dumps(mapping)


def test_with_no_snapshot_and_a_mapped_speaker_room_the_name_tier_writes_that_room_only(monkeypatch):
    chat = Chat(monkeypatch, registry=False)
    with_satellite_map(chat, {"assist_satellite.study_voice": "study"})
    out = chat.turn("the lights please")
    assert set(chat.written) == STUDY, out.answer
    assert not any(not w.startswith("light.study") for w in chat.written), "never the whole house"


def test_with_no_snapshot_and_no_speaker_room_it_asks_which_room_and_writes_nothing(monkeypatch):
    chat = Chat(monkeypatch, registry=False)
    out = chat.turn("the lights please", room="unknown")
    assert out.answer == "Which room?" and chat.written == []


def test_with_no_snapshot_an_unmapped_speaker_room_is_not_believed(monkeypatch):
    chat = Chat(monkeypatch, registry=False)
    out = chat.turn("the lights please", room="study")
    assert out.answer == "Which room?" and chat.written == []


def test_the_owners_whole_house_with_no_registry_goes_through_the_light_groups_without_indicators(monkeypatch):
    chat = Chat(monkeypatch, registry=False, exclude='["(?:^|[._])led_ring(?:_|$)"]')
    states = chat.rig.manager._entities_cache
    states["light.study_led_ring"] = _state("light.study_led_ring")
    states["light.house_group"] = _state(
        "light.house_group", members=[k for k in states if k.startswith("light.") and "group" not in k]
    )
    chat.rig.manager._build_indexes()
    chat.turn("turn on all the lights")
    assert "light.study_led_ring" not in chat.written and "light.study_lamp_0" in chat.written
    assert len(chat.written) == len(set(chat.written))


# -- the gateway's side: the registry never holds up a turn ----------------------

class Fetcher:
    def __init__(self, error=None, hang=None):
        self.calls = 0
        self.error = error
        self.hang = hang

    async def __call__(self):
        self.calls += 1
        if self.hang is not None:
            await self.hang.wait()
        if self.error:
            raise self.error
        raw, _ = __import__("tests.unit._room_chat_harness", fromlist=["x"]).house_raw_and_states()
        return raw


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def resolve(registry, device_id=STUDY_DEVICE):
    return asyncio.run(sr.resolve_speaker_room(device_id=device_id, trusted=True, registry=registry))


def test_a_registry_that_never_answers_does_not_hold_the_first_turn_past_its_wait(monkeypatch):
    monkeypatch.setattr(sr, "COLD_ENSURE_SECONDS", 0.05)

    async def go():
        hang = asyncio.Event()
        registry = hr.HARegistry(Fetcher(hang=hang), monotonic=Clock())
        got = await asyncio.wait_for(
            sr.resolve_speaker_room(device_id=STUDY_DEVICE, trusted=True, registry=registry), timeout=5
        )
        await registry.close()
        return got

    assert asyncio.run(go()) == sr.UNKNOWN_ROOM


def test_a_stale_snapshot_with_a_hanging_refresh_is_still_used_with_one_fetch_in_flight(monkeypatch):
    async def go():
        clock = Clock()
        fetcher = Fetcher()
        registry = hr.HARegistry(fetcher, ttl=300, min_refresh_interval=60, monotonic=clock)
        await registry.ensure(1)
        fetcher.hang = asyncio.Event()
        clock.now += 400
        # the snapshot has no satellite entity for this id, so use a device it knows
        known = next(iter(registry.current()._device_area), None)
        results = [await sr.resolve_speaker_room(device_id=known or STUDY_DEVICE, trusted=True, registry=registry)
                   for _ in range(5)]
        calls = fetcher.calls
        await registry.close()
        return results, calls

    results, calls = asyncio.run(go())
    assert calls == 2, "one refresh, not one per turn"


def test_a_refused_registry_makes_no_fetcher_calls_over_fifty_turns():
    fetcher = Fetcher(error=hr.AuthInvalid())
    registry = hr.HARegistry(fetcher, monotonic=Clock())

    async def go():
        for _ in range(50):
            await sr.resolve_speaker_room(device_id=STUDY_DEVICE, trusted=True, registry=registry)

    asyncio.run(go())
    assert registry.state == hr.STATE_REFUSED and fetcher.calls == 1


def test_a_disabled_registry_is_never_asked():
    registry = hr.HARegistry(None)
    assert resolve(registry) == sr.UNKNOWN_ROOM
