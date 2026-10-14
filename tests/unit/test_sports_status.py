"""sports_status: live games are live, winners come from the final score."""
from __future__ import annotations

import ast
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")

from orchestrator import sports_status as ss  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)
TODAY = "2026-10-11"


def game(state, hs="14", as_="7", status=None, home="Baltimore Ravens", away="Kansas City Chiefs"):
    return {
        "home_team": home, "away_team": away, "home_score": hs, "away_score": as_,
        "state": state, "status": status or {"in": "LIVE - 5:00 (Q2)", "post": "FINAL", "pre": "Starts 01:00 PM ET"}[state],
    }


def upcoming(state="pre", date=TODAY):
    return {"strHomeTeam": "Baltimore Ravens", "strAwayTeam": "Kansas City Chiefs", "dateEvent": date,
            "strTime": "17:00", "game_state": state}


def last(date="2026-10-04", hs="21", as_="24"):
    return {"strHomeTeam": "Baltimore Ravens", "strAwayTeam": "Buffalo Bills", "dateEvent": date,
            "intHomeScore": hs, "intAwayScore": as_, "winner": "WRONG PROVIDER VALUE"}


def test_pre_game_is_upcoming():
    out = ss.status_headline([upcoming()], [last()], [game("pre", "0", "0")], NOW)
    assert out["state"] == "upcoming" and out["winner"] is None
    assert "Kansas City Chiefs at Baltimore Ravens" in out["summary"]


def test_in_game_is_live_and_has_no_winner():
    out = ss.status_headline([upcoming("in")], [last()], [game("in")], NOW)
    assert out["state"] == "live" and out["winner"] is None
    assert "14" in out["summary"] and "LIVE" in out["summary"]


def test_post_game_is_final_with_the_winner_from_the_score():
    out = ss.status_headline([], [last()], [game("post", "10", "27")], NOW)
    assert out["state"] == "final"
    assert out["winner"] == "Kansas City Chiefs"


def test_home_winner():
    assert ss.status_headline([], [], [game("post", "31", "3")], NOW)["winner"] == "Baltimore Ravens"


def test_a_tie_has_no_winner():
    assert ss.status_headline([], [], [game("post", "20", "20")], NOW)["winner"] is None


def test_unreadable_or_missing_scores_have_no_winner():
    assert ss.winner_from_scores("A", "B", None, "3") is None
    assert ss.winner_from_scores("A", "B", "x", "3") is None


def test_last_game_winner_ignores_the_provider_field():
    out = ss.status_headline([], [last(date=TODAY, hs="9", as_="30")], [], NOW)
    assert out["state"] == "final" and out["winner"] == "Buffalo Bills"


def test_nothing_is_none():
    assert ss.status_headline([], [], [], NOW) == {"state": "none", "summary": "", "winner": None}


def test_an_old_last_game_does_not_outrank_the_next_game():
    assert ss.status_headline([upcoming(date="2026-10-18")], [last()], [], NOW)["state"] == "upcoming"


def fake_client(last_events, next_events, games):
    async def get(_svc, path, params=None):
        data = {"/last": {"events": last_events}, "/next": {"events": next_events}}
        for suffix, payload in data.items():
            if path.endswith(suffix):
                return SimpleNamespace(success=True, data=payload)
        get.live_params = params
        return SimpleNamespace(success=True, data={"games": games})

    return SimpleNamespace(get=get)


TEAM = {"idTeam": "133", "strTeam": "Baltimore Ravens", "strLeague": "football/nfl"}


def test_a_live_game_in_both_upcoming_and_live_is_reported_live_and_not_upcoming():
    client = fake_client([last()], [upcoming("in"), upcoming("pre", "2026-10-18")], [game("in")])
    data = asyncio.run(ss.team_events(client, TEAM, now=NOW))
    assert data["status"]["state"] == "live"
    assert data["has_live_game"] is True
    assert len(data["live_games"]) == 1
    assert [e["dateEvent"] for e in data["upcoming_games"]] == ["2026-10-18"]
    assert "Ravens" in data["live_score_summary"]


