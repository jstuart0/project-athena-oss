"""GET /api/settings/household and PUT /api/settings/household/{section}: the
credential matrix, per-section permissions, masking, the 422 shape, the CAS 409
(on SQLite; the Postgres race is in tests/integration) and the audit rows.

Production auth (DEV_MODE off) with real Bearer tokens; the dependencies call
get_current_user themselves, so dependency_overrides can't stand in.
"""
import asyncio
import json
from unittest import mock

import pytest
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.auth.oidc import create_access_token
from app.models import AuditLog, SystemSetting
from app.routes import household_settings as route_module
from shared.config import _clear_cache_for_tests, get_config
from shared.household_settings import SECTIONS, parse_stored

from tests.route_auth_legacy import LEGACY_PERMISSIONLESS
from tests.test_route_auth_population import INTENTIONALLY_PUBLIC, _walked_by_op

URL = "/api/settings/household"
FEED = {"id": "rtd_bus", "name": "RTD Local Bus", "url": "https://feeds.example.org/gtfs.zip?api_key=SECRET"}
MASKED = "https://feeds.example.org"


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


def _stored(db, name):
    row = _row(db, name)
    return json.loads(row.value) if row else None


def put(client, user, section, value, revision=0):
    return client.put(f"{URL}/{section}", json={"value": value, "revision": revision}, headers=_bearer(user))


def audits(db, action):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()


# -- registration ------------------------------------------------------------------------------------

def test_both_routes_exist_and_are_neither_public_nor_legacy():
    by_op = _walked_by_op()
    for op in (("GET", URL), ("PUT", URL + "/{section}")):
        assert op in by_op, op
        assert op not in INTENTIONALLY_PUBLIC
        assert f"{op[0]} {op[1]}" not in LEGACY_PERMISSIONLESS


def test_permissions_are_pinned_on_the_dependencies():
    from shared.route_walk import dependency_calls

    by_op = _walked_by_op()
    for op, kinds, permission in ((("GET", URL), ("service", "user"), "read"), (("PUT", URL + "/{section}"), ("user",), "write")):
        (walked,) = by_op[op]
        inners = [c for c in dependency_calls(walked) if getattr(c, "required_permission", None)]
        assert len(inners) == 1
        assert inners[0].required_permission == permission and tuple(inners[0].caller_kinds) == kinds


# -- credentials -------------------------------------------------------------------------------------

def test_get_credentials(client, viewer_user, operator_user, test_user):
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=_bearer(viewer_user)).status_code == 403
    assert client.get(URL, headers={"X-Service-Key": "wrong"}).status_code == 401
    assert client.get(URL, headers=_service()).status_code == 200
    assert client.get(URL, headers=_bearer(operator_user)).status_code == 200
    assert client.get(URL, headers=_bearer(test_user)).status_code == 200


def test_put_credentials(client, viewer_user, operator_user, test_user):
    body = {"value": {"default_station": "NYP"}, "revision": 0}
    target = f"{URL}/amtrak"
    assert client.put(target, json=body).status_code == 401
    assert client.put(target, json=body, headers=_service()).status_code == 401
    assert client.put(target, json=body, headers={**_bearer(test_user), **_service()}).status_code == 401
    assert client.put(target, json=body, headers=_bearer(viewer_user)).status_code == 403
    ok = client.put(target, json=body, headers=_bearer(operator_user))
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    assert client.put(target, json={**body, "revision": 1}, headers=_bearer(test_user)).status_code == 200


def test_a_key_sent_while_the_server_has_none_configured(client, monkeypatch, test_user):
    monkeypatch.setenv("SERVICE_API_KEY", "")
    _clear_cache_for_tests()
    assert client.get(URL, headers={"X-Service-Key": "anything"}).status_code == 503
    body = {"value": {"default_station": "NYP"}, "revision": 0}
    resp = client.put(f"{URL}/amtrak", json=body, headers={"X-Service-Key": "anything"})
    assert resp.status_code == 401
    assert client.get(URL, headers=_bearer(test_user)).status_code == 200


def test_a_service_key_put_is_refused_even_for_an_owner_only_section(client, db):
    resp = client.put(f"{URL}/sms", json={"value": {"default_country_code": "44"}, "revision": 0}, headers=_service())
    assert resp.status_code == 401 and _row(db, "sms") is None


