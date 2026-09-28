"""Unit tests for the ATHENA-69 HA write authorization guard
(orchestrator.mode_permission's guard module).

Pass A cases (call_service interception + read allowlist only --
automation-method cases land in Pass B):

 - test_authorize_matrix — parametrized authorize_ha_write matrix.
 - test_guard_denied_never_calls_inner
 - test_guard_allowed_forwards
 - test_latch_denies_after_first_denial
 - test_passthrough_reads_allowlist
 - test_raw_transport_not_exposed
 - test_unset_scope_is_baseline
 - test_degraded_permissions_when_mode_service_down
 - test_scope_propagates_into_create_task_and_gather
 - test_scope_propagates_through_langgraph_ainvoke
 - test_scope_resets_after_exit
 - test_concurrent_request_scopes_isolated
 - test_concurrent_unscoped_calls_do_not_share_denials
 - test_ensure_idempotent_none_and_proxy
 - test_fallback_config_invalid_json_uses_default_not_empty
 - test_intent_write_domains_whole_house_is_light_only

Pass B cases (authorize_automation_config, authorize_sequence, and the
guard's create_/delete_/disable_automation interception):

 - test_create_automation_with_lock_action_denied_for_guest
 - test_create_automation_light_only_allowed_when_automation_domain_allowed
 - test_automation_walker_nested_choose_and_data_entity_id
 - test_automation_unknown_step_denied_for_guest
 - test_authorize_sequence_lock_step_denied
 - test_authorize_sequence_unknown_device_type_denied_for_guest
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import pytest

from orchestrator import mode_permission as mp
from shared import config as config_module


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_config_cache():
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


def _guest_perms(**overrides):
    perms = mp.apply_guest_baseline({"mode": "guest"})
    perms.update(overrides)
    return perms


def _owner_perms():
    return {"mode": "owner"}


# ---------------------------------------------------------------------------
# authorize_ha_write matrix
# ---------------------------------------------------------------------------

_MATRIX_CASES = [
    ("lock-unlock-front-guest-deny", "lock", "unlock", {"entity_id": "lock.front_door"}, "guest", False),
    ("cover-open-garage-guest-deny", "cover", "open_cover", {"entity_id": "cover.garage_door"}, "guest", False),
    ("light-turn-on-kitchen-guest-allow", "light", "turn_on", {"entity_id": "light.kitchen"}, "guest", True),
    ("lock-lock-all-guest-deny", "lock", "lock", {"entity_id": "all"}, "guest", False),
    ("light-turn-on-area-guest-allow", "light", "turn_on", {"area_id": "kitchen"}, "guest", True),
    ("lock-unlock-target-list-guest-deny", "lock", "unlock", {"target": {"entity_id": ["lock.a", "lock.b"]}}, "guest", False),
    ("homeassistant-turn-on-cover-entity-guest-deny", "homeassistant", "turn_on", {"entity_id": "cover.garage"}, "guest", False),
    ("homeassistant-turn-on-data-entity-guest-deny", "homeassistant", "turn_on", {"data": {"entity_id": "lock.front"}}, "guest", False),
    ("comma-spaced-mixed-domain-guest-deny", "light", "turn_on", {"entity_id": " light.a , lock.b "}, "guest", False),
    ("bare-id-under-lock-guest-deny", "lock", "unlock", {"entity_id": "front_door"}, "guest", False),
    ("device-id-under-lock-guest-deny", "lock", "lock", {"device_id": "abc123"}, "guest", False),
    ("floor-id-under-lock-guest-deny", "lock", "lock", {"floor_id": "ground"}, "guest", False),
    ("label-id-under-lock-guest-deny", "lock", "lock", {"label_id": "secure"}, "guest", False),
    ("lock-open-no-entity-guest-deny", "lock", "open", None, "guest", False),
    ("mixed-domain-list-guest-deny", "light", "turn_on", {"entity_id": ["light.a", "lock.b"]}, "guest", False),
    ("service-data-none-light-guest-allow", "light", "turn_on", None, "guest", True),
    ("service-data-none-lock-guest-deny", "lock", "lock", None, "guest", False),
    ("scene-turn-on-movie-guest-deny", "scene", "turn_on", {"entity_id": "scene.movie"}, "guest", False),
    ("select-select-option-guest-deny", "select", "select_option", None, "guest", False),
    ("owner-lock-unlock-allow", "lock", "unlock", {"entity_id": "lock.front_door"}, "owner", True),
    ("owner-cover-open-allow", "cover", "open_cover", {"entity_id": "cover.garage_door"}, "owner", True),
    # Mutation-resistance (tessa, Pass A review): the two homeassistant.*
    # cases above target entities already denied by the floor regex on
    # their own, so a mutant that deletes the domain-mismatch companion
    # check (f"{domain}.all") in authorize_ha_write survives against them.
    # This case dispatches an entity that WOULD be allowed on its own
    # (light.kitchen, guest-baseline-allowed) via the generic
    # "homeassistant" domain -- only the companion check denies it, since
    # "homeassistant" itself is never in allowed_domains.
    ("homeassistant-turn-on-light-via-generic-domain-guest-deny", "homeassistant", "turn_on", {"entity_id": "light.kitchen"}, "guest", False),
    ("homeassistant-turn-on-light-via-generic-domain-owner-allow", "homeassistant", "turn_on", {"entity_id": "light.kitchen"}, "owner", True),
]


class TestAuthorizeMatrix:
    @pytest.mark.parametrize(
        "name,domain,service,data,perm_kind,expected_allowed",
        _MATRIX_CASES,
        ids=[c[0] for c in _MATRIX_CASES],
    )
    def test_authorize_matrix(self, name, domain, service, data, perm_kind, expected_allowed):
        perms = _owner_perms() if perm_kind == "owner" else _guest_perms()
        decision = mp.authorize_ha_write(domain, service, data, perms)
        assert decision.allowed is expected_allowed, name

    def test_empty_perms_degraded_lock_deny_light_allow(self):
        lock_decision = mp.authorize_ha_write("lock", "unlock", None, {})
        light_decision = mp.authorize_ha_write("light", "turn_on", None, {})
        assert lock_decision.allowed is False
        assert light_decision.allowed is True


# ---------------------------------------------------------------------------
# PermissionEnforcingHAClient
# ---------------------------------------------------------------------------

def _make_inner():
    inner = MagicMock()
    inner.call_service = AsyncMock(return_value={"ok": True})
    inner.get_state = AsyncMock(return_value={"state": "on"})
    inner.get_states = AsyncMock(return_value=[{"entity_id": "light.kitchen", "state": "on"}])
    inner.health_check = AsyncMock(return_value=True)
    inner.close = AsyncMock(return_value=None)
    inner.is_configured = True
    inner.url = "http://ha.local"  # not exposed through the guard (Pass H)
    inner.headers = {"Authorization": "Bearer super-secret-token"}  # not exposed either
    inner.client = MagicMock()  # the raw authenticated transport
    inner.token = "super-secret-token"
    return inner


class TestGuardDeniedNeverCallsInner:
    def test_guard_denied_never_calls_inner(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope(_guest_perms(), mode="guest") as scope:
            with pytest.raises(mp.HAWritePermissionDenied):
                _run(guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"}))
            assert len(scope.denials) == 1
        inner.call_service.assert_not_awaited()

    def test_counter_incremented_on_denial(self, monkeypatch):
        # prometheus_client is stubbed repo-wide in unit tests (not
        # installed in this environment); assert against a substitute
        # counter object rather than a real REGISTRY sample.
        fake_counter = MagicMock()
        monkeypatch.setattr(mp, "ha_write_denied_total", fake_counter)
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope(_guest_perms(), mode="guest"):
            with pytest.raises(mp.HAWritePermissionDenied):
                _run(guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"}))
        fake_counter.labels.assert_called_once_with(domain="lock", scope_mode="guest")
        fake_counter.labels.return_value.inc.assert_called_once()


class TestGuardAllowedForwards:
    def test_guard_allowed_forwards(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope(_owner_perms(), mode="owner") as scope:
            result = _run(guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"}))
        assert result == {"ok": True}
        inner.call_service.assert_awaited_once_with("lock", "unlock", {"entity_id": "lock.front_door"})
        assert scope.allowed_writes == 1


class TestLatchDeniesAfterFirstDenial:
    def test_latch_denies_after_first_denial(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope(_guest_perms(), mode="guest") as scope:
            with pytest.raises(mp.HAWritePermissionDenied):
                _run(guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"}))
            with pytest.raises(mp.HAWritePermissionDenied) as excinfo:
                _run(guard.call_service("light", "turn_on", {"entity_id": "light.kitchen"}))
            assert scope.denials[-1].reason == "halted_after_denial"
        inner.call_service.assert_not_awaited()


class TestPassthroughReadsAllowlist:
    def test_passthrough_reads_allowlist(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        assert _run(guard.get_state("light.kitchen")) == {"state": "on"}
        assert _run(guard.get_states()) == [{"entity_id": "light.kitchen", "state": "on"}]
        assert _run(guard.health_check()) is True
        assert guard.is_configured is True
        _run(guard.close())
        inner.close.assert_awaited_once()


class TestRawTransportNotExposed:
    def test_raw_transport_not_exposed(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with pytest.raises(AttributeError):
            guard.client
        with pytest.raises(AttributeError):
            guard.url
        with pytest.raises(AttributeError):
            guard.headers
        with pytest.raises(AttributeError):
            guard.token


class TestUnsetScopeIsBaseline:
    def test_unset_scope_is_baseline(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        assert mp.current_ha_scope() is None
        with pytest.raises(mp.HAWritePermissionDenied):
            _run(guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"}))
        inner.call_service.reset_mock()
        result = _run(guard.call_service("media_player", "media_play", {"entity_id": "media_player.living_room"}))
        assert result == {"ok": True}


class TestDegradedPermissionsWhenModeServiceDown:
    def test_degraded_permissions_when_mode_service_down(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        from orchestrator.nodes import _runtime
        _runtime.set_mode_client(client)
        result = _run(mp.get_current_mode())
        assert result["mode"] == "owner"
        assert result["permissions"]["mode"] == "degraded"
        assert result["degraded"] is True

        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope(result["permissions"], mode=result["mode"]):
            with pytest.raises(mp.HAWritePermissionDenied):
                _run(guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"}))


class TestScopePropagation:
    def test_scope_propagates_into_create_task_and_gather(self):
        """Mutation-resistance (tessa, Pass B review): the original version
        of this test used `lock.unlock` for every gathered write, which
        passes even with the D20 latch disabled entirely (every write is
        independently denied by the guest floor regardless of latch
        state). The second write here is an otherwise-ALLOWED
        `light.turn_on` -- it can only be denied because the scope already
        latched after the first (lock) denial, which the reason field
        proves directly."""
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)

        async def _write_lock():
            with pytest.raises(mp.HAWritePermissionDenied):
                await guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"})

        async def _write_light():
            with pytest.raises(mp.HAWritePermissionDenied):
                await guard.call_service("light", "turn_on", {"entity_id": "light.kitchen"})

        async def _main():
            with mp.ha_permission_scope(_guest_perms(), mode="guest") as scope:
                await asyncio.gather(_write_lock(), _write_light())
                task = asyncio.create_task(_write_light())
                await task
                assert len(scope.denials) == 3
                reasons = [d.reason for d in scope.denials]
                assert reasons[0] == "entity_or_domain_denied"
                assert reasons[1] == "halted_after_denial"
                assert reasons[2] == "halted_after_denial"

        _run(_main())

    def test_scope_propagates_through_langgraph_ainvoke(self):
        """Simulates LangGraph's ainvoke pattern: a node coroutine awaited
        directly from within the scope's with-block inherits the scope
        (contextvars propagate through plain awaits, no task boundary
        needed)."""
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)

        async def _node():
            assert mp.current_ha_scope() is not None
            with pytest.raises(mp.HAWritePermissionDenied):
                await guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"})

        async def _graph_ainvoke():
            with mp.ha_permission_scope(_guest_perms(), mode="guest"):
                await _node()

        _run(_graph_ainvoke())

    def test_scope_resets_after_exit(self):
        with mp.ha_permission_scope(_guest_perms(), mode="guest"):
            assert mp.current_ha_scope() is not None
        assert mp.current_ha_scope() is None


class TestConcurrentScopes:
    def test_concurrent_request_scopes_isolated(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)

        async def _guest_write():
            with mp.ha_permission_scope(_guest_perms(), mode="guest") as scope:
                with pytest.raises(mp.HAWritePermissionDenied):
                    await guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"})
                await asyncio.sleep(0)
                return len(scope.denials)

        async def _owner_write():
            with mp.ha_permission_scope(_owner_perms(), mode="owner") as scope:
                await asyncio.sleep(0)
                await guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"})
                return len(scope.denials)

        async def _both():
            return await asyncio.gather(_guest_write(), _owner_write())

        guest_denials, owner_denials = _run(_both())
        assert guest_denials == 1
        assert owner_denials == 0

    def test_concurrent_unscoped_calls_do_not_share_denials(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)

        async def _unscoped_write():
            with pytest.raises(mp.HAWritePermissionDenied):
                await guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"})

        async def _all_three():
            await asyncio.gather(_unscoped_write(), _unscoped_write(), _unscoped_write())

        _run(_all_three())
        # Each call opened its own throwaway baseline scope; nothing here
        # asserts a shared denial count because none is shared by design.
        assert mp.current_ha_scope() is None


class TestEnsurePermissionEnforcing:
    def test_ensure_idempotent_none_and_proxy(self):
        assert mp.ensure_permission_enforcing(None) is None

        guard = mp.PermissionEnforcingHAClient(_make_inner())
        assert mp.ensure_permission_enforcing(guard) is guard

        class _Proxy:
            _athena_ha_guard = mp._GUARD_SENTINEL

            def __getattr__(self, name):
                return getattr(guard, name)

        proxy = _Proxy()
        assert mp.ensure_permission_enforcing(proxy) is proxy

        plain = MagicMock()
        wrapped = mp.ensure_permission_enforcing(plain)
        assert isinstance(wrapped, mp.PermissionEnforcingHAClient)
        assert wrapped is not plain


class TestFallbackConfigInvalidJson:
    def test_fallback_config_invalid_json_uses_default_not_empty(self, monkeypatch):
        monkeypatch.setenv("HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES", "{not valid")
        config_module._clear_cache_for_tests()
        result = mp.degraded_permissions()
        assert result["restricted_entities"] == [
            r"^lock\.", r"^cover\.", r"^alarm_control_panel\.", r"^camera\.",
            r"^automation\.", r"^script\.", r"^scene\.",
        ]


class TestIntentWriteDomains:
    def test_intent_write_domains_whole_house_is_light_only(self):
        assert mp.CONTROL_DEVICE_DOMAINS["whole_house"] == ("light",)

    def test_intent_write_domains_light_intent(self):
        assert mp.intent_write_domains({"device_type": "light"}) == ("light",)

    def test_intent_write_domains_unknown_device_type_is_empty(self):
        assert mp.intent_write_domains({"device_type": "garage_fan_thing"}) == ()

    def test_intent_write_domains_missing_device_type_is_empty(self):
        assert mp.intent_write_domains({}) == ()


# ---------------------------------------------------------------------------
# Pass B: authorize_automation_config, authorize_sequence, guard automation
# methods
# ---------------------------------------------------------------------------

def _isolated_perms(**overrides):
    """A permissions dict that isolates the automation walker's per-step
    checks from the D8 guest blanket floor (which always includes
    ^automation\\. -- so a real guest scope can never pass the outer
    automation.<id> gate regardless of what a create_automation config
    contains). mode is deliberately NOT "guest" (normalize_permissions only
    applies apply_guest_baseline for mode=="guest") and NOT "owner" (which
    bypasses every check trivially) -- just a scope whose restricted_entities/
    allowed_domains are set explicitly by the test.
    """
    perms = {
        "mode": "restricted",
        "restricted_entities": [],
        "allowed_domains": [],
        "allowed_intents": [],
        "restricted_intents": [],
    }
    perms.update(overrides)
    return perms


class TestAuthorizeAutomationConfig:
    def test_create_automation_with_lock_action_denied_for_guest(self):
        """A real guest is denied outright by the automation.<id> floor
        (D8) before the walker even inspects the lock action -- this is
        the production-realistic case."""
        guest = mp.apply_guest_baseline({"mode": "guest"})
        config = {"action": [{"service": "lock.unlock", "target": {"entity_id": "lock.front_door"}}]}
        decision = mp.authorize_automation_config("goodnight", config, guest)
        assert decision.allowed is False

    def test_create_automation_light_only_allowed_when_automation_domain_allowed(self):
        perms = _isolated_perms(allowed_domains=["automation", "light"])
        config = {"action": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
        decision = mp.authorize_automation_config("morning_lights", config, perms)
        assert decision.allowed is True

    def test_automation_walker_nested_choose_allowed_actions(self):
        """Mutation-resistance (tessa, Pass B review): a positive case is
        required alongside the denial case below, because deleting the
        choose[].sequence recursion entirely makes EVERY choose step "an
        unknown step, denied unless owner" -- which would make the denial
        case below pass for the wrong reason (the choose wrapper itself
        being denied, not the nested lock action). This case has NO denied
        action anywhere inside the choose block, so it can only pass if the
        walker actually recurses into choose[].sequence and authorizes each
        nested step on its own merits."""
        perms = _isolated_perms(allowed_domains=["automation", "light"])
        config = {
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [],
                            "sequence": [
                                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}},
                                {"service": "light.turn_off", "target": {"entity_id": "light.office"}},
                            ],
                        }
                    ]
                }
            ]
        }
        decision = mp.authorize_automation_config("nested_allowed", config, perms)
        assert decision.allowed is True

    def test_automation_walker_nested_choose_and_data_entity_id(self):
        """Covers the recursive walk through choose[].sequence and the
        data.entity_id target-normalization path (a lock action nested two
        levels deep, expressed with the data-wrapped shape) -- denied even
        though the top-level automation domain is allowed. Asserts the
        SPECIFIC reason/target (mutation-resistance, tessa Pass B review):
        a mutant that stops recursing into choose[].sequence entirely would
        still deny this config (the un-recursed choose step falls to
        "unknown step"), but with reason == "unknown_automation_step" and
        no denied_targets naming the lock entity -- this assertion
        distinguishes "denied because the nested lock action was correctly
        found and checked" from "denied because the walker gave up."""
        perms = _isolated_perms(allowed_domains=["automation", "light"], restricted_entities=[r"^lock\."])
        config = {
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [],
                            "sequence": [
                                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}},
                                {"service": "lock.unlock", "data": {"entity_id": "lock.front_door"}},
                            ],
                        }
                    ]
                }
            ]
        }
        decision = mp.authorize_automation_config("nested", config, perms)
        assert decision.allowed is False
        assert decision.reason == "entity_or_domain_denied"
        assert "lock.front_door" in decision.denied_targets

    def test_automation_unknown_step_denied_for_guest(self):
        guest = mp.apply_guest_baseline({"mode": "guest"})
        config = {"action": [{"event": "custom_event", "event_data": {}}]}
        decision = mp.authorize_automation_config("custom", config, guest)
        assert decision.allowed is False

    def test_automation_inert_steps_allowed(self):
        """Only steps whose keys are a SUBSET of the inert set count --
        e.g. a bare {"delay": ...} or {"condition": ..., "conditions": ...}
        (the plan's literal contract: the inert set is exactly {delay,
        wait_template, wait_for_trigger, condition, conditions, alias,
        enabled, continue_on_error, stop, variables}; a condition step that
        also carries entity_id/state (real HA syntax) is NOT a subset and
        falls to the unknown-step branch instead)."""
        perms = _isolated_perms(allowed_domains=["automation"])
        config = {"action": [{"delay": {"seconds": 5}}, {"condition": "and", "conditions": []}]}
        decision = mp.authorize_automation_config("delay_only", config, perms)
        assert decision.allowed is True

    def test_automation_device_action_checked_as_domain_all(self):
        perms = _isolated_perms(allowed_domains=["automation"], restricted_entities=[r"^lock\."])
        config = {"action": [{"device_id": "abc123", "domain": "lock", "type": "lock"}]}
        decision = mp.authorize_automation_config("device_action", config, perms)
        assert decision.allowed is False


class TestAuthorizeSequence:
    def test_authorize_sequence_lock_step_denied(self):
        guest = mp.apply_guest_baseline({"mode": "guest"})
        sequence = [{"target": {"entity_id": "lock.front_door"}, "action": "unlock"}]
        decision = mp.authorize_sequence(sequence, guest)
        assert decision.allowed is False

    def test_authorize_sequence_unknown_device_type_denied_for_guest(self):
        guest = mp.apply_guest_baseline({"mode": "guest"})
        sequence = [{"target": {"device_type": "totally_unknown_thing"}, "action": "turn_on"}]
        decision = mp.authorize_sequence(sequence, guest)
        assert decision.allowed is False

    def test_authorize_sequence_owner_unknown_device_type_allowed(self):
        sequence = [{"target": {"device_type": "totally_unknown_thing"}, "action": "turn_on"}]
        decision = mp.authorize_sequence(sequence, {"mode": "owner"})
        assert decision.allowed is True

    def test_authorize_sequence_light_step_allowed_for_guest(self):
        guest = mp.apply_guest_baseline({"mode": "guest"})
        sequence = [{"target": {"entity_id": "light.kitchen"}, "action": "turn_on"}]
        decision = mp.authorize_sequence(sequence, guest)
        assert decision.allowed is True

    def test_authorize_sequence_denies_whole_sequence_on_first_denied_step(self):
        guest = mp.apply_guest_baseline({"mode": "guest"})
        sequence = [
            {"target": {"entity_id": "light.kitchen"}, "action": "turn_on"},
            {"target": {"entity_id": "lock.front_door"}, "action": "unlock"},
            {"target": {"entity_id": "light.office"}, "action": "turn_on"},
        ]
        decision = mp.authorize_sequence(sequence, guest)
        assert decision.allowed is False


class TestGuardAutomationMethods:
    def test_create_automation_denied_never_calls_inner(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        guest = mp.apply_guest_baseline({"mode": "guest"})
        with mp.ha_permission_scope(guest, mode="guest") as scope:
            with pytest.raises(mp.HAWritePermissionDenied):
                _run(guard.create_automation("goodnight", {"action": []}))
            assert len(scope.denials) == 1
        inner.create_automation.assert_not_called()

    def test_create_automation_allowed_forwards(self):
        inner = _make_inner()
        inner.create_automation = AsyncMock(return_value=True)
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            result = _run(guard.create_automation("goodnight", {"action": []}))
        assert result is True
        inner.create_automation.assert_awaited_once_with("goodnight", {"action": []})

    def test_delete_automation_checks_automation_entity(self):
        inner = _make_inner()
        inner.delete_automation = AsyncMock(return_value=True)
        guard = mp.PermissionEnforcingHAClient(inner)
        guest = mp.apply_guest_baseline({"mode": "guest"})
        with mp.ha_permission_scope(guest, mode="guest"):
            with pytest.raises(mp.HAWritePermissionDenied):
                _run(guard.delete_automation("goodnight"))
        inner.delete_automation.assert_not_awaited()

    def test_disable_automation_checks_automation_entity(self):
        inner = _make_inner()
        inner.disable_automation = AsyncMock(return_value=True)
        guard = mp.PermissionEnforcingHAClient(inner)
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            result = _run(guard.disable_automation("goodnight"))
        assert result is True
        inner.disable_automation.assert_awaited_once_with("goodnight")

    def test_ha_client_write_surface_is_intercepted(self):
        """Public async def methods of HomeAssistantClient minus
        {get_state, get_states, health_check, close} equal exactly
        {call_service, create_automation, delete_automation, disable_automation}."""
        import inspect
        from shared.ha_client import HomeAssistantClient

        public_async_methods = {
            name for name, member in inspect.getmembers(HomeAssistantClient, predicate=inspect.iscoroutinefunction)
            if not name.startswith("_")
        }
        write_methods = public_async_methods - {"get_state", "get_states", "health_check", "close"}
        assert write_methods == {"call_service", "create_automation", "delete_automation", "disable_automation"}
        for name in write_methods:
            assert hasattr(mp.PermissionEnforcingHAClient, name), f"guard doesn't intercept {name}"
