"""ATHENA-89 Phase 2 — region-configurable RAG services.

Covers plan/contract R1-R12 (incl. R3b, R3c, R5b, R10b, R11 boundary,
R11b's two required sub-cases, R11c parametrized over two reasons) and C1
(config.py fields, also covered directly in test_config.py).

Each of transportation/community_events/amtrak's main.py is loaded under a
private module name via importlib (all three files are literally named
main.py, so a plain `import main` would collide). `importlib.reload` is
used for subsequent env-var changes within the same test session, matching
the plan's fixture note that these modules resolve their config at import
time against an lru_cache'd get_config().
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import io
import json
import sys
import zipfile
import unittest.mock as mock
from pathlib import Path

import pytest
import structlog.testing
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "community_events"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

from shared.config import _clear_cache_for_tests  # noqa: E402
from shared.url_safety import SsrfBlockedError  # noqa: E402


def _import_service(unique_name: str, service_dir: str):
    """Fresh-import (or re-execute) a RAG service's main.py under a private
    name. `importlib.reload` can't be used here: its spec-search machinery
    requires the module to be resolvable by name via sys.meta_path, which a
    synthetic name loaded via spec_from_file_location is not. Re-running
    exec_module on the existing module object achieves the same "fresh
    module-level state" outcome without that constraint."""
    path = _SRC / "rag" / service_dir / "main.py"
    if unique_name in sys.modules:
        module = sys.modules[unique_name]
        module.__spec__.loader.exec_module(module)
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


_TRANSIT_ENV_VARS = ("TRANSIT_REGION_NAME", "TRANSIT_GTFS_FEEDS", "TRANSIT_STATIC_SERVICES")
_COMMUNITY_ENV_VARS = ("COMMUNITY_EVENTS_SOURCES",)
_AMTRAK_ENV_VARS = ("DEFAULT_AMTRAK_STATION",)


def _reload_transportation(monkeypatch, **env):
    for var in _TRANSIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _clear_cache_for_tests()
    return _import_service("_test_transportation_main", "transportation")


def _reload_community(monkeypatch, **env):
    for var in _COMMUNITY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _clear_cache_for_tests()
    return _import_service("_test_community_events_main", "community_events")


def _reload_amtrak(monkeypatch, **env):
    for var in _AMTRAK_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _clear_cache_for_tests()
    return _import_service("_test_amtrak_main", "amtrak")


@pytest.fixture(autouse=True)
def _clear_config_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# R1: transportation unconfigured
# ---------------------------------------------------------------------------


def test_R1_unconfigured_health_and_9_routes_503(monkeypatch):
    module = _reload_transportation(monkeypatch)
    client = TestClient(module.app)

    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["configured"] is False
    assert "TRANSIT_GTFS_FEEDS" in body["message"]

    data_routes = [
        ("GET", "/transit/nearby?lat=39.7&lon=-104.9"),
        ("GET", "/transit/routes"),
        ("GET", "/transit/departures?stop_id=x"),
        ("GET", "/transit/route/x"),
        ("GET", "/transit/search?query=ab"),
        ("GET", "/transit/water"),
        ("GET", "/transit/agencies"),
        ("GET", "/transit/free"),
        ("POST", "/transit/refresh"),
        ("GET", "/transit/query"),
    ]
    assert len(data_routes) == 10
    for method, path in data_routes:
        resp = client.request(method, path)
        assert resp.status_code == 503, f"{method} {path} -> {resp.status_code}"
        assert "TRANSIT_GTFS_FEEDS" in resp.json()["detail"]

    # Population check (not just a spot check): every route gated by
    # require_transit_configured must be one of the 10 above, and every one
    # of the 10 must actually be gated -- a future route that forgets the
    # Depends() (or the reverse) fails loudly here instead of shipping an
    # ungated data route.
    from shared.route_walk import dependency_calls, iter_api_routes

    gated_paths = {
        walked.path
        for walked in iter_api_routes(module.app)
        if module.require_transit_configured in dependency_calls(walked)
    }

    expected_paths = {
        "/transit/nearby", "/transit/routes", "/transit/departures",
        "/transit/route/{route_id}", "/transit/search", "/transit/water",
        "/transit/agencies", "/transit/free", "/transit/refresh", "/transit/query",
    }
    assert gated_paths == expected_paths


