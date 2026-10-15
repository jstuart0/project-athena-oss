"""The SMS stay match uses the household country code: the admin-saved value
(owner-only setting), else SMS_DEFAULT_COUNTRY_CODE, else 1.

A booking stored as a national number is only matched once the right country
code is applied to it, so each case plants a booking that matches under one
code and not the other.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.routes.sms_webhook as sw
from app.models import CalendarEvent, SystemSetting
from app.services.household_settings import resolve_sms_country_code
from shared.config import _clear_cache_for_tests
from shared.household_settings import SECTIONS, stored_value, validate_update

T = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
UK_SENDER = "+442079460000"
UK_BOOKING_NATIONAL = "020 7946 0000"
_counter = iter(range(10_000))


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.delenv("SMS_DEFAULT_COUNTRY_CODE", raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def _stay(db, phone):
    event = CalendarEvent(
        external_id=f"ev-{next(_counter)}", checkin=T - timedelta(days=1), checkout=T + timedelta(days=1),
        status="confirmed", guest_phone=phone, guest_name="Guest", source="manual",
    )
    db.add(event)
    db.commit()
    return event


def _store_admin_code(db, code, revision=1):
    model = validate_update("sms", {"default_country_code": code})
    db.add(SystemSetting(key=SECTIONS["sms"].key, value=stored_value("sms", model, revision), category="household"))
    db.commit()


def _match(db):
    return sw.find_guest_by_phone(UK_SENDER, db, now=T)


def test_nothing_stored_uses_the_default_code_and_a_national_uk_booking_does_not_match(db):
    code = resolve_sms_country_code(db)
    assert code == "1" and code.source == "default"
    _stay(db, UK_BOOKING_NATIONAL)
    assert _match(db) is None


def test_the_admin_code_makes_the_national_booking_match(db):
    _stay(db, UK_BOOKING_NATIONAL)
    _store_admin_code(db, "44")
    code = resolve_sms_country_code(db)
    assert code == "44" and code.source == "admin"
    match = _match(db)
    assert match is not None and match[1] == "current"


def test_a_plus_prefixed_stored_code_is_normalised(db):
    _store_admin_code(db, "+44")
    assert resolve_sms_country_code(db) == "44"


def test_the_admin_code_beats_the_environment(db, monkeypatch):
    monkeypatch.setenv("SMS_DEFAULT_COUNTRY_CODE", "33")
    _clear_cache_for_tests()
    assert resolve_sms_country_code(db).source == "env"
    _store_admin_code(db, "44")
    assert resolve_sms_country_code(db) == "44"


def test_the_environment_alone_still_applies(db, monkeypatch):
    monkeypatch.setenv("SMS_DEFAULT_COUNTRY_CODE", "44")
    _clear_cache_for_tests()
    code = resolve_sms_country_code(db)
    assert code == "44" and code.source == "env"
    _stay(db, UK_BOOKING_NATIONAL)
    assert _match(db) is not None


def test_a_corrupt_stored_value_falls_back_to_the_environment(db, monkeypatch):
    monkeypatch.setenv("SMS_DEFAULT_COUNTRY_CODE", "44")
    _clear_cache_for_tests()
    db.add(SystemSetting(key=SECTIONS["sms"].key, value='{"v":1,"revision":1,"value":{"default_country_code":"abc"}}',
                         category="household"))
    db.commit()
    assert resolve_sms_country_code(db) == "44"
