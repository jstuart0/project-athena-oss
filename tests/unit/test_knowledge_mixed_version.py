"""Mixed-version states during the roll fail closed.

Roll order: admin-backend and admin-frontend, then the orchestrator and the
directions RAG, then jarvis-web. These tests pin what each mixed state can and
can't do, from the new side (an old peer is modelled by what it can send or
read).
"""
from __future__ import annotations

import asyncio

import pytest

from . import _public_audience_harness as h

# What a jarvis-web that predates web_owner can send as caller_trust.
OLD_JARVIS_TRUSTS = ["web_authenticated", "web_local", "web_guest_net", "web_public", None]


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.mark.parametrize("trust", OLD_JARVIS_TRUSTS)
@pytest.mark.parametrize("authenticated", [True, False])
@pytest.mark.parametrize("server", ["owner", "guest"])
def test_new_orchestrator_with_old_jarvis_web_has_no_proven_owner(trust, authenticated, server):
    h.install_mode_client(server_mode=server)
    authz = asyncio.run(h.mode_permission.resolve_request_authorization(
        None, None, caller_trust=trust, service_authenticated=authenticated,
    ))
    assert not authz.knowledge_audience.owner_caller and not authz.knowledge_audience.owner_proven
    assert "owner" not in authz.knowledge_audience.visible_tiers()


def test_an_old_orchestrators_view_never_sees_household_or_unknown_audiences():
    """The pre-audience filter kept a row when applies_to was 'both' or the mode.
    'household' (the new shared value) and legacy values match neither mode, so
    an old orchestrator ignores Household rows rather than leaking them."""
    rows = [{"applies_to": t} for t in ("both", "guest", "household", "owner", "chat")]
    for mode in ("owner", "guest"):
        old = {r["applies_to"] for r in rows if r["applies_to"] in ("both", mode)}
        assert "household" not in old and "chat" not in old


def test_owner_sessions_are_invisible_to_a_process_that_only_knows_the_old_namespaces():
    from orchestrator import session_keys as keys

    owner = keys.new_session_id(keys.CALLER_CLASS_OWNER)
    old_session, old_context = "athena:session:" + owner, "athena:context:" + owner
    assert keys.session_storage_key(owner) != old_session
    assert keys.context_storage_key(owner) != old_context


def test_guest_sessions_are_invisible_to_a_process_that_only_knows_the_old_namespaces():
    from orchestrator import session_keys as keys

    guest = keys.new_session_id(keys.CALLER_CLASS_GUEST)
    assert guest.startswith("gst-")
    assert keys.session_storage_key(guest) != "athena:session:" + guest
    assert keys.context_storage_key(guest) != "athena:context:" + guest


def test_every_ordinary_and_public_key_is_unchanged_for_old_pods():
    from orchestrator import session_keys as keys

    for sid in ("abc.def", "pub-xyz", "sms_0123"):
        assert keys.session_storage_key(sid) == "athena:session:" + sid
        assert keys.context_storage_key(sid) == "athena:context:" + sid
    assert keys.OAI_SESSION_INDEX_KEY == "athena:session:oai_index"


def test_new_jarvis_web_with_an_old_orchestrator_is_refused_not_trusted():
    """An old orchestrator's caller_trust Literal doesn't include web_owner, so
    its request validation rejects the body (422). Pinned from the new side:
    web_owner is exactly the value the new Literal adds."""
    import typing

    literal = typing.get_args(typing.get_args(typing.get_type_hints(h.main.QueryRequest)["caller_trust"])[0])
    assert "web_owner" in literal
    old = set(literal) - {"web_owner"}
    assert old == {"household", "sms", "web_authenticated", "web_local", "web_guest_net", "web_public"}
