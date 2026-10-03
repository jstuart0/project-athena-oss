"""A TV app launch with real app configs, through the real permission guard.

``AppleTVHandler.handle_launch`` opens the app with
``media_player.select_source`` and, for an app with a profile screen while
``auto_profile_select`` is on, presses select with ``remote.send_command``.
A guest's baseline domains don't include ``remote``, so that press must not
be attempted for a guest; for anyone else it is optional, and its failure
must not turn an app that opened into "Failed to launch".

The handler's Home Assistant client is the real ``PermissionEnforcingHAClient``
over a recording inner client, inside a real ``ha_permission_scope``. The app
list and the feature flag answer in the shape of the seeded rows.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import tv_handler  # noqa: E402

ROOM = "living_room"
MEDIA_PLAYER = "media_player.living_room_tv"
REMOTE = "remote.living_room_tv"
PROFILE_DELAY_MS = 1500


def _app(name, *, has_profile_screen, guest_allowed):
    return {
        "app_name": name,
        "display_name": name,
        "icon_url": None,
        "has_profile_screen": has_profile_screen,
        "profile_select_delay_ms": PROFILE_DELAY_MS,
        "guest_allowed": guest_allowed,
        "deep_link_scheme": None,
        "enabled": True,
        "sort_order": 1,
    }


SEEDED_APPS = [
    _app("Netflix", has_profile_screen=True, guest_allowed=True),
    _app("Hulu", has_profile_screen=False, guest_allowed=True),
    _app("Photos", has_profile_screen=False, guest_allowed=False),
]

SELECT_SOURCE = ("media_player", "select_source", {"entity_id": MEDIA_PLAYER, "source": "Netflix"})
SEND_SELECT = ("remote", "send_command", {"entity_id": REMOTE, "command": "select"})


class _RecordingHA:
    """The inner Home Assistant client: records each write it carries out."""

    def __init__(self, failing=()):
        self.made = []
        self.attempted = []
        self._failing = set(failing)

    async def call_service(self, domain, service, service_data=None):
        self.attempted.append((domain, service, service_data))
        if (domain, service) in self._failing:
            raise RuntimeError("zz-home-assistant-error-text")
        self.made.append((domain, service, service_data))
        return {}


class _DenialCounter:
    """Stands in for ``ha_write_denied_total``: counts increments."""

    def __init__(self):
        self.increments = 0

    def labels(self, **_labels):
        return self

    def inc(self):
        self.increments += 1


@pytest.fixture
def denied_total(monkeypatch):
    counter = _DenialCounter()
    monkeypatch.setattr(mp, "ha_write_denied_total", counter)
    return counter


@pytest.fixture
def sleep(monkeypatch):
    slept = AsyncMock()
    monkeypatch.setattr(tv_handler.asyncio, "sleep", slept)
    return slept


@pytest.fixture
def admin(monkeypatch):
    """The three admin reads, answering like the seeded database. ``apps``
    and ``auto_profile_select`` can be changed by a test before the launch."""

    class _Admin:
        apps = list(SEEDED_APPS)
        auto_profile_select = True

    async def get_tv_configs():
        return {ROOM: {
            "room_name": ROOM,
            "display_name": "Living Room",
            "media_player_entity_id": MEDIA_PLAYER,
            "remote_entity_id": REMOTE,
        }}

    async def get_app_configs(guest_mode=False):
        # The route filters on guest_allowed itself when asked for the guest list.
        return [app for app in _Admin.apps if app["guest_allowed"] or not guest_mode]

    async def get_feature_flag(feature_name):
        return feature_name == "auto_profile_select" and _Admin.auto_profile_select

    monkeypatch.setattr(tv_handler, "get_tv_configs", get_tv_configs)
    monkeypatch.setattr(tv_handler, "get_app_configs", get_app_configs)
    monkeypatch.setattr(tv_handler, "get_feature_flag", get_feature_flag)
    return _Admin


def _launch(ha, *, mode, permissions=None, app="Netflix"):
    """(result, scope) of one launch under a scope opened the way the TV node
    opens it: the request's permissions and mode."""
    handler = tv_handler.AppleTVHandler(ha, admin_client=None)
    assert isinstance(handler.ha, mp.PermissionEnforcingHAClient), "the real guard is in the path"

    async def run():
        with mp.ha_permission_scope(permissions or {"mode": mode}, mode=mode) as scope:
            result = await handler.handle_launch(app_name=app, room=ROOM, guest_mode=(mode == "guest"))
        return result, scope

    return asyncio.run(run())


def _profile_logs(logs):
    return [record for record in logs if record.get("event", "").startswith("tv_profile_select")]


