"""Light effect commands.

Phase 2b: the single-room helper ``_room_light_targets`` returns what the
dispatcher's inline code resolved before the extraction. The reference in this
file is a transcription of that tail as it stood at 89084b9, returning its
values where the original went on to the gate and the writes.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import textwrap
import types

import pytest

from .test_room_light_area_tier import Rig, _isolated, _state, light  # noqa: F401  (autouse fixture; sets sys.path and stubs)
import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from shared import household  # noqa: E402
from orchestrator.smart_home_controller import RoomLightTargets  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

QUERY = "turn on the lights"
SCOPES = ("all_individual", "individual_lights", "group")
OWNER = ({"mode": "owner"}, "owner")
GUEST_DENIED = (mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\."]}), "guest")
BATHROOMS = {s["entity_id"]: s for s in [
    _state("light.north_bathroom_lamp"), _state("light.south_bathroom_lamp"),
]}
GONE_LIGHTS = {"study": ("light.gone_one", "light.gone_two")}

# (room, who, rig kwargs, service, outcome kind, records a denial). An ambiguous
# name whose every candidate is denied is refused without a recorded denial.
_ROWS = [
    (room, OWNER, {}, "turn_on", kind, False)
    for room, kind in (("galley", "targets"), ("study", "targets"), ("hall", "targets"),
                       ("attic", "all_down"), ("nowhere", "not_found"))
]
_ROWS += [
    ("bathroom", OWNER, {"registry": False, "states": BATHROOMS}, "turn_on", "ask_which", False),
    ("galley", GUEST_DENIED, {}, "turn_on", "denied_all", True),
    ("galley", GUEST_DENIED, {}, "turn_off", "denied_all", True),
    ("bathroom", GUEST_DENIED, {"registry": False, "states": BATHROOMS}, "turn_on", "denied_all", False),
    ("study", OWNER, {"admin_lights": GONE_LIGHTS}, "turn_on", "all_missing", False),
    ("study", OWNER, {"admin_lights": {"study": ("light.study_lamp", "light.gone_one")}}, "turn_on", "targets", False),
]
KINDS_EXPECTED = {"targets", "all_down", "not_found", "ask_which", "denied_all", "all_missing"}


async def _reference_targets(controller, room, intent, scope, per_light, service):
    """The single-room tail of the dispatcher as it read at 89084b9."""
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


def _kind(shape, room):
    if not isinstance(shape, str):
        return "targets"
    if shape.startswith("Which "):
        return "ask_which"
    if "guest mode" in shape.lower():
        return "denied_all"
    if shape.startswith("I couldn't find"):
        return "not_found"
    if shape == "The study lights aren't responding right now." and room == "study":
        return "all_missing"
    if "aren't responding" in shape:
        return "all_down"
    return f"other:{shape}"


def _run(monkeypatch, room, who, rig_kwargs, scope, service, use_helper):
    """(result shape, denials recorded) from a fresh rig and permission scope."""
    perms, mode = who
    rig_kwargs = dict(rig_kwargs)
    admin_lights = rig_kwargs.pop("admin_lights", None)
    rig = Rig(monkeypatch, **rig_kwargs)
    if admin_lights:
        monkeypatch.setattr(shc, "_room_lights_value", lambda: types.SimpleNamespace(
            mapping=admin_lights, source=household.SOURCE_ADMIN))
    intent = light(room, target_scope=scope)
    per_light = scope in ("all_individual", "individual_lights")

    async def go():
        with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(QUERY), trusted_voice=True):
            if use_helper:
                result = await rig.controller._room_light_targets(
                    room, intent, QUERY, target_scope=scope, per_light=per_light, denial_service=service,
                )
            else:
                result = await _reference_targets(rig.controller, room, intent, scope, per_light, service)
            denials = [(d.domain, d.service, tuple(d.targets), d.reason) for d in mp.current_ha_scope().denials]
            return _shape(result) if use_helper else result, denials

    return asyncio.run(go())


_CASES = [(row, scope) for row in _ROWS for scope in SCOPES]


@pytest.mark.parametrize("row,scope", _CASES, ids=[f"{r[0]}-{r[4]}-{r[3]}-{s}" for r, s in _CASES])
def test_room_light_targets_returns_the_same_targets_as_before(monkeypatch, row, scope):
    room, who, rig_kwargs, service, expected_kind, records = row
    before, before_denials = _run(monkeypatch, room, who, rig_kwargs, scope, service, use_helper=False)
    after, after_denials = _run(monkeypatch, room, who, rig_kwargs, scope, service, use_helper=True)
    assert after == before
    assert after_denials == before_denials
    kind = _kind(after, room)
    assert kind == expected_kind
    if records:
        assert after_denials and {d[1] for d in after_denials} == {service}
    else:
        assert after_denials == []


def test_room_light_targets_population_covers_a_named_member_and_each_outcome():
    """Each row asserts its own outcome kind, so the declared kinds are what ran."""
    assert len(_CASES) >= 12
    assert ("galley", "targets", "turn_on") in {(r[0], r[4], r[3]) for r, _ in _CASES}
    assert any(r[0] == "galley" and s == "all_individual" for r, s in _CASES)
    assert {r[4] for r, _ in _CASES} == KINDS_EXPECTED
    assert {r[3] for r, _ in _CASES if r[4] == "denied_all"} == {"turn_on", "turn_off"}


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


def test_the_dispatcher_no_longer_resolves_or_finalizes_the_single_room_tail():
    from orchestrator.smart_home_controller import SmartHomeController
    calls = {
        getattr(node.func, "attr", "")
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(
            SmartHomeController._dispatch_light_or_room_command))))
        if isinstance(node, ast.Call)
    }
    assert {"_resolve_room_lights", "_resolve_ambiguous_room", "_finalize_light_targets",
            "_light_availability", "_single_light_name"}.isdisjoint(calls)
    assert {"_room_light_targets", "call_service"} <= calls
