"""The one sports status builder.

``team_events`` fetches a team's last games, next games and the live
scoreboard and returns one dict; ``status_headline`` is the pure rule that
decides whether the team is playing now, finished, or has a game coming. The
live scoreboard wins: a game in progress is reported as live, removed from the
upcoming list and never counted as the last game, and a winner is computed from
the final score, never taken from a provider field.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import structlog

from shared.local_time import local_now

logger = structlog.get_logger(__name__)

# Sports RAG league path (the team's ``strLeague``) -> live scoreboard code.
# A league missing here gets no live lookup.
LIVE_LEAGUE_CODES: Dict[str, str] = {
    "soccer/eng.1": "premier-league",
    "soccer/esp.1": "la-liga",
    "soccer/ger.1": "bundesliga",
    "soccer/ita.1": "serie-a",
    "soccer/fra.1": "ligue-1",
    "soccer/usa.1": "mls",
    "football/nfl": "nfl",
    "basketball/nba": "nba",
    "baseball/mlb": "mlb",
    "hockey/nhl": "nhl",
}

# A league the table does not know is still looked up when it is an ESPN path
# ("college-football/..." style): the scoreboard route accepts a path.
_ESPN_PATH = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)

LIVE_STATE = "in"
FINAL_STATE = "post"


def _game_state(game: Dict[str, Any]) -> str:
    """A scoreboard row without a state is treated as in progress."""
    return game.get("state") or LIVE_STATE


def live_league_code(league: str) -> Optional[str]:
    """The scoreboard league for a team's league: a known code, else a path-shaped value, else None."""
    if league in LIVE_LEAGUE_CODES:
        return LIVE_LEAGUE_CODES[league]
    return league if _ESPN_PATH.match(league or "") else None


def is_tie(home_score: Any, away_score: Any, provider_winner: Any = None) -> bool:
    """Equal readable scores, or an explicit provider "Tie" when the scores are unreadable."""
    h, a = _score(home_score), _score(away_score)
    if h is not None and a is not None:
        return h == a
    return str(provider_winner or "").strip().lower() == "tie"


def _score(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def winner_from_scores(home: Any, away: Any, home_score: Any, away_score: Any) -> Optional[str]:
    """The team with the higher score; None for a tie or a missing or unreadable score."""
    h, a = _score(home_score), _score(away_score)
    if h is None or a is None or h == a:
        return None
    return home if h > a else away


def _scoreboard_line(game: Dict[str, Any], tag: str) -> str:
    return (
        f"{game.get('away_team', '')} {game.get('away_score', '')} - "
        f"{game.get('home_score', '')} {game.get('home_team', '')} ({tag})"
    )


def _same_matchup(event: Dict[str, Any], game: Dict[str, Any]) -> bool:
    return (
        str(event.get("strHomeTeam", "")).lower() == str(game.get("home_team", "")).lower()
        and str(event.get("strAwayTeam", "")).lower() == str(game.get("away_team", "")).lower()
    )


def _is_today(event: Dict[str, Any], now: datetime) -> bool:
    date = event.get("dateEvent")
    return not date or str(date) == now.date().isoformat()


def _same_team(name: Any, team: str) -> bool:
    a, b = str(name or "").strip().lower(), (team or "").strip().lower()
    return bool(a and b and (a == b or a in b or b in a))


def _event_date(event: Dict[str, Any]) -> Optional[date]:
    try:
        return date.fromisoformat(str(event.get("dateEvent", ""))[:10])
    except ValueError:
        return None


def _display_date(day: date) -> str:
    return f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}"


