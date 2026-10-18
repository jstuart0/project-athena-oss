"""Voice -> policy scripts: the router in ``orchestrator.room_policy`` and the
motion-control handler's use of it.

The fictional house: a study (a policy room: its policy helper carries
``ha_area``, and the three scripts exist), a nursery (motion helpers and no
``ha_area``, so a plain room) and a galley. ``FakePolicyHA`` is the inner
client of the real permission guard; its answers are built from the contract
in docs/ROOM_POLICY.md."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from . import _room_chat_harness as rch
from ._room_chat_harness import Chat

import orchestrator.smart_home_controller as shc  # noqa: E402
import shared.config  # noqa: E402
from orchestrator import ha_entity_manager as hem  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_policy as rp  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared import ha_registry as hr  # noqa: E402
from shared.ha_client import HAResponseShapeError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
NO_END = "until you leave or press the switch"

OWNER = {"mode": "owner"}
GUEST = {"mode": "guest", "allowed_domains": ["input_boolean", "input_number", "light"], "allowed_intents": ["control"],
         "restricted_entities": []}
RESTRICTED = {"mode": "degraded", "restricted_entities": [r".*_leave_lights_on$"], "allowed_domains": [],
              "allowed_intents": [], "restricted_intents": []}

PLAIN_HELPERS = (
    ("input_boolean", "motion_disable"), ("input_boolean", "leave_lights_on"),
    ("input_boolean", "leave_lights_off"), ("input_boolean", "brightness_override"),
    ("input_number", "leave_lights_on_minutes"), ("input_number", "leave_lights_off_minutes"),
    ("input_number", "override_brightness"),
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("HA_POLICY_PROTECTED_EXTRA", raising=False)
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    for reset in (_runtime.reset_for_test, shared.config._clear_cache_for_tests, hem._clear_room_light_exclude_cache):
        reset()
    yield
    for reset in (_runtime.reset_for_test, shared.config._clear_cache_for_tests, hem._clear_room_light_exclude_cache):
        reset()


def _state(entity_id, state="off", **attributes):
    attributes.setdefault("friendly_name", entity_id.split(".")[1].replace("_", " ").title())
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def helper_states(slug):
    return {f"{d}.{slug}_{n}": _state(f"{d}.{slug}_{n}") for d, n in PLAIN_HELPERS}


def policy_states(slug="study", area="area_study", scripts=("room_policy_set", "room_policy_clear", "room_policy_good_night")):
    states = helper_states(slug)
    states[f"input_text.{slug}_policy"] = _state(f"input_text.{slug}_policy", ha_area=area)
    for name in scripts:
        states[f"script.{name}"] = _state(f"script.{name}")
    return states


class FakePolicyHA:
    """The Home Assistant side of the three scripts, from docs/ROOM_POLICY.md."""

    def __init__(self, until="", until_spoken="", stale=False):
        self.until, self.until_spoken, self.stale = until, until_spoken, stale
        self.error = None
        self.calls = []

    async def call_service_response(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        if self.error is not None:
            raise self.error
        if service == "room_policy_good_night":
            return {"area": data["area"], "until": self.until, "until_spoken": self.until_spoken}
        return {"area": data["area"], "policy": data.get("kind", "clear"), "brightness_hold": False, "until": self.until,
                "until_spoken": self.until_spoken, "set_by": data.get("set_by", ""), "stale": self.stale}

    @property
    def fields(self):
        return [data for _, _, data in self.calls]


def refusal(status, message):
    request = httpx.Request("POST", "http://ha/api/services/script/room_policy_set")
    return httpx.HTTPStatusError(
        "refused", request=request, response=httpx.Response(status, json={"message": message}, request=request))


def run(coro):
    return asyncio.run(coro)


def apply_through_guard(req, *, ha=None, permissions=OWNER, mode="owner", writes=None, slugs=("study",), **scope):
    """``apply`` against the real guard; ``(outcome, ha, scope)``. No scope when ``permissions`` is None."""
    ha = ha or FakePolicyHA()
    inner = _InnerWithScript(ha)
    guard = mp.PermissionEnforcingHAClient(inner)
    writes = writes if writes is not None else [f"input_boolean.{req.slug}_leave_lights_on", f"input_boolean.{req.slug}_motion_disable"]
    if permissions is None:
        return run(rp.apply(req, guard, equivalent_writes=writes, policy_slugs=frozenset(slugs))), ha, None
    with mp.ha_permission_scope(permissions, mode=mode, **scope) as opened:
        outcome = run(rp.apply(req, guard, equivalent_writes=writes, policy_slugs=frozenset(slugs)))
    return outcome, ha, opened


class _InnerWithScript:
    def __init__(self, ha):
        self.call_service_response = ha.call_service_response
        self.call_service = AsyncMock(return_value={})


def hold_on(**kw):
    return rp.PolicyRequest("hold_on", "study", "study", **kw)


# ---------------------------------------------------------------------------
# who may: exactly the callers today's helper writes allow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,permissions,scope", [
    ("owner_trusted", OWNER, {"trusted_voice": True}),
    ("owner_untrusted", OWNER, {"trusted_voice": False}),
    ("degraded", mp.degraded_permissions(), {}),
    ("no_scope", None, {}),
])
def test_untrusted_owner_allowed_as_today(name, permissions, scope):
    outcome, ha, _ = apply_through_guard(hold_on(), permissions=permissions, **scope)
    assert outcome.ok, name
    assert [f["caller_role"] for f in ha.fields] == ["owner"] and ha.calls[0][:2] == ("script", "room_policy_set")


@pytest.mark.parametrize("name,kwargs,reason", [
    ("guest", {"permissions": GUEST, "mode": "guest"}, "guest"),
    ("guest_scope_only", {"permissions": OWNER, "mode": "guest"}, "guest"),
    ("read_only", {"read_only": True}, "read_only"),
    ("restricted_helper", {"permissions": RESTRICTED, "mode": "owner"}, "no_permission"),
])
def test_refused_callers_make_no_ha_call(name, kwargs, reason):
    outcome, ha, scope = apply_through_guard(hold_on(), **kwargs)
    assert not outcome.ok and outcome.reason == reason and ha.calls == [], name
    assert scope.denials, "the refusal is recorded on the scope"
    assert outcome.speech


def test_the_request_has_no_place_for_a_role_or_a_room_from_the_caller():
    import dataclasses
    assert {f.name for f in dataclasses.fields(rp.PolicyRequest)} == {
        "kind", "slug", "spoken_room", "duration_minutes", "brightness", "clear_capture"}
    outcome, ha, _ = apply_through_guard(hold_on(duration_minutes=20))
    assert ha.fields == [{"area": "study", "caller_role": "owner", "kind": "hold_on", "set_by": "voice",
                          "apply_now": False, "duration_minutes": 20}]


def test_an_unknown_slug_makes_no_call():
    outcome, ha, _ = apply_through_guard(rp.PolicyRequest("hold_on", "den", "den"))
    assert outcome.reason == "unknown_area" and ha.calls == []


@pytest.mark.parametrize("slug", ["whole_house", "all", "", "house", "everywhere"])
def test_whole_house_all_and_empty_are_refused(slug):
    outcome, ha, _ = apply_through_guard(rp.PolicyRequest("hold_on", slug, "the house"), slugs=("study", slug))
    assert outcome.reason == "whole_house" and ha.calls == []


def test_a_spoken_whole_house_is_refused_even_with_a_real_slug():
    outcome, ha, _ = apply_through_guard(rp.PolicyRequest("hold_on", "study", "whole house"))
    assert outcome.reason == "whole_house" and ha.calls == []


# ---------------------------------------------------------------------------
# durations and the speech
# ---------------------------------------------------------------------------

def test_no_duration_sends_no_minutes_and_says_until_you_leave():
    for kind, state in (("hold_on", "on"), ("hold_off", "off")):
        outcome, ha, _ = apply_through_guard(rp.PolicyRequest(kind, "study", "study"))
        assert "duration_minutes" not in ha.fields[0] and "until" not in ha.fields[0]
        assert outcome.speech == f"Lights will stay {state} in the study {NO_END}."


@pytest.mark.parametrize("asked,sent", [(600, 480), (300, 300), (2, 5), (5, 5), (480, 480), (45, 45)])
def test_an_explicit_duration_is_clamped_to_5_480(asked, sent):
    for kind in ("hold_on", "hold_off"):
        _, ha, _ = apply_through_guard(rp.PolicyRequest(kind, "study", "study", duration_minutes=asked))
        assert ha.fields[0]["duration_minutes"] == sent


def test_the_reply_for_an_explicit_duration_is_todays_wording():
    ha = FakePolicyHA(until="2026-01-01T10:00:00-05:00", until_spoken="10:00 AM")
    outcome, _, _ = apply_through_guard(hold_on(duration_minutes=90), ha=ha)
    assert outcome.speech == "Lights will stay on in the study for 1 hour 30 minutes."
    outcome, _, _ = apply_through_guard(rp.PolicyRequest("hold_off", "study", "study", duration_minutes=600), ha=ha)
    assert outcome.speech == "Lights will stay off in the study for 8 hours.", "the reply says what was set, 480 minutes"


def test_an_empty_until_in_the_response_gives_the_no_end_reply():
    outcome, _, _ = apply_through_guard(hold_on(duration_minutes=30), ha=FakePolicyHA(until=""))
    assert outcome.speech == f"Lights will stay on in the study {NO_END}."


def test_a_stale_response_is_spoken_from_what_was_asked():
    stale = FakePolicyHA(until="", stale=True)
    outcome, _, _ = apply_through_guard(hold_on(duration_minutes=30), ha=stale)
    assert outcome.speech == "Lights will stay on in the study for 30 minutes."
    outcome, _, _ = apply_through_guard(hold_on(), ha=FakePolicyHA(until="2026-01-01T10:00:00-05:00", stale=True))
    assert outcome.speech == f"Lights will stay on in the study {NO_END}."


def test_a_script_answer_with_an_until_and_no_request_duration_speaks_the_time():
    ha = FakePolicyHA(until="2026-01-01T10:00:00-05:00", until_spoken="10:00 AM")
    outcome, _, _ = apply_through_guard(hold_on(), ha=ha)
    assert outcome.speech == "Lights will stay on in the study until 10:00 AM."


def test_a_refusal_from_the_script_is_spoken_and_capped():
    ha = FakePolicyHA()
    ha.error = refusal(400, "x" * 400)
    outcome, _, _ = apply_through_guard(hold_on(), ha=ha)
    assert outcome.reason == "script_refused" and not outcome.ok
    assert outcome.speech.startswith("I couldn't change that: ") and len(outcome.speech) == len("I couldn't change that: ") + 160
    ha.error = refusal(500, "Motion lighting is paused for the night")
    outcome, _, _ = apply_through_guard(hold_on(), ha=ha)
    assert outcome.speech == "I couldn't change that: Motion lighting is paused for the night"


def test_unreachable_and_malformed_answers_are_spoken_without_detail():
    for error, reason in (
        (httpx.ConnectError("http://ha.invalid refused"), "ha_unavailable"),
        (httpx.ReadTimeout("slow"), "ha_unavailable"),
        (HAResponseShapeError("no service_response"), "bad_response"),
    ):
        ha = FakePolicyHA()
        ha.error = error
        outcome, _, _ = apply_through_guard(hold_on(), ha=ha)
        assert outcome.reason == reason and "http" not in outcome.speech and "ha.invalid" not in outcome.speech


def test_a_capture_needs_a_level_and_clamps_it():
    with pytest.raises(ValueError):
        apply_through_guard(rp.PolicyRequest("capture", "study", "study"))
    _, ha, _ = apply_through_guard(rp.PolicyRequest("capture", "study", "study", brightness=400))
    assert ha.fields[0]["brightness"] == 100 and ha.fields[0]["kind"] == "capture"


def test_one_call_and_no_retry_on_failure():
    ha = FakePolicyHA()
    ha.error = httpx.ConnectError("x")
    apply_through_guard(hold_on(), ha=ha)
    assert len(ha.calls) == 1


# ---------------------------------------------------------------------------
# which room: resolving a spoken room to a policy slug
# ---------------------------------------------------------------------------

def registry(areas):
    return hr.RegistrySnapshot.from_raw(
        {"areas": [{"area_id": a, "name": n, "aliases": al} for a, n, al in areas], "devices": [], "entities": []},
        fetched_at=1.0)


def test_master_bedroom_reached_by_ha_area_without_alias():
    """The room is in the policy map by its area id; no alias of the area
    has the room's slug in it."""
    states = policy_states("suite", "primary_suite")
    snapshot = registry([
        ("primary_suite", "Primary Suite", []), ("primary_suite_2", "Primary Suite 2", []),
        ("primary_suite_tv", "Primary Suite TV", []), ("hall", "Hall", [])])
    assert rp.resolve("primary_suite", states, snapshot) == "suite"
    for decoy in ("primary_suite_2", "primary_suite_tv", "hall", "suite", ""):
        assert rp.resolve(decoy, states, snapshot) is None, decoy


