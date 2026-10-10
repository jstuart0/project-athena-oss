"""Base-knowledge tiers: write validation, 409 on collisions, audit rows on
every write path, and the all-or-nothing bulk re-tier route.

Real SQLite session via the conftest fixtures; the only override is the
authenticated user.
"""
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

from unittest import mock

from fastapi.routing import APIRoute

from app.auth.oidc import get_current_user
from app.models import AuditLog, BaseKnowledge, User
from app.routes import base_knowledge as bk_routes
from main import app
from shared.knowledge_tiers import KNOWLEDGE_TIERS

URL = "/api/base-knowledge"
BULK_TIER_URL = f"{URL}/bulk-tier"
SETTINGS_URL = f"{URL}/settings"
TIER_DETAIL = "applies_to: must be one of " + ", ".join(KNOWLEDGE_TIERS)
COLLISION_DETAIL = "applies_to: another entry with this category and key already has that audience"
SENTINEL = "AUDIT_SENTINEL"


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client


@pytest.fixture
def viewer_client(client, viewer_user):
    async def _get_user():
        return viewer_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client


@pytest.fixture
def operator_client(client, db):
    user = User(authentik_id="op-001", username="operator", email="op@example.com",
                full_name="Op", role="operator", active=True)
    db.add(user)
    db.commit()
    db.refresh(user)

    async def _get_user():
        return user
    app.dependency_overrides[get_current_user] = _get_user
    yield client


def _payload(**over):
    body = {"category": "property", "key": "wifi", "value": "v", "applies_to": "household"}
    body.update(over)
    return body


def _row(db, category="property", key="wifi", applies_to="both", value="v"):
    row = BaseKnowledge(category=category, key=key, value=value, applies_to=applies_to)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _audits(db, action=None):
    q = db.query(AuditLog).filter(AuditLog.resource_type == "base_knowledge")
    if action:
        q = q.filter(AuditLog.action == action)
    return q.all()


def _count(db):
    return db.query(BaseKnowledge).count()


# ---- validation -----------------------------------------------------------

@pytest.mark.parametrize("tier", KNOWLEDGE_TIERS)
def test_every_tier_accepted_on_create_update_bulk(owner_client, db, tier):
    created = owner_client.post(URL, json=_payload(key=f"k_{tier}", applies_to=tier))
    assert created.status_code == 201 and created.json()["applies_to"] == tier

    row = _row(db, key=f"u_{tier}", applies_to="both")
    updated = owner_client.put(f"{URL}/{row.id}", json={"applies_to": tier})
    assert updated.status_code == 200 and updated.json()["applies_to"] == tier

    bulk = owner_client.post(f"{URL}/bulk", json={"entries": [_payload(key=f"b_{tier}", applies_to=tier)]})
    assert bulk.status_code == 201 and bulk.json()["created_count"] == 1


@pytest.mark.parametrize("tier", ["chat", "OWNER", "", "owner_private"])
def test_bad_tier_is_422_on_create_update_bulk(owner_client, db, tier):
    r = owner_client.post(URL, json=_payload(applies_to=tier))
    assert r.status_code == 422 and r.json()["detail"] == TIER_DETAIL
    row = _row(db, key="existing")
    r = owner_client.put(f"{URL}/{row.id}", json={"applies_to": tier})
    assert r.status_code == 422 and r.json()["detail"] == TIER_DETAIL
    r = owner_client.post(f"{URL}/bulk", json={"entries": [_payload(applies_to=tier)]})
    assert r.status_code == 422 and r.json()["detail"] == "entries[0]." + TIER_DETAIL
    assert _count(db) == 1


@pytest.mark.parametrize("field,value,prefix", [
    ("category", "Career ", "category: "),
    ("category", "a" * 51, "category: "),
    ("category", "car\neer", "category: "),
    ("key", "-bad", "key: "),
    ("key", "k" * 101, "key: "),
    ("key", "has space", "key: "),
])
def test_bad_category_or_key_is_422(owner_client, db, field, value, prefix):
    r = owner_client.post(URL, json=_payload(**{field: value}))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"].startswith(prefix)
    assert _count(db) == 0 and _audits(db) == []


