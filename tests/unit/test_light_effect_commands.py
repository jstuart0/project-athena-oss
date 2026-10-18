"""Light effect commands.

Phase 2b: the single-room helper ``_room_light_targets`` returns what the
dispatcher's inline code resolved before the extraction.
"""
from __future__ import annotations

import asyncio

import pytest

from .test_room_light_area_tier import Rig, _isolated, light  # noqa: F401  (autouse fixture; sets sys.path and stubs)
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator.smart_home_controller import RoomLightTargets  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

ROOMS = ("galley", "study", "hall", "attic", "bathroom", "nowhere")
SCOPES = ("all_individual", "individual_lights", "group")
QUERY = "turn on the lights"


async def _inline_targets(controller, room, intent, scope, per_light, service):
    """The single-room tail of the dispatcher as it read before the extraction,
    kept here as the reference the helper is compared against."""
    matches = await controller._resolve_room_lights(room)
    ambiguous = getattr(matches, "ambiguous_rooms", ())
    if ambiguous and not matches:
        resolved = await controller._resolve_ambiguous_room(room, ambiguous, intent, QUERY)
        if isinstance(resolved, str):
            return resolved
        room, matches = resolved
    label = controller._spoken_room(room)
    if not matches:
        if getattr(matches, "missing", 0):
            return f"The {label} lights aren't responding right now."
        return f"I couldn't find any lights in the {label}."
    individual = scope in ("all_individual", "individual_lights")
    ids = []
    if len(matches) > 1:
        for match in matches:
            if individual:
                ids.extend(match.get("members") or [match["entity_id"]])
            else:
                ids.append(match["entity_id"])
    else:
        only = matches[0]
        ids = (only.get("members") or [only["entity_id"]]) if individual else [only["entity_id"]]
    lt = await controller._finalize_light_targets(matches, ids=ids, per_light=per_light)
    if lt.denied_all:
        controller._record_light_denials(lt, service)
        return mp.permission_refusal_message(("light",), mp.current_ha_scope())
    total, down = await controller._light_availability(
        lt.gate_ids, refresh_key=room, missing=getattr(matches, "missing", 0),
    )
    if total and down == total:
        return f"The {label} lights aren't responding right now."
    return (lt.write_ids, lt.gate_ids, room, label, down, await controller._single_light_name(lt.gate_ids))


def _shape(result):
    if isinstance(result, str):
        return result
    return (result.lt.write_ids, result.lt.gate_ids, result.room, result.room_label,
            result.light_down, result.single_name)


@pytest.mark.parametrize("room", ROOMS)
@pytest.mark.parametrize("scope", SCOPES)
def test_room_light_targets_returns_the_same_targets_as_before(monkeypatch, room, scope):
    rig = Rig(monkeypatch)
    intent = light(room, target_scope=scope)
    per_light = scope in ("all_individual", "individual_lights")

    async def both():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(QUERY),
                                    trusted_voice=True):
            before = await _inline_targets(rig.controller, room, intent, scope, per_light, "turn_on")
            after = await rig.controller._room_light_targets(
                room, intent, QUERY, target_scope=scope, per_light=per_light, denial_service="turn_on",
            )
            return before, after

    before, after = asyncio.run(both())
    assert isinstance(after, (RoomLightTargets, str))
    assert _shape(after) == before


def test_room_light_targets_population_covers_a_named_member_and_each_outcome():
    assert len(ROOMS) * len(SCOPES) >= 12
    assert ("galley", "all_individual") in {(r, s) for r in ROOMS for s in SCOPES}


def test_room_light_targets_galley_reaches_the_leaves_and_names_no_single_light(monkeypatch):
    rig = Rig(monkeypatch)

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(QUERY),
                                    trusted_voice=True):
            return await rig.controller._room_light_targets(
                "galley", light("galley"), QUERY,
                target_scope="all_individual", per_light=True, denial_service="turn_on",
            )

    result = asyncio.run(go())
    assert isinstance(result, RoomLightTargets)
    assert len(result.lt.gate_ids) == 3
    assert result.single_name is None
    assert result.room_label == "galley"
