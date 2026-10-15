"""GET/PUT /api/settings/favorite-teams: the credential matrix, the revision
check, the first-insert race, validation, the audit row and the read failure.

Production auth (DEV_MODE off) with real Bearer tokens; the factories call
get_current_user themselves, so dependency_overrides can't stand in.
"""
import json
from unittest import mock

import pytest
from sqlalchemy.exc import IntegrityError

from app.auth.oidc import create_access_token
from app.models import AuditLog, SystemSetting
from shared.config import _clear_cache_for_tests, get_config
from shared.favorite_teams import SETTING_KEY

from tests.route_auth_legacy import LEGACY_PERMISSIONLESS
from tests.test_route_auth_population import INTENTIONALLY_PUBLIC, _walked_by_op

URL = "/api/settings/favorite-teams"
OWLS = {"name": "Anytown Owls", "league": "nfl"}
HAWKS = {"name": "Hawks", "league": None}


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _service():
    return {"X-Service-Key": get_config().service_api_key}


def _stored(db):
    db.expire_all()
    row = db.query(SystemSetting).filter(SystemSetting.key == SETTING_KEY).first()
    return json.loads(row.value) if row else None


def test_both_routes_exist_and_are_neither_public_nor_legacy():
    by_op = _walked_by_op()
    for op in (("GET", URL), ("PUT", URL)):
        assert op in by_op
        assert op not in INTENTIONALLY_PUBLIC
        assert f"{op[0]} {op[1]}" not in LEGACY_PERMISSIONLESS


def test_permissions_are_pinned_on_the_dependencies():
    from shared.route_walk import dependency_calls

    by_op = _walked_by_op()
    for op, kind, permission in ((("GET", URL), ("service", "user"), "read"), (("PUT", URL), ("user",), "write")):
        (walked,) = by_op[op]
        inners = [c for c in dependency_calls(walked) if getattr(c, "required_permission", None)]
        assert len(inners) == 1
        assert inners[0].required_permission == permission
        assert tuple(inners[0].caller_kinds) == kind


def test_get_credentials(client, viewer_user, operator_user, test_user):
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=_bearer(viewer_user)).status_code == 403
    assert client.get(URL, headers=_service()).status_code == 200
    assert client.get(URL, headers=_bearer(operator_user)).status_code == 200
    assert client.get(URL, headers=_bearer(test_user)).status_code == 200


def test_put_credentials(client, viewer_user, operator_user, test_user):
    body = {"teams": [OWLS], "revision": 0}
    assert client.put(URL, json=body).status_code == 401
    assert client.put(URL, json=body, headers=_service()).status_code == 401
    assert client.put(URL, json=body, headers={**_bearer(test_user), **_service()}).status_code == 401
    assert client.put(URL, json=body, headers=_bearer(viewer_user)).status_code == 403
    ok = client.put(URL, json=body, headers=_bearer(operator_user))
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    assert client.put(URL, json={**body, "revision": 1}, headers=_bearer(test_user)).status_code == 200


def test_get_with_no_row(client, test_user):
    body = client.get(URL, headers=_bearer(test_user)).json()
    assert body["teams"] == [] and body["revision"] == 0 and body["max"] == 8
    assert len(body["leagues"]) == 17
    assert {"code", "label", "sport"} == set(body["leagues"][0])


def test_put_round_trips_in_order(client, db, operator_user):
    teams = [HAWKS, OWLS, {"name": "Bayern München", "league": "bundesliga"}]
    resp = client.put(URL, json={"teams": teams, "revision": 0}, headers=_bearer(operator_user))
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1 and resp.json()["teams"] == teams
    assert _stored(db)["teams"] == teams
    assert client.get(URL, headers=_service()).json()["teams"] == teams
    reordered = list(reversed(teams))
    resp2 = client.put(URL, json={"teams": reordered, "revision": 1}, headers=_bearer(operator_user))
    assert resp2.json()["revision"] == 2
    assert _stored(db)["teams"] == reordered


def test_empty_list_is_a_valid_save(client, db, operator_user):
    client.put(URL, json={"teams": [OWLS], "revision": 0}, headers=_bearer(operator_user))
    resp = client.put(URL, json={"teams": [], "revision": 1}, headers=_bearer(operator_user))
    assert resp.status_code == 200 and resp.json()["teams"] == []
    assert _stored(db)["teams"] == []


