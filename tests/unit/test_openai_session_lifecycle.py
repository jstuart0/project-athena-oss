"""Red/green contract for ATHENA-88 phase 2 (F88): first-turn session reset,
memory/Redis eviction caps, and the session_hmac_secret startup gate.

Plan Phase 2 tests 13-17d. Test contract Phase 2 C13-C17, C22-C24.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import structlog

sys.path.insert(0, "src")

# See test_openai_session_key.py for why orchestrator.nodes must be imported
# before orchestrator.helpers (circular partial-init ImportError otherwise).
sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-session-lifecycle")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
import orchestrator.nodes  # noqa: E402,F401

import orchestrator.session_manager as session_manager_module
from orchestrator.session_manager import SessionManager
from orchestrator.helpers import (
    ResolvedSession,
    prepare_openai_session,
    session_hmac_secret,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"


class _FakeCacheClient:
    """Minimal stand-in for shared.cache.CacheClient: cache_client.client.delete(...)."""

    def __init__(self):
        self.client = SimpleNamespace(delete=mock.AsyncMock())


@pytest.fixture(autouse=True)
def _isolate_memory_sessions():
    session_manager_module._memory_sessions.clear()
    yield
    session_manager_module._memory_sessions.clear()


@pytest.fixture(autouse=True)
def _patch_conversation_settings(monkeypatch):
    async def _fake_get_config():
        return SimpleNamespace(
            get_conversation_settings=mock.AsyncMock(
                return_value={"session_ttl_seconds": 3600}
            )
        )

    monkeypatch.setattr(session_manager_module, "get_config", _fake_get_config)


def _fake_athena_config(session_max_count: int = 5000):
    return SimpleNamespace(session_max_count=session_max_count)


# ---------------------------------------------------------------------------
# 13. test_first_turn_resets_same_opener
# ---------------------------------------------------------------------------

def test_first_turn_resets_same_opener(monkeypatch):
    """Regression guard for the reset itself, with F38's grace window
    explicitly disabled (reset_grace_seconds=0) -- a genuinely new
    conversation reusing the same opener (no recent session under this
    fingerprint, or the grace window has elapsed) must still be reset. The
    "recent retry keeps history" case is covered separately below."""
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = None
        cache = _FakeCacheClient()

        resolved_a = ResolvedSession("oai-sameopener00000000000000000000", "fingerprint", True)
        await prepare_openai_session(resolved_a, sm, cache, max_count=5000, reset_grace_seconds=0)
        session_a = await sm.get_or_create_session(session_id=resolved_a.session_id)
        await sm.add_message(resolved_a.session_id, "user", "what place has happy hour?")
        await sm.add_message(resolved_a.session_id, "assistant", "Here are a few...")

        resolved_b = ResolvedSession("oai-sameopener00000000000000000000", "fingerprint", True)
        await prepare_openai_session(resolved_b, sm, cache, max_count=5000, reset_grace_seconds=0)
        session_b = await sm.get_or_create_session(session_id=resolved_b.session_id)

        assert len(session_b.messages) == 0
        cache.client.delete.assert_awaited_with(f"athena:context:{resolved_a.session_id}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# F38 (reconciliation round 1, codex r2 Medium): a truncated HA ASR retry
# resends the same single-user-message opener for the SAME turn. Without a
# grace window, "exactly one user message" alone can't tell that apart from
# a genuinely new conversation, and the unconditional reset fragments (or,
# if the truncated text happens to equal the opener, wipes) a still-live
# conversation.
# ---------------------------------------------------------------------------

def test_truncated_retry_within_grace_window_keeps_history(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = None
        cache = _FakeCacheClient()

        session_id = "oai-truncatedretry0000000000000000"
        resolved_a = ResolvedSession(session_id, "fingerprint", True)
        await prepare_openai_session(resolved_a, sm, cache, max_count=5000)
        await sm.get_or_create_session(session_id=session_id)
        await sm.add_message(session_id, "user", "what place has happy hour?")
        await sm.add_message(session_id, "assistant", "Here are a few...")

        # The initial turn's own prepare_openai_session call issues an
        # unconditional (no-op, since nothing existed yet) delete -- reset
        # the spy so the assertion below is scoped to the retry only.
        cache.client.delete.reset_mock()

        # HA's truncated-ASR retry: same fingerprint, still exactly one user
        # message, arriving moments later (well within the default 120s
        # grace window) -- must NOT reset.
        resolved_retry = ResolvedSession(session_id, "fingerprint", True)
        await prepare_openai_session(resolved_retry, sm, cache, max_count=5000)
        session_after_retry = await sm.get_or_create_session(session_id=session_id)

        assert len(session_after_retry.messages) == 2
        cache.client.delete.assert_not_awaited()

    asyncio.run(_run())


def test_new_conversation_after_grace_window_still_resets(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        from datetime import datetime, timedelta

        sm = SessionManager()
        sm.redis_client = None
        cache = _FakeCacheClient()

        session_id = "oai-staleopener00000000000000000000"
        resolved_a = ResolvedSession(session_id, "fingerprint", True)
        await prepare_openai_session(resolved_a, sm, cache, max_count=5000)
        session_a = await sm.get_or_create_session(session_id=session_id)
        await sm.add_message(session_id, "user", "what place has happy hour?")
        await sm.add_message(session_id, "assistant", "Here are a few...")

        # Backdate the session's creation time past the grace window, so
        # this looks like a genuinely new conversation reusing the same
        # opener, not a fresh retry of the turn that created it.
        stale_session = await sm.get_session(session_id)
        stale_session.created_at = datetime.utcnow() - timedelta(seconds=200)
        await sm._save_session(stale_session)

        resolved_new = ResolvedSession(session_id, "fingerprint", True)
        await prepare_openai_session(resolved_new, sm, cache, max_count=5000, reset_grace_seconds=120)
        session_after_reset = await sm.get_or_create_session(session_id=session_id)

        assert len(session_after_reset.messages) == 0
        cache.client.delete.assert_awaited_with(f"athena:context:{session_id}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 14. test_follow_up_turn_is_not_reset
# ---------------------------------------------------------------------------

def test_follow_up_turn_is_not_reset(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = None
        cache = _FakeCacheClient()

        session_id = "oai-followup000000000000000000000"
        resolved_a = ResolvedSession(session_id, "fingerprint", True)
        await prepare_openai_session(resolved_a, sm, cache, max_count=5000)
        await sm.get_or_create_session(session_id=session_id)
        await sm.add_message(session_id, "user", "what place has happy hour?")
        await sm.add_message(session_id, "assistant", "Here are a few...")

        resolved_c = ResolvedSession(session_id, "fingerprint", False)
        await prepare_openai_session(resolved_c, sm, cache, max_count=5000)
        session_c = await sm.get_or_create_session(session_id=session_id)

        assert len(session_c.messages) == 2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 15. test_explicit_id_never_reset
# ---------------------------------------------------------------------------

def test_explicit_id_never_reset(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = None
        cache = _FakeCacheClient()

        session_id = "explicit-abc"
        await sm.get_or_create_session(session_id=session_id)
        await sm.add_message(session_id, "user", "hi")

        resolved = ResolvedSession(session_id, "explicit", True)
        await prepare_openai_session(resolved, sm, cache, max_count=5000)
        session = await sm.get_or_create_session(session_id=session_id)

        assert len(session.messages) == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 16. test_memory_store_capped_oldest_first
# ---------------------------------------------------------------------------

def test_memory_store_capped_oldest_first(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config(session_max_count=3)
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = None

        for i in range(4):
            session = session_manager_module.ConversationSession(session_id=f"sess-{i}")
            session.last_activity = session.last_activity.replace(microsecond=i)
            await sm._save_session(session)

        assert len(session_manager_module._memory_sessions) == 3
        assert "sess-0" not in session_manager_module._memory_sessions

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 17. test_redis_index_evicts_oldest_and_their_context
# 17b. test_eviction_clears_memory_fallback_session
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Minimal async fake Redis: zadd/zcard/zpopmin/delete/get/setex."""

    def __init__(self, fail_setex_for=None):
        self._zset: dict[str, float] = {}
        self._store: dict[str, str] = {}
        self._fail_setex_for = fail_setex_for or set()
        self.delete_calls = []

    async def zadd(self, key, mapping):
        self._zset.update(mapping)

    async def zcard(self, key):
        return len(self._zset)

    async def zpopmin(self, key):
        if not self._zset:
            return []
        member = min(self._zset, key=lambda m: self._zset[m])
        score = self._zset.pop(member)
        return [(member, score)]

    async def delete(self, key):
        self.delete_calls.append(key)
        self._store.pop(key, None)

    async def get(self, key):
        return self._store.get(key)

    async def setex(self, key, ttl, value):
        session_id = key.split(":")[-1]
        if session_id in self._fail_setex_for:
            raise ConnectionError("simulated redis outage")
        self._store[key] = value


