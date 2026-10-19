"""room_policy: voice changes to a room's lighting holds, through the room's
Home Assistant policy scripts and nowhere else.

A *policy room* is one whose lighting a Home Assistant policy engine runs.
Home Assistant says which rooms those are (``policy_slugs``) and which spoken
area is which room (``policy_map``), so nothing here names a house. For such a
room ``apply`` calls one of three scripts, ``script.room_policy_set``,
``script.room_policy_clear`` or ``script.room_policy_good_night``, through
``PermissionEnforcingHAClient.run_policy_script``, and speaks what the script
reports it did. Every other write to a policy room's helpers is refused by the
guard (``mode_permission.policy_protection``), so this module is the only way
in. See docs/ROOM_POLICY.md.

Invariants ``apply`` keeps:

* the role (``owner``) and the room (a slug in the same snapshot's policy
  slugs) never come from the request body;
* ``whole_house``, ``all`` and an empty room are refused: no voice action here
  reaches more than one room;
* only the three scripts above are ever called, with the fields in
  docs/ROOM_POLICY.md; ``duration_minutes`` is omitted when none was stated
  (no end time) and otherwise clamped to 5..480;
* one script call, no retries.

``python -m orchestrator.room_policy --selftest [--expect-extras N]`` runs the
guard's matrix against a recording fake and prints one line a deploy can gate
on (``SELFTEST_LINE``).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import AbstractSet, Any, Awaitable, Callable, Dict, FrozenSet, List, Literal, Mapping, Optional, Sequence, Tuple

import httpx
import structlog

from orchestrator import mode_permission as mp
from shared.ha_client import HAResponseShapeError, HomeAssistantNotConfiguredError

logger = structlog.get_logger(__name__)

SET_SCRIPT = "room_policy_set"
CLEAR_SCRIPT = "room_policy_clear"
GOOD_NIGHT_SCRIPT = "room_policy_good_night"
POLICY_SCRIPTS = frozenset({SET_SCRIPT, CLEAR_SCRIPT, GOOD_NIGHT_SCRIPT})

CALLER_ROLE = "owner"
SET_BY = "voice"
MIN_MINUTES = 5
MAX_MINUTES = 480
MAX_REFUSAL_CHARS = 160
AREA_ATTRIBUTE = "ha_area"
WHOLE_HOUSE_WORDS = frozenset({"", "whole_house", "all", "house", "everywhere"})
NO_END_PHRASE = "until you leave or press the switch"

_MOTION_DISABLE_PREFIX = "input_boolean."
_MOTION_DISABLE_SUFFIX = "_motion_disable"
_POLICY_PREFIX = "input_text."
_POLICY_SUFFIX = "_policy"
_SET_ENTITY = f"script.{SET_SCRIPT}"

Kind = Literal["hold_on", "hold_off", "disable", "clear", "capture", "good_night"]
Reason = Literal[
    "guest", "no_permission", "read_only", "unknown_area", "whole_house", "script_refused", "ha_unavailable",
    "bad_response",
]


class PolicyGrant:
    """Held by this module alone: the guard's ``run_policy_script`` accepts
    only this module's instance (a drift test pins the single construction)."""


_GRANT = PolicyGrant()


# ---------------------------------------------------------------------------
# Which rooms are policy rooms (from Home Assistant's states)
# ---------------------------------------------------------------------------

def _area_of(state: Any) -> str:
    attributes = state.get("attributes") if isinstance(state, Mapping) else None
    area = attributes.get(AREA_ATTRIBUTE) if isinstance(attributes, Mapping) else None
    return area.strip() if isinstance(area, str) else ""


def _slug_areas(states: Mapping[str, Any]) -> Dict[str, str]:
    """``{slug: ha_area}`` for every slug that is a policy room: it has
    ``input_boolean.<slug>_motion_disable`` and ``input_text.<slug>_policy``
    carrying a non-empty ``ha_area`` attribute, and ``script.room_policy_set``
    exists."""
    if not isinstance(states, Mapping) or _SET_ENTITY not in states:
        return {}
    found: Dict[str, str] = {}
    for entity_id, state in states.items():
        if not (isinstance(entity_id, str) and entity_id.startswith(_POLICY_PREFIX) and entity_id.endswith(_POLICY_SUFFIX)):
            continue
        slug = entity_id[len(_POLICY_PREFIX):-len(_POLICY_SUFFIX)]
        area = _area_of(state)
        if slug and area and f"{_MOTION_DISABLE_PREFIX}{slug}{_MOTION_DISABLE_SUFFIX}" in states:
            found[slug] = area
    return found


def policy_slugs(states: Mapping[str, Any]) -> FrozenSet[str]:
    """The slugs of the rooms Athena routes to the policy scripts: the rooms
    whose lighting Home Assistant's policy engine runs, with their area."""
    return frozenset(_slug_areas(states))


_ENGINE_PREFIX = "script.room_policy_"
_engine_seen = False


def _reset_engine_seen_for_tests() -> None:
    global _engine_seen
    _engine_seen = False