def test_a_same_matchup_upcoming_row_without_game_state_is_still_removed_when_live():
    row = upcoming(state="")
    row.pop("game_state")
    client = fake_client([], [row], [game("in")])
    data = asyncio.run(ss.team_events(client, TEAM, now=NOW))
    assert data["upcoming_games"] == []


def test_a_live_game_is_never_last():
    finished_looking = last(date=TODAY)
    finished_looking.update(strAwayTeam="Kansas City Chiefs")
    client = fake_client([finished_looking], [], [game("in")])
    data = asyncio.run(ss.team_events(client, TEAM, now=NOW))
    assert data["last_games"] == []


def test_last_game_winner_is_recomputed_from_the_score():
    client = fake_client([last(hs="21", as_="24")], [], [])
    data = asyncio.run(ss.team_events(client, TEAM, now=NOW))
    assert data["last_games"][0]["winner"] == "Buffalo Bills"


def test_an_unmapped_league_gets_no_live_lookup():
    calls = []

    async def get(_svc, path, params=None):
        calls.append(path)
        return SimpleNamespace(success=True, data={"events": []})

    team = dict(TEAM, strLeague="Rugby Union")
    data = asyncio.run(ss.team_events(SimpleNamespace(get=get), team, now=NOW))
    assert not any("scores/live" in c for c in calls)
    assert data["live_games"] == [] and data["status"]["state"] == "none"


def test_the_live_lookup_has_no_default_league_for_a_mapped_one():
    client = fake_client([], [], [])
    asyncio.run(ss.team_events(client, TEAM, now=NOW))
    assert client.get.live_params == {"league": "nfl", "team": "Baltimore Ravens"}


def test_only_this_teams_games_count_and_failed_requests_give_empty_lists():
    async def get(_svc, path, params=None):
        if "scores/live" in path:
            return SimpleNamespace(success=True, data={"games": [game("in", home="Other", away="Else")]})
        raise RuntimeError("down")

    data = asyncio.run(ss.team_events(SimpleNamespace(get=get), TEAM, now=NOW))
    assert data["live_games"] == [] and data["last_games"] == [] and data["upcoming_games"] == []


def test_season_ended_fields_are_carried():
    ended = {"season_status": "ended", "team_record": "12-5", "team_standing": "1st", "message": "Season over"}
    data = asyncio.run(ss.team_events(fake_client([], [ended], []), TEAM, now=NOW))
    assert data["season_status"] == "ended" and data["team_record"] == "12-5"


def test_the_events_path_literal_lives_only_in_sports_status():
    hits = {}
    for path in (ROOT / "src" / "orchestrator").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/sports/events/" in node.value:
                hits.setdefault(path.relative_to(ROOT).as_posix(), 0)
                hits[path.relative_to(ROOT).as_posix()] += 1
    assert set(hits) == {"src/orchestrator/sports_status.py"}
    assert hits["src/orchestrator/sports_status.py"] >= 1


def test_a_season_ended_placeholder_is_not_a_played_game():
    placeholder = {"strEvent": "Season ended", "dateEvent": None, "season_status": "ended"}
    assert ss.status_headline([], [placeholder], [], NOW)["state"] == "none"


def test_a_row_the_provider_marks_in_progress_is_dropped_from_upcoming_whatever_the_team_spelling():
    row = dict(upcoming("in"), strHomeTeam="Ravens", strAwayTeam="Chiefs")
    data = asyncio.run(ss.team_events(fake_client([], [row], [game("in")]), TEAM, now=NOW))
    assert data["upcoming_games"] == []
    pre = dict(row, game_state="pre")
    data = asyncio.run(ss.team_events(fake_client([], [pre], []), TEAM, now=NOW))
    assert len(data["upcoming_games"]) == 1  # positive control


@pytest.mark.parametrize("league", ["football/college-football", "basketball/wnba", "soccer/uefa.champions"])
def test_a_path_shaped_league_is_passed_through_to_the_live_lookup(league):
    client = fake_client([], [], [game("in")])
    asyncio.run(ss.team_events(client, dict(TEAM, strLeague=league), now=NOW))
    assert client.get.live_params == {"league": league, "team": "Baltimore Ravens"}


