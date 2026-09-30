"""Calendar-sources routes: a user credential on every route, no service key,
the feed URL masked everywhere except GET /{id}, audited source changes, and
source-update hardening (https, interval floor, Lodgify type lock).

Every test here turns DEV_MODE's auth bypass off, so the real production
authentication path is exercised.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.auth.oidc import create_access_token
from app.models import AuditLog, CalendarSource, ExternalAPIKey
from main import app
from shared.config import _clear_cache_for_tests, get_config
from shared.route_walk import dependency_calls, iter_api_routes

BASE = "/api/calendar-sources"
SECRET_URL = "https://user:SECRET@feed.example.com:8443/x?token=SECRET"
EMPTY_FEED = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"

EXPECTED_ROUTES = {
    ("GET", BASE),
    ("GET", f"{BASE}/types"),
    ("GET", f"{BASE}/{{source_id}}"),
    ("POST", BASE),
    ("PUT", f"{BASE}/{{source_id}}"),
    ("DELETE", f"{BASE}/{{source_id}}"),
    ("POST", f"{BASE}/{{source_id}}/test"),
    ("POST", f"{BASE}/test-url"),
    ("POST", f"{BASE}/{{source_id}}/sync"),
    ("POST", f"{BASE}/sync-all"),
    ("POST", f"{BASE}/sync-guest-sessions"),
}


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    _clear_cache_for_tests()
    with patch("app.routes.calendar_sources.fetch_ical_data", new=AsyncMock(return_value=EMPTY_FEED)):
        yield
    _clear_cache_for_tests()


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _source(db, **kw):
    defaults = dict(name="Feed", source_type="generic_ical", ical_url="https://feed.example.com/a.ics")
    defaults.update(kw)
    s = CalendarSource(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _lodgify_key(db, user, enabled=True):
    from app.utils.encryption import encrypt_value

    row = ExternalAPIKey(
        service_name="lodgify", api_name="Lodgify", api_key_encrypted=encrypt_value("k"),
        endpoint_url="https://api.lodgify.example", enabled=enabled, created_by_id=user.id,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# (a) route population + anonymous 401
# ---------------------------------------------------------------------------

def _calendar_routes():
    """(method, served path) -> walked route, for every calendar-sources
    route the app serves. shared.route_walk sees through FastAPI 0.141's
    included-router wrappers, which a flat app.routes walk doesn't."""
    found = {}
    for walked in iter_api_routes(app):
        if walked.path == BASE or walked.path.startswith(BASE + "/"):
            for method in walked.methods - {"HEAD", "OPTIONS"}:
                found[(method, walked.path)] = walked
    return found


def test_route_population_is_exactly_the_eleven_routes():
    found = set(_calendar_routes())
    assert len(found) >= 11
    assert ("POST", f"{BASE}/{{source_id}}/sync") in found
    assert found == EXPECTED_ROUTES


def _anon_request(client, method, path, source_id):
    url = path.replace("{source_id}", str(source_id))
    if (method, path) == ("POST", BASE):
        return client.post(url, json={"name": "n", "source_type": "generic_ical", "ical_url": "https://x.example/a.ics"})
    if method == "PUT":
        return client.put(url, json={})
    if path.endswith("/test-url"):
        return client.post(url, json={"url": "https://x.example/a.ics", "source_type": "generic_ical"})
    return client.request(method, url)


@pytest.mark.parametrize("method,path", sorted(EXPECTED_ROUTES - {("GET", f"{BASE}/types")}))
def test_every_route_but_types_is_401_anonymous(client, db, method, path):
    source = _source(db)
    resp = _anon_request(client, method, path, source.id)
    assert resp.status_code == 401, (method, path, resp.status_code, resp.text)


def test_types_stays_public(client, db):
    assert client.get(f"{BASE}/types").status_code == 200


# ---------------------------------------------------------------------------
# (b) credential matrix on every protected route
# ---------------------------------------------------------------------------

PROTECTED_ROUTES = sorted(EXPECTED_ROUTES - {("GET", f"{BASE}/types")})


def _call(client, route, source_id, headers):
    method, path = route
    url = path.replace("{source_id}", str(source_id))
    if (method, path) == ("POST", BASE):
        body = {"name": "New", "source_type": "generic_ical", "ical_url": "https://new.example.com/n.ics"}
        return client.post(url, json=body, headers=headers)
    if method == "PUT":
        return client.put(url, json={}, headers=headers)
    if path.endswith("/test-url"):
        return client.post(url, json={"url": "https://x.example/a.ics", "source_type": "generic_ical"},
                           headers=headers)
    return client.request(method, url, headers=headers)


_SUCCESS = {("POST", BASE): 201, ("DELETE", f"{BASE}/{{source_id}}"): 204}


