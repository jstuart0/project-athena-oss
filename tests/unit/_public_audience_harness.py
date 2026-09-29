"""Shared orchestrator harness for the public-audience tests.

Same import discipline as tests/unit/test_query_mode_escalation.py: stub the
absent heavy deps and the DB-driven config_loader before the first
orchestrator import, then import orchestrator.nodes before helpers.

Nothing here references a symbol the public-audience work adds, so a test
module importing this harness still collects on an unrepaired tree; each
test imports the new symbols in its own body.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = str(REPO_ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()
os.environ.setdefault("SERVICE_API_KEY", "test-key-public-audience")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

from shared.config import get_config as _shared_get_config  # noqa: E402
import shared.config as shared_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _shared_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules.setdefault("orchestrator.config_loader", _config_loader_mock)

import orchestrator.nodes  # noqa: E402,F401
import orchestrator.main as main  # noqa: E402
import orchestrator.session_manager as session_manager_module  # noqa: E402
import orchestrator.nodes.synthesize as synthesize_module  # noqa: E402
import orchestrator.helpers as helpers_module  # noqa: E402
from orchestrator import mode_permission  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.session_manager import SessionManager  # noqa: E402
from orchestrator.state import IntentCategory, OrchestratorState  # noqa: E402

MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"
ORCH_DIR = REPO_ROOT / "src" / "orchestrator"

GUEST_NAME = "Alice Renter"
SECRET_WIFI = "hunter2"
HOME_STREET = "12 Juniper Lane"
KNOWLEDGE = f"HOUSE FACTS: wifi_password={SECRET_WIFI}; address={HOME_STREET}"
LOCATION_OVERRIDE = {"address": "Somewhere Public, Anytown"}


def service_headers() -> dict:
    return {"X-Service-Key": shared_config.get_config().service_api_key}


def make_response(status_code: int, json_data: dict) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def install_mode_client(*, server_mode: str = "owner", degraded: bool = False, guest_profile: dict | None = None):
    """A fake mode service. ``guest_profile`` is what the mode service
    returns for guest permissions (both the server-guest read and the
    explicit ?mode=guest fetch)."""
    profile = guest_profile or {
        "mode": "guest",
        "allowed_intents": ["weather", "control", "dining", "websearch"],
        "restricted_entities": [],
        "allowed_domains": ["switch", "light"],
    }

    async def _get(url, params=None, **kwargs):
        if url == "/mode":
            if degraded:
                return make_response(200, {"mode": "degraded", "reason": "cold start"})
            return make_response(200, {"mode": server_mode, "override_active": False, "reason": "ok"})
        if url == "/mode/permissions" and params and params.get("mode") == "guest":
            return make_response(200, profile)
        if url == "/mode/permissions":
            return make_response(200, profile if server_mode == "guest" else {"mode": "owner"})
        if url == "/health":
            return make_response(200, {"pin_authority": "admin"})
        raise AssertionError(f"unexpected GET {url}")

    client = mock.AsyncMock()
    client.get = mock.AsyncMock(side_effect=_get)
    client.post = mock.AsyncMock(return_value=make_response(200, {}))
    _runtime.set_mode_client(client)
    return client


def reset_runtime():
    shared_config._clear_cache_for_tests()
    _runtime.reset_for_test()
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()
    session_manager_module._memory_sessions.clear()
    sm = SessionManager()
    sm.redis_client = None
    _runtime.set_session_manager(sm)
    return sm


def patch_conversation_config(monkeypatch, *, enabled: bool = False, history_mode: str = "full"):
    settings = {"enabled": enabled, "use_context": enabled, "history_mode": history_mode, "max_llm_history_messages": 10}
    fake_conv_config = SimpleNamespace(
        get_conversation_settings=mock.AsyncMock(return_value=settings),
        log_analytics_event=mock.AsyncMock(),
    )
    monkeypatch.setattr(main, "get_config", mock.AsyncMock(return_value=fake_conv_config))

    async def _fake_sm_get_config():
        return SimpleNamespace(
            get_conversation_settings=mock.AsyncMock(return_value={"timeout_seconds": 3600}),
            log_analytics_event=mock.AsyncMock(),
        )

    monkeypatch.setattr(session_manager_module, "get_config", _fake_sm_get_config)


def fake_admin_client(guest_info: dict | None = None):
    admin = mock.MagicMock()
    admin.get_user_session_by_device = mock.AsyncMock(return_value=guest_info)
    admin.get_base_knowledge = mock.AsyncMock(return_value=[])
    admin.get_tool_calling_settings = mock.AsyncMock(return_value={
        "enabled": True, "max_parallel_tools": 3, "tool_call_timeout_seconds": 30,
    })
    admin.get_enabled_tools = mock.AsyncMock(return_value=[])
    return admin


def tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    }


ALL_TOOL_NAMES = [
    "get_weather", "get_news", "search_recipes", "search_streaming",
    "search_web", "search_restaurants", "get_directions", "get_tesla_metrics",
    "get_sports_scores", "scrape_website",
]


class CapturingLLM:
    """Records every prompt / message list handed to the LLM.

    ``tool_calls`` is what the first generate_with_tools call emits.
    """

    def __init__(self, tool_calls=None, content: str = "Here you go."):
        self.tool_calls = tool_calls
        self.content = content
        self.captured: list[str] = []
        self._with_tools_calls = 0

    def _capture(self, *parts):
        for part in parts:
            if part is None:
                continue
            self.captured.append(part if isinstance(part, str) else repr(part))

    async def generate_with_tools(self, model=None, messages=None, tools=None, **kwargs):
        self._capture(messages)
        self._with_tools_calls += 1
        if self._with_tools_calls == 1 and self.tool_calls:
            return {"tool_calls": self.tool_calls, "content": "", "eval_count": 1}
        return {"content": self.content, "eval_count": 1}

    async def generate(self, model=None, prompt=None, system_prompt=None, **kwargs):
        self._capture(prompt, system_prompt)
        return {"response": self.content, "eval_count": 1}

    async def generate_stream(self, model=None, prompt=None, system_prompt=None, **kwargs):
        self._capture(prompt, system_prompt)
        yield {"token": self.content}
        yield {"token": "", "done": True}

    def text(self) -> str:
        return "\n".join(self.captured)


def patch_tool_call_dependencies(monkeypatch, *, admin=None, tools=None, executor_results=None):
    """Wire tool_call_node's collaborators to fakes. Returns
    (admin, execute_mock, knowledge_mock, home_mock)."""
    admin = admin or fake_admin_client()
    schemas = [tool_schema(n) for n in (tools or ALL_TOOL_NAMES)]
    monkeypatch.setattr(main, "get_admin_client", lambda: admin)
    monkeypatch.setattr(main, "check_escalation_triggers", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(main, "TOOL_REGISTRY_AVAILABLE", False)
    monkeypatch.setattr(main, "tool_schema_cache", {"guest_tools": list(schemas), "owner_tools": list(schemas)})
    monkeypatch.setattr(main, "tool_config_cache", {"guest_tools": [], "owner_tools": []})
    monkeypatch.setattr(main, "build_core_assistant_prompt", mock.AsyncMock(side_effect=_core_prompt))
    knowledge = mock.AsyncMock(return_value=KNOWLEDGE)
    home = mock.AsyncMock(return_value=HOME_STREET)
    monkeypatch.setattr(main, "get_knowledge_context_for_user", knowledge)
    monkeypatch.setattr(main, "get_home_address_for_user", home)
    monkeypatch.setattr(synthesize_module, "get_knowledge_context_for_user", knowledge)
    monkeypatch.setattr(synthesize_module, "build_core_assistant_prompt", mock.AsyncMock(side_effect=_core_prompt))
    monkeypatch.setattr(synthesize_module, "get_admin_client", lambda: admin)
    component = {"model_name": "m", "backend_type": "ollama", "max_tokens": 256}
    monkeypatch.setattr(main, "get_component_config", mock.AsyncMock(return_value=component))
    monkeypatch.setattr(main, "get_model_for_component", mock.AsyncMock(return_value="m"))
    monkeypatch.setattr(main, "store_conversation_context", mock.AsyncMock())

    async def _execute(tool_calls, guest_mode=False, location=None):
        results = {}
        for tc in tool_calls:
            name = tc.get("function", {}).get("name")
            results[tc.get("id")] = (executor_results or {}).get(name, {"current": {"temp": 70}, "results": [1]})
        return results

    execute = mock.AsyncMock(side_effect=_execute)
    monkeypatch.setattr(main, "execute_tools_parallel", execute)
    return admin, execute, knowledge, home


async def _core_prompt(include_voice_formatting=True, guest_name=None, owner_name=None, **kwargs):
    prompt = "You are the assistant."
    if guest_name:
        prompt += f" You are speaking with {guest_name}."
    return prompt


def tool_call(name: str, call_id: str = "c1", arguments: dict | None = None) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": arguments or {}}}


def make_state(*, permissions: dict, mode: str = "guest", intent=IntentCategory.GENERAL_INFO, context=None, query="tell me something"):
    return OrchestratorState(
        query=query,
        mode=mode,
        room="kitchen",
        permissions=permissions,
        intent=intent,
        interface_type="chat",
        context=context or {},
        session_id=None,
    )
