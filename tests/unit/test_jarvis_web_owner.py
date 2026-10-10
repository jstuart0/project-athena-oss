"""jarvis-web and the owner: who is sent as web_owner, which chat ids and which
persistent thread an owner uses, and that the orchestrator's session rule keeps
those ids (the two images share one rule: see test_owner_session_ids.py)."""
from __future__ import annotations

import importlib.util
from unittest import mock

import pytest

from . import _jarvis_web_harness as h

main = h.main
LAN, INTERNET = h.LAN, h.INTERNET


def _spec():
    spec = importlib.util.spec_from_file_location("_orch_session_keys_for_jarvis", h.REPO_ROOT / "src/orchestrator/session_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KEYS = _spec()


@pytest.fixture
def out(monkeypatch):
    out = h.install_outbound(monkeypatch)
    h.configure()
    return out


def _owner_headers(**extra):
    return {**h.via_proxy(INTERNET, Authorization="Bearer t"), **h.CSRF, **extra}


def _home_headers():
    return {**h.via_proxy(LAN), **h.CSRF}


def _chat(client, headers, path="/api/chat", **body):
    return client.post(path, json={"message": "hi", **body}, headers=headers)


def _session_from_stream(resp):
    first = resp.text.split("\n\n", 1)[0]
    return first.split('"session_id": "', 1)[1].split('"', 1)[0]


# --- who is web_owner ----------------------------------------------------------

@pytest.mark.parametrize("role,expected", [("owner", "web_owner"), ("operator", "web_authenticated")])
def test_bearer_role_decides_the_trust(out, role, expected):
    h.install_role(role)
    _chat(h.client(), _owner_headers())
    assert out.orchestrator_bodies()[-1]["caller_trust"] == expected


def test_nobody_else_is_web_owner(out):
    h.configure()
    _chat(h.client(), _home_headers())
    assert out.orchestrator_bodies()[-1]["caller_trust"] == "web_local"


def test_a_browser_supplied_trust_never_reaches_the_orchestrator(out):
    h.install_role("operator")
    _chat(h.client(), _owner_headers(), caller_trust="web_owner")
    assert out.orchestrator_bodies()[-1]["caller_trust"] == "web_authenticated"


def test_is_bearer_owner_needs_class_source_and_role():
    c = main.caller_auth.Caller
    A = main.caller_auth.CLASS_AUTHENTICATED
    assert c(A, "owner", True, "owner", "ok", "bearer").trust == "web_owner"
    assert c(A, "owner", True, "operator", "ok", "bearer").trust == "web_authenticated"
    assert c(A, "owner", True, "owner", "ok", "edge").trust == "web_authenticated"
    assert c(main.caller_auth.CLASS_LOCAL, "owner", False, "owner", "ok", "local").trust == "web_local"
    assert c(main.caller_auth.CLASS_GUEST_NET, "guest", False, "owner", "ok", "bearer").trust == "web_guest_net"


# --- ids -----------------------------------------------------------------------

def test_an_owner_gets_a_signed_own_id_the_orchestrator_recognises_and_keeps(out):
    h.install_role("owner")
    client = h.client()
    sid = _chat(client, _owner_headers()).json()["session_id"]
    assert sid.startswith("own-") and "." in sid
    assert KEYS.id_class(sid) == KEYS.CALLER_CLASS_OWNER
    assert KEYS.usable_session_id(sid, KEYS.CALLER_CLASS_OWNER) == sid  # kept, not re-minted
    # multi-turn continuity: the same id goes upstream every turn
    for _ in range(3):
        again = _chat(client, _owner_headers(), session_id=sid).json()["session_id"]
        assert again == sid
        assert out.orchestrator_bodies()[-1]["session_id"] == sid


def test_stream_announces_and_sends_the_same_own_id(out):
    h.install_role("owner")
    client = h.client()
    sid = _session_from_stream(_chat(client, _owner_headers(), path="/api/chat/stream"))
    assert sid.startswith("own-") and out.orchestrator_bodies()[-1]["session_id"] == sid
    again = _chat(client, _owner_headers(), path="/api/chat/stream", session_id=sid)
    assert _session_from_stream(again) == sid and out.orchestrator_bodies()[-1]["session_id"] == sid


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_a_stale_ordinary_id_is_replaced_for_the_owner_and_a_stale_owner_id_for_everyone_else(out, path):
    h.install_role("owner")
    client = h.client()  # one browser: one cookie, one id key
    pick = (lambda r: r.json()["session_id"]) if path == "/api/chat" else _session_from_stream

    ordinary = pick(_chat(client, _home_headers(), path=path))
    assert not ordinary.startswith("own-")
    owner = pick(_chat(client, _owner_headers(), path=path, session_id=ordinary))
    assert owner != ordinary and owner.startswith("own-")
    assert out.orchestrator_bodies()[-1]["session_id"] == owner

    back = pick(_chat(client, _home_headers(), path=path, session_id=owner))
    assert back != owner and not back.startswith("own-")
    assert out.orchestrator_bodies()[-1]["session_id"] == back


def test_during_a_stay_the_owner_uses_ordinary_ids(monkeypatch):
    out = h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "Gina Guest", "id": 7})
    h.configure()
    h.install_role("owner")
    sid = _chat(h.client(), _owner_headers()).json()["session_id"]
    assert not sid.startswith("own-") and sid.startswith("gst-")
    assert out.orchestrator_bodies()[-1]["caller_trust"] == "web_owner"  # still an owner caller; the orchestrator decides proof


