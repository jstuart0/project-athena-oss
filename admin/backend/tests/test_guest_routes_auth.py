"""The guest-data routes the hardening gated: the full credential matrix on
every one of them, and the behaviour behind the gate.

Every test turns DEV_MODE's auth bypass off and authenticates with real
Bearer JWTs and a real user API key. The factories call get_current_user
themselves, so dependency_overrides can't stand in for a user here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from app.auth.oidc import create_access_token
from app.models import (
    CalendarEvent, EmergingIntent, Guest, LLMBackend, PipelineEvent, RoomGroup,
    RoomGroupAlias, SMSCostTracking, UserSession, VoiceAutomation,
)
from shared.config import _clear_cache_for_tests, get_config

from tests.test_route_auth_population import GATED

VA = "/api/voice-automations"
PIPELINES = {"result": {"pipelines": [
    {"id": "p-simple", "name": "Ollama", "conversation_engine": "conversation.ollama_conversation"},
]}}


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    # Both the oidc module the served app holds and the live one (another
    # test file evicts and re-imports app.* mid-suite).
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    # The HA call behind POST /mode/set, stubbed in the module the served
    # route was defined in.
    from shared.route_walk import iter_api_routes
    from tests.conftest import app

    (mode_set,) = [w.route.endpoint for w in iter_api_routes(app) if w.path == "/api/ha-pipelines/mode/set"]
    monkeypatch.setitem(mode_set.__globals__, "ha_websocket_command", AsyncMock(return_value=PIPELINES))
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _now():
    return datetime.now(timezone.utc)


def _event(db, *, external_id, checkin, checkout, status="confirmed", deleted_at=None, **kw):
    ev = CalendarEvent(
        external_id=external_id, checkin=checkin, checkout=checkout, status=status,
        deleted_at=deleted_at, source="manual", **kw,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _automation(db, **kw):
    defaults = dict(name="Automation", owner_type="owner", trigger_config={"type": "time", "time": "18:00"},
                    actions_config=[{"service": "light.turn_on", "entity_id": "light.porch"}], status="active")
    defaults.update(kw)
    row = VoiceAutomation(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed(db):
    now = _now()
    ev = _event(db, external_id="ev-active", checkin=now - timedelta(days=1), checkout=now + timedelta(days=1),
                guest_name="Stay Guest")
    guest = Guest(calendar_event_id=ev.id, name="Ana", is_primary=True)
    db.add(guest)
    db.commit()
    db.refresh(guest)
    db.add(UserSession(session_id="sess-1", guest_id=guest.id, device_id="dev-1"))
    group = RoomGroup(name="first_floor", display_name="First Floor")
    db.add(group)
    db.commit()
    db.refresh(group)
    db.add(RoomGroupAlias(room_group_id=group.id, alias="downstairs"))
    db.add(LLMBackend(model_name="m1", backend_type="ollama", endpoint_url="http://ollama.example:11434"))
    ei = EmergingIntent(canonical_name="ask_x", sample_queries=["a"], status="discovered")
    db.add(ei)
    db.add(PipelineEvent(session_id="pe-sess", event_type="stt_complete", event_data={"text": "hello"},
                         timestamp=datetime.utcnow()))
    # log-send's first send of a month crashes on a fresh tracking row
    # (pre-existing; unrelated to the gate), so the month's row exists.
    db.add(SMSCostTracking(month=date.today().replace(day=1), message_count=0, segment_count=0,
                           incoming_count=0, outgoing_count=0, estimated_cost_cents=0,
                           outgoing_sms_cents=0, incoming_sms_cents=0))
    db.commit()
    db.refresh(ei)
    owner_row = _automation(db, name="Owner auto")
    archived_row = _automation(db, name="Old auto", status="archived")
    _automation(db, name="Guest auto", owner_type="guest", guest_name="Ana", guest_session_id="gs-1")
    _automation(db, name="Guest old", owner_type="guest", guest_name="Ana", status="archived")
    return {"event": ev.id, "guest": guest.id, "intent": ei.id, "owner_row": owner_row.id,
            "archived_row": archived_row.id}


def _request(op, ids):
    """(method, url, kwargs) for one gated route on seeded data."""
    method, path = op
    body = None
    url = path
    subs = {
        "{guest_id}": str(ids["guest"]), "{device_id}": "dev-1", "{session_id}": "sess-1",
        "{query_term}": "downstairs", "{model_name}": "m1", "{filename}": "athena.log",
        "{guest_name}": "Ana", "{intent_id}": str(ids["intent"]),
    }
    if path == f"{VA}/{{automation_id}}/restore":
        subs["{automation_id}"] = str(ids["archived_row"])
    else:
        subs["{automation_id}"] = str(ids["owner_row"])
    if path.startswith("/api/pipeline-events/"):
        subs["{session_id}"] = "pe-sess"
    if path == f"{VA}/guest-departure/{{session_id}}":
        subs["{session_id}"] = "gs-1"
    for k, v in subs.items():
        url = url.replace(k, v)
    params = None
    if op == ("GET", "/api/guests/by-events"):
        params = {"event_ids": str(ids["event"])}
    elif op == ("POST", "/api/guests/current/add"):
        body = {"name": "New Guest"}
    elif op == ("POST", "/api/user-sessions"):
        body = {"session_id": "sess-new", "guest_id": ids["guest"], "device_id": "dev-2"}
    elif op == ("POST", "/api/sms/internal/log-send"):
        params = {"phone_number": "+15550100000", "content": "hi", "status": "sent"}
    elif op == ("POST", "/api/ha-pipelines/mode/set"):
        body = {"mode": "simple"}
    elif op == ("POST", VA):
        body = {"name": "New auto", "owner_type": "owner", "trigger_config": {"type": "time", "time": "18:00"},
                "actions_config": [{"service": "light.turn_on", "entity_id": "light.porch"}]}
    elif op in {("POST", f"{VA}/archive-guest"), ("POST", f"{VA}/restore-guest")}:
        body = {"guest_name": "Ana"}
    elif op == ("POST", "/api/pipeline-events/emit"):
        params = {"event_type": "test", "session_id": "pe-2"}
    elif op == ("POST", "/api/internal/emerging-intents"):
        body = {"canonical_name": "new_intent", "sample_queries": ["q"]}
    elif op == ("POST", "/api/internal/emerging-intents/{intent_id}/increment"):
        body = {"sample_query": "x"}
    elif op == ("POST", "/api/internal/intent-metrics"):
        body = {"intent": "weather", "confidence": 0.9}
    kwargs = {}
    if params:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body
    return method, url, kwargs


def _call(client, op, ids, headers):
    method, url, kwargs = _request(op, ids)
    if op[1] == VA or op[1].startswith(VA + "/"):
        headers = {**headers, "X-Athena-Caller-Mode": "owner"}
    return client.request(method, url, headers=headers, **kwargs)


# Exact success code per route on seeded data (200 unless listed).
_SUCCESS: dict = {}


def _creds(case, *, owner, viewer, operator, api_key, monkeypatch):
    svc = get_config().service_api_key
    if case == "none":
        return {}
    if case == "svc_correct":
        return {"X-Service-Key": svc}
    if case == "svc_wrong":
        return {"X-Service-Key": "wrong-key"}
    if case == "svc_unset":
        monkeypatch.setenv("SERVICE_API_KEY", "")
        _clear_cache_for_tests()
        return {"X-Service-Key": "anything"}
    if case == "owner_svc_correct":
        return {**_bearer(owner), "X-Service-Key": svc}
    if case == "owner_svc_wrong":
        return {**_bearer(owner), "X-Service-Key": "wrong-key"}
    if case == "owner_svc_unset":
        monkeypatch.setenv("SERVICE_API_KEY", "")
        _clear_cache_for_tests()
        return {**_bearer(owner), "X-Service-Key": "anything"}
    if case == "viewer":
        return _bearer(viewer)
    if case == "operator":
        return _bearer(operator)
    if case == "owner":
        return _bearer(owner)
    if case == "owner_api_key":
        return {"X-API-Key": api_key}
    raise AssertionError(case)


S = "success"
MATRIX = {
    #                   user   service_or_user  service
    "none":              (401, 401, 422),
    "svc_correct":       (401, S, S),
    "svc_wrong":         (401, 401, 401),
    "svc_unset":         (401, 503, 503),
    "owner_svc_correct": (401, S, S),
    "owner_svc_wrong":   (401, 401, 401),
    "owner_svc_unset":   (401, 503, 503),
    "viewer":            (403, 403, 422),
    "operator":          (S, S, 422),
    "owner":             (S, S, 422),
    "owner_api_key":     (S, S, 422),
}
_KIND_COLUMN = {"user": 0, "service_or_user": 1, "service": 2}
GATED_ROUTES = sorted(GATED)


def test_matrix_population():
    assert len(GATED_ROUTES) == 46
    assert ("POST", "/api/guests/current/add") in GATED_ROUTES
    assert ("GET", "/api/internal/emerging-intents") in GATED_ROUTES


@pytest.mark.parametrize("route", GATED_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
@pytest.mark.parametrize("case", sorted(MATRIX))
def test_credential_matrix(client, db, test_user, viewer_user, operator_user, test_api_key,
                           monkeypatch, route, case):
    assert get_config().service_api_key, "conftest sets SERVICE_API_KEY"
    ids = _seed(db)
    kind, _permission = GATED[route]
    expected = MATRIX[case][_KIND_COLUMN[kind]]
    if expected == S:
        expected = _SUCCESS.get(route, 200)
    headers = _creds(case, owner=test_user, viewer=viewer_user, operator=operator_user,
                     api_key=test_api_key[1], monkeypatch=monkeypatch)
    resp = _call(client, route, ids, headers)
    assert resp.status_code == expected, (route, case, resp.status_code, resp.text[:300])


# ---------------------------------------------------------------------------
# The hard delete stays user-only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("who,expected", [("svc_correct", 401), ("operator", 403), ("owner", 200)])
def test_hard_delete_is_user_only(client, db, test_user, operator_user, monkeypatch, who, expected):
    row = _automation(db, name="Deletable")
    headers = {"svc_correct": {"X-Service-Key": get_config().service_api_key, "X-Athena-Caller-Mode": "owner"},
               "operator": _bearer(operator_user), "owner": _bearer(test_user)}[who]
    resp = client.delete(f"{VA}/{row.id}", headers=headers)
    assert resp.status_code == expected, resp.text
    db.expire_all()
    exists = db.query(VoiceAutomation).filter(VoiceAutomation.id == row.id).first() is not None
    assert exists == (expected != 200)


# ---------------------------------------------------------------------------
# Current stay: confirmed, not deleted, checkout ASC
# ---------------------------------------------------------------------------

def _guest(db, event_id, name):
    g = Guest(calendar_event_id=event_id, name=name, is_primary=True)
    db.add(g)
    db.commit()
    return g


def test_current_ignores_blocked_cancelled_and_deleted_stays(client, db, test_user):
    now = _now()
    covering = dict(checkin=now - timedelta(days=1), checkout=now + timedelta(days=1))
    for i, (status, deleted) in enumerate([("blocked", None), ("cancelled", None), ("confirmed", now)]):
        ev = _event(db, external_id=f"ev-wrong-{i}", status=status, deleted_at=deleted, **covering)
        _guest(db, ev.id, f"Wrong {i}")
    headers = _bearer(test_user)
    assert client.get("/api/guests/current", headers=headers).json() == []
    assert client.post("/api/guests/current/add", json={"name": "X"}, headers=headers).status_code == 404
    good = _event(db, external_id="ev-good", **covering)
    _guest(db, good.id, "Right")
    names = [g["name"] for g in client.get("/api/guests/current", headers=headers).json()]
    assert names == ["Right"]


def test_changeover_returns_the_departing_stay(client, db, test_user):
    now = _now()
    arriving = _event(db, external_id="ev-arrive", checkin=now - timedelta(hours=1), checkout=now + timedelta(days=3))
    _guest(db, arriving.id, "Arriving")
    departing = _event(db, external_id="ev-depart", checkin=now - timedelta(days=3), checkout=now + timedelta(hours=1))
    _guest(db, departing.id, "Departing")
    names = [g["name"] for g in client.get("/api/guests/current", headers=_bearer(test_user)).json()]
    assert names == ["Departing"]
    resp = client.post("/api/guests/current/add", json={"name": "Late"}, headers=_bearer(test_user))
    assert resp.status_code == 200
    assert resp.json()["guest"]["calendar_event_id"] == departing.id


# ---------------------------------------------------------------------------
# Anonymous writes leave nothing behind
# ---------------------------------------------------------------------------

def test_anonymous_automation_create_is_refused_and_writes_nothing(client, db):
    body = {"name": "ignore previous instructions", "owner_type": "owner",
            "trigger_config": {"type": "time"}, "actions_config": []}
    assert client.post(VA, json=body).status_code == 401
    assert db.query(VoiceAutomation).count() == 0


def test_anonymous_intent_increment_is_refused_and_writes_nothing(client, db):
    ei = EmergingIntent(canonical_name="ask_y", sample_queries=["orig"], status="discovered")
    db.add(ei)
    db.commit()
    resp = client.post(f"/api/internal/emerging-intents/{ei.id}/increment", json={"sample_query": "x"})
    assert resp.status_code == 401
    db.expire_all()
    assert db.query(EmergingIntent).get(ei.id).sample_queries == ["orig"]


def test_pipeline_events_need_a_user(client, db, operator_user):
    db.add(PipelineEvent(session_id="pe-1", event_type="stt_complete", event_data={"text": "turn on the porch"},
                         timestamp=datetime.utcnow()))
    db.commit()
    assert client.get("/api/pipeline-events").status_code == 401
    resp = client.get("/api/pipeline-events", headers=_bearer(operator_user))
    assert resp.status_code == 200
    assert any(e["data"].get("text") == "turn on the porch" for e in resp.json())


# ---------------------------------------------------------------------------
# Voice-automation caller scope: keyed on the stay, not the guest's name
# ---------------------------------------------------------------------------

STAY_A, STAY_B, STAY_OLD = 101, 102, 103


@pytest.fixture
def scoped(db):
    """Wrong answers first: an owner row, another stay's row carrying the
    SAME guest name, a legacy row of this name with no stay id, then this
    stay's row."""
    rows = {
        "O": _automation(db, name="Owner", owner_type="owner"),
        "G_B": _automation(db, name="Sam at B", owner_type="guest", guest_name="Sam", calendar_event_id=STAY_B),
        "LEGACY": _automation(db, name="Sam legacy", owner_type="guest", guest_name="Sam", calendar_event_id=None),
        "G_A": _automation(db, name="Sam at A", owner_type="guest", guest_name="Sam", calendar_event_id=STAY_A),
    }
    return {k: v.id for k, v in rows.items()}


