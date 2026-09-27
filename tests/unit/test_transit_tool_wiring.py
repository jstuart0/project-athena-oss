"""ATHENA-90 (Campaign 2026-09-27-deliver-athena-transit-and-base-knowledge,
Phase 1): search_transit reaches a real transportation-service route.

Plan: .mozart/plans/active/2026-09-27-deliver-athena-transit-and-base-knowledge.md
Test contract: same directory,
2026-09-27-deliver-athena-transit-and-base-knowledge.test-contract.md
(r2 final) -- T1-T8, T5a-q, T6a-c.

Mocking strategy (see contract for the full rationale):
  - T1/T2/T6b/T6c: real source, AST literal_eval of the dict/If literals
    inside execute_single_tool / tool_call_node in src/orchestrator/main.py.
    Nothing is faked; the test reads the file jackson ships.
  - T3/T4/T8: real RAGClient -> httpx.ASGITransport -> the actual
    transportation FastAPI app, per the DC9 pattern
    (tests/unit/test_rag_client_dc9.py).
  - T5: real fastapi.testclient.TestClient against the real app with seeded
    in-memory transit_data, fixed clock.
  - T6a: real, no mock -- helpers.is_transit_query is pure.
  - T7: real source; migration 059 loaded by path (not importable as a
    package module), same technique as test_rag_client_dc9.py's
    _import_amtrak_app.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
from datetime import datetime as _real_datetime
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
MAIN_PY = _SRC / "orchestrator" / "main.py"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

from shared.config import _clear_cache_for_tests  # noqa: E402
from orchestrator.rag_client import RAGClient  # noqa: E402


# ---------------------------------------------------------------------------
# AST extraction helpers (T1, T2, T6b, T6c) -- same technique as
# tests/unit/test_context_continuation.py::_extract_intent_to_tools, reused
# here rather than duplicating a second walker for the same dict shapes.
# ---------------------------------------------------------------------------

def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {MAIN_PY}")


def _extract_literal(func_name: str, var_name: str):
    tree = ast.parse(MAIN_PY.read_text())
    func = _find_function(tree, func_name)
    for inner in ast.walk(func):
        if isinstance(inner, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == var_name for t in inner.targets
        ):
            return ast.literal_eval(inner.value)
    raise AssertionError(f"{var_name!r} literal not found in {func_name}()")


def _extract_intent_to_tools() -> dict:
    return _extract_literal("tool_call_node", "intent_to_tools")


def _find_directions_gate_if(tree: ast.AST) -> ast.If:
    """The `If` node in tool_call_node whose test compares state.intent.value
    to "directions" and calls is_transit_query, and whose body appends
    "search_transit"."""
    func = _find_function(tree, "tool_call_node")
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "directions" not in test_src or "is_transit_query" not in test_src:
            continue
        body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "search_transit" in body_src and "append" in body_src:
            return node
    raise AssertionError("gated is_transit_query If node not found in tool_call_node")


# ---------------------------------------------------------------------------
# Denver fixture (L1) -- shared by T3, T5, T8.
# ---------------------------------------------------------------------------

_TRANSIT_ENV_VARS = ("TRANSIT_REGION_NAME", "TRANSIT_GTFS_FEEDS", "TRANSIT_STATIC_SERVICES")


def _import_transportation(unique_name: str):
    """Fresh-import (or re-execute) transportation/main.py under a private
    module name -- it's literally named main.py, the same collision every
    RAG-service test hits (see test_rag_client_dc9.py::_import_amtrak_app)."""
    path = _SRC / "rag" / "transportation" / "main.py"
    if unique_name in sys.modules:
        module = sys.modules[unique_name]
        module.__spec__.loader.exec_module(module)
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _reload_configured_transportation(monkeypatch, unique_name: str):
    for var in _TRANSIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TRANSIT_REGION_NAME", "Denver")
    monkeypatch.setenv(
        "TRANSIT_GTFS_FEEDS",
        json.dumps({
            "rtd_bus": {
                "name": "RTD Bus", "agency": "rtd",
                "url": "https://example.org/gtfs.zip",
                "type": "bus", "free": True,
            }
        }),
    )
    monkeypatch.setenv(
        "TRANSIT_STATIC_SERVICES",
        json.dumps({
            "confluence_ferry": {
                "name": "Confluence Ferry",
                "type": "ferry",
                "free": True,
                "hours": {"weekday": {"start": "07:00", "end": "19:00"}, "weekend": None},
                "frequency_minutes": 20,
                "stops": [{"name": "Confluence Park", "lat": 39.7527, "lon": -105.0089}],
                "agency_name": "Denver Parks & Rec",
            }
        }),
    )
    _clear_cache_for_tests()
    return _import_transportation(unique_name)


def _seed_transit_data(module) -> None:
    """L1: complete fixture -- feed-prefixed stop ids (production shape,
    :284), a complete route dict (route_short_name/long_name/type/feed_id,
    so get_free_transit's per-feed route check passes), and a stop with no
    stop_times row (T5q)."""
    module.transit_data["stops"].update({
        "rtd_bus_1001": {
            "stop_id": "rtd_bus_1001", "stop_name": "Union Station",
            "stop_lat": 39.7527, "stop_lon": -104.9997,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
        "rtd_bus_1003": {
            "stop_id": "rtd_bus_1003", "stop_name": "Stadium Stop",
            "stop_lat": 39.7561, "stop_lon": -105.0201,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
        "confluence_ferry_0": {
            "stop_id": "confluence_ferry_0", "stop_name": "Confluence Park",
            "stop_lat": 39.7527, "stop_lon": -105.0089,
            "feed_id": "confluence_ferry", "stop_type": "ferry_terminal",
            "wheelchair_boarding": 1,
            "service_info": {
                "name": "Confluence Ferry", "free": True,
                "hours": {"weekday": {"start": "07:00", "end": "19:00"}, "weekend": None},
                "frequency_minutes": 20,
            },
        },
        # T5f (codex P3 FIX): four "c"-matching filler stops, all farther
        # from the query point than rtd_bus_1002 below, and all inserted
        # BEFORE it -- so a cap-before-sort regression would keep these and
        # drop the actual nearest stop (rtd_bus_1002), rather than
        # coincidentally keeping it because it happened to iterate first.
        "rtd_bus_1004": {
            "stop_id": "rtd_bus_1004", "stop_name": "Commerce City Stop",
            "stop_lat": 39.8083, "stop_lon": -104.9342,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
        "rtd_bus_1005": {
            "stop_id": "rtd_bus_1005", "stop_name": "Cherry Creek Stop",
            "stop_lat": 39.7047, "stop_lon": -104.9412,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
        "rtd_bus_1006": {
            "stop_id": "rtd_bus_1006", "stop_name": "Capitol Hill Stop",
            "stop_lat": 39.7355, "stop_lon": -104.9812,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
        "rtd_bus_1007": {
            "stop_id": "rtd_bus_1007", "stop_name": "Curtis Park Stop",
            "stop_lat": 39.7547, "stop_lon": -104.9764,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
        "rtd_bus_1002": {
            "stop_id": "rtd_bus_1002", "stop_name": "Civic Center",
            "stop_lat": 39.7392, "stop_lon": -104.9903,
            "feed_id": "rtd_bus", "stop_type": "bus", "wheelchair_boarding": 0,
        },
    })
    module.transit_data["routes"].update({
        "rtd_bus_1": {
            "route_id": "rtd_bus_1", "route_short_name": "1", "route_long_name": "Downtown",
            "route_type": 3, "feed_id": "rtd_bus", "agency_id": "", "route_color": "", "route_text_color": "",
        },
    })
    module.transit_data["stop_times"].update({
        "rtd_bus_1001": [
            {"trip_id": "rtd_bus_t1", "stop_id": "rtd_bus_1001", "arrival_time": "12:05:00", "departure_time": "12:05:00", "stop_sequence": 1, "feed_id": "rtd_bus"},
            {"trip_id": "rtd_bus_t2", "stop_id": "rtd_bus_1001", "arrival_time": "12:20:00", "departure_time": "12:20:00", "stop_sequence": 1, "feed_id": "rtd_bus"},
            {"trip_id": "rtd_bus_t3", "stop_id": "rtd_bus_1001", "arrival_time": "12:35:00", "departure_time": "12:35:00", "stop_sequence": 1, "feed_id": "rtd_bus"},
            {"trip_id": "rtd_bus_t4", "stop_id": "rtd_bus_1001", "arrival_time": "12:50:00", "departure_time": "12:50:00", "stop_sequence": 1, "feed_id": "rtd_bus"},
        ],
        # rtd_bus_1002 / rtd_bus_1003 deliberately have no stop_times row
        # (T5q -- a real GTFS gap: the stop exists, the feed loaded fine,
        # but this stop has no scheduled departures today).
    })
    module.transit_data["last_updated"] = "2026-09-28T12:00:00"


class _FixedDateTime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 28, 12, 0, 0)  # Monday


def _configured_module(monkeypatch, unique_name: str):
    module = _reload_configured_transportation(monkeypatch, unique_name)
    _seed_transit_data(module)
    monkeypatch.setattr(module, "datetime", _FixedDateTime)
    return module


@pytest.fixture(autouse=True)
def _clear_config_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# T1/T2 -- wiring + drift guard (unchanged from r1)
# ---------------------------------------------------------------------------

def test_T1_endpoint_service_and_get_tools_wiring():
    endpoint_map = _extract_literal("execute_single_tool", "endpoint_map")
    service_name_map = _extract_literal("execute_single_tool", "service_name_map")
    get_tools = _extract_literal("execute_single_tool", "get_tools")

    assert endpoint_map["search_transit"] == "/transit/query"
    assert service_name_map["search_transit"] == "transportation"
    assert "search_transit" in get_tools
    assert len(endpoint_map) >= 17


def test_T2_every_TOOL_DEFINITIONS_tool_has_an_endpoint_map_entry():
    import orchestrator.rag_tools as rag_tools

    endpoint_map = _extract_literal("execute_single_tool", "endpoint_map")
    names = {t["tool_name"] for t in rag_tools.TOOL_DEFINITIONS}
    names -= {"get_sports_scores", "get_sports_standings"}

    assert names.issubset(endpoint_map.keys())
    assert len(names) >= 18
    assert "search_transit" in names


# ---------------------------------------------------------------------------
# T3/T4 -- round trip (real RAGClient -> ASGITransport -> the real app)
# ---------------------------------------------------------------------------

class _FakeHttpPool:
    """Call-forwarding shim for orchestrator.http_pool.get_http_pool() --
    not a mock of behavior, per the DC9 pattern this reuses verbatim."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def get_client(self, _pool_name: str) -> httpx.AsyncClient:
        return self._client


@pytest.mark.asyncio
async def test_T3_round_trip_configured_search_mode(monkeypatch):
    module = _configured_module(monkeypatch, "_test_transit_wiring_t3")

    endpoint_map = _extract_literal("execute_single_tool", "endpoint_map")
    get_tools = _extract_literal("execute_single_tool", "get_tools")
    endpoint = endpoint_map["search_transit"]
    dispatch = "GET" if "search_transit" in get_tools else "POST"

    asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://transportation-svc"
    )
    try:
        rag_client = RAGClient(service_urls={"transportation": "http://transportation-svc"})
        rag_client._http_pool = _FakeHttpPool(asgi_client)

        if dispatch == "GET":
            resp = await rag_client.get(
                "transportation", endpoint, params={"query": "union"},
                skip_circuit_breaker=True, skip_rate_limit=True,
            )
        else:
            resp = await rag_client.post(
                "transportation", endpoint, json={"query": "union"},
                skip_circuit_breaker=True, skip_rate_limit=True,
            )

        assert resp.success is True
        assert resp.data["mode"] == "search"
        assert {s["stop_id"] for s in resp.data["stops"]} == {"rtd_bus_1001"}
        next_departures = resp.data["stops"][0]["next_departures"]
        assert len(next_departures) == 3
        assert next_departures[0]["departure_time"] == "12:05"
    finally:
        await asgi_client.aclose()