def protected_slugs(states: Optional[Mapping[str, Any]]) -> Optional[FrozenSet[str]]:
    """The slugs whose helpers the guard protects; None when that can't be known.

    Wider than ``policy_slugs`` on purpose: a room's helpers stay protected
    while ``script.room_policy_set`` is briefly missing (a script reload, a
    Home Assistant restart) or its ``ha_area`` is blank, which would only stop
    the *routing*. Whether a ``room_policy_*`` script has been seen is
    recorded from every snapshot first, before any other check, and once it
    has (sticky, for the life of the process) the set comes from the helper
    markers alone: ``input_text.<slug>_policy`` with
    ``input_boolean.<slug>_motion_disable``. Until one has, nothing is
    protected and helper writes go through as before the engine was
    published, including in a house with no ``input_boolean`` at all.

    Cold (None, every helper of the policy shape protected): nothing cached,
    an empty cache, or a cache that lost its ``input_boolean`` entities after
    the engine was seen."""
    global _engine_seen
    if not isinstance(states, Mapping):
        return None
    if not _engine_seen and any(isinstance(i, str) and i.startswith(_ENGINE_PREFIX) for i in states):
        _engine_seen = True
    if not states:
        return None
    if not any(isinstance(i, str) and i.startswith(_MOTION_DISABLE_PREFIX) for i in states):
        return None if _engine_seen else frozenset()
    if not _engine_seen:
        return frozenset()
    return frozenset(
        entity_id[len(_POLICY_PREFIX):-len(_POLICY_SUFFIX)]
        for entity_id in states
        if isinstance(entity_id, str) and entity_id.startswith(_POLICY_PREFIX) and entity_id.endswith(_POLICY_SUFFIX)
        and len(entity_id) > len(_POLICY_PREFIX) + len(_POLICY_SUFFIX)
        and f"{_MOTION_DISABLE_PREFIX}{entity_id[len(_POLICY_PREFIX):-len(_POLICY_SUFFIX)]}{_MOTION_DISABLE_SUFFIX}" in states
    )


def policy_map(states: Mapping[str, Any]) -> Mapping[str, str]:
    """``{ha_area: slug}``. An area two slugs claim is dropped (and counted in
    the log): neither room is then reachable by voice, which is the safe side."""
    by_area: Dict[str, List[str]] = {}
    for slug, area in _slug_areas(states).items():
        by_area.setdefault(area, []).append(slug)
    dropped = sum(1 for slugs in by_area.values() if len(slugs) > 1)
    if dropped:
        logger.warning("room_policy_area_claimed_twice", areas=dropped)
    return {area: slugs[0] for area, slugs in by_area.items() if len(slugs) == 1}


def resolve(room_key: str, states: Mapping[str, Any], snapshot: Any) -> Optional[str]:
    """The policy slug for a room the way the speaker named it, else None.

    The area is the registry's own area for the key, or the one area that has
    the key as an alias. With no registry the key is taken as the area id."""
    key = (room_key or "").strip().lower()
    if key in WHOLE_HOUSE_WORDS:
        return None
    areas = policy_map(states)
    if not areas:
        return None
    if snapshot is None:
        return areas.get(key)
    area = snapshot.area_by_key(key)
    if area is None:
        hits = snapshot.areas_by_alias(key)
        area = hits[0] if len(hits) == 1 else None
    return areas.get(area.area_id) if area is not None else None


# ---------------------------------------------------------------------------
# One request, one script call
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyRequest:
    kind: Kind
    slug: str
    spoken_room: str
    duration_minutes: Optional[int] = None
    brightness: Optional[int] = None
    clear_capture: bool = False


@dataclass(frozen=True)
class PolicyOutcome:
    ok: bool
    speech: str
    reason: Optional[Reason] = None
    response: Optional[Mapping[str, Any]] = None


def clamp_minutes(minutes: int) -> int:
    return min(MAX_MINUTES, max(MIN_MINUTES, minutes))


def spoken_duration(minutes: int) -> str:
    """``90`` -> ``1 hour 30 minutes``."""
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins > 0:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


def _call_for(req: PolicyRequest) -> Tuple[str, Dict[str, Any]]:
    """The script and its fields for a request (the contract in docs/ROOM_POLICY.md)."""
    fields: Dict[str, Any] = {"area": req.slug, "caller_role": CALLER_ROLE}
    if req.kind == "clear":
        fields["clear_capture"] = bool(req.clear_capture)
        return CLEAR_SCRIPT, fields
    if req.kind == "good_night":
        return GOOD_NIGHT_SCRIPT, fields
    fields.update(kind=req.kind if req.kind in ("hold_on", "hold_off", "disable") else "capture",
                  set_by=SET_BY, apply_now=False)
    if req.kind in ("hold_on", "hold_off") and req.duration_minutes is not None:
        fields["duration_minutes"] = clamp_minutes(int(req.duration_minutes))
    if req.kind == "capture":
        if req.brightness is None:
            raise ValueError("capture needs a brightness")
        fields["brightness"] = min(100, max(1, int(req.brightness)))
    return SET_SCRIPT, fields


def _hold_speech(req: PolicyRequest, response: Mapping[str, Any]) -> str:
    state = "on" if req.kind == "hold_on" else "off"
    lead = f"Lights will stay {state} in the {req.spoken_room}"
    stale = bool(response.get("stale"))
    until = response.get("until")
    if (req.duration_minutes is None) if stale else not until:
        return f"{lead} {NO_END_PHRASE}."
    if req.duration_minutes is not None:
        return f"{lead} for {spoken_duration(clamp_minutes(int(req.duration_minutes)))}."
    spoken = response.get("until_spoken")
    return f"{lead} until {spoken}." if spoken else f"{lead} for a while."


