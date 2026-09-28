"""ATHENA-69 Pass B — permission enforcement inside SmartHomeController's
ha_client-accepting methods (D1, D2, D14, D20).

Uses a real SmartHomeController with a fake entity_manager (get_entities /
find_lights_by_room stubbed to return lock.front_door, lock.back_door,
cover.garage_door, fan.office, light.kitchen, media_player.living_room,
script.leaving) and a RAW recording fake ha_client -- the controller's own
`ensure_permission_enforcing(ha_client)` wrap (added at the top of each of
the 13 handler methods) is what does the guarding; the test never wraps the
fake itself.

 - test_guest_denied_handler_call_list — parametrized direct handler
   invocations under a guest scope; full call list and denial count.
 - test_execute_intent_guest_refusal_text — same device types via
   execute_intent.
 - test_execute_intent_partial_refusal_text
 - test_owner_handler_proceeds, test_guest_light_still_allowed
 - test_controller_via_runtime_proxy_single_denial
 - test_unscoped_execute_intent_surfaces_refusal
 - test_scene_permission_denial_does_not_start_fallback

Pass H additions:
 - test_whole_house_restricted_entity_partial_refusal
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock

import httpx

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import pytest

import orchestrator.smart_home_controller as shc
from orchestrator import mode_permission as mp
from orchestrator.nodes import _runtime


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake entity manager
# ---------------------------------------------------------------------------

_ENTITIES = {
    "lock.front_door": {"state": "locked", "attributes": {"friendly_name": "Front Door"}},
    "lock.back_door": {"state": "locked", "attributes": {"friendly_name": "Back Door"}},
    "cover.garage_door": {"state": "closed", "attributes": {"friendly_name": "Garage Door"}},
    "fan.office": {"state": "off", "attributes": {"friendly_name": "Office Fan"}},
    "light.kitchen": {"state": "off", "attributes": {"friendly_name": "Kitchen Light"}},
    "media_player.living_room": {"state": "off", "attributes": {"friendly_name": "Living Room"}},
    "script.leaving": {"state": "off", "attributes": {"friendly_name": "Leaving"}},
}


def _fake_entity_manager():
    em = MagicMock()
    em.get_entities = AsyncMock(return_value=dict(_ENTITIES))

    async def _find_lights_by_room(room_name):
        if room_name and "kitchen" in room_name.lower():
            return [{
                "entity_id": "light.kitchen", "friendly_name": "Kitchen Light",
                "members": [], "state": "off", "type": "individual",
            }]
        return []

    em.find_lights_by_room = AsyncMock(side_effect=_find_lights_by_room)
    return em


def _raw_ha_client():
    """A raw (unwrapped) recording fake -- SmartHomeController's own
    ensure_permission_enforcing wrap is what guards it."""
    client = MagicMock()
    client.call_service = AsyncMock(return_value={"ok": True})
    client.get_state = AsyncMock(return_value={"state": "on"})
    return client


def _controller(entity_manager=None):
    return shc.SmartHomeController(
        entity_manager=entity_manager or _fake_entity_manager(),
        llm_router=MagicMock(),
    )


def _guest_perms(**overrides):
    perms = mp.apply_guest_baseline({"mode": "guest"})
    perms.update(overrides)
    return perms


@pytest.fixture(autouse=True)
def _reset_runtime():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


# ---------------------------------------------------------------------------
# test_guest_denied_handler_call_list
# ---------------------------------------------------------------------------

async def _case_lock_unlock_front(controller, ha_client):
    return await controller._handle_lock_intent("unlock", "front", ha_client)


async def _case_lock_lock_all(controller, ha_client):
    return await controller._handle_lock_intent("lock", None, ha_client)


async def _case_cover_open_garage(controller, ha_client):
    return await controller._handle_cover_intent("open", "garage", ha_client)


async def _case_fan_turn_on_office(controller, ha_client):
    return await controller._handle_fan_intent("turn_on", "office", ha_client)


async def _case_scene_leaving_explicit(controller, ha_client):
    return await controller._handle_scene_intent("activate", {"entity_id": "script.leaving"}, ha_client)


async def _case_scene_goodbye_fallback(controller, ha_client):
    """The scene/script doesn't exist -- exercises the ':3976-3981'
    good-bye fallback (light.turn_off all + lock.lock all), which must
    never fire after the activation attempt is itself denied (D20)."""
    return await controller._handle_scene_intent(
        "activate", {"entity_id": "script.leaving_nonexistent"}, ha_client, original_query="goodbye"
    )


async def _case_scene_goodbye_fallback_scene_domain(controller, ha_client):
    """valerie r1, Low: the D20 fallback-suppression case above only ever
    exercised the `script.` domain branch of _handle_scene_intent (domain
    is chosen from the entity_id's prefix, script vs scene, before the
    denial). A non-existent `scene.` entity takes the sibling branch
    (domain='scene', service='scene.turn_on') -- same fallback-suppression
    guarantee, different domain/service pair reaching authorize_ha_write."""
    return await controller._handle_scene_intent(
        "activate", {"entity_id": "scene.leaving_nonexistent"}, ha_client, original_query="goodbye"
    )


async def _case_bed_warmer(controller, ha_client, monkeypatch):
    monkeypatch.setattr(shc, "get_config", lambda: MagicMock(ha_bed_warmer_entities=(
        '{"level_left": "select.bed_level_left", "level_right": "select.bed_level_right", '
        '"power_main": "switch.bed_power_main", "power_side_a": "switch.bed_power_a", '
        '"power_side_b": "switch.bed_power_b"}'
    )))
    return await controller._handle_bed_warmer_intent("warm_bed", {"side": "both", "level": 3}, ha_client)


async def _case_multi_room_light_restricted(controller, ha_client, monkeypatch):
    """Not a lock (D_execute_multi_room_command only ever writes light
    domain) -- a guest-restricted single light entity proves the guard's
    per-entity floor still binds inside an asyncio.gather fan-out even
    when the domain itself (light) is otherwise allowed for guests."""
    intent = {"device_type": "light", "action": "turn_on"}
    return await controller._execute_multi_room_command(
        ["kitchen"], "turn_on", "group", {}, intent, ha_client, "turn on the kitchen lights"
    )


async def _case_motion_control_input_boolean(controller, ha_client):
    return await controller._handle_motion_control_intent(
        "disable_motion", {}, ha_client, room="office"
    )


# Full expected HA call list per case (valerie r1, Low: previously only
# the script-leaving-goodbye-guest case asserted this; every case now
# does). () for every case denied outright before any inner call; the
# bed-warmer case is the one partial-success case -- switch.turn_on(main
# power) succeeds (switch is guest-baseline-allowed) before both
# select.select_option calls are denied (select isn't).
_HANDLER_CASES = [
    ("lock-unlock-front", _case_lock_unlock_front, False, []),
    ("lock-lock-all", _case_lock_lock_all, False, []),
    ("cover-open-garage", _case_cover_open_garage, False, []),
    ("fan-turn-on-office", _case_fan_turn_on_office, False, []),
    ("scene-leaving-explicit-guest", _case_scene_leaving_explicit, False, []),
    ("script-leaving-goodbye-guest", _case_scene_goodbye_fallback, False, []),
    ("scene-goodbye-fallback-guest", _case_scene_goodbye_fallback_scene_domain, False, []),
    ("bed-warmer-guest", _case_bed_warmer, True, [
        (("switch", "turn_on", {"entity_id": "switch.bed_power_main"}), {}),
    ]),
    ("multi-room-light-restricted-entity-guest", _case_multi_room_light_restricted, True, []),
    ("motion-control-input-boolean-guest", _case_motion_control_input_boolean, False, []),
]


class TestGuestDeniedHandlerCallList:
    @pytest.mark.parametrize(
        "name,case_fn,needs_monkeypatch,expected_calls",
        _HANDLER_CASES,
        ids=[c[0] for c in _HANDLER_CASES],
    )
    def test_guest_denied_handler_call_list(self, name, case_fn, needs_monkeypatch, expected_calls, monkeypatch):
        controller = _controller()
        ha_client = _raw_ha_client()
        perms = _guest_perms()
        if name == "multi-room-light-restricted-entity-guest":
            perms = _guest_perms(restricted_entities=[r"^light\.kitchen"])

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest") as scope:
                if needs_monkeypatch:
                    result = await case_fn(controller, ha_client, monkeypatch)
                else:
                    result = await case_fn(controller, ha_client)
                return result, list(scope.denials)

        result, denials = _run(_drive())
        assert len(denials) >= 1, name
        assert ha_client.call_service.await_args_list == [mock.call(*args, **kwargs) for args, kwargs in expected_calls], name

    def test_call_list_population_floor(self):
        assert len(_HANDLER_CASES) >= 9


# ---------------------------------------------------------------------------
# test_scene_permission_denial_does_not_start_fallback
# ---------------------------------------------------------------------------

class TestScenePermissionDenialDoesNotStartFallback:
    def test_scene_permission_denial_does_not_start_fallback(self):
        """D20: _handle_scene_intent's inner except HAWritePermissionDenied:
        raise must prevent the 'goodbye' fallback (light.turn_off all +
        lock.lock all) from ever running after the activation itself is
        denied -- the guest floor denies scene.turn_on for
        scene.leaving_party, and no light/lock write follows."""
        controller = _controller()
        ha_client = _raw_ha_client()
        perms = _guest_perms()

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest") as scope:
                result = await controller._handle_scene_intent(
                    "activate", {"entity_id": "scene.leaving_party"}, ha_client, original_query="goodbye"
                )
                return result, list(scope.denials)

        result, denials = _run(_drive())
        assert len(denials) == 1
        assert ha_client.call_service.await_args_list == []


# ---------------------------------------------------------------------------
# test_execute_intent_guest_refusal_text / partial
# ---------------------------------------------------------------------------

_EXECUTE_INTENT_CASES = [
    ("lock", {"device_type": "lock", "action": "unlock", "room": "front"}),
    ("cover", {"device_type": "cover", "action": "open", "room": "garage"}),
    ("fan", {"device_type": "fan", "action": "turn_on", "room": "office"}),
    ("scene", {"device_type": "scene", "action": "activate", "parameters": {"entity_id": "script.leaving"}}),
]


class TestExecuteIntentGuestRefusalText:
    @pytest.mark.parametrize("name,intent", _EXECUTE_INTENT_CASES, ids=[c[0] for c in _EXECUTE_INTENT_CASES])
    def test_execute_intent_guest_refusal_text(self, name, intent):
        controller = _controller()
        ha_client = _raw_ha_client()
        perms = _guest_perms(allowed_intents=["control"])

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest"):
                return await controller.execute_intent(dict(intent), ha_client)

        result = _run(_drive())
        assert "guest mode" in result.lower(), (name, result)

    def test_execute_intent_partial_refusal_text(self):
        """bed_warmer's 'both' path writes switch.turn_on (allowed under
        the guest baseline domain list) THEN select.select_option (denied,
        not in the baseline domain list) -- the switch write already
        happened when the denial hits, so the partial-aware refusal
        variant fires."""
        controller = _controller()
        ha_client = _raw_ha_client()
        perms = _guest_perms(allowed_intents=["control"])

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest") as scope:
                import orchestrator.smart_home_controller as shc_mod
                with mock.patch.object(shc_mod, "get_config", return_value=MagicMock(ha_bed_warmer_entities=(
                    '{"level_left": "select.bed_level_left", "level_right": "select.bed_level_right", '
                    '"power_main": "switch.bed_power_main", "power_side_a": "switch.bed_power_a", '
                    '"power_side_b": "switch.bed_power_b"}'
                ))):
                    result = await controller.execute_intent(
                        {"device_type": "bed_warmer", "action": "warm_bed", "parameters": {"side": "both", "level": 3}},
                        ha_client,
                    )
                return result, scope

        result, scope = _run(_drive())
        assert scope.allowed_writes >= 1
        assert "did part of that" in result.lower()
        assert "guest mode" in result.lower()


# ---------------------------------------------------------------------------
# test_owner_handler_proceeds / test_guest_light_still_allowed
# ---------------------------------------------------------------------------

class TestOwnerAndAllowedPaths:
    def test_owner_handler_proceeds(self):
        controller = _controller()
        ha_client = _raw_ha_client()

        async def _drive():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await controller._handle_lock_intent("unlock", "front", ha_client)

        result = _run(_drive())
        assert "unlocked" in result.lower()
        ha_client.call_service.assert_awaited_once_with("lock", "unlock", {"entity_id": "lock.front_door"})

    def test_guest_light_still_allowed(self):
        controller = _controller()
        ha_client = _raw_ha_client()
        perms = _guest_perms()

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest") as scope:
                result = await controller._execute_multi_room_command(
                    ["kitchen"], "turn_on", "group", {}, {"device_type": "light", "action": "turn_on"},
                    ha_client, "turn on the kitchen lights",
                )
                return result, len(scope.denials)

        result, denial_count = _run(_drive())
        assert denial_count == 0
        ha_client.call_service.assert_awaited_once_with("light", "turn_on", {"entity_id": "light.kitchen"})


# ---------------------------------------------------------------------------
# test_controller_via_runtime_proxy_single_denial
# ---------------------------------------------------------------------------

class TestControllerViaRuntimeProxySingleDenial:
    def test_controller_via_runtime_proxy_single_denial(self):
        """ensure_permission_enforcing's identity check (_GUARD_SENTINEL)
        means a _HAClientProxy-shaped object (or any object that already
        forwards to a guard) is never double-wrapped -- a single denial,
        not two, and only one HADenial recorded."""
        real_guard = mp.PermissionEnforcingHAClient(_raw_ha_client())

        class _RuntimeStyleProxy:
            _athena_ha_guard = mp._GUARD_SENTINEL

            def __getattr__(self, name):
                return getattr(real_guard, name)

        proxy = _RuntimeStyleProxy()
        controller = _controller()
        perms = _guest_perms()

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest") as scope:
                result = await controller._handle_lock_intent("unlock", "front", proxy)
                return result, len(scope.denials)

        result, denial_count = _run(_drive())
        assert denial_count == 1


# ---------------------------------------------------------------------------
# test_unscoped_execute_intent_surfaces_refusal
# ---------------------------------------------------------------------------

class TestUnscopedExecuteIntent:
    def test_unscoped_execute_intent_surfaces_refusal(self):
        """D3: execute_intent called with no scope open opens its own
        system-mode (degraded) scope -- a lock write is denied by the D4
        fallback floor even though nothing ever called ha_permission_scope."""
        assert mp.current_ha_scope() is None
        controller = _controller()
        ha_client = _raw_ha_client()

        result = _run(controller.execute_intent(
            {"device_type": "lock", "action": "unlock", "room": "front"}, ha_client
        ))
        assert "couldn't verify permissions" in result.lower() or "right now" in result.lower()
        ha_client.call_service.assert_not_awaited()
        assert mp.current_ha_scope() is None


# ---------------------------------------------------------------------------
# test_whole_house_restricted_entity_partial_refusal (Pass H)
# ---------------------------------------------------------------------------

class TestWholeHouseFanOutDenialSurfacing:
    def test_whole_house_restricted_entity_partial_refusal(self):
        """Medium (codex full-diff, Pass H): _execute_whole_house_command's
        asyncio.gather fan-out previously had no return_exceptions=True, so
        a per-entity guest denial inside an otherwise-allowed domain
        (light) raised HAWritePermissionDenied straight out of the gather
        call, past execute_intent's _finish() wrapper entirely, landing on
        whatever generic "I encountered an error" the caller's own
        try/except produces -- instead of the partial-refusal text naming
        what ran. light.kitchen is allowed; light.owner_bedroom is
        guest-restricted; "turn off all lights" must still turn off the
        kitchen light and report a partial refusal, not crash."""
        em = _fake_entity_manager()
        em.get_all_light_groups = AsyncMock(return_value=[
            {"friendly_name": "Kitchen", "entity_id": "light.kitchen_group", "members": ["light.kitchen"]},
            {"friendly_name": "Owner Bedroom", "entity_id": "light.owner_bedroom_group", "members": ["light.owner_bedroom"]},
        ])
        controller = _controller(entity_manager=em)
        ha_client = _raw_ha_client()
        perms = _guest_perms(allowed_intents=["control"], restricted_entities=[r"^light\.owner_bedroom"])

        async def _drive():
            with mp.ha_permission_scope(perms, mode="guest") as scope:
                result = await controller.execute_intent(
                    {"device_type": "light", "action": "turn_off", "room": "whole_house"},
                    ha_client,
                    original_query="turn off all lights",
                )
                return result, scope

        result, scope = _run(_drive())

        assert scope.allowed_writes >= 1
        assert len(scope.denials) >= 1
        assert "guest mode" in result.lower()
        assert "did part of that" in result.lower()

        call_list = [
            (c.args[0], c.args[1], c.args[2]) for c in ha_client.call_service.await_args_list
        ]
        assert ("light", "turn_off", {"entity_id": "light.kitchen"}) in call_list
        assert not any(c[2].get("entity_id") == "light.owner_bedroom" for c in call_list)


# ---------------------------------------------------------------------------
# Pass H2 (xander delta review, item 4): a genuine (non-permission)
# exception inside the gather fan-out must surface an error reply, not a
# false "Done!".
# ---------------------------------------------------------------------------

class TestFanOutGenuineErrorNotSwallowed:
    def test_whole_house_connect_error_surfaces_as_error_not_done(self):
        """return_exceptions=True alone would silently absorb a real HA
        transport failure (httpx.ConnectError) into "Done! I've turned off
        lights in 2 rooms." -- owner scope, no permission denials in play,
        so the ONLY reason this must not say "Done!" is the connection
        error on light.owner_bedroom."""
        em = _fake_entity_manager()
        em.get_all_light_groups = AsyncMock(return_value=[
            {"friendly_name": "Kitchen", "entity_id": "light.kitchen_group", "members": ["light.kitchen"]},
            {"friendly_name": "Owner Bedroom", "entity_id": "light.owner_bedroom_group", "members": ["light.owner_bedroom"]},
        ])
        controller = _controller(entity_manager=em)
        ha_client = _raw_ha_client()

        async def _flaky_call_service(domain, service, data):
            if data.get("entity_id") == "light.owner_bedroom":
                raise httpx.ConnectError("connection refused")
            return {"ok": True}

        ha_client.call_service = AsyncMock(side_effect=_flaky_call_service)

        async def _drive():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await controller.execute_intent(
                    {"device_type": "light", "action": "turn_off", "room": "whole_house"},
                    ha_client,
                    original_query="turn off all lights",
                )

        result = _run(_drive())

        assert "done" not in result.lower()
        assert "problem" in result.lower()
        call_list = [
            (c.args[0], c.args[1], c.args[2]) for c in ha_client.call_service.await_args_list
        ]
        assert ("light", "turn_off", {"entity_id": "light.kitchen"}) in call_list

    def test_multi_room_connect_error_surfaces_as_error_not_done(self):
        """Same guarantee for _execute_multi_room_command's own gather
        (smart_home_controller.py:4272) -- a genuine transport failure
        must not be reported as success."""
        controller = _controller()
        ha_client = _raw_ha_client()
        ha_client.call_service = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        async def _drive():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await controller._execute_multi_room_command(
                    ["kitchen"], "turn_on", "group", {}, {"device_type": "light", "action": "turn_on"},
                    ha_client, "turn on the kitchen lights",
                )

        result = _run(_drive())

        assert "done" not in result.lower()
        assert "problem" in result.lower()