def test_a_spoken_alias_reaches_the_room_when_one_area_has_it():
    states = policy_states("suite", "primary_suite")
    snapshot = registry([("primary_suite", "Primary Suite", ["Owner Wing"]), ("hall", "Hall", [])])
    assert rp.resolve("owner_wing", states, snapshot) == "suite"
    for other in ("aa_hall", "zz_hall"):  # whichever order the registry lists them in
        two = registry([("primary_suite", "Primary Suite", ["Owner Wing"]), (other, "Hall", ["Owner Wing"])])
        assert rp.resolve("owner_wing", states, two) is None, "an alias two areas share names no room"


def test_without_a_registry_the_key_is_the_area_id():
    states = policy_states("suite", "primary_suite")
    assert rp.resolve("primary_suite", states, None) == "suite"
    assert rp.resolve("suite", states, None) is None


def test_no_policy_rooms_resolve_nothing():
    assert rp.resolve("study", helper_states("study"), registry([("area_study", "Study", [])])) is None
    assert rp.resolve("study", policy_states(scripts=()), registry([("area_study", "Study", [])])) is None


# ---------------------------------------------------------------------------
# through the motion-control handler
# ---------------------------------------------------------------------------

def intent(room=None, action="leave_lights_on", **params):
    return {"device_type": "motion_control", "room": room, "action": action, "target_scope": "group",
            "parameters": params}


