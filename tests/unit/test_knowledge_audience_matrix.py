"""The exhaustive prompt matrix: for every caller_trust value x server state x
authenticated service hop x prompt builder, each tier sentinel is in the
prompt iff the request's resolved audience may hear that tier, and the owner
is the addressee iff the audience is a proven owner.

The audience is the real one resolve_request_authorization builds; only
(web_owner, authenticated hop, server owner, healthy mode service) is proven.
"""
from __future__ import annotations

import asyncio
import dataclasses
import itertools
import typing
from types import SimpleNamespace

import pytest

from . import _public_audience_harness as h
from shared.knowledge_tiers import KnowledgeAudience

TRUSTS = [*typing.get_args(typing.get_args(typing.get_type_hints(h.main.QueryRequest)["caller_trust"])[0]), None]
SERVERS = ["owner", "guest", "degraded"]
BUILDERS = ["tool_call_node", "synthesize_node", "build_synthesis_prompt_for_streaming"]
TIER_SENTINELS = {"both": h.S_BOTH, "guest": h.S_GUEST, "household": h.S_HOUSEHOLD, "owner": h.S_OWNER}

CASES = list(itertools.product(TRUSTS, SERVERS, [False, True], BUILDERS))


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _state_for(trust, server, authenticated, *, device=None, request_mode=None):
    from orchestrator.helpers import build_query_context

    if server == "degraded":
        h.install_mode_client(degraded=True)
    else:
        h.install_mode_client(server_mode=server)
    authz = asyncio.run(h.mode_permission.resolve_request_authorization(
        request_mode, device, caller_trust=trust, service_authenticated=authenticated,
    ))
    aud = authz.knowledge_audience
    request = SimpleNamespace(caller_trust=trust, context={})
    context = build_query_context(request, device, server_mode=authz.server_mode, degraded=authz.degraded)
    state = h.OrchestratorState(
        query="tell me about the area", mode=authz.mode, room="kitchen", permissions=authz.permissions,
        intent=h.IntentCategory.GENERAL_INFO, interface_type="chat", context=context,
        mode_degraded=authz.degraded, knowledge_audience=aud,
    )
    return state, aud


def _assert_prompt(text, aud, where):
    visible = aud.visible_tiers()
    for tier, sentinel in TIER_SENTINELS.items():
        assert (sentinel in text) == (tier in visible), f"{where}: {tier}"
    assert h.S_CHAT not in text, where
    assert (h.S_OWNERCAT_BOTH in text) == aud.owner_proven, where
    assert (f"You are speaking with {h.OWNER_NAME}" in text) == aud.owner_proven, where
    assert (h.OWNER_NAME in text) == aud.owner_proven, where


def test_matrix_floor_and_named_member():
    assert len(CASES) >= 144
    assert ("web_local", "owner", True, "synthesize_node") in CASES
    assert "web_owner" in TRUSTS


@pytest.mark.parametrize("trust,server,proven,builder", CASES)
def test_prompt_matrix(monkeypatch, trust, server, proven, builder):
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    state, aud = _state_for(trust, server, proven)
    assert aud.owner_proven == (trust == "web_owner" and proven and server == "owner")
    text = h.prompts_for(state)[builder]
    _assert_prompt(text, aud, f"{trust}/{server}/{proven}/{builder}")


def test_named_member_web_local_owner_mode_lacks_owner_and_has_household(monkeypatch):
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    state, aud = _state_for("web_local", "owner", True)
    text = h.prompts_for(state)["synthesize_node"]
    assert h.S_OWNER not in text and h.S_HOUSEHOLD in text and not aud.owner_proven


def test_proven_owner_with_a_guest_device_is_a_guest_audience(monkeypatch):
    """guest_info present and request_mode=guest: guest audience, so no owner
    and no household sentinel even when the caller would otherwise be proven."""
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    device = {"guest_id": 9, "guest_name": "Bob Device", "device_type": "voice"}
    state, aud = _state_for("web_owner", "owner", True, device=device, request_mode="guest")
    assert aud.mode == "guest" and aud.owner_caller and not aud.owner_proven
    for builder, text in h.prompts_for(state).items():
        assert h.S_OWNER not in text and h.S_HOUSEHOLD not in text, builder
        assert h.S_OWNERCAT_BOTH not in text, builder
        assert h.S_BOTH in text and h.S_GUEST in text, builder


def test_degraded_audience_sees_only_everyone_rows(monkeypatch):
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    state, aud = _state_for("web_owner", "degraded", True)
    assert aud.owner_caller and not aud.owner_proven
    assert aud.visible_tiers() == frozenset({"both"})
    for builder, text in h.prompts_for(state).items():
        assert h.S_BOTH in text, builder
        for hidden in (h.S_GUEST, h.S_HOUSEHOLD, h.S_OWNER, h.S_OWNERCAT_BOTH, h.OWNER_NAME):
            assert hidden not in text, builder


def test_state_round_trip_keeps_the_audience_and_default_fails_closed():
    aud = KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=True, owner_proven=True)
    state = h.OrchestratorState(query="q", knowledge_audience=aud)
    again = h.OrchestratorState.model_validate(state.model_dump())
    assert again.knowledge_audience == aud and again.knowledge_audience.owner_proven is True
    assert h.OrchestratorState(query="q").knowledge_audience.visible_tiers() == frozenset()


# --- the audience resolve_request_authorization builds ------------------------

@pytest.mark.parametrize("trust,server,expected", [
    ("household", "owner", dict(mode="owner", degraded=False, public=False)),
    ("web_local", "guest", dict(mode="guest", degraded=False, public=False)),
    ("web_authenticated", "degraded", dict(mode="owner", degraded=True, public=False)),
    ("web_public", "owner", dict(mode="guest", degraded=False, public=True)),
    ("web_public", "degraded", dict(mode="guest", degraded=True, public=True)),
    (None, "owner", dict(mode="owner", degraded=False, public=False)),
])
def test_authorization_builds_the_audience(trust, server, expected):
    _, aud = _state_for(trust, server, False)
    got = dataclasses.asdict(aud)
    assert got.pop("guest_verified") is not None
    assert got == {**expected, "owner_caller": False, "owner_proven": False}


def test_guest_tier_needs_a_server_guest_mode_or_a_real_stay(monkeypatch):
    """A caller's mode=guest hint in an owner house narrows who they are; it
    doesn't make them a guest, so it can't read guest-tier rows."""
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    hint_state, hint = _state_for("web_local", "owner", False, request_mode="guest")
    assert hint.mode == "guest" and hint.visible_tiers() == frozenset({"both"})
    text = h.prompts_for(hint_state)["synthesize_node"]
    assert h.S_GUEST not in text and h.S_BOTH in text

    device = {"guest_id": 9, "guest_name": "Bob Device", "device_type": "voice"}
    _, stay = _state_for("web_local", "owner", False, device=device)
    assert stay.visible_tiers() == frozenset({"both", "guest"})
    _, server_guest = _state_for("web_local", "guest", False, request_mode="guest")
    assert server_guest.visible_tiers() == frozenset({"both", "guest"})
