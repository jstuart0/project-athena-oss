"""
Twilio SMS Webhook Handler.

Receives incoming SMS messages from Twilio and routes them to the
orchestrator for processing. Enables bidirectional SMS conversations
with guests.
"""

from datetime import datetime, timezone
from typing import Optional
import httpx
import structlog
from fastapi import APIRouter, Form, Response, Depends, Request
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse

from ..database import get_db
from ..models import SMSIncoming, CalendarEvent, GuestSMSPreference

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/sms/webhook", tags=["SMS Webhook"])

import os

# Orchestrator URL for processing queries
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")


@router.post("/incoming")
async def handle_incoming_sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(...),
    To: str = Form(None),
    NumMedia: str = Form("0"),
    db: Session = Depends(get_db),
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
        TwiML response with assistant's reply
    """
    logger.info(
        "incoming_sms_received",
        from_number=From[-4:],  # Log only last 4 digits
        message_length=len(Body),
        message_sid=MessageSid,
    )

    # Create incoming SMS record
    incoming = SMSIncoming(
        phone_number=From,
        message=Body,
        twilio_sid=MessageSid,
        received_at=datetime.now(timezone.utc),
        matched_guest=False,
        response_sent=False,
    )

    # Try to match to a guest by phone number
    guest = find_guest_by_phone(From, db)

    if guest:
        incoming.calendar_event_id = guest.id
        incoming.matched_guest = True
        logger.info(
            "incoming_sms_matched",
            guest_name=guest.guest_name,
            event_id=guest.id,
        )

    db.add(incoming)
    db.commit()

    # Process the message
    twiml = MessagingResponse()

    if not guest:
        # Unknown sender
        twiml.message(
            "Hi! I'm Athena, your vacation rental assistant. "
            "I don't recognize this number. If you're a guest, "
            "please use the phone number from your reservation."
        )
        incoming.response_sent = True
        incoming.response_content = str(twiml)
        db.commit()
        return Response(content=str(twiml), media_type="application/xml")

    # Check if guest has opted out of SMS
    prefs = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == guest.id
    ).first()

    if prefs and prefs.opted_out:
        twiml.message(
            "You've opted out of SMS communication. "
            "Text 'START' to opt back in."
        )
        incoming.response_sent = True
        incoming.response_content = str(twiml)
        db.commit()
        return Response(content=str(twiml), media_type="application/xml")

    # Handle opt-in/opt-out commands
    body_lower = Body.strip().lower()
    if body_lower in ["stop", "unsubscribe", "cancel", "quit"]:
        await handle_opt_out(guest.id, db)
        twiml.message(
            "You've been unsubscribed from SMS notifications. "
            "Text 'START' to opt back in."
        )
        incoming.response_sent = True
        incoming.response_content = str(twiml)
        db.commit()
        return Response(content=str(twiml), media_type="application/xml")

    if body_lower in ["start", "subscribe", "yes"]:
        await handle_opt_in(guest.id, db)
        twiml.message(
            "Welcome back! You'll now receive SMS notifications again. "
            "Text any question and I'll help you out!"
        )
        incoming.response_sent = True
        incoming.response_content = str(twiml)
        db.commit()
        return Response(content=str(twiml), media_type="application/xml")

    # Route to orchestrator for AI response
    try:
        response_text = await route_to_orchestrator(
            query=Body,
            phone_number=From,
            calendar_event_id=guest.id,
            guest_name=guest.guest_name,
        )

        twiml.message(response_text)
        incoming.response_sent = True
        incoming.response_content = response_text
        incoming.processed_at = datetime.now(timezone.utc)

    except Exception as e:
        logger.exception("orchestrator_error", error=str(e))
        twiml.message(
            "Sorry, I'm having trouble processing your message right now. "
            "Please try again in a moment or call the host directly."
        )
        incoming.response_sent = True
        incoming.response_content = "Error: " + str(e)

    db.commit()
    return Response(content=str(twiml), media_type="application/xml")


@router.post("/status")
async def handle_status_callback(
    MessageSid: str = Form(...),
    MessageStatus: str = Form(...),
    To: str = Form(None),
    ErrorCode: str = Form(None),
    ErrorMessage: str = Form(None),
    db: Session = Depends(get_db),
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


def find_guest_by_phone(phone_number: str, db: Session) -> Optional[CalendarEvent]:
    """
    Find a current or recent guest by phone number.

    Args:
        phone_number: Phone number to search for
        db: Database session

    Returns:
        CalendarEvent if found, None otherwise
    """
    # Normalize phone number (remove formatting)
    normalized = "".join(c for c in phone_number if c.isdigit() or c == "+")

    # Also check without country code
    without_country = normalized.lstrip("+1")

    now = datetime.now(timezone.utc)

    # Look for current stays first
    current_stay = db.query(CalendarEvent).filter(
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.checkin <= now,
        CalendarEvent.checkout >= now,
    ).filter(
        (CalendarEvent.guest_phone.contains(normalized)) |
        (CalendarEvent.guest_phone.contains(without_country))
    ).first()

    if current_stay:
        return current_stay

    # Look for recent past stays (within 24 hours of checkout)
    recent_checkout = now - timedelta(hours=24)
    recent_stay = db.query(CalendarEvent).filter(
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.checkout >= recent_checkout,
        CalendarEvent.checkout <= now,
    ).filter(
        (CalendarEvent.guest_phone.contains(normalized)) |
        (CalendarEvent.guest_phone.contains(without_country))
    ).first()

    if recent_stay:
        return recent_stay

    # Look for upcoming stays (check-in within 48 hours)
    upcoming_window = now + timedelta(hours=48)
    upcoming_stay = db.query(CalendarEvent).filter(
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.checkin >= now,
        CalendarEvent.checkin <= upcoming_window,
    ).filter(
        (CalendarEvent.guest_phone.contains(normalized)) |
        (CalendarEvent.guest_phone.contains(without_country))
    ).first()

    return upcoming_stay


async def route_to_orchestrator(
    query: str,
    phone_number: str,
    calendar_event_id: int,
    guest_name: Optional[str] = None,
) -> str:
    """
    Route the incoming SMS query to the orchestrator.

    Args:
        query: The user's message
        phone_number: Sender's phone number
        calendar_event_id: Associated calendar event ID
        guest_name: Guest's name if known

    Returns:
        Response text from the orchestrator
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/query",
            json={
                "query": query,
                "mode": "guest",
                "interface_type": "text",  # Full details for SMS
                "session_id": f"sms_{phone_number}",
                "room": "sms",
                "context": {
                    "calendar_event_id": calendar_event_id,
                    "guest_name": guest_name,
                    "channel": "sms",
                },
            },
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
                body=response.text[:200],
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


# Import timedelta for phone matching
from datetime import timedelta
