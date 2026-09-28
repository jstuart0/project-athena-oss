"""ATHENA-69 Pass H (codex full-diff, Low): guest_restricted_intents was
readable via GET /api/guest-mode/config (a real DB column, default
['tesla']) but had no field on GuestModeConfigCreate/GuestModeConfigUpdate
-- there was no way to set it through the API at all. Covers create,
update, and the validated-intent-name rejection.
"""
from __future__ import annotations

from app.auth.oidc import create_access_token
from app.models import GuestModeConfig

CONFIG_URL = "/api/guest-mode/config"


def _owner_headers(test_user):
    token = create_access_token({"user_id": test_user.id, "username": test_user.username, "role": test_user.role})
    return {"Authorization": f"Bearer {token}"}


def test_create_accepts_guest_restricted_intents(client, db, test_user):
    resp = client.post(
        CONFIG_URL,
        json={"guest_restricted_intents": ["tesla", "control"]},
        headers=_owner_headers(test_user),
    )
    assert resp.status_code == 200
    assert resp.json()["guest_restricted_intents"] == ["tesla", "control"]

    config = db.query(GuestModeConfig).first()
    assert config.guest_restricted_intents == ["tesla", "control"]


def test_update_sets_guest_restricted_intents(client, db, test_user):
    db.add(GuestModeConfig(
        enabled=False, calendar_source="ical", guest_allowed_intents=[],
        guest_restricted_entities=[], guest_allowed_domains=[],
        guest_restricted_intents=["tesla"], created_by_id=test_user.id,
    ))
    db.commit()

    resp = client.patch(
        CONFIG_URL,
        json={"guest_restricted_intents": ["tesla", "music_play"]},
        headers=_owner_headers(test_user),
    )
    assert resp.status_code == 200
    assert resp.json()["guest_restricted_intents"] == ["tesla", "music_play"]

    config = db.query(GuestModeConfig).first()
    assert config.guest_restricted_intents == ["tesla", "music_play"]


def test_update_omitted_guest_restricted_intents_leaves_existing_value(client, db, test_user):
    db.add(GuestModeConfig(
        enabled=False, calendar_source="ical", guest_allowed_intents=[],
        guest_restricted_entities=[], guest_allowed_domains=[],
        guest_restricted_intents=["tesla"], created_by_id=test_user.id,
    ))
    db.commit()

    resp = client.patch(CONFIG_URL, json={"enabled": True}, headers=_owner_headers(test_user))
    assert resp.status_code == 200
    assert resp.json()["guest_restricted_intents"] == ["tesla"]


def test_create_rejects_unknown_intent_name(client, db, test_user):
    resp = client.post(
        CONFIG_URL,
        json={"guest_restricted_intents": ["tesla", "not_a_real_intent"]},
        headers=_owner_headers(test_user),
    )
    assert resp.status_code == 422
    assert db.query(GuestModeConfig).count() == 0


def test_update_rejects_unknown_intent_name(client, db, test_user):
    db.add(GuestModeConfig(
        enabled=False, calendar_source="ical", guest_allowed_intents=[],
        guest_restricted_entities=[], guest_allowed_domains=[],
        guest_restricted_intents=["tesla"], created_by_id=test_user.id,
    ))
    db.commit()

    resp = client.patch(
        CONFIG_URL,
        json={"guest_restricted_intents": ["totally_bogus"]},
        headers=_owner_headers(test_user),
    )
    assert resp.status_code == 422

    config = db.query(GuestModeConfig).first()
    assert config.guest_restricted_intents == ["tesla"]


def test_create_normalizes_case(client, db, test_user):
    resp = client.post(
        CONFIG_URL,
        json={"guest_restricted_intents": ["TESLA", "Control"]},
        headers=_owner_headers(test_user),
    )
    assert resp.status_code == 200
    assert resp.json()["guest_restricted_intents"] == ["tesla", "control"]
