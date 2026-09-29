"""Red/green contract for ATHENA-88 phase 2 (F88): gateway identity
forwarding (user/session_id/room) and the per-IP new-conversation limiter.

Plan Phase 2 tests 18-22 (+19b). Test contract Phase 2 C18-C21, C25-C26.

In-process import of gateway.main, with prometheus_client stubbed (matches
the plan's own stubbing note) — module-level code in gateway/main.py only
reads config and builds the FastAPI app; no network I/O happens at import.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-session-forwarding")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from gateway.conversation_limiter import (  # noqa: E402
    NewConversationLimiter,
    RedisNewConversationLimiter,
    resolve_client_key,
)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_MAIN_PY = REPO_ROOT / "src" / "gateway" / "main.py"


# ---------------------------------------------------------------------------
# 18. test_stream_payload_forwards_identity
# ---------------------------------------------------------------------------

class _CapturingStreamCtx:
    def __init__(self):
        pass

    async def __aenter__(self):
        resp = mock.MagicMock()
        resp.raise_for_status = mock.MagicMock()

        async def _aiter_lines():
            return
            yield  # pragma: no cover - makes this an async generator

        resp.aiter_lines = _aiter_lines
        return resp

    async def __aexit__(self, *exc_info):
        return False


class _FakeOrchestratorClient:
    def __init__(self):
        self.captured_json = None

    def stream(self, method, path, json=None, timeout=None):
        self.captured_json = json
        return _CapturingStreamCtx()


def test_stream_payload_forwards_identity(monkeypatch):
    fake_client = _FakeOrchestratorClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    monkeypatch.setattr(gw, "orchestrator_timeout", 5)

    request = gw.ChatCompletionRequest(
        model="m",
        messages=[gw.ChatMessage(role="user", content="hi")],
        stream=True,
        user="u1",
        session_id="explicit-a",
    )

    async def _run():
        async for _ in gw.stream_orchestrator_response(request, device_id="kitchen"):
            pass

    asyncio.run(_run())

    payload = fake_client.captured_json
    assert payload["user"] == "u1"
    assert payload["session_id"] == "explicit-a"
    assert payload["room"] == "kitchen"
    assert payload["extra_body"] == {"room": "kitchen"}


def test_stream_payload_omits_unset_identity(monkeypatch):
    fake_client = _FakeOrchestratorClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    monkeypatch.setattr(gw, "orchestrator_timeout", 5)

    request = gw.ChatCompletionRequest(
        model="m",
        messages=[gw.ChatMessage(role="user", content="hi")],
        stream=True,
    )

    async def _run():
        async for _ in gw.stream_orchestrator_response(request, device_id=None):
            pass

    asyncio.run(_run())

    payload = fake_client.captured_json
    assert "user" not in payload
    assert "session_id" not in payload
    assert "room" not in payload


# ---------------------------------------------------------------------------
# 19. test_responses_request_carries_identity
# 19b. test_chat_completion_request_user_field_preexists
# ---------------------------------------------------------------------------

def test_responses_request_carries_identity():
    request = gw.ResponsesAPIRequest(
        model="m",
        input=[{"role": "user", "content": "hi"}, {"role": "user", "content": "there"}],
        stream=True,
        user="u1",
        session_id="explicit-a",
    )
    chat_request = gw._responses_to_chat_request(request)

    assert isinstance(chat_request, gw.ChatCompletionRequest)
    assert chat_request.user == "u1"
    assert chat_request.session_id == "explicit-a"
    assert [m.content for m in chat_request.messages] == ["hi", "there"]


def test_chat_completion_request_user_field_preexists():
    request = gw.ChatCompletionRequest(
        model="m", messages=[gw.ChatMessage(role="user", content="hi")], user="u1"
    )
    assert request.user == "u1"


# ---------------------------------------------------------------------------
# 20. test_new_conversation_limiter
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_new_conversation_limiter():
    clock = _FakeClock()
    limiter = NewConversationLimiter(per_minute=2, max_keys=2, clock=clock)

    async def _run():
        assert await limiter.allow("A") is True
        assert await limiter.allow("A") is True
        assert await limiter.allow("A") is False

        clock.advance(61)
        assert await limiter.allow("A") is True

        assert await limiter.allow("B") is True
        assert await limiter.allow("C") is True
        assert len(limiter) <= 2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 21. test_limiter_applies_to_both_routes_first_turn_only
# ---------------------------------------------------------------------------

def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _call_lines(node: ast.AST, name: str):
    lines = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                lines.append(n.lineno)
    return lines


def test_limiter_applies_to_both_routes_first_turn_only():
    tree = ast.parse(GATEWAY_MAIN_PY.read_text())

    chat_completions = _find_function(tree, "chat_completions")
    responses_api = _find_function(tree, "responses_api")

    assert _call_lines(chat_completions, "_check_new_conversation_limit")
    assert _call_lines(responses_api, "_check_new_conversation_limit")

    chat_limit_line = _call_lines(chat_completions, "_check_new_conversation_limit")[0]
    chat_room_line = _call_lines(chat_completions, "_detect_room_from_active_satellite")[0]
    assert chat_limit_line < chat_room_line

    resp_limit_line = _call_lines(responses_api, "_check_new_conversation_limit")[0]
    resp_convert_line = _call_lines(responses_api, "_responses_to_chat_request")[0]
    resp_room_line = _call_lines(responses_api, "_detect_room_from_active_satellite")[0]
    assert resp_convert_line < resp_limit_line < resp_room_line


# ---------------------------------------------------------------------------
# F45 (reconciliation round 2, codex r2b Medium): once lifespan swaps
# new_conversation_limiter to a Redis-backed instance, a RUNTIME Redis
# failure (connection drop, timeout) must not propagate as a request error
# -- it must degrade this request to the in-memory fallback limiter.
# ---------------------------------------------------------------------------


class _RedisErrorLimiter:
    """Simulates a Redis-backed limiter whose connection has failed."""

    async def allow(self, key: str) -> bool:
        import redis.exceptions as redis_exceptions

        raise redis_exceptions.ConnectionError("redis connection lost")


def test_check_new_conversation_limit_degrades_to_memory_on_redis_error(monkeypatch):
    fallback = NewConversationLimiter(per_minute=1, max_keys=10)
    monkeypatch.setattr(gw, "new_conversation_limiter", _RedisErrorLimiter())
    monkeypatch.setattr(gw, "_new_conversation_memory_fallback", fallback)

    one_user_msg = [gw.ChatMessage(role="user", content="hi")]

    async def _run():
        # Primary (Redis) limiter raises on every call; the request must
        # not 500 -- it degrades to the in-memory fallback and still
        # enforces the per_minute=1 budget for this key.
        await gw._check_new_conversation_limit("9.9.9.9", one_user_msg, None)
        with pytest.raises(HTTPException) as exc_info:
            await gw._check_new_conversation_limit("9.9.9.9", one_user_msg, None)
        assert exc_info.value.status_code == 429

    asyncio.run(_run())


def test_check_new_conversation_limit_first_turn_only(monkeypatch):
    limiter = NewConversationLimiter(per_minute=1, max_keys=10)
    monkeypatch.setattr(gw, "new_conversation_limiter", limiter)

    one_user_msg = [gw.ChatMessage(role="user", content="hi")]
    two_user_msgs = [
        gw.ChatMessage(role="user", content="hi"),
        gw.ChatMessage(role="assistant", content="hello"),
        gw.ChatMessage(role="user", content="again"),
    ]

    async def _run():
        # First one-user-message call consumes budget and passes.
        await gw._check_new_conversation_limit("1.2.3.4", one_user_msg, None)
        # Second one-user-message call from the same host is over budget.
        with pytest.raises(HTTPException) as exc_info:
            await gw._check_new_conversation_limit("1.2.3.4", one_user_msg, None)
        assert exc_info.value.status_code == 429

        # A follow-up turn (2+ user messages) never consumes budget, even
        # though the limiter above is already exhausted for this host.
        await gw._check_new_conversation_limit("1.2.3.4", two_user_msgs, None)

        # An explicit session_id never consumes budget either.
        await gw._check_new_conversation_limit("1.2.3.4", one_user_msg, "explicit-a")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 22. test_route_level_429_on_both_routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_route_level_429_on_both_routes(monkeypatch, path):
    limiter = NewConversationLimiter(per_minute=1, max_keys=10)
    # TestClient reports the client host as "testclient"; pre-consume it.
    assert asyncio.run(limiter.allow("testclient")) is True
    monkeypatch.setattr(gw, "new_conversation_limiter", limiter)

    room_sentinel = mock.AsyncMock(side_effect=HTTPException(status_code=418))
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", room_sentinel)
    monkeypatch.setattr(gw, "validate_api_key", mock.AsyncMock(return_value=True))

    client = TestClient(gw.app)

    if path == "/v1/chat/completions":
        first_turn_body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        follow_up_body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "again"},
            ],
        }
    else:
        first_turn_body = {"model": "m", "input": [{"role": "user", "content": "hi"}]}
        follow_up_body = {
            "model": "m",
            "input": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "again"},
            ],
        }

    response = client.post(path, json=first_turn_body)
    assert response.status_code == 429
    room_sentinel.assert_not_awaited()

    response = client.post(path, json=follow_up_body)
    assert response.status_code == 418
    room_sentinel.assert_awaited()


# ---------------------------------------------------------------------------
# F39 (reconciliation round 1, codex r2 Medium): behind Traefik, every
# caller's immediate TCP peer collapses to the proxy's pod IP; two in-memory
# gateway replicas also enforce their own window inconsistently.
# ---------------------------------------------------------------------------

TRUSTED_CIDR = "10.244.0.0/16"


def test_resolve_client_key_trusted_peer_uses_forwarded_for():
    # Traefik's pod IP (inside the trusted CIDR) forwarding for a real caller.
    key = resolve_client_key("10.244.3.7", "192.168.55.50, 10.244.3.7", TRUSTED_CIDR)
    assert key == "192.168.55.50"


def test_resolve_client_key_untrusted_peer_ignores_forwarded_for():
    # A caller hitting the gateway directly (not through Traefik) can't
    # spoof another source's key via its own X-Forwarded-For header.
    key = resolve_client_key("203.0.113.9", "10.0.0.1", TRUSTED_CIDR)
    assert key == "203.0.113.9"


def test_resolve_client_key_no_forwarded_for_header():
    key = resolve_client_key("10.244.3.7", None, TRUSTED_CIDR)
    assert key == "10.244.3.7"


def test_resolve_client_key_unparseable_peer_falls_back():
    key = resolve_client_key("not-an-ip", "192.168.55.50", TRUSTED_CIDR)
    assert key == "not-an-ip"


# ---------------------------------------------------------------------------
# F44 (reconciliation round 2, codex r2b Medium): the left-most X-Forwarded-
# For hop is attacker-controlled whenever a trusted proxy in the chain
# forwards an inbound header without sanitizing it -- it only APPENDS its
# own observed peer to whatever the caller already sent. The nearest hop
# NOT inside trusted_proxy_cidrs is authoritative; walk right-to-left.
# ---------------------------------------------------------------------------


def test_resolve_client_key_ignores_spoofed_leftmost_hop_behind_trusted_proxies():
    # Attacker-supplied leftmost value ("1.2.3.4") plus a trusted internal
    # hop, with Traefik (non-sanitizing) appending the attacker's real,
    # untrusted IP at the end. Old left-most parsing would trust the
    # attacker's forged value; right-to-left parsing finds the real client.
    key = resolve_client_key(
        "10.244.3.7", "1.2.3.4, 10.244.9.1, 203.0.113.9", TRUSTED_CIDR
    )
    assert key == "203.0.113.9"


def test_resolve_client_key_all_hops_trusted_falls_back_to_peer():
    # Every hop in the chain is inside the trusted CIDR (e.g. an internal
    # health check or another in-cluster proxy) -- no untrusted hop exists,
    # so the immediate peer is the only trustworthy value.
    key = resolve_client_key("10.244.3.7", "10.244.1.1, 10.244.9.1", TRUSTED_CIDR)
    assert key == "10.244.3.7"


def test_resolve_client_key_single_hop_still_works():
    # Single-hop chain (one proxy) -- the original common case must be
    # unaffected by the right-to-left rewrite.
    key = resolve_client_key("10.244.3.7", "192.168.55.50", TRUSTED_CIDR)
    assert key == "192.168.55.50"


class _FakeRedisForLimiter:
    """Minimal async fake Redis: zadd/zcard/zremrangebyscore/expire/eval.

    F43 (reconciliation round 2): production now issues one EVAL rather
    than four separate commands, so eval() replays the same trim+count+
    conditional-add+expire semantics against the same in-memory zset. The
    four separate methods are kept for any caller still using them
    directly.
    """

    def __init__(self):
        self._zsets: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key, min_score, max_score):
        zset = self._zsets.get(key, {})
        min_val = float("-inf") if min_score == "-inf" else float(min_score)
        max_val = float("inf") if max_score == "inf" else float(max_score)
        self._zsets[key] = {
            m: s for m, s in zset.items() if not (min_val <= s <= max_val)
        }

    async def zcard(self, key):
        return len(self._zsets.get(key, {}))

    async def zadd(self, key, mapping):
        self._zsets.setdefault(key, {}).update(mapping)

    async def expire(self, key, ttl):
        return True

    async def eval(self, script, numkeys, key, window_start, now, per_minute, member, ttl):
        zset = self._zsets.get(key, {})
        window_start = float(window_start)
        zset = {m: s for m, s in zset.items() if s > window_start}
        if len(zset) >= int(per_minute):
            self._zsets[key] = zset
            return 0
        zset[member] = float(now)
        self._zsets[key] = zset
        return 1


def test_redis_backed_limiter_shares_budget_across_two_instances():
    """Two RedisNewConversationLimiter instances (simulating two gateway
    replicas) backed by the SAME Redis must share one budget per key."""
    fake_redis = _FakeRedisForLimiter()
    clock = _FakeClock()

    replica_a = RedisNewConversationLimiter(fake_redis, per_minute=2, clock=clock)
    replica_b = RedisNewConversationLimiter(fake_redis, per_minute=2, clock=clock)

    async def _run():
        assert await replica_a.allow("house") is True
        # A different replica instance, same Redis backend, same key.
        assert await replica_b.allow("house") is True
        # Budget (2/min) is now exhausted regardless of which replica asks.
        assert await replica_a.allow("house") is False
        assert await replica_b.allow("house") is False

        clock.advance(61)
        assert await replica_a.allow("house") is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# F43 (reconciliation round 2, codex r2b Medium): trim+count+add+expire must
# be one atomic Redis EVAL. Separate ZREMRANGEBYSCORE/ZCARD/ZADD/EXPIRE
# awaits let two concurrent first-turn requests both observe the same
# below-limit ZCARD before either ZADDs, letting both pass a per_minute=1
# budget. The prior coverage above is sequential only -- it never races two
# callers against the same key.
# ---------------------------------------------------------------------------


class _FakeRedisWithInterleavingLimiter:
    """Each of zremrangebyscore/zcard/zadd/expire yields at entry, so two
    concurrent callers CAN interleave between them -- this models the old
    non-atomic four-round-trip implementation and is what would let two
    racing callers both pass a per_minute=1 budget.

    eval() yields exactly once (the single network round-trip to Redis)
    then runs trim+count+conditional-add+expire as one synchronous block --
    exactly like a Lua script executing atomically once Redis receives it.
    Two concurrent eval() callers can only interleave at that one await
    boundary, never inside the atomic body.
    """

    def __init__(self):
        self._zsets: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key, min_score, max_score):
        await asyncio.sleep(0)
        zset = self._zsets.get(key, {})
        min_val = float("-inf") if min_score == "-inf" else float(min_score)
        max_val = float("inf") if max_score == "inf" else float(max_score)
        self._zsets[key] = {
            m: s for m, s in zset.items() if not (min_val <= s <= max_val)
        }

    async def zcard(self, key):
        await asyncio.sleep(0)
        return len(self._zsets.get(key, {}))

    async def zadd(self, key, mapping):
        await asyncio.sleep(0)
        self._zsets.setdefault(key, {}).update(mapping)

    async def expire(self, key, ttl):
        await asyncio.sleep(0)
        return True

    async def eval(self, script, numkeys, key, window_start, now, per_minute, member, ttl):
        await asyncio.sleep(0)  # the only yield point -- simulates network latency
        zset = self._zsets.get(key, {})
        window_start = float(window_start)
        zset = {m: s for m, s in zset.items() if s > window_start}
        if len(zset) >= int(per_minute):
            self._zsets[key] = zset
            return 0
        zset[member] = float(now)
        self._zsets[key] = zset
        return 1


def test_redis_backed_limiter_atomic_across_interleaved_requests():
    """Two coroutines racing at a per_minute=1 budget for the SAME key must
    result in exactly one True and one False -- never both True."""
    fake_redis = _FakeRedisWithInterleavingLimiter()
    clock = _FakeClock()
    limiter = RedisNewConversationLimiter(fake_redis, per_minute=1, clock=clock)

    async def _run():
        results = await asyncio.gather(
            limiter.allow("house"),
            limiter.allow("house"),
        )
        assert sorted(results) == [False, True]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Both routes hand the limiter every X-Forwarded-For line, joined in order:
# a caller's own first line can't stand in for the proxy's appended one.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_routes_join_every_forwarded_for_line(monkeypatch, path):
    seen = []

    async def _spy(client_host, messages, session_id, forwarded_for=None):
        seen.append(forwarded_for)
        raise HTTPException(status_code=418)

    monkeypatch.setattr(gw, "_check_new_conversation_limit", _spy)
    monkeypatch.setattr(gw, "validate_api_key", mock.AsyncMock(return_value=True))
    client = TestClient(gw.app)
    body = (
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        if path == "/v1/chat/completions"
        else {"model": "m", "input": [{"role": "user", "content": "hi"}]}
    )
    headers = [("x-forwarded-for", "192.0.2.5"), ("x-forwarded-for", "203.0.113.9")]
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 418
    assert seen == ["192.0.2.5, 203.0.113.9"]
