"""H1: an audience that sees no Base Knowledge row (public, unresolved) is given the
city-level home location ("City, ST"), never the stored location text, which can be a
street address. Every orchestrator consumer of the home location is driven here:
the tool-call prompt, the forced-directions origin, the weather location (tool and
retrieve paths), the search-query building and the Base Knowledge fallbacks."""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

# Import the services from src/ whatever else runs in this process (CI sets no PYTHONPATH).
_SRC = str(_Path(__file__).resolve().parents[2] / "src")
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from shared import household  # noqa: E402
from shared.knowledge_tiers import KnowledgeAudience  # noqa: E402

from . import _public_audience_harness as h  # noqa: E402

STREET = "123 Main St"
FULL_TEXT = f"{STREET}, Springfield, IL"
CITY_LEVEL = "Springfield, IL"


@pytest.fixture(autouse=True)
def _house(monkeypatch):
    h.reset_runtime()
    monkeypatch.setenv("DEFAULT_CITY", "Springfield")
    monkeypatch.setenv("DEFAULT_STATE", "IL")
    h.shared_config._clear_cache_for_tests()
    household.install_for_test(sources={"location": {"text": FULL_TEXT}})
    yield
    h.shared_config._clear_cache_for_tests()
    h.reset_runtime()


def _public_perms():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    return normalize_permissions(public_permissions())


def _owner_perms():
    return h.mode_permission.normalize_permissions({"mode": "owner"})


def _state(audience, *, permissions=None, **kwargs):
    state = h.make_state(permissions=permissions or _public_perms(), **kwargs)
    state.knowledge_audience = audience
    return state


PUBLIC = h.audience(None, public=True)
UNRESOLVED = KnowledgeAudience.UNRESOLVED
OWNER = h.audience("owner")


# -- the reader ---------------------------------------------------------------------------------

@pytest.mark.parametrize("aud,expect", [
    (PUBLIC, CITY_LEVEL),
    (UNRESOLVED, CITY_LEVEL),
    (OWNER, FULL_TEXT),
    (h.audience("guest"), FULL_TEXT),
    (h.audience("owner", degraded=True), FULL_TEXT),
], ids=["public", "unresolved", "owner", "guest", "degraded"])
def test_default_location_by_audience(aud, expect):
    from orchestrator.utils import constants

    assert constants.default_location(aud) == expect


def test_default_location_requires_an_audience():
    from orchestrator.utils import constants

    with pytest.raises(TypeError):
        constants.default_location()


def test_a_text_only_row_with_no_env_city_gives_the_public_nothing_not_the_address(monkeypatch):
    from orchestrator.utils import constants

    monkeypatch.delenv("DEFAULT_CITY")
    monkeypatch.delenv("DEFAULT_STATE")
    h.shared_config._clear_cache_for_tests()
    assert constants.default_location(PUBLIC) == ""
    assert constants.default_location(OWNER) == FULL_TEXT


def test_the_location_value_has_a_city_level_form():
    where = household.location()
    assert where.text == FULL_TEXT
    assert where.city_level == CITY_LEVEL


# -- the tool-call node -------------------------------------------------------------------------

def _no_street(*texts):
    for text in texts:
        assert STREET not in str(text)


def test_public_tool_call_prompt_and_weather_location_carry_city_only(monkeypatch):
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch)
    llm = h.CapturingLLM(tool_calls=[h.tool_call("get_weather")])
    h._runtime.set_llm_router(llm)
    asyncio.run(h.main.tool_call_node(_state(PUBLIC, intent=h.IntentCategory.WEATHER, query="what's the weather")))
    prompt = llm.text()
    assert f"HOME address is: {CITY_LEVEL}" in prompt, "floor: the home line is rendered with the city"
    _no_street(prompt)
    assert execute.await_args.kwargs["location"] == CITY_LEVEL


def test_unresolved_audience_gets_city_only_even_with_owner_permissions(monkeypatch):
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch)
    llm = h.CapturingLLM(tool_calls=[h.tool_call("get_weather")])
    h._runtime.set_llm_router(llm)
    state = _state(UNRESOLVED, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.WEATHER)
    asyncio.run(h.main.tool_call_node(state))
    _no_street(llm.text(), execute.await_args.kwargs["location"])


def test_owner_still_gets_the_stored_text(monkeypatch):
    """Positive control: the fix narrows the public audience only."""
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch)
    monkeypatch.setattr(h.main, "extract_home_address", mock.Mock(return_value=""))
    llm = h.CapturingLLM(tool_calls=[h.tool_call("get_weather")])
    h._runtime.set_llm_router(llm)
    state = _state(OWNER, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.WEATHER)
    asyncio.run(h.main.tool_call_node(state))
    assert execute.await_args.kwargs["location"] == FULL_TEXT


def test_public_forced_directions_origin_is_city_level(monkeypatch):
    h.patch_tool_call_dependencies(monkeypatch)
    rag = mock.MagicMock()
    rag.get = mock.AsyncMock(return_value=mock.Mock(success=False, data=None))
    h._runtime.set_rag_client(rag)
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=None))
    state = _state(PUBLIC, intent=h.IntentCategory.DIRECTIONS, query="directions to the airport")
    asyncio.run(h.main.tool_call_node(state))
    assert rag.get.await_count, "floor: the forced directions call was made"
    assert rag.get.await_args.kwargs["params"]["origin"] == CITY_LEVEL


