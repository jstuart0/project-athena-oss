"""
Proactive SMS Scheduler.

Background task system for sending scheduled SMS messages based on:
- Time relative to check-in (e.g., 24 hours before)
- Time relative to check-out (e.g., morning of checkout)
- Specific times of day
- Stay duration conditions

Also handles the delayed SMS queue for "text me in X hours" requests.
"""

import asyncio
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import Optional, List, Dict, Any
import structlog
import httpx

logger = structlog.get_logger(__name__)

# Admin backend URL
ADMIN_BACKEND_URL = "http://localhost:8080"


class SMSScheduler:
    """
    Background scheduler for proactive and delayed SMS messages.

    Runs periodically to:
    1. Check for scheduled SMS that should be sent based on stay events
    2. Process the delayed SMS queue
    """

    def __init__(
        self,
        admin_url: str = ADMIN_BACKEND_URL,
        check_interval_seconds: int = 60,
    ):
        self.admin_url = admin_url
        self.check_interval = check_interval_seconds
        self._client = httpx.AsyncClient(timeout=30.0)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the scheduler background task."""
        if self._running:
            logger.warning("scheduler_already_running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("sms_scheduler_started", interval=self.check_interval)

    async def stop(self):
        """Stop the scheduler background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("sms_scheduler_stopped")

    async def _run_loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                await self._process_scheduled_sms()
                await self._process_pending_queue()
            except Exception as e:
                logger.exception("scheduler_error", error=str(e))

            await asyncio.sleep(self.check_interval)

    async def _process_scheduled_sms(self):
        """
        Process scheduled SMS based on stay events.

        Checks all active scheduled SMS configurations against
        current and upcoming stays.
        """
        try:
            # Get scheduled SMS configurations that are due
            response = await self._client.get(
                f"{self.admin_url}/api/sms/scheduled/due",
            )

            if response.status_code != 200:
                logger.warning("scheduled_fetch_failed", status=response.status_code)
                return

            data = response.json()
            due_items = data.get("items", [])

            for item in due_items:
                await self._send_scheduled_item(item)

        except Exception as e:
            logger.exception("scheduled_sms_error", error=str(e))

    async def _send_scheduled_item(self, item: Dict[str, Any]):
        """Send a single scheduled SMS item."""
        try:
            # Get template content if using template
            content = item.get("custom_message")
            if not content and item.get("template_id"):
                template = await self._get_template(item["template_id"])
                if template:
                    content = self._render_template(
                        template["body"],
                        template.get("variables", []),
                        item.get("stay_data", {}),
                    )

            if not content:
                logger.warning("scheduled_no_content", item_id=item.get("id"))
                return

            # Send via SMS service
            response = await self._client.post(
                f"{self.admin_url}/api/sms/send",
                json={
                    "to_number": item["phone_number"],
                    "content": content,
                    "calendar_event_id": item.get("calendar_event_id"),
                    "content_type": "scheduled",
                    "scheduled_sms_id": item.get("scheduled_sms_id"),
                },
            )

            if response.status_code == 200:
                logger.info(
                    "scheduled_sms_sent",
                    scheduled_id=item.get("scheduled_sms_id"),
                    event_id=item.get("calendar_event_id"),
                )
            else:
                logger.error(
                    "scheduled_sms_failed",
                    scheduled_id=item.get("scheduled_sms_id"),
                    status=response.status_code,
                )

        except Exception as e:
            logger.exception("scheduled_item_error", error=str(e), item_id=item.get("id"))

    async def _get_template(self, template_id: int) -> Optional[Dict[str, Any]]:
        """Fetch an SMS template by ID."""
        try:
            response = await self._client.get(
                f"{self.admin_url}/api/sms/templates/{template_id}",
            )
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logger.exception("template_fetch_error", error=str(e))
            return None

    def _render_template(
        self,
        template: str,
        variables: List[str],
        data: Dict[str, Any],
    ) -> str:
        """
        Render a template with variable substitution.

        Args:
            template: Template string with {variable} placeholders
            variables: List of expected variable names
            data: Dictionary of values to substitute

        Returns:
            Rendered template string
        """
        result = template
        for var in variables:
            placeholder = "{" + var + "}"
            value = str(data.get(var, ""))
            result = result.replace(placeholder, value)
        return result

    async def _process_pending_queue(self):
        """
        Process the delayed SMS queue.

        Sends any pending SMS messages whose scheduled time has arrived.
        """
        try:
            now = datetime.now(timezone.utc)

            response = await self._client.get(
                f"{self.admin_url}/api/sms/pending/due",
                params={"before": now.isoformat()},
            )

            if response.status_code != 200:
                return

            data = response.json()
            pending_items = data.get("items", [])

            for item in pending_items:
                await self._send_pending_item(item)

        except Exception as e:
            logger.exception("pending_queue_error", error=str(e))

    async def _send_pending_item(self, item: Dict[str, Any]):
        """Send a single pending SMS item."""
        try:
            response = await self._client.post(
                f"{self.admin_url}/api/sms/send",
                json={
                    "to_number": item["phone_number"],
                    "content": item["content"],
                    "calendar_event_id": item.get("calendar_event_id"),
                    "content_type": item.get("content_type", "delayed"),
                    "pending_sms_id": item["id"],
                },
            )

            if response.status_code == 200:
                logger.info("pending_sms_sent", pending_id=item["id"])
            else:
                logger.error(
                    "pending_sms_failed",
                    pending_id=item["id"],
                    status=response.status_code,
                )

        except Exception as e:
            logger.exception("pending_item_error", error=str(e), item_id=item["id"])

    async def close(self):
        """Close resources."""
        await self.stop()
        await self._client.aclose()