class PolicyChat:
    """The harness house plus a study that policy runs and a nursery that it doesn't."""

    def __init__(self, monkeypatch, *, study_area="area_study", **fake):
        self.chat = Chat(monkeypatch)
        cache = self.chat.rig.manager._entities_cache
        cache.update(policy_states("study", study_area))
        cache.update(helper_states("nursery"))
        self.chat.rig.manager._build_indexes()
        self.ha = FakePolicyHA(**fake)
        self.chat.rig.raw.call_service_response = AsyncMock(side_effect=self.ha.call_service_response)
        manager = self.chat.rig.manager
        mp.set_policy_room_source(lambda: rp.policy_slugs(manager.cached_entities()))

    def say(self, reply, query="leave the lights on", **kw):
        self.chat.llm.reply = reply
        return self.chat.turn(query, **kw).answer

    @property
    def legacy(self):
        return [(c.args[0], c.args[1], c.args[2]) for c in self.chat.rig.raw.call_service.await_args_list]


@pytest.fixture
def pc(monkeypatch):
    return PolicyChat(monkeypatch)


def test_no_duration_through_the_handler(pc):
    for reply in (intent("study"), intent("study", duration_minutes=None)):
        pc.ha.calls.clear()
        answer = pc.say(reply)
        assert pc.ha.fields == [{"area": "study", "caller_role": "owner", "kind": "hold_on", "set_by": "voice",
                                 "apply_now": False}], reply
        assert NO_END in answer and pc.legacy == []