def test_every_protected_route_uses_require_user_permission():
    seen = set()
    for key, walked in _calendar_routes().items():
        if key == ("GET", f"{BASE}/types"):
            continue
        seen.add(key)
        names = {getattr(call, "__qualname__", "") for call in dependency_calls(walked)}
        assert any(n.startswith("require_user_permission.") for n in names), (key, names)
    assert len(seen) == 10
    assert ("POST", f"{BASE}/{{source_id}}/sync") in seen
    assert seen == set(PROTECTED_ROUTES)


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
    if case == "owner_svc_wrong":
        return {**_bearer(owner), "X-Service-Key": "wrong-key"}
    if case == "owner_svc_correct":
        return {**_bearer(owner), "X-Service-Key": svc}
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


MATRIX = [
    ("none", 401),
    ("svc_correct", 401),
    ("svc_wrong", 401),
    ("svc_unset", 401),
    ("owner_svc_wrong", 401),
    ("owner_svc_correct", 401),
    ("owner_svc_unset", 401),
    ("viewer", 403),
    ("operator", 200),
    ("owner", 200),
    ("owner_api_key", 200),
]


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
@pytest.mark.parametrize("case,expected", MATRIX)
def test_credential_matrix(client, db, test_user, viewer_user, operator_user, test_api_key,
                           monkeypatch, route, case, expected):
    from app.services import calendar_sync
    from tests.conftest import TestingSessionLocal

    monkeypatch.setattr(calendar_sync, "SessionLocal", TestingSessionLocal)
    assert get_config().service_api_key, "conftest sets SERVICE_API_KEY"
    source = _source(db)
    headers = _creds(case, owner=test_user, viewer=viewer_user, operator=operator_user,
                     api_key=test_api_key[1], monkeypatch=monkeypatch)
    resp = _call(client, route, source.id, headers)
    if expected == 200:
        expected = _SUCCESS.get(route, 200)
    assert resp.status_code == expected, (route, case, resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# (c) masking
# ---------------------------------------------------------------------------

def mask_feed_url(url):
    from app.models import mask_feed_url as _mask

    return _mask(url)


def test_mask_function_edges():
    assert mask_feed_url(SECRET_URL) == "https://feed.example.com:8443/…"
    assert mask_feed_url("") == ""
    assert mask_feed_url(None) == ""
    assert mask_feed_url("http://legacy.example.com/x") == "http://legacy.example.com/…"
    assert mask_feed_url("not a url") == ""


def test_list_create_put_never_carry_the_feed_url(client, db, test_user):
    h = _bearer(test_user)
    created = client.post(BASE, json={"name": "S", "source_type": "generic_ical", "ical_url": SECRET_URL}, headers=h)
    assert created.status_code == 201, created.text
    body = created.json()
    assert "ical_url" not in body
    assert body["ical_url_masked"] == "https://feed.example.com:8443/…"
    assert "SECRET" not in created.text

    listed = client.get(BASE, headers=h)
    assert listed.status_code == 200
    assert all("ical_url" not in s for s in listed.json())
    assert "SECRET" not in listed.text

    put = client.put(f"{BASE}/{body['id']}", json={"name": "S2"}, headers=h)
    assert put.status_code == 200, put.text
    assert "ical_url" not in put.json()
    assert "SECRET" not in put.text

    audits = db.query(AuditLog).filter(AuditLog.resource_type == "calendar_source").all()
    assert audits
    assert "SECRET" not in json.dumps([[a.old_value, a.new_value] for a in audits])

    raw = client.get(f"{BASE}/{body['id']}", headers=h)
    assert raw.status_code == 200
    assert raw.json()["ical_url"] == SECRET_URL


def test_list_masks_blank_and_legacy_http(client, db, test_user):
    _source(db, name="blank", ical_url="")
    _source(db, name="legacy", ical_url="http://legacy.example.com/x")
    resp = client.get(BASE, headers=_bearer(test_user))
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()}
    assert by_name["blank"]["ical_url_masked"] == ""
    assert by_name["legacy"]["ical_url_masked"] == "http://legacy.example.com/…"


# ---------------------------------------------------------------------------
# (d) audit
# ---------------------------------------------------------------------------

