"""The room-policy deny boundary: policy rooms' helpers and the ``room_policy_*``
scripts can be written only through ``run_policy_script``, and that method
authorizes a caller exactly as the direct writes it replaces would have been.

The fictional house has one policy room, ``den`` (and an ordinary one,
``attic``). The guard is the real ``PermissionEnforcingHAClient`` around a
recording inner client."""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# rp.main(["--selftest"]) silences structlog for the process it runs in.
pytestmark = pytest.mark.usefixtures("isolated_structlog")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import shared.config  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_policy as rp  # noqa: E402
DEN = "den"
EXTRAS = "script.room_lighting_*,input_boolean.*_lighting_v2"
OWNER = {"mode": "owner"}
GUEST = {"mode": "guest"}
RESTRICTED = {"mode": "owner_restricted", "restricted_entities": [r".*_leave_lights_on$"], "allowed_domains": []}
WRITES = [f"input_boolean.{DEN}_leave_lights_on", f"input_boolean.{DEN}_motion_disable"]
FIELDS = {"area": DEN, "caller_role": "owner", "kind": "hold_on", "set_by": "voice", "apply_now": False}
RESPONSE = {"area": DEN, "policy": "hold_on", "brightness_hold": False, "until": "", "until_spoken": "",
            "set_by": "voice", "stale": False}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _policy_world(monkeypatch):
    """Policy room ``den``; no extras unless a test sets them."""
    monkeypatch.delenv("HA_POLICY_PROTECTED_EXTRA", raising=False)
    shared.config._clear_cache_for_tests()
    mp.set_policy_room_source(lambda: frozenset({DEN}))
    yield
    shared.config._clear_cache_for_tests()


def set_extras(monkeypatch, value=EXTRAS):
    monkeypatch.setenv("HA_POLICY_PROTECTED_EXTRA", value)
    shared.config._clear_cache_for_tests()


def make_guard():
    inner = MagicMock()
    inner.call_service = AsyncMock(return_value={})
    inner.call_service_response = AsyncMock(return_value=dict(RESPONSE))
    inner.create_automation = AsyncMock(return_value=True)
    inner.delete_automation = AsyncMock(return_value=True)
    inner.disable_automation = AsyncMock(return_value=True)
    inner.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
    return mp.PermissionEnforcingHAClient(inner), inner


def reached(inner):
    return (
        inner.call_service.await_count + inner.call_service_response.await_count
        + inner.create_automation.await_count + inner.delete_automation.await_count
        + inner.disable_automation.await_count
    )


def attempt(call, *, permissions=OWNER, mode="owner", **scope):
    """Run ``call(guard)`` in a fresh scope; ``(denial reason or None, inner)``."""
    guard, inner = make_guard()

    async def go():
        try:
            await call(guard)
        except mp.HAWritePermissionDenied as exc:
            return exc.reason
        return None

    with mp.ha_permission_scope(permissions, mode=mode, **scope):
        return run(go()), inner


def service(domain, name, data=None):
    return lambda guard: guard.call_service(domain, name, data)


def sequence_step(entity_id):
    def call(guard):
        async def go():
            decision = mp.authorize_sequence([{"action": "turn_on", "target": {"entity_id": entity_id}}], OWNER)
            if not decision.allowed:
                raise mp.HAWritePermissionDenied("sequence", reason=decision.reason)
        return go()
    return call


def automation_calling(entity_service):
    return lambda guard: guard.create_automation(
        "x", {"alias": "x", "trigger": [], "action": [{"service": entity_service, "data": {}}]})