def test_R1_named_member_transit_free_503(monkeypatch):
    module = _reload_transportation(monkeypatch)
    client = TestClient(module.app)
    resp = client.get("/transit/free")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# R2: one-feed/one-static-service Denver fixture
# ---------------------------------------------------------------------------


def test_R2_configured_denver_fixture_reflects_seeded_data(monkeypatch):
    feeds = json.dumps({
        "rtd_bus": {"name": "RTD Bus", "agency": "rtd", "url": "https://example.org/gtfs.zip", "type": "bus", "free": True}
    })
    static = json.dumps({
        "confluence_ferry": {
            "name": "Confluence Ferry",
            "type": "ferry",
            "free": True,
            "hours": {"weekday": {"start": "07:00", "end": "19:00"}, "weekend": None},
            "frequency_minutes": 20,
            "stops": [{"name": "Confluence Park", "lat": 39.7527, "lon": -105.0089}],
            "agency_name": "Denver Parks & Rec",
        }
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds, TRANSIT_STATIC_SERVICES=static)

    assert module.transit_config.configured is True
    assert len(module.transit_config.feeds) == 1
    assert len(module.transit_config.static_services) == 1

    # Seed in-memory data directly -- the real GTFS fetch is covered by
    # R11/R11b/R11c, out of scope for this test.
    module.transit_data["routes"]["rtd_bus_1"] = {
        "route_id": "rtd_bus_1", "route_short_name": "1", "route_long_name": "Downtown",
        "route_type": 3, "feed_id": "rtd_bus", "agency_id": "", "route_color": "", "route_text_color": "",
    }
    module.transit_data["last_updated"] = "now"

    client = TestClient(module.app)

    free = client.get("/transit/free")
    assert free.status_code == 200
    names = {o["name"] for o in free.json()["free_transit_options"]}
    assert names == {"RTD Bus", "Confluence Ferry"}

    agencies = client.get("/transit/agencies")
    assert agencies.status_code == 200
    agency_ids = {a["agency_id"] for a in agencies.json()["agencies"]}
    assert "confluence_ferry" in agency_ids


# ---------------------------------------------------------------------------
# R3 / R3b / R3c
# ---------------------------------------------------------------------------


def test_R3_garbage_json_transit_configured_false_key_named(monkeypatch):
    module = _reload_transportation(
        monkeypatch,
        TRANSIT_GTFS_FEEDS="{not valid json",
        TRANSIT_STATIC_SERVICES="also not json{{{",
    )
    assert module.transit_config.configured is False
    assert "TRANSIT_GTFS_FEEDS" in module.transit_config.error


def test_R3_garbage_json_community_configured_false_key_named(monkeypatch):
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES="[not json")
    assert module.event_sources_config.configured is False
    assert "COMMUNITY_EVENTS_SOURCES" in module.event_sources_config.error


def test_R3b_lifespan_survives_malformed_transit_json(monkeypatch):
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS="{broken")
    with TestClient(module.app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["config_error"] is not None


def test_R3b_lifespan_survives_malformed_community_json(monkeypatch):
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES="[broken")
    with TestClient(module.app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["config_error"] is not None


def test_R3c_transit_feed_missing_url_names_feed_not_keyerror(monkeypatch):
    feeds = json.dumps({"badfeed": {"name": "Bad Feed"}})
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)
    assert module.transit_config.configured is False
    assert "badfeed" in module.transit_config.error
    assert "url" in module.transit_config.error