# -- GET ---------------------------------------------------------------------------------------------

def test_get_with_nothing_stored(client, test_user):
    body = client.get(URL, headers=_bearer(test_user)).json()
    assert list(body) == list(SECTIONS)
    for name, section in body.items():
        spec = SECTIONS[name]
        assert section["value"] is None and section["revision"] == 0
        assert section["required_permission"] == spec.required_permission and section["writable"] is spec.writable
        assert set(section["stored"]) == set(spec.model.model_fields) and not any(section["stored"].values())


def test_get_unknown_sections_are_dropped(client, test_user, operator_user):
    for params in ({"sections": ["transit", "bogus"]}, {"sections": ["bogus", "transit"]}):
        body = client.get(URL, params=params, headers=_bearer(test_user)).json()
        assert list(body) == ["transit"]
    assert client.get(URL, params={"sections": ["bogus"]}, headers=_service()).json() == {}
    assert list(client.get(URL, params={"sections": ["sms", "amtrak"]}, headers=_service()).json()) == ["amtrak", "sms"]
    assert list(client.get(URL, params={"sections": ["sms"]}, headers=_bearer(operator_user)).json()) == ["sms"]


def test_get_masks_urls_for_users_and_not_for_the_service(client, db, test_user, operator_user):
    assert put(client, test_user, "transit", {"gtfs_feeds": [FEED]}).status_code == 200
    source = {"id": "d", "name": "D", "type": "link_scan", "url": "https://events.example.org/e?k=SECRET",
              "base_url": "https://events.example.org/base?k=SECRET"}
    assert put(client, test_user, "community_events", {"sources": [source]}).status_code == 200
    for user in (operator_user, test_user):
        body = client.get(URL, params={"sections": ["transit", "community_events"]}, headers=_bearer(user)).json()
        feed = body["transit"]["value"]["gtfs_feeds"][0]
        assert feed["url"] == MASKED and feed["url_masked"] is True
        src = body["community_events"]["value"]["sources"][0]
        assert src["url"] == "https://events.example.org" and src["base_url"] == "https://events.example.org"
        assert "SECRET" not in json.dumps(body) and "gtfs.zip" not in json.dumps(body)
        assert body["transit"]["stored"] == {"region_name": False, "gtfs_feeds": True, "static_services": False}
    whole = client.get(URL, params={"sections": ["transit", "community_events"]}, headers=_service()).json()
    assert whole["transit"]["value"]["gtfs_feeds"][0]["url"] == FEED["url"]
    assert "url_masked" not in whole["transit"]["value"]["gtfs_feeds"][0]
    assert whole["community_events"]["value"]["sources"][0]["base_url"] == source["base_url"]


def test_get_masks_whenever_auth_kind_is_not_service(db):
    db.add(SystemSetting(key="household.transit", category="household", value=json.dumps(
        {"v": 1, "revision": 2, "value": {"gtfs_feeds": [FEED]}})))
    db.commit()

    def call(**state):
        request = Request({"type": "http", "headers": [], "query_string": b"", "method": "GET", "path": URL})
        for k, v in state.items():
            setattr(request.state, k, v)
        return asyncio.run(route_module.get_household_settings(request, sections=["transit"], db=db))

    assert call()["transit"]["value"]["gtfs_feeds"][0]["url"] == MASKED
    assert call(auth_kind="user")["transit"]["value"]["gtfs_feeds"][0]["url"] == MASKED
    assert call(auth_kind="anything")["transit"]["value"]["gtfs_feeds"][0]["url"] == MASKED
    assert call(auth_kind="service")["transit"]["value"]["gtfs_feeds"][0]["url"] == FEED["url"]


def test_location_is_served_from_base_knowledge_and_is_read_only(client, db, test_user):
    from app.models import BaseKnowledge

    db.add(BaseKnowledge(category="location", key="default_location", value="Springfield, IL", applies_to="both",
                         priority=0, enabled=True))
    db.add(SystemSetting(key="base_knowledge_settings", category="base_knowledge",
                         value=json.dumps({"city": "Springfield", "state": "IL"})))
    db.commit()
    section = client.get(URL, params={"sections": ["location"]}, headers=_service()).json()["location"]
    assert section["value"] == {"city": "Springfield", "state": "IL", "text": "Springfield, IL"}
    assert section["writable"] is False and section["stored"] == {"city": True, "state": True, "text": True}
    resp = put(client, test_user, "location", {"city": "X"})
    assert resp.status_code == 405 and _row(db, "location") is None