def test_owner_category_rule_named_member(owner_client, db):
    r = owner_client.post(URL, json=_payload(category="owner", key="employer", applies_to="household"))
    assert r.status_code == 422 and r.json()["detail"].startswith("applies_to: ")
    ok = owner_client.post(URL, json=_payload(category="owner", key="employer", applies_to="owner"))
    assert ok.status_code == 201
    name = owner_client.post(URL, json=_payload(category="owner", key="owner_name", applies_to="household"))
    assert name.status_code == 201


def test_editing_a_legacy_owner_category_row_to_a_non_owner_tier_is_422(owner_client, db):
    legacy = _row(db, category="owner", key="favorite_color", applies_to="both")
    r = owner_client.put(f"{URL}/{legacy.id}", json={"applies_to": "guest"})
    assert r.status_code == 422 and r.json()["detail"].startswith("applies_to: ")
    # the UI always sends applies_to: an unchanged audience is not re-judged
    assert owner_client.put(f"{URL}/{legacy.id}", json={"applies_to": "both", "value": "blue"}).status_code == 200
    assert owner_client.put(f"{URL}/{legacy.id}", json={"applies_to": "owner"}).status_code == 200


def test_legacy_row_with_odd_category_and_key_can_be_edited_and_retiered(owner_client, db):
    legacy = _row(db, category=" Owner ", key="Odd Key!", applies_to="owner")
    assert owner_client.put(f"{URL}/{legacy.id}", json={"applies_to": "owner", "value": "x"}).status_code == 200
    r = owner_client.put(f"{URL}/{legacy.id}", json={"applies_to": "household"})
    assert r.status_code == 422 and r.json()["detail"].startswith("applies_to: ")
    plain = _row(db, category="Misc Stuff", key="Odd Key!", applies_to="both")
    assert owner_client.put(f"{URL}/{plain.id}", json={"applies_to": "guest"}).status_code == 200


def test_same_fact_can_exist_once_per_audience(owner_client, db):
    assert owner_client.post(URL, json=_payload(key="k", applies_to="both")).status_code == 201
    assert owner_client.post(URL, json=_payload(key="k", applies_to="guest")).status_code == 201
    assert owner_client.post(URL, json=_payload(key="k", applies_to="guest")).status_code == 409


def test_value_bounds(owner_client, db):
    assert owner_client.post(URL, json=_payload(key="ml", value="line one\n\tline two")).status_code == 201
    assert owner_client.post(URL, json=_payload(key="max", value="x" * 4000)).status_code == 201
    for bad in ("x" * 4001, "a\x00b", "a\rb", "a\x1bb", "a\x7fb"):
        r = owner_client.post(URL, json=_payload(key="bad", value=bad))
        assert r.status_code == 422 and r.json()["detail"].startswith("value: ")
    row = _row(db, key="existing")
    r = owner_client.put(f"{URL}/{row.id}", json={"value": "a\x00b"})
    assert r.status_code == 422 and r.json()["detail"].startswith("value: ")
    r = owner_client.post(f"{URL}/bulk", json={"entries": [_payload(key="b", value="a\x00b")]})
    assert r.status_code == 422 and r.json()["detail"].startswith("entries[0].value: ")


def test_bulk_create_is_capped_at_500_entries(owner_client, db):
    entries = [_payload(key=f"k{i}") for i in range(501)]
    assert owner_client.post(f"{URL}/bulk", json={"entries": entries}).status_code == 422
    assert _count(db) == 0


# ---- demoting out of Owner only needs the owner role -------------------------

def _refusals(db):
    return [a for a in _audits(db) if a.success is False]


def test_operator_cannot_demote_via_update(operator_client, db):
    row = _row(db, key="secret", applies_to="owner")
    before = len(_audits(db))
    r = operator_client.put(f"{URL}/{row.id}", json={"applies_to": "household"})
    assert r.status_code == 403 and r.json()["detail"] == {"error": "insufficient_role"}
    db.expire_all()
    assert db.get(BaseKnowledge, row.id).applies_to == "owner"
    refusals = _refusals(db)
    assert len(_audits(db)) == before + 1 and len(refusals) == 1
    assert refusals[0].error_message == "insufficient_role" and refusals[0].old_value is None


