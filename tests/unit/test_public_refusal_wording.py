"""Public refusals never mention modes, guests, owners, PINs or sign-in (V2.3).

An anonymous website visitor has no idea what "guest mode" is; each
refusal they can hit reads as a plain "can't do that here". Driven through
the gate and each self-gated node with the public permission set.
Floor 7; named member CONTROL.
"""
from __future__ import annotations

import asyncio
import re
from unittest import mock

import pytest

from . import _public_audience_harness as h
import orchestrator.nodes.route_control as route_control_module
import orchestrator.nodes.route_music as route_music_module
import orchestrator.nodes.route_tv as route_tv_module

I = h.IntentCategory
MODE_WORDS = re.compile(r"guest|owner|\bmode\b|\bpin\b|sign in", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _public():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    return normalize_permissions(public_permissions())


def _state(intent, query):
    state = h.make_state(permissions=_public(), intent=intent, query=query)
    state.session_id = "s1"
    return state


def _no_flags(monkeypatch, module):
    monkeypatch.setattr(module, "get_feature_config", mock.AsyncMock(return_value={"enabled": False, "config": {}}))
    if hasattr(module, "configured_assistant_names"):
        monkeypatch.setattr(module, "configured_assistant_names", mock.AsyncMock(return_value=()))
    if hasattr(module, "_configured_assistant_names"):
        monkeypatch.setattr(module, "_configured_assistant_names", mock.AsyncMock(return_value=()))


def _control(monkeypatch):
    _no_flags(monkeypatch, route_control_module)
    controller = mock.MagicMock()
    controller.detect_sequence_intent.return_value = False
    controller.extract_intent = mock.AsyncMock(side_effect=AssertionError("no dispatch"))
    h._runtime.set_smart_controller(controller)
    return asyncio.run(route_control_module.route_control_node(_state(I.CONTROL, "turn on the garage relay")))


def _music(monkeypatch, intent):
    _no_flags(monkeypatch, route_music_module)
    h._runtime.set_music_handler(mock.MagicMock())
    return asyncio.run(route_music_module.route_music_node(_state(intent, "play some jazz")))


def _tv(monkeypatch):
    _no_flags(monkeypatch, route_tv_module)
    h._runtime.set_tv_handler(mock.MagicMock())
    return asyncio.run(route_tv_module.route_tv_node(_state(I.TV_CONTROL, "open netflix")))


def _notification(monkeypatch):
    from orchestrator.nodes import notification_pref_node

    return asyncio.run(notification_pref_node(_state(I.NOTIFICATION_PREF, "stop the morning notifications")))


def _refusals(monkeypatch):
    from orchestrator.mode_permission import (
        HAWriteDecision, PermissionScope, intent_gate_refusal,
        permission_refusal_message, sequence_refusal_message,
    )

    scope = PermissionScope(permissions=_public(), mode="guest")
    return {
        "gate:websearch": intent_gate_refusal(I.WEBSEARCH, _public()),
        "gate:dining": intent_gate_refusal(I.DINING, _public()),
        "CONTROL": _control(monkeypatch).answer,
        "MUSIC_PLAY": _music(monkeypatch, I.MUSIC_PLAY).answer,
        "MUSIC_CONTROL": _music(monkeypatch, I.MUSIC_CONTROL).answer,
        "TV_CONTROL": _tv(monkeypatch).answer,
        "NOTIFICATION_PREF": _notification(monkeypatch).answer,
        "permission_refusal_message": permission_refusal_message(["lock"], scope),
        "permission_refusal_message:partial": permission_refusal_message(["lock"], scope, partial=True),
        "sequence_refusal_message": sequence_refusal_message(
            HAWriteDecision(allowed=False, denied_targets=("lock.front",), reason="x"), scope
        ),
    }


def test_public_refusals_are_mode_free(monkeypatch):
    refusals = _refusals(monkeypatch)
    assert len(refusals) >= 7
    assert refusals["CONTROL"], "named member: the control refusal exists"
    for name, text in refusals.items():
        assert text, name
        assert not MODE_WORDS.search(text), f"{name}: {text!r}"


def test_public_control_refusal_is_the_public_intent_refusal(monkeypatch):
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL

    assert _control(monkeypatch).answer == PUBLIC_INTENT_REFUSAL


def test_guest_wording_unchanged():
    from orchestrator.mode_permission import PermissionScope, permission_refusal_message

    guest = h.mode_permission.normalize_permissions({"mode": "guest"})
    scope = PermissionScope(permissions=guest, mode="guest")
    assert permission_refusal_message(["lock"], scope) == "Sorry, I can't control the locks in guest mode."


def test_public_tool_creation_gets_the_public_refusal(monkeypatch):
    """codex L (named): with self-building tools enabled, a public "create a
    tool" request gets the public refusal before any owner-mode copy, and
    nothing is generated."""
    from fastapi.testclient import TestClient
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL

    h.patch_conversation_config(monkeypatch)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    manager = mock.MagicMock()
    manager.check_enabled = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(h.main.SelfBuildingToolsFactory, "get", staticmethod(lambda: manager))
    generate = mock.AsyncMock(side_effect=AssertionError("no tool generated for the public"))
    monkeypatch.setattr(h.main, "generate_tool_from_request", generate)

    resp = TestClient(h.main.app).post(
        "/query",
        json={"query": "create a tool that opens the garage", "caller_trust": "web_public"},
        headers=h.service_headers(),
    )
    assert resp.status_code == 200
    answer = resp.json()["answer"]
    assert answer == PUBLIC_INTENT_REFUSAL
    assert not MODE_WORDS.search(answer)
    generate.assert_not_called()


# ---------------------------------------------------------------------------
# D20: reason-aware refusals (ATHENA-233). A guest, public, degraded or
# read-only refusal outranks the one-door wording; door reasons keep a fixed
# order; nothing here asks "Which one?".
# ---------------------------------------------------------------------------

def _denial(reason, domain="lock"):
    from orchestrator.mode_permission import HADenial

    return HADenial(domain=domain, service="unlock", targets=(), reason=reason)


def _scope(kind):
    from orchestrator import mode_permission as mp

    if kind == "owner":
        return mp.PermissionScope(permissions=mp.normalize_permissions({"mode": "owner"}), mode="owner")
    if kind == "guest":
        return mp.PermissionScope(permissions=mp.normalize_permissions({"mode": "guest"}), mode="guest")
    if kind == "public":
        return mp.PermissionScope(permissions=mp.normalize_permissions(mp.public_permissions()), mode="guest")
    if kind == "degraded":
        return mp.PermissionScope(permissions=mp.degraded_permissions(), mode="owner")
    if kind == "stay":
        perms = mp.normalize_permissions(mp.off_stay_overlay({"mode": "owner"}, "recent"))
        return mp.PermissionScope(permissions={**perms, "mode": "owner"}, mode="owner")
    if kind == "read_only":
        scope = _scope("owner")
        scope.read_only = True
        return scope
    raise AssertionError(kind)


DOOR_REASONS = ("opening_needs_named_target", "unlock_needs_one_lock", "one_opening_per_request")


@pytest.mark.parametrize("reason", DOOR_REASONS)
def test_an_owner_hears_the_one_door_phrase_for_each_door_reason(reason):
    from orchestrator import mode_permission as mp

    text = mp.refusal_message([_denial(reason)], _scope("owner"))
    assert text == mp.ONE_DOOR_REFUSAL == "I can unlock or open one door at a time. Name the door, for example 'unlock the front door'."
    assert "?" not in text and "which" not in text.lower()
    assert not MODE_WORDS.search(text)


def test_an_owner_hears_the_template_phrase_for_a_templated_service():
    from orchestrator import mode_permission as mp

    assert mp.refusal_message([_denial("templated_service", "automation")], _scope("owner")) == "I can't set that up with a template."


def test_partial_does_not_change_a_door_phrase():
    from orchestrator import mode_permission as mp

    assert mp.refusal_message([_denial("one_opening_per_request")], _scope("owner"), partial=True) == mp.ONE_DOOR_REFUSAL


@pytest.mark.parametrize("kind,expected", [
    ("guest", "Sorry, I can't control the locks in guest mode."),
    ("public", "Sorry, I can't control the locks from here."),
    ("degraded", "Sorry, I can't control the locks right now because I couldn't verify permissions."),
    ("stay", "Sorry, I can't control the locks in guest mode."),
    ("read_only", "I couldn't check that right now."),
])
@pytest.mark.parametrize("reason", DOOR_REASONS + ("templated_service",))
def test_a_guest_public_degraded_or_read_only_refusal_outranks_every_door_reason(kind, expected, reason):
    from orchestrator import mode_permission as mp

    assert mp.refusal_message([_denial(reason)], _scope(kind)) == expected
    decision = mp.HAWriteDecision(allowed=False, denied_targets=("lock.a",), reason=reason)
    assert mp.sequence_refusal_message(decision, _scope(kind)) != mp.ONE_DOOR_REFUSAL


def test_the_public_marker_alone_outranks_the_door_phrase():
    from orchestrator import mode_permission as mp

    scope = mp.PermissionScope(permissions={"mode": "owner", "audience": "public"}, mode="owner")
    assert mp.refusal_message([_denial("one_opening_per_request")], scope) == "Sorry, I can't control the locks from here."


def test_the_public_door_refusal_names_no_mode():
    from orchestrator import mode_permission as mp

    assert not MODE_WORDS.search(mp.refusal_message([_denial("one_opening_per_request")], _scope("public")))


@pytest.mark.parametrize("reasons,expected_phrase", [
    (("templated_service", "one_opening_per_request"), "door"),
    (("one_opening_per_request", "templated_service"), "door"),
    (("templated_service", "unlock_needs_one_lock"), "door"),
    (("one_opening_per_request", "opening_needs_named_target"), "door"),
    (("halted_after_denial", "templated_service"), "template"),
    (("templated_service", "halted_after_denial"), "template"),
])
def test_two_reasons_in_one_turn_follow_the_d20_order(reasons, expected_phrase):
    from orchestrator import mode_permission as mp

    text = mp.refusal_message([_denial(r) for r in reasons], _scope("owner"))
    assert text == (mp.ONE_DOOR_REFUSAL if expected_phrase == "door" else mp.TEMPLATE_REFUSAL)


def test_the_reason_order_is_the_documented_one():
    from orchestrator import mode_permission as mp

    assert mp._DOOR_REASON_ORDER == (
        "opening_needs_named_target", "unlock_needs_one_lock", "one_opening_per_request", "templated_service",
    )
    assert set(mp._DOOR_REASON_PHRASES) == set(mp._DOOR_REASON_ORDER)


def test_other_reasons_fall_through_to_the_domain_wording():
    from orchestrator import mode_permission as mp

    denials = [_denial("entity_or_domain_denied", "lock"), _denial("halted_after_denial", "cover")]
    assert mp.refusal_message(denials, _scope("guest")) == "Sorry, I can't control the locks in guest mode."
    assert mp.refusal_message(denials, _scope("owner")) == mp.permission_refusal_message(["lock"], _scope("owner"))
    assert mp.refusal_message([], None) == mp.permission_refusal_message([], None)


def test_a_denial_set_with_a_domain_reason_and_a_door_reason_for_an_owner_says_the_door_phrase():
    from orchestrator import mode_permission as mp

    denials = [_denial("entity_or_domain_denied", "light"), _denial("one_opening_per_request")]
    assert mp.refusal_message(denials, _scope("owner")) == mp.ONE_DOOR_REFUSAL


def test_no_scope_never_gets_a_door_phrase():
    from orchestrator import mode_permission as mp

    assert mp.refusal_message([_denial("one_opening_per_request")], None) != mp.ONE_DOOR_REFUSAL


def test_a_sequence_decision_with_a_door_reason_uses_the_phrase_for_an_owner():
    from orchestrator import mode_permission as mp

    for reason in DOOR_REASONS:
        decision = mp.HAWriteDecision(allowed=False, denied_targets=("lock.a",), reason=reason)
        assert mp.sequence_refusal_message(decision, _scope("owner")) == mp.ONE_DOOR_REFUSAL
    templated = mp.HAWriteDecision(allowed=False, reason="templated_service")
    assert mp.sequence_refusal_message(templated, _scope("owner")) == mp.TEMPLATE_REFUSAL
    other = mp.HAWriteDecision(allowed=False, denied_targets=("lock.a",), reason="entity_or_domain_denied")
    assert "schedule" in mp.sequence_refusal_message(other, _scope("guest"))


def test_every_refusal_phrase_is_vetted_text_the_agent_may_relay():
    from orchestrator import mode_permission as mp
    from orchestrator.model_safe_errors import make_user_safe

    for phrase in (mp.ONE_DOOR_REFUSAL, mp.TEMPLATE_REFUSAL, mp.READ_ONLY_REFUSAL):
        assert make_user_safe(phrase, 403) == phrase
    for kind in ("guest", "public", "degraded"):
        assert make_user_safe(mp.refusal_message([_denial("entity_or_domain_denied")], _scope(kind)), 403)


def test_the_denied_exception_carries_its_reason():
    from orchestrator import mode_permission as mp

    assert mp.HAWritePermissionDenied("x", reason="one_opening_per_request").reason == "one_opening_per_request"
    assert mp.HAWritePermissionDenied("x").reason == ""
