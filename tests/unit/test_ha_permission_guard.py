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
    inner.health_check = AsyncMock(return_value=True)
    inner.close = AsyncMock(return_value=None)
    inner.is_configured = True
    inner.url = "http://ha.local"
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
        assert _run(guard.health_check()) is True
        assert guard.is_configured is True
        assert guard.url == "http://ha.local"
        _run(guard.close())
        inner.close.assert_awaited_once()


class TestRawTransportNotExposed:
    def test_raw_transport_not_exposed(self):
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)
        with pytest.raises(AttributeError):
            guard.client
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
        inner = _make_inner()
        guard = mp.PermissionEnforcingHAClient(inner)

        async def _write():
            with pytest.raises(mp.HAWritePermissionDenied):
                await guard.call_service("lock", "unlock", {"entity_id": "lock.front_door"})

        async def _main():
            with mp.ha_permission_scope(_guest_perms(), mode="guest") as scope:
                await asyncio.gather(_write(), _write())
                task = asyncio.create_task(_write())
                await task
                assert len(scope.denials) == 3

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
