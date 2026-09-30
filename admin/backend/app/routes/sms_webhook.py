"""
Twilio SMS Webhook Handler.

Receives incoming SMS messages from Twilio and routes them to the
orchestrator for processing. Enables bidirectional SMS conversations
with guests.
"""

import asyncio
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlsplit
import httpx
import os
import structlog
from fastapi import APIRouter, Form, HTTPException, Response, Depends, Request
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from ..database import SessionLocal, get_db
from ..models import SMSIncoming, CalendarEvent, GuestSMSPreference
from shared.config import get_config

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/sms/webhook", tags=["SMS Webhook"])

# Orchestrator URL for processing queries
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")

# Twilio auth token for signature validation.
# If unset, validation is skipped with a warning (allows local dev without Twilio config).
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

# Public base URL Twilio actually calls (scheme://host[:port][/prefix]), used
# to build the URL passed to RequestValidator.validate(). This deliberately
# ignores the Host header and any forwarded-proto/forwarded-host headers,
# which this deployment's ingress does not authenticate. Required whenever
# TWILIO_AUTH_TOKEN is set; see .env.example for the validity rule and
# format.
#
# Note: if an ASGI root_path is ever introduced, whether request.url.path
# carries that prefix depends on the uvicorn/starlette pairing (measured
# different between older and newer starlette versions). None is set today;
# re-verify the prefix rule before adding one.
TWILIO_WEBHOOK_BASE_URL = os.getenv("TWILIO_WEBHOOK_BASE_URL", "")


def _twilio_validation_url(request: Request) -> Optional[str]:
    """Build the external URL Twilio signed, from the configured base only."""
    if not _base_url_is_valid(TWILIO_WEBHOOK_BASE_URL):
        return None
    parsed = urlsplit(TWILIO_WEBHOOK_BASE_URL)
    prefix = parsed.path.rstrip("/")
    url = f"{parsed.scheme}://{parsed.netloc}{prefix}{request.url.path}"
    if request.url.query:
        url += "?" + request.url.query
    return url


def _base_url_is_valid(base: str) -> bool:
    """Scheme http/https, non-empty host, no query/fragment/userinfo, valid port."""
    if not base or "?" in base or "#" in base:
        return False
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    return True


if TWILIO_AUTH_TOKEN and not _base_url_is_valid(TWILIO_WEBHOOK_BASE_URL):
    logger.error("twilio_webhook_base_url_not_configured", at="import")

if not TWILIO_AUTH_TOKEN and os.getenv("TWILIO_ALLOW_UNSIGNED") == "true":
    # Once per process: every webhook will be accepted unsigned, so anyone
    # who knows a guest's number can text as that guest.
    logger.error("twilio_unsigned_webhooks_allowed", at="import")


async def validate_twilio_signature(request: Request) -> None:
    """
    FastAPI dependency that validates the X-Twilio-Signature header.

    Protects SMS webhook endpoints from spoofed requests. With
    TWILIO_AUTH_TOKEN set, a missing or bad signature is 403 (the
    TWILIO_ALLOW_UNSIGNED opt-in is never read). With it unset:

    - DEV_MODE: validation is skipped with a warning (local development);
    - TWILIO_ALLOW_UNSIGNED exactly "true": the request is accepted unsigned
      with a warning. The sender is then unauthenticated: anyone who knows a
      guest's number can text as that guest;
    - otherwise 503, before the handler runs (nothing is stored).

    DEV_MODE and the opt-in are read per request through get_config().
    ``request.state.twilio_signed`` is True only when a signature was
    verified.
    """
    request.state.twilio_signed = False
    if not TWILIO_AUTH_TOKEN:
        config = get_config()
        if config.dev_mode:
            logger.warning("twilio_auth_token_not_configured_skipping_validation")
            return
        if config.twilio_allow_unsigned == "true":
            logger.warning("twilio_unsigned_request_accepted", path=request.url.path)
            return
        logger.error("twilio_auth_token_not_configured")
        raise HTTPException(status_code=503, detail="SMS webhook not configured")

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("twilio_signature_header_missing", path=request.url.path)
        raise HTTPException(status_code=403, detail="Missing Twilio signature")

    validation_url = _twilio_validation_url(request)
    if validation_url is None:
        logger.error("twilio_webhook_base_url_not_configured")
        raise HTTPException(status_code=503, detail="SMS webhook not configured")

    # FastAPI parses Form(...) params before resolving dependencies. Starlette
    # caches the parsed form on the Request, so request.form() returns the
    # object the handler's params were bound from. Reading the raw stream
    # here would raise "Stream consumed".
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        logger.warning("twilio_webhook_unsupported_content_type")
        raise HTTPException(status_code=403, detail="Unsupported content type")

    form = await request.form()

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    if not validator.validate(validation_url, form, signature):
        logger.warning("twilio_signature_invalid", path=request.url.path)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    request.state.twilio_signed = True


