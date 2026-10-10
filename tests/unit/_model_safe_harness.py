"""Drive the real `tool_call_node` and `execute_tools_parallel` with a fake RAG client and a recording LLM.

The recording LLM answers its first call with the scripted tool calls and records every message list it
is handed, so a test can read exactly what the model would have seen in each `role: tool` message.
"""
from __future__ import annotations

import asyncio
from unittest import mock

from orchestrator.rag_client import RAGResponse

from . import _public_audience_harness as h

REAL_EXECUTE_TOOLS_PARALLEL = h.main.execute_tools_parallel


class FakeRag:
    """`get`/`post` answer from `script`: service -> RAGResponse, Exception (raised), or a callable(path, params)."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def update_service_url(self, service, url):
        pass

    async def _answer(self, service, path, params):
        self.calls.append((service, path))
        entry = self.script[service]
        entry = entry(path, params) if callable(entry) else entry
        if isinstance(entry, BaseException):
            raise entry
        return entry

    async def get(self, service, path, params=None, **kwargs):
        return await self._answer(service, path, params)

    async def post(self, service, path, json=None, **kwargs):
        return await self._answer(service, path, json)


class ToolLLM:
    """First generate_with_tools -> the scripted tool calls; later calls -> `final`. Records every message list."""

    def __init__(self, tool_calls, final="All done."):
        self.tool_calls = tool_calls
        self.final = final
        self.message_lists = []
        self._n = 0

    async def generate_with_tools(self, **kwargs):
        self.message_lists.append(kwargs.get("messages"))
        self._n += 1
        if self._n == 1:
            return {"content": "", "tool_calls": self.tool_calls, "stop_reason": "stop"}
        return {"content": self.final, "stop_reason": "stop", "eval_count": 3}

    async def generate(self, **kwargs):
        return {"response": self.final, "eval_count": 3, "stop_reason": "stop"}

    def tool_messages(self):
        """The `role: tool` messages of the synthesis call, as content strings."""
        return [m["content"] for m in self.message_lists[-1] if m.get("role") == "tool"]


def install(monkeypatch, rag, llm, admin=None):
    """Wire the real executor to `rag` and the node to `llm`. Returns the recorded tool-usage metric mock."""
    admin = admin or h.fake_admin_client()
    admin.get_api_keys_for_tool = mock.AsyncMock(return_value={})
    metric = mock.AsyncMock()
    admin.record_tool_metric = metric
    h.patch_tool_call_dependencies(
        monkeypatch, admin=admin, tools=list(h.ALL_TOOL_NAMES) + ["search_transit", "get_train_schedule", "get_airport_info"],
    )
    monkeypatch.setattr(h.main, "execute_tools_parallel", REAL_EXECUTE_TOOLS_PARALLEL)
    monkeypatch.setattr("shared.admin_config.get_admin_client", lambda: admin)

    async def registry(function_name):
        return "http://rag.test"

    monkeypatch.setattr("orchestrator.rag_tools.get_tool_service_url_from_registry", registry)
    monkeypatch.setattr(h.main, "record_tool_execution", mock.MagicMock(), raising=False)
    component = {"model_name": "m", "backend_type": "ollama", "max_tokens": 256}
    monkeypatch.setattr(h.main, "get_component_config", mock.AsyncMock(return_value=component))
    h._runtime.set_rag_client(rag)
    h._runtime.set_llm_router(llm)
    return metric


def run_tool_call(query="what is the weather in Anytown", interface_type="text"):
    h.install_mode_client(server_mode="owner")
    authz = asyncio.run(h.mode_permission.resolve_request_authorization(
        "owner", None, caller_trust="household", service_authenticated=False,
    ))
    state = h.OrchestratorState(
        query=query, mode=authz.mode, room="kitchen", permissions=authz.permissions,
        intent=h.IntentCategory.GENERAL_INFO, interface_type=interface_type, context={}, session_id=None,
        knowledge_audience=authz.knowledge_audience,
    )
    return asyncio.run(h.main.tool_call_node(state))


def call(name, call_id="c1", **arguments):
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


def ok(data):
    return RAGResponse(success=True, data=data, status_code=200, service_name="x")


def failed(error, status=None, user_detail=None):
    return RAGResponse(success=False, error=error, status_code=status, service_name="x", user_detail=user_detail)


