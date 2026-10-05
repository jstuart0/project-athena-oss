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

The second half drives ``route_tv_node`` with the mode and permissions the
real ``resolve_request_authorization`` gives for an owner house, a guest house
and a degraded one (mode service unreachable, or still starting).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestrator.nodes import _runtime, route_tv_node  # noqa: E402
from orchestrator.nodes import route_tv as route_tv_module  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import tv_handler  # noqa: E402
from orchestrator.state import IntentCategory, OrchestratorState  # noqa: E402

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

def _select_source(app="Netflix", entity_id=MEDIA_PLAYER):
    return ("media_player", "select_source", {"entity_id": entity_id, "source": app})


SELECT_SOURCE = _select_source()
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
        multi_tv_commands = False
        rooms = [ROOM]

    async def get_tv_configs():
        return {room: {
            "room_name": room,
            "display_name": room.replace("_", " ").title(),
            "media_player_entity_id": f"media_player.{room}_tv",
            "remote_entity_id": f"remote.{room}_tv",
        } for room in _Admin.rooms}

    async def get_app_configs(guest_mode=False):
        # The route filters on guest_allowed itself when asked for the guest list.
        return [app for app in _Admin.apps if app["guest_allowed"] or not guest_mode]

    async def get_feature_flag(feature_name):
        return bool(getattr(_Admin, feature_name, False))

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
    # The wait and the writes in one list: the profile screen needs the delay
    # before the press, not after it.
    order = ha.made
    sleep.side_effect = lambda seconds: order.append(("sleep", seconds))
    result, scope = _launch(ha, mode="owner")

    assert result["success"] is True
    assert order == [SELECT_SOURCE, ("sleep", PROFILE_DELAY_MS / 1000), SEND_SELECT], "open, wait, then press"
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


def test_owner_launch_of_an_app_without_a_profile_screen_makes_one_write(admin, sleep, denied_total):
    ha = _RecordingHA()
    result, scope = _launch(ha, mode="owner", app="Hulu")

    assert admin.auto_profile_select is True, "the flag is on: only the app's own setting holds the press back"
    assert result["success"] is True
    assert ha.attempted == [_select_source("Hulu")]
    sleep.assert_not_awaited()
    assert scope.denials == []


def test_owner_launch_of_an_app_not_in_the_list_makes_one_write(admin, sleep, denied_total):
    ha = _RecordingHA()
    assert "Twitch" not in [app["app_name"] for app in admin.apps]
    result, scope = _launch(ha, mode="owner", app="Twitch")

    assert result["success"] is True
    assert ha.attempted == [_select_source("Twitch")]
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


# A write the guard refuses is never passed off as a failed or optional one ----

class _LatchingHA(_RecordingHA):
    """Latches the scope shut once the app has opened, as a denial elsewhere
    in the same request would, so the guard refuses the press."""

    async def call_service(self, domain, service, service_data=None):
        result = await super().call_service(domain, service, service_data)
        mp.current_ha_scope().halted = True
        return result


def test_a_press_the_guard_refuses_is_raised_not_swallowed(admin, sleep, denied_total, captured_logs):
    ha = _LatchingHA()
    handler = tv_handler.AppleTVHandler(ha, admin_client=None)

    async def run():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner") as scope:
            with pytest.raises(mp.HAWritePermissionDenied):
                await handler.handle_launch(app_name="Netflix", room=ROOM, guest_mode=False)
        return scope

    scope = asyncio.run(run())

    assert ha.attempted == [SELECT_SOURCE], "the refused press never reached Home Assistant"
    assert [(d.domain, d.service, d.reason) for d in scope.denials] == [
        ("remote", "send_command", "halted_after_denial"),
    ]
    assert denied_total.increments == 1
    assert _profile_logs(captured_logs) == [], "a refusal is the guard's to log, not a failed press"
    assert _events(captured_logs, "tv_launch_failed") == [], "nor a failed launch"


def test_a_launch_outside_any_scope_presses_nothing(admin, sleep, denied_total):
    """No open scope means nobody has been established as the owner."""
    ha = _RecordingHA()
    handler = tv_handler.AppleTVHandler(ha, admin_client=None)

    result = asyncio.run(handler.handle_launch(app_name="Netflix", room=ROOM, guest_mode=False))

    assert result["success"] is True
    assert ha.attempted == [SELECT_SOURCE]
    sleep.assert_not_awaited()


# Through route_tv_node, with the real mode resolution ------------------------

GUEST_PERMISSIONS = {
    "mode": "guest",
    "allowed_intents": ["tv_control", "weather"],
    "allowed_domains": ["light", "media_player", "switch", "climate"],
    "restricted_entities": [],
}


class _Answer:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _ModeService:
    """The mode service as the orchestrator's client sees it. ``house`` is
    "owner", "guest", "unreachable" (every call fails) or "cold_start" (it
    answers ``mode="degraded"`` until its first config load)."""

    def __init__(self, house, guest_permissions=None):
        self.house = house
        self.guest_permissions = guest_permissions or GUEST_PERMISSIONS

    async def get(self, path, params=None):
        if self.house == "unreachable":
            raise httpx.ConnectError("mode service is down")
        if path == "/mode":
            if self.house == "cold_start":
                return _Answer({"mode": "degraded", "reason": "config never loaded"})
            return _Answer({"mode": self.house, "override_active": False})
        assert path == "/mode/permissions", path
        return _Answer({"mode": "owner"} if self.house == "owner" else self.guest_permissions)