def test_operator_cannot_demote_via_bulk_tier(operator_client, db):
    a = _row(db, key="a", applies_to="owner")
    b = _row(db, key="b", applies_to="guest")
    r = operator_client.post(BULK_TIER_URL, json={"ids": [a.id, b.id], "applies_to": "household"})
    assert r.status_code == 403 and r.json()["detail"] == {"error": "insufficient_role"}
    db.expire_all()
    assert db.get(BaseKnowledge, a.id).applies_to == "owner"
    assert db.get(BaseKnowledge, b.id).applies_to == "guest"
    assert len(_audits(db)) == 1 and len(_refusals(db)) == 1


def test_operator_can_promote_into_owner_and_move_between_other_tiers(operator_client, db):
    a = _row(db, key="a", applies_to="household")
    b = _row(db, key="b", applies_to="guest")
    assert operator_client.put(f"{URL}/{a.id}", json={"applies_to": "owner"}).status_code == 200
    assert operator_client.post(BULK_TIER_URL, json={"ids": [b.id], "applies_to": "owner"}).status_code == 200
    c = _row(db, key="c", applies_to="guest")
    assert operator_client.post(BULK_TIER_URL, json={"ids": [c.id], "applies_to": "household"}).status_code == 200
    assert operator_client.put(f"{URL}/{a.id}", json={"applies_to": "owner", "value": "v2"}).status_code == 200


def test_owner_role_can_demote(owner_client, db):
    row = _row(db, key="secret", applies_to="owner")
    assert owner_client.put(f"{URL}/{row.id}", json={"applies_to": "household"}).status_code == 200
    assert _refusals(db) == []


def test_bulk_with_one_bad_entry_writes_zero_rows(owner_client, db):
    entries = [_payload(key="good"), _payload(key="bad", applies_to="chat")]
    r = owner_client.post(f"{URL}/bulk", json={"entries": entries})
    assert r.status_code == 422 and r.json()["detail"].startswith("entries[1].")
    assert _count(db) == 0 and _audits(db) == []


# ---- 409 ------------------------------------------------------------------

def test_retier_collision_is_409_and_row_unchanged(owner_client, db):
    _row(db, key="k", applies_to="both")
    other = _row(db, key="k", applies_to="guest")
    audits_before = len(_audits(db))
    r = owner_client.put(f"{URL}/{other.id}", json={"applies_to": "both"})
    assert r.status_code == 409 and r.json()["detail"] == COLLISION_DETAIL
    db.expire_all()
    assert db.get(BaseKnowledge, other.id).applies_to == "guest"
    assert len(_audits(db)) == audits_before


def test_create_collision_is_409(owner_client, db):
    _row(db, key="k", applies_to="guest")
    r = owner_client.post(URL, json=_payload(key="k", applies_to="guest"))
    assert r.status_code == 409
    assert _count(db) == 1 and _audits(db) == []


def test_create_db_race_is_409_with_no_audit_row(owner_client, db):
    _row(db, key="k", applies_to="household")
    with mock.patch.object(bk_routes, "_find_existing", return_value=None):
        r = owner_client.post(URL, json=_payload(key="k", applies_to="household"))
    assert r.status_code == 409
    assert _count(db) == 1 and _audits(db) == []


def test_bulk_db_race_is_409_and_all_or_nothing(owner_client, db):
    _row(db, key="dup", applies_to="household")
    entries = [_payload(key="fresh"), _payload(key="dup", applies_to="household")]
    with mock.patch.object(bk_routes, "_find_existing", return_value=None):
        r = owner_client.post(f"{URL}/bulk", json={"entries": entries})
    assert r.status_code == 409
    assert _count(db) == 1 and _audits(db) == []


# ---- bulk-tier ------------------------------------------------------------