def _svc(mode=None, name=None, stay=None):
    headers = {"X-Service-Key": get_config().service_api_key}
    if mode:
        headers["X-Athena-Caller-Mode"] = mode
    if name is not None:
        headers["X-Athena-Guest-Name"] = quote(name, safe="")
    if stay is not None:
        headers["X-Athena-Guest-Stay"] = str(stay)
    return headers


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return sorted(r["id"] for r in resp.json())


def test_guest_scope_lists_only_its_own_stay(client, scoped):
    guest = _svc("guest", "Sam", STAY_A)
    assert _ids(client.get(VA, headers=guest)) == [scoped["G_A"]]
    conflicting = client.get(VA, params={"owner_type": "owner", "guest_name": "Sam"}, headers=guest)
    assert _ids(conflicting) == [scoped["G_A"]]


def test_two_stays_sharing_a_name_see_only_their_own_rows(client, scoped):
    assert _ids(client.get(VA, headers=_svc("guest", "Sam", STAY_B))) == [scoped["G_B"]]
    assert _ids(client.get(VA, headers=_svc("guest", "Sam", STAY_OLD))) == []


def test_legacy_rows_without_a_stay_are_never_visible_to_a_guest(client, db, scoped):
    for stay in (STAY_A, STAY_B, STAY_OLD):
        guest = _svc("guest", "Sam", stay)
        assert scoped["LEGACY"] not in _ids(client.get(VA, params={"include_archived": "true"}, headers=guest))
        for action in ("archive", "restore", "triggered"):
            assert client.post(f"{VA}/{scoped['LEGACY']}/{action}", headers=guest).status_code == 404
        by_name = client.get(f"{VA}/internal/by-guest-name/Sam", params={"include_archived": "true"}, headers=guest)
        assert scoped["LEGACY"] not in _ids(by_name)
    db.expire_all()
    assert db.query(VoiceAutomation).get(scoped["LEGACY"]).status == "active"


