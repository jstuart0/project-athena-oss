"""Red/green contract for ATHENA-88 phase 2 (F88): per-conversation OpenAI
session key resolution.

Plan: .mozart/plans/active/2026-09-26-deliver-athena-voice-intent-defects.md,
Phase 2, tests 1-12. Test contract: same directory,
2026-09-26-deliver-athena-voice-intent-defects.test-contract.md, Phase 2
C1-C12 (r1/r2 amendments).
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from shared.knowledge_tiers import KnowledgeAudience
from unittest import mock

import pytest
import structlog

sys.path.insert(0, "src")

# Stub heavy/absent deps *before* the first orchestrator.* import.
#
# - langgraph / langgraph.graph / prometheus_client aren't installed in the
#   unit-test environment.
# - orchestrator.config_loader must be mocked here, first, not after
#   orchestrator.nodes/orchestrator.helpers: those already import
#   `from orchestrator.config_loader import ADMIN_API_URL` (helpers.py) and
#   would otherwise load the REAL config_loader (DB-driven, needs a live
#   admin backend) before this module gets a chance to install the mock —
#   sys.modules.setdefault would then be a no-op. Registering the mock first
#   makes every later `from orchestrator.config_loader import X` resolve
#   against it instead (same pattern as tests/unit/test_health_probes.py).
# - orchestrator.nodes must be imported before orchestrator.helpers —
#   helpers.py's own `from orchestrator.nodes import _runtime` otherwise
#   races nodes/__init__.py's `from orchestrator.helpers import
#   maybe_post_synthesis_fallback` (via finalize.py) into a circular
#   partial-init ImportError. See also helpers.py's "Import contract" note.
for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()
os.environ.setdefault("SERVICE_API_KEY", "test-key-session-key")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

from shared.config import get_config as _shared_get_config  # noqa: E402
import shared.config as _shared_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _shared_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules.setdefault("orchestrator.config_loader", _config_loader_mock)

import orchestrator.nodes  # noqa: E402,F401

from orchestrator.helpers import resolve_openai_session, ResolvedSession, session_hmac_secret  # noqa: E402

import orchestrator.main as _main_module  # noqa: E402
import orchestrator.session_manager as _session_manager_module  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.session_manager import SessionManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"


def _service_headers() -> dict:
    """ATHENA-89 / D10: the gated routes this file posts to now require
    X-Service-Key. Read via get_config() at call time, never a hardcoded
    literal (r3 test-harness hygiene note), so this tracks whatever env
    this test session actually configured."""
    return {"X-Service-Key": _shared_config.get_config().service_api_key}


@pytest.fixture(autouse=True)
def _reset_runtime_between_tests():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()

SECRET = b"test-secret"


def _msg(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content)


def _resolve(messages, **kwargs):
    kwargs.setdefault("top_level_session_id", None)
    kwargs.setdefault("extra_body", None)
    kwargs.setdefault("user", None)
    kwargs.setdefault("room", None)
    kwargs.setdefault("secret", SECRET)
    return resolve_openai_session(messages, **kwargs)


# ---------------------------------------------------------------------------
# 1. test_fingerprint_stable_across_replayed_turns
# ---------------------------------------------------------------------------

def test_fingerprint_stable_across_replayed_turns():
    sys_msg = _msg("system", "You are Athena.")
    u1 = _msg("user", "what plae have happy our and outdoor seating?")
    a1 = _msg("assistant", "Here are a few options...")
    u2 = _msg("user", "the second one")
    a2 = _msg("assistant", "Got it, booking that.")
    u3 = _msg("user", "yes please")

    histories = [
        [sys_msg, u1],
        [sys_msg, u1, a1, u2],
        [sys_msg, u1, a1, u2, a2, u3],
    ]

    results = [_resolve(h, room="kitchen", user="alice") for h in histories]

    ids = {r.session_id for r in results}
    assert len(ids) == 1
    session_id = next(iter(ids))
    assert re.match(r"^oai-[0-9a-f]{32}$", session_id)
    assert all(r.source == "fingerprint" for r in results)
    assert [r.is_first_turn for r in results] == [True, False, False]


# ---------------------------------------------------------------------------
# 2. test_system_prompt_changes_do_not_change_key
# ---------------------------------------------------------------------------

def test_system_prompt_changes_do_not_change_key():
    u1 = _msg("user", "turn on the lights")
    a = _resolve([_msg("system", "time is 22:09"), u1], room="kitchen", user="alice")
    b = _resolve([_msg("system", "time is 22:10"), u1], room="kitchen", user="alice")
    assert a.session_id == b.session_id


# ---------------------------------------------------------------------------
# 3. test_different_conversations_isolated
# ---------------------------------------------------------------------------

def test_different_conversations_isolated():
    opener_a = [_msg("user", "what's the weather")]
    opener_b = [_msg("user", "turn off the lights")]
    assert _resolve(opener_a, room="kitchen", user="alice").session_id != _resolve(
        opener_b, room="kitchen", user="alice"
    ).session_id

    same_opener = [_msg("user", "what's the weather")]
    alice = _resolve(same_opener, room="kitchen", user="alice")
    bob = _resolve(same_opener, room="kitchen", user="bob")
    none_user = _resolve(same_opener, room="kitchen", user=None)
    ids = {alice.session_id, bob.session_id, none_user.session_id}
    assert len(ids) == 3

    # ATHENA-89 / D11 amendment (O10, O13): this assertion is DELIBERATELY
    # FLIPPED from `!=` to `==`. Pre-D11, room was always part of the
    # fingerprint key, so same user + different room produced different
    # ids. D11's identity precedence puts `user` first: when `user` is
    # present, room never enters the key at all, so satellite room-detection
    # flapping between turns can't fragment one HA conversation. O11 below
    # is the complement: same test shape with user=None, where room DOES
    # still differentiate the key.
    kitchen = _resolve(same_opener, room="kitchen", user="alice")
    office = _resolve(same_opener, room="office", user="alice")
    assert kitchen.session_id == office.session_id


# ---------------------------------------------------------------------------
# O11 (D11): the complement of O10 -- with NO user, room still differentiates
# ---------------------------------------------------------------------------

def test_no_user_different_room_gives_different_id():
    same_opener = [_msg("user", "what's the weather")]
    kitchen = _resolve(same_opener, room="kitchen", user=None)
    office = _resolve(same_opener, room="office", user=None)
    assert kitchen.session_id != office.session_id


# ---------------------------------------------------------------------------
# O12 (D11): no user, room "unknown"/"" both collapse to the same room-less
# fingerprint -- "office" is no longer reachable through any code path.
# ---------------------------------------------------------------------------

def test_no_user_no_real_room_collapses_to_roomless_key_logs_weak_identity():
    same_opener = [_msg("user", "what's the weather")]

    calls = []
    import orchestrator.helpers as helpers_module
    original_info = helpers_module.logger.info
    helpers_module.logger.info = lambda event, **kw: calls.append({"event": event, **kw})
    try:
        unknown_room = _resolve(same_opener, room="unknown", user=None)
        empty_room = _resolve(same_opener, room="", user=None)
    finally:
        helpers_module.logger.info = original_info

    assert unknown_room.session_id == empty_room.session_id
    assert unknown_room.identity_kind == "none"

    weak_events = [c for c in calls if c["event"] == "openai_session_identity_weak"]
    assert len(weak_events) == 2
    assert all(c["reason"] == "no_user_no_room" for c in weak_events)


# ---------------------------------------------------------------------------
# 4. test_key_is_hmac_not_bare_hash
# ---------------------------------------------------------------------------

def test_key_is_hmac_not_bare_hash():
    import hashlib

    messages = [_msg("user", "what's the weather")]
    a = resolve_openai_session(
        messages, top_level_session_id=None, extra_body=None,
        user=None, room="kitchen", secret=b"a",
    )
    b = resolve_openai_session(
        messages, top_level_session_id=None, extra_body=None,
        user=None, room="kitchen", secret=b"b",
    )
    assert a.session_id != b.session_id

    bare_sha = "oai-" + hashlib.sha256(
        "kitchen\x00\x00what's the weather".encode("utf-8")
    ).hexdigest()[:32]
    assert a.session_id != bare_sha
    assert b.session_id != bare_sha


# ---------------------------------------------------------------------------
# 5. test_explicit_session_id_accepted
# ---------------------------------------------------------------------------

def test_explicit_session_id_accepted():
    messages = [_msg("user", "hi")]

    top_level = _resolve(messages, top_level_session_id="explicit-jarvis_1.2:3")
    assert top_level.session_id == "explicit-jarvis_1.2:3"
    assert top_level.source == "explicit"
    assert top_level.is_first_turn is False

    via_extra_body = _resolve(messages, extra_body={"session_id": "explicit-x"})
    assert via_extra_body.session_id == "explicit-x"
    assert via_extra_body.source == "explicit"

    both_set = _resolve(
        messages,
        top_level_session_id="explicit-top",
        extra_body={"session_id": "explicit-extra"},
    )
    assert both_set.session_id == "explicit-top"

    boundary = "explicit-" + "a" * 55
    assert len(boundary) == 64
    result = _resolve(messages, top_level_session_id=boundary)
    assert result.session_id == boundary


# ---------------------------------------------------------------------------
# 6. test_invalid_explicit_session_id_rejected
# ---------------------------------------------------------------------------

INVALID_IDS = [
    "a b",
    "explicit-" + "a" * 56,  # 65 chars total
    123,
    "",
    "k\n",
    "openwebui-session",
    "ha-voice-assistant",
    "oai-0123456789abcdef0123456789abcdef",
    "jarvis-abc",
]


def test_invalid_ids_population_floor():
    assert len(INVALID_IDS) == 9


@pytest.mark.parametrize("bad_id", INVALID_IDS, ids=[repr(x) for x in INVALID_IDS])
def test_invalid_explicit_session_id_rejected(bad_id):
    messages = [_msg("user", "hi")]
    with structlog.testing.capture_logs() as logs:
        result = _resolve(messages, top_level_session_id=bad_id, room="kitchen", user="alice")

    assert result.source == "fingerprint"
    assert result.session_id.startswith("oai-")
    warnings = [e for e in logs if e.get("event") == "openai_session_id_rejected"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 7. test_missing_extra_body_falls_back_to_fingerprint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra_body", [None, {}])
def test_missing_extra_body_falls_back_to_fingerprint(extra_body):
    messages = [_msg("user", "hi")]
    with structlog.testing.capture_logs() as logs:
        result = _resolve(messages, extra_body=extra_body)

    assert result.source == "fingerprint"
    rejections = [e for e in logs if e.get("event") == "openai_session_id_rejected"]
    assert rejections == []


# ---------------------------------------------------------------------------
# 8. test_never_returns_legacy_shared_ids
# ---------------------------------------------------------------------------

def test_never_returns_legacy_shared_ids():
    messages = [_msg("user", "hi")]
    all_results = []
    for bad_id in INVALID_IDS:
        all_results.append(_resolve(messages, top_level_session_id=bad_id))
    all_results.append(_resolve(messages))
    all_results.append(_resolve(messages, top_level_session_id="explicit-ok"))

    for r in all_results:
        assert r.session_id not in {"openwebui-session", "ha-voice-assistant"}
        assert len(r.session_id) <= 64


# ---------------------------------------------------------------------------
# 9. test_no_literal_shared_session_ids
# ---------------------------------------------------------------------------

def test_no_literal_shared_session_ids():
    tree = ast.parse(MAIN_PY.read_text())
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "openwebui-session" not in literals
    assert "ha-voice-assistant" not in literals


# ---------------------------------------------------------------------------
# 10. test_chat_completions_resolves_once_and_uses_it_in_both_branches
# ---------------------------------------------------------------------------

def _find_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls_named(node: ast.AST, name: str):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id == name:
                yield n
            elif isinstance(n.func, ast.Attribute) and n.func.attr == name:
                yield n


def test_chat_completions_resolves_once_and_uses_it_in_both_branches():
    tree = ast.parse(MAIN_PY.read_text())
    func = _find_function(tree, "chat_completions")

    resolve_calls = list(_calls_named(func, "resolve_openai_session"))
    prepare_calls = list(_calls_named(func, "prepare_openai_session"))
    assert len(resolve_calls) == 1
    assert len(prepare_calls) == 1

    stream_if = None
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Attribute) and test.attr == "stream":
                stream_if = node
                break
    assert stream_if is not None, "expected an `if request.stream:` node"

    assert resolve_calls[0].lineno < stream_if.lineno
    assert prepare_calls[0].lineno < stream_if.lineno

    # get_or_create_session(session_id=...) and QueryRequest(session_id=...)
    # must read the resolved value (an ast.Attribute/ast.Name), not a literal.
    session_id_kw_values = []
    for call in _calls_named(func, "get_or_create_session"):
        for kw in call.keywords:
            if kw.arg == "session_id":
                session_id_kw_values.append(kw.value)
    for call in _calls_named(func, "QueryRequest"):
        for kw in call.keywords:
            if kw.arg == "session_id":
                session_id_kw_values.append(kw.value)

    assert session_id_kw_values, "expected session_id keyword args in chat_completions"
    for value in session_id_kw_values:
        assert isinstance(value, (ast.Attribute, ast.Name)), ast.dump(value)
        if isinstance(value, ast.Constant):
            raise AssertionError("session_id must not be a literal constant")


# ---------------------------------------------------------------------------
# 11. test_openai_chat_request_accepts_identity_fields
# ---------------------------------------------------------------------------

def test_openai_chat_request_accepts_identity_fields():
    tree = ast.parse(MAIN_PY.read_text())
    class_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatRequest":
            class_node = node
            break
    assert class_node is not None

    annotated_fields = {
        stmt.target.id
        for stmt in class_node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    assert {"user", "session_id", "room"} <= annotated_fields


# ---------------------------------------------------------------------------
# 12. test_non_stream_branch_passes_resolved_session (binding, C10)
#
# Tests 9 and 10 prove *shape*: no literal legacy string, and the two call
# sites pass a variable literally named session_id. Neither proves that
# variable is bound to resolve_openai_session's actual return value at the
# point of use. This test runs the real /v1/chat/completions endpoint (via
# TestClient) with a real memory-mode SessionManager and asserts on the
# actual QueryRequest.session_id two different requests receive.
# ---------------------------------------------------------------------------

class _FakeSessionCacheClient:
    """Minimal stand-in for shared.cache.CacheClient: .client.delete(...)."""

    def __init__(self):
        self.client = SimpleNamespace(
            delete=mock.AsyncMock(),
            get=mock.AsyncMock(return_value=None),
            setex=mock.AsyncMock(),
        )


def test_non_stream_branch_passes_resolved_session():
    sm = SessionManager()
    sm.redis_client = None
    _runtime.set_session_manager(sm)
    _runtime.set_cache_client(_FakeSessionCacheClient())

    captured_requests = []

    async def _fake_process_query(query_request):
        captured_requests.append(query_request)
        return SimpleNamespace(request_id="req-fake", answer="ok")

    original_process_query = _main_module.process_query
    _main_module.process_query = _fake_process_query
    try:
        client = TestClient(_main_module.app)

        opener_a = "what place has happy hour and outdoor seating?"
        opener_b = "turn off the lights"

        resp_a1 = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": opener_a}], "stream": False},
            headers=_service_headers(),
        )
        resp_b = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": opener_b}], "stream": False},
            headers=_service_headers(),
        )
        # Replay of the first opener (e.g. a second, independent
        # conversation starting the same way) must resolve to the same id.
        resp_a2 = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": opener_a}], "stream": False},
            headers=_service_headers(),
        )
    finally:
        _main_module.process_query = original_process_query

    assert resp_a1.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a2.status_code == 200
    assert len(captured_requests) == 3

    session_id_a1 = captured_requests[0].session_id
    session_id_b = captured_requests[1].session_id
    session_id_a2 = captured_requests[2].session_id

    assert session_id_a1 != session_id_b
    assert session_id_a1 == session_id_a2

    secret = session_hmac_secret(_shared_config.get_config())
    expected_a = resolve_openai_session(
        [SimpleNamespace(role="user", content=opener_a)],
        top_level_session_id=None, extra_body=None, user=None, room=None,
        secret=secret,
    )
    expected_b = resolve_openai_session(
        [SimpleNamespace(role="user", content=opener_b)],
        top_level_session_id=None, extra_body=None, user=None, room=None,
        secret=secret,
    )
    assert session_id_a1 == expected_a.session_id
    assert session_id_b == expected_b.session_id


# ---------------------------------------------------------------------------
# F36 (reconciliation round 1, codex r2 High): streaming branches must
# persist through SessionManager, not mutate the in-memory
# ConversationSession directly — a bare session.add_message() is invisible
# to the next get_or_create_session() call, so the next turn replayed no
# history despite the "Session ... updated" log claiming otherwise.
#
# ConversationSession.to_dict() does not deep-copy "messages" — a memory-
# mode SessionManager's _memory_sessions dict ends up holding the SAME list
# object as the live ConversationSession, so a raw session.add_message()
# mutation is (accidentally) visible on the next memory-mode read even
# without a save, masking the defect. Redis-backed storage has no such
# aliasing (a save writes an immutable JSON snapshot), which is what
# production actually uses, so this test uses a minimal fake Redis to
# reproduce the real failure mode.
# ---------------------------------------------------------------------------

class _FakeRedisForSessionPersistence:
    """Minimal async fake Redis: get/setex (sessions) + zadd/zcard/zpopmin
    (the oai_index register_bounded_session touches on every request)."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._zset: dict[str, float] = {}

    async def get(self, key):
        return self._store.get(key)

    async def setex(self, key, ttl, value):
        self._store[key] = value

    async def delete(self, key):
        self._store.pop(key, None)

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

    async def eval(self, script, numkeys, key, score, member, max_count):
        """ATHENA-88 / F40: register_bounded_session now calls EVAL
        (one atomic register+evict), not separate zadd/zcard/zpopmin."""
        self._zset[member] = float(score)
        max_count = int(max_count)
        evicted = []
        while len(self._zset) > max_count:
            oldest = min(self._zset, key=lambda m: self._zset[m])
            del self._zset[oldest]
            evicted.append(oldest)
        return evicted