def test_bulk_tier_ok(owner_client, db, test_user):
    a = _row(db, key="a", applies_to="owner")
    b = _row(db, key="b", applies_to="owner")
    c = _row(db, key="c", applies_to="guest")
    r = owner_client.post(BULK_TIER_URL, json={"ids": [a.id, b.id, c.id], "applies_to": "household"})
    assert r.status_code == 200 and r.json() == {"updated": 3}
    db.expire_all()
    assert {db.get(BaseKnowledge, i).applies_to for i in (a.id, b.id, c.id)} == {"household"}
    audit = _audits(db, "bulk_tier")
    assert len(audit) == 1
    assert audit[0].user_id == test_user.id
    assert audit[0].new_value == {"ids": sorted([a.id, b.id, c.id]), "old_tiers": {"owner": 2, "guest": 1}, "new_tier": "household"}


def test_bulk_tier_owner_category_row_is_422_all_or_nothing(owner_client, db):
    ok = _row(db, key="a", applies_to="owner")
    locked = _row(db, category="owner", key="employer", applies_to="owner")
    r = owner_client.post(BULK_TIER_URL, json={"ids": [ok.id, locked.id], "applies_to": "household"})
    assert r.status_code == 422
    assert r.json()["detail"] == "applies_to: 1 selected entries are in the owner category and must stay Owner only"
    db.expire_all()
    assert db.get(BaseKnowledge, ok.id).applies_to == "owner"
    assert _audits(db) == []
    name_row = _row(db, category="owner", key="owner_name", applies_to="owner")
    assert owner_client.post(BULK_TIER_URL, json={"ids": [name_row.id], "applies_to": "household"}).status_code == 200


def test_bulk_tier_collision_is_409_and_nothing_changes(owner_client, db):
    a = _row(db, key="a", applies_to="owner")
    b = _row(db, key="b", applies_to="guest")
    _row(db, key="b", applies_to="household")
    audits_before = len(_audits(db))
    r = owner_client.post(BULK_TIER_URL, json={"ids": [a.id, b.id], "applies_to": "household"})
    assert r.status_code == 409 and r.json()["detail"] == COLLISION_DETAIL
    db.expire_all()
    assert db.get(BaseKnowledge, a.id).applies_to == "owner"
    assert db.get(BaseKnowledge, b.id).applies_to == "guest"
    assert len(_audits(db)) == audits_before


@pytest.mark.parametrize("body", [
    {"ids": [], "applies_to": "household"},
    {"ids": [1, 1], "applies_to": "household"},
    {"ids": list(range(1, 502)), "applies_to": "household"},
    {"ids": [0], "applies_to": "household"},
    {"ids": [2**31], "applies_to": "household"},
    {"ids": [1], "applies_to": "chat"},
])
def test_bulk_tier_bad_body_is_422(owner_client, body):
    r = owner_client.post(BULK_TIER_URL, json=body)
    assert r.status_code == 422


def test_bulk_tier_unknown_id_is_404(owner_client):
    assert owner_client.post(BULK_TIER_URL, json={"ids": [9999], "applies_to": "household"}).status_code == 404


def test_bulk_tier_non_owner_role_is_403(viewer_client, db):
    row = _row(db, key="a", applies_to="owner")
    r = viewer_client.post(BULK_TIER_URL, json={"ids": [row.id], "applies_to": "household"})
    assert r.status_code == 403
    db.expire_all()
    assert db.get(BaseKnowledge, row.id).applies_to == "owner"


def test_bulk_tier_service_key_is_401(owner_client, db):
    row = _row(db, key="a", applies_to="owner")
    for key in (os.environ["SERVICE_API_KEY"], "wrong"):
        r = owner_client.post(BULK_TIER_URL, json={"ids": [row.id], "applies_to": "household"},
                              headers={"X-Service-Key": key})
        assert r.status_code == 401


def test_bulk_tier_route_carries_the_write_permission():
    """Walk the app the way FastAPI serves it (0.141 keeps included routers as
    wrappers, so a flat app.routes scan finds nothing) and read the permission
    from wherever the dependency tree records it."""
    from shared.route_walk import dependency_calls, iter_api_routes

    walked = [w for w in iter_api_routes(app) if w.path == BULK_TIER_URL and "POST" in w.methods]
    assert len(walked) == 1, "floor: exactly one POST bulk-tier route is served"
    permissions = [getattr(call, "required_permission", None) for call in dependency_calls(walked[0])]
    assert "write:base_knowledge" in permissions


