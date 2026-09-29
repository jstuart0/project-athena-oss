"""No web search for the public audience, by any path (V1.7, PP11).

Web search runs outside the tool allowlist in several places: tool
fallbacks, the post-synthesis retry, retrieve_node's per-service fallbacks,
its WEBSEARCH branch and its final parallel search. Each asks
helpers.web_search_allowed first.
"""
from __future__ import annotations

import ast
import asyncio
from unittest import mock

import pytest

from . import _public_audience_harness as h
import orchestrator.nodes.retrieve as retrieve_module


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


class _Search:
    def __init__(self):
        self.search = mock.AsyncMock(return_value=("general", []))


@pytest.fixture
def search_engine():
    engine = _Search()
    h._runtime.set_parallel_search_engine(engine)
    return engine


def _public_perms():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    return normalize_permissions(public_permissions())


def _owner_perms():
    return h.mode_permission.normalize_permissions({"mode": "owner"})


def test_public_tool_failure_never_web_searches(monkeypatch, search_engine):
    """Named: get_news fails for a public caller; no fallback search."""
    h.patch_tool_call_dependencies(monkeypatch, executor_results={"get_news": {"error": "down"}})
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=[h.tool_call("get_news")]))
    fallback = mock.AsyncMock()
    monkeypatch.setattr(h.main, "_fallback_to_web_search", fallback)
    asyncio.run(h.main.tool_call_node(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.NEWS)))
    search_engine.search.assert_not_awaited()
    fallback.assert_not_awaited()


def test_owner_tool_failure_still_web_searches(monkeypatch, search_engine):
    """Positive control: the same failure for an owner does fall back."""
    h.patch_tool_call_dependencies(monkeypatch, executor_results={"get_news": {"error": "down"}})
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=[h.tool_call("get_news")]))
    asyncio.run(h.main.tool_call_node(h.make_state(permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.NEWS)))
    search_engine.search.assert_awaited()


def test_public_empty_tool_result_never_web_searches(monkeypatch, search_engine):
    h.patch_tool_call_dependencies(monkeypatch, executor_results={"get_news": {"results": []}})
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=[h.tool_call("get_news")]))
    asyncio.run(h.main.tool_call_node(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.NEWS)))
    search_engine.search.assert_not_awaited()


def test_public_no_tool_selected_never_web_searches(monkeypatch, search_engine):
    h.patch_tool_call_dependencies(monkeypatch)
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=None, content=""))
    state = asyncio.run(h.main.tool_call_node(h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO)))
    search_engine.search.assert_not_awaited()
    assert state.error


def test_public_retrieve_failure_never_web_searches(monkeypatch, search_engine):
    """retrieve_node's weather path: the service is unconfigured, which
    normally falls back to web search."""
    monkeypatch.setattr(retrieve_module, "get_weather_provider_mode", mock.AsyncMock(return_value="weather"))
    monkeypatch.setattr(retrieve_module, "get_rag_service_url", mock.AsyncMock(return_value=None))
    state = h.make_state(permissions=_public_perms(), intent=h.IntentCategory.WEATHER, query="weather today")
    asyncio.run(retrieve_module.retrieve_node(state))
    search_engine.search.assert_not_awaited()


def test_owner_retrieve_failure_still_web_searches(monkeypatch, search_engine):
    monkeypatch.setattr(retrieve_module, "get_weather_provider_mode", mock.AsyncMock(return_value="weather"))
    monkeypatch.setattr(retrieve_module, "get_rag_service_url", mock.AsyncMock(return_value=None))
    state = h.make_state(permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.WEATHER, query="weather today")
    asyncio.run(retrieve_module.retrieve_node(state))
    search_engine.search.assert_awaited()


def test_public_retrieve_else_never_searches(search_engine):
    state = h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO, query="who won the game")
    asyncio.run(retrieve_module.retrieve_node(state))
    search_engine.search.assert_not_awaited()


def test_public_retrieve_websearch_branch_never_calls_service(monkeypatch, search_engine):
    rag = mock.MagicMock()
    rag.get = mock.AsyncMock()
    h._runtime.set_rag_client(rag)
    url = mock.AsyncMock(return_value="http://websearch.example:8018")
    monkeypatch.setattr(retrieve_module, "get_rag_service_url", url)
    state = h.make_state(permissions=_public_perms(), intent=h.IntentCategory.WEBSEARCH, query="search the web for cats")
    asyncio.run(retrieve_module.retrieve_node(state))
    rag.get.assert_not_awaited()
    url.assert_not_awaited()
    search_engine.search.assert_not_awaited()


