"""A sequence step's ``parameters`` and the colour it carries.

A model writes ``"parameters": null``, a list or a string; the step must
still run (it used to raise inside the step loop and be skipped without a
word). The sequence prompt asks for a single ``hs_color`` while the room
light writer reads ``hs_colors``; the executor turns the first into the
second, and an explicit ``hs_colors`` wins. Writes are read off the raw
client, below the permission guard.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import sequence_executor as se  # noqa: E402
from orchestrator.sequence_executor import SequenceExecutor  # noqa: E402

from .test_room_light_area_tier import Rig, _isolated  # noqa: E402,F401


def _step(action, parameters, room="hall", **extra):
    return {"action": action, "target": {"device_type": "light", "room": room}, "parameters": parameters,
            "delay_after": 0, "at_time": None, **extra}


def _run(rig, steps, query="make the hall lights red"):
    executor = SequenceExecutor(rig.controller, rig.raw)

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            text = await rig.controller.decide_sequence_rooms(steps, query, None)
            assert text is None, text
            return await executor.execute_sequence(steps, session_id=None, background=False)

    return asyncio.run(go())


def _light_calls(rig):
    return [(c.args[0], c.args[1], c.args[2]) for c in rig.raw.call_service.await_args_list]


class TestParametersThatAreNotAnObject:
    @pytest.mark.parametrize("parameters", [None, [], ["a"], "bright", 7, True])
    def test_the_step_still_runs(self, monkeypatch, parameters):
        rig = Rig(monkeypatch)
        _run(rig, [_step("turn_on", parameters)], "turn on the hall lights")
        assert _light_calls(rig), "the step was skipped"
        assert all(c[0] == "light" and c[1] == "turn_on" for c in _light_calls(rig))

    def test_a_missing_key_runs_too(self, monkeypatch):
        rig = Rig(monkeypatch)
        step = _step("turn_off", None)
        del step["parameters"]
        _run(rig, [step], "turn off the hall lights")
        assert _light_calls(rig)

    def test_a_direct_entity_step_with_null_parameters_runs(self, monkeypatch):
        rig = Rig(monkeypatch)
        step = {"action": "turn_on", "target": {"device_type": "light", "entity_id": "light.hall_lamp"},
                "parameters": None, "delay_after": 0, "at_time": None}
        _run(rig, [step], "turn on the hall lamp")
        assert ("light", "turn_on", {"entity_id": "light.hall_lamp"}) in _light_calls(rig)

    def test_the_planned_step_is_not_rewritten(self):
        step = {"parameters": None}
        assert se._step_parameters(step) == {}
        assert step == {"parameters": None}


class TestHsColor:
    def _colour_calls(self, rig):
        return [c[2] for c in _light_calls(rig) if "hs_color" in (c[2] or {})]

    def test_a_lone_hs_color_reaches_the_light_writer(self, monkeypatch):
        rig = Rig(monkeypatch)
        _run(rig, [_step("set_color", {"hs_color": [240, 100]})], "make the hall lights blue")
        got = self._colour_calls(rig)
        assert got and all(c["hs_color"] == [240, 100] for c in got), _light_calls(rig)

    def test_hs_colors_wins_when_both_are_present(self, monkeypatch):
        rig = Rig(monkeypatch)
        _run(rig, [_step("set_color", {"hs_color": [240, 100], "hs_colors": [[0, 100]]})], "make the hall lights red")
        got = self._colour_calls(rig)
        assert got and all(c["hs_color"] == [0, 100] for c in got), _light_calls(rig)

    @pytest.mark.parametrize("junk", [None, "blue", [1], [1, 2, 3], ["a", "b"], [True, False], {"h": 1}])
    def test_a_malformed_hs_color_is_not_turned_into_a_list(self, junk):
        got = se._step_parameters({"parameters": {"hs_color": junk}})
        assert "hs_colors" not in got

    def test_the_conversion_is_in_the_parameters_not_the_step(self):
        step = {"parameters": {"hs_color": [10, 20]}}
        assert se._step_parameters(step)["hs_colors"] == [[10, 20]]
        assert step == {"parameters": {"hs_color": [10, 20]}}

    def test_a_direct_entity_set_color_sends_a_valid_pair_only(self, monkeypatch):
        rig = Rig(monkeypatch)
        good = {"action": "set_color", "target": {"device_type": "light", "entity_id": "light.hall_lamp"},
                "parameters": {"hs_color": [10, 20]}, "delay_after": 0, "at_time": None}
        null = {"action": "set_color", "target": {"device_type": "light", "entity_id": "light.hall_lamp"},
                "parameters": {"hs_color": None}, "delay_after": 0, "at_time": None}
        _run(rig, [good, null], "make the hall lamp pink then green")
        sent = [c[2] for c in _light_calls(rig)]
        assert {"entity_id": "light.hall_lamp", "hs_color": [10, 20], "brightness": 255} in sent
        assert all(c.get("hs_color") != None or "hs_color" not in c for c in sent)  # noqa: E711


class TestHsColorsValues:
    @pytest.mark.parametrize("bad", [
        [[400, 50]], [[10, 150]], [[-1, 5]], [["a", 1]], [[1]], [[1, 2, 3]], "red", [], [None], [[True, 1]],
    ])
    def test_an_invalid_hs_colors_is_ignored(self, bad):
        assert "hs_colors" not in se._step_parameters({"parameters": {"hs_colors": bad}})

    def test_an_invalid_hs_colors_falls_back_to_a_valid_hs_color(self):
        got = se._step_parameters({"parameters": {"hs_colors": [[400, 5]], "hs_color": [10, 20]}})
        assert got["hs_colors"] == [[10, 20]]

    def test_a_valid_list_is_kept_as_is(self):
        colors = [[0, 100], [120, 50]]
        assert se._step_parameters({"parameters": {"hs_colors": colors}})["hs_colors"] == colors

    def test_an_out_of_range_hs_color_is_not_converted(self):
        assert "hs_colors" not in se._step_parameters({"parameters": {"hs_color": [361, 10]}})

    def test_an_invalid_list_reaches_no_light_writer(self, monkeypatch):
        rig = Rig(monkeypatch)
        _run(rig, [_step("set_color", {"hs_colors": [[999, 999]]})], "make the hall lights blue")
        assert all("hs_color" not in (c[2] or {}) or c[2]["hs_color"] != [999, 999] for c in _light_calls(rig))