def test_R3c_community_source_missing_name_not_keyerror(monkeypatch):
    sources = json.dumps([{"type": "link_scan", "url": "https://example.org"}])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)
    assert module.event_sources_config.configured is False
    assert "missing 'name'" in module.event_sources_config.error


# ---------------------------------------------------------------------------
# R4: community_events unconfigured
# ---------------------------------------------------------------------------


def test_R4_community_unconfigured(monkeypatch):
    module = _reload_community(monkeypatch)
    client = TestClient(module.app)

    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["configured"] is False
    assert "COMMUNITY_EVENTS_SOURCES" in body["message"]

    assert client.get("/events/search").status_code == 503
    assert client.post("/events/refresh").status_code == 503

    sources = client.get("/events/sources")
    assert sources.status_code == 200
    assert sources.json() == {"configured": False, "sources": []}


# ---------------------------------------------------------------------------
# R5 / R5b
# ---------------------------------------------------------------------------


def test_R5_unknown_type_rejected(monkeypatch):
    sources = json.dumps([{"name": "X", "type": "not_a_type", "url": "https://example.org"}])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)
    assert module.event_sources_config.configured is False
    assert "not_a_type" in module.event_sources_config.error


@pytest.mark.parametrize("source_type", ["link_scan", "event_cards", "tribe_events_api", "squarespace_eventlist"])
def test_R5_known_types_accepted(monkeypatch, source_type):
    sources = json.dumps([{"name": "X", "type": source_type, "url": "https://example.org"}])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)
    assert module.event_sources_config.configured is True


def test_R5_scrapers_dispatch_keys(monkeypatch):
    module = _reload_community(monkeypatch)
    assert set(module.SCRAPERS) == {"link_scan", "event_cards", "tribe_events_api", "squarespace_eventlist"}


_D3_DEFAULTS = {
    "link_scan": {"category": "community", "is_free": True},
    "event_cards": {"category": "community", "is_free": False},
    "tribe_events_api": {"category": "downtown", "is_free": False},  # always computed per-event
    "squarespace_eventlist": {"category": "neighborhood", "is_free": True},
}


def test_R5b_population_is_4():
    assert len(_D3_DEFAULTS) == 4


@pytest.mark.parametrize("source_type,expected", list(_D3_DEFAULTS.items()))
def test_R5b_per_type_defaults_match_d3(monkeypatch, source_type, expected):
    sources = json.dumps([{"name": "X", "type": source_type, "url": "https://example.org"}])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)
    src = module.event_sources_config.sources[0]
    assert src.category == expected["category"]
    if source_type != "tribe_events_api":
        assert src.is_free == expected["is_free"]


def test_R5b_tribe_events_api_is_free_not_independently_configurable(monkeypatch):
    """Named member: tribe_events_api -> category='downtown'; an explicit
    is_free in the source JSON is ignored (D3) -- the real per-event value
    is always computed from the API's own `cost` field at scrape time."""
    sources = json.dumps([{"name": "X", "type": "tribe_events_api", "url": "https://example.org", "is_free": True}])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)
    src = module.event_sources_config.sources[0]
    assert src.category == "downtown"
    assert src.is_free is False


# ---------------------------------------------------------------------------
# R6: tribe_events_api fixture (venue without city -> default_city/state)
# ---------------------------------------------------------------------------


