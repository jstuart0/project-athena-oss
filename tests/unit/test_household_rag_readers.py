"""P3: the gateway and the four RAG services read household settings from admin.

Each RAG ``main.py`` is loaded under a private name, with no module stubs and no
real clock: reloads are driven by calling the poll step directly (or the poll
loop with an injected ``sleep``), and the feed and scraper calls are replaced by
counting fakes. The reader is a ``household.install_for_test`` seam, so nothing
here touches the network.

Covered: amtrak in every precedence state; a transit/community change reloading
once, an identical poll not reloading, a reset to env reloading the env config;
a reload that raises keeping the old data; a change during an in-flight load
committing only the newer config; configured -> [] emptying stops and events;
clean cancellation on shutdown; ``allow_private`` (422 at the model for admin,
kept for env); a hostname that resolves privately being blocked at fetch with no
``allowed_private_hosts``; a key in a feed URL never reaching /health or logs;
the gateway model chain and city keyword; the directions fallback.
"""
from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shared import household  # noqa: E402
from shared import household_settings as hs  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402
from shared.url_safety import SsrfBlockedError  # noqa: E402

SECRET_URL = "https://feeds.example.org/gtfs.zip?api_key=SECRET"

_ENV_NAMES = (
    "TRANSIT_REGION_NAME", "TRANSIT_GTFS_FEEDS", "TRANSIT_STATIC_SERVICES",
    "COMMUNITY_EVENTS_SOURCES", "DEFAULT_AMTRAK_STATION", "GATEWAY_FALLBACK_MODEL",
    "DEFAULT_CITY", "DEFAULT_STATE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def _load(unique_name: str, service_dir: str):
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


def transportation():
    return _load("_test_hh_transportation", "transportation")


def community():
    return _load("_test_hh_community", "community_events")


def amtrak():
    return _load("_test_hh_amtrak", "amtrak")


def directions():
    return _load("_test_hh_directions", "directions")


def feed(fid="a", url="https://example.org/a.zip", **extra):
    return {"id": fid, "name": f"Feed {fid}", "url": url, **extra}


def source(sid="s", url="https://example.org/events", **extra):
    return {"id": sid, "name": f"Source {sid}", "type": "link_scan", "url": url,
            "link_path": "/events/", **extra}


def admin_transit(*feeds, static=None):
    return {"transit": {"gtfs_feeds": list(feeds), **({"static_services": static} if static is not None else {})}}


def hs_transit(*feeds):
    """The resolved TransitSettings for admin feeds, without touching the reader."""
    household.install_for_test(sources=admin_transit(*feeds))
    return household.transit()


def keep_seam(monkeypatch):
    """Lifespans call configure(); keep the test reader instead of building a real one."""
    monkeypatch.setattr(household, "configure", lambda **kw: household.reader())


# ---------------------------------------------------------------------------
# Amtrak: every precedence state
# ---------------------------------------------------------------------------

def test_amtrak_admin_station_wins_over_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_AMTRAK_STATION", "was")
    _clear_cache_for_tests()
    module = amtrak()
    household.install_for_test(sources={"amtrak": {"default_station": "NYP"}})
    assert module._default_origin() == "NYP"
    assert household.amtrak_station().source == "admin"


def test_amtrak_null_admin_falls_to_env_uppercased(monkeypatch):
    monkeypatch.setenv("DEFAULT_AMTRAK_STATION", "was")
    _clear_cache_for_tests()
    module = amtrak()
    household.install_for_test(sources={"amtrak": {"default_station": None}})
    assert module._default_origin() == "WAS"
    assert household.amtrak_station().source == "env"


def test_amtrak_env_only_and_default_only():
    module = amtrak()
    household.install_for_test(sources={})
    assert module._default_origin() == ""
    assert household.amtrak_station().source == "default"


def test_amtrak_unset_names_the_admin_page_and_the_env_fallback(monkeypatch):
    module = amtrak()
    monkeypatch.setattr(module, "load_gtfs", lambda: None)
    household.install_for_test(sources={})
    client = TestClient(module.app)

    health = client.get("/health").json()
    assert health["default_origin"] is None
    assert "Directions & Local Area" in health["message"]
    assert "DEFAULT_AMTRAK_STATION" in health["message"]

    schedule = client.get("/amtrak/schedule", params={"destination": "NYP"})
    assert schedule.status_code == 400
    assert "Directions & Local Area" in schedule.json()["detail"]

    hint = client.get("/amtrak/query", params={"query": "train to new york"}).json()["hint"]
    assert "Directions & Local Area" in hint


def test_amtrak_reads_the_setting_per_request_with_no_restart(monkeypatch):
    module = amtrak()
    monkeypatch.setattr(module, "load_gtfs", lambda: None)
    client = TestClient(module.app)
    household.install_for_test(sources={})
    assert client.get("/health").json()["default_origin"] is None
    household.install_for_test(sources={"amtrak": {"default_station": "BAL"}})
    assert client.get("/health").json()["default_origin"] == "BAL"


def test_amtrak_lifespan_configures_its_section_and_closes_the_reader(monkeypatch):
    module = amtrak()
    seen = {}
    reader = household.install_for_test(sources={})
    monkeypatch.setattr(household, "configure", lambda **kw: seen.update(kw) or reader)
    monkeypatch.setattr(module, "startup_service", mock.AsyncMock())
    monkeypatch.setattr(module, "unregister_service", mock.AsyncMock())
    fake_cache = mock.MagicMock(connect=mock.AsyncMock(), disconnect=mock.AsyncMock())
    monkeypatch.setattr(module, "CacheClient", mock.MagicMock(return_value=fake_cache))
    monkeypatch.setattr(module, "load_gtfs", lambda: None)
    closed = []
    real_aclose = reader.aclose

    async def aclose():
        closed.append(1)
        await real_aclose()

    reader.aclose = aclose

    async def run():
        async with module.lifespan(module.app):
            pass

    asyncio.run(run())
    assert seen["sections"] == ("amtrak",)
    assert closed == [1]


# ---------------------------------------------------------------------------
# Transportation: reloads (D16)
# ---------------------------------------------------------------------------

def counting_loader(module, monkeypatch, *, gates=None, raises=None):
    """Replace load_gtfs_data. Data is one stop per feed id, so the committed
    config is visible in transit_data. ``gates`` maps a feed-id tuple to an
    Event the load waits on (never set = hangs until cancelled)."""
    calls = []

    async def load(cfg):
        key = tuple(cfg.feeds)
        calls.append(key)
        if raises is not None and raises():
            raise RuntimeError("boom")
        if gates and key in gates:
            await gates[key].wait()
        stops = {f"{fid}_1": {"stop_id": f"{fid}_1"} for fid in cfg.feeds}
        return ({"stops": stops, "routes": {}, "stop_times": {}, "agencies": {},
                 "last_updated": "now"}, {})

    monkeypatch.setattr(module, "load_gtfs_data", load)
    return calls


async def settle(module):
    task = module._reload_task
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def test_transit_change_reloads_once_identical_poll_does_not_and_reset_reloads_env(monkeypatch):
    module = transportation()
    calls = counting_loader(module, monkeypatch)
    monkeypatch.setenv("TRANSIT_GTFS_FEEDS", json.dumps({"env_feed": {"name": "Env", "url": "https://example.org/e.zip"}}))
    _clear_cache_for_tests()

    async def run():
        household.install_for_test(sources=admin_transit(feed("a")))
        await module.sync_household_config()
        await settle(module)
        assert calls == [("a",)]
        assert set(module.transit_data["stops"]) == {"a_1"}

        await module.sync_household_config()
        await settle(module)
        assert calls == [("a",)], "an identical poll must not reload"

        household.install_for_test(sources=admin_transit(feed("a"), feed("b")))
        await module.sync_household_config()
        await settle(module)
        assert calls == [("a",), ("a", "b")]
        assert set(module.transit_data["stops"]) == {"a_1", "b_1"}

        household.install_for_test(sources={})
        await module.sync_household_config()
        await settle(module)
        assert calls[-1] == ("env_feed",)
        assert set(module.transit_data["stops"]) == {"env_feed_1"}
        assert module.transit_config.feeds["env_feed"]["origin"] == "env"

    asyncio.run(run())


def test_transit_poll_loop_applies_a_change_between_ticks(monkeypatch):
    module = transportation()
    calls = counting_loader(module, monkeypatch)
    household.install_for_test(sources=admin_transit(feed("a")))
    ticks = []

    async def fake_sleep(seconds):
        assert seconds == module.HOUSEHOLD_POLL_SECONDS
        ticks.append(1)
        await settle(module)
        if len(ticks) == 1:
            household.install_for_test(sources=admin_transit(feed("b")))
        if len(ticks) == 2:
            raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await module.poll_household_config(sleep=fake_sleep)

    asyncio.run(run())
    assert calls == [("a",), ("b",)]


def test_a_transit_reload_that_raises_keeps_the_old_data_and_the_loop_survives(monkeypatch):
    module = transportation()
    fail = {"on": False}
    calls = counting_loader(module, monkeypatch, raises=lambda: fail["on"])

    async def run():
        household.install_for_test(sources=admin_transit(feed("a")))
        await module.sync_household_config()
        await settle(module)
        good_config = module.transit_config
        assert set(module.transit_data["stops"]) == {"a_1"}

        fail["on"] = True
        household.install_for_test(sources=admin_transit(feed("b")))
        await module.sync_household_config()
        await settle(module)
        assert module.transit_config is good_config
        assert set(module.transit_data["stops"]) == {"a_1"}

        fail["on"] = False
        await module.sync_household_config()
        await settle(module)
        assert set(module.transit_data["stops"]) == {"b_1"}, "the next poll tries again"

    asyncio.run(run())
    assert calls == [("a",), ("b",), ("b",)]


def test_transit_poll_step_survives_an_unexpected_error(monkeypatch):
    module = transportation()
    counting_loader(module, monkeypatch)
    household.install_for_test(sources=admin_transit(feed("a")))
    real = module.apply_transit_config
    state = {"n": 0}

    async def flaky(cfg, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("first poll fails")
        return await real(cfg, **kw)

    monkeypatch.setattr(module, "apply_transit_config", flaky)

    async def run():
        await module.sync_household_config()
        assert module.transit_data["last_updated"] is None
        await module.sync_household_config()
        await settle(module)
        assert set(module.transit_data["stops"]) == {"a_1"}

    asyncio.run(run())


def test_a_change_during_an_inflight_transit_load_commits_only_the_newer_config(monkeypatch):
    module = transportation()
    gates = {("a",): asyncio.Event()}  # never set: the first load hangs until cancelled
    calls = counting_loader(module, monkeypatch, gates=gates)

    async def run():
        household.install_for_test(sources=admin_transit(feed("a")))
        await module.sync_household_config()
        first = module._reload_task
        await asyncio.sleep(0)
        assert calls == [("a",)] and not first.done()

        household.install_for_test(sources=admin_transit(feed("b")))
        await module.sync_household_config()
        await settle(module)
        assert first.cancelled()
        assert set(module.transit_data["stops"]) == {"b_1"}
        assert tuple(module.transit_config.feeds) == ("b",)

    asyncio.run(run())


def test_a_transit_reload_older_than_the_current_generation_never_commits(monkeypatch):
    module = transportation()
    counting_loader(module, monkeypatch)
    household.install_for_test(sources=admin_transit(feed("a")))
    old_cfg = module.load_transit_config(household.transit())
    module._generation = 5

    asyncio.run(module._run_reload(old_cfg, 4))
    assert module.transit_data["stops"] == {} and module.transit_config.configured is False

    asyncio.run(module._run_reload(old_cfg, 5))
    assert set(module.transit_data["stops"]) == {"a_1"}


def test_concurrent_transit_applies_start_only_the_newest_reload(monkeypatch):
    module = transportation()
    calls = counting_loader(module, monkeypatch, gates={("a",): asyncio.Event()})

    async def run():
        await module.apply_transit_config(module.load_transit_config(hs_transit(feed("a"))))
        await asyncio.sleep(0)
        second = module.load_transit_config(hs_transit(feed("b")))
        third = module.load_transit_config(hs_transit(feed("c")))
        results = await asyncio.gather(module.apply_transit_config(second), module.apply_transit_config(third))
        await settle(module)
        return results

    results = asyncio.run(run())
    assert sorted(results) == [False, True]
    assert calls == [("a",), ("c",)]
    assert set(module.transit_data["stops"]) == {"c_1"}


def test_a_stale_transit_load_that_ignores_cancellation_still_does_not_commit(monkeypatch):
    module = transportation()
    calls = []

    async def load(cfg):
        key = tuple(cfg.feeds)
        calls.append(key)
        data = {"stops": {f"{fid}_1": {} for fid in cfg.feeds}, "routes": {}, "stop_times": {},
                "agencies": {}, "last_updated": "now"}
        if key == ("a",):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return data, {}  # swallows the cancellation and returns anyway
        return data, {}

    monkeypatch.setattr(module, "load_gtfs_data", load)

    async def run():
        household.install_for_test(sources=admin_transit(feed("a")))
        await module.sync_household_config()
        await asyncio.sleep(0)
        household.install_for_test(sources=admin_transit(feed("b")))
        await module.sync_household_config()
        await settle(module)

    asyncio.run(run())
    assert set(module.transit_data["stops"]) == {"b_1"}
    assert tuple(module.transit_config.feeds) == ("b",)


def test_transit_configured_then_empty_lists_clears_stops_and_errors(monkeypatch):
    module = transportation()
    counting_loader(module, monkeypatch)

    async def run():
        household.install_for_test(sources=admin_transit(feed("a")))
        await module.sync_household_config()
        await settle(module)
        module.fetch_errors["a"] = "HTTP 500 from example.org"
        assert module.transit_data["stops"]

        household.install_for_test(sources={"transit": {"gtfs_feeds": [], "static_services": []}})
        await module.sync_household_config()
        await settle(module)
        assert module.transit_data["stops"] == {}
        assert module.transit_data["last_updated"] is None
        assert module.fetch_errors == {}
        assert module.transit_config.configured is False

    asyncio.run(run())
    client = TestClient(module.app)
    assert client.get("/health").json()["configured"] is False
    assert client.get("/transit/free").status_code == 503


def test_transit_lifespan_shutdown_leaves_no_destroyed_task(monkeypatch, caplog):
    module = transportation()
    counting_loader(module, monkeypatch, gates={("a",): asyncio.Event()})
    keep_seam(monkeypatch)
    household.install_for_test(sources=admin_transit(feed("a")))
    fake_cache = mock.MagicMock(connect=mock.AsyncMock(), disconnect=mock.AsyncMock())
    monkeypatch.setattr(module, "CacheClient", mock.MagicMock(return_value=fake_cache))

    async def run():
        async with module.lifespan(module.app):
            await asyncio.sleep(0)
            assert module._reload_task is not None and not module._reload_task.done()
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert leftover == []

    with caplog.at_level(logging.DEBUG, logger="asyncio"):
        asyncio.run(run())
        gc.collect()
    assert "Task was destroyed" not in caplog.text
    assert module._poll_task is None


def test_transit_refresh_forces_a_reload_of_the_current_config(monkeypatch):
    module = transportation()
    calls = counting_loader(module, monkeypatch)

    async def run():
        household.install_for_test(sources=admin_transit(feed("a")))
        await module.sync_household_config()
        await settle(module)
        await module.apply_transit_config(module.transit_config, force=True)
        await settle(module)

    asyncio.run(run())
    assert calls == [("a",), ("a",)]


# ---------------------------------------------------------------------------
# Community events
# ---------------------------------------------------------------------------

def fake_community(module, monkeypatch, *, raises=None, gates=None):
    scraped, cached, cleared = [], [], []

    async def scrape(cfg, errors):
        key = tuple(s.name for s in cfg.sources)
        scraped.append(key)
        if raises is not None and raises():
            raise RuntimeError("boom")
        if gates and key in gates:
            await gates[key].wait()
        return [{"id": name, "title": name} for name in key]

    async def cache_events(events):
        cached.append([e["id"] for e in events])
        module.in_memory_events = list(events)
        return True

    async def clear():
        cleared.append(1)
        module.in_memory_events = []

    monkeypatch.setattr(module, "scrape_sources", scrape)
    monkeypatch.setattr(module, "cache_events", cache_events)
    monkeypatch.setattr(module, "clear_events_cache", clear)
    return scraped, cached, cleared


def admin_events(*sources):
    return {"community_events": {"sources": list(sources)}}


def test_community_change_reloads_once_identical_poll_does_not_and_reset_reloads_env(monkeypatch):
    module = community()
    scraped, cached, _ = fake_community(module, monkeypatch)
    monkeypatch.setenv("COMMUNITY_EVENTS_SOURCES", json.dumps([
        {"name": "Env Source", "type": "link_scan", "url": "https://example.org/e", "link_path": "/e/"}]))
    _clear_cache_for_tests()

    async def run():
        household.install_for_test(sources=admin_events(source("s")))
        await module.sync_household_config()
        await settle(module)
        assert scraped == [("Source s",)] and cached == [["Source s"]]

        await module.sync_household_config()
        await settle(module)
        assert len(scraped) == 1

        household.install_for_test(sources=admin_events(source("s"), source("t")))
        await module.sync_household_config()
        await settle(module)
        assert scraped[-1] == ("Source s", "Source t")

        household.install_for_test(sources={})
        await module.sync_household_config()
        await settle(module)
        assert scraped[-1] == ("Env Source",)
        assert module.event_sources_config.sources[0].origin == "env"

    asyncio.run(run())


def test_a_community_reload_that_raises_keeps_the_old_config_and_events(monkeypatch):
    module = community()
    fail = {"on": False}
    fake_community(module, monkeypatch, raises=lambda: fail["on"])

    async def run():
        household.install_for_test(sources=admin_events(source("s")))
        await module.sync_household_config()
        await settle(module)
        good = module.event_sources_config

        fail["on"] = True
        household.install_for_test(sources=admin_events(source("t")))
        await module.sync_household_config()
        await settle(module)
        assert module.event_sources_config is good
        assert [e["id"] for e in module.in_memory_events] == ["Source s"]

        fail["on"] = False
        await module.sync_household_config()
        await settle(module)
        assert [e["id"] for e in module.in_memory_events] == ["Source t"]

    asyncio.run(run())


def test_a_change_during_an_inflight_community_scrape_commits_only_the_newer_config(monkeypatch):
    module = community()
    scraped, cached, _ = fake_community(module, monkeypatch, gates={("Source s",): asyncio.Event()})

    async def run():
        household.install_for_test(sources=admin_events(source("s")))
        await module.sync_household_config()
        first = module._reload_task
        await asyncio.sleep(0)
        household.install_for_test(sources=admin_events(source("t")))
        await module.sync_household_config()
        await settle(module)
        assert first.cancelled()

    asyncio.run(run())
    assert cached == [["Source t"]]
    assert [s.name for s in module.event_sources_config.sources] == ["Source t"]


def test_community_configured_then_empty_list_clears_events_and_redis(monkeypatch):
    module = community()
    redis = mock.MagicMock(delete=mock.AsyncMock())
    monkeypatch.setattr(module, "get_redis_client", mock.AsyncMock(return_value=redis))
    monkeypatch.setattr(module, "scrape_sources", mock.AsyncMock(return_value=[{"id": "e1"}]))

    async def cache_events(events):
        module.in_memory_events = list(events)
        return True

    monkeypatch.setattr(module, "cache_events", cache_events)

    async def run():
        household.install_for_test(sources=admin_events(source("s")))
        await module.sync_household_config()
        await settle(module)
        assert module.in_memory_events == [{"id": "e1"}]
        module.fetch_errors["Source s"] = "HTTP 500 from example.org"

        household.install_for_test(sources={"community_events": {"sources": []}})
        await module.sync_household_config()
        await settle(module)

    asyncio.run(run())
    assert module.in_memory_events == []
    assert module.fetch_errors == {}
    assert module.event_sources_config.configured is False
    redis.delete.assert_awaited_with(module.REDIS_KEY_BY_DATE, module.REDIS_KEY_NO_DATE, module.REDIS_KEY_METADATA)
    client = TestClient(module.app)
    assert client.get("/events/search").status_code == 503


def test_a_community_refresh_started_under_an_old_config_writes_nothing_after_a_change(monkeypatch):
    module = community()
    scraped, cached, _ = fake_community(module, monkeypatch)

    async def run():
        household.install_for_test(sources=admin_events(source("s")))
        await module.sync_household_config()
        await settle(module)
        old = module.event_sources_config
        household.install_for_test(sources=admin_events(source("t")))
        await module.sync_household_config()
        await settle(module)
        cached.clear()
        assert await module.refresh_events_cache(old) == []

    asyncio.run(run())
    assert cached == []


def test_a_community_reload_older_than_the_current_generation_never_commits(monkeypatch):
    module = community()
    scraped, cached, _ = fake_community(module, monkeypatch)
    household.install_for_test(sources=admin_events(source("s")))
    old_cfg = module.load_event_sources(household.community_sources())
    module._generation = 5

    asyncio.run(module._run_reload(old_cfg, 4))
    assert cached == [] and module.event_sources_config.configured is False

    asyncio.run(module._run_reload(old_cfg, 5))
    assert cached == [["Source s"]]


def test_a_community_source_marked_allow_private_but_from_admin_gets_no_exemption(monkeypatch):
    import dataclasses

    module = community()
    household.install_for_test(sources=admin_events(source("s", "https://internal.example.org/e")))
    src = dataclasses.replace(module.load_event_sources(household.community_sources()).sources[0],
                              allow_private=True)
    captured = capture_safe_get(module, monkeypatch)
    asyncio.run(module.scrape_link_scan_source(src, {}))
    assert "allowed_private_hosts" not in captured


def test_community_lifespan_starts_unconfigured_and_shuts_down_clean(monkeypatch, caplog):
    module = community()
    fake_community(module, monkeypatch)
    keep_seam(monkeypatch)
    household.install_for_test(sources={})
    monkeypatch.setattr(module, "get_redis_client", mock.AsyncMock(return_value=None))
    started = {}

    async def run():
        async with module.lifespan(module.app):
            started["refresh"] = module._refresh_task is not None and not module._refresh_task.done()
            started["poll"] = module._poll_task is not None and not module._poll_task.done()
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert leftover == []

    with caplog.at_level(logging.DEBUG, logger="asyncio"):
        asyncio.run(run())
        gc.collect()
    assert started == {"refresh": True, "poll": True}, "the periodic refresh runs even when unconfigured"
    assert "Task was destroyed" not in caplog.text


# ---------------------------------------------------------------------------
# allow_private and the private-address block
# ---------------------------------------------------------------------------

def test_admin_feed_with_allow_private_is_a_422_at_the_model():
    with pytest.raises(hs.SettingsValidationError) as caught:
        hs.validate_update("transit", {"gtfs_feeds": [feed("a", allow_private=True)]})
    assert {e["type"] for e in caught.value.errors} == {"extra_forbidden"}
    with pytest.raises(hs.SettingsValidationError):
        hs.validate_update("community_events", {"sources": [source("s", allow_private=True)]})


def capture_safe_get(module, monkeypatch):
    captured = {}

    async def fake(url, **kwargs):
        captured.update(kwargs, url=url)
        raise SsrfBlockedError("blocked for test capture")

    monkeypatch.setattr(module, "safe_get", fake)
    return captured


def test_env_transit_feed_keeps_allow_private_and_an_admin_feed_never_gets_it(monkeypatch):
    module = transportation()
    env_feed = {"name": "E", "url": "https://internal.example.org/e.zip", "allow_private": True}
    monkeypatch.setenv("TRANSIT_GTFS_FEEDS", json.dumps({"envf": env_feed}))
    _clear_cache_for_tests()
    household.install_for_test(sources={})
    cfg = module.load_transit_config(household.transit())
    captured = capture_safe_get(module, monkeypatch)
    asyncio.run(module.download_and_parse_gtfs("envf", cfg.feeds["envf"], {}))
    assert captured["allowed_private_hosts"] == ["internal.example.org"]

    household.install_for_test(sources=admin_transit(feed("adm", "https://internal.example.org/e.zip")))
    cfg = module.load_transit_config(household.transit())
    assert cfg.feeds["adm"]["origin"] == "admin"
    captured.clear()
    asyncio.run(module.download_and_parse_gtfs("adm", cfg.feeds["adm"], {}))
    assert "allowed_private_hosts" not in captured

    forced = {**cfg.feeds["adm"], "allow_private": True}
    captured.clear()
    asyncio.run(module.download_and_parse_gtfs("adm", forced, {}))
    assert "allowed_private_hosts" not in captured, "provenance, not a payload key, decides"


def test_env_community_source_keeps_allow_private_and_an_admin_source_never_gets_it(monkeypatch):
    module = community()
    monkeypatch.setenv("COMMUNITY_EVENTS_SOURCES", json.dumps([
        {"name": "E", "type": "link_scan", "url": "https://internal.example.org/e", "link_path": "/e/",
         "allow_private": True}]))
    _clear_cache_for_tests()
    household.install_for_test(sources={})
    env_src = module.load_event_sources(household.community_sources()).sources[0]
    captured = capture_safe_get(module, monkeypatch)
    asyncio.run(module.scrape_link_scan_source(env_src, {}))
    assert captured["allowed_private_hosts"] == ["internal.example.org"]

    household.install_for_test(sources=admin_events(source("s", "https://internal.example.org/e")))
    adm_src = module.load_event_sources(household.community_sources()).sources[0]
    assert adm_src.origin == "admin"
    captured.clear()
    asyncio.run(module.scrape_link_scan_source(adm_src, {}))
    assert "allowed_private_hosts" not in captured


@pytest.fixture
def private_dns(monkeypatch):
    import socket as socket_module

    def fake_getaddrinfo(host, *a, **k):
        return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr("shared.url_safety.socket.getaddrinfo", fake_getaddrinfo)


def test_admin_transit_feed_resolving_to_a_private_address_is_blocked_at_fetch(monkeypatch, private_dns):
    module = transportation()
    household.install_for_test(sources=admin_transit(feed("adm", SECRET_URL)))
    cfg = module.load_transit_config(household.transit())
    errors: dict = {}
    result = asyncio.run(module.download_and_parse_gtfs("adm", cfg.feeds["adm"], errors))
    assert result["stops"] == []
    assert "private" in errors["adm"].lower()
    assert "SECRET" not in errors["adm"]


def test_admin_community_source_resolving_to_a_private_address_is_blocked_at_fetch(monkeypatch, private_dns):
    module = community()
    household.install_for_test(sources=admin_events(source("s", SECRET_URL)))
    src = module.load_event_sources(household.community_sources()).sources[0]
    errors: dict = {}
    assert asyncio.run(module.scrape_link_scan_source(src, errors)) == []
    assert "private" in errors[src.name].lower()
    assert "SECRET" not in errors[src.name]


# ---------------------------------------------------------------------------
# A key in a feed URL never reaches /health, an API response or a log
# ---------------------------------------------------------------------------

def test_a_feed_key_never_reaches_health_responses_or_logs(monkeypatch, captured_logs, caplog):
    module = transportation()
    household.install_for_test(sources=admin_transit(feed("adm", SECRET_URL)))

    async def leaky(url, **kwargs):
        raise SsrfBlockedError(f"blocked {url}")

    monkeypatch.setattr(module, "safe_get", leaky)
    cfg = module.load_transit_config(household.transit())

    async def run():
        module.transit_config = cfg
        data, errors = await module.load_gtfs_data(cfg)
        module.fetch_errors.update(errors)
        module.transit_data = data

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    client = TestClient(module.app)
    health = client.get("/health")
    assert "feeds.example.org" in health.json()["message"]
    assert "SECRET" not in health.text and "api_key" not in health.text

    route = {"route_id": "adm_1", "route_short_name": "1", "route_long_name": "L", "route_type": 3,
             "feed_id": "adm", "agency_id": "", "route_color": "", "route_text_color": ""}
    module.transit_data["routes"]["adm_1"] = route
    detail = client.get("/transit/route/adm_1")
    assert detail.status_code == 200
    assert "SECRET" not in detail.text and detail.json()["feed_config"]["host"] == "feeds.example.org"

    assert "SECRET" not in json.dumps(captured_logs, default=str)
    assert "SECRET" not in caplog.text


def test_a_source_key_never_reaches_health_sources_or_logs(monkeypatch, captured_logs):
    module = community()
    household.install_for_test(sources=admin_events(source("s", SECRET_URL, base_url="https://feeds.example.org")))
    cfg = module.load_event_sources(household.community_sources())

    async def leaky(url, **kwargs):
        raise SsrfBlockedError(f"blocked {url}")

    monkeypatch.setattr(module, "safe_get", leaky)
    monkeypatch.setattr(module, "get_redis_client", mock.AsyncMock(return_value=None))
    module.event_sources_config = cfg
    errors: dict = {}
    asyncio.run(module.scrape_sources(cfg, errors))
    module.fetch_errors.update(errors)

    client = TestClient(module.app)
    health = client.get("/health")
    sources = client.get("/events/sources")
    assert "feeds.example.org" in health.json()["message"]
    for response in (health, sources):
        assert "SECRET" not in response.text and "api_key" not in response.text
    assert sources.json()["sources"][0]["host"] == "feeds.example.org"
    assert "SECRET" not in json.dumps(captured_logs, default=str)


def test_unconfigured_messages_name_the_admin_page_and_keep_the_env_names(monkeypatch):
    t, c = transportation(), community()
    household.install_for_test(sources={})
    tc, cc = TestClient(t.app), TestClient(c.app)
    monkeypatch.setattr(c, "get_redis_client", mock.AsyncMock(return_value=None))
    assert "Directions & Local Area" in tc.get("/health").json()["message"]
    assert "TRANSIT_GTFS_FEEDS" in tc.get("/health").json()["message"]
    assert "Directions & Local Area" in tc.get("/transit/free").json()["detail"]
    assert "Directions & Local Area" in cc.get("/health").json()["message"]
    assert "COMMUNITY_EVENTS_SOURCES" in cc.get("/events/search").json()["detail"]


# ---------------------------------------------------------------------------
# Directions
# ---------------------------------------------------------------------------

def test_directions_falls_back_to_the_household_location_text_then_env(monkeypatch):
    module = directions()
    module.BASE_KNOWLEDGE.clear()
    household.install_for_test(sources={"location": {"text": "123 Main St, Springfield, IL"}})
    assert module.get_default_origin() == "123 Main St, Springfield, IL"

    monkeypatch.setenv("DEFAULT_CITY", "Boulder")
    monkeypatch.setenv("DEFAULT_STATE", "CO")
    _clear_cache_for_tests()
    household.install_for_test(sources={})
    assert module.get_default_origin() == "Boulder, CO"

    module.BASE_KNOWLEDGE["default_location"] = "Denver, CO"
    assert module.get_default_origin() == "Denver, CO", "a Base Knowledge row still comes first"


def test_directions_lifespan_configures_the_location_section(monkeypatch):
    module = directions()
    seen = {}
    reader = household.install_for_test(sources={})
    monkeypatch.setattr(household, "configure", lambda **kw: seen.update(kw) or reader)
    monkeypatch.setattr(module, "startup_service", mock.AsyncMock())
    monkeypatch.setattr(module, "unregister_service", mock.AsyncMock())
    fake_cache = mock.MagicMock(connect=mock.AsyncMock(), disconnect=mock.AsyncMock())
    monkeypatch.setattr(module, "CacheClient", mock.MagicMock(return_value=fake_cache))
    fake_admin = mock.MagicMock(get_external_api_key=mock.AsyncMock(return_value=None), close=mock.AsyncMock())
    monkeypatch.setattr(module, "get_admin_client", lambda: fake_admin)
    closed = []
    real = reader.aclose

    async def aclose():
        closed.append(1)
        await real()

    reader.aclose = aclose

    class _NoNetwork:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("no network")
        async def aclose(self): ...

    monkeypatch.setattr(module.httpx, "AsyncClient", _NoNetwork)

    async def run():
        async with module.lifespan(module.app):
            pass

    asyncio.run(run())
    assert seen["sections"] == ("location",)
    assert closed == [1]


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

@pytest.fixture
def gw():
    import gateway.main as gateway_main

    return gateway_main


def test_gateway_section_tuple_is_one_named_constant(gw):
    assert gw.HOUSEHOLD_SECTIONS == ("gateway", "location", "speaker_rooms")


def test_gateway_model_chain_admin_then_env_then_default(gw, monkeypatch):
    default = _default_model()
    household.install_for_test(sources={})
    assert gw._fallback_model() == default and household.gateway_fallback_model().source == "default"

    monkeypatch.setenv("GATEWAY_FALLBACK_MODEL", "env:2b")
    _clear_cache_for_tests()
    assert gw._fallback_model() == "env:2b" and household.gateway_fallback_model().source == "env"

    household.install_for_test(sources={"gateway": {"fallback_model": "admin:1b"}})
    assert gw._fallback_model() == "admin:1b" and household.gateway_fallback_model().source == "admin"
    assert gw._resolve_ollama_model("gpt-4") == "admin:1b"
    assert gw._resolve_ollama_model("llama3.2:3b") == "llama3.2:3b"

    household.install_for_test(sources={"gateway": {"fallback_model": None}})
    assert gw._fallback_model() == "env:2b"


def _default_model() -> str:
    from shared.config import get_config

    return get_config().athena_default_model


def _says(gw, text):
    return gw.is_athena_query_keywords([gw.ChatMessage(role="user", content=text)])


def test_gateway_city_keyword_follows_a_matching_admin_location_only(gw, monkeypatch):
    monkeypatch.setenv("DEFAULT_CITY", "Denver")
    _clear_cache_for_tests()
    household.install_for_test(sources={})
    assert _says(gw, "denver please") and not _says(gw, "zzyzx please")

    household.install_for_test(sources={"location": {"city": "Zzyzx", "state": "CA", "text": "Zzyzx, CA"}})
    assert _says(gw, "zzyzx please") and not _says(gw, "denver please")

    household.install_for_test(sources={"location": {"text": "123 Main St, Zzyzx, CA"}})
    assert _says(gw, "denver please"), "text-only keeps the env city"
    assert not _says(gw, "zzyzx please")


def test_gateway_lifespan_source_configures_the_named_constant_and_closes_before_admin():
    source_text = (_SRC / "gateway" / "main.py").read_text()
    assert "household.configure(sections=HOUSEHOLD_SECTIONS, spawn=_spawn_household_refresh)" in source_text
    assert source_text.index("household_reader.aclose()") < source_text.index("await admin_client.close()")
