"""Notification preference opt-in/opt-out handler (main.py:7964-8117)."""

import time

import structlog

from orchestrator.state import OrchestratorState
from orchestrator.urls import NOTIFICATIONS_SERVICE_URL

logger = structlog.get_logger(__name__)


async def notification_pref_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle notification preference changes via voice commands.

    Examples:
    - "Stop the morning notifications" -> opt-out of morning_greeting
    - "I don't want morning updates" -> opt-out of morning_greeting
    - "Turn morning updates back on" -> opt-in to morning_greeting
    - "Enable notifications" -> opt-in to all
    - "Pause notifications" -> opt-out of all

    Uses the notifications service at NOTIFICATIONS_SERVICE_URL.
    """
    import httpx

    start = time.time()
    query_lower = state.query.lower()

    # Determine if opt-in or opt-out
    opt_out_keywords = ["stop", "disable", "turn off", "don't want", "no more", "pause", "opt out"]
    opt_in_keywords = ["start", "enable", "turn on", "resume", "opt in", "back on", "want"]

    is_opt_out = any(kw in query_lower for kw in opt_out_keywords)
    is_opt_in = any(kw in query_lower for kw in opt_in_keywords)

    # If both or neither, default based on common patterns
    if is_opt_out == is_opt_in:
        # "I want morning updates" vs "I don't want morning updates"
        if "don't" in query_lower or "not" in query_lower:
            is_opt_out = True
            is_opt_in = False
        else:
            # Ambiguous - default to opt-out since most voice requests are to stop something
            is_opt_out = True
            is_opt_in = False

    action = "opt-out" if is_opt_out else "opt-in"

    # Determine which rule(s) are affected
    rule_slugs = []
    if "morning" in query_lower or "greeting" in query_lower:
        rule_slugs.append("morning_greeting")
    if "alert" in query_lower:
        rule_slugs.append("fridge_open_alert")
        rule_slugs.append("door_unlocked_alert")
        rule_slugs.append("tesla_charge_alert")
    if "weather" in query_lower:
        rule_slugs.append("morning_greeting")  # Weather is part of morning greeting

    # If no specific rule identified, assume morning_greeting (most common)
    if not rule_slugs:
        rule_slugs = ["morning_greeting"]

    # Get room from state
    room = state.room or "office"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            results = []

            for rule_slug in rule_slugs:
                endpoint = f"{NOTIFICATIONS_SERVICE_URL}/api/preferences/{action}"
                payload = {
                    "rule_slug": rule_slug,
                    "room": room,
                    "reason": "voice_command"
                }

                logger.info(
                    "notification_pref_request",
                    action=action,
                    rule_slug=rule_slug,
                    room=room,
                    endpoint=endpoint
                )

                response = await client.post(endpoint, json=payload)

                if response.status_code == 200:
                    result = response.json()
                    results.append({
                        "rule": rule_slug,
                        "status": result.get("status"),
                        "success": True
                    })
                elif response.status_code == 404:
                    # Rule not found - might not be configured yet
                    results.append({
                        "rule": rule_slug,
                        "status": "rule_not_found",
                        "success": False
                    })
                else:
                    results.append({
                        "rule": rule_slug,
                        "status": "error",
                        "success": False,
                        "error": response.text
                    })

            # Build response message
            successful = [r for r in results if r["success"]]
            failed = [r for r in results if not r["success"]]

            if action == "opt-out":
                if successful:
                    if "morning_greeting" in [r["rule"] for r in successful]:
                        state.answer = "Okay, I've turned off the morning notifications for this room. Just say 'turn morning updates back on' whenever you'd like them again."
                    else:
                        rule_names = ", ".join([r["rule"].replace("_", " ") for r in successful])
                        state.answer = f"Done, I've disabled notifications for: {rule_names}. Let me know when you want them back."
                elif failed:
                    if any(r.get("status") == "rule_not_found" for r in failed):
                        state.answer = "I couldn't find that notification rule. The proactive notification system may still be setting up."
                    else:
                        state.answer = "I'm having trouble updating your notification preferences right now. Please try again later."
                        state.is_fallback = True
            else:  # opt-in
                if successful:
                    if "morning_greeting" in [r["rule"] for r in successful]:
                        state.answer = "Great, I've turned morning notifications back on for this room. You'll start getting them again tomorrow."
                    else:
                        rule_names = ", ".join([r["rule"].replace("_", " ") for r in successful])
                        state.answer = f"Done, I've re-enabled notifications for: {rule_names}."
                elif failed:
                    if any(r.get("status") == "already_opted_in" for r in failed):
                        state.answer = "You're already receiving those notifications."
                    elif any(r.get("status") == "rule_not_found" for r in failed):
                        state.answer = "I couldn't find that notification rule. The proactive notification system may still be setting up."
                    else:
                        state.answer = "I'm having trouble updating your notification preferences right now. Please try again later."
                        state.is_fallback = True

            logger.info(
                "notification_pref_complete",
                action=action,
                results=results,
                answer=state.answer[:100]
            )

    except httpx.ConnectError:
        logger.warning("notification_service_unreachable", url=NOTIFICATIONS_SERVICE_URL)
        state.answer = "The notification service isn't available right now. Please try again later."
        state.error = "Notifications service unreachable"

    except Exception as e:
        logger.error(f"notification_pref_error: {e}", exc_info=True)
        state.answer = "I had trouble updating your notification preferences. Please try again."
        state.error = f"Notification preference update failed: {str(e)}"

    notif_pref_duration = time.time() - start
    state.node_timings["notification_pref"] = notif_pref_duration
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "notification_pref", notif_pref_duration)

    return state
