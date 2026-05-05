"""SMS handler for 'text me that' voice intent."""

import logging
import time

from orchestrator.state import OrchestratorState
from sms.service import get_sms_service

logger = logging.getLogger(__name__)


async def send_sms_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle "text me that" requests by sending the previous response via SMS.

    This node:
    1. Gets the previous response from conversation history
    2. Extracts textable content
    3. Queues SMS for sending via admin backend
    """
    from sms.text_me_that import handle_text_me_that

    start = time.time()

    try:
        # Get the previous assistant response from conversation history
        previous_response = None
        for msg in reversed(state.conversation_history):
            if msg.get("role") == "assistant":
                previous_response = msg.get("content", "")
                break

        if not previous_response:
            state.answer = "I don't have a previous message to text you. Could you ask me something first?"
            state.node_timings["send_sms"] = time.time() - start
            return state

        # Get guest's phone number from context
        context = state.context or {}
        phone_number = context.get("phone_number") or context.get("guest_phone")
        calendar_event_id = context.get("calendar_event_id")

        if not phone_number:
            # No phone number available - prompt user for it
            state.answer = (
                "I'd be happy to text that to you! "
                "Could you tell me your phone number? "
                "Just say it like 'four one zero, five five five, one two three four'."
            )
            send_sms_duration = time.time() - start
            state.node_timings["send_sms"] = send_sms_duration
            if state.timing_tracker:
                state.timing_tracker.track_sync("graph", "send_sms", send_sms_duration)
            return state

        # Get SMS service (will be in test mode if Twilio not configured)
        try:
            sms_service = await get_sms_service()
        except Exception as e:
            logger.warning(f"Could not initialize SMS service: {e}")
            sms_service = None

        # Use the text_me_that handler to process and send
        result = await handle_text_me_that(
            query=state.query,
            conversation_history=state.conversation_history,
            guest_phone=phone_number,
            sms_service=sms_service,
            calendar_event_id=calendar_event_id,
        )

        if result.get("success"):
            state.answer = result.get("answer", "Done! I've texted that information to you.")
        elif result.get("needs_phone"):
            state.answer = result.get("answer", "What phone number should I send it to?")
        else:
            state.answer = result.get("answer", "I'm sorry, I couldn't send the text right now. Please try again.")

    except Exception as e:
        logger.error(f"SMS send error: {e}", exc_info=True)
        state.answer = "I'm having trouble sending the text. Please try again later."
        state.error = f"SMS send failed: {str(e)}"

    send_sms_duration = time.time() - start
    state.node_timings["send_sms"] = send_sms_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "send_sms", send_sms_duration)
    return state
