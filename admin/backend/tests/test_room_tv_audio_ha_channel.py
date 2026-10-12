"""Every Home Assistant call in the room TV and room audio routes goes through the
shared channel, and every failure reaches the browser as a closed phrase.

The set of HA-calling route functions is read from the source (AST) and pinned, so
a new route that reaches Home Assistant fails here until it has a case below.
"""
import ast
import pathlib

import httpx
import pytest

from app.models import RoomAudioConfig, RoomTVConfig
from app.services import ha_access
from tests import ha_stack
from tests.ha_stack import TOKEN, FakeHA

ROUTES = pathlib.Path(__file__).resolve().parents[1] / "app" / "routes"
PINNED = {
    "room_tv.py": {
        "discover_apple_tvs", "sync_apps_from_device", "launch_app_on_tv", "power_control",
        "send_remote_command", "get_tv_state",
    },
    "room_audio.py": {"discover_media_players", "test_room_audio"},
}
PLAYER = {"entity_id": "media_player.lr_tv", "state": "idle", "attributes": {"friendly_name": "LR", "source_list": [f"App{i}" for i in range(12)]}}


class RoomHA(FakeHA):
    """Answers state reads with a media player and service calls with an empty list."""

    def _rest(self, request, address):
        response = super()._rest(request, address)
        if response.status_code != 200 or request.method != "GET":
            return response
        path = request.url.path
        body = [PLAYER] if path == "/api/states" else PLAYER
        return httpx.Response(200, json=body, request=request)


# (function, module, method, url, path the call must reach)
CASES = [
    ("discover_apple_tvs", "room_tv.py", "GET", "/api/room-tv/discover", "/api/states"),
    ("sync_apps_from_device", "room_tv.py", "GET", "/api/room-tv/apps/sync/media_player.lr_tv", "/api/states/media_player.lr_tv"),
    ("launch_app_on_tv", "room_tv.py", "POST", "/api/room-tv/control/lr/launch?app_name=App1&send_select=false", "/api/services/media_player/select_source"),
    ("power_control", "room_tv.py", "POST", "/api/room-tv/control/lr/power?action=on", "/api/services/media_player/turn_on"),
    ("send_remote_command", "room_tv.py", "POST", "/api/room-tv/control/lr/remote?command=up", "/api/services/remote/send_command"),
    ("get_tv_state", "room_tv.py", "GET", "/api/room-tv/control/lr/state", "/api/states/media_player.lr_tv"),
    ("discover_media_players", "room_audio.py", "GET", "/api/room-audio/discover/entities?filter_type=all", "/api/states"),
    ("test_room_audio", "room_audio.py", "POST", "/api/room-audio/1/test", "/api/services/tts/speak"),
]
IDS = [case[0] for case in CASES]


@pytest.fixture(autouse=True)
def rooms(db):
    db.add(RoomTVConfig(room_name="lr", display_name="LR", media_player_entity_id="media_player.lr_tv", remote_entity_id="remote.lr_tv", enabled=True))
    db.add(RoomAudioConfig(
        room_name="lr", display_name="LR", speaker_type="stereo_pair",
        primary_entity_id="media_player.lr_left", secondary_entity_id="media_player.lr_right",
    ))
    db.commit()


def functions_calling_the_channel(filename):
    tree = ast.parse((ROUTES / filename).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "channel"
                        and getattr(call.func.value, "id", "") == "ha_access"):
                    found.add(node.name)
    return found


@pytest.mark.parametrize("filename", sorted(PINNED))
def test_the_set_of_home_assistant_calling_routes_is_pinned(filename):
    assert functions_calling_the_channel(filename) == PINNED[filename]
    assert {c[0] for c in CASES if c[1] == filename} == PINNED[filename], "every pinned route has a case"


@pytest.mark.parametrize("filename", sorted(PINNED))
def test_the_route_files_hold_no_credentials_or_clients_of_their_own(filename):
    tree = ast.parse((ROUTES / filename).read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"httpx", "requests", "aiohttp", "websockets"}
    source = (ROUTES / filename).read_text()
    for word in ("HA_URL", "HA_TOKEN", "Bearer", "localhost:8123", "getenv", "environ"):
        assert word not in source, word


@pytest.mark.parametrize("name,filename,method,url,reaches", CASES, ids=IDS)
def test_the_call_goes_through_the_channel_and_succeeds(owner_client, name, filename, method, url, reaches):
    fake = RoomHA()
    ha_stack.build(fake, ha_stack.constant_source())
    response = owner_client.request(method, url)
    assert response.status_code == 200, response.text
    assert any(path == reaches for _, _, path in fake.rest_sent), fake.rest_sent
    assert TOKEN not in response.text