def _speech(req: PolicyRequest, response: Mapping[str, Any]) -> str:
    room = req.spoken_room
    if req.kind in ("hold_on", "hold_off"):
        return _hold_speech(req, response)
    if req.kind == "disable":
        return f"Motion detection disabled in the {room}. Use the light switch to control lights."
    if req.kind == "clear":
        return f"Motion detection resumed in the {room}. All overrides cleared."
    if req.kind == "capture":
        level = min(100, max(1, int(req.brightness or 1)))
        return f"Motion will set lights to {level}% brightness in the {room}."
    spoken = response.get("until_spoken")
    tail = f" Motion lighting resumes at {spoken}." if spoken else ""
    return f"Good night! The {room} lights are off and set for the night.{tail}"


# Entity ids (including registry ids) and URLs never reach the speaker.
_ENTITY_OR_URL = re.compile(r"https?://\S+|\b[a-z_][a-z0-9_]*\.[a-z0-9_]+\b|\b[0-9a-f]{32}\b", re.IGNORECASE)


def _refusal_message(exc: httpx.HTTPStatusError) -> str:
    """What the script said when it refused, capped, never the entity ids it
    doesn't contain and never the raw body."""
    message = ""
    try:
        body = exc.response.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            message = body["message"]
    except ValueError:
        pass
    return _sanitized_refusal(message)


def _sanitized_refusal(message: str) -> str:
    message = _ENTITY_OR_URL.sub("that", message)
    message = " ".join(message.split()) or "that wasn't allowed"
    return message[:MAX_REFUSAL_CHARS]


def _script_declined(response: Any) -> Optional[str]:
    """The reason when the script answered with a refusal, exactly
    ``{ok: false, reason: <str>}``; anything else with ``ok`` in it is not one."""
    if (
        isinstance(response, Mapping) and set(response) == {"ok", "reason"}
        and response["ok"] is False and isinstance(response["reason"], str)
    ):
        return response["reason"]
    return None


def _valid_until(value: Any) -> bool:
    """F4: empty for no end, else an ISO-8601 timestamp with an offset (not
    null, not naive, not a bare date)."""
    if value == "":
        return True
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None and "T" in value.upper()
    except ValueError:
        return False


def _response_problem(req: PolicyRequest, response: Mapping[str, Any]) -> Optional[str]:
    """What is wrong with a script's answer against F4, else None. Nothing is
    confirmed aloud from an answer that fails: an empty answer must not read as
    "no end time"."""
    keys = ("area", "until", "until_spoken") if req.kind == "good_night" else (
        "area", "policy", "brightness_hold", "until", "until_spoken", "set_by", "stale")
    missing = [key for key in keys if key not in response]
    if missing:
        return f"missing {missing[0]}"
    if response["area"] != req.slug:
        return "area"
    if not _valid_until(response["until"]):
        return "until"
    if not isinstance(response["until_spoken"], str):
        return "until_spoken"
    if req.kind != "good_night" and not (
        isinstance(response["policy"], str) and isinstance(response["set_by"], str)
        and isinstance(response["brightness_hold"], bool) and isinstance(response["stale"], bool)
    ):
        return "policy, set_by, brightness_hold or stale"
    return None


def _refused(reason: Reason, speech: str) -> PolicyOutcome:
    return PolicyOutcome(ok=False, speech=speech, reason=reason)


_UNCONFIRMED = "I sent that, but I couldn't confirm it went through."
_DENIAL_SPEECH: Mapping[str, str] = {
    "guest": mp.GUEST_INTENT_REFUSAL,
    "read_only": "Sorry, I can't change that right now.",
    "no_permission": "Sorry, I can't change that.",
}


def _denied(exc: "mp.HAWritePermissionDenied") -> PolicyOutcome:
    """The guard's refusal, spoken from a fixed sentence (never the exception's text)."""
    reason: Reason = "guest" if exc.reason == "guest" else "read_only" if exc.reason == "read_only_scope" else "no_permission"
    return _refused(reason, _DENIAL_SPEECH[reason])


def _script_refused(script: str, exc: httpx.HTTPStatusError) -> PolicyOutcome:
    logger.warning("room_policy_script_refused", script=script, status=exc.response.status_code)
    return _refused("script_refused", f"I couldn't change that: {_refusal_message(exc)}")


async def apply(
    req: PolicyRequest,
    ha: Any,
    *,
    policy_slugs: AbstractSet[str],
) -> PolicyOutcome:
    """Carry out one request through the room's policy script.

    ``ha`` is the permission-enforcing HA client; it checks the fields, the
    room and the caller itself (docs/ROOM_POLICY.md). ``policy_slugs`` is the
    set from the snapshot the room was resolved in."""
    room = req.spoken_room
    if req.slug in WHOLE_HOUSE_WORDS or (req.spoken_room or "").strip().lower().replace(" ", "_") in WHOLE_HOUSE_WORDS:
        return _refused("whole_house", "Tell me which room to change.")
    if req.slug not in policy_slugs:
        return _refused("unknown_area", f"Voice control isn't set up for the {room} yet.")
    script, fields = _call_for(req)
    failure: Optional[PolicyOutcome] = None
    response: Any = None
    try:
        response = await ha.run_policy_script(script, fields, grant=_GRANT)
    except mp.HAWritePermissionDenied as exc:
        failure = _denied(exc)
    except httpx.HTTPStatusError as exc:
        failure = _script_refused(script, exc)
    except HAResponseShapeError:
        logger.warning("room_policy_bad_response", script=script)
        failure = _refused("bad_response", _UNCONFIRMED)
    except (httpx.HTTPError, HomeAssistantNotConfiguredError) as exc:
        logger.warning("room_policy_ha_unavailable", script=script, error_class=type(exc).__name__)
        failure = _refused("ha_unavailable", "I couldn't reach Home Assistant just now.")
    if failure is not None:
        return failure
    declined = _script_declined(response)
    if declined is not None:
        logger.warning("room_policy_script_refused", script=script, status=200)
        return _refused("script_refused", f"I couldn't change that: {_sanitized_refusal(declined)}")
    if not isinstance(response, Mapping) or "ok" in response or _response_problem(req, response):
        logger.warning("room_policy_bad_response", script=script)
        return _refused("bad_response", _UNCONFIRMED)
    logger.info("room_policy_applied", script=script, kind=req.kind)
    return PolicyOutcome(ok=True, speech=_speech(req, response), response=response)


