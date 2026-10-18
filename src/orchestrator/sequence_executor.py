"""
Sequence Executor for Smart Home Commands

Executes sequences of smart home actions with delays and scheduled times.
Supports:
- Relative delays: {"delay_after": 5} - wait 5 seconds after action
- Absolute times: {"at_time": "18:00"} - execute at specific time
- Mixed sequences: combine delays and scheduled times
- Any entity type: lights, climate, media players, etc.
"""
import asyncio
import contextlib
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Any
import json

from orchestrator import access_target
from orchestrator.room_context import SERVER_DECISION_KEYS
from shared.local_time import local_now, local_tz

logger = logging.getLogger(__name__)

UNGROUNDED_ENTITY_STEP_REFUSAL = "I didn't run that step because I couldn't tell which device you meant."
UNGROUNDED_ROOM_STEP_REFUSAL = "I didn't run that step because I couldn't tell which room you meant."
EFFECT_STEP_REFUSAL = "I can't set a light effect in a sequence yet."
ONE_DOOR_STEP_REFUSAL = "I can unlock or open one door at a time. Name the door, for example 'unlock the front door'."

# ATHENA-69: imported lazily inside SequenceExecutor (not at module scope)
# so a caller that only needs the pure has_sequence_timing/
# detect_sequence_intent helpers below doesn't pull in
# orchestrator.mode_permission -> orchestrator.metrics -> prometheus_client
# as a hard import-time dependency.


# ---------------------------------------------------------------------------
# Shared sequence-timing matcher (ATHENA-88 / F87, D1/D3).
#
# Single word-boundary-anchored implementation, shared by this module's
# detect_sequence_intent (classify gate, action-gated bare temporals) and
# SmartHomeController.detect_sequence_intent (controller path, unconditional
# bare temporals). Follows the compiled-pattern idiom of
# search_providers/intent_classifier.py:28-70.
# ---------------------------------------------------------------------------

_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
)

_LEADING_ACTION_VERB_RE = re.compile(
    r"^(?:please\s+|hey\s+|(?:can|could|would|will)\s+you\s+)*"
    r"(?:turn|switch|set|dim|brighten|lock|unlock|open|close|start|stop|"
    r"play|pause|run|remind|activate|arm|disarm)\b",
    re.IGNORECASE,
)

# Explicit timing: counts unconditionally, no leading-verb gate required.
_EXPLICIT_TIMING_PATTERNS = [
    re.compile(
        r"\bat\s+(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?=\W|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bat\s+(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*o'?clock\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bat\s+(?:noon|midnight|sunset|sunrise|dawn|dusk)\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:at\s+)?(?:{_NUMBER_WORDS})\s*(?:o'?clock|a\.?m\.?|p\.?m\.?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?=\W|$)",
        re.IGNORECASE,
    ),
    re.compile(r"\bo'?clock\b", re.IGNORECASE),
    re.compile(
        r"\b(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"fifteen|twenty|thirty|forty[- ]five|few|a few|couple of|a couple of)\s+"
        r"(?:second|minute|hour)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:wait|pause|delay)\b", re.IGNORECASE),
    re.compile(r"\bthen\b", re.IGNORECASE),
    re.compile(r"\bafter that\b", re.IGNORECASE),
    re.compile(
        r"\b(?:flash(?:es|ed|ing)?|blink(?:s|ed|ing)?|repeat(?:s|ed|ing)?|"
        r"strob(?:e|es|ed|ing)|cycl(?:e|es|ed|ing)|loop(?:s|ed|ing)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:\d+|{_NUMBER_WORDS}|several|few|(?:a )?couple(?: of)?|multiple)\s+times\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!how )\bmany\s+times\b", re.IGNORECASE),
    re.compile(r"\b(?:twice|thrice)\b", re.IGNORECASE),
    re.compile(r"\b(?:on and off|off and on|on then off|off then on)\b", re.IGNORECASE),
    re.compile(r"\bschedule\b", re.IGNORECASE),
]

# Action-gated: bare temporal words and bare/trailing "at <number>" only count
# when the classify gate requires a leading device verb (require_action_for_
# bare_temporal=True); they count unconditionally on the controller path.
_BARE_TEMPORAL_WORDS_RE = re.compile(
    r"\b(?:tonight|tomorrow|morning|afternoon|evening|later|again|noon|"
    r"midnight|sunset|sunrise|dusk|dawn)\b",
    re.IGNORECASE,
)

