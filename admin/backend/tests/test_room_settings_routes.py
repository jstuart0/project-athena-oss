"""The room_lights and speaker_rooms sections through the generic household routes
(owner-only saves, refusals, the 422 shape, audit rows without values, old-admin GET)
and the two Home Assistant pickers.

Production auth (DEV_MODE off) with real Bearer tokens, like the household route tests.
"""
import ast
import json
import pathlib

import httpx
import pytest

from app.auth.oidc import create_access_token
from app.models import AuditLog, SystemSetting
from app.routes import household_settings as route_module
from app.services import ha_access
from shared.config import _clear_cache_for_tests, get_config
from shared.household_settings import SECTIONS
from tests import ha_stack
from tests.ha_stack import TOKEN, FakeHA

URL = "/api/settings/household"
LIGHTS = "/api/settings/room-lights/candidates"
SATELLITES = "/api/settings/speaker-rooms/satellites"
SECRET_ROOM = "Secret Den"
SECRET_LIGHT = "light.secret_lamp"
DEVICE = "a" * 32
ROOMS = {"rooms": [{"room": SECRET_ROOM, "lights": [SECRET_LIGHT]}]}
ENTRIES = {"entries": [{"key": DEVICE, "room": SECRET_ROOM}]}
VALUES = {"room_lights": ROOMS, "speaker_rooms": ENTRIES}


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    route_module._refusal_audited_at.clear()
    _clear_cache_for_tests()
    yield
    route_module._refusal_audited_at.clear()
    _clear_cache_for_tests()


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _service():
    return {"X-Service-Key": get_config().service_api_key}


def _row(db, name):
    db.expire_all()
    return db.query(SystemSetting).filter(SystemSetting.key == SECTIONS[name].key).first()


def put(client, user, section, value, revision=0):
    return client.put(f"{URL}/{section}", json={"value": value, "revision": revision}, headers=_bearer(user))


def audits(db, action):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()


def no_secret(text):
    lowered = str(text).lower()
    assert "secret" not in lowered and DEVICE not in lowered


# -- owner-only saves -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("section", ["room_lights", "speaker_rooms"])
def test_the_owner_round_trips_a_value(client, db, test_user, section):
    resp = put(client, test_user, section, VALUES[section])
    assert resp.status_code == 200 and resp.json()["revision"] == 1
    got = client.get(URL, params={"sections": [section]}, headers=_service()).json()[section]
    assert got["value"] == VALUES[section] and got["revision"] == 1
    assert got["required_permission"] == "manage_infrastructure" and got["writable"] is True
    again = put(client, test_user, section, {next(iter(VALUES[section])): []}, revision=1)
    assert again.status_code == 200 and again.json()["value"] == {next(iter(VALUES[section])): []}


def test_writer_without_owner_role_cannot_save_room_lights(client, db, operator_user):
    resp = put(client, operator_user, "room_lights", ROOMS)
    assert resp.status_code == 403 and resp.json() == {"detail": {"error": "insufficient_role"}}
    assert _row(db, "room_lights") is None


def test_writer_without_owner_role_cannot_reset_speaker_rooms(client, db, test_user, operator_user):
    assert put(client, test_user, "speaker_rooms", ENTRIES).status_code == 200
    before = _row(db, "speaker_rooms").value
    resp = put(client, operator_user, "speaker_rooms", {"entries": None}, revision=1)
    assert resp.status_code == 403
    assert _row(db, "speaker_rooms").value == before


def test_the_403_comes_before_the_revision_and_validation_checks(client, operator_user, test_user):
    put(client, test_user, "room_lights", ROOMS)
    assert put(client, operator_user, "room_lights", ROOMS, revision=0).status_code == 403  # stale, still 403
    assert put(client, operator_user, "room_lights", {"rooms": [{"room": "x", "lights": ["switch.a"]}]}, revision=9).status_code == 403


def test_refused_save_is_audited_without_values(client, db, operator_user):
    for _ in range(3):
        put(client, operator_user, "room_lights", ROOMS)
    (row,) = audits(db, "household_settings_refused")
    assert row.user_id == operator_user.id and row.success is False and row.error_message == "insufficient_role"
    assert row.new_value == {"section": "room_lights", "reason": "insufficient_role"}
    no_secret(json.dumps(row.new_value))
    put(client, operator_user, "speaker_rooms", ENTRIES)
    assert len(audits(db, "household_settings_refused")) == 2  # another section, another row