def test_a_forged_own_prefix_without_the_mac_starts_fresh(out):
    h.install_role("owner")
    client = h.client()
    for forged in ("own-" + "0" * 32 + "." + "0" * 24, "own-1", "own-" + "a" * 200):
        sid = _chat(client, _owner_headers(), session_id=forged).json()["session_id"]
        assert sid != forged and sid.startswith("own-")


# --- the owner's persistent thread ---------------------------------------------

def _caller(trust_owner, mode="owner"):
    A = main.caller_auth.CLASS_AUTHENTICATED
    return main.caller_auth.Caller(A, mode, True, "owner" if trust_owner else "operator", "ok", "bearer")


def test_history_identity_key_is_a_derived_36_char_value_only_for_the_owner():
    cookie = "11111111-2222-3333-4444-555555555555"
    key = main._history_identity_key(cookie, _caller(True))
    assert key != cookie and len(key) == 36 and key.startswith("o")
    assert key == main._history_identity_key(cookie, _caller(True))
    assert key != main._history_identity_key("other-cookie", _caller(True))
    assert main._history_identity_key(cookie, _caller(False)) == cookie
    stay = main._history_identity_key(cookie, _caller(True, mode="guest"))  # a stay: the guest-mode thread
    assert stay.startswith("g") and len(stay) == 36 and stay not in (cookie, key)