_BARE_AT_NUMBER_RE = re.compile(
    r"\bat\s+(?:1[0-2]|0?[1-9])(?::[0-5]\d)?(?![\d%:])(?!\s*(?:percent|degrees|%))",
    re.IGNORECASE,
)

_TRAILING_AT_NUMBER_WORD_RE = re.compile(
    rf"\bat\s+(?:{_NUMBER_WORDS})[?.!]*$",
    re.IGNORECASE,
)


def has_sequence_timing(query_lower: str, *, require_action_for_bare_temporal: bool) -> bool:
    """
    Word-boundary-anchored check for sequence-timing language (delays, loops,
    schedules) in an already-lowercased query.

    Explicit timing (a meridiem/o'clock anchor, durations, loop verbs,
    counts, wait/then/schedule) counts unconditionally. Bare temporal words
    ("tonight", "later", ...) and bare/trailing "at <number>" only count when
    `require_action_for_bare_temporal` is False, or when it's True and the
    query starts with an imperative device verb.
    """
    for pattern in _EXPLICIT_TIMING_PATTERNS:
        if pattern.search(query_lower):
            return True

    has_bare_temporal = (
        _BARE_TEMPORAL_WORDS_RE.search(query_lower) is not None
        or _BARE_AT_NUMBER_RE.search(query_lower) is not None
        or _TRAILING_AT_NUMBER_WORD_RE.search(query_lower) is not None
    )
    if not has_bare_temporal:
        return False

    if require_action_for_bare_temporal:
        return _LEADING_ACTION_VERB_RE.match(query_lower) is not None
    return True


def _next_local_occurrence(now: datetime, wall: time, tz) -> datetime:
    """The first UTC instant after ``now`` at which the property's clock reads
    ``wall``: today if that's still ahead, otherwise tomorrow.

    DST is resolved explicitly. An ambiguous time (the fall-back repeated
    hour) is its first occurrence (fold=0) unless that has already passed
    and the second (fold=1) hasn't, so at 01:30 standard time "01:45" is 15
    minutes away, not a day. A nonexistent time (inside the spring-forward
    gap) moves forward: 02:30 becomes 03:30 daylight time.
    """
    now_utc = now.astimezone(timezone.utc)
    local_date = now.astimezone(tz).date()
    for day_offset in range(3):
        day = local_date + timedelta(days=day_offset)
        for fold in (0, 1):
            candidate = datetime.combine(day, wall, tzinfo=tz).replace(fold=fold).astimezone(timezone.utc)
            if candidate > now_utc:
                return candidate
    raise AssertionError("no local occurrence within three days")  # unreachable: a day always has one


def _is_hue_saturation(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple)) and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
        and 0 <= value[0] <= 360 and 0 <= value[1] <= 100
    )


def _step_parameters(step: Dict) -> Dict:
    """A step's parameters as a dict: a missing, null or non-object value
    (a model writes ``"parameters": null``) is empty, so the step still runs.
    The prompt asks for a single ``hs_color``; the room light writer reads
    ``hs_colors``, so a lone pair becomes a list of one. A valid ``hs_colors``
    (a non-empty list of in-range hue/saturation pairs) wins; an invalid one
    is dropped. The step itself is not changed."""
    raw = step.get('parameters')
    parameters = dict(raw) if isinstance(raw, dict) else {}
    colors = parameters.get('hs_colors')
    if not (isinstance(colors, (list, tuple)) and colors and all(_is_hue_saturation(c) for c in colors)):
        parameters.pop('hs_colors', None)
        if _is_hue_saturation(parameters.get('hs_color')):
            parameters['hs_colors'] = [list(parameters['hs_color'])]
    return parameters


