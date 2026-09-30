"""An SMS matches a stay only on an exact E.164 number, is answered once per
Twilio message, and never loses a retried reply.

- ``to_e164``: the one number normaliser for matching (strict for the
  sender, lenient for the booking's stored number).
- ``find_guest_by_phone``: confirmed, not-deleted stays only, in three tiers
  (current, recent, upcoming), always with an explicit ``now``. Wrong
  answers are inserted first, so an unordered ``.first()`` can't pass.
- The webhook: SID validation, replay bound to the sender, the in-progress
  retry (wait, then 503 + Retry-After, never an empty 200), and plain-text
  ``response_content`` on every terminal path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.routes.sms_webhook as sw
from app.database import get_db
from app.models import CalendarEvent, SMSIncoming
from main import app
from shared.config import AthenaConfig
from tests.conftest import TestingSessionLocal
from tests.test_sms_webhook_signature import BASE, INCOMING_PATH, TOKEN, _encode_form, _sign, _signing_url

T = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
SID = "SM" + "0123456789abcdef" * 2
SID2 = "SM" + "fedcba9876543210" * 2
P_A = "+15550123456"


# ---------------------------------------------------------------------------
# to_e164
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "", None, "+", "++15550123456", "+15550123456 x12", "ATHENA", "Athena-1", "12345", "+12345",
    "+１５５５０１２３４５６",  # full-width digits
    "+555012345",          # 9 digits
    "+1234567890123456",   # 16 digits
])
def test_to_e164_rejects(raw):
    assert sw.to_e164(raw, "1") is None


@pytest.mark.parametrize("raw,cc,expected", [
    ("+5550123456", "1", "+5550123456"),              # 10 digits
    ("+123456789012345", "1", "+123456789012345"),    # 15 digits
    (" \t+1 555 012 3456\t", "1", "+15550123456"),
    ("1 555 012 3456", "1", "+15550123456"),
    ("(555) 012-3456", "1", "+15550123456"),
    ("555.012.3456", "1", "+15550123456"),
    ("5550123456", "1", "+15550123456"),
    ("00442079460000", "1", "+442079460000"),
    ("020 7946 0000", "44", "+442079460000"),
    ("04941 123456", "49", "+494941123456"),          # a trunk 0 always gets the country code
])
def test_to_e164_normalises(raw, cc, expected):
    assert sw.to_e164(raw, cc) == expected


def test_to_e164_strict_requires_a_plus():
    assert sw.to_e164("5550123456", "1", strict=True) is None
    assert sw.to_e164("00442079460000", "1", strict=True) is None
    assert sw.to_e164("+15550123456", "1", strict=True) == "+15550123456"


# ---------------------------------------------------------------------------
# SMS_DEFAULT_COUNTRY_CODE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["+1", "abcd", "1234", ""])
def test_country_code_falls_back_with_one_error(monkeypatch, caplog, value):
    monkeypatch.setenv("SMS_DEFAULT_COUNTRY_CODE", value)
    with caplog.at_level(logging.ERROR, logger="shared.config"):
        cfg = AthenaConfig()
    assert cfg.sms_default_country_code == "1"
    assert sum("sms_default_country_code_invalid" in r.getMessage() for r in caplog.records) == 1


def test_country_code_accepts_digits(monkeypatch, caplog):
    monkeypatch.setenv("SMS_DEFAULT_COUNTRY_CODE", "44")
    with caplog.at_level(logging.ERROR, logger="shared.config"):
        cfg = AthenaConfig()
    assert cfg.sms_default_country_code == "44"
    assert not any("sms_default_country_code_invalid" in r.getMessage() for r in caplog.records)


def test_country_code_default(monkeypatch):
    monkeypatch.delenv("SMS_DEFAULT_COUNTRY_CODE", raising=False)
    assert AthenaConfig().sms_default_country_code == "1"


# ---------------------------------------------------------------------------
# find_guest_by_phone
# ---------------------------------------------------------------------------

_counter = iter(range(10_000))


def _stay(db, *, phone, checkin, checkout, status="confirmed", deleted_at=None, name="Guest"):
    ev = CalendarEvent(external_id=f"ev-{next(_counter)}", checkin=checkin, checkout=checkout, status=status,
                       deleted_at=deleted_at, guest_phone=phone, guest_name=name, source="manual")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _find(db, number, now=T):
    return sw.find_guest_by_phone(number, db, now=now)


def _ids(result):
    return None if result is None else (result[0].id, result[1])


CURRENT = dict(checkin=T - timedelta(days=1), checkout=T + timedelta(days=1))


def test_only_a_confirmed_stay_matches(db):
    blocked = _stay(db, phone=P_A, status="blocked", **CURRENT)
    assert _find(db, P_A) is None, "a blocked stay must never match"
    confirmed = _stay(db, phone="+1 (555) 012-3456", **CURRENT)
    assert _ids(_find(db, P_A)) == (confirmed.id, "current")
    assert blocked.id != confirmed.id


def test_cancelled_and_deleted_never_match(db):
    _stay(db, phone=P_A, status="cancelled", **CURRENT)
    _stay(db, phone=P_A, deleted_at=T - timedelta(hours=1), **CURRENT)
    assert _find(db, P_A) is None


@pytest.mark.parametrize("sender", ["", "+11111111111", "11111111111", "12345", "ATHENA"])
def test_empty_booking_number_and_bad_senders_never_match(db, sender):
    _stay(db, phone="", **CURRENT)
    _stay(db, phone=None, **CURRENT)
    assert _find(db, sender) is None


def test_substring_of_a_booking_number_never_matches(db):
    _stay(db, phone="+15550123456", **CURRENT)
    assert _find(db, "+1555012345") is None
    assert _find(db, "+155501234567") is None


def test_national_booking_number_matches_the_e164_sender(db):
    c = _stay(db, phone="5550199999", **CURRENT)
    assert _ids(_find(db, "+15550199999")) == (c.id, "current")


def test_duplicate_booking_earlier_checkout_wins(db):
    later = _stay(db, phone=P_A, checkin=T - timedelta(days=1), checkout=T + timedelta(days=3))
    earlier = _stay(db, phone=P_A, checkin=T - timedelta(days=2), checkout=T + timedelta(hours=2))
    assert _ids(_find(db, P_A)) == (earlier.id, "current")
    assert later.id != earlier.id


@pytest.mark.parametrize("checkin,checkout,tier", [
    (T - timedelta(days=2), T, "current"),
    (T - timedelta(days=2), T - timedelta(hours=24), "recent"),
    (T - timedelta(days=2), T - timedelta(hours=24, seconds=1), None),
    (T + timedelta(hours=48), T + timedelta(days=4), "upcoming"),
    (T + timedelta(hours=48, seconds=1), T + timedelta(days=4), None),
])
def test_tier_boundaries(db, checkin, checkout, tier):
    ev = _stay(db, phone=P_A, checkin=checkin, checkout=checkout)
    assert _ids(_find(db, P_A)) == (None if tier is None else (ev.id, tier))


def test_changeover_day(db):
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    p1, p2 = "+15550000001", "+15550000002"
    arriving = _stay(db, phone=p2, checkin=day + timedelta(hours=16), checkout=day + timedelta(days=3))
    departing = _stay(db, phone=p1, checkin=day - timedelta(days=3), checkout=day + timedelta(hours=11))
    noon = day + timedelta(hours=12)
    assert _ids(_find(db, p1, noon)) == (departing.id, "recent")
    assert _ids(_find(db, p2, noon)) == (arriving.id, "upcoming")
    assert _ids(_find(db, p2, day + timedelta(hours=10))) == (arriving.id, "upcoming")


def test_changeover_repeat_guest_gets_the_current_stay(db):
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    p1 = "+15550000001"
    arriving = _stay(db, phone=p1, checkin=day + timedelta(hours=16), checkout=day + timedelta(days=3))
    _stay(db, phone=p1, checkin=day - timedelta(days=3), checkout=day + timedelta(hours=11))
    assert _ids(_find(db, p1, day + timedelta(hours=17))) == (arriving.id, "current")


def test_no_query_for_a_sender_that_isnt_e164(db, monkeypatch):
    queried = []
    monkeypatch.setattr(db, "query", lambda *a, **k: queried.append(a) or (_ for _ in ()).throw(AssertionError("queried")))
    assert sw.find_guest_by_phone("ATHENA", db, now=T) is None
    assert queried == []


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------

@pytest.fixture
def signed(monkeypatch, db):
    """Signed mode, a fresh session per request (as in production), and the
    poll's session factory on the test database."""
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", BASE)
    monkeypatch.setattr(sw, "SessionLocal", TestingSessionLocal, raising=False)

    def _get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