def test_streaming_endpoint_persists_session_history(monkeypatch):
    async def _fake_sm_get_config():
        return SimpleNamespace(
            get_conversation_settings=mock.AsyncMock(return_value={"session_ttl_seconds": 3600})
        )
    monkeypatch.setattr(_session_manager_module, "get_config", _fake_sm_get_config)

    sm = SessionManager()
    sm.redis_client = _FakeRedisForSessionPersistence()
    _runtime.set_session_manager(sm)
    _runtime.set_cache_client(_FakeSessionCacheClient())

    fake_state = SimpleNamespace(
        answer="Hello there!", intent=SimpleNamespace(value="general_info"), request_id="req-1", error=None,
    )
    monkeypatch.setattr(
        _main_module, "run_orchestrator_for_streaming", mock.AsyncMock(return_value=fake_state)
    )
    monkeypatch.setattr(
        _main_module,
        "resolve_request_authorization",
        mock.AsyncMock(return_value=SimpleNamespace(
            mode="owner", permissions={}, server_mode="owner", degraded=False,
            escalation_ignored=False, mode_info={"mode": "owner", "permissions": {}},
            knowledge_audience=KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=False, owner_proven=False),
        )),
    )
    fake_conv_config = SimpleNamespace(
        get_conversation_settings=mock.AsyncMock(return_value={"enabled": False})
    )
    monkeypatch.setattr(_main_module, "get_config", mock.AsyncMock(return_value=fake_conv_config))

    client = TestClient(_main_module.app)
    opener = "what's the weather"
    body = {"model": "m", "messages": [{"role": "user", "content": opener}], "stream": True}

    with client.stream("POST", "/v1/chat/completions", json=body, headers=_service_headers()) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass  # drain the SSE stream

    secret = session_hmac_secret(_shared_config.get_config())
    resolved = resolve_openai_session(
        [SimpleNamespace(role="user", content=opener)],
        top_level_session_id=None, extra_body=None, user=None, room=None,
        secret=secret,
    )

    async def _fetch():
        return await sm.get_session(resolved.session_id)

    persisted = _run_asyncio_test_helper(_fetch())
    assert persisted is not None, "session was never persisted — the streaming turn's history is lost"
    assert len(persisted.messages) == 2
    assert persisted.messages[0]["role"] == "user"
    assert persisted.messages[0]["content"] == opener
    assert persisted.messages[1]["role"] == "assistant"
    assert persisted.messages[1]["content"] == "Hello there!"