# A Twilio message SID: SM (SMS) or MM (MMS) + 32 hex digits.
_MESSAGE_SID = re.compile(r"^(SM|MM)[0-9a-fA-F]{32}$")

# How long a retry of a message still being answered waits for the reply,
# and how often it looks. Twilio retries after its own 15 s timeout; our
# orchestrator call can take up to 30 s, so the reply is usually seconds
# away. Past the wait, 503 + Retry-After keeps Twilio's retry and fallback
# path alive instead of ending the conversation with an empty 200.
SMS_REPLAY_WAIT_SECONDS = 10.0
SMS_REPLAY_POLL_SECONDS = 0.5
_REPLAY_RETRY_AFTER = "5"

UNKNOWN_SENDER_REPLY = (
    "Hi! I'm Athena, your vacation rental assistant. "
    "I don't recognize this number. If you're a guest, "
    "please use the phone number from your reservation."
)
OPTED_OUT_REPLY = "You've opted out of SMS communication. Text 'START' to opt back in."
UNSUBSCRIBED_REPLY = "You've been unsubscribed from SMS notifications. Text 'START' to opt back in."
RESUBSCRIBED_REPLY = (
    "Welcome back! You'll now receive SMS notifications again. "
    "Text any question and I'll help you out!"
)
ERROR_REPLY = (
    "Sorry, I'm having trouble processing your message right now. "
    "Please try again in a moment or call the host directly."
)


def _twiml(text: Optional[str]) -> Response:
    twiml = MessagingResponse()
    if text:
        twiml.message(text)
    return Response(content=str(twiml), media_type="application/xml")


def _find_replay(db: Session, message_sid: str, sender: str) -> Optional[SMSIncoming]:
    """The earlier delivery of this message from this sender, if any. The
    same SID from a different sender is not a replay (a forged request
    carrying someone else's SID can't pull their reply)."""
    rows = db.query(SMSIncoming).filter(SMSIncoming.twilio_sid == message_sid).all()
    for row in rows:
        if row.phone_number == sender:
            return row
    if rows:
        logger.warning("sms_sid_sender_mismatch", message_sid=message_sid)
    return None


def _replay_response(content: Optional[str], signed: bool) -> Response:
    """The stored reply, only to a verified sender. Unsigned, the sender is
    unauthenticated, so a stored reply is never echoed."""
    if not signed:
        return _twiml(None)
    logger.info("sms_replay_answered")
    return _twiml(content)


async def _answer_replay(row: SMSIncoming, signed: bool) -> Response:
    if row.response_sent:
        return _replay_response(row.response_content, signed)
    row_id = row.id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SMS_REPLAY_WAIT_SECONDS
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(SMS_REPLAY_POLL_SECONDS, remaining))
        # A fresh session: the request's own would read a stale snapshot.
        session = SessionLocal()
        try:
            fresh = session.get(SMSIncoming, row_id)
            if fresh is not None and fresh.response_sent:
                return _replay_response(fresh.response_content, signed)
        finally:
            session.close()
    logger.warning("sms_replay_in_progress_timeout", message_sid=row.twilio_sid)
    return Response(status_code=503, headers={"Retry-After": _REPLAY_RETRY_AFTER})