# Owner ----------------------------------------------------------------------

def test_owner_launch_of_a_profile_screen_app_presses_select(admin, sleep, denied_total, captured_logs):
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="owner")

    assert result["success"] is True
    assert ha.made == [SELECT_SOURCE, SEND_SELECT], "two writes, in order"
    sleep.assert_awaited_once_with(PROFILE_DELAY_MS / 1000)
    assert scope.denials == [] and scope.allowed_writes == 2
    assert denied_total.increments == 0
    assert _profile_logs(captured_logs) == []


def test_owner_launch_still_succeeds_when_the_select_press_fails(admin, sleep, denied_total, captured_logs, caplog):
    ha = _RecordingHA(failing={("remote", "send_command")})
    result, scope = _launch(ha, mode="owner")

    assert result["success"] is True, "the app opened; a failed optional press isn't a failed launch"
    assert result["message"] == "Opening Netflix on Living Room TV."
    assert ha.attempted == [SELECT_SOURCE, SEND_SELECT], "the press really was tried"
    assert ha.made == [SELECT_SOURCE], "one write was made"
    assert scope.denials == [] and denied_total.increments == 0
    assert _profile_logs(captured_logs) == [{
        "event": "tv_profile_select_failed",
        "log_level": "error",
        "app": "Netflix",
        "room": ROOM,
        "error_type": "RuntimeError",
    }], "one line: event, app, room and the error's type, never its text"
    assert "zz-home-assistant-error-text" not in repr(captured_logs)
    assert "zz-home-assistant-error-text" not in caplog.text


def test_owner_launch_with_the_flag_off_makes_one_write(admin, sleep, denied_total):
    admin.auto_profile_select = False
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="owner")

    assert result["success"] is True
    assert ha.made == [SELECT_SOURCE]
    sleep.assert_not_awaited()
    assert scope.denials == []


# Guest ----------------------------------------------------------------------

def test_guest_launch_of_a_profile_screen_app_stops_at_the_profile_screen(admin, sleep, denied_total, captured_logs):
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="guest")

    assert "remote" not in scope.permissions["allowed_domains"], "the guest baseline really excludes remote"
    assert result["success"] is True, "the app opened"
    assert ha.attempted == [SELECT_SOURCE], "exactly one write, and no press was tried"
    assert ha.made == [SELECT_SOURCE]
    sleep.assert_not_awaited()
    assert scope.denials == [] and scope.halted is False
    assert denied_total.increments == 0
    assert _profile_logs(captured_logs) == []


def test_guest_launch_of_an_app_off_the_guest_list_is_refused(admin, sleep, denied_total):
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="guest", app="Photos")

    assert result["success"] is False and result["error"] == "app_not_allowed"
    assert ha.attempted == []
    assert scope.denials == [] and denied_total.increments == 0


def test_guest_launch_is_refused_when_the_app_list_is_empty(admin, sleep, denied_total):
    """Admin-backend refused the call or is down: fail closed."""
    admin.apps = []
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="guest")

    assert result["success"] is False and result["error"] == "app_not_allowed"
    assert ha.attempted == []
    assert scope.denials == []


# The launch itself ----------------------------------------------------------

def test_a_failed_select_source_is_a_failed_launch(admin, sleep, denied_total, captured_logs):
    ha = _RecordingHA(failing={("media_player", "select_source")})
    result, scope = _launch(ha, mode="owner")

    assert result["success"] is False
    assert result["message"] == "Failed to launch Netflix. Please try again."
    assert ha.attempted == [SELECT_SOURCE], "no second write"
    assert ha.made == []
    sleep.assert_not_awaited()
    assert _profile_logs(captured_logs) == []


# A denied press is never passed off as a success -----------------------------

def test_a_denied_select_press_stays_a_denial(admin, sleep, denied_total, captured_logs):
    """Not a guest, but the scope's permissions deny ``remote`` (a degraded
    house whose fallback restricts it). Best-effort covers a press that
    failed, not one the guard refused: the denial stays on the scope, where
    the TV node turns it into the refusal the caller hears."""
    degraded = {**mp.degraded_permissions(), "restricted_entities": [r"^remote\."]}
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="owner", permissions=degraded)

    assert ha.attempted == [SELECT_SOURCE], "the denied press never reached Home Assistant"
    assert [(d.domain, d.service) for d in scope.denials] == [("remote", "send_command")]
    assert scope.halted is True and scope.allowed_writes == 1
    assert denied_total.increments == 1
    assert result["success"] is False
    assert _profile_logs(captured_logs) == [], "a denial is the guard's to log, not a failed press"