class SequenceExecutor:
    """Executes sequences of smart home actions with delays and scheduling."""

    def __init__(self, smart_controller, ha_client):
        """
        Initialize the sequence executor.

        Args:
            smart_controller: SmartHomeController instance for executing individual actions
            ha_client: Home Assistant client for direct API calls
        """
        from orchestrator.mode_permission import ensure_permission_enforcing

        self.smart_controller = smart_controller
        self.ha_client = ensure_permission_enforcing(ha_client)
        self._running_sequences: Dict[str, asyncio.Task] = {}

    async def execute_sequence(
        self,
        sequence: List[Dict],
        session_id: Optional[str] = None,
        background: bool = True
    ) -> str:
        """
        Execute a sequence of actions.

        Args:
            sequence: List of action steps, each containing:
                - action: The action to perform (turn_on, turn_off, set_color, etc.)
                - target: Target specification (device_type, room, entity_id)
                - parameters: Optional action parameters (brightness, color, etc.)
                - delay_after: Optional seconds to wait after this action
                - at_time: Optional time to execute this action (HH:MM format)
            session_id: Optional session ID for tracking
            background: If True, execute in background and return immediately

        Returns:
            Acknowledgment message
        """
        if not sequence:
            return "No actions to execute."

        # Calculate total sequence info for acknowledgment
        total_steps = len(sequence)
        has_delays = any(step.get('delay_after') for step in sequence)
        has_scheduled = any(step.get('at_time') for step in sequence)

        # Build acknowledgment message
        if has_scheduled:
            scheduled_times = [step.get('at_time') for step in sequence if step.get('at_time')]
            ack = f"Scheduled {total_steps} actions. First action at {scheduled_times[0]}."
        elif has_delays:
            total_delay = sum(step.get('delay_after', 0) for step in sequence)
            ack = f"Starting sequence of {total_steps} actions over {total_delay} seconds."
        else:
            ack = f"Executing {total_steps} actions."

        if background:
            # Execute in background
            task = asyncio.create_task(self._execute_sequence_steps(sequence, session_id))
            if session_id:
                self._running_sequences[session_id] = task
            logger.info(f"Started background sequence: {total_steps} steps, session={session_id}")
            return ack
        else:
            # Execute synchronously
            step_results = await self._execute_sequence_steps(sequence, session_id)
            refused = [r for r in step_results if r.get("status") == "refused_fanout"]
            if refused:
                skipped_text = " ".join(f"Skipped: {r['message']}" for r in refused)
                return f"Sequence complete. {skipped_text}"
            return "Sequence complete."

    async def _execute_sequence_steps(
        self,
        sequence: List[Dict],
        session_id: Optional[str] = None
    ):
        """Execute sequence steps with delays and scheduling.

        ATHENA-69 (D3): runs inside the request's scope when spawned via
        asyncio.create_task from execute_sequence's background branch --
        contextvars propagate a copy into the task at creation time, so
        `current_ha_scope()` is already set here for the normal
        route_control_node call path. When this is invoked with no scope
        open (e.g. a future caller that doesn't pre-authorize), a fresh
        system-mode scope is opened for the duration of the run instead of
        leaving every write unscoped (which the guard would otherwise
        treat as a series of independent throwaway baseline scopes, one
        per call, defeating the latch).
        """
        from orchestrator.mode_permission import current_ha_scope, ha_permission_scope
        from orchestrator import write_fanout

        step_results: List[Dict] = []

        scope_cm = ha_permission_scope(None, mode="system") if current_ha_scope() is None else contextlib.nullcontext()
        with scope_cm:
            for i, step in enumerate(sequence):
                try:
                    step_num = i + 1
                    total_steps = len(sequence)

                    # Handle scheduled time (at_time)
                    at_time = step.get('at_time')
                    if at_time:
                        wait_seconds = self._calculate_wait_until(at_time)
                        if wait_seconds > 0:
                            logger.info(f"Sequence step {step_num}/{total_steps}: waiting {wait_seconds}s until {at_time}")
                            await asyncio.sleep(wait_seconds)

                    # Execute the action. ATHENA-128 4.7: can_carry_pending
                    # is False here (outside pending_carrier, D16), so a
                    # fan-out block always returns the rewording refusal,
                    # never a dangling "Should I go ahead?" prompt.
                    result = await self._execute_step(step, step_num, total_steps)
                    blk = write_fanout.take_block()
                    if blk is not None:
                        domains = sorted({w.domain for w in blk.writes})
                        n = sum(len(w.entity_ids) for w in blk.writes)
                        logger.warning(
                            f"sequence_step_fanout_refused: step={step_num}/{total_steps} "
                            f"domains={domains} n={n}"
                        )
                        step_results.append({
                            "step": step_num,
                            "status": "refused_fanout",
                            "message": result,
                        })

                    # Handle delay after action
                    delay_after = step.get('delay_after')
                    if delay_after and delay_after > 0:
                        logger.info(f"Sequence step {step_num}/{total_steps}: waiting {delay_after}s")
                        await asyncio.sleep(delay_after)

                except asyncio.CancelledError:
                    logger.info(f"Sequence cancelled at step {i+1}")
                    raise
                except Exception as e:
                    logger.error(f"Error in sequence step {i+1}: {e}")
                    # Continue with next step on error
                    continue

            logger.info(f"Sequence complete: {len(sequence)} steps executed")

            # Clean up
            if session_id and session_id in self._running_sequences:
                del self._running_sequences[session_id]

        # The synchronous caller reports refused steps; for a background
        # run the sequence_step_fanout_refused log is the record.
        return step_results

    async def _execute_step(self, step: Dict, step_num: int, total_steps: int):
        """Execute a single sequence step."""
        action = step.get('action', 'turn_on')
        target = step.get('target', {})
        parameters = _step_parameters(step)

        # Extract target info
        device_type = target.get('device_type', 'light')
        room = target.get('room')
        entity_id = target.get('entity_id')

        logger.info(f"Executing step {step_num}/{total_steps}: {action} on {device_type} (has_room={bool(room)})")

        if await self._opening_unplanned(target, action):
            return ONE_DOOR_STEP_REFUSAL

        if entity_id and target.get('decided_entity') != entity_id:
            # Only the planner names a device (decided_entity, which a model
            # cannot set): a bare model entity id never reaches a write.
            logger.warning("sequence_entity_refused", extra={"reason": "entity_not_grounded"})
            return UNGROUNDED_ENTITY_STEP_REFUSAL

        if entity_id and isinstance(action, str) and action.strip().lower() in access_target.READ_ONLY_ACTIONS:
            # A read is not a service: there is nothing to call.
            return "I can't read a status in the middle of a sequence."

        if entity_id:
            # Direct entity control: gated on its own target like every
            # other write (a refusal is returned as the step result).
            return await self._execute_direct_action(entity_id, action, parameters)
        else:
            # Use smart controller for room-based control. ATHENA-128 4.7:
            # the result (a gate rewording, on a fan-out block) is
            # returned instead of discarded.
            if room and target.get('decided_room') != room:
                # The room is the server's decision (decided_room); a model's
                # room alone never aims a write.
                logger.warning("sequence_room_refused", extra={"reason": "room_not_decided"})
                return UNGROUNDED_ROOM_STEP_REFUSAL
            intent = {
                'device_type': device_type,
                'room': target.get('decided_room') or None,
                'action': action,
                'target_scope': target.get('target_scope', 'group'),
                'parameters': parameters,
                'color_description': parameters.get('color_description')
            }
            # The room was decided when the sequence was planned; the executor
            # never decides (and never asks).
            for key in SERVER_DECISION_KEYS + ('rooms', 'excluded_rooms'):
                if key in target:
                    intent[key] = target[key]
            return await self.smart_controller.execute_intent(intent, self.ha_client)

    async def _opening_unplanned(self, target: Dict, action: Any) -> bool:
        """Whether this step opens a way in (an unlock, or opening an access
        cover) without the planning-time decision of which door.

        A step carrying ``decided_access`` and no model entity id is the
        planned one. A step that still carries an entity id never runs,
        except a cover that is plainly not a way in (blinds). A lock step
        with neither never runs. A cover step with neither goes to the cover
        handler, which opens a plain cover by room and never an access cover
        without a decision."""
        entity_id = target.get('entity_id')
        domain = access_target.opening_domain(entity_id, target.get('device_type'))
        if domain is None or not access_target.is_opening_action(domain, action):
            return False
        if entity_id:
            if domain == 'cover' and isinstance(entity_id, str):
                try:
                    known = (await self.smart_controller.entity_manager.get_entities()).get(entity_id)
                except Exception:
                    known = None
                if known is not None and not access_target.is_access_cover(entity_id, known.get('attributes')):
                    return False
            logger.warning("sequence_opening_refused", extra={"reason": "entity_id_not_planned"})
            return True
        if target.get('decided_access'):
            return False
        if domain == 'cover':
            return False
        logger.warning("sequence_opening_refused", extra={"reason": "no_decision"})
        return True

    async def _execute_direct_action(self, entity_id: str, action: str, parameters: Dict):
        """Execute action directly on an entity."""
        domain = entity_id.split('.')[0]

        if action in ('set_effect', 'clear_effect'):
            # An effect is backed by the user's own words, which a step lacks; no
            # service is called and no fan-out block is recorded.
            return EFFECT_STEP_REFUSAL

        # Map common actions to HA services
        service_map = {
            'turn_on': 'turn_on',
            'turn_off': 'turn_off',
            'toggle': 'toggle',
            'set_color': 'turn_on',  # Color is set via turn_on with params
            'set_brightness': 'turn_on',  # Brightness is set via turn_on
            'set_temperature': 'set_temperature',
            'lock': 'lock',
            'unlock': 'unlock',
            'open': 'open_cover',
            'close': 'close_cover',
            'play': 'media_play',
            'pause': 'media_pause',
            'stop': 'media_stop',
        }

        service = service_map.get(action, action)
        service_data = {'entity_id': entity_id}

        # Add parameters
        if action == 'set_color' and _is_hue_saturation(parameters.get('hs_color')):
            service_data['hs_color'] = parameters['hs_color']
            service_data['brightness'] = parameters.get('brightness', 255)
        elif action == 'set_brightness' and 'brightness' in parameters:
            service_data['brightness'] = parameters['brightness']
        elif 'temperature' in parameters:
            service_data['temperature'] = parameters['temperature']

        # ATHENA-128: the fan-out gate reads the request's classification
        # from the scope, so a real state question (routing reverted by the
        # kill switch) never writes through a sequence step. Outside
        # pending_carrier, a block is always the rewording, recorded by the
        # step loop as refused_fanout -- never a dangling prompt.
        from orchestrator import write_fanout
        refusal = write_fanout.gate(domain, service, (entity_id,), None)
        if refusal:
            return refusal

        await self.ha_client.call_service(domain, service, service_data)
        return None

    def _calculate_wait_until(self, time_str: str) -> float:
        """
        Calculate seconds to wait until the specified time.

        Args:
            time_str: Time in HH:MM or HH:MM:SS format

        The target is a wall-clock time in the property zone (DEFAULT_TIMEZONE):
        its next occurrence after now, found by comparing UTC instants (see
        _next_local_occurrence), so the wait is real elapsed time across a
        DST change and is never negative.

        Returns:
            Seconds to wait (0 if the time can't be parsed)
        """
        now = local_now()

        # Parse time
        try:
            if ':' in time_str:
                parts = time_str.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                second = int(parts[2]) if len(parts) > 2 else 0
            else:
                # Handle "6pm" style
                time_str = time_str.lower().strip()
                is_pm = 'pm' in time_str or 'p.m.' in time_str
                is_am = 'am' in time_str or 'a.m.' in time_str
                time_str = time_str.replace('pm', '').replace('am', '').replace('p.m.', '').replace('a.m.', '').strip()
                hour = int(time_str)
                minute = 0
                second = 0
                if is_pm and hour < 12:
                    hour += 12
                elif is_am and hour == 12:
                    hour = 0
        except ValueError:
            logger.warning(f"Could not parse time: {time_str}, executing immediately")
            return 0

        tz = local_tz()
        wake_at = _next_local_occurrence(now, time(hour, minute, second), tz)
        wait_seconds = max(0.0, (wake_at - now.astimezone(timezone.utc)).total_seconds())
        wake_at = wake_at.astimezone(tz)
        logger.info(f"Calculated wait for {time_str}: {wait_seconds:.1f}s (until {wake_at})")

        return wait_seconds

    def cancel_sequence(self, session_id: str) -> bool:
        """Cancel a running sequence."""
        if session_id in self._running_sequences:
            task = self._running_sequences[session_id]
            task.cancel()
            del self._running_sequences[session_id]
            logger.info(f"Cancelled sequence for session {session_id}")
            return True
        return False

    def get_running_sequences(self) -> List[str]:
        """Get list of running sequence session IDs."""
        return list(self._running_sequences.keys())