@pytest.mark.asyncio
async def test_T4_round_trip_unconfigured(monkeypatch):
    for var in _TRANSIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    _clear_cache_for_tests()
    module = _import_transportation("_test_transit_wiring_t4")

    endpoint_map = _extract_literal("execute_single_tool", "endpoint_map")
    get_tools = _extract_literal("execute_single_tool", "get_tools")
    endpoint = endpoint_map["search_transit"]

    asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://transportation-svc"
    )
    try:
        rag_client = RAGClient(service_urls={"transportation": "http://transportation-svc"})
        rag_client._http_pool = _FakeHttpPool(asgi_client)

        if "search_transit" in get_tools:
            resp = await rag_client.get(
                "transportation", endpoint, params={"query": "union"},
                skip_circuit_breaker=True, skip_rate_limit=True,
            )
        else:
            resp = await rag_client.post(
                "transportation", endpoint, json={"query": "union"},
                skip_circuit_breaker=True, skip_rate_limit=True,
            )

        assert resp.success is False
        assert resp.status_code == 503
        assert "TRANSIT_GTFS_FEEDS" in resp.error
    finally:
        await asgi_client.aclose()


# ---------------------------------------------------------------------------
# T5 -- endpoint modes via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def transit_client(monkeypatch):
    module = _configured_module(monkeypatch, "_test_transit_wiring_t5")
    return TestClient(module.app)