@router.post("/incoming")
async def handle_incoming_sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(...),
    To: str = Form(None),
    NumMedia: str = Form("0"),
    db: Session = Depends(get_db),
    _: None = Depends(validate_twilio_signature),
):
    """
    Handle incoming SMS from Twilio webhook.

    Twilio sends SMS to this endpoint when a message is received.
    The message is:
    1. Logged to the database
    2. Matched to a guest if possible
    3. Routed to the orchestrator for response
    4. Response sent back via TwiML

    Args:
        From: Sender phone number
        Body: Message content
        MessageSid: Twilio message SID
        To: Recipient phone number (our number)
        NumMedia: Number of media attachments
        db: Database session

    Returns:
        TwiML response with assistant's reply. A Twilio retry of a message
        already answered gets the same reply (never a second orchestrator
        call); a retry of one still being answered waits for it, then 503s.
    """
    if not _MESSAGE_SID.match(MessageSid):
        logger.warning("invalid_message_sid", sid_length=len(MessageSid))
        raise HTTPException(status_code=400, detail="invalid_message_sid")

    logger.info(
        "incoming_sms_received",
        from_number=From[-4:],  # Log only last 4 digits
        message_length=len(Body),
        message_sid=MessageSid,
    )

    signed = bool(getattr(request.state, "twilio_signed", False))
    earlier = _find_replay(db, MessageSid, From)
    if earlier is not None:
        return await _answer_replay(earlier, signed)

    # Create incoming SMS record
    incoming = SMSIncoming(
        phone_number=From,
        message=Body,
        twilio_sid=MessageSid,
        received_at=datetime.now(timezone.utc),
        matched_guest=False,
        response_sent=False,
    )

    # Match the sender to a stay (exact E.164 number)
    match = find_guest_by_phone(From, db)
    guest, stay_phase = match if match else (None, None)

    if guest:
        incoming.calendar_event_id = guest.id
        incoming.matched_guest = True
        logger.info(
            "incoming_sms_matched",
            event_id=guest.id,
            stay_phase=stay_phase,
        )

    db.add(incoming)
    db.commit()

    def _answered(text: str) -> Response:
        incoming.response_sent = True
        incoming.response_content = text
        db.commit()
        return _twiml(text)

    if not guest:
        return _answered(UNKNOWN_SENDER_REPLY)

    # Check if guest has opted out of SMS
    prefs = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == guest.id
    ).first()

    if prefs and prefs.opted_out:
        return _answered(OPTED_OUT_REPLY)

    # Handle opt-in/opt-out commands
    body_lower = Body.strip().lower()
    if body_lower in ["stop", "unsubscribe", "cancel", "quit"]:
        await handle_opt_out(guest.id, db)
        return _answered(UNSUBSCRIBED_REPLY)

    if body_lower in ["start", "subscribe", "yes"]:
        await handle_opt_in(guest.id, db)
        return _answered(RESUBSCRIBED_REPLY)

    # Route to orchestrator for AI response
    try:
        response_text = await route_to_orchestrator(
            query=Body,
            phone_number=From,
            calendar_event_id=guest.id,
            guest_name=guest.guest_name,
            stay_phase=stay_phase,
        )
        incoming.processed_at = datetime.now(timezone.utc)
        return _answered(response_text)

    except Exception as e:
        logger.exception("orchestrator_error", error_type=type(e).__name__)
        return _answered(ERROR_REPLY)


