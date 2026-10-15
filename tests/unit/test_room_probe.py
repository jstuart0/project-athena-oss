"""The roll probe: it reads, summarises and prints nothing identifying."""
from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from pathlib import Path

import pytest

from .test_room_light_area_tier import _isolated, _reg, _state  # noqa: F401
from . import _room_chat_harness as rch

from orchestrator import room_probe  # noqa: E402
from shared import household  # noqa: E402


def run_probe(**kw):
    raw, states = rch.house_raw_and_states()
    states["light.study_lamp_0"]["state"] = "unavailable"
    raw["devices"] = [{"id": "dev_sat", "area_id": "area_study", "name": "Secret Satellite", "parent_device_id": None}]
    raw["entities"].append(_reg("assist_satellite.study_voice", area="area_study", device="dev_sat"))
    fetches = []

    async def fetch():
        fetches.append(1)
        return raw

    async def read_states():
        return states

    out = io.StringIO()
    code = asyncio.run(room_probe.run(
        fetch, read_states, runs=kw.pop("runs", 3), shape=kw.pop("shape", False),
        show_names=kw.pop("show_names", False), utterances=kw.pop("utterances", []), out=out, **kw))
    return code, out.getvalue(), fetches


def test_it_fetches_the_requested_number_of_times_and_summarises_the_registry():
    code, text, fetches = run_probe(runs=4)
    assert code == 0 and len(fetches) == 4
    assert re.search(r"fetch: runs 4, p50 \d+ ms, p95 \d+ ms, payload \d+ bytes", text)
    assert re.search(r"registry: areas 5, devices 1, entities \d+, collisions 0, unmatchable \d+", text)


def test_per_area_counts_lamp_targets_and_unavailable_lights():
    _, text, _ = run_probe()
    lines = [l for l in text.splitlines() if l.startswith("  area ")]
    assert len(lines) == 5
    assert any("lights 3, unavailable 1" in l for l in lines), lines


def test_area_names_are_printed_only_with_the_flag():
    _, plain, _ = run_probe()
    _, named, _ = run_probe(show_names=True)
    assert "study" not in plain.lower() and "galley" not in plain.lower()
    assert "study:" in named


def test_nothing_identifying_is_printed():
    _, text, _ = run_probe(shape=True, utterances=["turn on the lights"], show_names=False)
    assert "light." not in text and "Secret" not in text and "http" not in text.lower() and "token" not in text.lower()


def test_shape_lists_key_names_only():
    _, text, _ = run_probe(shape=True)
    assert "shape:" in text and "areas: " in text and "entities: " in text
    assert "area_study" not in text and "Secret" not in text


def test_utterances_report_decisions_from_each_satellite_area_without_writing():
    _, text, _ = run_probe(utterances=["turn on the lights", "turn on all the lights", "turn on the bathroom lights"])
    assert "from 1 satellite areas (no writes)" in text
    assert "utterance 1: room x1" in text
    assert "utterance 2: whole_house(excluding 0) x1" in text
    assert "utterance 3: ask_which(3) x1" in text


def test_shape_of_ignores_values():
    assert room_probe.shape_of({"areas": [{"id": "a", "name": "N"}, {"id": "b", "extra": 1}], "x": 3}) == {
        "areas": ["extra", "id", "name"]}


def test_main_needs_the_ha_environment(monkeypatch, capsys):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert room_probe.main(["--runs", "1"]) == 2
    assert "HA_URL and HA_TOKEN" in capsys.readouterr().err


def test_the_probe_has_no_write_path():
    source = Path(room_probe.__file__).read_text()
    for needle in ("call_service", ".post(", ".put(", ".delete(", "/api/services"):
        assert needle not in source, needle