def test_T5a_stop_id_exact_gives_departures(transit_client):
    resp = transit_client.get("/transit/query", params={"stop_id": "rtd_bus_1001"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "departures"


def test_T5b_bare_stop_id_resolves_via_feed_prefix(transit_client):
    resp = transit_client.get("/transit/query", params={"stop_id": "1001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "departures"
    assert body["resolved_stop_id"] == "rtd_bus_1001"


def test_T5c_stop_id_as_name_falls_back_to_search(transit_client):
    resp = transit_client.get("/transit/query", params={"stop_id": "union station"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "search"
    assert any(s["stop_id"] == "rtd_bus_1001" for s in body["stops"])


def test_T5d_unresolvable_stop_id_gives_404_after_full_fallback(transit_client):
    resp = transit_client.get("/transit/query", params={"stop_id": "zzz"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Stop not found: zzz"


def test_T5e_nearby_sorts_by_distance(transit_client):
    resp = transit_client.get("/transit/query", params={"lat": 39.7527, "lon": -104.9997, "radius": 500})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "nearby"
    assert body["stops"][0]["stop_id"] == "rtd_bus_1001"


def test_T5f_query_with_location_sorts_by_distance(transit_client):
    resp = transit_client.get("/transit/query", params={"query": "c", "lat": 39.7392, "lon": -104.9903})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "search"
    stop_ids = [s["stop_id"] for s in body["stops"]]
    # Six stops match "c" (Union Station and Stadium Stop don't); the query
    # point is exactly rtd_bus_1002's coordinates (dist 0), which is
    # inserted LAST in the fixture -- a cap-before-sort regression (codex
    # P3 FIX) would drop it entirely, since the first 5 in insertion order
    # are the other five "c" matches. With limit=5, the correct survivors
    # are the 5 nearest; rtd_bus_1004 (Commerce City, ~9km away) is the
    # farthest of the six and is the one correctly dropped.
    assert len(stop_ids) == 5
    assert stop_ids[0] == "rtd_bus_1002"
    assert "rtd_bus_1004" not in stop_ids
    assert all("distance_meters" in s for s in body["stops"])
    assert body["stops"] == sorted(body["stops"], key=lambda s: s["distance_meters"])


def test_T5g_query_zero_matches_gives_message(transit_client):
    resp = transit_client.get("/transit/query", params={"query": "zzzz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["stops"] == []
    assert body["routes"] == []
    assert "zzzz" in body["message"]


def test_T5h_free_only_alone(transit_client):
    resp = transit_client.get("/transit/query", params={"free_only": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "free"
    assert {o["name"] for o in body["free_transit_options"]} == {"RTD Bus", "Confluence Ferry"}


def test_T5i_no_params_gives_overview(transit_client):
    resp = transit_client.get("/transit/query")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "overview"
    assert body["agencies"]


def test_T5j_transit_type_filters_to_ferry(transit_client):
    resp = transit_client.get("/transit/query", params={"transit_type": "ferry", "query": "confluence"})
    assert resp.status_code == 200
    body = resp.json()
    assert {s["stop_id"] for s in body["stops"]} == {"confluence_ferry_0"}


def test_T5k_lat_alone_gives_422(transit_client):
    resp = transit_client.get("/transit/query", params={"lat": 39.7})
    assert resp.status_code == 422


def test_T5l_configured_but_not_loaded_gives_503(monkeypatch):
    module = _reload_configured_transportation(monkeypatch, "_test_transit_wiring_t5l")
    module.transit_data["last_updated"] = None
    client = TestClient(module.app)
    resp = client.get("/transit/query")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Transit data not loaded yet"


def test_T5m_loaded_but_every_feed_failed_gives_503_with_fetch_errors(monkeypatch):
    module = _reload_configured_transportation(monkeypatch, "_test_transit_wiring_t5m")
    module.transit_data["last_updated"] = "2026-09-28T12:00:00"
    module.transit_data["stops"] = {}
    module.fetch_errors.clear()
    module.fetch_errors["rtd_bus"] = "SSRF blocked"
    client = TestClient(module.app)
    resp = client.get("/transit/query")
    assert resp.status_code == 503
    assert "rtd_bus: SSRF blocked" in resp.json()["detail"]


def test_T5n_lon_alone_gives_422(transit_client):
    resp = transit_client.get("/transit/query", params={"lon": -104.9})
    assert resp.status_code == 422


def test_T5o_empty_query_string_behaves_like_omitted(transit_client):
    resp = transit_client.get("/transit/query", params={"query": "", "lat": 39.7527, "lon": -104.9997})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "nearby"


def test_T5p_unmatched_transit_type_filter_gives_empty_200(transit_client):
    resp = transit_client.get("/transit/query", params={"transit_type": "monorail", "query": "union"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "search"
    assert body["stops"] == []
    assert body["routes"] == []


def test_T5q_stop_with_no_stop_times_gives_empty_departures_not_500(transit_client):
    resp = transit_client.get("/transit/query", params={"stop_id": "rtd_bus_1003"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["departures"] == []
    assert body["message"] == "No schedule data available"


# ---------------------------------------------------------------------------
# T6a -- is_transit_query predicate table
# ---------------------------------------------------------------------------

_TRUE_QUERIES = [
    "when's the next bus at union station",
    "is there a free shuttle downtown",
    "light rail to the stadium",
    "what time does the ferry leave",
    "route 15 schedule",
    "departures from civic center",
    "nearest subway stop",
    "water taxi to the harbor",
    "is the commuter rail running today",
    "what's the rail schedule this weekend",
]

_FALSE_QUERIES = [
    "how do I drive to the airport",
    "directions to the nearest gas station",
    "route to work",
    "walking directions to the park",
    "find a charging station on the way",
    "non-stop route to Denver",
    "I need to train my dog before we leave",
    "can you train the new hire on safety",
    "stop by the grocery store on the way home",
    "",
    None,
]


def _import_helpers_module():
    for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
        if _mod not in sys.modules:
            sys.modules[_mod] = mock.MagicMock()
    import os
    os.environ.setdefault("SERVICE_API_KEY", "test-key-transit-wiring")
    os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

    if "orchestrator.config_loader" not in sys.modules:
        from shared.config import get_config as _shared_get_config
        _config_loader_mock = mock.MagicMock()
        _config_loader_mock.get_config = _shared_get_config
        _config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
        _config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
        _config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
        _config_loader_mock.clear_cache = mock.AsyncMock()
        sys.modules["orchestrator.config_loader"] = _config_loader_mock

    import orchestrator.nodes  # noqa: F401 -- must precede orchestrator.helpers
    import orchestrator.helpers as helpers_module
    return helpers_module


@pytest.mark.parametrize("query", _TRUE_QUERIES)
def test_T6a_is_transit_query_true_cases(query):
    helpers_module = _import_helpers_module()
    assert helpers_module.is_transit_query(query) is True


@pytest.mark.parametrize("query", _FALSE_QUERIES)
def test_T6a_is_transit_query_false_cases(query):
    helpers_module = _import_helpers_module()
    assert helpers_module.is_transit_query(query) is False


def test_T6a_floor_named_members():
    helpers_module = _import_helpers_module()
    assert len(_TRUE_QUERIES) > 0
    assert len(_FALSE_QUERIES) > 0
    assert helpers_module.is_transit_query("how do I drive to the airport") is False
    assert helpers_module.is_transit_query("when's the next bus at union station") is True


# ---------------------------------------------------------------------------
# T6b/T6c -- reachability, AST
# ---------------------------------------------------------------------------

def test_T6b_directions_literal_unchanged_and_gate_exists():
    intent_to_tools = _extract_intent_to_tools()
    assert intent_to_tools["directions"] == ["get_directions", "search_restaurants"]

    tree = ast.parse(MAIN_PY.read_text())
    _find_directions_gate_if(tree)  # raises AssertionError if not found


_BASELINE_INTENT_TO_TOOLS = {
    "weather": ["get_weather"],
    "sports": ["get_sports_scores", "get_sports_standings"],
    "airports": ["get_airport_info"],
    "flights": ["search_flights", "get_train_schedule"],
    "events": ["search_events"],
    "streaming": ["search_streaming"],
    "news": ["get_news"],
    "stocks": ["get_stock_info"],
    "websearch": ["search_web", "scrape_website", "scrape_webpage_bright"],
    "scraping": ["scrape_webpage_bright", "scrape_website"],
    "dining": ["search_restaurants", "scrape_website"],
    "recipes": ["search_recipes"],
    "directions": ["get_directions", "search_restaurants"],
    "transit": ["get_train_schedule", "get_directions"],
    "shopping": ["compare_prices", "search_web"],
    "tesla": ["get_tesla_metrics"],
    "media": ["request_media"],
    "planning": ["get_weather", "search_events", "search_restaurants"],
    "itinerary": ["get_weather", "search_events", "search_restaurants"],
}


def test_T6c_intent_to_tools_differs_from_baseline_in_exactly_one_key():
    current = _extract_intent_to_tools()
    expected = {**_BASELINE_INTENT_TO_TOOLS, "transit": ["get_train_schedule", "get_directions", "search_transit"]}
    assert current == expected

    changed_keys = {
        key for key in _BASELINE_INTENT_TO_TOOLS
        if _BASELINE_INTENT_TO_TOOLS[key] != current.get(key)
    }
    assert changed_keys == {"transit"}


# ---------------------------------------------------------------------------
# T7 -- migration 059 schema/chain drift guard
# ---------------------------------------------------------------------------

def _load_migration_059():
    path = _REPO_ROOT / "admin" / "backend" / "alembic" / "versions" / "059_seed_search_transit_tool.py"
    spec = importlib.util.spec_from_file_location("_test_migration_059", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_T7_schema_equality_and_revision_chain():
    import orchestrator.rag_tools as rag_tools

    migration = _load_migration_059()
    live_schema = next(
        t["function_schema"] for t in rag_tools.TOOL_DEFINITIONS if t["tool_name"] == "search_transit"
    )

    assert migration.SEARCH_TRANSIT_SCHEMA == live_schema
    assert "Give stop_id for next departures" in live_schema["function"]["description"]

    assert migration.revision == "059"
    assert migration.down_revision == "058"

    versions_dir = _REPO_ROOT / "admin" / "backend" / "alembic" / "versions"
    other_058_children = []
    for f in versions_dir.glob("*.py"):
        if f.name == "059_seed_search_transit_tool.py":
            continue
        text = f.read_text()
        if 'down_revision = "058"' in text or "down_revision = '058'" in text:
            other_058_children.append(f.name)
    assert other_058_children == []


# ---------------------------------------------------------------------------
# T8 (renamed from r1 T7) -- precedence when multiple params given at once
# ---------------------------------------------------------------------------

def test_T8_stop_id_wins_over_everything_else(transit_client):
    resp = transit_client.get(
        "/transit/query",
        params={"stop_id": "rtd_bus_1001", "lat": 39.7, "lon": -104.9, "query": "civic", "free_only": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "departures"
    assert body["resolved_stop_id"] == "rtd_bus_1001"


def test_T8_query_wins_over_lat_lon_when_no_stop_id(transit_client):
    resp = transit_client.get(
        "/transit/query", params={"lat": 39.7527, "lon": -104.9997, "query": "union"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "search"