# ---------------------------------------------------------------------------
# Self-test: the guard's matrix, against a recording fake
# ---------------------------------------------------------------------------

SELFTEST_LINE = "room_policy selftest boundary={boundary} router={router} extras={extras} cases={cases} failures={failures}"

_SELFTEST_ROOM = "selftest_den"
_SELFTEST_OTHER = "selftest_attic"
_SELFTEST_RESPONSE = {
    "area": _SELFTEST_ROOM, "policy": "hold_on", "brightness_hold": False, "until": "", "until_spoken": "",
    "set_by": SET_BY, "stale": False,
}
_PROTECTED_HELPER = f"input_boolean.{_SELFTEST_ROOM}_leave_lights_on"


def _member_state(entity_id: str, members: Optional[List[str]]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {} if members is None else {"entity_id": members}
    return {"entity_id": entity_id, "state": "on", "attributes": attributes}


# What the guard reads group and scene members from during the self-test.
_SELFTEST_STATES: Dict[str, Any] = {
    "group.selftest_bad": _member_state("group.selftest_bad", ["light.selftest_lamp", _PROTECTED_HELPER]),
    "group.selftest_nested": _member_state("group.selftest_nested", ["group.selftest_bad"]),
    "group.selftest_ok": _member_state("group.selftest_ok", ["light.selftest_lamp"]),
    "scene.selftest_bad": _member_state("scene.selftest_bad", [_PROTECTED_HELPER]),
    "scene.selftest_ok": _member_state("scene.selftest_ok", ["light.selftest_lamp"]),
    "scene.selftest_integration": _member_state("scene.selftest_integration", None),
}


class _RecordingInner:
    """Stands in for the HA client: records every call, talks to nothing."""

    def __init__(self, response: Optional[Mapping[str, Any]] = None) -> None:
        self.calls: List[Tuple[str, str, Any]] = []
        self.response = dict(_SELFTEST_RESPONSE) if response is None else dict(response)

    async def call_service(self, domain: str, service: str, service_data: Any = None) -> Dict[str, Any]:
        self.calls.append((domain, service, service_data))
        return {}

    async def call_service_response(self, domain: str, service: str, service_data: Any = None) -> Dict[str, Any]:
        self.calls.append((domain, service, service_data))
        return dict(self.response)

    async def create_automation(self, automation_id: str, config: Any) -> bool:
        self.calls.append(("automation", "create", automation_id))
        return True


def _sample_for(glob: str) -> str:
    return glob.replace("*", "selftest")


def _scope_for(permissions: Mapping[str, Any], mode: str, **scope_kwargs: Any) -> Any:
    """The reuse-aware opener the scope-drift guard accepts: a self-test run
    inside a request (it never is) would share that request's scope."""
    return (
        mp.ha_permission_scope(dict(permissions), mode=mode, **scope_kwargs)
        if mp.current_ha_scope() is None else contextlib.nullcontext()
    )


class _Matrix:
    """Runs cases, each in a fresh scope and a fresh recording inner client."""

    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []
        self.boundary_ran = False
        self.router_ran = False

    def record(self, name: str, passed: bool) -> None:
        self.results.append((name, bool(passed)))

    async def guarded(self, permissions: Optional[Mapping[str, Any]], mode: str, body: Callable[..., Awaitable[Any]],
                      **scope_kwargs: Any) -> Tuple[Any, _RecordingInner]:
        inner = _RecordingInner()
        guard = mp.PermissionEnforcingHAClient(inner)
        if permissions is None:
            return await body(guard), inner
        with _scope_for(permissions, mode, **scope_kwargs):
            return await body(guard), inner

    async def denied(self, name: str, reason: str, body: Callable[..., Awaitable[Any]],
                     permissions: Optional[Mapping[str, Any]] = None, mode: str = "owner", **scope_kwargs: Any) -> None:
        async def attempt(guard: Any) -> Optional[str]:
            refusal: Optional[str] = None
            try:
                await body(guard)
            except mp.HAWritePermissionDenied as exc:
                refusal = exc.reason
            return refusal

        owner = {"mode": "owner"} if permissions is None else permissions
        got, inner = await self.guarded(owner, mode, attempt, **scope_kwargs)
        self.record(name, got == reason and not inner.calls)

    async def allowed(self, name: str, body: Callable[..., Awaitable[Any]],
                      permissions: Optional[Mapping[str, Any]] = None, mode: str = "owner", **scope_kwargs: Any) -> None:
        async def attempt(guard: Any) -> bool:
            try:
                await body(guard)
            except mp.HAWritePermissionDenied:
                return False
            return True

        owner = {"mode": "owner"} if permissions is None else permissions
        ok, inner = await self.guarded(owner, mode, attempt, **scope_kwargs)
        self.record(name, ok and len(inner.calls) == 1)


async def _boundary_cases(m: _Matrix) -> None:
    m.boundary_ran = True
    den = _SELFTEST_ROOM

    def call(domain: str, service: str, data: Optional[Dict[str, Any]] = None):
        async def body(guard: Any) -> Any:
            return await guard.call_service(domain, service, data)
        return body

    await m.denied("script_by_name", "policy_protected", call("script", SET_SCRIPT, {"area": den}))
    await m.denied("script_turn_on_string", "policy_protected", call("script", "turn_on", {"entity_id": _SET_ENTITY}))
    await m.denied("script_turn_on_comma", "policy_protected",
                   call("script", "turn_on", {"entity_id": f"script.good_night, script.{CLEAR_SCRIPT}"}))
    await m.denied("script_turn_on_list", "policy_protected",
                   call("script", "turn_on", {"entity_id": [f"script.{GOOD_NIGHT_SCRIPT}"]}))
    await m.denied("script_turn_on_area", "policy_protected_unverifiable", call("script", "turn_on", {"area_id": "x"}))
    await m.denied("homeassistant_mixed_targets", "policy_protected",
                   call("homeassistant", "turn_on", {"entity_id": ["light.selftest_lamp", f"script.{CLEAR_SCRIPT}"]}))
    await m.denied("helper_boolean", "policy_protected",
                   call("input_boolean", "turn_on", {"entity_id": f"input_boolean.{den}_leave_lights_on"}))
    await m.denied("helper_number", "policy_protected",
                   call("input_number", "set_value", {"entity_id": f"input_number.{den}_override_brightness", "value": 5}))
    await m.denied("helper_domain_all", "policy_protected_unverifiable", call("input_boolean", "turn_off", {"entity_id": "all"}))

    async def automation(guard: Any) -> Any:
        return await guard.create_automation("selftest", {"alias": "x", "trigger": [], "action": [
            {"service": f"script.{SET_SCRIPT}", "data": {}}]})
    await m.denied("automation_calls_script", "policy_protected", automation)

    async def sequence(guard: Any) -> Any:
        decision = mp.authorize_sequence(
            [{"action": "turn_on", "target": {"entity_id": f"input_boolean.{den}_motion_disable"}}], {"mode": "owner"})
        if not decision.allowed:
            raise mp.HAWritePermissionDenied("sequence", reason=decision.reason)
    await m.denied("sequence_step_on_helper", "policy_protected", sequence)

    previous = mp._policy_room_source
    mp.set_policy_room_source(None)
    try:
        await m.denied("no_source_input_write", "policy_source_missing",
                       call("input_boolean", "turn_on", {"entity_id": f"input_boolean.{_SELFTEST_OTHER}_leave_lights_on"}))
    finally:
        mp.set_policy_room_source(previous)

    protected_helper = f"input_boolean.{den}_leave_lights_on"
    await m.denied("homeassistant_all", "policy_protected_unverifiable",
                   call("homeassistant", "turn_off", {"entity_id": "all"}))
    for verb in ("turn_on", "turn_off", "toggle"):
        await m.denied(f"homeassistant_{verb}_all", "policy_protected_unverifiable",
                       call("homeassistant", verb, {"entity_id": "all"}))
    await m.denied("homeassistant_area_only", "policy_protected_unverifiable",
                   call("homeassistant", "toggle", {"area_id": "x"}))
    await m.denied("protected_id_with_dummy_area", "policy_protected",
                   call("homeassistant", "turn_on", {"entity_id": protected_helper, "area_id": "x"}))
    await m.denied("protected_script_with_dummy_device", "policy_protected",
                   call("script", "turn_on", {"entity_id": _SET_ENTITY, "device_id": "x"}))
    await m.denied("registry_id_target", "policy_protected_unverifiable",
                   call("homeassistant", "turn_on", {"entity_id": "0123456789abcdef0123456789abcdef"}))
    await m.denied("scene_apply_entities", "policy_protected",
                   call("scene", "apply", {"entities": {protected_helper: {"state": "on"}}}))
    await m.denied("scene_create_snapshot", "policy_protected",
                   call("scene", "create", {"scene_id": "x", "snapshot_entities": [protected_helper]}))
    await m.denied("scene_apply_templated", "policy_protected_unverifiable",
                   call("scene", "apply", {"entities": "{{ states }}"}))

    def config(*steps: Dict[str, Any]):
        async def body(guard: Any) -> Any:
            return await guard.create_automation("selftest", {"alias": "x", "trigger": [], "action": list(steps)})
        return body

    await m.denied("automation_data_template", "policy_protected_unverifiable",
                   config({"service": "input_boolean.turn_on", "data_template": {"entity_id": protected_helper}}))
    await m.denied("automation_templated_data", "templated_service",
                   config({"service": "homeassistant.turn_on", "data": {"entity_id": "{{ x }}"}}))
    await m.denied("automation_templated_script_data", "policy_protected_unverifiable",
                   config({"service": "script.turn_on", "data": {"entity_id": "{{ x }}"}}))
    await m.denied("automation_string_target", "policy_protected_unverifiable",
                   config({"service": "script.turn_on", "target": "{{ t }}"}))
    await m.denied("automation_device_action", "policy_protected_unverifiable",
                   config({"domain": "input_boolean", "type": "turn_on", "device_id": "x", "entity_id": "y"}))
    await m.denied("automation_scene_step", "policy_protected_unverifiable", config({"scene": "scene.selftest"}))
    await m.denied("automation_event_step", "policy_protected_unverifiable", config({"event": "x"}))
    await m.denied("automation_all_with_area", "policy_protected_unverifiable",
                   config({"service": "homeassistant.turn_off", "target": {"entity_id": "all"}}))
    await m.allowed("automation_plain_light", config({"service": "light.turn_on", "target": {"entity_id": "light.x"}}))

    await m.denied("group_set_adds_a_protected_id", "policy_protected",
                   call("group", "set", {"object_id": "x", "entities": ["light.selftest_lamp", _PROTECTED_HELPER]}))
    await m.denied("group_set_add_entities", "policy_protected",
                   call("group", "set", {"object_id": "x", "add_entities": _PROTECTED_HELPER}))
    await m.denied("group_target_with_a_protected_member", "policy_protected",
                   call("homeassistant", "turn_off", {"entity_id": "group.selftest_bad"}))
    await m.denied("nested_group_target", "policy_protected", call("homeassistant", "turn_off", {"entity_id": "group.selftest_nested"}))
    await m.denied("group_target_not_in_the_cache", "policy_protected_unverifiable",
                   call("homeassistant", "turn_off", {"entity_id": "group.selftest_unknown"}))
    await m.denied("scene_turn_on_with_a_protected_member", "policy_protected",
                   call("scene", "turn_on", {"entity_id": "scene.selftest_bad"}))
    await m.denied("homeassistant_turn_on_a_protected_scene", "policy_protected",
                   call("homeassistant", "turn_on", {"entity_id": "scene.selftest_bad"}))
    await m.denied("scene_not_in_the_cache", "policy_protected_unverifiable",
                   call("scene", "turn_on", {"entity_id": "scene.selftest_unknown"}))
    await m.denied("scene_apply_lists_a_bad_group", "policy_protected",
                   call("scene", "apply", {"entities": {"group.selftest_bad": {}}}))
    await m.denied("scene_create_snapshots_a_bad_scene", "policy_protected",
                   call("scene", "create", {"scene_id": "x", "snapshot_entities": ["scene.selftest_bad"]}))
    await m.denied("group_set_adds_a_bad_group", "policy_protected",
                   call("group", "set", {"object_id": "x", "add_entities": ["group.selftest_bad"]}))
    await m.denied("reload_lookalike_is_not_exempt", "policy_protected_unverifiable",
                   call("input_boolean", "reload_all_the_things", {}))
    await m.allowed("input_boolean_reload", call("input_boolean", "reload", {}))
    await m.allowed("scene_turn_on_plain", call("scene", "turn_on", {"entity_id": "scene.selftest_ok"}))
    await m.allowed("scene_turn_on_integration_scene", call("scene", "turn_on", {"entity_id": "scene.selftest_integration"}))
    await m.allowed("group_target_plain", call("homeassistant", "turn_off", {"entity_id": "group.selftest_ok"}))
    await m.allowed("automation_wait_with_timeout", config(
        {"wait_template": "{{ is_state('light.x', 'on') }}", "timeout": 30, "continue_on_timeout": True},
        {"condition": "state", "entity_id": "light.x", "state": "on"},
        {"stop": "done", "error": False},
        {"service": "light.turn_on", "alias": "{{ now() }}", "target": {"entity_id": "light.x"}}))
    await m.allowed("automation_script_variables_template", config(
        {"service": "script.turn_on", "target": {"entity_id": "script.good_night"},
         "data": {"variables": {"when": "{{ now() }}"}}}))
    await m.denied("automation_rewrites_a_protected_automation_by_alias", "policy_protected",
                   lambda guard: guard.create_automation("fresh_id", {"alias": "Selftest Kill Switch", "trigger": [], "action": []}))        if _alias_extra() else m.record("automation_rewrites_a_protected_automation_by_alias", True)
    if _extras_name("automation"):
        for verb in ("turn_off", "turn_on", "toggle", "trigger"):
            await m.denied(f"automation_{verb}_all", "policy_protected_unverifiable",
                           call("automation", verb, {"entity_id": "all"}))
        await m.denied("automation_area_target", "policy_protected_unverifiable",
                       call("automation", "turn_off", {"area_id": "x"}))
        await m.denied("automation_device_target", "policy_protected_unverifiable",
                       call("automation", "turn_off", {"device_id": "x"}))
        await m.denied("automation_label_target", "policy_protected_unverifiable",
                       call("automation", "turn_off", {"label_id": "x"}))
        await m.allowed("automation_reload", call("automation", "reload", {}))
    await m.allowed("other_room_helper", call(
        "input_boolean", "turn_on", {"entity_id": f"input_boolean.{_SELFTEST_OTHER}_leave_lights_on"}))
    await m.allowed("light", call("light", "turn_on", {"entity_id": "light.selftest_lamp"}))
    await m.allowed("unprotected_script", call("script", "turn_on", {"entity_id": "script.good_night"}))

    for glob in mp.policy_extras():
        sample = _sample_for(glob)
        await m.denied(
            f"extra_{glob}", "policy_protected",
            call(sample.split(".", 1)[0], "turn_on", {"entity_id": sample}),
        )
        if sample.startswith("automation."):
            object_id = sample.split(".", 1)[1]
            await m.denied(f"extra_{glob}_rewrite", "policy_protected", lambda guard, i=object_id: guard.create_automation(
                               i, {"alias": "x", "trigger": [], "action": []}))
            await m.denied(f"extra_{glob}_delete", "policy_protected",
                           lambda guard, i=object_id: guard.delete_automation(i))


def _extras_name(domain: str) -> bool:
    return mp._extras_name_domain(mp.policy_extras(), domain)


def _alias_extra() -> bool:
    """Whether an extra names exactly the automation the self-test's alias would give."""
    return "automation.selftest_kill_switch" in mp.policy_extras()


async def _router_cases(m: _Matrix) -> None:
    m.router_ran = True
    den = _SELFTEST_ROOM
    fields = {"area": den, "caller_role": CALLER_ROLE, "kind": "hold_on", "set_by": SET_BY, "apply_now": False}

    def run(script: str = SET_SCRIPT, grant: Any = None, body: Optional[Mapping[str, Any]] = None):
        async def go(guard: Any) -> Any:
            return await guard.run_policy_script(
                script, dict(fields if body is None else body), grant=_GRANT if grant is None else grant)
        return go

    await m.allowed("owner_trusted", run(), trusted_voice=True)
    await m.allowed("owner_untrusted", run(), trusted_voice=False)
    await m.allowed("degraded_as_today", run(), permissions=mp.degraded_permissions(), mode="owner")

    inner = _RecordingInner()
    try:
        await mp.PermissionEnforcingHAClient(inner).run_policy_script(SET_SCRIPT, dict(fields), grant=_GRANT)
        m.record("no_scope_as_today", len(inner.calls) == 1)
    except mp.HAWritePermissionDenied:
        m.record("no_scope_as_today", False)

    await m.denied("foreign_grant", "policy_grant", run(grant=object()))
    await m.denied("unlisted_script", "policy_script", run(script="good_night"))
    await m.denied("read_only", "read_only_scope", run(), read_only=True)
    await m.denied("guest", "guest", run(), permissions={"mode": "guest"}, mode="guest")
    await m.denied("guest_role", "policy_caller_role", run(body={**fields, "caller_role": "guest"}))
    await m.denied("restricted_helper", "entity_or_domain_denied", run(), permissions={
        "mode": "owner_restricted", "restricted_entities": [r".*_leave_lights_on$"], "allowed_domains": []})
    await m.denied("restricted_motion_disable", "entity_or_domain_denied", run(body={**fields, "kind": "disable"}), permissions={
        "mode": "owner_restricted", "restricted_entities": [r".*_motion_disable$"], "allowed_domains": []})
    await m.denied("area_not_a_policy_room", "policy_area", run(body={**fields, "area": _SELFTEST_OTHER}))
    await m.denied("area_not_a_slug", "policy_fields", run(body={**fields, "area": "den room"}))
    await m.denied("field_until", "policy_fields", run(body={**fields, "until": "2099-01-01T00:00:00+00:00"}))
    await m.denied("field_caller_area", "policy_fields", run(body={**fields, "caller_area": _SELFTEST_OTHER}))
    await m.denied("field_unknown", "policy_fields", run(body={**fields, "entity_id": "light.x"}))
    await m.denied("kind_unlisted", "policy_fields", run(body={**fields, "kind": "max_brightness"}))
    await m.denied("set_by_other", "policy_fields", run(body={**fields, "set_by": "button"}))
    await m.denied("apply_now_true", "policy_fields", run(body={**fields, "apply_now": True}))
    await m.denied("duration_out_of_range", "policy_fields", run(body={**fields, "duration_minutes": 481}))
    await m.denied("duration_on_disable", "policy_fields", run(body={**fields, "kind": "disable", "duration_minutes": 30}))
    await m.denied("capture_without_level", "policy_fields", run(body={**fields, "kind": "capture"}))
    await m.denied("clear_extra_field", "policy_fields", run(
        script=CLEAR_SCRIPT, body={"area": den, "caller_role": CALLER_ROLE, "kind": "hold_on"}))
    await m.allowed("clear_capture", run(
        script=CLEAR_SCRIPT, body={"area": den, "caller_role": CALLER_ROLE, "clear_capture": True}))
    await m.denied("good_night_degraded", "entity_or_domain_denied", run(
        script=GOOD_NIGHT_SCRIPT, body={"area": den, "caller_role": CALLER_ROLE}),
        permissions=mp.degraded_permissions(), mode="owner")

    previous = mp._policy_room_source
    mp.set_policy_room_source(None)
    try:
        await m.denied("run_without_source", "policy_source_missing", run())
    finally:
        mp.set_policy_room_source(previous)
    mp.set_policy_room_source(lambda: None)
    try:
        await m.denied("run_with_cold_cache", "policy_area", run())
    finally:
        mp.set_policy_room_source(previous)

    async def through_apply(fields_out: Dict[str, Any]) -> bool:
        inner = _RecordingInner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with _scope_for({"mode": "owner"}, "owner"):
            outcome = await apply(PolicyRequest("hold_on", den, "den"), guard, policy_slugs={den})
            refused = await apply(PolicyRequest("hold_on", "whole_house", "house"), guard, policy_slugs={den})
            unknown = await apply(PolicyRequest("hold_on", "elsewhere", "elsewhere"), guard, policy_slugs={den})
        sent = inner.calls[0][2] if inner.calls else {}
        fields_out.update(sent)
        return (
            outcome.ok and NO_END_PHRASE in outcome.speech and len(inner.calls) == 1
            and refused.reason == "whole_house" and unknown.reason == "unknown_area"
        )

    async def bad_answer(response: Mapping[str, Any]) -> bool:
        guard = mp.PermissionEnforcingHAClient(_RecordingInner(response))
        with _scope_for({"mode": "owner"}, "owner"):
            outcome = await apply(PolicyRequest("hold_on", den, "den"), guard, policy_slugs={den})
        return outcome.reason == "bad_response" and not outcome.ok and NO_END_PHRASE not in outcome.speech

    good = dict(_SELFTEST_RESPONSE)
    m.record("answer_empty_is_not_confirmed", await bad_answer({}))
    m.record("answer_wrong_area_is_not_confirmed", await bad_answer({**good, "area": _SELFTEST_OTHER}))
    m.record("answer_until_not_text_is_not_confirmed", await bad_answer({**good, "until": 5}))
    m.record("answer_missing_key_is_not_confirmed", await bad_answer({k: v for k, v in good.items() if k != "stale"}))
    for key in ("brightness_hold", "set_by", "policy"):
        m.record(f"answer_without_{key}_is_not_confirmed", await bad_answer({k: v for k, v in good.items() if k != key}))
    for label, until in (("null", None), ("naive", "2026-01-01T10:00:00"), ("date_only", "2026-01-01")):
        m.record(f"answer_until_{label}_is_not_confirmed", await bad_answer({**good, "until": until}))

    sent: Dict[str, Any] = {}
    m.record("apply_no_duration_no_end", await through_apply(sent) and "duration_minutes" not in sent
             and sent.get("caller_role") == CALLER_ROLE and sent.get("apply_now") is False)


def _protection_cases(m: _Matrix) -> None:
    """Which slugs are protected, whatever the routing says (``protected_slugs``)."""
    global _engine_seen
    den = _SELFTEST_ROOM
    up = {
        f"input_text.{den}_policy": {"attributes": {AREA_ATTRIBUTE: "x"}},
        f"input_boolean.{den}_motion_disable": {}, _SET_ENTITY: {},
    }
    no_script = {k: v for k, v in up.items() if k != _SET_ENTITY}
    no_area = {**up, f"input_text.{den}_policy": {"attributes": {}}}
    saved = _engine_seen
    try:
        _engine_seen = False
        m.record("protection_before_the_engine_is_published", protected_slugs(no_script) == frozenset())
        m.record("protection_when_the_engine_is_first_seen", protected_slugs(up) == {den})
        m.record("protection_survives_the_script_going_missing", protected_slugs(no_script) == {den})
        m.record("protection_ignores_a_blank_area", protected_slugs(no_area) == {den})
        m.record("routing_needs_the_script_and_the_area",
                 policy_slugs(no_script) == frozenset() and policy_slugs(no_area) == frozenset())
        m.record("a_cache_that_lost_its_booleans_after_the_engine_is_cold", protected_slugs({_SET_ENTITY: {}}) is None)
        m.record("an_empty_or_missing_cache_is_cold", protected_slugs({}) is None and protected_slugs(None) is None)
        _engine_seen = False
        m.record("no_booleans_and_no_engine_is_inactive", protected_slugs({"light.x": {}}) == frozenset())
        m.record("the_engine_is_seen_before_the_cold_check", protected_slugs({_SET_ENTITY: {}}) is None and _engine_seen)
        m.record("protection_comes_from_the_markers_alone_once_seen", protected_slugs(no_script) == {den})
        _engine_seen = False
        m.record("a_boolean_less_cache_pre_engine_leaves_writes_alone", not mp.policy_active(protected_slugs({"light.x": {}}), ()))
    finally:
        _engine_seen = saved


async def _run_matrix() -> _Matrix:
    matrix = _Matrix()
    previous = mp._policy_room_source
    mp.set_policy_room_source(lambda: frozenset({_SELFTEST_ROOM}))
    previous_states = mp._policy_states_source
    mp.set_policy_states_source(lambda: _SELFTEST_STATES)
    try:
        await _boundary_cases(matrix)
        await _router_cases(matrix)
        _protection_cases(matrix)
    finally:
        mp.set_policy_room_source(previous)
        mp.set_policy_states_source(previous_states)
    return matrix


def run_selftest(expect_extras: Optional[int] = None, out: Any = None) -> int:
    """Print the F6 line and return the exit status: 0 only when no case failed
    and, when ``expect_extras`` is given, exactly that many extras are parsed."""
    stream = out if out is not None else sys.stdout
    matrix = asyncio.run(_run_matrix())
    failures = [name for name, passed in matrix.results if not passed]
    extras = len(mp.policy_extras())
    print(SELFTEST_LINE.format(
        boundary=int(matrix.boundary_ran), router=int(matrix.router_ran), extras=extras,
        cases=len(matrix.results), failures=len(failures),
    ), file=stream)
    for name in failures:
        print(f"failed: {name}", file=sys.stderr)
    return 0 if not failures and (expect_extras is None or extras == expect_extras) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.room_policy", description=__doc__.split("\n\n")[0])
    parser.add_argument("--selftest", action="store_true", help="run the guard's matrix and print one result line")
    parser.add_argument("--expect-extras", type=int, metavar="N", help="also require N globs in HA_POLICY_PROTECTED_EXTRA")
    args = parser.parse_args(argv)
    if not args.selftest:
        parser.print_usage(sys.stderr)
        return 2
    # The guard logs every denial it is about to be shown making; the one line
    # a deploy reads must not be buried in them.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    return run_selftest(args.expect_extras)


if __name__ == "__main__":
    # Run the importable module's main, not this copy: the guard checks the
    # grant by identity against orchestrator.room_policy's, and `-m` makes this
    # file a second module with its own.
    from orchestrator.room_policy import main as _main

    sys.exit(_main())
