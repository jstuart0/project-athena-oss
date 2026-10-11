"""A successful tool result reaches the model exactly as the tool produced it.

Scrubbing applies only inside error objects. A successful payload's `message`,
`reason` or `detail` is user-facing data (sports, transit), so it must arrive in
the `role: tool` message byte-identical to `json.dumps(payload)`, through the
real `tool_call_node` and the real `execute_tools_parallel`.
"""
from __future__ import annotations

import json

import pytest

from orchestrator.model_safe_errors import is_error_object, scrub_tool_result, tool_error_result

from . import _model_safe_harness as m
from . import _public_audience_harness as h


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _reaching_the_model(monkeypatch, tool_calls, script):
    llm = m.ToolLLM(tool_calls)
    m.install(monkeypatch, m.FakeRag(script), llm)
    m.run_tool_call()
    return llm.tool_messages()


def test_the_sports_disambiguation_result(monkeypatch):
    options = [{"display_name": "Giants (NFL)"}, {"display_name": "Giants (MLB)"}]
    messages = _reaching_the_model(
        monkeypatch, [m.call("get_sports_scores", team="Giants")],
        {"sports": m.ok({"needs_disambiguation": True, "disambiguation_options": options})},
    )
    names = ["Giants (NFL)", "Giants (MLB)"]
    expected = {
        "needs_clarification": True,
        "team": "Giants",
        "question": "Which sport are you asking about for Giants?",
        "options": options,
        "option_names": names,
        "message": "I found Giants in multiple sports that are currently in season: Giants (NFL), Giants (MLB). Which one are you interested in?",
    }
    assert messages == [json.dumps(expected)]


def test_the_transportation_no_service_result(monkeypatch):
    payload = {"stop": "Main Street", "departures": [], "message": "No service on weekends"}
    messages = _reaching_the_model(monkeypatch, [m.call("search_transit", stop="Main Street")], {"transportation": m.ok(payload)})
    assert messages == [json.dumps(payload)]


def test_the_sports_season_ended_result(monkeypatch):
    season = {
        "strEvent": "Season ended", "dateEvent": None, "season_status": "ended",
        "team_record": "10-7", "team_standing": "3rd in the division", "season_name": "Regular Season",
        "message": "The team's regular season has ended with a record of 10-7. 3rd in the division.", "source": "espn",
    }

    def sports(path, params):
        if path.endswith("/teams/search"):
            return m.ok({"teams": [{"idTeam": "7", "strTeam": "Giants", "strLeague": "football/nfl"}]})
        if path.endswith("/last"):
            return m.ok({"events": [season]})
        if path.endswith("/next"):
            return m.ok({"events": []})
        return m.ok({"games": []})

    messages = _reaching_the_model(monkeypatch, [m.call("get_sports_scores", team="Giants")], {"sports": sports})
    expected = {"team": "Giants", "team_id": "7", "last_games": [season], "upcoming_games": [], "live_games": []}
    assert messages == [json.dumps(expected)]


# --- what counts as an error object ---------------------------------------------------------------


@pytest.mark.parametrize("value", [{"error": "x"}, {"success": False, "message": "x"}, tool_error_result(RuntimeError("boom"))])
def test_error_objects(value):
    assert is_error_object(value) is True


@pytest.mark.parametrize("value", [
    {"error": None, "message": "x"}, {"error": "", "data": 1}, {"message": "x"}, {"reason": "r", "detail": "d"},
    {"success": True, "message": "x"}, {"success": None}, [], "error", None, 5,
])
def test_successful_payloads(value):
    assert is_error_object(value) is False


def test_a_dict_with_only_a_message_comes_back_unchanged():
    payload = {"message": "Check http://10.0.0.5/x and TESLAMATE_DB_HOST", "reason": "weekend", "detail": "d"}
    assert scrub_tool_result(payload) == payload


def test_a_nested_provider_failure_is_scrubbed_and_the_rest_survives():
    result = {
        "events": [{"title": "Concert", "message": "Doors at 8"}],
        "providers": [{"events": [], "error": "http://10.0.0.5/boom"}, {"events": [1], "name": "ok"}],
    }
    scrubbed = scrub_tool_result(result)
    assert scrubbed["events"] == result["events"]
    assert scrubbed["providers"][1] == result["providers"][1]
    assert "10.0.0.5" not in json.dumps(scrubbed)
    assert scrubbed["providers"][0]["events"] == [] and scrubbed["providers"][0]["error"]