@pytest.fixture
def node(monkeypatch, admin, sleep, denied_total):
    """ask(house, ha, query) -> (authorization, state after route_tv_node)."""
    monkeypatch.setattr(route_tv_module, "get_feature_config",
                        AsyncMock(return_value={"enabled": False, "config": {}}))
    monkeypatch.setattr(route_tv_module, "configured_assistant_names", AsyncMock(return_value=()))

    def ask(house, ha, query="open Netflix", guest_permissions=None):
        _runtime.set_mode_client(_ModeService(house, guest_permissions))
        _runtime.set_tv_handler(tv_handler.AppleTVHandler(ha, admin_client=None))

        async def run():
            authz = await mp.resolve_request_authorization(None, None, "household")
            state = OrchestratorState(
                query=query, mode=authz.mode, permissions=authz.permissions, mode_degraded=authz.degraded,
            )
            state.intent = IntentCategory.TV_CONTROL
            state.room = ROOM
            return authz, await route_tv_node(state)

        return asyncio.run(run())

    yield ask
    _runtime.reset_for_test()


def _events(logs, event):
    return [record for record in logs if record.get("event") == event]


def test_node_owner_house_opens_the_app_and_presses_select(node, sleep):
    ha = _RecordingHA()
    authz, state = node("owner", ha)

    assert (authz.mode, authz.degraded, authz.permissions["mode"]) == ("owner", False, "owner")
    assert ha.made == [SELECT_SOURCE, SEND_SELECT]
    assert state.answer == "Opening Netflix on Living Room TV." and state.error is None


def test_node_guest_house_opens_a_listed_app_and_presses_nothing(node, sleep, denied_total):
    ha = _RecordingHA()
    authz, state = node("guest", ha)

    assert (authz.mode, authz.degraded, authz.permissions["mode"]) == ("guest", False, "guest")
    assert ha.attempted == [SELECT_SOURCE]
    assert state.answer == "Opening Netflix on Living Room TV." and state.error is None
    assert denied_total.increments == 0


def test_node_guest_house_refuses_an_app_off_the_guest_list(node):
    ha = _RecordingHA()
    _authz, state = node("guest", ha, query="open Photos")

    assert ha.attempted == []
    assert state.answer == "Sorry, Photos is not available in guest mode."
    assert state.error == "app_not_allowed"


@pytest.mark.parametrize("house", ["unreachable", "cold_start"])
def test_node_degraded_house_opens_the_app_and_presses_nothing(house, node, sleep, denied_total):
    """A degraded house reports mode "owner" with the degraded permission
    set, which restricts neither media_player nor remote. It isn't known to
    be the owner's, so nobody is entered into the first profile."""
    ha = _RecordingHA()
    authz, state = node(house, ha)

    assert (authz.mode, authz.degraded, authz.permissions["mode"]) == ("owner", True, "degraded")
    assert state.mode == "owner" and state.mode_degraded is True
    assert ha.attempted == [SELECT_SOURCE], "the app opens; no press is tried"
    sleep.assert_not_awaited()
    assert state.answer == "Opening Netflix on Living Room TV." and state.error is None
    assert denied_total.increments == 0


def test_node_degraded_house_does_not_apply_the_guest_app_list(node):
    """Pins what the tree does today, not a decision: in a degraded house the
    guest app list isn't consulted, like every other intent during an outage
    ("owners keep lights, climate, and media"). Whether it should be is open."""
    ha = _RecordingHA()
    _authz, state = node("unreachable", ha, query="open Photos")

    assert ha.made == [_select_source("Photos")]
    assert state.answer == "Opening Photos on Living Room TV." and state.error is None


def test_node_answers_a_refused_launch_with_the_refusal(node, denied_total, captured_logs):
    """A guest whose permissions restrict this TV: the caller hears the
    refusal, and no "launch failed" error is logged for a permission denial."""
    ha = _RecordingHA()
    restricted = {**GUEST_PERMISSIONS, "restricted_entities": [r"^media_player\.living_room"]}
    _authz, state = node("guest", ha, guest_permissions=restricted)

    assert ha.attempted == []
    assert state.answer == "Sorry, I can't control the media player in guest mode."
    assert state.error == "permission_denied"
    assert denied_total.increments == 1
    assert _events(captured_logs, "tv_launch_failed") == []


def test_node_everywhere_stops_at_the_first_refused_tv(node, admin, denied_total, captured_logs):
    admin.multi_tv_commands = True
    admin.rooms = [ROOM, "den", "office"]
    ha = _RecordingHA()
    restricted = {**GUEST_PERMISSIONS, "restricted_entities": [r"^media_player\.den"]}
    _authz, state = node("guest", ha, query="open Netflix everywhere", guest_permissions=restricted)

    assert ha.attempted == [SELECT_SOURCE], "the first TV opened; nothing was tried after the refusal"
    assert state.answer == "I did part of that, but I can't control the media player in guest mode."
    assert state.error == "permission_denied"
    assert denied_total.increments == 1, "one denial, not one more per remaining TV"
    assert _events(captured_logs, "tv_launch_failed") == []