def detect_sequence_intent(query: str) -> bool:
    """
    Detect if a query requires sequence execution.

    Args:
        query: User's natural language query

    Returns:
        True if the query involves delays, loops, or scheduling
    """
    query_lower = query.lower()

    # BRIGHTNESS EXCLUSION (Round 11): Simple brightness commands should NOT be sequences
    # "all lights at half" is NOT a schedule, it's an immediate brightness command
    brightness_exclusions = [
        'lights at half', 'light at half', 'lights to half',
        'lights at fifty', 'lights to fifty', 'lights at 50', 'lights to 50',
        'all lights at half', 'all lights to half',
        'at twenty percent', 'at thirty percent', 'at forty percent',
        'at fifty percent', 'at sixty percent', 'at seventy percent',
        'at eighty percent', 'at ninety percent', 'at hundred percent',
    ]
    if any(p in query_lower for p in brightness_exclusions):
        return False

    # Round 21-30: CASUAL "THEN" EXCLUSIONS
    # "then" as a filler word, not a sequencing word
    # e.g., "turn them on then genius" or "well then just do it"
    casual_then_patterns = [
        'then genius', 'then dummy', 'then idiot',  # Sarcastic commands with "then"
        'well then', 'ok then', 'okay then', 'alright then',  # Filler "then"
        'fine then', 'whatever then',  # Dismissive "then"
        'then please', 'then already',  # Impatient "then"
    ]
    if any(p in query_lower for p in casual_then_patterns):
        # Check if there's actual sequencing - needs TWO actions with delay
        action_words = ['turn', 'set', 'change', 'dim', 'bright']
        action_count = sum(1 for w in action_words if w in query_lower)
        if action_count < 2:
            return False  # Single action with casual "then" - not a sequence

    # Round 21-30: EMOTIONAL/CONVERSATIONAL EXCLUSIONS
    # "tomorrow" in emotional context, not scheduling context
    emotional_exclusions = [
        'tomorrow will be better', 'will be better tomorrow',
        'better tomorrow', 'tomorrow is another day',
        'will be better right',  # Reassurance seeking
        'tomorrow morning', 'see you tomorrow',  # Farewells, not schedules
    ]
    if any(p in query_lower for p in emotional_exclusions):
        # If no explicit scheduling action, it's not a sequence
        action_words = ['turn', 'set', 'schedule', 'start', 'run']
        if not any(w in query_lower for w in action_words):
            return False

    return has_sequence_timing(query_lower, require_action_for_bare_temporal=True)