def test_owner_scope_lists_every_row(client, scoped):
    assert _ids(client.get(VA, headers=_svc("owner"))) == sorted(scoped.values())


@pytest.mark.parametrize("action", ["archive", "triggered"])
def test_guest_scope_by_id_actions_reach_only_its_own_stay(client, db, scoped, action):
    guest = _svc("guest", "Sam", STAY_A)
    for other in ("O", "G_B", "LEGACY"):
        assert client.post(f"{VA}/{scoped[other]}/{action}", headers=guest).status_code == 404
    assert client.post(f"{VA}/{scoped['G_A']}/{action}", headers=guest).status_code == 200
    db.expire_all()
    if action == "archive":
        for other in ("O", "G_B", "LEGACY"):
            assert db.query(VoiceAutomation).get(scoped[other]).status == "active"


def test_guest_scope_restore_reaches_only_its_own_stay(client, db, scoped):
    for key in scoped:
        db.query(VoiceAutomation).get(scoped[key]).status = "archived"
    db.commit()
    guest = _svc("guest", "Sam", STAY_A)
    for other in ("O", "G_B", "LEGACY"):
        assert client.post(f"{VA}/{scoped[other]}/restore", headers=guest).status_code == 404
    assert client.post(f"{VA}/{scoped['G_A']}/restore", headers=guest).status_code == 200