# ============================================================================
# Trigger Time Calculation
# ============================================================================

def calculate_trigger_time(
    stay_checkin: datetime,
    stay_checkout: datetime,
    trigger_type: str,
    trigger_offset_hours: int = 0,
    trigger_time: Optional[dt_time] = None,
) -> Optional[datetime]:
    """
    Calculate when a scheduled SMS should be sent.

    Args:
        stay_checkin: Check-in datetime
        stay_checkout: Check-out datetime
        trigger_type: Type of trigger ('before_checkin', 'after_checkin', etc.)
        trigger_offset_hours: Hours offset from the event
        trigger_time: Specific time of day (for time_of_day trigger)

    Returns:
        DateTime when the message should be sent, or None if not applicable
    """
    if trigger_type == "before_checkin":
        return stay_checkin - timedelta(hours=trigger_offset_hours)

    elif trigger_type == "after_checkin":
        return stay_checkin + timedelta(hours=trigger_offset_hours)

    elif trigger_type == "before_checkout":
        return stay_checkout - timedelta(hours=trigger_offset_hours)

    elif trigger_type == "after_checkout":
        return stay_checkout + timedelta(hours=trigger_offset_hours)

    elif trigger_type == "time_of_day" and trigger_time:
        # Send at specific time on check-in day
        return datetime.combine(stay_checkin.date(), trigger_time, tzinfo=stay_checkin.tzinfo)

    elif trigger_type == "checkout_morning":
        # Morning of checkout day (default 9 AM)
        morning = trigger_time or dt_time(9, 0)
        return datetime.combine(stay_checkout.date(), morning, tzinfo=stay_checkout.tzinfo)

    return None


# ============================================================================
# Delayed SMS Queue Helper
# ============================================================================

async def schedule_delayed_sms(
    phone_number: str,
    content: str,
    delay_hours: float,
    calendar_event_id: Optional[int] = None,
    content_type: str = "delayed",
    session_id: Optional[str] = None,
    admin_url: str = ADMIN_BACKEND_URL,
) -> bool:
    """
    Schedule an SMS to be sent after a delay.

    Args:
        phone_number: Recipient phone number
        content: Message content
        delay_hours: Hours to wait before sending
        calendar_event_id: Associated calendar event
        content_type: Type of content being sent
        session_id: Current session ID
        admin_url: Admin backend URL

    Returns:
        True if successfully scheduled
    """
    try:
        scheduled_for = datetime.now(timezone.utc) + timedelta(hours=delay_hours)

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{admin_url}/api/sms/pending",
                json={
                    "phone_number": phone_number,
                    "content": content,
                    "scheduled_for": scheduled_for.isoformat(),
                    "calendar_event_id": calendar_event_id,
                    "content_type": content_type,
                    "session_id": session_id,
                },
            )

            if response.status_code == 200:
                logger.info(
                    "delayed_sms_scheduled",
                    delay_hours=delay_hours,
                    scheduled_for=scheduled_for.isoformat(),
                )
                return True
            else:
                logger.error("delayed_sms_schedule_failed", status=response.status_code)
                return False

    except Exception as e:
        logger.exception("delayed_sms_error", error=str(e))
        return False


# ============================================================================
# Singleton Instance
# ============================================================================

_scheduler: Optional[SMSScheduler] = None


async def get_scheduler() -> SMSScheduler:
    """Get the singleton scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SMSScheduler()
    return _scheduler


async def start_scheduler():
    """Start the scheduler background task."""
    scheduler = await get_scheduler()
    await scheduler.start()


async def stop_scheduler():
    """Stop the scheduler background task."""
    global _scheduler
    if _scheduler:
        await _scheduler.stop()
        await _scheduler.close()
        _scheduler = None