@pytest.mark.parametrize("league", ["", "Rugby Union", "a/b/c", "x/../y", "football/nfl?x=1"])
def test_other_league_values_get_no_live_lookup(league):
    assert ss.live_league_code(league) is None


def test_a_game_finished_today_but_not_in_last_yet_is_data_and_final():
    client = fake_client([last()], [], [game("post", "10", "27")])
    data = asyncio.run(ss.team_events(client, TEAM, now=NOW))
    assert data["last_games"][0]["dateEvent"] == TODAY
    assert data["last_games"][0]["winner"] == "Kansas City Chiefs"
    assert data["status"]["state"] == "final"


def test_a_final_today_row_alone_makes_the_payload_non_empty_for_the_validator():
    from orchestrator.rag_validator import ValidationResult, validator

    data = asyncio.run(ss.team_events(fake_client([], [], [game("post", "10", "27")]), TEAM, now=NOW))
    result, _reason, _ = validator.validate_sports_response(data, "did the ravens win")
    assert result != ValidationResult.EMPTY
    empty = asyncio.run(ss.team_events(fake_client([], [], []), TEAM, now=NOW))
    assert validator.validate_sports_response(empty, "did the ravens win")[0] == ValidationResult.EMPTY  # control


def test_a_final_today_row_already_in_last_is_not_duplicated():
    row = dict(last(date=TODAY, hs="10", as_="27"), strAwayTeam="Kansas City Chiefs")
    data = asyncio.run(ss.team_events(fake_client([row], [], [game("post", "10", "27")]), TEAM, now=NOW))
    assert len(data["last_games"]) == 1


def test_an_explicit_provider_tie_is_preserved_as_a_tie_not_a_missing_winner():
    tied = dict(last(hs=None, as_=None), winner="Tie")
    data = asyncio.run(ss.team_events(fake_client([tied], [], []), TEAM, now=NOW))
    ev = data["last_games"][0]
    assert ev["winner"] is None and ev["tie"] is True and ev["result"] == "tie"
    unknown = asyncio.run(ss.team_events(fake_client([last(hs=None, as_=None)], [], []), TEAM, now=NOW))
    assert "tie" not in unknown["last_games"][0]  # control: unknown score is not a tie


def test_equal_scores_are_a_tie_in_events_and_headlines():
    data = asyncio.run(ss.team_events(fake_client([last(hs="20", as_="20")], [], []), TEAM, now=NOW))
    assert data["last_games"][0]["tie"] is True
    out = ss.status_headline([], [], [game("post", "17", "17")], NOW)
    assert out["tie"] is True and out["winner"] is None
    assert ss.status_headline([], [], [game("post", "17", "3")], NOW)["tie"] is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),  # unset: no default team
        ("", ""),
        ("  Real Madrid  ", "Real Madrid"),
        ("St. Louis-Blues O'Neil", "St. Louis-Blues O'Neil"),
        ("x" * 64, "x" * 64),
        ("x" * 65, ""),
        ("Team; drop table", ""),
        ("Team\nnewline", ""),
        ("<b>Team</b>", ""),
    ],
)
def test_sports_default_team_is_stripped_validated_and_empty_by_default(monkeypatch, raw, expected):
    from shared.config import AthenaConfig

    if raw is None:
        monkeypatch.delenv("SPORTS_DEFAULT_TEAM", raising=False)
    else:
        monkeypatch.setenv("SPORTS_DEFAULT_TEAM", raw)
    assert AthenaConfig(_env_file=None).sports_default_team == expected


def test_the_default_team_is_documented_and_never_named_in_code():
    assert "SPORTS_DEFAULT_TEAM" in (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    assert "SPORTS_DEFAULT_TEAM=" in (ROOT / "config.env.example").read_text(encoding="utf-8")
    assert '"Ravens"' not in (ROOT / "src/orchestrator/nodes/retrieve.py").read_text(encoding="utf-8")