def last_game(last_games: List[Dict[str, Any]], team: str, now: datetime) -> Optional[Dict[str, Any]]:
    """The team's most recent finished game, tense-neutral: its date (with
    weekday and ``days_ago``), the opponent, home or away, both scores from the
    asked team's side, and ``result`` (won/lost/tied) computed from those
    scores. ``summary`` is the one sentence a reply can repeat. None when no
    finished game with a readable matchup exists."""
    played = [e for e in last_games if e.get("strHomeTeam") and e.get("strAwayTeam")]
    played.sort(key=lambda e: str(e.get("dateEvent", "")), reverse=True)
    if not played:
        return None
    event = played[0]
    home, away = event["strHomeTeam"], event["strAwayTeam"]
    hs, as_ = _score(event.get("intHomeScore")), _score(event.get("intAwayScore"))
    day = _event_date(event)
    is_home, is_away = _same_team(home, team), _same_team(away, team)
    side = "home" if is_home and not is_away else "away" if is_away and not is_home else None
    out: Dict[str, Any] = {"opponent": None, "home_away": side, "team_score": None, "opponent_score": None, "result": None}
    if day:
        out.update(date=day.isoformat(), weekday=day.strftime("%A"), display_date=_display_date(day),
                   days_ago=(now.date() - day).days)
    if side:
        mine, theirs = (hs, as_) if side == "home" else (as_, hs)
        out.update(opponent=away if side == "home" else home, team_score=mine, opponent_score=theirs)
        if mine is not None and theirs is not None:
            out["result"] = "tied" if mine == theirs else "won" if mine > theirs else "lost"
    out["winner"] = winner_from_scores(home, away, hs, as_)
    out["tie"] = out["result"] == "tied" or (side is None and is_tie(event.get("intHomeScore"), event.get("intAwayScore"), event.get("winner")))
    when = f" on {out['display_date']}" if day else ""
    if out["result"]:
        where = "at home against" if side == "home" else "away at"
        verb = f"tied {out['team_score']}-{out['opponent_score']} {where}" if out["result"] == "tied" else (
            f"{out['result']} {out['team_score']}-{out['opponent_score']} {where}")
        out["summary"] = f"{team} {verb} {out['opponent']}{when} (final)"
    else:
        out["summary"] = f"{away} {event.get('intAwayScore', '')} - {event.get('intHomeScore', '')} {home}{when} (final)"
    return out


def status_headline(
    next_games: List[Dict[str, Any]],
    last_games: List[Dict[str, Any]],
    live_games: List[Dict[str, Any]],
    now: datetime,
    team: str = "",
) -> Dict[str, Any]:
    """Pure: {state: live|final|upcoming|none, summary, winner}.

    ``live_games`` is the team's scoreboard rows (any state). Priority: a game
    in progress; a game that finished today; the next game; the last game.
    ``upcoming`` is only a game dated today or later (a past-dated row in the
    next list is stale). Every non-live answer also carries the team's
    ``last_game`` so a past-tense question has its answer in any state.
    """
    in_play = [g for g in live_games if _game_state(g) == LIVE_STATE]
    if in_play:
        return {"state": "live", "summary": _scoreboard_line(in_play[0], str(in_play[0].get("status", "LIVE"))), "winner": None}

    finished = [g for g in live_games if _game_state(g) == FINAL_STATE]
    if finished:
        g = finished[0]
        return {
            "state": "final",
            "summary": _scoreboard_line(g, "FINAL"),
            "winner": winner_from_scores(g.get("home_team"), g.get("away_team"), g.get("home_score"), g.get("away_score")),
            "tie": is_tie(g.get("home_score"), g.get("away_score")),
        }

    played = [e for e in last_games if e.get("strHomeTeam")]
    last = played[0] if played else None
    if last and last.get("dateEvent") == now.date().isoformat():
        return _final_headline(last, last_game(played, team, now))

    previous = last_game(played, team, now)
    upcoming = [
        e for e in next_games
        if e.get("game_state") != LIVE_STATE and e.get("strHomeTeam")
        and (_event_date(e) is None or _event_date(e) >= now.date())
    ]
    if upcoming:
        e = upcoming[0]
        when = " ".join(str(p) for p in (e.get("dateEvent"), e.get("strTime")) if p)
        headline: Dict[str, Any] = {
            "state": "upcoming", "summary": f"{e.get('strAwayTeam', '')} at {e.get('strHomeTeam', '')} on {when}".strip(),
            "winner": None,
        }
        if previous:
            headline["last_game"] = previous
        return headline

    if last:
        return _final_headline(last, previous)
    return {"state": "none", "summary": "", "winner": None}


