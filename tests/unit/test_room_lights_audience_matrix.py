"""What "all the lights" reaches, per audience and per source of the room lights.

Audiences: owner the gateway vouched for, owner it didn't, guest, public and an
off-stay SMS. Sources: the server's setting only, admin only, both. The leaf
sets are exact. Admin data changes only the non-owner sets; the trusted owner
gets every area's lamps whatever is configured.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

# Import the services from src/ whatever else runs in this process (CI sets no PYTHONPATH).
_SRC = str(_Path(__file__).resolve().parents[2] / "src")
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from orchestrator import mode_permission as mp  # noqa: E402

from .test_room_light_area_tier import (  # noqa: F401
    TRUSTED_OWNER_WRITES, UNTRUSTED_OWNER_WRITES, Rig, _isolated, guest,
)
from .test_room_lights_admin_source import use_sources  # noqa: E402

ENV = json.dumps({"study": "light.study_lamp"})
ADMIN = {"galley": ["light.galley_bulb_1", "light.hall_lamp"], "north": ["light.north_lamp"]}
BOTH_ADMIN = ADMIN
SOURCES = {
    "env_only": dict(admin=None, env=ENV),
    "admin_only": dict(admin=ADMIN, env=""),
    "both": dict(admin=BOTH_ADMIN, env=ENV),
}
NON_OWNER_SETS = {
    "env_only": {"light.study_lamp"},
    "admin_only": {"light.galley_bulb_1", "light.hall_lamp", "light.north_lamp"},
    "both": {"light.galley_bulb_1", "light.hall_lamp", "light.north_lamp"},
}


def audiences():
    base = guest(allowed_intents=["control"])
    return {
        "owner_trusted": ({"mode": "owner"}, "owner", True),
        "owner_untrusted": ({"mode": "owner"}, "owner", False),
        "guest": (base, "guest", True),
        "public": ({**base, "audience": "public"}, "guest", True),
        "off_stay_sms": (mp.off_stay_overlay(base, "upcoming"), "guest", True),
    }


def leaf_sets(monkeypatch, audience, source, *, excluded=(), unassigned=False):
    perms, mode, trusted = audiences()[audience]
    rig = Rig(monkeypatch, attic_state="on", unassigned=unassigned)
    use_sources(rig, **SOURCES[source])

    async def go():
        with mp.ha_permission_scope(perms, mode=mode, trusted_voice=trusted):
            return await rig.controller._whole_house_lamp_rooms(list(excluded))

    rooms, why = asyncio.run(go())
    return (None if rooms is None else {light for room in rooms for light in room}), why


@pytest.mark.parametrize("source", SOURCES)
def test_the_trusted_owner_reaches_every_areas_lamps_whatever_is_configured(monkeypatch, source):
    leaves, _ = leaf_sets(monkeypatch, "owner_trusted", source)
    assert leaves == TRUSTED_OWNER_WRITES


@pytest.mark.parametrize("source", SOURCES)
def test_the_untrusted_owner_reaches_the_home_assistant_light_groups_whatever_is_configured(monkeypatch, source):
    leaves, _ = leaf_sets(monkeypatch, "owner_untrusted", source)
    assert leaves == UNTRUSTED_OWNER_WRITES


@pytest.mark.parametrize("audience", ["guest", "public", "off_stay_sms"])
@pytest.mark.parametrize("source", SOURCES)
def test_every_non_owner_reaches_exactly_the_configured_rooms(monkeypatch, audience, source):
    leaves, _ = leaf_sets(monkeypatch, audience, source)
    assert leaves == NON_OWNER_SETS[source]


@pytest.mark.parametrize("audience", ["guest", "public", "off_stay_sms"])
def test_a_non_owner_with_nothing_configured_is_asked_which_room(monkeypatch, audience):
    perms, mode, trusted = audiences()[audience]
    rig = Rig(monkeypatch, attic_state="on")
    use_sources(rig)

    async def go():
        with mp.ha_permission_scope(perms, mode=mode, trusted_voice=trusted):
            return await rig.controller._whole_house_lamp_rooms([])

    assert asyncio.run(go()) == (None, "ask")


@pytest.mark.parametrize("audience", ["guest", "public", "off_stay_sms"])
@pytest.mark.parametrize("source", SOURCES)
def test_unassigned_lights_never_join_a_non_owner(monkeypatch, audience, source):
    leaves, _ = leaf_sets(monkeypatch, audience, source, unassigned=True)
    assert "light.loose_lamp" not in leaves and leaves == NON_OWNER_SETS[source]


def test_unassigned_lights_join_only_the_trusted_owner(monkeypatch):
    assert "light.loose_lamp" in leaf_sets(monkeypatch, "owner_trusted", "admin_only", unassigned=True)[0]
    assert "light.loose_lamp" not in leaf_sets(monkeypatch, "owner_untrusted", "admin_only", unassigned=True)[0]


@pytest.mark.parametrize("audience", ["guest", "public", "off_stay_sms"])
def test_an_admin_room_named_in_an_exclusion_is_subtracted(monkeypatch, audience):
    leaves, _ = leaf_sets(monkeypatch, audience, "admin_only", excluded=["north"])
    assert leaves == {"light.galley_bulb_1", "light.hall_lamp"}


def test_an_admin_room_whose_lights_are_all_excluded_leaves_the_asking_answer(monkeypatch):
    perms, mode, trusted = audiences()["guest"]
    rig = Rig(monkeypatch, attic_state="on")
    use_sources(rig, admin={"north": ["light.north_lamp"]})

    async def go():
        with mp.ha_permission_scope(perms, mode=mode, trusted_voice=trusted):
            return await rig.controller._whole_house_lamp_rooms(["north"])

    assert asyncio.run(go()) == (None, "ask")