class _FakeLLMRouterForStreaming:
    """ATHENA-88 / F46 (reconciliation round 2, codex r2b Low): the
    precomputed-answer branch above only exercises the fake-streaming path
    (state.answer truthy). Reverting the true-LLM-streaming persistence fix
    at the `else` branch (build_synthesis_prompt_for_streaming +
    llm.generate_stream) would still pass that test. This fake models the
    real LLMRouter.generate_stream contract: an async generator yielding
    {"token": str, "done": bool} chunks.
    """

    async def generate_stream(self, model, prompt, system_prompt, temperature, max_tokens):
        for token in ("Hello", " there", "!"):
            yield {"token": token, "done": False}
        yield {"token": "", "done": True}


def test_true_streaming_endpoint_persists_session_history(monkeypatch):
    """F46: state.answer is falsy (no handler pre-computed an answer), so
    the streaming generator takes the true-LLM-streaming branch, not the
    fake-streaming precomputed-answer branch tested above."""
    async def _fake_sm_get_config():
        return SimpleNamespace(
            get_conversation_settings=mock.AsyncMock(return_value={"session_ttl_seconds": 3600})
        )
    monkeypatch.setattr(_session_manager_module, "get_config", _fake_sm_get_config)

    sm = SessionManager()
    sm.redis_client = _FakeRedisForSessionPersistence()
    _runtime.set_session_manager(sm)
    _runtime.set_cache_client(_FakeSessionCacheClient())
    _runtime.set_llm_router(_FakeLLMRouterForStreaming())

    fake_state = SimpleNamespace(
        answer=None,
        intent=SimpleNamespace(value="general_info"),
        request_id="req-2",
        retrieved_data=None,
        temperature=0.5,
        error=None,
    )
    monkeypatch.setattr(
        _main_module, "run_orchestrator_for_streaming", mock.AsyncMock(return_value=fake_state)
    )
    monkeypatch.setattr(
        _main_module,
        "resolve_request_authorization",
        mock.AsyncMock(return_value=SimpleNamespace(
            mode="owner", permissions={}, server_mode="owner", degraded=False,
            escalation_ignored=False, mode_info={"mode": "owner", "permissions": {}},
            knowledge_audience=KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=False, owner_proven=False),
        )),
    )
    monkeypatch.setattr(
        _main_module,
        "build_synthesis_prompt_for_streaming",
        mock.AsyncMock(return_value=("full prompt", "some-model", "system prompt")),
    )
    fake_conv_config = SimpleNamespace(
        get_conversation_settings=mock.AsyncMock(return_value={"enabled": False})
    )
    monkeypatch.setattr(_main_module, "get_config", mock.AsyncMock(return_value=fake_conv_config))

    client = TestClient(_main_module.app)
    opener = "tell me something interesting"
    body = {"model": "m", "messages": [{"role": "user", "content": opener}], "stream": True}

    with client.stream("POST", "/v1/chat/completions", json=body, headers=_service_headers()) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass  # drain the SSE stream

    secret = session_hmac_secret(_shared_config.get_config())
    resolved = resolve_openai_session(
        [SimpleNamespace(role="user", content=opener)],
        top_level_session_id=None, extra_body=None, user=None, room=None,
        secret=secret,
    )

    async def _fetch():
        return await sm.get_session(resolved.session_id)

    persisted = _run_asyncio_test_helper(_fetch())
    assert persisted is not None, "session was never persisted — the true-streaming turn's history is lost"
    assert len(persisted.messages) == 2
    assert persisted.messages[0]["role"] == "user"
    assert persisted.messages[0]["content"] == opener
    assert persisted.messages[1]["role"] == "assistant"
    assert persisted.messages[1]["content"] == "Hello there!"


def _run_asyncio_test_helper(coro):
    import asyncio
    return asyncio.run(coro)