@router.post("/status")
async def handle_status_callback(
    request: Request,
    MessageSid: str = Form(...),
    MessageStatus: str = Form(...),
    To: str = Form(None),
    ErrorCode: str = Form(None),
    ErrorMessage: str = Form(None),
    db: Session = Depends(get_db),
    _: None = Depends(validate_twilio_signature),
):
    """
    Handle SMS delivery status callbacks from Twilio.

    Updates the SMS history with delivery status.

    Args:
        MessageSid: Twilio message SID
        MessageStatus: Status (queued, sent, delivered, failed, etc.)
        To: Recipient phone number
        ErrorCode: Error code if failed
        ErrorMessage: Error message if failed
        db: Database session
    """
    logger.info(
        "sms_status_update",
        message_sid=MessageSid,
        status=MessageStatus,
        error_code=ErrorCode,
    )

    # Update SMS history record
    from ..models import SMSHistory

    history = db.query(SMSHistory).filter(
        SMSHistory.twilio_sid == MessageSid
    ).first()

    if history:
        history.status = MessageStatus
        if ErrorCode:
            history.error_code = ErrorCode
            history.error_message = ErrorMessage
        if MessageStatus == "delivered":
            history.delivered_at = datetime.now(timezone.utc)
        db.commit()

    return {"status": "ok"}


_SMS_SESSION_ID_DOMAIN = b"athena-sms-session-v1|"


def _normalize_phone(phone_number: str) -> str:
    """Digits and '+' only (formatting removed)."""
    return "".join(c for c in phone_number if c.isdigit() or c == "+")