def test_owner_forced_directions_origin_is_the_stored_text(monkeypatch):
    h.patch_tool_call_dependencies(monkeypatch)
    rag = mock.MagicMock()
    rag.get = mock.AsyncMock(return_value=mock.Mock(success=False, data=None))
    h._runtime.set_rag_client(rag)
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=None))
    state = _state(OWNER, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.DIRECTIONS,
                   query="directions to the airport")
    asyncio.run(h.main.tool_call_node(state))
    assert rag.get.await_args.kwargs["params"]["origin"] == FULL_TEXT


# -- search-query building ----------------------------------------------------------------------

@pytest.fixture
def search_engine():
    engine = mock.MagicMock()
    engine.search = mock.AsyncMock(return_value=("general", []))
    h._runtime.set_parallel_search_engine(engine)
    return engine


def test_tool_failure_web_search_query_for_an_unresolved_audience_is_city_level(monkeypatch, search_engine):
    h.patch_tool_call_dependencies(monkeypatch, executor_results={"search_restaurants": {"error": "down"}})
    monkeypatch.setattr(h.main, "extract_home_address", mock.Mock(return_value=""))
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=[h.tool_call("search_restaurants")]))
    state = _state(UNRESOLVED, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.DINING,
                   query="good restaurants")
    asyncio.run(h.main.tool_call_node(state))
    assert search_engine.search.await_count, "floor: the fallback search ran"
    for call in search_engine.search.await_args_list:
        _no_street(call.kwargs.get("query"), call.kwargs.get("location"))
    assert CITY_LEVEL in search_engine.search.await_args.kwargs["query"]


@pytest.mark.parametrize("failure", [{"error": "down"}, {"results": []}], ids=["tool-error", "empty-results"])
@pytest.mark.parametrize("env_city", [True, False], ids=["city-configured", "no-city-configured"])
def test_tool_fallback_searches_and_execution_location_never_carry_the_street(monkeypatch, search_engine, failure, env_city):
    """Both fallback branches build a query and a search location; with no city configured
    the city-level form is empty, so the street address is the only thing a leak could show."""
    if not env_city:
        monkeypatch.delenv("DEFAULT_CITY")
        monkeypatch.delenv("DEFAULT_STATE")
        h.shared_config._clear_cache_for_tests()
    _, execute, _, _ = h.patch_tool_call_dependencies(monkeypatch, executor_results={"search_restaurants": failure})
    monkeypatch.setattr(h.main, "extract_home_address", mock.Mock(return_value=""))
    h._runtime.set_llm_router(h.CapturingLLM(tool_calls=[h.tool_call("search_restaurants")]))
    state = _state(UNRESOLVED, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.DINING,
                   query="good restaurants")
    asyncio.run(h.main.tool_call_node(state))
    assert search_engine.search.await_count, "floor: the fallback search ran"
    for call in search_engine.search.await_args_list:
        _no_street(call.kwargs.get("query"), call.kwargs.get("location"))
    _no_street(execute.await_args.kwargs["location"])
    expected = CITY_LEVEL if env_city else ""
    assert search_engine.search.await_args.kwargs["location"] == expected
    assert execute.await_args.kwargs["location"] == expected


def test_post_synthesis_fallback_search_location_is_city_level(monkeypatch, search_engine):
    monkeypatch.setattr(
        h.helpers_module, "get_post_synthesis_fallback_config",
        mock.AsyncMock(return_value={"enabled": True, "config": {}}),
    )
    monkeypatch.setattr(h.helpers_module, "detect_insufficient_response", lambda answer, config: "couldn't find")
    state = _state(UNRESOLVED, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.GENERAL_INFO,
                   query="q")
    state.answer = "I couldn't find information about that."
    asyncio.run(h.helpers_module.maybe_post_synthesis_fallback(state))
    assert search_engine.search.await_count, "floor: the fallback search ran"
    _no_street(search_engine.search.await_args.kwargs.get("location"))
    assert search_engine.search.await_args.kwargs["location"] == CITY_LEVEL


def test_rag_failure_fallback_search_is_city_level(monkeypatch, search_engine):
    state = _state(UNRESOLVED, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.WEATHER,
                   query="weather")
    asyncio.run(h.helpers_module._fallback_to_web_search(state, "Weather", "down"))
    assert search_engine.search.await_count, "floor: the fallback search ran"
    for call in search_engine.search.await_args_list:
        _no_street(call.kwargs.get("query"), call.kwargs.get("location"))
    assert CITY_LEVEL in search_engine.search.await_args.kwargs["query"]


# -- retrieve (weather and the general search branch) -------------------------------------------