def test_get_database_failure_is_500_not_empty(client, db):
    real_query = db.query

    def failing(model, *a, **k):
        if model is SystemSetting:
            raise RuntimeError("database is down: secret-detail")
        return real_query(model, *a, **k)

    with mock.patch.object(db, "query", side_effect=failing):
        resp = client.get(URL, headers=_service())
    assert resp.status_code == 500 and "secret-detail" not in resp.text and "transit" not in resp.json()


def test_unreadable_and_unknown_version_rows_read_as_unset(client, db, operator_user):
    db.add(SystemSetting(key="household.sms", value="{", category="household"))
    db.add(SystemSetting(key="household.amtrak", value='{"v":9,"revision":4,"value":{"default_station":"BOS"}}', category="household"))
    db.commit()
    body = client.get(URL, headers=_bearer(operator_user)).json()
    assert body["sms"]["value"] is None and body["sms"]["revision"] == 0
    assert body["amtrak"]["value"] is None and body["amtrak"]["revision"] == 4


# -- PUT: permissions per section ----------------------------------------------------------------------------

def test_an_operator_put_to_an_owner_only_section_is_403_and_audited_once(client, db, operator_user):
    for _ in range(3):
        resp = put(client, operator_user, "sms", {"default_country_code": "44"})
        assert resp.status_code == 403 and resp.json() == {"detail": {"error": "insufficient_role"}}
    assert _row(db, "sms") is None
    (row,) = audits(db, "household_settings_refused")
    assert row.user_id == operator_user.id and row.success is False and row.error_message == "insufficient_role"
    assert row.new_value == {"section": "sms", "reason": "insufficient_role"}
    assert "44" not in json.dumps(row.new_value)
    put(client, operator_user, "lights", {"room_light_exclude": []})
    assert len(audits(db, "household_settings_refused")) == 2  # another section, another row


def test_the_refusal_throttle_expires(client, db, operator_user):
    clock = mock.Mock(monotonic=mock.Mock(side_effect=[1000.0, 1000.0 + 61.0]))
    with mock.patch.object(route_module, "time", clock):
        put(client, operator_user, "sms", {"default_country_code": "44"})
        put(client, operator_user, "sms", {"default_country_code": "44"})
    assert len(audits(db, "household_settings_refused")) == 2


@pytest.mark.parametrize("section,value", [
    ("sms", {"default_country_code": "44"}),
    ("lights", {"room_light_exclude": ["light.porch_*"]}),
    ("bed_warmer", {"power_main": "switch.bed"}),
    ("jarvis_web", {"climate_entity": "climate.hall"}),
])
def test_the_owner_only_sections_refuse_an_operator_and_allow_an_owner(client, db, operator_user, test_user, section, value):
    assert put(client, operator_user, section, value).status_code == 403
    assert _row(db, section) is None
    ok = put(client, test_user, section, value)
    assert ok.status_code == 200 and ok.json()["revision"] == 1 and ok.json()["required_permission"] == "manage_infrastructure"
    assert _stored(db, section)["value"][next(iter(value))] == next(iter(value.values()))


@pytest.mark.parametrize("section,value", [
    ("amtrak", {"default_station": "bos"}),
    ("gateway", {"fallback_model": "tiny:1b"}),
    ("transit", {"region_name": "Denver"}),
    ("community_events", {"sources": []}),
])
def test_the_write_sections_allow_an_operator(client, db, operator_user, section, value):
    assert put(client, operator_user, section, value).status_code == 200
    assert _row(db, section) is not None