def _params(**kw):
    params = {"From": P_A, "Body": "what's the wifi password?", "MessageSid": SID, "To": "", "NumMedia": "0"}
    params.update(kw)
    return params


def _signed_body(params):
    return _encode_form(params.items()), _sign(_signing_url(INCOMING_PATH), params)


async def _apost(client, params, *, sign=True):
    body, sig = _signed_body(params)
    headers = {"content-type": "application/x-www-form-urlencoded"}
    if sign:
        headers["X-Twilio-Signature"] = sig
    return await client.post(INCOMING_PATH, content=body, headers=headers)


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _send(params, *, sign=True):
    async def _go():
        async with _client() as c:
            return await _apost(c, params, sign=sign)
    return asyncio.run(_go())


def _rows(db):
    db.expire_all()
    return db.query(SMSIncoming).order_by(SMSIncoming.id).all()


def _recording_orchestrator(monkeypatch, reply="the reply"):
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return reply

    monkeypatch.setattr(sw, "route_to_orchestrator", fake)
    return calls


def test_alphanumeric_sender_gets_the_unknown_sender_reply(db, signed, monkeypatch):
    calls = _recording_orchestrator(monkeypatch)
    resp = _send(_params(From="ATHENA"))
    assert resp.status_code == 200
    assert "I don't recognize this number" in resp.text
    (row,) = _rows(db)
    assert row.matched_guest is False
    assert row.response_sent is True
    assert row.response_content.startswith("Hi! I'm Athena")
    assert "<Response>" not in row.response_content
    assert calls == []