def test_redis_index_evicts_oldest_and_their_context(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = _FakeRedis()
        cache = _FakeCacheClient()

        delete_spy = mock.AsyncMock(wraps=sm.delete_session)
        monkeypatch.setattr(sm, "delete_session", delete_spy)

        from orchestrator.context.storage import clear_conversation_context

        ids = ["oai-a", "oai-b", "oai-c", "oai-d"]
        evicted_all = []
        for session_id in ids:
            evicted = await sm.register_bounded_session(session_id, max_count=3)
            evicted_all.extend(evicted)
            for eid in evicted:
                # This is what prepare_openai_session does with the evicted
                # ids register_bounded_session returns.
                await clear_conversation_context(cache, eid)

        assert evicted_all == ["oai-a"]
        delete_spy.assert_awaited_once_with("oai-a")
        cache.client.delete.assert_awaited_with("athena:context:oai-a")
        assert await sm.redis_client.zcard("athena:session:oai_index") == 3

    asyncio.run(_run())


def test_eviction_clears_memory_fallback_session(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "_get_athena_config", lambda: _fake_athena_config()
    )

    async def _run():
        sm = SessionManager()
        sm.redis_client = _FakeRedis(fail_setex_for={"first-id"})

        # first-id's setex fails -> falls back to _memory_sessions.
        s = session_manager_module.ConversationSession(session_id="first-id")
        await sm._save_session(s)
        assert "first-id" in session_manager_module._memory_sessions

        for session_id in ["first-id", "second-id", "third-id", "fourth-id"]:
            await sm.register_bounded_session(session_id, max_count=3)

        assert "first-id" not in session_manager_module._memory_sessions
        assert await sm.get_session("first-id") is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 17c. test_session_hmac_secret_gate
# ---------------------------------------------------------------------------

def test_session_hmac_secret_gate():
    key_config = SimpleNamespace(service_api_key="k", dev_mode=False)
    assert session_hmac_secret(key_config) == b"k"

    empty_config = SimpleNamespace(service_api_key="", dev_mode=False)
    with pytest.raises(RuntimeError):
        session_hmac_secret(empty_config)

    placeholder_config = SimpleNamespace(
        service_api_key="dev-service-key-change-in-production", dev_mode=False
    )
    with pytest.raises(RuntimeError):
        session_hmac_secret(placeholder_config)

    dev_config = SimpleNamespace(service_api_key="", dev_mode=True)
    with structlog.testing.capture_logs() as logs:
        first = session_hmac_secret(dev_config)
        second = session_hmac_secret(dev_config)

    assert len(first) == 64
    assert first == second
    warnings = [e for e in logs if e.get("event") == "openai_session_hmac_ephemeral_secret"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 17d. test_lifespan_gates_on_session_secret_first
# ---------------------------------------------------------------------------

def test_lifespan_gates_on_session_secret_first():
    tree = ast.parse(MAIN_PY.read_text())
    lifespan_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            lifespan_node = node
            break
    assert lifespan_node is not None

    secret_call_line = None
    for node in ast.walk(lifespan_node):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "session_hmac_secret":
                secret_call_line = node.lineno
                break
    assert secret_call_line is not None, "lifespan must call session_hmac_secret("

    # It must sit inside a Try with an `except RuntimeError` that raises SystemExit.
    found_try_guard = False
    for node in ast.walk(lifespan_node):
        if isinstance(node, ast.Try):
            call_lines = {
                n.lineno for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and (
                    (isinstance(n.func, ast.Name) and n.func.id == "session_hmac_secret")
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == "session_hmac_secret")
                )
            }
            if not call_lines:
                continue
            for handler in node.handlers:
                handler_type_name = getattr(handler.type, "id", None)
                if handler_type_name == "RuntimeError":
                    raises_system_exit = any(
                        isinstance(n, ast.Raise)
                        and isinstance(n.exc, ast.Call)
                        and isinstance(n.exc.func, ast.Name)
                        and n.exc.func.id == "SystemExit"
                        for n in ast.walk(handler)
                    )
                    if raises_system_exit:
                        found_try_guard = True
    assert found_try_guard

    first_set_call_line = None
    for node in ast.walk(lifespan_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith("set_") and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "_runtime":
                first_set_call_line = node.lineno
                break
    assert first_set_call_line is not None
    assert secret_call_line < first_set_call_line