def test_an_owner_put_to_sms_audits_the_codes(client, db, test_user):
    resp = put(client, test_user, "sms", {"default_country_code": "+44"})
    assert resp.status_code == 200 and resp.json()["value"] == {"default_country_code": "44"}
    assert resp.json()["stored"] == {"default_country_code": True}
    (row,) = audits(db, "household_settings_update")
    assert row.user_id == test_user.id and row.resource_type == "system_setting" and row.success is True
    assert row.old_value == {"section": "sms", "default_country_code": None}
    assert row.new_value == {"section": "sms", "default_country_code": "44", "changed_fields": ["default_country_code"], "revision": 1}
    put(client, test_user, "sms", {"default_country_code": "1"}, revision=1)
    second = audits(db, "household_settings_update")[1]
    assert second.old_value["default_country_code"] == "44" and second.new_value["default_country_code"] == "1"


def test_a_read_only_user_reads_masked_urls_and_cannot_write(client, db, test_user, operator_user, viewer_user):
    put(client, test_user, "transit", {"gtfs_feeds": [FEED]})
    body = client.get(URL, params={"sections": ["transit"]}, headers=_bearer(operator_user)).json()
    assert body["transit"]["value"]["gtfs_feeds"][0]["url"] == "https://feeds.example.org"
    assert put(client, viewer_user, "transit", {"region_name": "x"}, revision=1).status_code == 403
    assert _stored(db, "transit")["revision"] == 1


def test_unknown_and_read_only_sections(client, test_user):
    resp = put(client, test_user, "bogus", {})
    assert resp.status_code == 404
    assert put(client, test_user, "location", {"city": "x"}).status_code == 405
    assert client.put(f"{URL}/amtrak", json={"value": {}, "revision": 0}).status_code == 401  # auth before the section check


# -- PUT: body shape and the 422 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"value": {"default_station": "NYP"}},
    {"revision": 0},
    {"value": {"default_station": "NYP"}, "revision": "0"},
    {"value": {"default_station": "NYP"}, "revision": True},
    {"value": {"default_station": "NYP"}, "revision": -1},
    {"value": {"default_station": "NYP"}, "revision": 0, "extra": 1},
    {"value": ["NYP"], "revision": 0},
    {"value": {"default_station": "NY"}, "revision": 0},
    {"value": {"unknown": 1}, "revision": 0},
    {},
])
def test_invalid_bodies_are_422_and_write_nothing(client, db, operator_user, body):
    resp = client.put(f"{URL}/amtrak", json=body, headers=_bearer(operator_user))
    assert resp.status_code == 422
    assert all(set(e) == {"loc", "type"} for e in resp.json()["detail"])
    assert _row(db, "amtrak") is None and not audits(db, "household_settings_update")


def test_a_missing_revision_is_422_at_loc_revision(client, operator_user):
    resp = client.put(f"{URL}/amtrak", json={"value": {}}, headers=_bearer(operator_user))
    assert resp.status_code == 422 and resp.json()["detail"] == [{"loc": ["revision"], "type": "missing"}]


def test_an_invalid_url_422_never_contains_the_url(client, db, operator_user):
    secret = "http://10.1.2.3/feed.zip?api_key=SECRET-KEY"
    resp = put(client, operator_user, "transit", {"gtfs_feeds": [{**FEED, "url": secret}]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"loc": ["gtfs_feeds", 0, "url"], "type": "url_private_host"}]
    assert "SECRET-KEY" not in resp.text and "10.1.2.3" not in resp.text
    assert _row(db, "transit") is None


def test_allow_private_is_refused(client, operator_user):
    resp = put(client, operator_user, "transit", {"gtfs_feeds": [{**FEED, "allow_private": True}]})
    assert resp.status_code == 422 and resp.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("raw", [b"{not json SECRET-BODY", b'["SECRET-BODY"]', b'"SECRET-BODY"', b"null", b"123"])