# (name, call, expected denial reason or None for allowed)
CASES = [
    ("script_room_policy_set_by_name", service("script", "room_policy_set", {"area": DEN}), mp.POLICY_PROTECTED),
    ("script_room_policy_clear_by_name", service("script", "room_policy_clear", {}), mp.POLICY_PROTECTED),
    ("turn_on_string", service("script", "turn_on", {"entity_id": "script.room_policy_set"}), mp.POLICY_PROTECTED),
    ("turn_on_comma", service("script", "turn_on", {"entity_id": "script.good_night, script.room_policy_set"}),
     mp.POLICY_PROTECTED),
    ("turn_on_list", service("script", "turn_on", {"entity_id": ["script.room_policy_good_night"]}), mp.POLICY_PROTECTED),
    ("turn_on_in_target_block", service("script", "turn_on", {"target": {"entity_id": "script.room_policy_set"}}),
     mp.POLICY_PROTECTED),
    ("turn_on_area", service("script", "turn_on", {"area_id": "x"}), mp.POLICY_PROTECTED_UNVERIFIABLE),
    ("turn_on_no_target", service("script", "turn_on", {}), mp.POLICY_PROTECTED_UNVERIFIABLE),
    ("homeassistant_turn_on_mixed",
     service("homeassistant", "turn_on", {"entity_id": ["light.den", "script.room_policy_clear"]}), mp.POLICY_PROTECTED),
    ("helper_boolean", service("input_boolean", "turn_on", {"entity_id": f"input_boolean.{DEN}_leave_lights_on"}),
     mp.POLICY_PROTECTED),
    ("helper_number", service("input_number", "set_value", {"entity_id": f"input_number.{DEN}_override_brightness", "value": 3}),
     mp.POLICY_PROTECTED),
    ("helper_text_policy", service("input_text", "set_value", {"entity_id": f"input_text.{DEN}_policy", "value": "x"}),
     mp.POLICY_PROTECTED),
    ("helper_datetime_hold_until",
     service("input_datetime", "set_datetime", {"entity_id": f"input_datetime.{DEN}_hold_until"}), mp.POLICY_PROTECTED),
    ("helper_domain_all", service("input_boolean", "turn_off", {"entity_id": "all"}), mp.POLICY_PROTECTED_UNVERIFIABLE),
    ("helper_templated_id", service("homeassistant", "turn_on", {"entity_id": "{{ x }}"}), mp.POLICY_PROTECTED_UNVERIFIABLE),
    ("automation_calls_script", automation_calling("script.room_policy_set"), mp.POLICY_PROTECTED),
    ("automation_turns_on_script", lambda g: g.create_automation("x", {"alias": "x", "trigger": [], "action": [
        {"service": "script.turn_on", "target": {"entity_id": "script.room_policy_set"}}]}), mp.POLICY_PROTECTED),
    ("automation_writes_helper", automation_calling(f"input_boolean.turn_on"), mp.POLICY_PROTECTED_UNVERIFIABLE),
    ("sequence_step_on_helper", sequence_step(f"input_boolean.{DEN}_motion_disable"), mp.POLICY_PROTECTED),
    ("sequence_step_on_script", sequence_step("script.room_policy_set"), mp.POLICY_PROTECTED),
    ("other_room_helper", service("input_boolean", "turn_on", {"entity_id": "input_boolean.attic_leave_lights_on"}), None),
    ("light", service("light", "turn_on", {"entity_id": "light.den"}), None),
    ("script_good_night", service("script", "turn_on", {"entity_id": "script.good_night"}), None),
    ("script_reload", service("script", "reload", {}), None),
    ("scene", service("scene", "turn_on", {"entity_id": "scene.movie"}), None),
    ("sequence_step_on_light", sequence_step("light.den"), None),
]


def test_the_matrix_has_its_floor():
    assert len(CASES) >= 20
    assert any(expected is None for *_, expected in CASES) and any(expected for *_, expected in CASES)


@pytest.mark.parametrize("name,call,expected", CASES, ids=[c[0] for c in CASES])
def test_the_general_path_is_denied_or_allowed(name, call, expected):
    reason, inner = attempt(call)
    if expected is None:
        assert reason is None, name
        assert reached(inner) == 1 or name.startswith("sequence")
    else:
        assert reason == expected, name
        assert reached(inner) == 0, name


def test_script_room_policy_set_denied_for_every_mode():
    for permissions, mode in ((OWNER, "owner"), (mp.degraded_permissions(), "owner"), (GUEST, "guest")):
        reason, inner = attempt(service("script", "room_policy_set", {}), permissions=permissions, mode=mode)
        assert reason is not None and reached(inner) == 0


