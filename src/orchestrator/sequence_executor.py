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
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import json

logger = logging.getLogger(__name__)


class SequenceExecutor:
    """Executes sequences of smart home actions with delays and scheduling."""

    def __init__(self, smart_controller, ha_client):
        """
        Initialize the sequence executor.

        Args:
            smart_controller: SmartHomeController instance for executing individual actions
            ha_client: Home Assistant client for direct API calls
        """
        self.smart_controller = smart_controller
        self.ha_client = ha_client
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
            await self._execute_sequence_steps(sequence, session_id)
            return "Sequence complete."

    async def _execute_sequence_steps(
        self,
        sequence: List[Dict],
        session_id: Optional[str] = None
    ):
        """Execute sequence steps with delays and scheduling."""
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

                # Execute the action
                await self._execute_step(step, step_num, total_steps)

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

    async def _execute_step(self, step: Dict, step_num: int, total_steps: int):
        """Execute a single sequence step."""
        action = step.get('action', 'turn_on')
        target = step.get('target', {})
        parameters = step.get('parameters', {})

        # Extract target info
        device_type = target.get('device_type', 'light')
        room = target.get('room')
        entity_id = target.get('entity_id')

        logger.info(f"Executing step {step_num}/{total_steps}: {action} on {device_type} in {room or entity_id}")

        if entity_id:
            # Direct entity control
            await self._execute_direct_action(entity_id, action, parameters)
        else:
            # Use smart controller for room-based control
            intent = {
                'device_type': device_type,
                'room': room,
                'action': action,
                'target_scope': target.get('target_scope', 'group'),
                'parameters': parameters,
                'color_description': parameters.get('color_description')
            }
            await self.smart_controller.execute_intent(intent, self.ha_client)

    async def _execute_direct_action(self, entity_id: str, action: str, parameters: Dict):
        """Execute action directly on an entity."""
        domain = entity_id.split('.')[0]

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
        if action == 'set_color' and 'hs_color' in parameters:
            service_data['hs_color'] = parameters['hs_color']
            service_data['brightness'] = parameters.get('brightness', 255)
        elif action == 'set_brightness' and 'brightness' in parameters:
            service_data['brightness'] = parameters['brightness']
        elif 'temperature' in parameters:
            service_data['temperature'] = parameters['temperature']

        await self.ha_client.call_service(domain, service, service_data)

    def _calculate_wait_until(self, time_str: str) -> float:
        """
        Calculate seconds to wait until the specified time.

        Args:
            time_str: Time in HH:MM or HH:MM:SS format

        Returns:
            Seconds to wait (0 if time has passed today, schedules for tomorrow)
        """
        now = datetime.now()

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

        # Create target datetime
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)

        # If time has passed today, schedule for tomorrow
        if target <= now:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"Calculated wait for {time_str}: {wait_seconds:.1f}s (until {target})")

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

    # Delay/timing patterns
    delay_patterns = [
        'wait', 'then', 'after', 'seconds', 'second',
        'minutes', 'minute', 'pause', 'delay'
    ]

    # Loop patterns
    loop_patterns = [
        'times', 'repeat', 'cycle', 'loop', 'again',
        'on and off', 'off and on', 'flash', 'blink'
    ]

    # Scheduling patterns
    schedule_patterns = [
        'at ', 'in ', ' pm', ' am', 'o\'clock', 'oclock',
        'tonight', 'tomorrow', 'morning', 'evening', 'noon',
        'midnight', 'later', 'schedule'
    ]

    has_delay = any(p in query_lower for p in delay_patterns)
    has_loop = any(p in query_lower for p in loop_patterns)
    has_schedule = any(p in query_lower for p in schedule_patterns)

    return has_delay or has_loop or has_schedule


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
