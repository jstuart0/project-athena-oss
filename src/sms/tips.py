"""
Tips System for Guest Experience Enhancement.

Shows contextual tips to guests during their stay, such as:
- SMS feature introduction (can text you information)
- Local recommendations
- Property features they haven't asked about
- Checkout reminders
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
import structlog
import httpx

logger = structlog.get_logger(__name__)

# Admin backend URL for database access
ADMIN_BACKEND_URL = "http://localhost:8080"


class TipsService:
    """
    Service for managing and showing tips to guests.

    Tips are configured in the admin UI and stored in the database.
    The service tracks which tips have been shown to each guest/stay
    to avoid repetition.
    """

    def __init__(self, admin_url: str = ADMIN_BACKEND_URL):
        self.admin_url = admin_url
        self._client = httpx.AsyncClient(timeout=10.0)

    async def maybe_show_tip(
        self,
        calendar_event_id: Optional[int],
        session_id: str,
        intent: Optional[str] = None,
        trigger_condition: Optional[str] = None,
    ) -> Optional[str]:
        """
        Check if a tip should be shown and return it if so.

        Args:
            calendar_event_id: Current guest's calendar event ID
            session_id: Current conversation session ID
            intent: The intent that was just processed
            trigger_condition: Specific trigger to check (e.g., 'first_question', 'after_wifi')

        Returns:
            Tip message to append to response, or None
        """
        if not calendar_event_id:
            # No guest context, no tips
            return None

        try:
            # Get applicable tips from admin backend
            params = {
                "calendar_event_id": calendar_event_id,
                "session_id": session_id,
            }
            if intent:
                params["intent"] = intent
            if trigger_condition:
                params["trigger_condition"] = trigger_condition

            response = await self._client.get(
                f"{self.admin_url}/api/tips/applicable",
                params=params,
            )

            if response.status_code != 200:
                logger.warning("tips_fetch_failed", status=response.status_code)
                return None

            data = response.json()
            tips = data.get("tips", [])

            if not tips:
                return None

            # Get the highest priority tip that hasn't been shown
            tip = tips[0]

            # Mark tip as shown
            await self._record_tip_shown(
                tip_id=tip["id"],
                calendar_event_id=calendar_event_id,
                session_id=session_id,
            )

            # Format the tip message
            return f"\n\n💡 Tip: {tip['message']}"

        except Exception as e:
            logger.exception("tips_error", error=str(e))
            return None

    async def _record_tip_shown(
        self,
        tip_id: int,
        calendar_event_id: int,
        session_id: str,
    ) -> bool:
        """Record that a tip was shown to a guest."""
        try:
            response = await self._client.post(
                f"{self.admin_url}/api/tips/record-shown",
                json={
                    "tip_id": tip_id,
                    "calendar_event_id": calendar_event_id,
                    "session_id": session_id,
                },
            )
            return response.status_code == 200
        except Exception as e:
            logger.exception("tip_record_failed", error=str(e))
            return False

    async def get_tips_for_stay(
        self,
        calendar_event_id: int,
    ) -> List[Dict[str, Any]]:
        """
        Get all tips that have been shown during a stay.

        Args:
            calendar_event_id: The calendar event ID

        Returns:
            List of tip records with timestamps
        """
        try:
            response = await self._client.get(
                f"{self.admin_url}/api/tips/history/{calendar_event_id}",
            )
            if response.status_code == 200:
                return response.json().get("tips", [])
            return []
        except Exception as e:
            logger.exception("tips_history_error", error=str(e))
            return []

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


# Singleton instance
_tips_service: Optional[TipsService] = None


async def get_tips_service() -> TipsService:
    """Get the singleton tips service instance."""
    global _tips_service
    if _tips_service is None:
        _tips_service = TipsService()
    return _tips_service


# ============================================================================
# Trigger Conditions
# ============================================================================

# Standard trigger conditions that can be used in tip configuration
TRIGGER_CONDITIONS = {
    "first_question": "First question asked during the stay",
    "after_wifi": "After asking about WiFi/password",
    "after_directions": "After asking for directions/address",
    "after_local_info": "After asking about local recommendations",
    "morning_greeting": "First interaction in the morning",
    "checkout_day": "Any interaction on checkout day",
    "long_stay": "For stays longer than 5 nights",
}


def get_trigger_condition(
    intent: Optional[str],
    is_first_question: bool = False,
    is_checkout_day: bool = False,
    stay_nights: int = 0,
) -> Optional[str]:
    """
    Determine the trigger condition based on context.

    Args:
        intent: The intent that was classified
        is_first_question: Whether this is the first question of the stay
        is_checkout_day: Whether today is checkout day
        stay_nights: Number of nights in the stay

    Returns:
        Trigger condition string if applicable
    """
    if is_first_question:
        return "first_question"

    if intent:
        intent_lower = intent.lower()
        if "wifi" in intent_lower or "password" in intent_lower:
            return "after_wifi"
        if "direction" in intent_lower or "address" in intent_lower:
            return "after_directions"
        if "restaurant" in intent_lower or "recommendation" in intent_lower:
            return "after_local_info"

    if is_checkout_day:
        return "checkout_day"

    if stay_nights > 5:
        return "long_stay"

    return None


# ============================================================================
# Default Tips
# ============================================================================

DEFAULT_TIPS = [
    {
        "tip_type": "sms_offer",
        "title": "SMS Feature",
        "message": "I can text you important info like WiFi passwords, door codes, and directions! Just say 'text me that' after any response.",
        "trigger_condition": "first_question",
        "priority": 100,
        "max_shows_per_stay": 1,
    },
    {
        "tip_type": "sms_offer",
        "title": "Text WiFi",
        "message": "Would you like me to text you the WiFi details so you have them handy?",
        "trigger_condition": "after_wifi",
        "priority": 90,
        "max_shows_per_stay": 1,
    },
    {
        "tip_type": "feature_hint",
        "title": "Local Recommendations",
        "message": "I know great local spots for food, coffee, and activities. Just ask!",
        "trigger_condition": "first_question",
        "priority": 50,
        "max_shows_per_stay": 1,
    },
    {
        "tip_type": "checkout_reminder",
        "title": "Checkout Info",
        "message": "Checkout is at 11 AM. Ask me about checkout procedures if you need a reminder!",
        "trigger_condition": "checkout_day",
        "priority": 80,
        "max_shows_per_stay": 1,
    },
]