def test_a_malformed_body_422_never_echoes_input(client, operator_user, raw):
    resp = client.put(
        f"{URL}/amtrak", content=raw, headers={**_bearer(operator_user), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert "SECRET-BODY" not in resp.text and '"input"' not in resp.text and '"ctx"' not in resp.text


def test_a_422_on_an_owner_only_section_is_audited_once_per_minute(client, db, test_user):
    for _ in range(3):
        assert put(client, test_user, "sms", {"default_country_code": "0"}).status_code == 422
    (row,) = audits(db, "household_settings_refused")
    assert row.new_value == {"section": "sms", "reason": "validation_failed"} and row.success is False
    assert "0" not in row.new_value.values()
    put(client, test_user, "amtrak", {"default_station": "N"})
    assert len(audits(db, "household_settings_refused")) == 1  # a write section's 422 isn't audited


# -- PUT: revisions and the CAS --------------------------------------------------------------------------------------

def test_stale_revision_is_409_and_leaves_the_row(client, db, operator_user):
    assert put(client, operator_user, "amtrak", {"default_station": "NYP"}).status_code == 200
    before = _row(db, "amtrak").value
    resp = put(client, operator_user, "amtrak", {"default_station": "BOS"}, revision=0)
    assert resp.status_code == 409 and resp.json()["detail"] == "stale_revision"
    assert _row(db, "amtrak").value == before
    assert len(audits(db, "household_settings_update")) == 1
    ahead = put(client, operator_user, "amtrak", {"default_station": "BOS"}, revision=5)
    assert ahead.status_code == 409 and _row(db, "amtrak").value == before


def test_a_409_on_an_owner_only_section_is_audited_once_per_minute(client, db, test_user):
    put(client, test_user, "sms", {"default_country_code": "44"})
    for _ in range(3):
        assert put(client, test_user, "sms", {"default_country_code": "1"}, revision=0).status_code == 409
    (row,) = audits(db, "household_settings_refused")
    assert row.new_value == {"section": "sms", "reason": "stale_revision"}


def test_revisions_count_up_and_a_round_trip_reads_back(client, db, operator_user):
    for revision, code in enumerate(("NYP", "BOS", "DEN")):
        resp = put(client, operator_user, "amtrak", {"default_station": code}, revision=revision)
        assert resp.status_code == 200 and resp.json()["revision"] == revision + 1
    assert _stored(db, "amtrak") == {"v": 1, "revision": 3, "value": {"default_station": "DEN"}}
    assert client.get(URL, params={"sections": ["amtrak"]}, headers=_service()).json()["amtrak"]["revision"] == 3


def test_a_row_changed_between_the_read_and_the_write_is_409_and_not_overwritten(client, db, operator_user):
    put(client, operator_user, "amtrak", {"default_station": "NYP"})
    rival = '{"v":1,"revision":2,"value":{"default_station":"BOS"}}'
    real = route_module.stored_value

    def stored_value_after_a_rival_save(*args, **kwargs):
        db.query(SystemSetting).filter(SystemSetting.key == "household.amtrak").update({"value": rival})
        db.commit()  # the rival's save, committed before ours reaches the database
        return real(*args, **kwargs)

    with mock.patch.object(route_module, "stored_value", side_effect=stored_value_after_a_rival_save):
        resp = put(client, operator_user, "amtrak", {"default_station": "DEN"}, revision=1)
    assert resp.status_code == 409
    assert _row(db, "amtrak").value == rival
    assert len(audits(db, "household_settings_update")) == 1


def test_first_insert_race_is_409_and_rolled_back(client, db, operator_user):
    def lose_the_race():
        raise IntegrityError("insert", {}, Exception("unique"))

    with mock.patch.object(db, "flush", side_effect=lose_the_race):
        resp = put(client, operator_user, "amtrak", {"default_station": "NYP"})
    assert resp.status_code == 409 and resp.json()["detail"] == "stale_revision"
    assert _row(db, "amtrak") is None and not audits(db, "household_settings_update")


def test_a_save_failure_is_500_without_detail(client, db, operator_user):
    with mock.patch.object(db, "flush", side_effect=RuntimeError("secret-detail")):
        resp = put(client, operator_user, "amtrak", {"default_station": "NYP"})
    assert resp.status_code == 500 and "secret-detail" not in resp.text and _row(db, "amtrak") is None


# -- PUT: values, null, lists, masking -----------------------------------------------------------------------------------

def test_null_clears_a_field_and_an_empty_list_is_kept(client, db, test_user):
    put(client, test_user, "jarvis_web", {"climate_entity": "climate.a", "default_room": "Den"})
    resp = put(client, test_user, "jarvis_web", {"climate_entity": None, "default_room": "Den"}, revision=1)
    assert resp.status_code == 200 and resp.json()["stored"]["climate_entity"] is False and resp.json()["stored"]["default_room"] is True
    put(client, test_user, "lights", {"room_light_exclude": []})
    assert _stored(db, "lights")["value"] == {"room_light_exclude": []}
    assert client.get(URL, params={"sections": ["lights"]}, headers=_service()).json()["lights"]["stored"] == {"room_light_exclude": True}


def test_the_masked_form_round_trips_without_losing_the_stored_url(client, db, test_user):
    put(client, test_user, "transit", {"gtfs_feeds": [FEED]})
    shown = client.get(URL, params={"sections": ["transit"]}, headers=_bearer(test_user)).json()["transit"]
    value = shown["value"]
    value["region_name"] = "Denver"
    resp = put(client, test_user, "transit", value, revision=shown["revision"])
    assert resp.status_code == 200
    assert _stored(db, "transit")["value"]["gtfs_feeds"][0]["url"] == FEED["url"]
    assert _stored(db, "transit")["value"]["region_name"] == "Denver"


def test_editing_a_masked_url_in_place_is_422_and_changes_nothing(client, db, test_user):
    put(client, test_user, "transit", {"gtfs_feeds": [FEED]})
    value = client.get(URL, params={"sections": ["transit"]}, headers=_bearer(test_user)).json()["transit"]["value"]
    value["gtfs_feeds"][0]["url"] = "https://feeds.example.org/other.zip"
    before = _row(db, "transit").value
    resp = put(client, test_user, "transit", value, revision=1)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"loc": ["gtfs_feeds", 0, "url"], "type": "url_masked_edit"}]
    assert _row(db, "transit").value == before