def test_a_service_key_put_is_refused(client, db):
    resp = client.put(f"{URL}/room_lights", json={"value": ROOMS, "revision": 0}, headers=_service())
    assert resp.status_code == 401 and _row(db, "room_lights") is None


def test_an_owners_409_and_422_are_audited_once_per_minute(client, db, test_user):
    put(client, test_user, "room_lights", ROOMS)
    for _ in range(3):
        assert put(client, test_user, "room_lights", ROOMS, revision=0).status_code == 409
    for _ in range(3):
        assert put(client, test_user, "room_lights", {"rooms": [{"room": "x", "lights": ["lock.a"]}]}, revision=1).status_code == 422
    rows = audits(db, "household_settings_refused")
    assert [r.new_value["reason"] for r in rows] == ["stale_revision", "validation_failed"]
    for row in rows:
        no_secret(json.dumps(row.new_value))


def test_a_stale_revision_is_409(client, test_user):
    put(client, test_user, "speaker_rooms", ENTRIES)
    assert put(client, test_user, "speaker_rooms", ENTRIES, revision=0).status_code == 409


def test_the_success_audit_row_contains_none_of_the_submitted_strings(client, db, test_user):
    put(client, test_user, "room_lights", ROOMS)
    put(client, test_user, "speaker_rooms", ENTRIES)
    rows = audits(db, "household_settings_update")
    assert len(rows) == 2
    for row in rows:
        no_secret(json.dumps([row.old_value, row.new_value]))
    assert rows[0].new_value == {"section": "room_lights", "changed_fields": ["rooms"], "revision": 1}


# -- the 422 shape ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("section,value,kind,loc", [
    ("room_lights", {"rooms": [{"room": "House", "lights": ["light.a"]}]}, "reserved_room", ["rooms", 0, "room"]),
    ("room_lights", {"rooms": [{"room": "Den", "lights": ["switch.secret"]}]}, "not_a_light", ["rooms", 0, "lights", 0]),
    ("room_lights", {"rooms": [{"room": "Den", "lights": ["light.a"]}, {"room": "den", "lights": ["light.b"]}]}, "duplicate_room", ["rooms", 1, "room"]),
    ("speaker_rooms", {"entries": [{"key": "SECRET", "room": "Den"}]}, "bad_satellite_key", ["entries", 0, "key"]),
    ("speaker_rooms", {"entries": [{"key": "assist_satellite.a", "room": "Den"}, {"key": "assist_satellite.b", "room": "DEN"}]},
     "duplicate_satellite_room", ["entries", 1, "room"]),
])
def test_a_422_carries_loc_and_type_only(client, db, test_user, section, value, kind, loc):
    resp = put(client, test_user, section, value)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert {"loc": loc, "type": kind} in detail and all(set(d) == {"loc", "type"} for d in detail)
    no_secret(resp.text)
    assert _row(db, section) is None


@pytest.mark.parametrize("raw", [b"{not json", b'{"value": ', b"[1,2]"])
def test_a_malformed_body_422_never_echoes_input(client, test_user, raw):
    headers = {**_bearer(test_user), "Content-Type": "application/json"}
    resp = client.put(f"{URL}/room_lights", content=raw, headers=headers)
    assert resp.status_code == 422
    assert all(set(d) <= {"loc", "type"} for d in resp.json()["detail"])
    assert "not json" not in resp.text


# -- an old admin and the GET -------------------------------------------------------------------------------------


def test_unknown_section_name_is_ignored(client, test_user):
    put(client, test_user, "room_lights", ROOMS)
    resp = client.get(URL, params=[("sections", "room_lights"), ("sections", "bogus")], headers=_service())
    assert resp.status_code == 200 and list(resp.json()) == ["room_lights"]


# -- the pickers --------------------------------------------------------------------------------------------------


def states_fake(states):
    class Fake(FakeHA):
        def _rest(self, request, address):
            response = super()._rest(request, address)
            if response.status_code == 200 and request.url.path == "/api/states":
                return httpx.Response(200, json=states, request=request)
            return response

    return Fake()