def test_main_failure_names_the_class_only(monkeypatch, capsys):
    monkeypatch.setenv("HA_URL", "http://ha.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "super-secret-token")

    def boom(*a, **k):
        raise ConnectionError("http://ha.invalid:8123 refused super-secret-token")

    monkeypatch.setattr(room_probe, "snapshot_fetcher", lambda resolver, latch, caller: boom)
    assert room_probe.main(["--runs", "1"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "probe failed: ConnectionError"


def test_main_refuses_zero_runs(monkeypatch, capsys):
    monkeypatch.setenv("HA_URL", "http://ha.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "t")
    assert room_probe.main(["--runs", "0"]) == 2
    assert "at least 1" in capsys.readouterr().err


def test_snapshot_serves_the_same_snapshot_through_read_and_current():
    snapshot = object()
    assert room_probe._Snapshot(snapshot).read() is snapshot and room_probe._Snapshot(snapshot).current() is snapshot


def test_main_reads_its_credentials_through_the_resolver_and_no_other_way(monkeypatch, capsys):
    """The environment is read inside shared.ha_credentials.resolve, never by the
    probe: with os.getenv failing, a configured environment still reaches the fetcher."""
    import os

    seen = {}

    def fetcher(resolver, latch, caller):
        seen["url"] = resolver.current().url
        seen["configured"] = resolver.current().configured
        raise ConnectionError("stop here")

    monkeypatch.setenv("HA_URL", "http://ha.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "super-secret-token")
    monkeypatch.setattr(os, "getenv", lambda *a, **k: pytest.fail("room_probe must not read the environment itself"))
    monkeypatch.setattr(room_probe, "snapshot_fetcher", fetcher)
    assert room_probe.main(["--runs", "1"]) == 1
    assert seen == {"url": "http://ha.invalid:8123", "configured": True}
    assert "super-secret-token" not in capsys.readouterr().err


def test_main_refuses_an_address_the_policy_rejects(monkeypatch, capsys):
    monkeypatch.setenv("HA_URL", "http://169.254.169.254/")
    monkeypatch.setenv("HA_TOKEN", "t")
    assert room_probe.main(["--runs", "1"]) == 2
    assert "HA_URL and HA_TOKEN" in capsys.readouterr().err


# --- --sources ----------------------------------------------------------------

class _EnvConfig:
    def __init__(self, groups="", satellites=""):
        self.ha_light_groups = groups
        self.ha_satellite_room_map = satellites


def test_sources_prints_counts_and_words_for_both_settings(monkeypatch):
    monkeypatch.setattr(household, "_config", lambda: _EnvConfig(
        groups=json.dumps({"Study": "light.study_lamp_0", "Nowhere Room": "light.not_in_home_assistant"}),
        satellites=json.dumps({"assist_satellite.study_voice": "study", "b" * 32: "Phantom Den"}),
    ))
    household.reset_room_settings_for_test()
    code, text, _ = run_probe(sources=True)
    assert code == 0
    assert ("room lights: source env, rooms 2, lights 2, not reported by Home Assistant 1, "
            "rooms that are no area 1") in text
    assert "speaker rooms: source env, device keys 1, entity keys 1, rooms that are no area 1" in text


def test_sources_with_nothing_configured_reports_default(monkeypatch):
    monkeypatch.setattr(household, "_config", lambda: _EnvConfig())
    household.reset_room_settings_for_test()
    _, text, _ = run_probe(sources=True)
    assert "room lights: source default, rooms 0, lights 0, not reported by Home Assistant 0, rooms that are no area 0" in text
    assert "speaker rooms: source default, device keys 0, entity keys 0, rooms that are no area 0" in text


def test_sources_prints_no_names_ids_or_urls(monkeypatch):
    monkeypatch.setattr(household, "_config", lambda: _EnvConfig(
        groups=json.dumps({"Nowhere Room": "light.not_in_home_assistant"}),
        satellites=json.dumps({"assist_satellite.secret_voice": "Phantom Den"}),
    ))
    household.reset_room_settings_for_test()
    _, text, _ = run_probe(sources=True)
    lowered = text.lower()
    for needle in ("nowhere", "phantom", "secret_voice", "not_in_home_assistant", "http", "light."):
        assert needle not in lowered, needle


def test_without_the_flag_no_sources_lines_are_printed():
    _, text, _ = run_probe()
    assert "room lights:" not in text and "speaker rooms:" not in text


class _FakeReader:
    """Counts the probe's calls; serves the inert test reader's snapshot."""

    def __init__(self, hang=False):
        self.hang = hang
        self.loaded = 0
        self.closed = 0
        self._inert = household.install_for_test()

    failing_since = None

    def peek(self):
        return self._inert.peek()

    async def current(self):
        self.loaded += 1
        if self.hang:
            await asyncio.sleep(60)

    async def aclose(self):
        self.closed += 1


def _drive_household(monkeypatch, reader, **kw):
    monkeypatch.setattr(household, "reader", lambda: reader)
    raw, states = rch.house_raw_and_states()

    async def fetch():
        return raw

    async def read_states():
        return states

    out = io.StringIO()
    code = asyncio.run(room_probe._run_with_household(
        fetch, read_states, runs=1, shape=False, show_names=False, utterances=[], sources=True, out=out, **kw))
    return code, out.getvalue()


def test_the_probe_loads_the_household_reader_once_and_closes_it(monkeypatch):
    reader = _FakeReader()
    code, text = _drive_household(monkeypatch, reader)
    assert code == 0 and "room lights:" in text
    assert (reader.loaded, reader.closed) == (1, 1)


def test_a_reader_that_never_answers_is_abandoned_after_the_bound_and_still_closed(monkeypatch):
    monkeypatch.setattr(room_probe, "HOUSEHOLD_LOAD_TIMEOUT_S", 0.01)
    reader = _FakeReader(hang=True)
    code, text = _drive_household(monkeypatch, reader)
    assert code == 0 and "room lights:" in text and reader.closed == 1


def test_the_reader_is_closed_when_the_probe_fails(monkeypatch):
    reader = _FakeReader()

    async def fetch():
        raise ConnectionError("down")

    monkeypatch.setattr(household, "reader", lambda: reader)

    async def read_states():
        return {}

    with pytest.raises(ConnectionError):
        asyncio.run(room_probe._run_with_household(
            fetch, read_states, runs=1, shape=False, show_names=False, utterances=[], sources=True))
    assert reader.closed == 1
