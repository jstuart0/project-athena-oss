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


def _audience(mode, degraded=False, proven=False):
    from shared.knowledge_tiers import KnowledgeAudience

    return KnowledgeAudience(mode=mode, degraded=degraded, public=False, owner_caller=proven, owner_proven=proven)


def _build(user_mode, degraded=False, proven=False, rows=None):
    from shared.base_knowledge_utils import build_knowledge_context

    return build_knowledge_context(ROWS if rows is None else rows, audience=_audience(user_mode, degraded, proven))


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


def test_every_name_key_is_owner_mode_only_and_owner_rows_need_proof():
    guest = _build("guest")
    assert "Fay First" not in guest
    assert "Owner Blue" not in guest
    assert "Vegetarian household" in guest
    owner = _build("owner")
    assert "Fay First" in owner
    assert "Owner Blue" not in owner  # owner-category row stored as Everyone: proof required
    proven = _build("owner", proven=True)
    assert "Fay First" in proven
    assert "Owner Blue" in proven
    assert "Property owner's name: Olive Owner" in proven


@pytest.mark.parametrize("category", ["owner", "Owner", " owner ", "OWNER\t"])
def test_legacy_owner_category_spellings_need_proof(category):
    row = {"id": 90, "category": category, "key": "employer", "value": "SENTINEL_EMPLOYER", "applies_to": "both"}
    for mode, degraded in (("owner", False), ("owner", True), ("guest", False)):
        assert "SENTINEL_EMPLOYER" not in _build(mode, degraded, rows=[row])
    assert "SENTINEL_EMPLOYER" in _build("owner", proven=True, rows=[row])


def test_owner_name_keys_in_a_legacy_spelled_category_keep_the_household_gate():
    row = {"id": 91, "category": " Owner ", "key": "owner_name", "value": "Olive Legacy", "applies_to": "household"}
    assert "Olive Legacy" in _build("owner", rows=[row])
    assert "Olive Legacy" not in _build("owner", degraded=True, rows=[row])
    assert "Olive Legacy" not in _build("guest", rows=[row])


def test_rows_outside_the_audience_tiers_are_dropped_by_the_builder_itself():
    rows = [
        {"id": 1, "category": "property", "key": "a", "value": "S_OWNER", "applies_to": "owner"},
        {"id": 2, "category": "property", "key": "b", "value": "S_HOUSEHOLD", "applies_to": "household"},
        {"id": 3, "category": "property", "key": "c", "value": "S_GUEST", "applies_to": "guest"},
        {"id": 4, "category": "property", "key": "d", "value": "S_BOTH", "applies_to": "both"},
        {"id": 5, "category": "property", "key": "e", "value": "S_CHAT", "applies_to": "chat"},
        {"id": 6, "category": "property", "key": "f", "value": "S_NONE"},
    ]
    owner = _build("owner", rows=rows)
    assert "S_HOUSEHOLD" in owner and "S_BOTH" in owner
    for absent in ("S_OWNER", "S_GUEST", "S_CHAT", "S_NONE"):
        assert absent not in owner
    guest = _build("guest", rows=rows)
    assert "S_GUEST" in guest and "S_BOTH" in guest and "S_HOUSEHOLD" not in guest
    degraded = _build("owner", degraded=True, rows=rows)
    assert "S_BOTH" in degraded and "S_HOUSEHOLD" not in degraded
    assert "S_OWNER" in _build("owner", proven=True, rows=rows)


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