def test_recent_stay_reaches_the_orchestrator_as_recent(db, signed, monkeypatch):
    now = datetime.now(timezone.utc)
    _stay(db, phone=P_A, checkin=now - timedelta(days=3), checkout=now - timedelta(hours=2))
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"answer": "checkout was at 11"})

    real_async_client = httpx.AsyncClient

    class _Httpx:
        AsyncClient = staticmethod(lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw))

    monkeypatch.setattr(sw, "httpx", _Httpx)
    resp = _send(_params())
    assert resp.status_code == 200, resp.text
    assert "checkout was at 11" in resp.text
    (payload,) = seen
    assert payload["context"]["stay_phase"] == "recent"
    assert payload["caller_trust"] == "sms"


def test_replay_returns_the_first_reply_without_a_second_call(db, signed, monkeypatch):
    _stay(db, phone=P_A, checkin=datetime.now(timezone.utc) - timedelta(days=1),
          checkout=datetime.now(timezone.utc) + timedelta(days=1))
    calls = _recording_orchestrator(monkeypatch, reply="first reply")
    first = _send(_params())
    second = _send(_params())
    assert first.status_code == second.status_code == 200
    assert "first reply" in second.text
    assert len(calls) == 1
    assert len(_rows(db)) == 1


def test_replay_is_bound_to_the_sender(db, signed, monkeypatch):
    _stay(db, phone=P_A, checkin=datetime.now(timezone.utc) - timedelta(days=1),
          checkout=datetime.now(timezone.utc) + timedelta(days=1))
    _recording_orchestrator(monkeypatch, reply="private reply")
    assert _send(_params()).status_code == 200
    other = _send(_params(From="+15559998888"))
    assert other.status_code == 200
    assert "private reply" not in other.text
    assert "I don't recognize this number" in other.text
    assert len(_rows(db)) == 2


