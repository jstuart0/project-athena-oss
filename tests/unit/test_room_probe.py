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

    monkeypatch.setattr(room_probe, "websocket_fetcher", lambda url, token: boom)
    assert room_probe.main(["--runs", "1"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "probe failed: ConnectionError"