def test_a_new_full_url_replaces_the_stored_one(client, db, test_user):
    put(client, test_user, "transit", {"gtfs_feeds": [FEED]})
    value = {"gtfs_feeds": [{**FEED, "url": "https://other.example.org/g.zip?k=2"}]}
    assert put(client, test_user, "transit", value, revision=1).status_code == 200
    assert _stored(db, "transit")["value"]["gtfs_feeds"][0]["url"] == "https://other.example.org/g.zip?k=2"


def test_the_response_to_a_save_is_masked(client, test_user):
    resp = put(client, test_user, "transit", {"gtfs_feeds": [FEED]})
    assert resp.json()["value"]["gtfs_feeds"][0]["url"] == MASKED and "SECRET" not in resp.text


def test_a_transit_audit_row_has_hosts_and_counts_but_no_url_path_or_query(client, db, operator_user):
    put(client, operator_user, "transit", {"gtfs_feeds": [FEED], "region_name": "Denver"})
    (row,) = audits(db, "household_settings_update")
    assert row.new_value["hosts_added"] == ["feeds.example.org"] and row.new_value["counts"]["gtfs_feeds"] == 1
    blob = json.dumps([row.old_value, row.new_value])
    for leaked in ("SECRET", "gtfs.zip", "api_key", "RTD Local Bus", "Denver", "https://"):
        assert leaked not in blob


def test_an_amtrak_audit_row_names_the_field_not_the_station(client, db, operator_user):
    put(client, operator_user, "amtrak", {"default_station": "NYP"})
    (row,) = audits(db, "household_settings_update")
    assert row.new_value == {"section": "amtrak", "changed_fields": ["default_station"], "revision": 1}


def test_a_jarvis_audit_row_has_entities_but_the_room_only_as_changed(client, db, test_user):
    put(client, test_user, "jarvis_web", {"default_room": "Secret Den", "climate_entity": "climate.hall"})
    (row,) = audits(db, "household_settings_update")
    assert row.new_value["climate_entity"] == "climate.hall" and "Secret Den" not in json.dumps(row.new_value)
    assert "default_room" in row.new_value["changed_fields"]


def test_the_stored_text_is_the_documented_shape(client, db, operator_user):
    put(client, operator_user, "gateway", {"fallback_model": "tiny:1b"})
    row = _row(db, "gateway")
    assert row.key == "household.gateway" and row.category == "household"
    assert json.loads(row.value) == {"v": 1, "revision": 1, "value": {"fallback_model": "tiny:1b"}}
    model, revision = parse_stored("gateway", row.value)
    assert model.fallback_model == "tiny:1b" and revision == 1