def test_a_denial_is_recorded_and_halts_the_scope():
    guard, inner = make_guard()
    with mp.ha_permission_scope(OWNER, mode="owner") as scope:
        with pytest.raises(mp.HAWritePermissionDenied):
            run(guard.call_service("script", "room_policy_set", {}))
        assert scope.halted and scope.denials[-1].reason == mp.POLICY_PROTECTED
        with pytest.raises(mp.HAWritePermissionDenied) as second:
            run(guard.call_service("light", "turn_on", {"entity_id": "light.den"}))
        assert second.value.reason == "halted_after_denial"


def test_no_source_refuses_an_input_write_and_only_that():
    mp.set_policy_room_source(None)
    reason, inner = attempt(service("input_boolean", "turn_on", {"entity_id": "input_boolean.attic_leave_lights_on"}))
    assert reason == mp.POLICY_SOURCE_MISSING and reached(inner) == 0
    reason, inner = attempt(service("light", "turn_on", {"entity_id": "light.den"}))
    assert reason is None


def test_a_cold_entity_cache_fails_closed_for_every_slug():
    mp.set_policy_room_source(lambda: None)
    for entity in ("input_boolean.attic_leave_lights_on", "input_number.anything_hold_brightness"):
        reason, _ = attempt(service(entity.split(".")[0], "turn_on", {"entity_id": entity}))
        assert reason == mp.POLICY_PROTECTED, entity
    reason, _ = attempt(service("input_boolean", "turn_on", {"entity_id": "input_boolean.coffee_maker"}))
    assert reason is None, "a helper that isn't the shape of a policy helper stays writable"


def test_a_source_that_raises_fails_closed():
    def boom():
        raise RuntimeError("cache gone")

    mp.set_policy_room_source(boom)
    reason, _ = attempt(service("input_boolean", "turn_on", {"entity_id": f"input_boolean.{DEN}_leave_lights_on"}))
    assert reason == mp.POLICY_PROTECTED


def test_extras_are_protected_and_nothing_else_is(monkeypatch):
    set_extras(monkeypatch)
    for domain, name, entity in (
        ("script", "turn_on", "script.room_lighting_set"),
        ("input_boolean", "turn_on", "input_boolean.attic_lighting_v2"),
    ):
        reason, inner = attempt(service(domain, name, {"entity_id": entity}))
        assert reason == mp.POLICY_PROTECTED and reached(inner) == 0, entity
    reason, _ = attempt(service("script", "room_lighting_x", {}))
    assert reason == mp.POLICY_PROTECTED
    reason, _ = attempt(service("input_boolean", "turn_on", {"entity_id": "input_boolean.attic_leave_lights_on"}))
    assert reason is None


def test_an_extra_naming_a_domain_makes_domain_wide_input_writes_unverifiable(monkeypatch):
    mp.set_policy_room_source(lambda: frozenset())
    reason, _ = attempt(service("input_boolean", "turn_off", {"entity_id": "all"}))
    assert reason is None, "no policy room and no extra: nothing to hide"
    set_extras(monkeypatch)
    reason, _ = attempt(service("input_boolean", "turn_off", {"entity_id": "all"}))
    assert reason == mp.POLICY_PROTECTED_UNVERIFIABLE


def test_a_deleted_or_disabled_extra_automation_is_protected(monkeypatch):
    set_extras(monkeypatch, "automation.keep_*")
    for method in ("delete_automation", "disable_automation"):
        reason, inner = attempt(lambda g, m=method: getattr(g, m)("keep_this"))
        assert reason == mp.POLICY_PROTECTED and reached(inner) == 0, method
        reason, _ = attempt(lambda g, m=method: getattr(g, m)("other"))
        assert reason is None, method


