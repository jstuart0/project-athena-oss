"""ATHENA-91 (Campaign 2026-09-27-deliver-athena-transit-and-base-knowledge,
Phase 2): Memory & Context -> Base Knowledge saves and re-loads.

Plan: .mozart/plans/active/2026-09-27-deliver-athena-transit-and-base-knowledge.md
Test contract: same directory,
2026-09-27-deliver-athena-transit-and-base-knowledge.test-contract.md
(r2 final) -- B1-B13.

Mocking strategy: real DB, in-process, SQLite-in-memory via the existing
db/client/test_user/viewer_user fixtures (conftest.py:45-102). The only
seam under test is "does the route's SQLAlchemy read/write actually
round-trip through a real session" -- no mock hides it.
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

from fastapi import HTTPException

from app.auth.oidc import get_current_user
from app.models import BaseKnowledge, SystemSetting
from app.routes.base_knowledge import BaseKnowledgeSettings
from main import app
from shared.config import _clear_cache_for_tests

SETTINGS_URL = "/api/base-knowledge/settings"

DENVER_PAYLOAD = {
    "city": "Denver",
    "state": "CO",
    "latitude": "39.7392",
    "longitude": "-104.9903",
    "timezone": "America/Denver",
    "temp_unit": "F",
    "distance_unit": "mi",
    "date_format": "MM/DD/YYYY",
}


@pytest.fixture(autouse=True)
def _clear_config_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


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
def anon_client(client):
    async def _unauthenticated():
        raise HTTPException(status_code=401, detail="Not authenticated")
    app.dependency_overrides[get_current_user] = _unauthenticated
    yield client


def _location_rows(db):
    return (
        db.query(BaseKnowledge)
        .filter(BaseKnowledge.category == "location", BaseKnowledge.key == "default_location")
        .all()
    )


# ---------------------------------------------------------------------------
# B1/B2/B3/B3b -- round trip, entry consistency, D6 authority
# ---------------------------------------------------------------------------

def test_B1_put_then_get_round_trips(owner_client):
    resp = owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    assert resp.status_code == 200

    resp2 = owner_client.get(SETTINGS_URL)
    assert resp2.status_code == 200
    body = resp2.json()
    for key, value in DENVER_PAYLOAD.items():
        assert body[key] == value
    assert body["default_location"] == "Denver, CO"


def _collect_keys(obj, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            _collect_keys(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_keys(item, found)


def test_B2_single_location_entry_and_no_coords_on_public(owner_client, db):
    owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)

    rows = _location_rows(db)
    assert len(rows) == 1
    assert rows[0].value == "Denver, CO"
    assert rows[0].enabled is True
    assert rows[0].applies_to == "both"

    public_resp = owner_client.get("/api/base-knowledge/public")
    assert public_resp.status_code == 200
    keys_found = set()
    _collect_keys(public_resp.json(), keys_found)
    assert "latitude" not in keys_found
    assert "longitude" not in keys_found


def test_B3_authority_fallback_branch_round_trips_verbatim(owner_client, db):
    owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)

    entry = _location_rows(db)[0]
    entry.value = "1600 Example Ave, Denver"
    db.commit()

    resp = owner_client.get(SETTINGS_URL)
    body = resp.json()
    assert body["city"] == "1600 Example Ave, Denver"
    assert body["state"] == ""

    payload = {k: body[k] for k in BaseKnowledgeSettings.model_fields}
    owner_client.put(SETTINGS_URL, json=payload)

    db.refresh(entry)
    assert entry.value == "1600 Example Ave, Denver"


def test_B3b_authority_primary_branch_splits_city_state(owner_client):
    owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    body = owner_client.get(SETTINGS_URL).json()
    assert body["city"] == "Denver"
    assert body["state"] == "CO"


# ---------------------------------------------------------------------------
# B4/B5 -- D8: PUT fans out to every default_location row
# ---------------------------------------------------------------------------

def test_B4_put_updates_every_default_location_row(owner_client, db):
    owner_row = BaseKnowledge(category="location", key="default_location", value="Old-Owner", applies_to="owner", enabled=True)
    guest_row = BaseKnowledge(category="location", key="default_location", value="Old-Guest", applies_to="guest", enabled=True)
    db.add(owner_row)
    db.add(guest_row)
    db.commit()
    db.refresh(owner_row)
    db.refresh(guest_row)

    # Before any PUT: no 'both' row exists, so D6 falls back to lowest id
    # (owner_row, added first) -- this is the meaningful half of "GET's
    # default_location reads the lowest-id row" (both rows share the same
    # value after the PUT below, so that assertion alone wouldn't
    # distinguish the tie-break rule from coincidence).
    pre_body = owner_client.get(SETTINGS_URL).json()
    assert pre_body["default_location"] == "Old-Owner"

    resp = owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    assert resp.status_code == 200

    rows = _location_rows(db)
    assert len(rows) == 2
    for row in rows:
        assert row.value == "Denver, CO"
        assert row.enabled is True

    assert resp.json()["default_location"] == "Denver, CO"


def test_B5_put_empty_clears_every_default_location_row(owner_client, db):
    owner_row = BaseKnowledge(category="location", key="default_location", value="Old-Owner", applies_to="owner", enabled=True)
    guest_row = BaseKnowledge(category="location", key="default_location", value="Old-Guest", applies_to="guest", enabled=True)
    db.add(owner_row)
    db.add(guest_row)
    db.commit()

    empty_payload = {**DENVER_PAYLOAD, "city": "", "state": ""}
    resp = owner_client.put(SETTINGS_URL, json=empty_payload)
    assert resp.status_code == 200

    rows = _location_rows(db)
    assert len(rows) == 2
    for row in rows:
        assert row.value == ""
        assert row.enabled is False

    body = resp.json()
    assert body["city"] == "" == body["state"]


# ---------------------------------------------------------------------------
# B6 -- validation: string details naming the field, state unchanged after
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_field,bad_value,expected_prefix", [
    ("latitude", "91", "latitude:"),
    ("latitude", "-91", "latitude:"),
    ("longitude", "181", "longitude:"),
    ("latitude", "abc", "latitude:"),
    ("timezone", "Mars/Olympus", "timezone:"),
    ("temp_unit", "K", "temp_unit:"),
    ("city", "x" * 101, "city:"),
    ("date_format", "DD-MM-YYYY", "date_format:"),
])
def test_B6_validation_rejects_bad_field_with_string_detail(owner_client, bad_field, bad_value, expected_prefix):
    owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    baseline = owner_client.get(SETTINGS_URL).json()

    bad_payload = {**DENVER_PAYLOAD, bad_field: bad_value}
    resp = owner_client.put(SETTINGS_URL, json=bad_payload)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith(expected_prefix)

    after = owner_client.get(SETTINGS_URL).json()
    assert after == baseline


def test_B6_list_body_gives_string_detail_not_partial_write(owner_client):
    owner_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    baseline = owner_client.get(SETTINGS_URL).json()

    resp = owner_client.put(SETTINGS_URL, json=["not", "an", "object"])
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)

    after = owner_client.get(SETTINGS_URL).json()
    assert after == baseline


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------

def test_boundary_city_state_at_100_chars_accepted(owner_client):
    payload = {**DENVER_PAYLOAD, "city": "c" * 100, "state": "s" * 100}
    resp = owner_client.put(SETTINGS_URL, json=payload)
    assert resp.status_code == 200


def test_boundary_lat_lon_zero_round_trips_as_string_zero(owner_client):
    payload = {**DENVER_PAYLOAD, "latitude": "0", "longitude": "0"}
    resp = owner_client.put(SETTINGS_URL, json=payload)
    assert resp.status_code == 200
    body = owner_client.get(SETTINGS_URL).json()
    assert body["latitude"] == "0"
    assert body["longitude"] == "0"


# ---------------------------------------------------------------------------
# B7/B8 -- auth and route order
# ---------------------------------------------------------------------------

def test_B7_viewer_forbidden_on_get_and_put(viewer_client):
    resp = viewer_client.get(SETTINGS_URL)
    assert resp.status_code == 403

    resp2 = viewer_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    assert resp2.status_code == 403


def test_B7_unauthenticated_gives_401_or_403_never_405(anon_client):
    resp = anon_client.get(SETTINGS_URL)
    assert resp.status_code in (401, 403)

    resp2 = anon_client.put(SETTINGS_URL, json=DENVER_PAYLOAD)
    assert resp2.status_code in (401, 403)
    assert resp2.status_code != 405


def test_B8_settings_route_registered_before_int_path_param(owner_client):
    resp = owner_client.get(SETTINGS_URL)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# B9 -- frontend/API static parity + toast escaping
# ---------------------------------------------------------------------------

def test_B9_frontend_schema_matches_api_and_uses_settings_endpoint():
    frontend_path = Path(_REPO_ROOT) / "admin" / "frontend" / "memory-context.js"
    text = frontend_path.read_text()

    load_match = re.search(r"async function loadBaseKnowledge\(container\)[\s\S]*?\n    \}\n", text)
    assert load_match, "loadBaseKnowledge function body not found"
    load_fn_text = load_match.group(0)

    save_match = re.search(r"async function saveBaseKnowledge\(\)[\s\S]*?\n    \}\n", text)
    assert save_match, "saveBaseKnowledge function body not found"
    save_fn_text = save_match.group(0)

    data_block_match = re.search(r"const data = \{([\s\S]*?)\};", save_fn_text)
    assert data_block_match, "saveBaseKnowledge's data literal not found"
    data_keys = set(re.findall(r"(\w+):\s*document\.getElementById", data_block_match.group(1)))

    expected_keys = set(BaseKnowledgeSettings.model_fields.keys())
    assert len(expected_keys) == 8
    assert "timezone" in expected_keys
    assert data_keys == expected_keys

    knowledge_reads = set(re.findall(r"knowledge\.(\w+)", text))
    get_keys = expected_keys | {"default_location"}
    assert knowledge_reads
    assert knowledge_reads.issubset(get_keys)

    assert "/api/internal/" not in text

    urls = set(re.findall(r"Athena\.api\('([^']+)'", load_fn_text + save_fn_text))
    assert urls == {"/api/base-knowledge/settings"}

    # M2: the catch passes error.message (not a bare hardcoded string) to
    # Toast.error.
    assert re.search(r"Toast\.error\(error\.message", save_fn_text)


def test_B9_toast_error_escapes_message_before_innerhtml():
    """M2's 'Toast escapes it' claim has no existing regression test:
    neither test_admin_frontend_escaping.py nor test_frontend_guard_scripts.py
    references toast.js (checked directly -- grep for 'toast' in both finds
    only an unrelated synthetic fixture in the guard-scripts file, not the
    real toast.js). If toast.js's show() ever stops wrapping `message` in
    escapeHtml() before it lands in its innerHTML template, an unescaped 422
    detail -- which can echo back a field name derived from this route's own
    request body -- becomes a stored-XSS-shaped path."""
    toast_path = Path(_REPO_ROOT) / "admin" / "frontend" / "toast.js"
    text = toast_path.read_text()
    assert "escapeHtml(message)" in text


# ---------------------------------------------------------------------------
# B10 -- timezone default from DEFAULT_TIMEZONE (D7/M3)
# ---------------------------------------------------------------------------

def test_B10_timezone_default_from_valid_config(owner_client, monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/Denver")
    _clear_cache_for_tests()
    resp = owner_client.get(SETTINGS_URL)
    assert resp.json()["timezone"] == "America/Denver"


def test_B10_invalid_timezone_env_falls_back_to_utc(owner_client, monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Not/AZone")
    _clear_cache_for_tests()
    resp = owner_client.get(SETTINGS_URL)
    assert resp.json()["timezone"] == "UTC"


# ---------------------------------------------------------------------------
# B11 -- corrupt system_settings JSON survives
# ---------------------------------------------------------------------------

def test_B11_corrupt_settings_blob_returns_defaults_not_500(owner_client, db, monkeypatch):
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    _clear_cache_for_tests()

    db.add(SystemSetting(key="base_knowledge_settings", value="{not json", category="base_knowledge"))
    db.commit()

    resp = owner_client.get(SETTINGS_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["timezone"] == "UTC"
    assert body["temp_unit"] == "F"
    assert body["distance_unit"] == "mi"
    assert body["date_format"] == "MM/DD/YYYY"


# ---------------------------------------------------------------------------
# B12 -- rollback atomicity across the two-table split (seeded with B4's
# two-row shape per mozart's note, so the proof covers D8's multi-row write)
# ---------------------------------------------------------------------------

def test_B12_commit_failure_rolls_back_both_stores(owner_client, db, monkeypatch):
    owner_row = BaseKnowledge(category="location", key="default_location", value="Baseline-Owner", applies_to="owner", enabled=True)
    guest_row = BaseKnowledge(category="location", key="default_location", value="Baseline-Guest", applies_to="guest", enabled=True)
    db.add(owner_row)
    db.add(guest_row)
    db.commit()

    baseline_payload = {**DENVER_PAYLOAD, "city": "Baseline City", "state": "BC"}
    owner_client.put(SETTINGS_URL, json=baseline_payload)
    pre_put_body = owner_client.get(SETTINGS_URL).json()
    pre_rows = sorted((r.id, r.value, r.enabled) for r in _location_rows(db))

    original_commit = db.commit
    state = {"raised": False}

    def _raise_once():
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("simulated commit failure")
        return original_commit()

    monkeypatch.setattr(db, "commit", _raise_once)

    new_payload = {**DENVER_PAYLOAD, "city": "New City", "state": "NC"}
    resp = owner_client.put(SETTINGS_URL, json=new_payload)
    assert resp.status_code == 500

    monkeypatch.undo()

    after_body = owner_client.get(SETTINGS_URL).json()
    assert after_body == pre_put_body

    after_rows = sorted((r.id, r.value, r.enabled) for r in _location_rows(db))
    assert after_rows == pre_rows


# ---------------------------------------------------------------------------
# B13 (renamed from r1's B10) -- cold-start GET on a fresh, empty DB
# ---------------------------------------------------------------------------

def test_B13_cold_start_get_returns_documented_defaults(owner_client, monkeypatch):
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    _clear_cache_for_tests()

    resp = owner_client.get(SETTINGS_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["city"] == ""
    assert body["state"] == ""
    assert body["latitude"] == ""
    assert body["longitude"] == ""
    assert body["timezone"] == "UTC"
    assert body["temp_unit"] == "F"
    assert body["distance_unit"] == "mi"
    assert body["date_format"] == "MM/DD/YYYY"
    assert body["default_location"] is None