def test_unsigned_mode_replay_never_echoes(db, signed, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED", "true")
    sw.get_config.cache_clear()
    try:
        _stay(db, phone=P_A, checkin=datetime.now(timezone.utc) - timedelta(days=1),
              checkout=datetime.now(timezone.utc) + timedelta(days=1))
        calls = _recording_orchestrator(monkeypatch, reply="private reply")
        first = _send(_params(), sign=False)
        assert first.status_code == 200 and "private reply" in first.text
        second = _send(_params(), sign=False)
        assert second.status_code == 200
        assert "private reply" not in second.text
        assert "<Message>" not in second.text
        assert len(calls) == 1
    finally:
        sw.get_config.cache_clear()


@pytest.mark.parametrize("set_during_wait", [False, True], ids=["reply-late-503", "reply-arrives-200"])
def test_in_progress_retry(db, signed, monkeypatch, set_during_wait):
    _stay(db, phone=P_A, checkin=datetime.now(timezone.utc) - timedelta(days=1),
          checkout=datetime.now(timezone.utc) + timedelta(days=1))
    monkeypatch.setattr(sw, "SMS_REPLAY_WAIT_SECONDS", 1.0, raising=False)
    calls = []

    async def scenario():
        released = asyncio.Event()

        async def slow_orchestrator(**kwargs):
            calls.append(kwargs)
            await released.wait()
            return "the slow reply"

        monkeypatch.setattr(sw, "route_to_orchestrator", slow_orchestrator)
        async with _client() as c:
            first = asyncio.create_task(_apost(c, _params()))
            while not calls:
                await asyncio.sleep(0.02)
            second = asyncio.create_task(_apost(c, _params()))
            if set_during_wait:
                await asyncio.sleep(0.3)
                released.set()
            try:
                # Bounded: a retry that runs the orchestrator again (or
                # waits forever) must fail, not hang.
                retry = await asyncio.wait_for(second, timeout=5.0)
            finally:
                released.set()
            original = await asyncio.wait_for(first, timeout=5.0)
            return original, retry

    original, retry = asyncio.run(scenario())
    assert original.status_code == 200 and "the slow reply" in original.text
    if set_during_wait:
        assert retry.status_code == 200
        assert "the slow reply" in retry.text
    else:
        assert retry.status_code == 503, retry.text
        assert retry.headers.get("retry-after") == "5"
        assert "<Response" not in retry.text
    assert len(calls) == 1


@pytest.mark.parametrize("sid", ["SM123", "XX" + "0" * 32, "SM" + "g" * 32, "SM" + "0" * 33, "sm" + "0" * 32])
def test_malformed_message_sid_is_400_with_no_row(db, signed, monkeypatch, sid):
    calls = _recording_orchestrator(monkeypatch)
    resp = _send(_params(MessageSid=sid))
    assert resp.status_code == 400, resp.text
    assert _rows(db) == []
    assert calls == []


def test_mms_sid_is_accepted(db, signed, monkeypatch):
    _recording_orchestrator(monkeypatch)
    assert _send(_params(MessageSid="MM" + "a" * 32)).status_code == 200
    assert len(_rows(db)) == 1


def test_orchestrator_failure_stores_the_apology_not_the_error(db, signed, monkeypatch):
    _stay(db, phone=P_A, checkin=datetime.now(timezone.utc) - timedelta(days=1),
          checkout=datetime.now(timezone.utc) + timedelta(days=1))

    async def boom(**kwargs):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(sw, "route_to_orchestrator", boom)
    resp = _send(_params())
    assert resp.status_code == 200
    assert "having trouble" in resp.text
    (row,) = _rows(db)
    assert row.response_sent is True
    assert row.response_content.startswith("Sorry, I'm having trouble")
    assert "secret-internal-detail" not in row.response_content
    assert "Error" not in row.response_content
