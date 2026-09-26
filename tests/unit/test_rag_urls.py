"""Unit tests for the RAG URL single source of truth (ATHENA-87, F81).

`orchestrator.urls` must be the only module that reads RAG service URL
environment variables. `orchestrator.rag_tools` and
`orchestrator.utils.constants` must re-export from it. Canonical spelling is
`RAG_<NAME>_URL` (matches the deployed manifest); the legacy `<NAME>_RAG_URL`
spelling is still honoured with a warning.

Module-import harness mirrors tests/unit/test_retrieve.py:51-55: stub heavy
deps, put `src` on sys.path, then vary env with monkeypatch and
importlib.reload the three modules in dependency order (urls -> rag_tools ->
utils.constants) so each test observes a clean re-resolution.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import sys
import unittest.mock as mock
from pathlib import Path

import pytest
import structlog
import yaml

# Stub heavy deps before any orchestrator import.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import orchestrator.urls as urls  # noqa: E402
import orchestrator.rag_tools as rag_tools  # noqa: E402
import orchestrator.utils.constants as constants  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = REPO_ROOT / "manifests" / "athena-prod" / "orchestrator.yaml"
URLS_PY_PATH = REPO_ROOT / "src" / "orchestrator" / "urls.py"
SRC_ROOT = REPO_ROOT / "src"

CANONICAL_ONLY_RE = re.compile(r"^RAG_[A-Z]+_URL$")
EITHER_SPELLING_RE = re.compile(r"^(RAG_[A-Z_]+_URL|[A-Z_]+_RAG_URL)$")

# The 23-row "Pattern parity / wiring sites" table from the plan. Each row:
# (urls_attr, canonical_env, legacy_env_or_None, default_port,
#  rag_tools_attr_or_None, constants_attr_or_None)
WIRING_TABLE = [
    ("WEATHER_SERVICE_URL", "RAG_WEATHER_URL", "WEATHER_RAG_URL", 8010, "WEATHER_URL", "WEATHER_SERVICE_URL"),
    ("ONECALL_SERVICE_URL", "RAG_ONECALL_URL", None, 8021, None, None),
    ("AIRPORTS_SERVICE_URL", "RAG_AIRPORTS_URL", "AIRPORTS_RAG_URL", 8011, "AIRPORTS_URL", "AIRPORTS_SERVICE_URL"),
    ("STOCKS_SERVICE_URL", "RAG_STOCKS_URL", "STOCKS_RAG_URL", 8012, "STOCKS_URL", "STOCKS_SERVICE_URL"),
    ("FLIGHTS_SERVICE_URL", "RAG_FLIGHTS_URL", "FLIGHTS_RAG_URL", 8013, "FLIGHTS_URL", "FLIGHTS_SERVICE_URL"),
    ("EVENTS_SERVICE_URL", "RAG_EVENTS_URL", "EVENTS_RAG_URL", 8014, "EVENTS_URL", "EVENTS_SERVICE_URL"),
    ("STREAMING_SERVICE_URL", "RAG_STREAMING_URL", "STREAMING_RAG_URL", 8015, "STREAMING_URL", "STREAMING_SERVICE_URL"),
    ("NEWS_SERVICE_URL", "RAG_NEWS_URL", "NEWS_RAG_URL", 8016, "NEWS_URL", "NEWS_SERVICE_URL"),
    ("SPORTS_SERVICE_URL", "RAG_SPORTS_URL", "SPORTS_RAG_URL", 8017, "SPORTS_URL", "SPORTS_SERVICE_URL"),
    ("WEBSEARCH_SERVICE_URL", "RAG_WEBSEARCH_URL", "WEBSEARCH_RAG_URL", 8018, "WEBSEARCH_URL", "WEBSEARCH_SERVICE_URL"),
    ("DINING_SERVICE_URL", "RAG_DINING_URL", "DINING_RAG_URL", 8019, "DINING_URL", "DINING_SERVICE_URL"),
    ("RECIPES_SERVICE_URL", "RAG_RECIPES_URL", "RECIPES_RAG_URL", 8020, "RECIPES_URL", "RECIPES_SERVICE_URL"),
    ("DIRECTIONS_SERVICE_URL", "RAG_DIRECTIONS_URL", "DIRECTIONS_RAG_URL", 8030, "DIRECTIONS_URL", "DIRECTIONS_SERVICE_URL"),
    ("COMMUNITY_EVENTS_SERVICE_URL", "RAG_COMMUNITY_URL", "COMMUNITY_EVENTS_RAG_URL", 8026, "COMMUNITY_EVENTS_URL", None),
    ("SERPAPI_EVENTS_SERVICE_URL", "RAG_SERPAPI_URL", "SERPAPI_EVENTS_RAG_URL", 8032, "SERPAPI_EVENTS_URL", None),
    ("SEATGEEK_EVENTS_SERVICE_URL", "RAG_SEATGEEK_URL", "SEATGEEK_EVENTS_RAG_URL", 8024, "SEATGEEK_EVENTS_URL", None),
    ("TRANSPORTATION_SERVICE_URL", "RAG_TRANSPORTATION_URL", "TRANSPORTATION_RAG_URL", 8025, "TRANSPORTATION_URL", None),
    ("AMTRAK_SERVICE_URL", "RAG_AMTRAK_URL", "AMTRAK_RAG_URL", 8027, "AMTRAK_URL", None),
    ("SITE_SCRAPER_SERVICE_URL", "RAG_SITESCRAPER_URL", "SITE_SCRAPER_RAG_URL", 8031, "SITE_SCRAPER_URL", None),
    ("PRICE_COMPARE_SERVICE_URL", "RAG_PRICECOMPARE_URL", "PRICE_COMPARE_RAG_URL", 8033, "PRICE_COMPARE_URL", None),
    ("TESLA_SERVICE_URL", "RAG_TESLA_URL", "TESLA_RAG_URL", 8028, "TESLA_URL", None),
    ("MEDIA_SERVICE_URL", "RAG_MEDIA_URL", "MEDIA_RAG_URL", 8029, "MEDIA_URL", None),
    ("BRIGHTDATA_SERVICE_URL", "RAG_BRIGHTDATA_URL", "BRIGHTDATA_RAG_URL", 8040, "BRIGHTDATA_URL", None),
]
assert len(WIRING_TABLE) == 23

LEGACY_ROWS = [row for row in WIRING_TABLE if row[2] is not None]
assert len(LEGACY_ROWS) == 22

ALL_ENV_NAMES = set()
for _, canonical, legacy, *_ in WIRING_TABLE:
    ALL_ENV_NAMES.add(canonical)
    if legacy:
        ALL_ENV_NAMES.add(legacy)
ALL_ENV_NAMES.add("MODE_SERVICE_URL")


def _reload_all() -> None:
    importlib.reload(urls)
    importlib.reload(rag_tools)
    importlib.reload(constants)


@pytest.fixture(autouse=True)
def _clean_env_and_reload():
    """Clear every RAG URL env var before and after each test, reloading the
    three modules so tests never see leakage from each other and later test
    modules see default-env values once this file finishes."""
    saved = {name: os.environ.pop(name, None) for name in ALL_ENV_NAMES}
    _reload_all()
    yield
    for name in ALL_ENV_NAMES:
        os.environ.pop(name, None)
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value
    _reload_all()


def _getenv_literal_names(tree: ast.AST) -> list[str]:
    """Literal first-args of os.getenv/os.environ.get calls in an AST."""
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_os_getenv = (
            isinstance(func, ast.Attribute)
            and func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        )
        is_os_environ_get = (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
        )
        if not (is_os_getenv or is_os_environ_get):
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            names.append(arg0.value)
    return names


def _manifest_env_names() -> set[str]:
    names: set[str] = set()
    with open(MANIFEST_PATH) as f:
        docs = list(yaml.safe_load_all(f))

    def collect(o):
        if isinstance(o, dict):
            if isinstance(o.get("name"), str) and "value" in o:
                names.add(o["name"])
            for v in o.values():
                collect(v)
        elif isinstance(o, list):
            for v in o:
                collect(v)

    for doc in docs:
        collect(doc)
    return {n for n in names if CANONICAL_ONLY_RE.match(n)}


def test_manifest_env_names_equal_canonical_set():
    manifest_set = _manifest_env_names()
    tree = ast.parse(URLS_PY_PATH.read_text(), filename=str(URLS_PY_PATH))
    urls_py_canonical = {
        n for n in _getenv_literal_names(tree) if CANONICAL_ONLY_RE.match(n)
    }

    assert len(manifest_set) >= 23
    assert "RAG_SEATGEEK_URL" in manifest_set
    assert manifest_set == urls_py_canonical


@pytest.mark.parametrize(
    "row",
    WIRING_TABLE,
    ids=[row[0] for row in WIRING_TABLE],
)
def test_canonical_env_reaches_every_consumer(row):
    urls_attr, canonical_env, _legacy_env, _default_port, rag_tools_attr, constants_attr = row
    value = f"http://canon-{urls_attr.lower()}:1"
    os.environ[canonical_env] = value

    with structlog.testing.capture_logs() as captured:
        _reload_all()

    assert getattr(urls, urls_attr) == value
    if rag_tools_attr is not None:
        assert getattr(rag_tools, rag_tools_attr) == value
    if constants_attr is not None:
        assert getattr(constants, constants_attr) == value

    marker_events = [
        e for e in captured if e.get("event") in ("rag_url_legacy_env_name", "rag_url_env_conflict")
    ]
    assert marker_events == []


@pytest.mark.parametrize(
    "row",
    LEGACY_ROWS,
    ids=[row[0] for row in LEGACY_ROWS],
)
def test_legacy_env_name_still_honoured_with_warning(row):
    urls_attr, canonical_env, legacy_env, _default_port, rag_tools_attr, constants_attr = row
    value = f"http://legacy-{urls_attr.lower()}:1"
    os.environ[legacy_env] = value

    with structlog.testing.capture_logs() as captured:
        _reload_all()

    assert getattr(urls, urls_attr) == value
    if rag_tools_attr is not None:
        assert getattr(rag_tools, rag_tools_attr) == value
    if constants_attr is not None:
        assert getattr(constants, constants_attr) == value

    legacy_events = [e for e in captured if e.get("event") == "rag_url_legacy_env_name"]
    matching = [
        e for e in legacy_events
        if e.get("canonical") == canonical_env and e.get("legacy") == legacy_env
    ]
    assert len(matching) == 1
    assert matching[0].get("log_level") == "warning"


def test_canonical_wins_on_conflict():
    os.environ["RAG_EVENTS_URL"] = "http://a:1"
    os.environ["EVENTS_RAG_URL"] = "http://b:1"

    with structlog.testing.capture_logs() as captured:
        _reload_all()

    assert urls.EVENTS_SERVICE_URL == "http://a:1"
    assert rag_tools.EVENTS_URL == "http://a:1"
    assert constants.EVENTS_SERVICE_URL == "http://a:1"

    conflict_events = [e for e in captured if e.get("event") == "rag_url_env_conflict"]
    assert len(conflict_events) == 1
    assert conflict_events[0].get("log_level") == "warning"


def test_defaults_match_service_ports():
    _reload_all()

    for urls_attr, _canonical, _legacy, port, _rt, _c in WIRING_TABLE:
        expected = f"http://localhost:{port}"
        assert getattr(urls, urls_attr) == expected, urls_attr

    assert constants.MODE_SERVICE_URL == "http://localhost:8022"
    assert urls.MODE_SERVICE_URL == "http://localhost:8022"
    assert constants.MODE_SERVICE_URL == urls.MODE_SERVICE_URL


def test_no_rag_url_getenv_outside_urls_py():
    names_by_file: dict[str, list[str]] = {}
    for py_file in SRC_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue
        matches = [n for n in _getenv_literal_names(tree) if EITHER_SPELLING_RE.match(n)]
        if matches:
            names_by_file[str(py_file.relative_to(REPO_ROOT))] = matches

    assert set(names_by_file.keys()) == {"src/orchestrator/urls.py"}
    assert len(names_by_file["src/orchestrator/urls.py"]) >= 45


@pytest.mark.parametrize(
    "canonical_raw,legacy_raw,expect_default,expect_warning",
    [
        ("", "http://b:1/", False, "legacy"),
        ("   ", None, True, None),
        ("http://a:1", "", False, None),
        (None, "", True, None),
        ("", "", True, None),
        (" http://a:1/ ", None, False, None),
    ],
    ids=[
        "blank_canonical_legacy_set",
        "whitespace_canonical_neither_else",
        "canonical_set_blank_legacy",
        "blank_legacy_only",
        "both_blank",
        "trailing_slash_and_spaces_normalized",
    ],
)
def test_blank_env_values_treated_as_unset(canonical_raw, legacy_raw, expect_default, expect_warning):
    if canonical_raw is not None:
        os.environ["RAG_EVENTS_URL"] = canonical_raw
    if legacy_raw is not None:
        os.environ["EVENTS_RAG_URL"] = legacy_raw

    with structlog.testing.capture_logs() as captured:
        _reload_all()

    if expect_default:
        expected = "http://localhost:8014"
    elif expect_warning == "legacy":
        expected = "http://b:1"
    else:
        expected = "http://a:1"

    assert urls.EVENTS_SERVICE_URL == expected
    assert rag_tools.EVENTS_URL == expected
    assert constants.EVENTS_SERVICE_URL == expected

    legacy_events = [e for e in captured if e.get("event") == "rag_url_legacy_env_name"]
    conflict_events = [e for e in captured if e.get("event") == "rag_url_env_conflict"]
    if expect_warning == "legacy":
        assert len(legacy_events) == 1
        assert conflict_events == []
    else:
        assert legacy_events == []
        assert conflict_events == []


def test_blank_env_values_onecall_no_legacy():
    os.environ["RAG_ONECALL_URL"] = ""

    with structlog.testing.capture_logs() as captured:
        _reload_all()

    assert urls.ONECALL_SERVICE_URL == "http://localhost:8021"
    assert [e for e in captured if e.get("event") in ("rag_url_legacy_env_name", "rag_url_env_conflict")] == []
