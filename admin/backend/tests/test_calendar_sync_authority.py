"""A Lodgify source with a configured API key writes only from the API:
an API failure or an unreadable key writes nothing and never reads the iCal
export. API reservation keys are scoped to their source. Errors and logs
never carry exception text (feed URLs and API keys live in it)."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog

from app.models import CalendarEvent, CalendarSource, ExternalAPIKey
from shared import config as config_module
from shared.booking_window import localize_stay, resolve_property_tz
from tests.conftest import TestingSessionLocal

ICAL = "app.routes.calendar_sources.fetch_ical_data"
API = "app.routes.calendar_sources.fetch_lodgify_reservations"
PLAINTEXT_KEY = "plaintext-lodgify-key-value"
REKEY_WARNING_1 = "1 events stored under a source-specific ID because their IDs are used elsewhere"


def _derived(source_id, uid):
    return f"src:{source_id}:" + hashlib.sha256(uid.encode()).hexdigest()[:32]


def _nine_event_feed():
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for i in range(9):
        start = date(2026, 10, 1) + timedelta(days=3 * i)
        lines += [
            "BEGIN:VEVENT", f"UID:feed-{i}@example.com",
            f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{start + timedelta(days=2):%Y%m%d}",
            "SUMMARY:J*** D**", "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"


NINE_FEED = _nine_event_feed()


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


def _api_event(res_id, arrival, departure, name="Guest"):
    tz, _ = resolve_property_tz("America/New_York")
    checkin, checkout = localize_stay(arrival, departure, "16:00", "11:00", tz)
    return {
        "external_id": f"lodgify_{res_id}", "title": f"Lodgify Booking - {name}",
        "checkin": checkin, "checkout": checkout, "guest_name": name, "guest_email": None,
        "guest_phone": None, "notes": "Source: Lodgify", "source": "lodgify",
        "status": "confirmed", "is_manual_block": False,
    }


def _source(db, **kw):
    defaults = dict(name="Lodgify", source_type="lodgify", ical_url="https://www.lodgify.com/export/x.ics")
    defaults.update(kw)
    s = CalendarSource(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _owner(db):
    from app.models import User

    user = db.query(User).filter(User.username == "key-owner").first()
    if user is None:
        user = User(authentik_id="key-owner", username="key-owner", email="k@example.com", role="owner", active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _key(db, *, ciphertext=None, plaintext=PLAINTEXT_KEY, enabled=True, key_type=None):
    from app.utils.encryption import encrypt_value

    row = ExternalAPIKey(
        service_name="lodgify", api_name="Lodgify",
        api_key_encrypted=ciphertext if ciphertext is not None else encrypt_value(plaintext),
        endpoint_url="https://api.lodgify.example", enabled=enabled, key_type=key_type,
        created_by_id=_owner(db).id,
    )
    db.add(row)
    db.commit()
    return row


def _event(db, **kw):
    defaults = dict(
        source="lodgify", status="confirmed", created_by="lodgify_api_sync", title="Seeded",
        checkin=datetime(2026, 11, 1, 20, tzinfo=timezone.utc), checkout=datetime(2026, 11, 3, 15, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    e = CalendarEvent(**defaults)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _snapshot(db, event_id):
    db.expire_all()
    row = db.get(CalendarEvent, event_id)
    return {c.name: getattr(row, c.name) for c in CalendarEvent.__table__.columns}


def _count(db):
    db.expire_all()
    return db.query(CalendarEvent).count()


async def _run(source_id, db):
    from app.services import calendar_sync

    return await calendar_sync.run_source_sync(source_id, db, trigger="manual")


# ---------------------------------------------------------------------------
# (a) / (b) API failure writes nothing and never reads iCal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_timeout_writes_nothing(db):
    source = _source(db)
    _key(db)
    seeded = _event(db, external_id="lodgify_1", source_id=source.id)
    before = _snapshot(db, seeded.id)

    with patch(API, new=AsyncMock(side_effect=httpx.ReadTimeout("timed out"))), \
            patch(ICAL, new=AsyncMock(return_value=NINE_FEED)) as ical:
        outcome = await _run(source.id, db)

    assert outcome.status == "failed"
    assert "no changes written" in outcome.error
    assert ical.await_count == 0
    assert _count(db) == 1
    assert _snapshot(db, seeded.id) == before
    db.refresh(source)
    assert source.last_sync_status == "failed"
    assert source.last_sync_error.startswith("Lodgify API sync failed (ReadTimeout)")
    assert source.last_sync_at is not None


def test_api_timeout_through_the_route_writes_nothing(owner_client, db):
    source = _source(db)
    _key(db)
    with patch(API, new=AsyncMock(side_effect=httpx.ReadTimeout("timed out"))), \
            patch(ICAL, new=AsyncMock(return_value=NINE_FEED)) as ical:
        resp = owner_client.post(f"/api/calendar-sources/{source.id}/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False, body
    assert "no changes written" in body["message"]
    assert not body["message"].startswith("Sync failed")
    assert ical.await_count == 0
    assert _count(db) == 0


# ---------------------------------------------------------------------------
# (c) key edges -> unreadable, (d) absent, (e) host inference
# ---------------------------------------------------------------------------

def _bad_key_setups():
    return {
        "not_decryptable": lambda db: _key(db, ciphertext="not-decryptable"),
        "empty_ciphertext": lambda db: _key(db, ciphertext=""),
        "decrypts_to_blank": lambda db: _key(db, plaintext="   "),
        "two_enabled_one_bad": lambda db: (_key(db, key_type="a"), _key(db, ciphertext="not-decryptable", key_type="b")),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_bad_key_setups()))
async def test_unreadable_key_fails_closed(db, case):
    from app.routes.calendar_sources import resolve_lodgify_api_key

    source = _source(db)
    _bad_key_setups()[case](db)
    assert resolve_lodgify_api_key(db) == ("unreadable", None)

    with structlog.testing.capture_logs() as logs, \
            patch(API, new=AsyncMock(return_value=[])) as api, \
            patch(ICAL, new=AsyncMock(return_value=NINE_FEED)) as ical:
        outcome = await _run(source.id, db)
    assert outcome.status == "failed"
    assert api.await_count == 0 and ical.await_count == 0
    assert _count(db) == 0
    assert PLAINTEXT_KEY not in repr(logs)


@pytest.mark.asyncio
async def test_decrypt_returning_none_is_unreadable(db):
    from app.routes.calendar_sources import resolve_lodgify_api_key

    source = _source(db)
    _key(db)
    with patch("app.utils.encryption.decrypt_value", return_value=None):
        assert resolve_lodgify_api_key(db) == ("unreadable", None)
        with patch(ICAL, new=AsyncMock(return_value=NINE_FEED)) as ical:
            outcome = await _run(source.id, db)
    assert outcome.status == "failed" and ical.await_count == 0 and _count(db) == 0


def test_multiple_ok_keys_use_the_lowest_id(db):
    from app.routes.calendar_sources import resolve_lodgify_api_key

    _key(db, plaintext="first", key_type="a")
    _key(db, plaintext="second", key_type="b")
    assert resolve_lodgify_api_key(db) == ("ok", "first")


@pytest.mark.asyncio
async def test_only_disabled_key_is_absent_and_ical_is_used(db):
    from app.routes.calendar_sources import resolve_lodgify_api_key

    source = _source(db)
    _key(db, enabled=False)
    assert resolve_lodgify_api_key(db) == ("absent", None)
    with patch(API, new=AsyncMock(return_value=[])) as api, patch(ICAL, new=AsyncMock(return_value=NINE_FEED)):
        outcome = await _run(source.id, db)
    assert outcome.status == "success" and outcome.method == "ical"
    assert api.await_count == 0
    assert _count(db) == 9


@pytest.mark.asyncio
async def test_generic_source_on_lodgify_host_with_key_uses_api(db):
    source = _source(db, source_type="generic_ical", ical_url="https://www.lodgify.com/export/y.ics")
    _key(db)
    events = [_api_event(7, date(2026, 10, 1), date(2026, 10, 4))]
    with patch(API, new=AsyncMock(return_value=events)) as api, patch(ICAL, new=AsyncMock(return_value=NINE_FEED)) as ical:
        outcome = await _run(source.id, db)
    assert outcome.status == "success" and outcome.method == "lodgify_api"
    assert api.await_count == 1 and ical.await_count == 0
    assert _count(db) == 1


# ---------------------------------------------------------------------------
# (f) token leak
# ---------------------------------------------------------------------------

def test_ical_error_text_never_leaks(owner_client, db):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/x.ics?token=SECRET123")
    exc = httpx.ConnectError("connect failed for https://x/feed?token=SECRET123")
    with structlog.testing.capture_logs() as logs, patch(ICAL, new=AsyncMock(side_effect=exc)):
        resp = owner_client.post(f"/api/calendar-sources/{source.id}/sync")
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    db.refresh(source)
    assert "SECRET123" not in resp.text
    assert "SECRET123" not in (source.last_sync_error or "")
    assert "SECRET123" not in repr(logs)
    assert source.last_sync_error == "iCal fetch failed (ConnectError); no changes written"


def test_api_error_text_never_leaks(owner_client, db):
    source = _source(db)
    _key(db)
    request = httpx.Request(
        "GET", "https://api.lodgify.example/v1/reservation?token=SECRET123", headers={"X-ApiKey": "SECRET123"},
    )
    exc = httpx.HTTPStatusError(
        "Server error '500' for url 'https://api.lodgify.example/v1/reservation?token=SECRET123'",
        request=request, response=httpx.Response(500, request=request),
    )
    with structlog.testing.capture_logs() as logs, \
            patch(API, new=AsyncMock(side_effect=exc)), patch(ICAL, new=AsyncMock(return_value=NINE_FEED)):
        resp = owner_client.post(f"/api/calendar-sources/{source.id}/sync")
    db.refresh(source)
    assert "SECRET123" not in resp.text
    assert "SECRET123" not in (source.last_sync_error or "")
    assert "SECRET123" not in repr(logs)
    assert "HTTP 500" in source.last_sync_error
    assert "HTTP 500" in resp.json()["message"]
    assert source.last_sync_error == (
        "Lodgify API sync failed (HTTPStatusError HTTP 500); no changes written, existing bookings kept"
    )
    assert any(e["event"] == "calendar_sync_lodgify_api_failed" for e in logs)


# ---------------------------------------------------------------------------
# (g) sync-all is a manual trigger: syncs even when not due
# ---------------------------------------------------------------------------

def test_sync_all_syncs_a_recently_synced_source(owner_client, db, monkeypatch):
    from app.services import calendar_sync

    monkeypatch.setattr(calendar_sync, "SessionLocal", TestingSessionLocal)
    _source(
        db, source_type="generic_ical", ical_url="https://feed.example.com/g.ics",
        last_sync_at=datetime.now(timezone.utc) - timedelta(minutes=1), sync_interval_minutes=30,
    )
    with patch(ICAL, new=AsyncMock(return_value=NINE_FEED)) as ical:
        resp = owner_client.post("/api/calendar-sources/sync-all")
    assert resp.status_code == 200
    assert ical.await_count == 1


# ---------------------------------------------------------------------------
# (h) API scoping, (i) orphan adoption
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_reservation_never_touches_another_sources_row(db):
    g = _source(db, name="G", source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    s = _source(db)
    _key(db)
    g_row = _event(db, external_id="lodgify_42", source_id=g.id, source="generic_ical", created_by="ical_sync")
    before = _snapshot(db, g_row.id)

    events = [_api_event(42, date(2026, 12, 1), date(2026, 12, 5))]
    with patch(API, new=AsyncMock(return_value=events)):
        outcome = await _run(s.id, db)

    assert outcome.status == "success"
    assert _snapshot(db, g_row.id) == before
    mine = db.query(CalendarEvent).filter(CalendarEvent.source_id == s.id).one()
    assert mine.external_id == _derived(s.id, "lodgify_42")
    assert mine.status == "confirmed"
    assert outcome.rekeyed == 1
    assert outcome.warning == REKEY_WARNING_1
    db.refresh(s)
    assert s.last_sync_error == REKEY_WARNING_1

    # A second sync hits the derived row: no new row, no new rekey.
    with patch(API, new=AsyncMock(return_value=events)):
        again = await _run(s.id, db)
    assert again.added == 0 and again.updated == 1 and again.rekeyed == 0
    assert _count(db) == 2
    db.refresh(s)
    assert s.last_sync_error is None


@pytest.mark.asyncio
async def test_api_orphan_is_adopted(db):
    s = _source(db)
    _key(db)
    orphan = _event(db, external_id="lodgify_43", source_id=None, created_by="lodgify_api_sync")
    events = [_api_event(43, date(2026, 12, 10), date(2026, 12, 14))]
    with patch(API, new=AsyncMock(return_value=events)):
        outcome = await _run(s.id, db)
    assert outcome.adopted == 1 and outcome.added == 0
    db.expire_all()
    row = db.get(CalendarEvent, orphan.id)
    assert row.source_id == s.id
    assert row.status == "confirmed"
    assert row.checkin.date() == date(2026, 12, 10)
    assert _count(db) == 1


@pytest.mark.asyncio
async def test_ical_orphan_is_not_adopted(db):
    s = _source(db)
    _key(db)
    orphan = _event(db, external_id="lodgify_43", source_id=None, created_by="ical_sync")
    before = _snapshot(db, orphan.id)
    events = [_api_event(43, date(2026, 12, 10), date(2026, 12, 14))]
    with patch(API, new=AsyncMock(return_value=events)):
        outcome = await _run(s.id, db)
    assert outcome.adopted == 0 and outcome.rekeyed == 1
    assert _snapshot(db, orphan.id) == before
    mine = db.query(CalendarEvent).filter(CalendarEvent.source_id == s.id).one()
    assert mine.external_id == _derived(s.id, "lodgify_43")
    assert mine.status == "confirmed"


@pytest.mark.asyncio
async def test_api_insert_falls_back_when_the_derived_key_is_taken(db):
    g = _source(db, name="G", source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    s = _source(db)
    _key(db)
    g_row = _event(db, external_id="lodgify_44", source_id=g.id, source="generic_ical", created_by="ical_sync")
    squatter = _event(db, external_id=_derived(s.id, "lodgify_44"), source_id=None, created_by="ical_sync",
                      source="generic_ical")
    before = {r.id: _snapshot(db, r.id) for r in (g_row, squatter)}
    events = [_api_event(44, date(2026, 12, 20), date(2026, 12, 23))]
    with patch(API, new=AsyncMock(return_value=events)):
        outcome = await _run(s.id, db)
    assert outcome.status == "success", outcome
    assert outcome.added == 1 and outcome.rekeyed == 1
    for rid, snap in before.items():
        assert _snapshot(db, rid) == snap
    mine = db.query(CalendarEvent).filter(CalendarEvent.source_id == s.id).one()
    assert mine.external_id.startswith(f"ical-nouid:{s.id}:")
    assert mine.status == "confirmed" and mine.created_by == "lodgify_api_sync"
