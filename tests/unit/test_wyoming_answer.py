"""The Wyoming bridge speaks the orchestrator's answer.

The orchestrator's /query response model carries the text in `answer`
(QueryResponse); the bridge read `response`, so every Wyoming reply --
including the fan-out rewording ("... To do it, say: ...") that Wyoming
satellites get instead of a confirmation prompt -- was an empty string.
"""
from __future__ import annotations

import asyncio
import sys
from unittest import mock

import pytest

sys.path.insert(0, "src")

wyoming_bridge = pytest.importorskip("gateway.wyoming_bridge")

REWORDING = "That would turn off 11 lights in the office. To do it, say: turn off all the office lights."


class TestOrchestratorAnswerText:
    def test_reads_answer(self):
        assert wyoming_bridge.orchestrator_answer_text({"answer": REWORDING, "intent": "control"}) == REWORDING

    def test_falls_back_to_response(self):
        assert wyoming_bridge.orchestrator_answer_text({"response": "legacy"}) == "legacy"

    def test_answer_wins_over_response(self):
        assert wyoming_bridge.orchestrator_answer_text({"answer": "a", "response": "r"}) == "a"

    def test_missing_both_is_empty(self):
        assert wyoming_bridge.orchestrator_answer_text({}) == ""


class _FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    body = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        return _FakeResponse(_FakeAsyncClient.body)


def test_query_response_contract_field_is_answer():
    """The fake body below mirrors the real QueryResponse fields: `answer`
    exists and `response` doesn't (read from source, not assumed)."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/orchestrator/main.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "QueryResponse")
    fields = {s.target.id for s in cls.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    assert "answer" in fields
    assert "response" not in fields


@pytest.fixture
def bridge_with_handler(monkeypatch):
    """The bridge module with AthenaWyomingHandler defined. Newer wyoming
    releases moved AsyncEventHandler out of wyoming.handle, which leaves
    the class undefined at import; a stand-in base class is enough for
    _process_query, which uses none of the base's behaviour."""
    if getattr(wyoming_bridge, "AthenaWyomingHandler", None) is not None:
        return wyoming_bridge
    import importlib

    wyoming_handle = pytest.importorskip("wyoming.handle")
    monkeypatch.setattr(wyoming_handle, "AsyncEventHandler", object, raising=False)
    return importlib.reload(wyoming_bridge)


class TestProcessQuerySpeaksTheAnswer:
    def _handler(self, bridge):
        handler = object.__new__(bridge.AthenaWyomingHandler)
        handler.interface_name = "kitchen"
        handler.state = bridge.WyomingSessionState.IDLE
        handler.interruption_context = None
        handler.current_response = ""
        handler.last_query = ""
        return handler

    def test_orchestrator_query_response_shape_is_spoken(self, monkeypatch, bridge_with_handler):
        bridge = bridge_with_handler
        # The exact field set QueryResponse serializes (no `response` key).
        _FakeAsyncClient.body = {
            "answer": REWORDING, "intent": "control", "confidence": 1.0, "citations": [],
            "request_id": "r1", "session_id": "s1", "processing_time": 0.1, "metadata": {},
        }
        monkeypatch.setattr(bridge.httpx, "AsyncClient", _FakeAsyncClient)
        handler = self._handler(bridge)
        asyncio.run(handler._process_query("office lights off please", "s1"))
        assert handler.current_response == REWORDING