def test_policy_protection_is_pure():
    assert mp.policy_protection("light", "turn_on", {"entity_id": "light.a"}, frozenset({"den"}), ()) is None
    assert mp.policy_protection(
        "input_boolean", "turn_on", {"entity_id": "INPUT_BOOLEAN.Den_Leave_Lights_On"}, frozenset({"den"}), ()
    ) == mp.POLICY_PROTECTED, "ids are compared lowercased"
    assert mp.policy_protection(
        "input_boolean", "turn_on", {"entity_id": "input_boolean.denver_leave_lights_on"}, frozenset({"den"}), ()
    ) is None, "a longer slug is another room"


def test_the_guard_runs_the_policy_check_at_every_write_entry():
    """Drift guard: each write entry of the guard (and each pre-authorizer)
    reaches the policy check; a new write method must be added here."""
    coroutines = {
        name for name, member in inspect.getmembers(mp.PermissionEnforcingHAClient, predicate=inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert coroutines == {
        "call_service", "create_automation", "delete_automation", "disable_automation", "run_policy_script",
    }
    for name in ("call_service", "delete_automation", "disable_automation"):
        assert "_policy_denial(" in inspect.getsource(getattr(mp.PermissionEnforcingHAClient, name)), name
    assert "authorize_automation_config(" in inspect.getsource(mp.PermissionEnforcingHAClient.create_automation)
    assert "_step_policy_denial(" in inspect.getsource(mp.authorize_automation_config)
    assert "_policy_denial(" in inspect.getsource(mp.authorize_sequence)
    assert "_GRANT" in inspect.getsource(mp.PermissionEnforcingHAClient.run_policy_script)


def test_the_policy_check_is_not_folded_into_authorize_ha_write():
    """If it were, run_policy_script's own check of the helpers it replaces
    would refuse every one of them."""
    assert "policy" not in inspect.getsource(mp.authorize_ha_write).lower()


# ---------------------------------------------------------------------------
# run_policy_script
# ---------------------------------------------------------------------------

def run_script(*, script="room_policy_set", grant=None, equivalent=tuple(WRITES), fields=None, permissions=OWNER,
               mode="owner", **scope):
    def call(guard):
        return guard.run_policy_script(
            script, dict(fields or FIELDS), grant=rp._GRANT if grant is None else grant, equivalent_writes=equivalent)
    return attempt(call, permissions=permissions, mode=mode, **scope)


def test_owner_with_a_trusted_voice_is_allowed():
    reason, inner = run_script(trusted_voice=True)
    assert reason is None
    inner.call_service_response.assert_awaited_once_with("script", "room_policy_set", FIELDS)


def test_owner_without_a_trusted_voice_is_allowed_as_today():
    reason, inner = run_script(trusted_voice=False)
    assert reason is None and inner.call_service_response.await_count == 1


def test_degraded_allowed_as_today():
    """Today's direct helper writes pass in a degraded scope (input_* isn't
    restricted there), so the script does too."""
    reason, inner = run_script(permissions=mp.degraded_permissions(), mode="owner")
    assert reason is None and inner.call_service_response.await_count == 1


def test_no_scope_is_allowed_as_today():
    guard, inner = make_guard()
    assert mp.current_ha_scope() is None
    run(guard.run_policy_script("room_policy_set", dict(FIELDS), grant=rp._GRANT, equivalent_writes=WRITES))
    assert inner.call_service_response.await_count == 1


@pytest.mark.parametrize("script", sorted(rp.POLICY_SCRIPTS))
def test_each_of_the_three_scripts_can_run(script):
    reason, inner = run_script(script=script)
    assert reason is None and inner.call_service_response.await_args.args[:2] == ("script", script)


def test_a_foreign_grant_is_refused():
    reason, inner = run_script(grant=object())
    assert reason == "policy_grant" and reached(inner) == 0


def test_a_script_outside_the_three_is_refused():
    for script in ("good_night", "room_policy_get", "room_policy_schedule_set", "room_lighting_set"):
        reason, inner = run_script(script=script)
        assert reason == "policy_script" and reached(inner) == 0, script


def test_read_only_halted_and_guest_are_refused():
    reason, inner = run_script(read_only=True)
    assert reason == "read_only_scope" and reached(inner) == 0
    reason, inner = run_script(permissions=GUEST, mode="guest")
    assert reason == "guest" and reached(inner) == 0
    reason, inner = run_script(permissions=OWNER, mode="guest")
    assert reason == "guest" and reached(inner) == 0, "the scope's own mode counts too"

    guard, inner = make_guard()
    with mp.ha_permission_scope(OWNER, mode="owner") as scope:
        scope.halted = True
        with pytest.raises(mp.HAWritePermissionDenied) as caught:
            run(guard.run_policy_script("room_policy_set", dict(FIELDS), grant=rp._GRANT, equivalent_writes=WRITES))
    assert caught.value.reason == "halted_after_denial" and reached(inner) == 0


def test_a_caller_role_other_than_owner_is_refused():
    for role in ("guest", "dashboard", "button", "test", None):
        reason, inner = run_script(fields={**FIELDS, "caller_role": role})
        assert reason == "policy_caller_role" and reached(inner) == 0, role


def test_no_equivalent_writes_is_refused():
    reason, inner = run_script(equivalent=())
    assert reason == "entity_or_domain_denied" and reached(inner) == 0


def test_a_restricted_helper_is_refused_with_no_call():
    reason, inner = run_script(permissions=RESTRICTED, mode="owner")
    assert reason == "entity_or_domain_denied" and reached(inner) == 0


def test_good_night_is_authorized_as_the_script_it_replaces():
    good_night = dict(area=DEN, caller_role="owner")
    reason, inner = run_script(script="room_policy_good_night", equivalent=("script.good_night",), fields=good_night)
    assert reason is None and inner.call_service_response.await_count == 1
    reason, inner = run_script(
        script="room_policy_good_night", equivalent=("script.good_night",), fields=good_night,
        permissions=mp.degraded_permissions(), mode="owner")
    assert reason == "entity_or_domain_denied" and reached(inner) == 0, "the degraded floor refuses scripts, as today"


def test_the_inner_error_and_shape_error_reach_the_caller():
    import httpx
    from shared.ha_client import HAResponseShapeError

    guard, inner = make_guard()
    inner.call_service_response.side_effect = HAResponseShapeError("x")
    with mp.ha_permission_scope(OWNER, mode="owner"):
        with pytest.raises(HAResponseShapeError):
            run(guard.run_policy_script("room_policy_set", dict(FIELDS), grant=rp._GRANT, equivalent_writes=WRITES))
    request = httpx.Request("POST", "http://ha/x")
    inner.call_service_response.side_effect = httpx.HTTPStatusError("x", request=request, response=httpx.Response(400, request=request))
    with mp.ha_permission_scope(OWNER, mode="owner"):
        with pytest.raises(httpx.HTTPStatusError):
            run(guard.run_policy_script("room_policy_set", dict(FIELDS), grant=rp._GRANT, equivalent_writes=WRITES))


def test_an_allowed_run_counts_as_a_write():
    guard, _ = make_guard()
    with mp.ha_permission_scope(OWNER, mode="owner") as scope:
        run(guard.run_policy_script("room_policy_set", dict(FIELDS), grant=rp._GRANT, equivalent_writes=WRITES))
        assert scope.allowed_writes == 1 and not scope.denials


def test_the_guard_does_not_expose_the_response_call_directly():
    guard, _ = make_guard()
    with pytest.raises(AttributeError):
        guard.call_service_response


# ---------------------------------------------------------------------------
# policy slugs and the area map
# ---------------------------------------------------------------------------

def state(entity_id, **attributes):
    return {"entity_id": entity_id, "state": "off", "attributes": attributes}


def house_states(slugs=(("den", "area_den"),), *, set_script=True, boolean=True):
    states = {}
    for slug, area in slugs:
        states[f"input_text.{slug}_policy"] = state(f"input_text.{slug}_policy", ha_area=area) if area else state(
            f"input_text.{slug}_policy")
        if boolean:
            states[f"input_boolean.{slug}_motion_disable"] = state(f"input_boolean.{slug}_motion_disable")
    if set_script:
        states["script.room_policy_set"] = state("script.room_policy_set")
    return states


def test_a_slug_is_a_policy_slug_only_with_all_three_markers():
    assert rp.policy_slugs(house_states()) == {"den"}
    assert rp.policy_slugs(house_states(set_script=False)) == frozenset()
    assert rp.policy_slugs(house_states(boolean=False)) == frozenset()
    assert rp.policy_slugs(house_states((("den", None),))) == frozenset(), "no ha_area attribute"
    assert rp.policy_slugs(house_states((("den", "  "),))) == frozenset(), "a blank ha_area"
    assert rp.policy_slugs({}) == frozenset() and rp.policy_slugs(None) == frozenset()


def test_an_area_two_slugs_claim_is_dropped_from_the_map():
    states = house_states((("a", "area_x"), ("b", "area_x"), ("c", "area_c")))
    assert rp.policy_slugs(states) == {"a", "b", "c"}
    assert dict(rp.policy_map(states)) == {"area_c": "c"}


def test_the_probe_marker_survives_the_real_attribute_shape():
    states = house_states((("den", "area_den"),))
    assert dict(rp.policy_map(states)) == {"area_den": "den"}


# ---------------------------------------------------------------------------
# the self-test and its line
# ---------------------------------------------------------------------------

F6 = re.compile(r"^room_policy selftest boundary=1 router=[01] extras=\d+ cases=\d+ failures=\d+$")


def selftest(capsys, *args):
    code = rp.main(["--selftest", *args])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("room_policy selftest")]
    assert len(lines) == 1, "exactly one result line (other tests may have cached a logger that still prints)"
    return code, lines[0].strip()


def test_the_selftest_prints_the_line_and_passes(capsys):
    code, line = selftest(capsys)
    assert code == 0 and F6.match(line), line
    assert "router=1 extras=0" in line and "failures=0" in line
    assert int(re.search(r"cases=(\d+)", line).group(1)) >= 20


def test_the_selftest_counts_the_extras_and_checks_each(monkeypatch, capsys):
    set_extras(monkeypatch)
    code, line = selftest(capsys, "--expect-extras", "2")
    assert code == 0 and "extras=2" in line and "failures=0" in line, line
    code, line = selftest(capsys, "--expect-extras", "7")
    assert code == 1 and "extras=2" in line


def test_the_selftest_fails_when_the_boundary_is_broken(monkeypatch, capsys):
    monkeypatch.setattr(mp, "policy_protection", lambda *a, **k: None)
    code, line = selftest(capsys)
    assert code == 1 and not line.endswith("failures=0"), line


def test_the_selftest_restores_the_source_it_found(capsys):
    marker = lambda: frozenset({"keep"})
    mp.set_policy_room_source(marker)
    selftest(capsys)
    assert mp._policy_room_source is marker


def test_without_selftest_the_module_prints_usage_and_fails(capsys):
    assert rp.main([]) == 2


def test_the_module_runs_as_a_program_and_passes_its_own_selftest():
    """``-m`` makes the file a second module; the grant must still match."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])}
    env.pop("HA_POLICY_PROTECTED_EXTRA", None)
    done = subprocess.run(
        [sys.executable, "-m", "orchestrator.room_policy", "--selftest"], capture_output=True, text=True, env=env,
        cwd=ROOT, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert F6.match(done.stdout.strip()) and "failures=0" in done.stdout


def test_the_env_example_audit_does_not_list_the_setting_as_missing():
    done = subprocess.run([sys.executable, "scripts/check-env-example.py"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    missing = done.stdout.split("In example files but never referenced")[0]
    assert "Missing from example files" in missing, "the audit's output changed shape"
    assert "HA_POLICY_PROTECTED_EXTRA" not in missing


def test_the_config_field_is_documented():
    names = ("HA_POLICY_PROTECTED_EXTRA",)
    for relative in (".env.example", "docs/CONFIGURATION.md", "manifests/athena-prod/config.yaml"):
        text = (ROOT / relative).read_text()
        for name in names:
            assert name in text, (relative, name)