def test_public_weather_retrieval_location_is_city_level(monkeypatch):
    import orchestrator.nodes.retrieve as retrieve_module

    monkeypatch.setattr(retrieve_module, "get_weather_provider_mode", mock.AsyncMock(return_value="weather"))
    monkeypatch.setattr(retrieve_module, "get_rag_service_url", mock.AsyncMock(return_value="http://weather.example"))
    rag = mock.MagicMock()
    rag.update_service_url = mock.Mock()
    rag.get = mock.AsyncMock(return_value=mock.Mock(success=False, data=None))
    h._runtime.set_rag_client(rag)
    state = _state(PUBLIC, intent=h.IntentCategory.WEATHER, query="what's the weather like")
    asyncio.run(retrieve_module.retrieve_node(state))
    assert rag.get.await_count, "floor: the weather service was called"
    assert rag.get.await_args.kwargs["params"]["location"] == CITY_LEVEL


def test_retrieve_general_search_query_for_an_unresolved_audience_is_city_level(search_engine):
    import orchestrator.nodes.retrieve as retrieve_module

    state = _state(UNRESOLVED, permissions=_owner_perms(), mode="owner", intent=h.IntentCategory.GENERAL_INFO,
                   query="what is there to do nearby")
    asyncio.run(retrieve_module.retrieve_node(state))
    assert search_engine.search.await_count, "floor: the search ran"
    for call in search_engine.search.await_args_list:
        _no_street(call.kwargs.get("query"), call.kwargs.get("location"))
    assert CITY_LEVEL in search_engine.search.await_args.kwargs["query"]


# -- the Base Knowledge fallbacks ---------------------------------------------------------------

def test_extract_home_address_with_nothing_visible_falls_back_to_city_level():
    from shared.base_knowledge_utils import extract_home_address

    assert extract_home_address([]) == CITY_LEVEL
    assert extract_home_address([{"category": "property", "key": "wifi", "value": "x"}]) == CITY_LEVEL


def test_extract_home_address_still_returns_a_visible_address_row():
    from shared.base_knowledge_utils import extract_home_address

    rows = [{"category": "property", "key": "address", "value": FULL_TEXT}]
    assert extract_home_address(rows) == FULL_TEXT


# -- population: every consumer passes its audience ---------------------------------------------

def _default_location_calls(source: str):
    import ast

    return [n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "default_location"]


def _lacks_audience(call) -> bool:
    import ast

    first = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "audience"), None)
    return first is None or (isinstance(first, ast.Constant) and first.value is None)


def test_every_orchestrator_default_location_call_passes_an_audience():
    root = h.REPO_ROOT / "src" / "orchestrator"
    seen = {}
    for path in root.rglob("*.py"):
        calls = _default_location_calls(path.read_text(encoding="utf-8"))
        bare = [c.lineno for c in calls if _lacks_audience(c)]
        assert not bare, f"{path.relative_to(h.REPO_ROOT)}:{bare} call default_location() with no audience"
        if calls:
            seen[path.name] = len(calls)
    assert sum(seen.values()) >= 13, seen
    assert {"main.py", "helpers.py", "retrieve.py"} <= set(seen), seen


def test_a_planted_bare_default_location_call_is_caught():
    assert len(_default_location_calls("x = default_location()\n")) == 1
    assert not [c for c in _default_location_calls("x = default_location(state.knowledge_audience)\n") if _lacks_audience(c)]
    assert [c for c in _default_location_calls("x = constants.default_location()\n") if _lacks_audience(c)]


def test_a_literal_none_or_a_none_keyword_is_not_an_audience():
    for source in ("x = default_location(None)\n", "x = default_location(audience=None)\n"):
        assert [c for c in _default_location_calls(source) if _lacks_audience(c)], source
    assert not [c for c in _default_location_calls("x = default_location(audience=a)\n") if _lacks_audience(c)]


# The orchestrator reads the home location only inside these two functions, so the street
# address can't reach a prompt except through default_location's audience check.
LOCATION_READERS = {("utils/constants.py", "default_city"), ("utils/constants.py", "default_location")}


def _household_location_reads(source: str):
    """(function name, line) of every ``household.location()`` call, and of any
    ``from shared.household import location`` / ``from shared import household``-free alias."""
    import ast

    found = []

    def visit(node, enclosing):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = node.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "location":
            receiver = ast.unparse(node.func.value)
            if receiver in ("household", "shared.household"):
                found.append((enclosing, node.lineno))
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("household") and any(
                a.name in ("location", "*") for a in node.names):
            found.append((enclosing, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, enclosing)

    visit(ast.parse(source), "")
    return found


def test_the_orchestrator_reads_the_home_location_only_through_default_location():
    root = h.REPO_ROOT / "src" / "orchestrator"
    seen = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        for function, line in _household_location_reads(path.read_text(encoding="utf-8")):
            assert (relative, function) in LOCATION_READERS, f"{relative}:{line} reads household.location() in {function or 'module scope'}"
            seen.add((relative, function))
    assert seen == LOCATION_READERS, seen


def test_planted_location_reads_are_caught():
    assert _household_location_reads("def f():\n    return household.location().text\n") == [("f", 2)]
    assert _household_location_reads("def g():\n    return shared.household.location()\n") == [("g", 2)]
    assert _household_location_reads("from shared.household import location\n") == [("", 1)]
    assert _household_location_reads("def h(c):\n    return c.location()\n") == []