def test_no_duration_sends_nothing_to_the_helpers(pc):
    pc.say(intent("study", "leave_lights_off"), "leave the lights off")
    assert pc.legacy == [] and [f["kind"] for f in pc.ha.fields] == ["hold_off"]


def test_every_action_maps_to_its_script_call(pc):
    cases = [
        (intent("study", "leave_lights_on", duration_minutes=20), "leave the lights on for 20 minutes",
         ("room_policy_set", {"kind": "hold_on", "duration_minutes": 20, "set_by": "voice", "apply_now": False})),
        (intent("study", "leave_lights_off", duration_minutes=15), "leave the lights off for 15 minutes",
         ("room_policy_set", {"kind": "hold_off", "duration_minutes": 15, "set_by": "voice", "apply_now": False})),
        (intent("study", "disable_motion"), "disable motion",
         ("room_policy_set", {"kind": "disable", "set_by": "voice", "apply_now": False})),
        (intent("study", "enable_motion"), "resume motion",
         ("room_policy_clear", {"clear_capture": True})),
        (intent("study", "set_brightness_override", brightness_percent=40), "set motion brightness to 40 percent",
         ("room_policy_set", {"kind": "capture", "brightness": 40, "set_by": "voice", "apply_now": False})),
    ]
    for reply, query, (script, extra) in cases:
        pc.ha.calls.clear()
        pc.say(reply, query)
        assert pc.ha.calls == [("script", script, {"area": "study", "caller_role": "owner", **extra})], query
    assert pc.legacy == []


