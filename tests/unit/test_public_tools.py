"""Tool entitlement for the public audience (V1.2).

The public audience is offered only get_weather, get_news, search_recipes
and search_streaming, and nothing outside what a caller is entitled to is
ever executed, whatever the model emits.
"""
from __future__ import annotations

import ast
import asyncio

import pytest

from . import _public_audience_harness as h


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


class _ToolsLLM(h.CapturingLLM):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offered: list[set] = []

    async def generate_with_tools(self, model=None, messages=None, tools=None, **kwargs):
        if tools is not None and self._with_tools_calls == 0:
            self.offered.append({t["function"]["name"] for t in tools})
        return await super().generate_with_tools(model=model, messages=messages, tools=tools, **kwargs)


def _public_perms():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    return normalize_permissions(public_permissions())


def _guest_perms():
    return h.mode_permission.normalize_permissions({
        "mode": "guest", "allowed_intents": [], "restricted_entities": [], "allowed_domains": [],
    })


def _executed_names(execute_mock) -> list[str]:
    names = []
    for call in execute_mock.await_args_list:
        names.extend(tc["function"]["name"] for tc in call.args[0])
    return names


def _run(state):
    return asyncio.run(h.main.tool_call_node(state))


def test_public_tool_schemas_exact(monkeypatch):
    h.patch_tool_call_dependencies(monkeypatch)
    llm = _ToolsLLM(content="hello")
    h._runtime.set_llm_router(llm)
    _run(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO))
    assert llm.offered, "floor: the model was offered tools"
    offered = llm.offered[0]
    assert offered == {"get_weather", "get_news", "search_recipes", "search_streaming"}
    assert "search_recipes" in offered
    assert "search_web" not in offered


def test_public_search_web_call_dropped(monkeypatch):
    """Named: the model emits search_web for a public caller; it never runs."""
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch)
    h._runtime.set_llm_router(_ToolsLLM(tool_calls=[h.tool_call("search_web", arguments={"query": "x"})], content=""))
    h._runtime.set_parallel_search_engine(None)
    _run(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO))
    assert "search_web" not in _executed_names(execute)


def test_public_mixed_calls_run_only_entitled(monkeypatch):
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch)
    h._runtime.set_llm_router(_ToolsLLM(tool_calls=[
        h.tool_call("get_weather", "a"), h.tool_call("get_tesla_metrics", "b"), h.tool_call("search_web", "c"),
    ]))
    _run(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO))
    assert _executed_names(execute) == ["get_weather"]


def test_not_offered_tool_call_dropped(monkeypatch):
    """Guest: the model emits get_tesla_metrics, which guests aren't
    offered; the executor never sees it."""
    guest_tools = ["get_weather", "get_news", "search_web"]
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch, tools=guest_tools)
    h._runtime.set_llm_router(_ToolsLLM(tool_calls=[h.tool_call("get_tesla_metrics")], content=""))
    h._runtime.set_parallel_search_engine(None)
    _run(h.make_state(permissions=_guest_perms(), intent=h.IntentCategory.GENERAL_INFO))
    assert "get_tesla_metrics" not in _executed_names(execute)


def test_owner_turn_unchanged_by_entitlement_check(monkeypatch, captured_logs):
    """Owner turns are unchanged: an offered call runs and the entitlement
    check drops nothing."""
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch)
    h._runtime.set_llm_router(_ToolsLLM(tool_calls=[h.tool_call("get_weather")]))
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    _run(h.make_state(permissions=owner, mode="owner", intent=h.IntentCategory.WEATHER))
    assert _executed_names(execute) == ["get_weather"]
    assert not [e for e in captured_logs if e.get("event") == "tool_call_not_entitled_dropped"]


def test_public_filter_does_not_mutate_tool_cache(monkeypatch):
    """A public turn, then an owner turn: the owner is still offered the
    full set, and the cached schema list is untouched."""
    h.patch_tool_call_dependencies(monkeypatch)
    cached_before = list(h.main.tool_schema_cache["guest_tools"])
    llm = _ToolsLLM(content="hi")
    h._runtime.set_llm_router(llm)
    _run(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO))
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    llm2 = _ToolsLLM(content="hi")
    h._runtime.set_llm_router(llm2)
    _run(h.make_state(permissions=owner, mode="owner", intent=h.IntentCategory.GENERAL_INFO))
    assert h.main.tool_schema_cache["guest_tools"] == cached_before
    assert llm2.offered[0] == set(h.ALL_TOOL_NAMES)


def test_execute_tools_parallel_single_call_site():
    """The executor has one call site, inside tool_call_node, and the
    entitlement filter runs in that function before it."""
    tree = ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))
    sites = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "execute_tools_parallel":
                    sites.append(fn.name)
    assert sites == ["tool_call_node"]
    source = ast.get_source_segment(h.MAIN_PY.read_text(encoding="utf-8"), next(
        fn for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "tool_call_node"
    ))
    assert "entitled_tool_names" in source
    assert source.index("tool_call_not_entitled_dropped") < source.index("execute_tools_parallel(")
