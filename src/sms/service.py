"""
SMS Service for Twilio integration.

Handles sending SMS messages with:
- Automatic message splitting for long content
- Test mode support (logs without sending)
- Rate limiting
- Cost tracking
- Error handling and retry logic
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Any

from .splitter import split_for_sms

logger = logging.getLogger(__name__)


class SMSService:
    """
    Service for sending SMS messages via Twilio.

    Supports test mode for development, automatic message splitting,
    and cost tracking.
    """

    # Twilio pricing (US, approximate - update periodically)
    OUTGOING_SMS_CENTS = 1  # ~$0.0079 rounded up
    INCOMING_SMS_CENTS = 1  # ~$0.0075 rounded up
    PHONE_NUMBER_MONTHLY_CENTS = 115  # ~$1.15/month

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize SMS service with Twilio credentials.

        Args:
            config: Dictionary containing:
                - account_sid: Twilio account SID
                - api_key_sid: Twilio API key SID (preferred over auth token)
                - api_key_secret: Twilio API key secret
                - from_number: Phone number to send from
                - test_mode: If True, log messages without sending
                - rate_limit_per_minute: Max messages per minute
        """
        self.account_sid = config.get("account_sid")
        self.api_key_sid = config.get("api_key_sid")
        self.api_key_secret = config.get("api_key_secret")
        self.from_number = config.get("from_number")
        self.test_mode = config.get("test_mode", True)
        self.rate_limit = config.get("rate_limit_per_minute", 10)

        self._client = None
        self._last_send_times: List[datetime] = []

        if not self.test_mode:
            self._init_twilio_client()

    def _init_twilio_client(self) -> None:
        """Initialize Twilio client with credentials."""
        try:
            from twilio.rest import Client

            if self.api_key_sid and self.api_key_secret:
                # Preferred: API key authentication
                self._client = Client(
                    self.api_key_sid,
                    self.api_key_secret,
                    self.account_sid
                )
            else:
                logger.warning("Twilio credentials not configured, running in test mode")
                self.test_mode = True
        except ImportError:
            logger.error("twilio package not installed, running in test mode")
            self.test_mode = True
        except Exception as e:
            logger.error(f"Failed to initialize Twilio client: {e}")
            self.test_mode = True

    def _check_rate_limit(self) -> bool:
        """
        Check if we're within rate limits.

        Returns:
            True if send is allowed, False if rate limited
        """
        now = datetime.now(timezone.utc)

        # Clean old entries (older than 1 minute)
        one_minute_ago = now.timestamp() - 60
        self._last_send_times = [
            t for t in self._last_send_times
            if t.timestamp() > one_minute_ago
        ]

        if len(self._last_send_times) >= self.rate_limit:
            logger.warning(f"SMS rate limit reached ({self.rate_limit}/minute)")
            return False

        return True

    def _record_send(self) -> None:
        """Record a send for rate limiting."""
        self._last_send_times.append(datetime.now(timezone.utc))

    async def send_sms(
        self,
        to_number: str,
        content: str,
        calendar_event_id: Optional[int] = None,
        content_type: Optional[str] = None,
    ) -> Tuple[bool, str, int]:
        """
        Send SMS message with automatic splitting for long content.

        Args:
            to_number: Destination phone number (E.164 format preferred)
            content: Message content (will be split if too long)
            calendar_event_id: Optional calendar event ID for tracking
            content_type: Optional content type for categorization

        Returns:
            Tuple of (success, sid_or_error, segment_count)
        """
        # Validate inputs
        if not to_number:
            return False, "No phone number provided", 0

        if not content:
            return False, "No content provided", 0

        # Normalize phone number
        to_number = self._normalize_phone(to_number)

        # Check rate limit
        if not self._check_rate_limit():
            return False, "Rate limit exceeded", 0

        # Split message if needed
        segments = split_for_sms(content)
        segment_count = len(segments)

        if self.test_mode:
            logger.info(
                f"[TEST MODE] Would send {segment_count} segment(s) to {to_number}\n"
                f"Content preview: {content[:100]}..."
            )
            return True, "test_mode", segment_count

        # Send each segment
        sids = []
        for i, segment in enumerate(segments):
            try:
                message = self._client.messages.create(
                    body=segment,
                    from_=self.from_number,
                    to=to_number
                )
                sids.append(message.sid)
                self._record_send()

                logger.info(
                    f"SMS sent: {message.sid} to {to_number} "
                    f"(segment {i+1}/{segment_count})"
                )

                # Small delay between segments to ensure delivery order
                if i < segment_count - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                error_msg = str(e)
                logger.error(f"SMS send failed: {error_msg}")
                return False, error_msg, i

        return True, ",".join(sids), segment_count

    def _normalize_phone(self, phone: str) -> str:
        """
        Normalize phone number to E.164 format.

        Args:
            phone: Phone number in various formats

        Returns:
            Normalized phone number
        """
        # Remove common formatting characters
        phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace(".", "")

        # Ensure US numbers have +1 prefix
        if phone.startswith("1") and len(phone) == 11:
            phone = "+" + phone
        elif len(phone) == 10:
            phone = "+1" + phone
        elif not phone.startswith("+"):
            phone = "+" + phone

        return phone

    def calculate_cost_cents(self, segment_count: int, direction: str = "outgoing") -> int:
        """
        Calculate estimated cost in cents for SMS message.

        Args:
            segment_count: Number of SMS segments
            direction: 'outgoing' or 'incoming'

        Returns:
            Cost in cents
        """
        if direction == "outgoing":
            return segment_count * self.OUTGOING_SMS_CENTS
        else:
            return segment_count * self.INCOMING_SMS_CENTS

    @classmethod
    async def from_admin_config(cls) -> "SMSService":
        """
        Create SMS service from admin backend configuration.

        Fetches Twilio credentials from:
        1. Environment variables (preferred for local/dev)
        2. Admin backend external_api_keys (for production)
        """
        import os
        import httpx

        # Check environment variables first (preferred)
        env_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        env_api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
        env_api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
        env_from_number = os.environ.get("TWILIO_FROM_NUMBER")
        env_test_mode = os.environ.get("TWILIO_TEST_MODE", "false").lower() == "true"

        if env_account_sid and env_api_key_sid and env_api_key_secret and env_from_number:
            logger.info("Using Twilio credentials from environment variables")
            config = {
                "account_sid": env_account_sid,
                "api_key_sid": env_api_key_sid,
                "api_key_secret": env_api_key_secret,
                "from_number": env_from_number,
                "test_mode": env_test_mode,
                "rate_limit_per_minute": int(os.environ.get("TWILIO_RATE_LIMIT", "10")),
            }
            return cls(config)

        # Fallback to admin backend
        admin_url = "http://localhost:8080"

        async with httpx.AsyncClient() as client:
            # Get SMS settings
            try:
                settings_resp = await client.get(f"{admin_url}/api/sms/settings")
                settings = settings_resp.json() if settings_resp.status_code == 200 else {}
            except Exception as e:
                logger.warning(f"Could not fetch SMS settings: {e}")
                settings = {}

            # Get Twilio credentials from external API keys
            try:
                twilio_resp = await client.get(
                    f"{admin_url}/external-api-keys/twilio",
                    timeout=5.0
                )
                if twilio_resp.status_code == 200:
                    twilio_config = twilio_resp.json()
                else:
                    twilio_config = {}
            except Exception as e:
                logger.warning(f"Could not fetch Twilio credentials: {e}")
                twilio_config = {}

        config = {
            "account_sid": twilio_config.get("api_key"),  # Account SID stored in api_key
            "api_key_sid": twilio_config.get("api_key2"),  # API Key SID
            "api_key_secret": twilio_config.get("api_key3"),  # API Key Secret
            "from_number": settings.get("from_number") or twilio_config.get("extra_config", {}).get("from_number"),
            "test_mode": settings.get("test_mode", True),
            "rate_limit_per_minute": settings.get("rate_limit_per_minute", 10),
        }

        return cls(config)


# Singleton instance for reuse
_sms_service: Optional[SMSService] = None


async def get_sms_service() -> SMSService:
    """
    Get or create SMS service singleton.

    Returns:
        Configured SMSService instance
    """
    global _sms_service

    if _sms_service is None:
        _sms_service = await SMSService.from_admin_config()

    return _sms_service