def study_lamps(pc, state, brightness=None):
    cache = pc.chat.rig.manager._entities_cache
    for name in ("light.study_lamp_0", "light.study_lamp_1", "light.study_lamp_2"):
        cache[name] = _state(name, "off")
    if state == "on":
        cache["light.study_lamp_0"] = _state("light.study_lamp_0", "on", brightness=brightness)


def test_keep_current_brightness_captures_the_rooms_level_clamped(pc):
    answers = {}
    for raw, expected in ((128, 50), (1, 1), (255, 100)):
        study_lamps(pc, "on", raw)
        pc.ha.calls.clear()
        answers[raw] = pc.say(intent("study", "keep_current_brightness"), "keep this brightness")
        assert pc.ha.fields == [{"area": "study", "caller_role": "owner", "kind": "capture", "brightness": expected,
                                 "set_by": "voice", "apply_now": False}], raw
    assert answers[128] == "Brightness locked at 50% in the study."


def test_keep_current_brightness_with_the_lights_off_changes_nothing(pc):
    study_lamps(pc, "off")
    answer = pc.say(intent("study", "keep_current_brightness"), "keep this brightness")
    assert pc.ha.calls == [] and "lights are off" in answer


def test_a_brightness_override_without_a_level_asks_for_one(pc):
    answer = pc.say(intent("study", "set_brightness_override"), "set motion brightness")
    assert pc.ha.calls == [] and "specify a brightness level" in answer


def test_durations_through_the_handler(pc):
    for asked, sent in ((600, 480), (2, 5), (90, 90), (30.0, 30)):
        pc.ha.calls.clear()
        pc.say(intent("study", duration_minutes=asked), "leave the lights on")
        assert pc.ha.fields[0]["duration_minutes"] == sent, asked
    pc.ha.calls.clear()
    pc.say(intent("study", "leave_lights_off", duration_minutes=300), "leave the lights off for 5 hours")
    assert pc.ha.fields[0]["duration_minutes"] == 300
    for junk in ("an hour", True, [60]):
        pc.ha.calls.clear()
        pc.say(intent("study", duration_minutes=junk), "leave the lights on")
        assert "duration_minutes" not in pc.ha.fields[0], junk


