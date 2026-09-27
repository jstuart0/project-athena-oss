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
from unittest import mock

import pytest
import structlog

sys.path.insert(0, "src")

# Stub heavy/absent deps and pre-import orchestrator.nodes before
# orchestrator.helpers — helpers.py's `from orchestrator.nodes import
# _runtime` (line ~29) otherwise races nodes/__init__.py's own
# `from orchestrator.helpers import maybe_post_synthesis_fallback` (via
# finalize.py) into a circular partial-init ImportError. Importing
# orchestrator.nodes first lets it pull in a fresh orchestrator.helpers
# to completion before this module ever touches it directly.
sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-session-key")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
import orchestrator.nodes  # noqa: E402,F401

from orchestrator.helpers import resolve_openai_session, ResolvedSession  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"

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

    kitchen = _resolve(same_opener, room="kitchen", user="alice")
    office = _resolve(same_opener, room="office", user="alice")
    assert kitchen.session_id != office.session_id


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