def test_guest_scope_name_keyed_reads_are_stay_scoped(client, db, scoped):
    guest = _svc("guest", "Sam", STAY_A)
    assert client.get(f"{VA}/internal/by-guest-name/Bo", headers=guest).status_code == 404
    assert client.get(f"{VA}/guest/Bo/archived", headers=guest).status_code == 404
    assert _ids(client.get(f"{VA}/internal/by-guest-name/Sam", headers=guest)) == [scoped["G_A"]]
    assert client.post(f"{VA}/guest-departure/s-ana", headers=guest).status_code == 404


@pytest.mark.parametrize("route", ["archive-guest", "restore-guest"])
def test_name_based_bulk_changes_are_owner_only(client, db, scoped, route):
    guest = _svc("guest", "Sam", STAY_A)
    assert client.post(f"{VA}/{route}", json={"guest_name": "Sam"}, headers=guest).status_code == 403
    db.expire_all()
    assert {r.status for r in db.query(VoiceAutomation).all()} == {"active"}
    owner = client.post(f"{VA}/{route}", json={"guest_name": "Sam"}, headers=_svc("owner"))
    assert owner.status_code == 200, owner.text


def test_guest_scope_create_is_bound_to_its_stay(client, db):
    guest = _svc("guest", "Sam", STAY_A)
    base = {"name": "n", "trigger_config": {"type": "time"}, "actions_config": []}
    assert client.post(VA, json={**base, "owner_type": "owner"}, headers=guest).status_code == 400
    assert client.post(VA, json={**base, "owner_type": "guest", "guest_name": "Bo"}, headers=guest).status_code == 400
    wrong_stay = {**base, "owner_type": "guest", "guest_name": "Sam", "calendar_event_id": STAY_B}
    assert client.post(VA, json=wrong_stay, headers=guest).status_code == 400
    assert db.query(VoiceAutomation).count() == 0
    ok = client.post(VA, json={**base, "owner_type": "guest", "guest_name": "Sam"}, headers=guest)
    assert ok.status_code == 200, ok.text
    assert ok.json()["calendar_event_id"] == STAY_A
    db.expire_all()
    assert db.query(VoiceAutomation).one().calendar_event_id == STAY_A