def test_a_model_cannot_steer_the_role_or_the_room(pc):
    steering = {"caller_role": "dashboard", "area": "nursery", "room": "nursery", "kind": "disable", "apply_now": True,
                "set_by": "button", "test": True, "caller_area": "nursery", "until": "2099-01-01T00:00:00+00:00"}
    reply = intent("study")
    reply["parameters"].update(steering)
    reply["caller_role"] = "dashboard"
    pc.say(reply, "leave the lights on")
    assert pc.ha.fields == [{"area": "study", "caller_role": "owner", "kind": "hold_on", "set_by": "voice",
                             "apply_now": False}]


def test_the_speakers_room_is_used_when_none_is_named(pc):
    pc.say(intent(None), "leave the lights on", room="study")
    assert [f["area"] for f in pc.ha.fields] == ["study"]


def test_a_readback_with_an_until_and_an_empty_one(pc):
    pc.ha.until, pc.ha.until_spoken = "2026-01-01T10:00:00-05:00", "10:00 AM"
    assert pc.say(intent("study", duration_minutes=20)) == "Lights will stay on in the study for 20 minutes."
    pc.ha.until = ""
    assert NO_END in pc.say(intent("study", duration_minutes=20))


def test_a_refusal_400_is_spoken_through_the_handler(pc):
    pc.ha.error = refusal(400, "That room is paused")
    assert pc.say(intent("study")) == "I couldn't change that: That room is paused"


# who may, through the whole turn

def test_an_untrusted_owner_turn_reaches_the_script(pc):
    pc.say(intent("study"), trusted_voice=False)
    assert len(pc.ha.calls) == 1 and pc.ha.fields[0]["caller_role"] == "owner"


def test_a_degraded_turn_reaches_the_script_as_today(pc):
    pc.say(intent("study"), perms=mp.degraded_permissions(), mode="owner")
    assert len(pc.ha.calls) == 1


def test_a_guest_turn_is_refused_before_any_call(pc):
    answer = pc.say(intent("study"), perms=GUEST, mode="guest")
    assert pc.ha.calls == [] and pc.legacy == [] and "guest mode" in answer


def test_a_restricted_helper_turn_is_refused_before_any_call(pc):
    answer = pc.say(intent("study"), perms=RESTRICTED, mode="owner")
    assert pc.ha.calls == [] and pc.legacy == [] and answer


def test_a_room_the_area_map_does_not_know_is_not_written_directly(monkeypatch):
    pc = PolicyChat(monkeypatch, study_area="area_elsewhere")
    answer = pc.say(intent("study"))
    assert pc.ha.calls == [] and pc.legacy == []
    assert answer == "Voice control isn't set up for the study yet."


def test_whole_house_or_an_unnamed_room_reaches_no_policy_script(pc):
    for room in ("whole_house", "all", "the whole house"):
        pc.say(intent(room), query="leave the lights on", room="unknown")
    assert pc.ha.calls == []
    assert [c for c in pc.legacy if c[0].startswith("input_")] == []


# plain rooms keep today's writes, byte for byte

