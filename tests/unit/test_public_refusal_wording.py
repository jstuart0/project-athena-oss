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
