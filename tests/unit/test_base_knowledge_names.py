"""Static base-knowledge name rows: guest_name is never rendered, and the
owner's name is rendered only into owner-mode prompts."""
from __future__ import annotations

import asyncio

import pytest

from . import _public_audience_harness as h

ROWS = [
    {"id": 51, "category": "user", "key": "guest_name", "value": "Zed Former", "applies_to": "both"},
    {"id": 52, "category": "owner", "key": "owner_name", "value": "Olive Owner", "applies_to": "both"},
    {"id": 53, "category": "user", "key": "name", "value": "Nora Name", "applies_to": "both"},
    {"id": 54, "category": "property", "key": "wifi", "value": "Wi-Fi: Example-Net", "applies_to": "both"},
    {"id": 55, "category": "user", "key": "first_name", "value": "Fay First", "applies_to": "both"},
    {"id": 56, "category": "owner", "key": "favorite_color", "value": "Owner Blue", "applies_to": "both"},
    {"id": 57, "category": "user", "key": "diet", "value": "Vegetarian household", "applies_to": "both"},
]


@pytest.fixture(autouse=True)
def _reset():
    import shared.base_knowledge_utils as bku

    reset = getattr(bku, "_reset_for_tests", None)
    if reset:
        reset()
    h.reset_runtime()
    yield
    if reset:
        reset()
    h.reset_runtime()


def _build(user_mode, degraded=False):
    from shared.base_knowledge_utils import build_knowledge_context

    return build_knowledge_context(ROWS, user_mode, degraded=degraded)


def test_guest_name_row_is_never_rendered(captured_logs):
    for mode in ("owner", "guest"):
        text = _build(mode)
        assert "Zed Former" not in text
        assert "The user's name is" not in text
        assert "Example-Net" in text
    warnings = [e for e in captured_logs if e.get("event") == "base_knowledge_static_guest_name_ignored"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0].get("row_id") == 51
    assert "Zed Former" not in repr(warnings)


def test_owner_name_only_in_owner_prompts():
    owner = _build("owner")
    assert "Property owner's name: Olive Owner" in owner
    assert "Property owner's name: Nora Name" in owner
    guest = _build("guest")
    assert "Olive Owner" not in guest
    assert "Nora Name" not in guest


def test_every_name_key_and_owner_row_is_owner_mode_only():
    guest = _build("guest")
    assert "Fay First" not in guest
    assert "Owner Blue" not in guest
    assert "Vegetarian household" in guest
    owner = _build("owner")
    assert "Fay First" in owner
    assert "Owner Blue" in owner


def test_degraded_owner_prompt_has_no_names_or_owner_rows():
    """A degraded mode service resolves to owner, but nobody is named or
    framed as the owner while the mode can't be trusted."""
    text = _build("owner", degraded=True)
    for value in ("Olive Owner", "Nora Name", "Fay First", "Owner Blue", "Zed Former"):
        assert value not in text
    assert "Property owner" not in text
    assert "Example-Net" in text
    assert "Vegetarian household" in text


def test_public_prompt_builds_no_base_knowledge(monkeypatch):
    """Row 16 (pin): the public audience never reaches base knowledge."""
    h.install_mode_client(server_mode="guest")
    _, _, knowledge, _ = h.patch_tool_call_dependencies(monkeypatch)
    h._runtime.set_llm_router(h.CapturingLLM())
    public = h.mode_permission.normalize_permissions(h.mode_permission.public_permissions())
    state = h.make_state(permissions=public, context={"guest_name": "Gina Guest"})
    asyncio.run(h.main.tool_call_node(state))
    knowledge.assert_not_awaited()