LEGACY_ROWS = [
    ("leave_lights_on", {}, [("input_number", "set_value", "leave_lights_on_minutes", 60), ("input_boolean", "turn_on", "leave_lights_on", None)],
     "Lights will stay on in the nursery for 1 hour."),
    ("leave_lights_on", {"duration_minutes": None}, [("input_number", "set_value", "leave_lights_on_minutes", 60), ("input_boolean", "turn_on", "leave_lights_on", None)],
     "Lights will stay on in the nursery for 1 hour."),
    ("leave_lights_on", {"duration_minutes": 90}, [("input_number", "set_value", "leave_lights_on_minutes", 90), ("input_boolean", "turn_on", "leave_lights_on", None)],
     "Lights will stay on in the nursery for 1 hour 30 minutes."),
    ("leave_lights_on", {"duration_minutes": 600}, [("input_number", "set_value", "leave_lights_on_minutes", 480), ("input_boolean", "turn_on", "leave_lights_on", None)],
     "Lights will stay on in the nursery for 10 hours."),
    ("leave_lights_off", {"duration_minutes": 2}, [("input_number", "set_value", "leave_lights_off_minutes", 5), ("input_boolean", "turn_on", "leave_lights_off", None)],
     "Lights will stay off in the nursery for 2 minutes."),
    ("leave_lights_off", {"duration_minutes": 45}, [("input_number", "set_value", "leave_lights_off_minutes", 45), ("input_boolean", "turn_on", "leave_lights_off", None)],
     "Lights will stay off in the nursery for 45 minutes."),
    ("disable_motion", {}, [("input_boolean", "turn_on", "motion_disable", None)],
     "Motion detection disabled in the nursery. Use the light switch to control lights."),
    ("enable_motion", {}, [("input_boolean", "turn_off", "motion_disable", None), ("input_boolean", "turn_off", "leave_lights_on", None),
                            ("input_boolean", "turn_off", "leave_lights_off", None), ("input_boolean", "turn_off", "brightness_override", None)],
     "Motion detection resumed in the nursery. All overrides cleared."),
    ("set_brightness_override", {"brightness_percent": 40}, [("input_number", "set_value", "override_brightness", 40), ("input_boolean", "turn_on", "brightness_override", None)],
     "Motion will set lights to 40% brightness in the nursery."),
]


@pytest.mark.parametrize("action,params,writes,reply", LEGACY_ROWS, ids=[f"{r[0]}-{r[1]}" for r in LEGACY_ROWS])
def test_non_policy_room_writes_identical(pc, action, params, writes, reply):
    answer = pc.say(intent("nursery", action, **params), f"{action.replace('_', ' ')} in the nursery")
    expected = []
    for domain, service, name, value in writes:
        data = {"entity_id": f"{domain}.nursery_{name}"}
        if value is not None:
            data["value"] = value
        expected.append((domain, service, data))
    assert pc.legacy == expected
    assert answer == reply
    assert pc.ha.calls == []


def test_a_non_policy_room_with_a_null_duration_writes_60_and_says_an_hour(pc):
    answer = pc.say(intent("nursery", duration_minutes=None), "leave the lights on in the nursery")
    assert pc.legacy[0] == ("input_number", "set_value", {"entity_id": "input_number.nursery_leave_lights_on_minutes", "value": 60})
    assert answer == "Lights will stay on in the nursery for 1 hour."


# ---------------------------------------------------------------------------
# good night
# ---------------------------------------------------------------------------

def good_night(pc, *, room="study", permissions=OWNER, mode="owner"):
    controller = pc.chat.rig.controller
    with mp.ha_permission_scope(permissions, mode=mode) as scope:
        answer = run(controller._handle_scene_intent(
            "activate", {"entity_id": "script.good_night"}, pc.chat.rig.raw, original_query="good night", room=room))
    return answer, scope


def test_good_night_in_a_policy_room_runs_its_script(pc):
    pc.ha.until, pc.ha.until_spoken = "2026-01-01T06:30:00-05:00", "6:30 AM"
    answer, _ = good_night(pc)
    assert pc.ha.calls == [("script", "room_policy_good_night", {"area": "study", "caller_role": "owner"})]
    assert pc.legacy == [] and "6:30 AM" in answer


def test_good_night_is_refused_in_a_degraded_scope_with_no_call(pc):
    answer, scope = good_night(pc, permissions=mp.degraded_permissions())
    assert pc.ha.calls == [] and pc.legacy == []
    assert scope.denials and scope.denials[-1].reason == "entity_or_domain_denied"


def test_good_night_is_refused_for_a_guest_with_no_call(pc):
    _, scope = good_night(pc, permissions=GUEST, mode="guest")
    assert pc.ha.calls == [] and scope.denials