def sms_session_id(phone_number: str) -> str:
    """The orchestrator session id for one SMS number: ``sms_`` + 24 hex of
    HMAC-SHA256(SERVICE_API_KEY, domain prefix + normalised number).

    The number itself never appears (the orchestrator logs session ids). The
    id is stable per number while the key is; rotating SERVICE_API_KEY
    starts every SMS conversation afresh (old sessions expire at their TTL).
    """
    message = _SMS_SESSION_ID_DOMAIN + _normalize_phone(phone_number).encode("utf-8")
    digest = hmac.new(get_config().service_api_key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return "sms_" + digest[:24]


_E164_SEPARATORS = str.maketrans("", "", " \t-.()")
_ASCII_DIGITS = frozenset("0123456789")


def to_e164(raw: Optional[str], default_cc: str, *, strict: bool = False) -> Optional[str]:
    """``raw`` as ``+<10-15 digits>``, or None.

    Spaces, tabs, ``-``, ``.``, ``(`` and ``)`` are dropped; anything left
    must be ASCII digits with at most one leading ``+`` (so an alphanumeric
    sender like "ATHENA" is None). ``+`` or ``00`` means international.
    A national number gets ``default_cc``: after stripping one trunk ``0``
    always, otherwise unless it already starts with the code and is at least
    11 digits long. ``strict`` (the sender, as Twilio sends it) requires the
    leading ``+``. Not the session-id input: that stays _normalize_phone.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().translate(_E164_SEPARATORS)
    international = value.startswith("+")
    if strict and not international:
        return None
    digits = value[1:] if international else value
    if not digits or any(c not in _ASCII_DIGITS for c in digits):
        return None
    if not international:
        if digits.startswith("00"):
            digits = digits[2:]
        elif digits.startswith("0"):
            digits = default_cc + digits[1:]
        elif not (digits.startswith(default_cc) and len(digits) >= 11):
            digits = default_cc + digits
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def find_guest_by_phone(
    phone_number: str, db: Session, *, now: Optional[datetime] = None
) -> Optional[Tuple[CalendarEvent, str]]:
    """The stay an incoming SMS belongs to, and which phase it's in.

    Only confirmed, not-deleted stays with a phone on file are considered,
    and only an exact E.164 match counts (the booking's number is normalised
    leniently, with SMS_DEFAULT_COUNTRY_CODE; the sender strictly). Tiers,
    first match wins:

    - "current": checkin <= now <= checkout, the earliest checkout first
      (the departing stay on a changeover day);
    - "recent": checked out within the last 24 h, the latest first;
    - "upcoming": checking in within the next 48 h, the soonest first.

    A match proves only possession of the booking's number, and only when
    Twilio signatures are validated. With TWILIO_ALLOW_UNSIGNED=true anyone
    who knows a guest's number can impersonate them. Recent and upcoming
    matches are answer-only (the orchestrator refuses house writes and
    house reads for them).
    """
    now = now or datetime.now(timezone.utc)
    country_code = get_config().sms_default_country_code
    sender = to_e164(phone_number, country_code, strict=True)
    if sender is None:
        return None

    def stays():
        return db.query(CalendarEvent).filter(
            CalendarEvent.deleted_at.is_(None),
            CalendarEvent.status == "confirmed",
            CalendarEvent.guest_phone.isnot(None),
        )

    tiers = (
        ("current", lambda: stays().filter(
            CalendarEvent.checkin <= now, CalendarEvent.checkout >= now,
        ).order_by(CalendarEvent.checkout.asc(), CalendarEvent.checkin.desc())),
        ("recent", lambda: stays().filter(
            CalendarEvent.checkout >= now - timedelta(hours=24), CalendarEvent.checkout < now,
        ).order_by(CalendarEvent.checkout.desc())),
        ("upcoming", lambda: stays().filter(
            CalendarEvent.checkin > now, CalendarEvent.checkin <= now + timedelta(hours=48),
        ).order_by(CalendarEvent.checkin.asc())),
    )
    for phase, query in tiers:
        for event in query():
            if to_e164(event.guest_phone, country_code) == sender:
                return event, phase
    return None


async def route_to_orchestrator(
    query: str,
    phone_number: str,
    calendar_event_id: int,
    guest_name: Optional[str] = None,
    stay_phase: Optional[str] = None,
) -> str:
    """
    Route the incoming SMS query to the orchestrator.

    Args:
        query: The user's message
        phone_number: Sender's phone number
        calendar_event_id: Associated calendar event ID
        guest_name: Guest's name if known
        stay_phase: "current", "recent" or "upcoming" (find_guest_by_phone).
            Anything but "current" is answer-only in the orchestrator.

    Returns:
        Response text from the orchestrator
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/query",
            json={
                "query": query,
                "mode": "guest",
                "caller_trust": "sms",
                # ATHENA-128 D14: the per-number SMS session persists
                "supports_followup": True,
                "interface_type": "text",  # Full details for SMS
                "session_id": sms_session_id(phone_number),
                "room": "sms",
                "context": {
                    "calendar_event_id": calendar_event_id,
                    "guest_name": guest_name,
                    "channel": "sms",
                    "stay_phase": stay_phase,
                },
            },
            headers={"X-Service-Key": get_config().service_api_key},
        )

        if response.status_code == 200:
            data = response.json()
            answer = data.get("answer", "")

            # Truncate if too long for SMS
            if len(answer) > 1500:
                answer = answer[:1450] + "... (Reply for more)"

            return answer
        else:
            logger.error(
                "orchestrator_request_failed",
                status=response.status_code,
                body_len=len(response.text),
            )
            raise Exception(f"Orchestrator returned {response.status_code}")


async def handle_opt_out(calendar_event_id: int, db: Session):
    """Handle guest opt-out from SMS."""
    prefs = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == calendar_event_id
    ).first()

    if prefs:
        prefs.opted_out = True
    else:
        prefs = GuestSMSPreference(
            calendar_event_id=calendar_event_id,
            sms_enabled=False,
            opted_out=True,
        )
        db.add(prefs)

    db.commit()
    logger.info("guest_opted_out", event_id=calendar_event_id)


async def handle_opt_in(calendar_event_id: int, db: Session):
    """Handle guest opt-in to SMS."""
    prefs = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == calendar_event_id
    ).first()

    if prefs:
        prefs.opted_out = False
        prefs.sms_enabled = True
    else:
        prefs = GuestSMSPreference(
            calendar_event_id=calendar_event_id,
            sms_enabled=True,
            opted_out=False,
        )
        db.add(prefs)

    db.commit()
    logger.info("guest_opted_in", event_id=calendar_event_id)

