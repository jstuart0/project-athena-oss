"""Notification preference opt-in/opt-out handler (main.py:7964-8117)."""

import re
import time
from typing import Optional

import structlog

from orchestrator.helpers import configured_assistant_names
from orchestrator.mode_permission import (
    STAY_READ_ONLY_REFUSAL,
    check_intent_permission,
    intent_refusal_message,
    is_stay_read_only,
)
from orchestrator.state import IntentCategory, OrchestratorState
from orchestrator.urls import NOTIFICATIONS_SERVICE_URL
from orchestrator.utterance_kind import UtteranceKind, classify_utterance

logger = structlog.get_logger(__name__)

STATE_QUESTION_REPLY = (
    "I can't check notification settings, but I can change them. Say 'stop morning "
    "notifications' or 'turn on morning notifications'."
)
AMBIGUOUS_REPLY = (
    "Do you want morning notifications off or on? Say 'stop morning notifications' "
    "or 'turn on morning notifications'."
)

# An explicit first-person desire makes an UNKNOWN utterance a request.
_DESIRE_PHRASE_RE = re.compile(
    r"\bi (?:don't|do not) want\b|\bno more\b|\bi(?:'d| would) like\b|\bopt[ -]?(?:in|out)\b|\bunsubscribe\b"
)
_NEGATED_WANT_RE = re.compile(r"\b(?:don't|do not) want\b")
_OPT_OUT_RE = re.compile(
    r"\b(?:stop|disable|turn off|pause|no more|opt[ -]?out|unsubscribe)\b|\b(?:don't|do not) want\b"
)
_OPT_IN_RE = re.compile(
    r"\b(?:start|enable|turn on|resume|back on|opt[ -]?in|want)\b|\bi(?:'d| would) like\b"
)


def _direction(query_lower: str) -> Optional[str]:
    """"opt-out", "opt-in", or None when the utterance names neither or both."""
    opts_out = bool(_OPT_OUT_RE.search(query_lower))
    opts_in = bool(_OPT_IN_RE.search(_NEGATED_WANT_RE.sub(" ", query_lower)))
    if opts_out == opts_in:
        return None
    return "opt-out" if opts_out else "opt-in"


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

    Writes only when the caller may use this intent, the utterance is a
    command (or states a first-person desire), and it names exactly one
    direction. A question gets instructions instead; an unclear request
    gets asked which way.
    """
    import httpx

    start = time.time()

    # Permission first, before any I/O (including the assistant-name read).
    # An SMS from outside the current stay is answer-only (the intent gate
    # already refuses this intent for it; this is the writer's own check).
    if is_stay_read_only(state.permissions):
        state.answer = STAY_READ_ONLY_REFUSAL
        state.error = "permission_denied"
        return _finish(state, start)
    if not check_intent_permission(IntentCategory.NOTIFICATION_PREF, state.permissions or {}):
        state.answer = intent_refusal_message(state.permissions)
        state.error = "permission_denied"
        logger.warning("notification_pref_denied", mode=(state.permissions or {}).get("mode"))
        return _finish(state, start)

    query_lower = state.query.lower()

    # D6 write rule: only a command (or an explicit first-person desire)
    # with exactly one direction changes a setting. A question never writes.
    kind = classify_utterance(state.query, assistant_names=await configured_assistant_names()).kind
    if kind == UtteranceKind.STATE_QUESTION:
        state.answer = STATE_QUESTION_REPLY
        return _finish(state, start)
    is_request = kind == UtteranceKind.IMPERATIVE or (
        kind == UtteranceKind.UNKNOWN and bool(_DESIRE_PHRASE_RE.search(query_lower))
    )
    action = _direction(query_lower) if is_request else None
    if action is None:
        state.answer = AMBIGUOUS_REPLY
        return _finish(state, start)

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

    return _finish(state, start)


def _finish(state: OrchestratorState, start: float) -> OrchestratorState:
    duration = time.time() - start
    state.node_timings["notification_pref"] = duration
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "notification_pref", duration)
    return state
