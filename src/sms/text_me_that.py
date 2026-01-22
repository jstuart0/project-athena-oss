"""
'Text Me That' Intent Handler.

Handles requests like:
- "Text me that"
- "Send that to my phone"
- "Can you text me this information?"
- "SMS me the details"

Retrieves the last assistant response and sends it via SMS.
"""

import re
from typing import Optional, Tuple, Dict, Any
import structlog

logger = structlog.get_logger(__name__)

# Patterns that indicate "text me that" intent
TEXT_ME_PATTERNS = [
    r"text\s+(?:me\s+)?that",
    r"send\s+(?:that\s+)?(?:to\s+)?(?:my\s+)?(?:phone|cell)",
    r"sms\s+(?:me\s+)?(?:that|this|the)",
    r"message\s+(?:me\s+)?that",
    r"can\s+you\s+text\s+(?:me\s+)?(?:that|this)",
    r"(?:send|text)\s+(?:that|this|the)\s+(?:info|information|details)",
    r"text\s+it\s+to\s+me",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in TEXT_ME_PATTERNS]


def is_text_me_that_request(query: str) -> bool:
    """
    Check if the query is a 'text me that' request.

    Args:
        query: The user's query string

    Returns:
        True if query matches text-me-that patterns
    """
    for pattern in COMPILED_PATTERNS:
        if pattern.search(query):
            return True
    return False


def extract_phone_from_query(query: str) -> Optional[str]:
    """
    Extract phone number if user provided it in the request.

    Args:
        query: The user's query string

    Returns:
        Phone number string if found, None otherwise
    """
    # Pattern for US phone numbers
    phone_pattern = r"(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})"
    match = re.search(phone_pattern, query)
    if match:
        # Clean the phone number
        phone = re.sub(r'[^\d+]', '', match.group(1))
        # Ensure it has country code
        if not phone.startswith('+'):
            if len(phone) == 10:
                phone = '+1' + phone
            elif len(phone) == 11 and phone.startswith('1'):
                phone = '+' + phone
        return phone
    return None


async def handle_text_me_that(
    query: str,
    conversation_history: list,
    guest_phone: Optional[str] = None,
    sms_service=None,
    calendar_event_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Handle 'text me that' requests by sending the last response via SMS.

    Args:
        query: The user's query string
        conversation_history: List of conversation messages
        guest_phone: Pre-populated guest phone number (if known)
        sms_service: SMS service instance for sending
        calendar_event_id: Current calendar event ID (for logging)

    Returns:
        Dictionary with:
        - success: bool
        - answer: str (response to user)
        - needs_phone: bool (if we need to capture phone number)
        - pending_content: str (content to send once we have phone)
    """
    # Try to extract phone from query first
    provided_phone = extract_phone_from_query(query)
    phone_to_use = provided_phone or guest_phone

    # Find the last assistant response to send
    last_response = None
    for msg in reversed(conversation_history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            # Skip very short responses or confirmations
            if len(content) > 20:
                last_response = content
                break

    if not last_response:
        return {
            "success": False,
            "answer": "I don't have any recent information to send you. Could you ask me something first, then say 'text me that'?",
            "needs_phone": False,
            "pending_content": None,
        }

    # Clean up the response for SMS
    sms_content = _prepare_content_for_sms(last_response)

    if not phone_to_use:
        return {
            "success": False,
            "answer": "I'd be happy to text you that information. What phone number should I send it to?",
            "needs_phone": True,
            "pending_content": sms_content,
        }

    # Send the SMS
    if sms_service:
        try:
            success, result, segment_count = await sms_service.send_sms(
                to_number=phone_to_use,
                content=sms_content,
                calendar_event_id=calendar_event_id,
                content_type="text_me_that",
            )

            if success:
                logger.info(
                    "text_me_that_sent",
                    phone=phone_to_use[-4:],  # Log only last 4 digits
                    content_length=len(sms_content),
                )
                return {
                    "success": True,
                    "answer": "Done! I've texted that information to your phone.",
                    "needs_phone": False,
                    "pending_content": None,
                }
            else:
                logger.error("text_me_that_failed", error=result)
                return {
                    "success": False,
                    "answer": "I wasn't able to send the text message. Please try again or ask me to read the information aloud.",
                    "needs_phone": False,
                    "pending_content": None,
                }
        except Exception as e:
            logger.exception("text_me_that_error", error=str(e))
            return {
                "success": False,
                "answer": "Something went wrong while sending the text. Please try again.",
                "needs_phone": False,
                "pending_content": None,
            }
    else:
        # SMS service not available
        logger.warning("text_me_that_no_service")
        return {
            "success": False,
            "answer": "Text messaging isn't available right now. I can read the information to you instead.",
            "needs_phone": False,
            "pending_content": None,
        }


def _prepare_content_for_sms(content: str) -> str:
    """
    Prepare assistant response content for SMS delivery.

    - Remove voice-specific phrases
    - Clean up formatting
    - Ensure SMS-appropriate length

    Args:
        content: Raw assistant response

    Returns:
        Cleaned content suitable for SMS
    """
    # Remove common voice-specific intros
    voice_phrases = [
        r"^(Sure!|Okay!|Of course!|Certainly!|Absolutely!)\s*",
        r"^(Let me|I'll|I can)\s+(tell|read|give|share)\s+you\s+",
        r"^(Here's|Here is)\s+(that|the)\s+information[:.]\s*",
    ]

    result = content
    for phrase in voice_phrases:
        result = re.sub(phrase, "", result, flags=re.IGNORECASE)

    # Remove markdown formatting
    result = re.sub(r'\*\*(.+?)\*\*', r'\1', result)  # Bold
    result = re.sub(r'\*(.+?)\*', r'\1', result)  # Italic
    result = re.sub(r'`(.+?)`', r'\1', result)  # Code

    # Clean up whitespace
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = result.strip()

    return result


async def handle_phone_capture(
    query: str,
    pending_content: str,
    sms_service=None,
    calendar_event_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Handle the follow-up when user provides their phone number.

    Args:
        query: User's response containing phone number
        pending_content: The content we were waiting to send
        sms_service: SMS service instance
        calendar_event_id: Current calendar event ID

    Returns:
        Result dictionary similar to handle_text_me_that
    """
    phone = extract_phone_from_query(query)

    if not phone:
        return {
            "success": False,
            "answer": "I didn't catch that phone number. Could you say it again? For example, '555-123-4567'.",
            "needs_phone": True,
            "pending_content": pending_content,
        }

    if sms_service:
        try:
            success, result, segment_count = await sms_service.send_sms(
                to_number=phone,
                content=pending_content,
                calendar_event_id=calendar_event_id,
                content_type="text_me_that",
            )

            if success:
                return {
                    "success": True,
                    "answer": f"Got it! I've sent that information to {_mask_phone(phone)}.",
                    "needs_phone": False,
                    "pending_content": None,
                }
            else:
                return {
                    "success": False,
                    "answer": f"I couldn't send to that number. Please double-check and try again.",
                    "needs_phone": True,
                    "pending_content": pending_content,
                }
        except Exception as e:
            logger.exception("phone_capture_error", error=str(e))
            return {
                "success": False,
                "answer": "Something went wrong. Please try again.",
                "needs_phone": True,
                "pending_content": pending_content,
            }
    else:
        return {
            "success": False,
            "answer": "Text messaging isn't available right now.",
            "needs_phone": False,
            "pending_content": None,
        }


def _mask_phone(phone: str) -> str:
    """Mask phone number for display, showing only last 4 digits."""
    if len(phone) >= 4:
        return f"***-***-{phone[-4:]}"
    return "****"
