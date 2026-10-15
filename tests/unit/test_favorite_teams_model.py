"""The favourite-team model: the name rule, caps, duplicates, the tolerant
stored-value reader, and the league list staying equal to the sports RAG's."""
import ast
import logging
import unicodedata
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.favorite_teams import (
    LEAGUE_CODES,
    LEAGUE_LABELS,
    LEAGUE_SPORT,
    MAX_FAVORITE_TEAMS,
    FavoriteTeam,
    FavoriteTeamsUpdate,
    league_catalog,
    parse_stored,
    stored_value,
    valid_team_name,
)

RAG_MAIN = Path(__file__).resolve().parents[2] / "src" / "rag" / "sports" / "main.py"


@pytest.mark.parametrize("name", ["St. Louis-Blues O'Neil", "Bayern München", "x" * 64, "Anytown Owls", "49ers"])
def test_valid_names(name):
    assert valid_team_name(name)
    assert FavoriteTeam(name=name).name == name


@pytest.mark.parametrize(
    "name",
    ["x" * 65, "<b>T</b>", "a\nb", "a‮b", "", "   ", "a\x00b", "a;b", 'a"b', "a_b"],
)
def test_invalid_names(name):
    with pytest.raises(ValidationError):
        FavoriteTeam(name=name)


def test_name_is_stripped_and_nfc_normalized():
    decomposed = "Bayern Mu" + "̈" + "nchen"
    assert not valid_team_name(decomposed)
    team = FavoriteTeam(name="  " + decomposed + " ")
    assert team.name == "Bayern München"
    assert team.name == unicodedata.normalize("NFC", team.name)


def test_non_text_name_is_rejected():
    with pytest.raises(ValidationError):
        FavoriteTeam(name=5)


def test_league_rules():
    assert FavoriteTeam(name="Owls", league="nfl").league == "nfl"
    assert FavoriteTeam(name="Owls", league=None).league is None
    assert FavoriteTeam(name="Owls", league="").league is None
    with pytest.raises(ValidationError):
        FavoriteTeam(name="Owls", league="curling")


def test_extra_key_is_rejected():
    with pytest.raises(ValidationError):
        FavoriteTeam(name="Owls", colour="blue")
    with pytest.raises(ValidationError):
        FavoriteTeamsUpdate(teams=[], revision=0, extra=1)


def test_update_caps_duplicates_and_revision():
    assert MAX_FAVORITE_TEAMS == 8
    ok = [{"name": f"Team {i}"} for i in range(8)]
    assert len(FavoriteTeamsUpdate(teams=ok, revision=0).teams) == 8
    with pytest.raises(ValidationError):
        FavoriteTeamsUpdate(teams=ok + [{"name": "Team 9"}], revision=0)
    with pytest.raises(ValidationError):
        FavoriteTeamsUpdate(teams=[{"name": "Owls"}, {"name": "owls"}], revision=0)
    with pytest.raises(ValidationError):
        FavoriteTeamsUpdate(teams=[], revision=-1)


def test_same_name_in_two_leagues_is_still_a_duplicate():
    with pytest.raises(ValidationError):
        FavoriteTeamsUpdate(teams=[{"name": "Giants", "league": "nfl"}, {"name": "Giants", "league": "mlb"}], revision=0)


def test_parse_stored_missing_and_unreadable(caplog):
    assert parse_stored(None) == ((), 0)
    with caplog.at_level(logging.ERROR):
        assert parse_stored("{") == ((), 0)
        assert parse_stored("[]") == ((), 0)
    assert "favorite_teams_unreadable" in caplog.text


def test_parse_stored_drops_only_the_bad_item(caplog):
    raw = (
        '{"v":1,"revision":4,"teams":[{"name":"Anytown Owls","league":"nfl"},'
        '{"name":"<b>x</b>","league":null},{"name":"Hawks","league":"curling"},'
        '{"name":"Hawks","league":"mlb"},{"name":"anytown owls"}]}'
    )
    with caplog.at_level(logging.ERROR):
        teams, revision = parse_stored(raw)
    assert revision == 4
    assert [(t.name, t.league) for t in teams] == [("Anytown Owls", "nfl"), ("Hawks", "mlb")]
    assert "index=1" in caplog.text and "index=2" in caplog.text and "index=4" in caplog.text
    assert "<b>" not in caplog.text and "curling" not in caplog.text


def test_parse_stored_bad_revision_is_zero():
    assert parse_stored('{"revision":"x","teams":[]}') == ((), 0)
    assert parse_stored('{"revision":true,"teams":[]}') == ((), 0)


def test_stored_value_round_trips():
    teams = (FavoriteTeam(name="Bayern München", league="bundesliga"), FavoriteTeam(name="Owls"))
    again, revision = parse_stored(stored_value(teams, 7))
    assert again == teams and revision == 7


def _rag_league_keys() -> set:
    tree = ast.parse(RAG_MAIN.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "LEAGUE_TO_ESPN_PATH" for t in node.targets):
            return {k.value for k in node.value.keys}
    raise AssertionError("LEAGUE_TO_ESPN_PATH not found")


def test_league_codes_equal_the_sports_rag_keys():
    keys = _rag_league_keys()
    assert len(keys) == 17 and "nfl" in keys
    assert set(LEAGUE_CODES) == keys
    assert len(LEAGUE_CODES) == 17 and "nfl" in LEAGUE_CODES
    assert set(LEAGUE_LABELS) == keys and set(LEAGUE_SPORT) == keys


def test_league_sport_matches_the_rag_path_prefix():
    tree = ast.parse(RAG_MAIN.read_text())
    paths = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "LEAGUE_TO_ESPN_PATH" for t in node.targets):
            paths = {k.value: v.value for k, v in zip(node.value.keys, node.value.values)}
    for code, path in paths.items():
        assert LEAGUE_SPORT[code].lower() == path.split("/")[0], code


def test_league_catalog_shape():
    catalog = league_catalog()
    assert [c["code"] for c in catalog] == list(LEAGUE_CODES)
    assert {"code", "label", "sport"} == set(catalog[0])
    assert next(c for c in catalog if c["code"] == "international")["label"] == "International (national teams)"