def test_public_post_synthesis_fallback_never_searches(monkeypatch, search_engine):
    monkeypatch.setattr(
        h.helpers_module, "get_post_synthesis_fallback_config",
        mock.AsyncMock(return_value={"enabled": True, "config": {}}),
    )
    monkeypatch.setattr(h.helpers_module, "detect_insufficient_response", lambda answer, config: "couldn't find")
    state = h.make_state(permissions=_public_perms(), intent=h.IntentCategory.GENERAL_INFO, query="q")
    state.answer = "I couldn't find information about that."
    assert asyncio.run(h.helpers_module.maybe_post_synthesis_fallback(state)) is False
    search_engine.search.assert_not_awaited()


def test_web_search_allowed_predicate():
    from orchestrator.helpers import web_search_allowed

    assert web_search_allowed(h.make_state(permissions=_owner_perms(), mode="owner")) is True
    guest = h.mode_permission.normalize_permissions({"mode": "guest"})
    assert web_search_allowed(h.make_state(permissions=guest)) is True
    assert web_search_allowed(h.make_state(permissions=_public_perms())) is False


# ---------------------------------------------------------------------------
# PP11: population of web-search executions
# ---------------------------------------------------------------------------

_SEARCH_RECEIVERS = {"parallel_search_engine", "psearch", "parallel_engine"}
_SEARCH_METHODS = {"search", "search_primary_parallel"}
_EXECUTOR = "execute_tools_parallel"

# The exact population (tessa M2). A new site fails here until it's added,
# guarded or pinned as the tool executor.
EXPECTED_GUARDED = {
    ("src/orchestrator/helpers.py", "maybe_post_synthesis_fallback", "parallel_search"): 1,
    ("src/orchestrator/helpers.py", "_fallback_to_web_search", "parallel_search"): 1,
    ("src/orchestrator/main.py", "tool_call_node", "parallel_search"): 2,
    ("src/orchestrator/nodes/retrieve.py", "retrieve_node", "parallel_search"): 1,
    ("src/orchestrator/nodes/retrieve.py", "retrieve_node", "websearch_service"): 1,
}
EXPECTED_EXECUTOR = {
    ("src/orchestrator/main.py", "execute_tools_parallel", "parallel_search"): 1,
}


def _outermost_functions(tree):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _is_engine_receiver(receiver):
    """`parallel_search_engine.search(`, `psearch.search(`, and the chained
    `get_parallel_search_engine().search(` / `(await get_parallel_search_engine(...)).search(`."""
    if isinstance(receiver, ast.Await):
        receiver = receiver.value
    if isinstance(receiver, ast.Name):
        return receiver.id in _SEARCH_RECEIVERS
    if isinstance(receiver, ast.Call):
        func = receiver.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        return name == "get_parallel_search_engine"
    return False


def _search_sites(fn, rel):
    sites = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SEARCH_METHODS and _is_engine_receiver(node.func.value)
        ):
            sites.append(("parallel_search", node.lineno))
        # retrieve_node's WEBSEARCH branch calls the websearch service; other
        # loads of the constant (the URL registry) execute nothing.
        if (
            rel.endswith("nodes/retrieve.py")
            and isinstance(node, ast.Name)
            and node.id == "WEBSEARCH_SERVICE_URL"
            and isinstance(node.ctx, ast.Load)
        ):
            sites.append(("websearch_service", node.lineno))
    return sites


def _calls(fn, name):
    return any(
        isinstance(node, ast.Call) and getattr(node.func, "id", None) == name for node in ast.walk(fn)
    )


def _population():
    guarded, executor = {}, {}
    for path in sorted(h.ORCH_DIR.rglob("*.py")):
        rel = path.relative_to(h.REPO_ROOT).as_posix()
        if "/search_providers/" in rel or rel.endswith(("parallel_search.py", "web_search.py", "urls.py")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in _outermost_functions(tree):
            for kind, _ in _search_sites(fn, rel):
                bucket = executor if fn.name == _EXECUTOR else guarded
                key = (rel, fn.name, kind)
                bucket[key] = bucket.get(key, 0) + 1
                if bucket is guarded:
                    assert _calls(fn, "web_search_allowed"), f"{rel}:{fn.name} runs web search without web_search_allowed"
    return guarded, executor


def test_web_search_sites_guarded():
    guarded, executor = _population()
    assert guarded == EXPECTED_GUARDED
    assert executor == EXPECTED_EXECUTOR
    assert sum(guarded.values()) >= 6


def test_recogniser_sees_chained_engine_calls():
    tree = ast.parse(
        "async def f():\n"
        "    await get_parallel_search_engine().search(q)\n"
        "    await (await get_parallel_search_engine(rag)).search_primary_parallel(q, 5)\n"
        "    await _runtime.get_parallel_search_engine().search(q)\n"
        "    re.search(p, q)\n"
    )
    assert len(_search_sites(tree.body[0], "x.py")) == 3