@pytest.mark.parametrize("name", ["Airbnb Guest", "VRBO Guest", "Guest", " airbnb guest "])
def test_feed_placeholder_names_are_refused(client, db, name):
    """Defence in depth: every Airbnb booking is 'Airbnb Guest', so two
    stays sharing that name must never reach each other's rows. A guest scope
    with a placeholder name is refused outright."""
    a = _automation(db, name="a", owner_type="guest", guest_name=name.strip(), calendar_event_id=STAY_A)
    guest = _svc("guest", name, STAY_A)
    assert client.get(VA, headers=guest).status_code == 403
    assert client.post(f"{VA}/{a.id}/archive", headers=guest).status_code == 403
    db.expire_all()
    assert db.query(VoiceAutomation).get(a.id).status == "active"


@pytest.mark.parametrize("headers", [
    {},
    {"X-Athena-Caller-Mode": "admin"},
    {"X-Athena-Caller-Mode": "guest"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": ""},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "%FF%FE", "X-Athena-Guest-Stay": "101"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "a" * 256, "X-Athena-Guest-Stay": "101"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam", "X-Athena-Guest-Stay": ""},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam", "X-Athena-Guest-Stay": "abc"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam", "X-Athena-Guest-Stay": "0"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam", "X-Athena-Guest-Stay": "-5"},
    {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam", "X-Athena-Guest-Stay": "99999999999"},
], ids=["missing", "unknown-mode", "guest-no-name", "guest-empty-name", "bad-utf8", "too-long",
        "no-stay", "empty-stay", "non-numeric-stay", "zero-stay", "negative-stay", "huge-stay"])
def test_service_calls_must_declare_a_valid_scope(client, scoped, headers):
    resp = client.get(VA, headers={"X-Service-Key": get_config().service_api_key, **headers})
    assert resp.status_code == 400, resp.text


def test_percent_encoded_non_ascii_guest_name_round_trips(client, db, scoped):
    row = _automation(db, name="Zoë's", owner_type="guest", guest_name="Zoë", calendar_event_id=104)
    assert _ids(client.get(VA, headers=_svc("guest", "Zoë", 104))) == [row.id]


def test_a_user_is_the_owner_whatever_the_headers_say(client, scoped, test_user):
    headers = {**_bearer(test_user), "X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Sam",
               "X-Athena-Guest-Stay": str(STAY_A)}
    assert _ids(client.get(VA, headers=headers)) == sorted(scoped.values())


# ---------------------------------------------------------------------------
# The factories check the permission themselves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory_name", ["require_user_permission", "require_service_or_user_permission"])
def test_factory_refuses_a_user_without_the_permission(db, viewer_user, operator_user, factory_name):
    """get_current_user's scoped-role rules already refuse a viewer on every
    gated route, so the factory's own has_permission check is only
    observable where those rules let a scoped role through (/api/auth/*).
    A toy route there proves the factory still refuses it (403) and admits
    a role that holds the permission."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.utils import service_auth

    toy = FastAPI()

    @toy.get("/api/auth/zz-probe")
    async def probe(_=Depends(getattr(service_auth, factory_name)("write"))):
        return {"ok": True}

    toy.dependency_overrides[get_db] = lambda: db
    with TestClient(toy) as c:
        assert c.get("/api/auth/zz-probe").status_code == 401
        assert c.get("/api/auth/zz-probe", headers=_bearer(viewer_user)).status_code == 403
        assert c.get("/api/auth/zz-probe", headers=_bearer(operator_user)).status_code == 200


@pytest.mark.parametrize("owner_type", ["admin", "", "<b>x</b>"])
def test_owner_type_is_owner_or_guest(client, db, owner_type):
    body = {"name": "n", "owner_type": owner_type, "trigger_config": {"type": "time"}, "actions_config": []}
    assert client.post(VA, json=body, headers=_svc("owner")).status_code == 422
    assert db.query(VoiceAutomation).count() == 0