def test_one_audit_row_per_create_put_delete(client, db, test_user):
    h = _bearer(test_user)
    created = client.post(BASE, json={"name": "S", "source_type": "generic_ical", "ical_url": SECRET_URL}, headers=h)
    sid = created.json()["id"]
    client.put(f"{BASE}/{sid}", json={"enabled": False}, headers=h)
    assert client.delete(f"{BASE}/{sid}", headers=h).status_code == 204

    rows = db.query(AuditLog).filter(AuditLog.resource_type == "calendar_source").order_by(AuditLog.id).all()
    assert [r.action for r in rows] == [
        "calendar_source_created", "calendar_source_updated", "calendar_source_deleted",
    ]
    assert all(r.resource_id == sid for r in rows)
    assert all(r.user_id == test_user.id for r in rows)
    dumped = json.dumps([[r.old_value, r.new_value] for r in rows])
    assert "SECRET" not in dumped
    assert "token=" not in dumped


# ---------------------------------------------------------------------------
# (e), (f), (k) URL validity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://feed.example.com/a.ics", ""])
def test_create_rejects_http_and_blank(client, db, test_user, url):
    resp = client.post(BASE, json={"name": "S", "source_type": "generic_ical", "ical_url": url}, headers=_bearer(test_user))
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("url", ["http://feed.example.com/a.ics", ""])
def test_put_rejects_http_and_blank(client, db, test_user, url):
    source = _source(db)
    resp = client.put(f"{BASE}/{source.id}", json={"ical_url": url}, headers=_bearer(test_user))
    assert resp.status_code == 400, resp.text


def test_partial_put_on_legacy_http_source_succeeds(client, db, test_user):
    source = _source(db, ical_url="http://legacy.example.com/x")
    resp = client.put(f"{BASE}/{source.id}", json={"enabled": False}, headers=_bearer(test_user))
    assert resp.status_code == 200, resp.text


def test_put_rejects_masked_value_posted_back(client, db, test_user):
    source = _source(db, ical_url=SECRET_URL)
    h = _bearer(test_user)
    masked = mask_feed_url(SECRET_URL)
    assert client.put(f"{BASE}/{source.id}", json={"ical_url": masked}, headers=h).status_code == 400
    ellipsis = "https://feed.example.com/…/a.ics"
    assert client.put(f"{BASE}/{source.id}", json={"ical_url": ellipsis}, headers=h).status_code == 400
    db.refresh(source)
    assert source.ical_url == SECRET_URL