def _final_headline(event: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    home, away = event.get("strHomeTeam"), event.get("strAwayTeam")
    hs, as_ = event.get("intHomeScore"), event.get("intAwayScore")
    headline = {
        "state": "final",
        "summary": f"{away} {as_} - {hs} {home} (final, {event.get('dateEvent', '')})",
        "winner": winner_from_scores(home, away, hs, as_),
        "tie": is_tie(hs, as_, event.get("winner")),
    }
    if previous:
        headline["last_game"] = previous
    return headline


def _final_event(g: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """A game finished today that the provider's last-games list does not carry yet."""
    tie = is_tie(g.get("home_score"), g.get("away_score"))
    event: Dict[str, Any] = {
        "strEvent": g.get("event_name") or f"{g.get('away_team', '')} @ {g.get('home_team', '')}",
        "dateEvent": now.date().isoformat(),
        "strHomeTeam": g.get("home_team"),
        "strAwayTeam": g.get("away_team"),
        "intHomeScore": g.get("home_score"),
        "intAwayScore": g.get("away_score"),
        "winner": winner_from_scores(g.get("home_team"), g.get("away_team"), g.get("home_score"), g.get("away_score")),
        "source": "scoreboard",
    }
    if tie:
        event["tie"] = True
        event["result"] = "tie"
    return event


def _ok(response: Any) -> bool:
    return not isinstance(response, BaseException) and bool(getattr(response, "success", False))


async def team_events(
    rag_client: Any,
    team_data: Dict[str, Any],
    *,
    team_name: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Last, upcoming and live games for one team, plus the status headline.

    ``team_data`` is a sports team-search row (``idTeam``, ``strTeam``,
    ``strLeague``). Failed sub-requests give empty lists; the caller validates.
    """
    now = now or local_now()
    team_id = team_data["idTeam"]
    full_name = team_data.get("strTeam") or team_name or ""
    league = team_data.get("strLeague", "")
    live_league = live_league_code(league)

    async def no_live() -> None:
        return None

    last_r, next_r, live_r = await asyncio.gather(
        rag_client.get("sports", f"/sports/events/{team_id}/last"),
        rag_client.get("sports", f"/sports/events/{team_id}/next"),
        rag_client.get("sports", "/sports/scores/live", params={"league": live_league, "team": full_name})
        if live_league
        else no_live(),
        return_exceptions=True,
    )

    data: Dict[str, Any] = {"team": full_name, "team_id": team_id, "league": league}

    last_games = [dict(e) for e in last_r.data.get("events", [])] if _ok(last_r) else []
    next_games = [dict(e) for e in next_r.data.get("events", [])] if _ok(next_r) else []
    if _ok(next_r) and next_games and next_games[0].get("season_status") == "ended":
        data["season_status"] = "ended"
        data["team_record"] = next_games[0].get("team_record")
        data["team_standing"] = next_games[0].get("team_standing")
        data["season_message"] = next_games[0].get("message")

    team_lower = full_name.lower()
    scoreboard = []
    if _ok(live_r):
        scoreboard = [
            g
            for g in live_r.data.get("games", [])
            if team_lower in g.get("home_team", "").lower() or team_lower in g.get("away_team", "").lower()
        ]
    live_games = [g for g in scoreboard if _game_state(g) == LIVE_STATE]

    next_games = [
        e
        for e in next_games
        if e.get("game_state") != LIVE_STATE
        and not any(_same_matchup(e, g) and _is_today(e, now) for g in live_games)
    ]
    last_games = [
        e for e in last_games if not any(_same_matchup(e, g) and _is_today(e, now) for g in live_games)
    ]
    for event in last_games:
        if "intHomeScore" not in event:
            continue
        provider_winner = event.get("winner")
        event["winner"] = winner_from_scores(
            event.get("strHomeTeam"), event.get("strAwayTeam"), event.get("intHomeScore"), event.get("intAwayScore")
        )
        if is_tie(event.get("intHomeScore"), event.get("intAwayScore"), provider_winner):
            event["tie"] = True
            event["result"] = "tie"

    for g in scoreboard:
        if _game_state(g) == FINAL_STATE and not any(
            _same_matchup(e, g) and _is_today(e, now) for e in last_games
        ):
            last_games.insert(0, _final_event(g, now))

    data["last_games"] = last_games
    data["upcoming_games"] = next_games
    data["live_games"] = live_games
    if live_games:
        data["has_live_game"] = True
        data["live_score_summary"] = _scoreboard_line(live_games[0], str(live_games[0].get("status", "LIVE")))
    previous = last_game(last_games, full_name, now)
    if previous:
        data["today"] = _display_date(now.date())
        data["last_game"] = previous
    data["status"] = status_headline(next_games, last_games, scoreboard, now, full_name)

    logger.info(
        "sports_data_fetched",
        last_games=len(last_games),
        upcoming_games=len(next_games),
        live_games=len(live_games),
        state=data["status"]["state"],
    )
    return data