def test_R6_tribe_events_api_fixture_uses_configured_name_and_default_city_state(monkeypatch):
    fixture = json.loads((_FIXTURES / "tribe_events.json").read_text())
    sources = json.dumps([{
        "name": "Downtown Denver Partnership",
        "type": "tribe_events_api",
        "url": "https://example.org/wp-json/tribe/events/v1/events",
        "default_location": "Downtown Denver",
        "default_address": "Denver, CO",
        "default_city": "Denver",
        "default_state": "CO",
        "category": "downtown",
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    fake_response = mock.MagicMock()
    fake_response.raise_for_status = mock.MagicMock()
    fake_response.json.return_value = fixture

    async def _fake_safe_get(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)

    src = module.event_sources_config.sources[0]
    events = asyncio.run(module.scrape_tribe_events_api_source(src))

    assert len(events) == 1
    event = events[0]
    assert event["source"] == "Downtown Denver Partnership"
    assert event["address"].endswith("Denver, CO")
    assert event["location"] == "Sample Venue"


# ---------------------------------------------------------------------------
# R7: squarespace_eventlist fixture (location_match)
# ---------------------------------------------------------------------------


def test_R7_squarespace_eventlist_location_match(monkeypatch):
    html = (_FIXTURES / "squarespace.html").read_text()
    sources = json.dumps([{
        "name": "Test Neighborhood Association",
        "type": "squarespace_eventlist",
        "url": "https://example.org/events",
        "base_url": "https://example.org",
        "default_location": "Test Neighborhood, Denver",
        "default_address": "Denver, CO 80202",
        "location_match": ["Denver"],
        "category": "neighborhood",
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    fake_response = mock.MagicMock()
    fake_response.raise_for_status = mock.MagicMock()
    fake_response.text = html

    async def _fake_safe_get(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)

    src = module.event_sources_config.sources[0]
    events = asyncio.run(module.scrape_squarespace_eventlist_source(src))
    assert len(events) == 1
    assert events[0]["location"] == "Denver, CO 80202"

    import dataclasses
    src_no_match = dataclasses.replace(src, location_match=["Nowhere"])
    events2 = asyncio.run(module.scrape_squarespace_eventlist_source(src_no_match))
    assert events2[0]["location"] == "Test Neighborhood, Denver"


# ---------------------------------------------------------------------------
# link_scan / event_cards fixture smoke (all 4 fixture files exercised)
# ---------------------------------------------------------------------------


def test_link_scan_fixture_smoke(monkeypatch):
    html = (_FIXTURES / "link_scan.html").read_text()
    sources = json.dumps([{
        "name": "Sample Link Scan Source",
        "type": "link_scan",
        "url": "https://example.org/events-calendar",
        "base_url": "https://example.org",
        "link_path": "/events-calendar/",
        "default_location": "Sample Region",
        "default_address": "Sample Region, CO",
        "is_free": True,
        "category": "community",
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    fake_response = mock.MagicMock()
    fake_response.raise_for_status = mock.MagicMock()
    fake_response.text = html

    async def _fake_safe_get(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)

    events = asyncio.run(module.scrape_link_scan_source(module.event_sources_config.sources[0]))
    assert len(events) == 1
    assert events[0]["title"] == "Sample Riverfront Cleanup"
    assert events[0]["location"] == "Confluence Park"
    assert events[0]["source"] == "Sample Link Scan Source"
    assert events[0]["is_free"] is True


def test_event_cards_fixture_smoke(monkeypatch):
    html = (_FIXTURES / "event_cards.html").read_text()
    sources = json.dumps([{
        "name": "Sample Event Cards Source",
        "type": "event_cards",
        "url": "https://example.org/events/",
        "base_url": "https://example.org",
        "link_path": "/event/",
        "default_location": "Sample City",
        "default_address": "Sample City, CO",
        "is_free": False,
        "category": "community",
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    fake_response = mock.MagicMock()
    fake_response.raise_for_status = mock.MagicMock()
    fake_response.text = html

    async def _fake_safe_get(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)

    events = asyncio.run(module.scrape_event_cards_source(module.event_sources_config.sources[0]))
    assert len(events) == 1
    assert events[0]["title"] == "Sample Arts Festival"
    assert events[0]["location"] == "City Park Pavilion"
    assert events[0]["is_free"] is False


# ---------------------------------------------------------------------------
# R8: amtrak
# ---------------------------------------------------------------------------


def test_R8_amtrak_unset_schedule_400_query_error_dict_health_null(monkeypatch):
    module = _reload_amtrak(monkeypatch)
    monkeypatch.setattr(module, "load_gtfs", lambda: None)
    client = TestClient(module.app)

    schedule = client.get("/amtrak/schedule", params={"destination": "NYP"})
    assert schedule.status_code == 400
    assert "DEFAULT_AMTRAK_STATION" in schedule.json()["detail"]

    query = client.get("/amtrak/query", params={"query": "train to new york"})
    assert query.status_code == 200
    assert "error" in query.json()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["default_origin"] is None
    assert "DEFAULT_AMTRAK_STATION" in health.json()["message"]


def test_R8_amtrak_set_default_origin_uppercased(monkeypatch):
    module = _reload_amtrak(monkeypatch, DEFAULT_AMTRAK_STATION="was")
    assert module.DEFAULT_ORIGIN == "WAS"


# ---------------------------------------------------------------------------
# R9: rag_tools TOOL_DEFINITIONS free of maintainer literals
# ---------------------------------------------------------------------------


def test_R9_tool_schemas_free_of_maintainer_literals():
    for _mod in ("langgraph", "langgraph.graph"):
        if _mod not in sys.modules:
            sys.modules[_mod] = mock.MagicMock()
    import orchestrator.rag_tools as rag_tools

    targets = {"search_transit", "get_train_schedule", "search_restaurants"}
    banned = ["Baltimore", "Charm City", "Harbor Connector", "39.2904", "-76.6122", "Cowboy Rose", "Ikaros"]

    checked = 0
    for tool in rag_tools.TOOL_DEFINITIONS:
        if tool["tool_name"] in targets:
            checked += 1
            text = json.dumps(tool["function_schema"])
            for term in banned:
                assert term not in text, f"{tool['tool_name']} schema still contains {term!r}"
    assert checked == len(targets)


# ---------------------------------------------------------------------------
# R10 / R10b: AST/text scan for house literals and raw HTTP client use
# ---------------------------------------------------------------------------


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


_R10_MODULES = [
    "src/rag/transportation/main.py",
    "src/rag/community_events/main.py",
    "src/rag/amtrak/main.py",
]


def test_R10_population_is_3():
    assert len(_R10_MODULES) == 3


@pytest.mark.parametrize("rel_path", _R10_MODULES)
def test_R10_no_house_literals_in_string_constants(rel_path):
    strings = _string_constants(_REPO_ROOT / rel_path)
    joined_lower = "\n".join(strings).lower()
    for term in ("feeds.mta.maryland.gov", "waterfrontpartnership"):
        assert term not in joined_lower, f"{rel_path} still contains {term!r}"
    if "amtrak" not in rel_path:
        # amtrak's STATION_ALIASES national table keeps "baltimore"/"baltimore
        # penn" (allowlisted, P0 D5); transportation/community_events must
        # have none.
        assert "baltimore" not in joined_lower, f"{rel_path} still contains 'baltimore'"


def test_R10_named_member_transportation():
    strings = _string_constants(_REPO_ROOT / "src/rag/transportation/main.py")
    assert not any("feeds.mta.maryland.gov" in s.lower() for s in strings)


@pytest.mark.parametrize("rel_path", ["src/rag/transportation/main.py", "src/rag/community_events/main.py"])
def test_R10b_no_raw_http_client_imports_safe_get(rel_path):
    text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert "http_client.get(" not in text
    assert "httpx.AsyncClient(" not in text
    assert "safe_get" in text or "safe_request" in text


# ---------------------------------------------------------------------------
# R11: bounds filtering (in-memory GTFS zip)
# ---------------------------------------------------------------------------


def _build_gtfs_zip(stops_rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        header = "stop_id,stop_name,stop_lat,stop_lon\n"
        body = "".join(
            f"{r['stop_id']},{r['stop_name']},{r['stop_lat']},{r['stop_lon']}\n" for r in stops_rows
        )
        zf.writestr("stops.txt", header + body)
    return buf.getvalue()


def _fake_response_for_zip(zip_bytes: bytes):
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.content = zip_bytes
    return resp


def test_R11_bounds_drops_out_of_box_keeps_in_box(monkeypatch):
    zip_bytes = _build_gtfs_zip([
        {"stop_id": "in", "stop_name": "In Box", "stop_lat": 39.75, "stop_lon": -105.0},
        {"stop_id": "out", "stop_name": "Out Of Box", "stop_lat": 40.5, "stop_lon": -105.0},
    ])
    feeds = json.dumps({
        "test_feed": {
            "name": "Test Feed", "agency": "test", "url": "https://example.org/gtfs.zip",
            "type": "bus", "free": False,
            "bounds": {"min_lat": 39.5, "max_lat": 39.9, "min_lon": -105.3, "max_lon": -104.7},
        }
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)

    fake_response = _fake_response_for_zip(zip_bytes)

    async def _fake_safe_get(*a, **k):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    result = asyncio.run(module.download_and_parse_gtfs("test_feed", module.transit_config.feeds["test_feed"]))
    stop_ids = {s["stop_id"] for s in result["stops"]}
    assert stop_ids == {"test_feed_in"}


def test_R11_no_bounds_keeps_both(monkeypatch):
    zip_bytes = _build_gtfs_zip([
        {"stop_id": "in", "stop_name": "In Box", "stop_lat": 39.75, "stop_lon": -105.0},
        {"stop_id": "out", "stop_name": "Out Of Box", "stop_lat": 40.5, "stop_lon": -105.0},
    ])
    feeds = json.dumps({
        "test_feed": {"name": "Test Feed", "agency": "test", "url": "https://example.org/gtfs.zip", "type": "bus", "free": False}
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)

    fake_response = _fake_response_for_zip(zip_bytes)

    async def _fake_safe_get(*a, **k):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    result = asyncio.run(module.download_and_parse_gtfs("test_feed", module.transit_config.feeds["test_feed"]))
    stop_ids = {s["stop_id"] for s in result["stops"]}
    assert stop_ids == {"test_feed_in", "test_feed_out"}


def test_R11_boundary_exact_edge_excluded(monkeypatch):
    """A stop exactly on a bounds edge is dropped (exclusive comparison)."""
    zip_bytes = _build_gtfs_zip([
        {"stop_id": "edge", "stop_name": "On Edge", "stop_lat": 39.5, "stop_lon": -105.0},  # lat == min_lat
        {"stop_id": "inside", "stop_name": "Inside", "stop_lat": 39.6, "stop_lon": -105.0},
    ])
    feeds = json.dumps({
        "test_feed": {
            "name": "Test Feed", "agency": "test", "url": "https://example.org/gtfs.zip", "type": "bus", "free": False,
            "bounds": {"min_lat": 39.5, "max_lat": 39.9, "min_lon": -105.3, "max_lon": -104.7},
        }
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)
    fake_response = _fake_response_for_zip(zip_bytes)

    async def _fake_safe_get(*a, **k):
        return fake_response

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    result = asyncio.run(module.download_and_parse_gtfs("test_feed", module.transit_config.feeds["test_feed"]))
    stop_ids = {s["stop_id"] for s in result["stops"]}
    assert stop_ids == {"test_feed_inside"}


_INVALID_BOUNDS_SHAPES = [
    {"min_lat": 39.5, "max_lat": 39.9, "min_lon": -105.3},  # missing max_lon
    {"min_lat": 39.5, "max_lat": 39.9, "min_lon": -105.3, "max_lon": "not-a-number"},
    {"min_lat": 40.0, "max_lat": 39.9, "min_lon": -105.3, "max_lon": -104.7},  # min >= max
]


def test_R11_invalid_bounds_shapes_population_is_3():
    assert len(_INVALID_BOUNDS_SHAPES) == 3


@pytest.mark.parametrize("bounds", _INVALID_BOUNDS_SHAPES)
def test_R11_invalid_bounds_configured_false_names_feed(monkeypatch, bounds):
    feeds = json.dumps({
        "test_feed": {
            "name": "Test Feed", "agency": "test", "url": "https://example.org/gtfs.zip",
            "type": "bus", "free": False, "bounds": bounds,
        }
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)
    assert module.transit_config.configured is False
    assert "test_feed" in module.transit_config.error


def test_R11_string_numeric_bounds_normalized_to_float_at_load(monkeypatch):
    """DC14 item 5: a JSON author who quoted their numbers ("39.5" instead
    of 39.5) is valid (float("39.5") succeeds) and must be NORMALIZED to a
    real float at load time -- not stored as the original string -- so
    _in_bounds's `bounds["min_lat"] < lat` comparison at actual filter time
    doesn't hit a str/float TypeError the first time a stop is checked."""
    feeds = json.dumps({
        "test_feed": {
            "name": "Test Feed", "agency": "test", "url": "https://example.org/gtfs.zip",
            "type": "bus", "free": False,
            "bounds": {"min_lat": "39.5", "max_lat": "39.9", "min_lon": "-105.3", "max_lon": "-104.7"},
        }
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)
    assert module.transit_config.configured is True
    bounds = module.transit_config.feeds["test_feed"]["bounds"]
    assert bounds == {"min_lat": 39.5, "max_lat": 39.9, "min_lon": -105.3, "max_lon": -104.7}
    for v in bounds.values():
        assert isinstance(v, float)

    # And _in_bounds actually works against the normalized values -- this
    # is the real regression: pre-fix, this call raised TypeError.
    assert module._in_bounds(bounds, 39.7, -105.0) is True
    assert module._in_bounds(bounds, 10.0, -105.0) is False


# ---------------------------------------------------------------------------
# R11b: SSRF allow_private composition (both required sub-cases)
# ---------------------------------------------------------------------------


def test_R11b_transit_default_path_no_allowed_private_hosts_kwarg(monkeypatch):
    feeds = json.dumps({
        "test_feed": {"name": "Test Feed", "agency": "test", "url": "https://internal.example.org/gtfs.zip", "type": "bus", "free": False}
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)

    captured: dict = {}

    async def _fake_safe_get(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        raise SsrfBlockedError("blocked for test capture")

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    asyncio.run(module.download_and_parse_gtfs("test_feed", module.transit_config.feeds["test_feed"]))

    assert "allowed_private_hosts" not in captured
    assert captured["max_hops"] == 5
    assert captured["max_bytes"] == 100 * 2**20


def test_R11b_transit_allow_private_true_passes_only_that_host(monkeypatch):
    feeds = json.dumps({
        "test_feed": {
            "name": "Test Feed", "agency": "test", "url": "https://internal.example.org/gtfs.zip",
            "type": "bus", "free": False, "allow_private": True, "max_bytes": 12345,
        }
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)

    captured: dict = {}

    async def _fake_safe_get(url, **kwargs):
        captured.update(kwargs)
        raise SsrfBlockedError("blocked for test capture")

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    asyncio.run(module.download_and_parse_gtfs("test_feed", module.transit_config.feeds["test_feed"]))

    assert captured["allowed_private_hosts"] == ["internal.example.org"]
    assert captured["max_hops"] == 5
    assert captured["max_bytes"] == 12345


def test_R11b_community_default_path_no_allowed_private_hosts_kwarg(monkeypatch):
    sources = json.dumps([{
        "name": "Test Source", "type": "link_scan", "url": "https://internal.example.org/events", "link_path": "/events/"
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    captured: dict = {}

    async def _fake_safe_get(url, **kwargs):
        captured.update(kwargs)
        raise SsrfBlockedError("blocked for test capture")

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    asyncio.run(module.scrape_link_scan_source(module.event_sources_config.sources[0]))

    assert "allowed_private_hosts" not in captured
    assert captured["max_hops"] == 5
    assert captured["max_bytes"] == 5 * 2**20


def test_R11b_community_allow_private_true_passes_only_that_host(monkeypatch):
    sources = json.dumps([{
        "name": "Test Source", "type": "link_scan", "url": "https://internal.example.org/events",
        "link_path": "/events/", "allow_private": True, "max_bytes": 999,
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    captured: dict = {}

    async def _fake_safe_get(url, **kwargs):
        captured.update(kwargs)
        raise SsrfBlockedError("blocked for test capture")

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)
    asyncio.run(module.scrape_link_scan_source(module.event_sources_config.sources[0]))

    assert captured["allowed_private_hosts"] == ["internal.example.org"]
    assert captured["max_bytes"] == 999


# ---------------------------------------------------------------------------
# R11c: SsrfBlockedError caught, logged, surfaced in /health -- never a 5xx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["redirect target resolves to a private address", "response exceeded max_bytes cap"],
)
def test_R11c_transit_ssrf_blocked_surfaces_in_health_not_5xx(monkeypatch, reason):
    feeds = json.dumps({
        "test_feed": {"name": "Test Feed", "agency": "test", "url": "https://internal.example.org/gtfs.zip", "type": "bus", "free": False}
    })
    module = _reload_transportation(monkeypatch, TRANSIT_GTFS_FEEDS=feeds)

    async def _fake_safe_get(*a, **k):
        raise SsrfBlockedError(reason)

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)

    with structlog.testing.capture_logs() as captured_logs:
        result = asyncio.run(module.download_and_parse_gtfs("test_feed", module.transit_config.feeds["test_feed"]))

    assert result == {"stops": [], "routes": [], "stop_times": [], "agencies": []}
    assert module.fetch_errors["test_feed"] == reason

    warnings = [e for e in captured_logs if e.get("event") == "transit_feed_ssrf_blocked"]
    assert len(warnings) == 1

    client = TestClient(module.app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["configured"] is True
    assert reason in health.json()["message"]


@pytest.mark.parametrize(
    "reason",
    ["redirect target resolves to a private address", "response exceeded max_bytes cap"],
)
def test_R11c_community_ssrf_blocked_surfaces_in_health_not_5xx(monkeypatch, reason):
    sources = json.dumps([{
        "name": "Test Source", "type": "link_scan", "url": "https://internal.example.org/events", "link_path": "/events/"
    }])
    module = _reload_community(monkeypatch, COMMUNITY_EVENTS_SOURCES=sources)

    async def _fake_safe_get(*a, **k):
        raise SsrfBlockedError(reason)

    monkeypatch.setattr(module, "safe_get", _fake_safe_get)

    with structlog.testing.capture_logs() as captured_logs:
        events = asyncio.run(module.scrape_link_scan_source(module.event_sources_config.sources[0]))

    assert events == []
    warnings = [e for e in captured_logs if e.get("event") == "community_source_ssrf_blocked"]
    assert len(warnings) == 1

    client = TestClient(module.app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["configured"] is True
    assert reason in health.json()["message"]


# ---------------------------------------------------------------------------
# R12: config.yaml lists every AthenaConfig field + DEFAULT_STATE
# ---------------------------------------------------------------------------


def test_R12_config_yaml_lists_every_athena_config_field():
    spec = importlib.util.spec_from_file_location(
        "_test_check_env_example", _REPO_ROOT / "scripts" / "check-env-example.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = module.collect_config_field_names()
    names.add("DEFAULT_STATE")

    assert len(names) >= 34
    assert "TRANSIT_GTFS_FEEDS" in names

    config_yaml_text = (_REPO_ROOT / "manifests" / "athena-prod" / "config.yaml").read_text()
    missing = sorted(n for n in names if n not in config_yaml_text)
    assert not missing, f"missing from config.yaml: {missing}"
    assert "NEVER `kubectl apply -f`" in config_yaml_text