# ---- audit ----------------------------------------------------------------

def test_every_write_path_writes_an_audit_row_without_values(owner_client, db, test_user):
    write_routes = {
        (m, r.path) for r in bk_routes.router.routes if isinstance(r, APIRoute)
        for m in r.methods if m in {"POST", "PUT", "DELETE"}
    }
    assert len(write_routes) >= 6

    created = owner_client.post(URL, json=_payload(key="c1", value=SENTINEL, applies_to="owner")).json()
    owner_client.put(f"{URL}/{created['id']}", json={"value": SENTINEL + "2", "description": SENTINEL, "priority": 3})
    owner_client.post(f"{URL}/bulk", json={"entries": [_payload(key="b1", value=SENTINEL, applies_to="guest")]})
    owner_client.post(BULK_TIER_URL, json={"ids": [created["id"]], "applies_to": "household"})
    settings = {"city": "Sentinelville", "state": "ZZ", "latitude": "", "longitude": "",
                "timezone": "UTC", "temp_unit": "F", "distance_unit": "mi", "date_format": "MM/DD/YYYY"}
    assert owner_client.put(SETTINGS_URL, json=settings).status_code == 200
    assert owner_client.delete(f"{URL}/{created['id']}").status_code == 204

    rows = _audits(db)
    assert {r.action for r in rows} == {"create", "update", "bulk_create", "bulk_tier", "settings_update", "delete"}
    assert len({r.action for r in rows}) >= 6
    for row in rows:
        assert row.user_id == test_user.id and row.success is True
        blob = json.dumps([row.old_value, row.new_value])
        assert "SENTINEL" not in blob and "Sentinelville" not in blob
        for side in (row.old_value, row.new_value):
            assert not {"value", "description", "category", "key"} & set(side or {})
    settings_row = next(r for r in rows if r.action == "settings_update")
    assert settings_row.new_value["changed_fields"] == ["city", "state"]
    assert settings_row.new_value["location_rows"] == 1
    update_row = next(r for r in rows if r.action == "update")
    assert update_row.new_value["changed_fields"] == ["value", "priority", "description"]


def test_operator_cannot_delete_an_owner_tier_row(operator_client, db):
    row = _row(db, key="secret", applies_to="owner")
    r = operator_client.delete(f"{URL}/{row.id}")
    assert r.status_code == 403 and r.json()["detail"] == {"error": "insufficient_role"}
    assert db.get(BaseKnowledge, row.id) is not None
    refusals = _refusals(db)
    assert len(_audits(db)) == 1 and len(refusals) == 1 and refusals[0].action == "delete"


def test_operator_can_delete_other_tiers(operator_client, db):
    other = _row(db, key="a", applies_to="household")
    assert operator_client.delete(f"{URL}/{other.id}").status_code == 204
    assert db.get(BaseKnowledge, other.id) is None
    assert _refusals(db) == []


def test_owner_role_can_delete_an_owner_tier_row(owner_client, db):
    secret = _row(db, key="b", applies_to="owner")
    assert owner_client.delete(f"{URL}/{secret.id}").status_code == 204
    assert db.get(BaseKnowledge, secret.id) is None


@pytest.mark.parametrize("category", ["property", "general"])
def test_owner_name_keys_outside_owner_or_user_category_are_422(owner_client, db, category):
    for key in ("owner_name", "name"):
        r = owner_client.post(URL, json=_payload(category=category, key=key, applies_to="household"))
        assert r.status_code == 422 and r.json()["detail"].startswith("key: ")
    assert _count(db) == 0
    assert owner_client.post(URL, json=_payload(category="user", key="name", applies_to="household")).status_code == 201


def test_legacy_property_name_row_can_still_be_retiered(owner_client, db):
    legacy = _row(db, category="property", key="name", applies_to="both")
    assert owner_client.put(f"{URL}/{legacy.id}", json={"applies_to": "guest"}).status_code == 200
    owner_name = _row(db, category="owner", key="owner_name", applies_to="owner")
    assert owner_client.put(f"{URL}/{owner_name.id}", json={"applies_to": "household"}).status_code == 200