def test_stale_revision_is_409_and_leaves_the_row(client, db, operator_user):
    client.put(URL, json={"teams": [OWLS], "revision": 0}, headers=_bearer(operator_user))
    before = _stored(db)
    resp = client.put(URL, json={"teams": [HAWKS], "revision": 0}, headers=_bearer(operator_user))
    assert resp.status_code == 409 and resp.json()["detail"] == "stale_revision"
    assert _stored(db) == before
    assert db.query(AuditLog).filter(AuditLog.action == "favorite_teams_update").count() == 1


def test_first_insert_race_is_409_and_rolled_back(client, db, operator_user):
    calls = {"rollback": 0}
    real_rollback = db.rollback

    def counting_rollback():
        calls["rollback"] += 1
        return real_rollback()

    def lose_the_race():
        raise IntegrityError("insert", {}, Exception("unique"))

    with mock.patch.object(db, "flush", side_effect=lose_the_race), mock.patch.object(
        db, "rollback", side_effect=counting_rollback
    ):
        resp = client.put(URL, json={"teams": [OWLS], "revision": 0}, headers=_bearer(operator_user))
    assert resp.status_code == 409 and resp.json()["detail"] == "stale_revision"
    assert calls["rollback"] >= 1
    assert _stored(db) is None


@pytest.mark.parametrize(
    "body",
    [
        {"teams": [{"name": "<b>T</b>"}], "revision": 0},
        {"teams": [{"name": "a\nb"}], "revision": 0},
        {"teams": [{"name": "x" * 65}], "revision": 0},
        {"teams": [{"name": ""}], "revision": 0},
        {"teams": [{"name": "Owls", "league": "curling"}], "revision": 0},
        {"teams": [{"name": f"Team {i}"} for i in range(9)], "revision": 0},
        {"teams": [{"name": "Owls"}, {"name": "owls"}], "revision": 0},
        {"teams": [{"name": "Owls", "colour": "blue"}], "revision": 0},
        {"teams": [], "revision": 0, "extra": 1},
        {"teams": [], "revision": -1},
        {"teams": []},
        {"revision": 0},
    ],
)
def test_invalid_bodies_are_422_and_write_nothing(client, db, operator_user, body):
    resp = client.put(URL, json=body, headers=_bearer(operator_user))
    assert resp.status_code == 422
    assert _stored(db) is None
    assert db.query(AuditLog).count() == 0


def test_one_audit_row_per_save_and_no_names(client, db, operator_user):
    teams = [OWLS, {"name": "Hawks", "league": "mlb"}, {"name": "Crows", "league": "mlb"}]
    client.put(URL, json={"teams": teams, "revision": 0}, headers=_bearer(operator_user))
    rows = db.query(AuditLog).filter(AuditLog.action == "favorite_teams_update").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == operator_user.id and row.resource_type == "system_setting"
    assert row.new_value == {"count": 3, "leagues": ["mlb", "nfl"]}
    dumped = json.dumps([row.old_value, row.new_value])
    assert not any(t["name"] in dumped for t in teams)
    assert row.old_value == {"count": 0}
    client.put(URL, json={"teams": [HAWKS], "revision": 1}, headers=_bearer(operator_user))
    second = db.query(AuditLog).filter(AuditLog.action == "favorite_teams_update").order_by(AuditLog.id.desc()).first()
    assert second.old_value == {"count": 3} and second.new_value == {"count": 1, "leagues": []}


def test_get_database_failure_is_500_not_empty(client, db, test_user):
    headers = _bearer(test_user)
    real_query = db.query

    def failing(model, *a, **k):
        if model is SystemSetting:
            raise RuntimeError("database is down: secret-detail")
        return real_query(model, *a, **k)

    with mock.patch.object(db, "query", side_effect=failing):
        resp = client.get(URL, headers=_service())
    assert resp.status_code == 500
    assert "secret-detail" not in resp.text and "teams" not in resp.json()
    assert headers


def test_unreadable_stored_value_reads_as_empty(client, db, operator_user):
    db.add(SystemSetting(key=SETTING_KEY, value="{", category="sports"))
    db.commit()
    body = client.get(URL, headers=_bearer(operator_user)).json()
    assert body["teams"] == [] and body["revision"] == 0