def test_good_night_in_a_plain_room_or_without_the_script_is_the_scene_path(pc):
    answer, _ = good_night(pc, room="nursery")
    assert pc.ha.calls == [] and pc.legacy == [("script", "turn_on", {"entity_id": "script.good_night"})]
    pc.chat.rig.raw.call_service.reset_mock()
    del pc.chat.rig.manager._entities_cache["script.room_policy_good_night"]
    good_night(pc, room="study")
    assert pc.ha.calls == [] and pc.legacy == [("script", "turn_on", {"entity_id": "script.good_night"})]


def test_good_night_with_no_room_is_the_scene_path(pc):
    good_night(pc, room=None)
    assert pc.ha.calls == [] and pc.legacy == [("script", "turn_on", {"entity_id": "script.good_night"})]


# ---------------------------------------------------------------------------
# the prompt, and what is never called
# ---------------------------------------------------------------------------

def test_the_motion_prompt_does_not_invent_a_duration(pc):
    prompts = []
    real = pc.chat.llm.generate

    async def capture(**kwargs):
        prompts.append(kwargs["prompt"])
        return await real(**kwargs)

    pc.chat.rig.controller.llm_router.generate = capture
    pc.chat.llm.reply = intent("study")
    run(pc.chat.rig.controller._extract_motion_control_intent("leave the lights on", "study"))
    prompt = prompts[0]
    assert "default 60" not in prompt and "-> 60 minutes" not in prompt and '"duration_minutes": 60' not in prompt
    assert "null when no duration is stated" in prompt and "-> 480 minutes" in prompt
    assert NO_END in prompt


def test_athena_never_calls_the_scripts_it_has_no_use_for():
    forbidden = ("room_policy_get", "room_policy_schedule_set")
    found = []
    for path in (ROOT / "src").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(f in node.value for f in forbidden):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert found == []


def test_the_scripts_athena_calls_are_exactly_the_three_in_the_contract():
    assert rp.POLICY_SCRIPTS == {"room_policy_set", "room_policy_clear", "room_policy_good_night"}
    doc = (ROOT / "docs" / "ROOM_POLICY.md").read_text()
    for name in rp.POLICY_SCRIPTS:
        assert f"`{name}`" in doc


def test_the_handler_authorizes_the_caller_against_the_writes_it_replaces(pc, monkeypatch):
    seen = {}
    real = rp.apply

    async def spy(req, ha, **kw):
        seen[req.kind] = kw["equivalent_writes"]
        return await real(req, ha, **kw)

    monkeypatch.setattr(rp, "apply", spy)
    pc.say(intent("study", duration_minutes=20), "leave the lights on")
    pc.say(intent("study", "leave_lights_off", duration_minutes=20), "leave the lights off")
    pc.say(intent("study", "disable_motion"), "disable motion")
    pc.say(intent("study", "enable_motion"), "resume motion")
    pc.say(intent("study", "set_brightness_override", brightness_percent=30), "set motion brightness to 30 percent")
    helpers = lambda *names: [f"{d}.study_{n}" for d, n in names]  # noqa: E731
    motion = ("input_boolean", "motion_disable")
    assert seen["hold_on"] == helpers(("input_number", "leave_lights_on_minutes"), ("input_boolean", "leave_lights_on"), motion)
    assert seen["hold_off"] == helpers(("input_number", "leave_lights_off_minutes"), ("input_boolean", "leave_lights_off"), motion)
    assert seen["disable"] == helpers(motion)
    assert seen["clear"] == helpers(("input_boolean", "leave_lights_on"), ("input_boolean", "leave_lights_off"),
                                    ("input_boolean", "brightness_override"), motion)
    assert seen["capture"] == helpers(("input_number", "override_brightness"), ("input_boolean", "brightness_override"), motion)


def test_the_entity_manager_hands_out_its_cache_as_it_stands(monkeypatch):
    manager = hem.HAEntityManager(object())
    assert manager.cached_entities() is None, "nothing fetched: the guard fails closed"
    manager._entities_cache = {"input_boolean.x_motion_disable": {}}
    assert manager.cached_entities() is manager._entities_cache