# Prompt template for sequence generation
SEQUENCE_PROMPT_TEMPLATE = """You are a smart home assistant that can create action sequences.

The user wants to perform a sequence of smart home actions. Parse their request and generate a sequence of steps.

User request: "{query}"
Current room: {room}
Current time: {current_time}

Generate a JSON response with this structure:
{{
    "type": "sequence",
    "acknowledge": "Brief message to say immediately (e.g., 'Starting your sequence...')",
    "steps": [
        {{
            "action": "turn_on|turn_off|set_color|set_brightness|set_temperature|lock|unlock|play|pause",
            "target": {{
                "device_type": "light|switch|climate|lock|media_player|cover",
                "room": "room name"
            }},
            "parameters": {{
                "brightness": 0-255 or null,
                "hs_color": [hue 0-360, saturation 0-100] or null,
                "temperature": number or null,
                "color_description": "color name" or null
            }},
            "delay_after": seconds to wait after this action (0 for none),
            "at_time": "HH:MM" for scheduled time or null for immediate
        }}
    ]
}}

Rules:
1. Unroll loops into explicit steps (e.g., "4 times" becomes 4 steps)
2. For "on and off" patterns, alternate turn_on and turn_off actions
3. For "different colors", pick visually distinct colors (red=0, orange=30, yellow=60, green=120, cyan=180, blue=240, purple=280, pink=330)
4. Use delay_after for relative waits, at_time for scheduled actions
5. Keep acknowledge message brief (will be spoken aloud)
6. If no room specified, use the current room: {room}

Examples:
- "turn on and off 3 times" → 6 steps alternating turn_on/turn_off with 1s delays
- "at 6pm turn off the lights" → 1 step with at_time="18:00"
- "wait 5 seconds then turn on" → current action + 1 step with delay_after=5

Return ONLY valid JSON, no markdown or explanation."""