def test_create_rejects_ellipsis(client, db, test_user):
    resp = client.post(
        BASE, json={"name": "S", "source_type": "generic_ical", "ical_url": "https://feed.example.com/…"},
        headers=_bearer(test_user),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# (g) Lodgify type lock
# ---------------------------------------------------------------------------

def test_type_lock_with_key_enabled(client, db, test_user):
    _lodgify_key(db, test_user, enabled=True)
    source = _source(db, source_type="lodgify", ical_url="https://www.lodgify.com/a.ics")
    h = _bearer(test_user)

    put = client.put(f"{BASE}/{source.id}", json={"source_type": "generic_ical"}, headers=h)
    assert put.status_code == 409, put.text
    assert "lodgify_source_type_locked" in put.text

    create = client.post(
        BASE, json={"name": "G", "source_type": "generic_ical", "ical_url": "https://www.lodgify.com/b.ics"}, headers=h,
    )
    assert create.status_code == 409, create.text
    assert "lodgify_source_type_locked" in create.text

    other = _source(db, name="other", ical_url="https://feed.example.com/o.ics")
    moved = client.put(f"{BASE}/{other.id}", json={"ical_url": "https://sub.lodgify.com/c.ics"}, headers=h)
    assert moved.status_code == 409, moved.text


def test_type_lock_off_with_key_disabled(client, db, test_user):
    _lodgify_key(db, test_user, enabled=False)
    source = _source(db, source_type="lodgify", ical_url="https://www.lodgify.com/a.ics")
    h = _bearer(test_user)
    assert client.put(f"{BASE}/{source.id}", json={"source_type": "generic_ical"}, headers=h).status_code == 200
    create = client.post(
        BASE, json={"name": "G", "source_type": "generic_ical", "ical_url": "https://www.lodgify.com/b.ics"}, headers=h,
    )
    assert create.status_code == 201, create.text


# ---------------------------------------------------------------------------
# (h) interval floor
# ---------------------------------------------------------------------------

def test_interval_floor(client, db, test_user):
    h = _bearer(test_user)
    body = {"name": "S", "source_type": "generic_ical", "ical_url": "https://feed.example.com/i.ics"}
    assert client.post(BASE, json={**body, "sync_interval_minutes": 4}, headers=h).status_code == 422
    ok = client.post(BASE, json={**body, "sync_interval_minutes": 5}, headers=h)
    assert ok.status_code == 201, ok.text
    sid = ok.json()["id"]
    assert client.put(f"{BASE}/{sid}", json={"sync_interval_minutes": 4}, headers=h).status_code == 422
    assert client.put(f"{BASE}/{sid}", json={"sync_interval_minutes": 5}, headers=h).status_code == 200


# ---------------------------------------------------------------------------
# r3.2: raw-URL reveal audit, interval cap, trailing-dot host, /test-url body
# ---------------------------------------------------------------------------

def test_get_by_id_audits_the_reveal_without_the_url(client, db, test_user):
    source = _source(db, ical_url=SECRET_URL)
    resp = client.get(f"{BASE}/{source.id}", headers=_bearer(test_user))
    assert resp.status_code == 200
    rows = db.query(AuditLog).filter(AuditLog.action == "calendar_source_url_revealed").all()
    assert len(rows) == 1
    assert rows[0].resource_id == source.id and rows[0].user_id == test_user.id
    assert rows[0].resource_type == "calendar_source"
    dumped = json.dumps([rows[0].old_value, rows[0].new_value])
    assert "SECRET" not in dumped and "feed.example.com" not in dumped


def test_interval_cap(client, db, test_user):
    h = _bearer(test_user)
    body = {"name": "S", "source_type": "generic_ical", "ical_url": "https://feed.example.com/cap.ics"}
    assert client.post(BASE, json={**body, "sync_interval_minutes": 1441}, headers=h).status_code == 422
    ok = client.post(BASE, json={**body, "sync_interval_minutes": 1440}, headers=h)
    assert ok.status_code == 201, ok.text
    sid = ok.json()["id"]
    assert client.put(f"{BASE}/{sid}", json={"sync_interval_minutes": 1441}, headers=h).status_code == 422
    assert client.put(f"{BASE}/{sid}", json={"sync_interval_minutes": 1440}, headers=h).status_code == 200


@pytest.mark.parametrize("url,expected", [
    ("https://www.lodgify.com./x.ics", True),
    ("https://lodgify.com./x.ics", True),
    ("https://WWW.LODGIFY.COM/x.ics", True),
    ("https://lodgify.com.evil.example/x.ics", False),
    ("https://notlodgify.com./x.ics", False),
])
def test_is_lodgify_host_trailing_dot(url, expected):
    from app.routes.calendar_sources import is_lodgify_host

    assert is_lodgify_host(url) is expected


def test_type_lock_catches_a_trailing_dot_host(client, db, test_user):
    _lodgify_key(db, test_user, enabled=True)
    create = client.post(
        BASE, json={"name": "G", "source_type": "generic_ical", "ical_url": "https://www.lodgify.com./b.ics"},
        headers=_bearer(test_user),
    )
    assert create.status_code == 409, create.text


TOKEN_URL = "https://feed.example.com/cal.ics?token=SECRETTOKEN42"
_ONE_EVENT = (
    "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:t@x\nDTSTART;VALUE=DATE:20991001\n"
    "DTEND;VALUE=DATE:20991003\nSUMMARY:Reserved\nEND:VEVENT\nEND:VCALENDAR\n"
)


def test_test_url_rejects_a_query_param(client, db, test_user):
    h = _bearer(test_user)
    resp = client.post(f"{BASE}/test-url", params={"url": TOKEN_URL, "source_type": "generic_ical"}, headers=h)
    assert resp.status_code == 422
    both = client.post(f"{BASE}/test-url", params={"url": TOKEN_URL},
                       json={"url": TOKEN_URL, "source_type": "generic_ical"}, headers=h)
    assert both.status_code == 422
    assert "SECRETTOKEN42" not in resp.text + both.text


def test_test_url_body_success_never_echoes_the_token(client, db, test_user):
    import structlog

    with structlog.testing.capture_logs() as logs, \
            patch("app.routes.calendar_sources.fetch_ical_data", new=AsyncMock(return_value=_ONE_EVENT)) as fetch:
        resp = client.post(f"{BASE}/test-url", json={"url": TOKEN_URL, "source_type": "generic_ical"},
                           headers=_bearer(test_user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True and resp.json()["event_count"] == 1
    assert fetch.await_args.args[0] == TOKEN_URL
    assert "SECRETTOKEN42" not in resp.text
    assert "SECRETTOKEN42" not in repr(logs)


def test_test_url_body_failure_never_echoes_the_token(client, db, test_user):
    import httpx
    import structlog

    exc = httpx.ConnectError(f"connect failed for {TOKEN_URL}")
    with structlog.testing.capture_logs() as logs, \
            patch("app.routes.calendar_sources.fetch_ical_data", new=AsyncMock(side_effect=exc)):
        resp = client.post(f"{BASE}/test-url", json={"url": TOKEN_URL, "source_type": "generic_ical"},
                           headers=_bearer(test_user))
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "ConnectError" in resp.json()["message"]
    assert "SECRETTOKEN42" not in resp.text
    assert "SECRETTOKEN42" not in repr(logs)