STATES = [
    {"entity_id": "light.zeta", "attributes": {"friendly_name": "Zeta lamp"}},
    {"entity_id": "light.kitchen_group", "attributes": {"friendly_name": "Kitchen", "entity_id": ["light.k1", "light.k2"]}},
    {"entity_id": "light.hostile", "attributes": {"friendly_name": "<img src=x onerror=alert(1)>" + "x" * 200}},
    {"entity_id": "light.noname", "attributes": {}},
    {"entity_id": "switch.fan", "attributes": {"friendly_name": "Fan"}},
    {"entity_id": "lock.front", "attributes": {"friendly_name": "Front"}},
    {"entity_id": "assist_satellite.den_sat", "attributes": {"friendly_name": "Den speaker"}},
    {"entity_id": "assist_satellite.attic", "attributes": {"friendly_name": "Attic speaker"}},
    "junk", {"entity_id": 5},
]


def test_the_light_picker_filters_by_domain_and_marks_groups(client, operator_user):
    ha_stack.build(states_fake(STATES), ha_stack.constant_source())
    resp = client.get(LIGHTS, headers=_bearer(operator_user))
    assert resp.status_code == 200
    items = resp.json()
    assert [i["entity_id"] for i in items] == ["light.hostile", "light.kitchen_group", "light.noname", "light.zeta"]
    assert {i["entity_id"]: i["group"] for i in items} == {
        "light.hostile": False, "light.kitchen_group": True, "light.noname": False, "light.zeta": False,
    }
    assert all(len(i["friendly_name"]) <= 100 for i in items)
    assert next(i for i in items if i["entity_id"] == "light.noname")["friendly_name"] == "light.noname"
    assert TOKEN not in resp.text


def test_the_satellite_picker_lists_only_satellites(client, operator_user):
    ha_stack.build(states_fake(STATES), ha_stack.constant_source())
    items = client.get(SATELLITES, headers=_bearer(operator_user)).json()
    assert items == [
        {"entity_id": "assist_satellite.attic", "friendly_name": "Attic speaker"},
        {"entity_id": "assist_satellite.den_sat", "friendly_name": "Den speaker"},
    ]


@pytest.mark.parametrize("url", [LIGHTS, SATELLITES])
def test_the_pickers_refuse_a_service_key_and_a_user_without_read(client, viewer_user, url):
    ha_stack.build(states_fake(STATES), ha_stack.constant_source())
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_service()).status_code == 401
    assert client.get(url, headers=_bearer(viewer_user)).status_code == 403


class ConnectFailureHA(FakeHA):
    def _rest(self, request, address):
        raise httpx.ConnectError("cannot reach ha-fixture.invalid secret-detail", request=request)


@pytest.mark.parametrize("url", [LIGHTS, SATELLITES])
def test_a_home_assistant_failure_is_a_closed_phrase(client, operator_user, url):
    ha_stack.build(ConnectFailureHA(), ha_stack.constant_source())
    resp = client.get(url, headers=_bearer(operator_user))
    assert resp.status_code != 200
    assert resp.json()["detail"] in {ha_access.CANT_CONNECT, ha_access.FAILED}
    assert "secret-detail" not in resp.text and "ha-fixture" not in resp.text


@pytest.mark.parametrize("url", [LIGHTS, SATELLITES])
def test_home_assistant_not_set_up_is_a_closed_phrase(client, operator_user, url):
    fake = FakeHA()
    ha_stack.build(fake, ha_stack.absent_source())
    resp = client.get(url, headers=_bearer(operator_user))
    assert resp.status_code != 200 and resp.json()["detail"] == ha_access.NOT_SET_UP
    assert fake.rest_sent == []


def test_the_picker_module_reaches_home_assistant_only_through_the_channel_and_logs_counts():
    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "routes" / "room_settings.py"
    tree = ast.parse(path.read_text())
    calling = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        for c in ast.walk(n)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "channel"
        and getattr(c.func.value, "id", "") == "ha_access"
    }
    assert calling == {"_states"}
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"httpx", "requests", "aiohttp", "websockets"}
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in ("info", "warning", "error"):
            assert {k.arg for k in call.keywords} <= {"count"}