@pytest.mark.parametrize("name,filename,method,url,reaches", CASES, ids=IDS)
def test_a_rejected_token_is_a_closed_phrase_and_the_stop_holds_for_the_next_call(owner_client, name, filename, method, url, reaches):
    fake = RoomHA(valid=())
    ha_stack.build(fake, ha_stack.constant_source())
    first = owner_client.request(method, url)
    second = owner_client.request(method, url)
    for response in (first, second):
        assert response.status_code in (200, 502)
        assert TOKEN not in response.text and "ha-fixture" not in response.text
        if response.status_code == 502:
            assert response.json()["detail"] == ha_access.TOKEN_REJECTED
    assert fake.failed_total == 1, "one failed sign-in in all"
    assert fake.rest_sent == []


@pytest.mark.parametrize("name,filename,method,url,reaches", CASES, ids=IDS)
def test_home_assistant_not_set_up_sends_nothing(owner_client, name, filename, method, url, reaches):
    fake = RoomHA()
    ha_stack.build(fake, ha_stack.absent_source())
    response = owner_client.request(method, url)
    assert fake.rest_sent == [] and fake.ws_connects == []
    if response.status_code != 200:
        assert response.json()["detail"] == ha_access.NOT_SET_UP


def test_the_audio_test_stops_after_a_rejection_and_reports_one_phrase(owner_client):
    fake = RoomHA(valid=())
    ha_stack.build(fake, ha_stack.constant_source())
    response = owner_client.post("/api/room-audio/1/test")
    body = response.json()
    errors = [r["error"] for r in body["results"]] if "results" in body else []
    assert errors and set(errors) == {ha_access.TOKEN_REJECTED}
    assert len(errors) == 2 and fake.failed_total == 1


def test_the_audio_test_sends_one_announcement_per_speaker(owner_client):
    fake = RoomHA()
    ha_stack.build(fake, ha_stack.constant_source())
    assert owner_client.post("/api/room-audio/1/test").status_code == 200
    assert [p for _, _, p in fake.rest_sent].count("/api/services/tts/speak") == 2


@pytest.mark.parametrize("entity", ["..%2F..%2Fconfig", "%2e%2e/config", "media_player.x%2F..%2F..%2Fconfig"])
def test_an_entity_path_that_escapes_the_api_is_a_400_before_any_send(owner_client, entity):
    fake = RoomHA()
    ha_stack.build(fake, ha_stack.constant_source())
    response = owner_client.get(f"/api/room-tv/apps/sync/{entity}")
    assert response.status_code == 400 and response.json()["detail"] == ha_access.BAD_REQUEST
    assert fake.rest_sent == [] and fake.ws_connects == []


def test_a_group_of_three_with_a_rejected_token_reports_three_closed_errors_and_sends_nothing(owner_client, db):
    config = db.query(RoomAudioConfig).one()
    config.speaker_type = "group"
    config.group_entity_ids = ["media_player.a", "media_player.b"]
    db.commit()
    fake = RoomHA(valid=())
    ha_stack.build(fake, ha_stack.constant_source())
    body = owner_client.post("/api/room-audio/1/test").json()
    errors = [r["error"] for r in body["results"]]
    assert len(errors) == 3 and set(errors) == {ha_access.TOKEN_REJECTED}
    assert fake.rest_sent == [] and fake.failed_total == 1


def test_a_401_on_the_first_announcement_sends_once_and_the_rest_report_the_same_closed_text(owner_client, db):
    config = db.query(RoomAudioConfig).one()
    config.speaker_type = "group"
    config.group_entity_ids = ["media_player.a", "media_player.b"]
    db.commit()
    fake = RoomHA(non_admin=(TOKEN,), admin_only_paths=("/api/services/tts/speak",))
    ha_stack.build(fake, ha_stack.constant_source())
    body = owner_client.post("/api/room-audio/1/test").json()
    errors = [r["error"] for r in body["results"]]
    assert len(errors) == 3 and len(set(errors)) == 1 and errors[0] in {ha_access.REFUSED, ha_access.TOKEN_REJECTED}
    assert [p for _, _, p in fake.rest_sent].count("/api/services/tts/speak") == 1, "one send, then nothing"
    assert fake.failed_total == 1


class ConnectFailureHA(RoomHA):
    """The proof passes; every later request fails on the wire with text that names the host."""

    def _rest(self, request, address):
        raise httpx.ConnectError(f"cannot reach ha-fixture.invalid secret-detail", request=request)


def test_a_wire_failure_on_an_announcement_is_a_closed_per_item_phrase(owner_client):
    fake = ConnectFailureHA()
    ha_stack.build(fake, ha_stack.constant_source())
    response = owner_client.post("/api/room-audio/1/test")
    text = response.text
    assert "secret-detail" not in text and "ha-fixture" not in text
    errors = [r.get("error") for r in response.json().get("results", [])]
    assert errors and set(errors) <= {ha_access.CANT_CONNECT, ha_access.FAILED}