def test_restore_clear_and_stream_use_the_derived_identity(out, monkeypatch):
    config = {"cookie_name": "jarvis_uid", "session_ttl_days": 30, "cookie_secure": False}
    monkeypatch.setattr(main, "get_persistent_sessions_config", mock.AsyncMock(return_value=config))
    monkeypatch.setattr(main, "get_engine", mock.AsyncMock(return_value=object()))
    identity = mock.AsyncMock(return_value="ident")
    monkeypatch.setattr(main, "get_or_create_identity", identity)
    monkeypatch.setattr(main, "get_or_create_active_thread", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_safe_db_exec", mock.AsyncMock())
    h.install_role("owner")
    client = h.client()
    client.cookies.set("jarvis_uid", "cookie-abc")

    owner_calls = []
    for call in (
        lambda: client.get("/api/session/restore", headers=_owner_headers()),
        lambda: client.delete("/api/session/current", headers=_owner_headers()),
        lambda: _chat(client, _owner_headers(), path="/api/chat/stream"),
    ):
        identity.reset_mock()
        assert call().status_code == 200
        owner_calls.append(identity.await_args.args[0])
    assert len(owner_calls) == 3 and all(k.startswith("o") and len(k) == 36 for k in owner_calls)
    assert len(set(owner_calls)) == 1

    identity.reset_mock()
    client.get("/api/session/restore", headers=_home_headers())
    assert identity.await_args.args[0] == "cookie-abc"  # signed out / home network: the raw cookie


def test_every_identity_call_goes_through_the_key_function():
    import ast

    source = (h.BACKEND / "main.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "get_or_create_identity"]
    assert len(calls) >= 3
    for call in calls:
        first = call.args[0]
        assert isinstance(first, ast.Call) and getattr(first.func, "id", None) == "_history_identity_key"


# --- review follow-ups: MAC encoding, failed mode lookup, owner cache TTL -----------

def test_session_mac_fields_cannot_collide():
    assert main._session_mac("P|x", "y") != main._session_mac("P", "x|y")
    assert main._session_mac("a", "bc") != main._session_mac("ab", "c")


@pytest.mark.parametrize("part", ["x" * 32, "own-" + "g" * 32, "OWN-" + "a" * 32, "own-own-" + "a" * 32, "a" * 31, "a" * 33, "p|" + "a" * 30])
def test_only_well_formed_session_parts_are_accepted_even_with_a_valid_mac(out, part):
    h.install_role("owner")
    client = h.client()
    client.get("/api/session/restore", headers=_owner_headers())  # nothing to restore; just gets a browser key
    first = _chat(client, _owner_headers()).json()["session_id"]  # sets the chat key cookie
    key = client.cookies.get("jarvis_chat_key")
    forged = f"{part}.{main._session_mac(part, key)}"
    sid = _chat(client, _owner_headers(), session_id=forged).json()["session_id"]
    assert sid != forged and first.startswith("own-")


def test_a_well_formed_signed_id_still_resumes(out):
    h.install_role("owner")
    client = h.client()
    sid = _chat(client, _owner_headers()).json()["session_id"]
    assert _chat(client, _owner_headers(), session_id=sid).json()["session_id"] == sid


def test_a_failed_guest_lookup_never_counts_as_owner_history(monkeypatch):
    """admin-backend down reads as 'no guest booked' (mode owner); that must not
    start the owner's own conversation, because a stay may be in progress."""
    out = h.install_outbound(monkeypatch)
    h.configure()
    h.install_role("owner")
    real_factory = out.factory()

    class _Down(real_factory):
        async def get(self, url, *a, **kw):
            if "current-guest" in url:
                raise ConnectionError("admin-backend down")
            return await super().get(url, *a, **kw)

    monkeypatch.setattr(main.httpx, "AsyncClient", _Down)
    sid = _chat(h.client(), _owner_headers()).json()["session_id"]
    assert not sid.startswith("own-")
    assert out.orchestrator_bodies()[-1]["caller_trust"] == "web_owner"  # still an owner caller; unproven upstream

    monkeypatch.setattr(main.httpx, "AsyncClient", real_factory)  # positive control: lookup works
    assert _chat(h.client(), _owner_headers()).json()["session_id"].startswith("own-")


def test_a_failed_guest_lookup_uses_the_ordinary_thread(monkeypatch):
    caller = _caller(True)
    token = main._mode_lookup_failed.set(True)
    try:
        failed = main._history_identity_key("cookie", caller)
        assert failed.startswith("g") and failed != "cookie"  # unknown reads as the narrowest audience
        assert main._owner_history(caller) is False
    finally:
        main._mode_lookup_failed.reset(token)
    assert main._owner_history(caller) is True


@pytest.mark.parametrize("role,ttl", [("owner", 10), ("operator", 60)])
def test_bearer_decision_cache_lifetime_by_role(monkeypatch, role, ttl):
    calls = []

    async def _me(token):
        calls.append(token)
        return h.FakeResponse(200, {"role": role})

    caller_auth = main.caller_auth
    caller_auth._reset_for_tests()
    caller_auth._set_auth_me_callable_for_tests(_me)
    monkeypatch.setattr(caller_auth, "get_admin_url", lambda: "http://admin.local:8080")
    clock = {"now": 1000.0}
    monkeypatch.setattr(caller_auth.time, "monotonic", lambda: clock["now"])
    resolve = lambda: __import__("asyncio").run(caller_auth._resolve_auth_decision("tok", "rate"))  # noqa: E731
    assert resolve().role == role and len(calls) == 1
    clock["now"] += ttl - 1
    resolve()
    assert len(calls) == 1, "still cached just inside the lifetime"
    clock["now"] += 2
    resolve()
    assert len(calls) == 2, "re-checked once the lifetime passed"


# --- the household / guest boundary on the web ----------------------------------------

class _FakeThreads:
    """An in-memory persistent-chat store keyed like the real tables: identity
    key -> one active thread of (user, assistant) turns."""

    def __init__(self, monkeypatch):
        self.threads = {}
        monkeypatch.setattr(main, "get_persistent_sessions_config", mock.AsyncMock(return_value={
            "cookie_name": "jarvis_uid", "session_ttl_days": 30, "cookie_secure": False, "max_restored_turns": 20}))
        monkeypatch.setattr(main, "get_engine", mock.AsyncMock(return_value=object()))
        monkeypatch.setattr(main, "get_or_create_identity", self.identity)
        monkeypatch.setattr(main, "get_or_create_active_thread", self.thread)
        monkeypatch.setattr(main, "load_chat_history", self.history)
        monkeypatch.setattr(main, "save_exchange", self.save)
        monkeypatch.setattr(main, "_safe_db_exec", mock.AsyncMock())

    async def identity(self, key, ttl):
        return key

    async def thread(self, identity_id):
        t = self.threads.setdefault(identity_id, {"id": identity_id, "turns": [], "live": None})
        return {"id": t["id"], "is_new": not t["turns"], "turn_count": len(t["turns"]),
                "current_orch_session_id": t["live"]}

    async def history(self, thread_id, max_turns):
        out = []
        for user, assistant in self.threads[thread_id]["turns"]:
            out += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
        return out

    async def save(self, thread_id, turn_number, user_content, assistant_content, orch_session_id):
        self.threads[thread_id]["turns"].append((user_content, assistant_content or ""))
        self.threads[thread_id]["live"] = orch_session_id


def _stream(client, headers, **body):
    resp = client.post("/api/chat/stream", json={"message": "SECRET HOUSEHOLD TURN", **body}, headers=headers)
    assert resp.status_code == 200
    return resp


def _last_history(out):
    return out.orchestrator_bodies()[-1].get("chat_history")


@pytest.mark.parametrize("first,second", [("owner", "guest"), ("guest", "owner")], ids=["stay_begins", "stay_ends"])
def test_a_mode_flip_never_restores_or_injects_the_other_audiences_thread(monkeypatch, first, second):
    out = h.install_outbound(monkeypatch)
    h.configure()
    h.install_role("operator")  # signed in, not the owner role: household vs guest is the only axis
    store = _FakeThreads(monkeypatch)
    client = h.client()
    client.cookies.set("jarvis_uid", "cookie-abc")
    guest = {"has_guest": True, "guest_name": "Gina Guest", "id": 7}

    def phase(mode):
        out.guest = guest if mode == "guest" else None

    phase(first)
    for _ in range(2):
        _stream(client, _owner_headers())
    assert len(next(iter(store.threads.values()))["turns"]) == 2
    first_threads = set(store.threads)

    phase(second)
    restored = client.get("/api/session/restore", headers=_owner_headers()).json()
    assert restored["restored"] is False
    _stream(client, _owner_headers())
    assert not _last_history(out), "the other audience's turns were injected"
    assert set(store.threads) - first_threads, "the new audience got its own thread"
    for t in store.threads.values():
        assert len(t["turns"]) in (1, 2)

    phase(first)  # and back: the original thread is still its own
    restored = client.get("/api/session/restore", headers=_owner_headers()).json()
    assert restored["restored"] is True and len(restored["messages"]) == 4


def test_web_ids_follow_the_audience_class(monkeypatch):
    out = h.install_outbound(monkeypatch)
    h.configure()
    h.install_role("operator")
    client = h.client()
    household = _chat(client, _owner_headers()).json()["session_id"]
    assert not household.startswith(("own-", "gst-"))
    out.guest = {"has_guest": True, "guest_name": "Gina Guest", "id": 7}
    guest = _chat(client, _owner_headers(), session_id=household).json()["session_id"]
    assert guest.startswith("gst-") and guest != household
    assert KEYS.id_class(guest) == KEYS.CALLER_CLASS_GUEST
    assert _chat(client, _owner_headers(), session_id=guest).json()["session_id"] == guest  # guest continuity
    out.guest = None
    back = _chat(client, _owner_headers(), session_id=guest).json()["session_id"]
    assert back != guest and not back.startswith("gst-")  # the stale guest id is replaced
