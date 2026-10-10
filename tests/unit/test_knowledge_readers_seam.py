"""Each base-knowledge reader, driven through the real AdminConfigClient over a
mocked admin API (one row per tier), shows only the tiers its audience may hear.

The sentinels are the row values; a reader's output must contain a sentinel iff
its tier is in the audience's visible tiers.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from unittest import mock

import httpx
import pytest

from . import _public_audience_harness as h
from . import _public_audience_harness as harness  # noqa: F401  (same module, readability)
from shared.base_knowledge_utils import extract_home_address, extract_owner_name, load_visible_knowledge

TIER_SENTINELS = {"both": h.S_BOTH, "guest": h.S_GUEST, "household": h.S_HOUSEHOLD, "owner": h.S_OWNER}


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _state(aud, *, context=None):
    return h.OrchestratorState(
        query="tell me about the area", mode=aud.mode or "guest", room="kitchen",
        permissions={"mode": aud.mode}, intent=h.IntentCategory.GENERAL_INFO, interface_type="chat",
        context=context or {}, mode_degraded=aud.degraded, knowledge_audience=aud,
    )


AUDIENCES = {
    "household_owner_mode": h.audience("owner"),
    "proven_owner": h.audience("owner", proven=True),
    "guest": h.audience("guest"),
    "degraded_owner_mode": h.audience("owner", degraded=True),
}


@pytest.mark.parametrize("builder", ["tool_call_node", "synthesize_node", "build_synthesis_prompt_for_streaming"])
@pytest.mark.parametrize("name", list(AUDIENCES))
def test_prompt_builders_show_only_the_audience_tiers(monkeypatch, builder, name):
    aud = AUDIENCES[name]
    h.install_mode_client(server_mode=aud.mode or "owner", degraded=aud.degraded)
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    text = h.prompts_for(_state(aud))[builder]
    visible = aud.visible_tiers()
    for tier, sentinel in TIER_SENTINELS.items():
        assert (sentinel in text) == (tier in visible), f"{name}/{builder}/{tier}"
    assert h.S_CHAT not in text
    assert (h.S_OWNERCAT_BOTH in text) == aud.owner_proven


def test_named_member_household_row_reaches_unproven_owner_mode_but_owner_row_does_not(monkeypatch):
    h.install_mode_client(server_mode="owner")
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    text = h.prompts_for(_state(h.audience("owner")))["synthesize_node"]
    assert h.S_HOUSEHOLD in text and h.S_OWNER not in text


def test_tool_call_home_address_ignores_owner_and_household_address_rows(monkeypatch):
    rows = [
        {"id": 1, "category": "property", "key": "address", "value": "1 OWNER-ONLY ST", "applies_to": "owner", "priority": 9, "enabled": True},
        {"id": 2, "category": "property", "key": "address", "value": "2 HOUSEHOLD AVE", "applies_to": "household", "priority": 8, "enabled": True},
    ]
    client = h.real_admin_client(rows)
    aud = h.audience("guest")
    entries = asyncio.run(load_visible_knowledge(client, audience=aud))
    assert extract_home_address(entries) != "1 OWNER-ONLY ST" and extract_home_address(entries) != "2 HOUSEHOLD AVE"
    unproven = asyncio.run(load_visible_knowledge(client, audience=h.audience("owner")))
    assert extract_home_address(unproven) == "2 HOUSEHOLD AVE"
    proven = asyncio.run(load_visible_knowledge(client, audience=h.audience("owner", proven=True)))
    assert extract_home_address(proven) == "1 OWNER-ONLY ST"


def test_owner_name_tier_order_and_resolution():
    rows = [
        {"id": 1, "category": "owner", "key": "owner_name", "value": "Both Name", "applies_to": "both", "priority": 99, "enabled": True},
        {"id": 2, "category": "owner", "key": "owner_name", "value": "Household Name", "applies_to": "household", "priority": 1, "enabled": True},
        {"id": 3, "category": "user", "key": "name", "value": "Owner Name", "applies_to": "owner", "priority": 1, "enabled": True},
    ]
    assert extract_owner_name(rows) == "Owner Name"
    assert extract_owner_name(rows[:2]) == "Household Name"
    assert extract_owner_name(rows[:1]) == "Both Name"
    assert extract_owner_name([{**rows[0], "value": "  "}]) is None
    assert extract_owner_name([]) is None


def test_resolve_addressee_names_only_a_proven_owner(monkeypatch):
    from orchestrator.helpers import resolve_addressee

    client = h.real_admin_client([
        {"id": 1, "category": "owner", "key": "owner_name", "value": "Stored Owner", "applies_to": "owner", "priority": 1, "enabled": True},
        {"id": 2, "category": "owner", "key": "owner_name", "value": "Stored Household", "applies_to": "household", "priority": 1, "enabled": True},
    ])
    proven = asyncio.run(resolve_addressee(_state(h.audience("owner", proven=True)), client))
    assert (proven.kind, proven.name) == ("owner", "Stored Owner")
    unproven = asyncio.run(resolve_addressee(_state(h.audience("owner")), client))
    assert (unproven.kind, unproven.name) == (None, None)
    degraded = asyncio.run(resolve_addressee(_state(h.audience("owner", degraded=True)), client))
    assert degraded.kind is None


def test_unresolved_audience_loads_nothing():
    from shared.knowledge_tiers import KnowledgeAudience

    client = h.real_admin_client()
    spy = mock.AsyncMock(wraps=client.get_base_knowledge)
    client.get_base_knowledge = spy
    assert asyncio.run(load_visible_knowledge(client, audience=KnowledgeAudience.UNRESOLVED)) == []
    spy.assert_not_awaited()


def test_real_client_filters_by_tiers_and_requires_them():
    client = h.real_admin_client()
    rows = asyncio.run(client.get_base_knowledge(tiers=frozenset({"household"})))
    assert [r["value"] for r in rows] == [h.S_HOUSEHOLD]
    assert asyncio.run(client.get_base_knowledge(tiers=frozenset())) == []
    with pytest.raises(TypeError):
        asyncio.run(client.get_base_knowledge())  # type: ignore[call-arg]


def _load_directions():
    path = h.REPO_ROOT / "src" / "rag" / "directions" / "main.py"
    spec = importlib.util.spec_from_file_location("directions_main_for_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_directions_default_origin_uses_only_everyone_rows(monkeypatch):
    directions = _load_directions()
    rows = [
        {"id": 1, "category": "property", "key": "address", "value": "1 OWNER ST", "applies_to": "owner", "enabled": True},
        {"id": 2, "category": "property", "key": "address", "value": "2 HOUSEHOLD AVE", "applies_to": "household", "enabled": True},
        {"id": 3, "category": "property", "key": "address", "value": "3 GUEST RD", "applies_to": "guest", "enabled": True},
        {"id": 4, "category": "location", "key": "default_location", "value": "Everyone City", "applies_to": "both", "enabled": True},
        {"id": 5, "category": "property", "key": "wifi", "value": "disabled both", "applies_to": "both", "enabled": False},
    ]

    def _handler(request):
        if request.url.path == "/api/base-knowledge/public":
            return httpx.Response(200, json=rows)
        return httpx.Response(404, json={})

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(directions.httpx, "AsyncClient", _factory)
    # Resolved at import from the environment, which earlier tests in a job may have cleared.
    monkeypatch.setattr(directions, "ADMIN_API_URL", "http://admin-backend:8080")
    monkeypatch.setattr(directions, "startup_service", mock.AsyncMock(), raising=False)
    monkeypatch.setattr(directions, "unregister_service", mock.AsyncMock(), raising=False)

    async def _run():
        cm = directions.lifespan(directions.app)
        await cm.__aenter__()
        try:
            return dict(directions.BASE_KNOWLEDGE), directions.get_default_origin()
        finally:
            await cm.__aexit__(None, None, None)

    loaded, origin = asyncio.run(_run())
    assert origin == "Everyone City"
    for private in ("1 OWNER ST", "2 HOUSEHOLD AVE", "3 GUEST RD"):
        assert private != origin
    assert loaded.get("default_location") == "Everyone City"
    assert "address" not in loaded  # named member: a household/owner/guest address row is ignored
    assert "wifi" not in loaded
