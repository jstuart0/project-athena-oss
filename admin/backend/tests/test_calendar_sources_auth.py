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

def test_route_population_is_exactly_the_eleven_routes():
    found = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path == BASE or path.startswith(BASE + "/"):
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
                found.add((method, path))
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
        return client.post(url, params={"url": "https://x.example/a.ics"})
    return client.request(method, url)


@pytest.mark.parametrize("method,path", sorted(EXPECTED_ROUTES - {("GET", f"{BASE}/types")}))
def test_every_route_but_types_is_401_anonymous(client, db, method, path):
    source = _source(db)
    resp = _anon_request(client, method, path, source.id)
    assert resp.status_code == 401, (method, path, resp.status_code, resp.text)


def test_types_stays_public(client, db):
    assert client.get(f"{BASE}/types").status_code == 200


# ---------------------------------------------------------------------------
# (b) credential matrix on the four adopting routes
# ---------------------------------------------------------------------------

FOUR_ROUTES = ["list", "put", "sync", "sync_guest_sessions"]


def _call(client, route, source_id, headers):
    if route == "list":
        return client.get(BASE, headers=headers)
    if route == "put":
        return client.put(f"{BASE}/{source_id}", json={}, headers=headers)
    if route == "sync":
        return client.post(f"{BASE}/{source_id}/sync", headers=headers)
    return client.post(f"{BASE}/sync-guest-sessions", headers=headers)


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


@pytest.mark.parametrize("route", FOUR_ROUTES)
@pytest.mark.parametrize("case,expected", MATRIX)
def test_credential_matrix(client, db, test_user, viewer_user, operator_user, test_api_key,
                           monkeypatch, route, case, expected):
    assert get_config().service_api_key, "conftest sets SERVICE_API_KEY"
    source = _source(db)
    headers = _creds(case, owner=test_user, viewer=viewer_user, operator=operator_user,
                     api_key=test_api_key[1], monkeypatch=monkeypatch)
    resp = _call(client, route, source.id, headers)
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
