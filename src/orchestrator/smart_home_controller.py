"""
Smart Home Controller with LLM-based intent extraction
Handles complex commands like "make the living room lights all different colors"
"""
import asyncio
import json
import random
from typing import Dict, List, Optional, Tuple
from .ha_entity_manager import HAEntityManager
from shared.admin_config import get_admin_client


# Round 17: Response variety templates for natural conversation
THERMOSTAT_SET_RESPONSES = [
    "Done! Thermostat set to {temp}°F.",
    "Got it, {temp} degrees.",
    "Set to {temp}°F.",
    "Done! {temp} degrees it is.",
    "Thermostat adjusted to {temp}°F.",
]

THERMOSTAT_SET_RANGE_RESPONSES = [
    "Done! Set to {temp}°F (range {low} to {high}).",
    "Got it, {temp} degrees. Range is {low} to {high}.",
    "Thermostat set to {temp}°F.",
    "Done! {temp} degrees.",
]

THERMOSTAT_UP_RESPONSES = [
    "Turned up the heat. Now set to {low} to {high} degrees.",
    "Done! Heat's up, range is {low} to {high}°F.",
    "Warmed it up to {low}-{high} degrees.",
    "Got it, bumped it up to {low}-{high}°F.",
]

THERMOSTAT_DOWN_RESPONSES = [
    "Cooled it down. Now set to {low} to {high} degrees.",
    "Done! Temperature lowered to {low}-{high}°F.",
    "Brought it down to {low}-{high} degrees.",
    "Got it, dropped it to {low}-{high}°F.",
]

LIGHT_ON_RESPONSES = [
    "Done! {lights} on.",
    "{lights} turned on.",
    "Got it, {lights} on.",
    "Done!",
    "Turned on {lights}.",
]

LIGHT_OFF_RESPONSES = [
    "Done! {lights} off.",
    "{lights} turned off.",
    "Got it, {lights} off.",
    "Done!",
    "Turned off {lights}.",
]


def vary_response(templates: list, **kwargs) -> str:
    """Pick a random response template and format it with provided kwargs."""
    template = random.choice(templates)
    try:
        return template.format(**kwargs)
    except KeyError:
        return templates[0].format(**kwargs)


class SmartHomeController:
    def __init__(self, entity_manager: HAEntityManager, llm_router):
        self.entity_manager = entity_manager
        self.llm_router = llm_router
    
    async def extract_intent(self, query: str, light_count: int = 3, device_room: str = None,
                              prev_query: str = None, prev_response: str = None,
                              prev_intent_entities: Dict = None) -> Dict:
        """Use LLM to extract structured intent from natural language query

        Args:
            query: The user's natural language query
            light_count: Number of lights to control for color distribution
            device_room: Room where the voice device is located (used as fallback when no room in query)
            prev_query: Previous user query (for context in follow-ups)
            prev_response: Previous assistant response (for context in corrections like "no, just my side")
            prev_intent_entities: Previous intent entities (device_type, room, action, parameters) for context
        """
        import logging
        logger = logging.getLogger(__name__)

        query_lower = query.lower()

        # Round 17: Typo correction for common misspellings
        # This enables recognition of "turn of the lihgts" → "turn off the lights"
        typo_corrections = {
            # Light misspellings
            'lihgts': 'lights', 'lighst': 'lights', 'ligths': 'lights', 'litghs': 'lights',
            'lghts': 'lights', 'lihgt': 'light', 'ligth': 'light', 'ligt': 'light',
            'lite': 'light', 'lites': 'lights',  # Alternate spellings
            # On/off misspellings
            'offf': 'off', 'onn': 'on', 'oon': 'on', 'oof': 'off',
            # "turn of" → "turn off" (very common typo)
            'turn of ': 'turn off ', 'turn fo ': 'turn off ',
            'trun ': 'turn ', 'tunr ': 'turn ', 'tur ': 'turn ',
            # Switch misspellings
            'swtich': 'switch', 'swich': 'switch', 'swtch': 'switch',
            # Thermostat misspellings
            'theromstat': 'thermostat', 'thermstat': 'thermostat', 'thermastat': 'thermostat',
            'temprature': 'temperature', 'tempature': 'temperature', 'temperture': 'temperature',
            # Other common device misspellings
            'dorr': 'door', 'dor': 'door', 'dooor': 'door',
            'locl': 'lock', 'lokc': 'lock',
        }
        for typo, correction in typo_corrections.items():
            if typo in query_lower:
                query_lower = query_lower.replace(typo, correction)
                logger.info(f"Typo corrected: '{typo}' → '{correction}' in query")

        # FAST PATH: Simple turn on/off commands without color
        # Skip LLM for basic commands like "turn on the lights" to reduce latency
        is_turn_on = any(p in query_lower for p in ['turn on', 'switch on', 'lights on'])
        is_turn_off = any(p in query_lower for p in ['turn off', 'switch off', 'lights off',
                                                     'kill the lights', 'kill all the lights', 'kill all lights',  # Round 12
                                                     'cut the lights', 'cut all the lights', 'lights out'])  # Round 12

        # Round 16: "lit" is slang for turn on/bright - "get the kitchen lit", "lemme get it lit"
        if 'lit' in query_lower and not is_turn_off:
            is_turn_on = True
        has_color = any(c in query_lower for c in [
            'red', 'blue', 'green', 'white', 'yellow', 'orange', 'purple', 'pink',
            'cyan', 'magenta', 'warm', 'cool', 'rainbow', 'sunset', 'sunrise', 'random', 'christmas',
            'color', 'colors', 'ravens', 'orioles', 'steelers', 'team'
        ])

        # FAST PATH: Lock commands
        lock_command_patterns = [
            'lock the front door', 'lock front door', 'lock the door',
            'unlock the front door', 'unlock front door', 'unlock the door',
            'lock the back door', 'lock back door',
            'unlock the back door', 'unlock back door',
            'lock all doors', 'lock the doors', 'unlock all doors', 'unlock the doors',
            'lock all the doors', 'unlock all the doors',  # Added "all the doors" variations
            'lock up', 'lock everything', 'lock it up', 'lock up the house',  # Round 11: casual lock phrases
            'is the door locked', 'is the front door locked', 'is the back door locked',
            'is the door unlocked', 'is the front door unlocked', 'is the back door unlocked',
            'check the lock', 'check the door lock', 'door status',
            'are the doors locked', 'are all doors locked',  # Added status check variations
            'are the doors unlocked', 'are all doors unlocked',  # Added unlocked status variations
            'are my doors locked', 'are all my doors locked',  # Round 17: "my doors" variants
            'are my doors unlocked', 'are all my doors unlocked',
            'did i lock', 'have i locked', 'did i already lock',  # Round 12: past tense status checks
            'check if everything is locked', 'check if its locked', 'check if it is locked',  # Round 13
            'everything locked', 'is everything locked',
            "the deal with", "deal with the front door", "deal with the back door",  # Round 13
            "whats up with", "what's up with", "status of the door",  # Round 13
            "how's the door", "hows the door", "door open", "is the door open",  # Round 13
            "front door status", "back door status",  # Round 13
            "door good", "door okay", "door ok",  # Round 15: "is the back door good"
            "lock it down", "lock down", "lock down for the night",  # Round 16: "lock it down for the night"
            # Round 17: more lock status patterns
            "status on the locks", "status on all the locks", "status of the locks",
            "check the locks", "check all the locks", "check all locks",
            "all the locks", "locks in the house", "all locks",
            "any doors unlocked", "doors unlocked", "left any doors",
            "check if i left", "left doors unlocked", "left the door"
        ]
        is_lock_command = any(p in query_lower for p in lock_command_patterns)

        if is_lock_command:
            # Determine action and room
            # Check for status query FIRST (before unlock action) - "is X locked/unlocked" is a query, not a command
            if 'is' in query_lower and ('locked' in query_lower or 'unlocked' in query_lower or 'status' in query_lower or 'check' in query_lower):
                action = 'get_status'
            elif 'are' in query_lower and ('locked' in query_lower or 'unlocked' in query_lower):
                action = 'get_status'
            # Round 12: "did i lock" / "have i locked" = status check, not lock command
            elif 'did i lock' in query_lower or 'have i locked' in query_lower or 'did i already lock' in query_lower:
                action = 'get_status'
            # Round 13: "check if everything is locked", "everything locked?"
            elif 'check if' in query_lower and 'locked' in query_lower:
                action = 'get_status'
            elif 'everything locked' in query_lower:
                action = 'get_status'
            # Round 13: "whats the deal", "whats up", "hows the door", "door open", "door status"
            elif 'the deal with' in query_lower:
                action = 'get_status'
            elif 'whats up' in query_lower or "what's up" in query_lower:
                action = 'get_status'
            elif "how's the" in query_lower or 'hows the' in query_lower:
                action = 'get_status'
            elif 'door open' in query_lower or 'door status' in query_lower:
                action = 'get_status'
            # Round 15: "is the back door good", "door okay"
            elif 'door good' in query_lower or 'door okay' in query_lower or 'door ok' in query_lower:
                action = 'get_status'
            # Round 17: "status on the locks", "check the locks", "any doors unlocked"
            elif 'status on' in query_lower or 'status of' in query_lower:
                action = 'get_status'
            elif 'check the locks' in query_lower or 'check all' in query_lower:
                action = 'get_status'
            elif 'any doors unlocked' in query_lower or 'doors unlocked' in query_lower:
                action = 'get_status'
            elif 'left any doors' in query_lower or 'left doors' in query_lower or 'left the door' in query_lower:
                action = 'get_status'
            elif 'all the locks' in query_lower or 'all locks' in query_lower or 'locks in the house' in query_lower:
                action = 'get_status'
            elif 'unlock' in query_lower:
                action = 'unlock'
            else:
                action = 'lock'

            # Determine which door
            if 'back' in query_lower:
                room = 'back_door'
            elif 'front' in query_lower:
                room = 'front_door'
            elif 'all' in query_lower or 'the doors' in query_lower or 'my doors' in query_lower or 'the locks' in query_lower or 'in the house' in query_lower:
                # "lock all doors", "lock all the doors", "lock the doors", "my doors", "all the locks", "locks in the house" -> all doors
                room = 'all_doors'
            else:
                room = 'front_door'  # Default to front door

            logger.info(f"Fast path lock command: action={action}, room={room}")
            return {
                "device_type": "lock",
                "room": room,
                "action": action,
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Occupancy/Presence queries (Round 15)
        # "is there anybody in the basement", "anyone home", "is somebody in the kitchen"
        occupancy_patterns = [
            'anybody in', 'anyone in', 'someone in', 'somebody in',
            'is there anybody', 'is there anyone', 'is there someone',
            'anybody home', 'anyone home', 'someone home', 'somebody home',
            'is anybody', 'is anyone', 'who is home', "who's home", 'whos home',
            'people in the', 'occupied', 'occupancy'
        ]
        if any(p in query_lower for p in occupancy_patterns):
            logger.info(f"Fast path occupancy query")
            return {
                "device_type": "sensor",
                "room": None,  # Will check all motion sensors
                "action": "get_status",
                "target_scope": "group",
                "parameters": {"sensor_type": "occupancy", "original_query": query},
                "color_description": None
            }

        # FAST PATH: Window/Door Sensor Status (Round 13)
        # "are any windows open", "check the windows", "window status"
        window_status_patterns = [
            'window open', 'windows open', 'are any windows', 'any windows open',
            'check the windows', 'check windows', 'window status', 'windows status',
            'are the windows', 'windows closed', 'window closed'
        ]
        if any(p in query_lower for p in window_status_patterns):
            logger.info(f"Fast path window sensor query")
            return {
                "device_type": "sensor",
                "room": None,  # Check all windows
                "action": "get_status",
                "target_scope": "group",
                "parameters": {"sensor_type": "window"},
                "color_description": None
            }

        # FAST PATH: TV/Media Player commands
        # Detect "turn off the TV", "turn on the TV", etc.
        tv_patterns = ['tv', 'television', 'media player', 'shield']
        is_tv_command = any(p in query_lower for p in tv_patterns)
        if is_tv_command and (is_turn_on or is_turn_off):
            action = 'turn_on' if is_turn_on else 'turn_off'
            logger.info(f"Fast path TV command: action={action}")
            return {
                "device_type": "media_player",
                "room": "living_room",  # Default TV location
                "action": action,
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Fan commands
        # Detect "turn on the ceiling fan", "turn off the fan"
        fan_patterns = ['ceiling fan', 'the fan', 'a fan', 'fans',
                       'air moving', 'air circulation', 'air flow', 'some air']  # Round 16: "get some air moving"
        is_fan_command = any(p in query_lower for p in fan_patterns)
        # Round 16: "get some air moving" implies turn on
        is_air_request = any(p in query_lower for p in ['air moving', 'air circulation', 'some air'])
        if is_fan_command and (is_turn_on or is_turn_off or is_air_request):
            # "get some air moving" = turn on, "turn off fan" = turn off
            action = 'turn_on' if (is_turn_on or is_air_request) else 'turn_off'
            # Extract room if mentioned
            room = None
            if 'living' in query_lower:
                room = 'living_room'
            elif 'bedroom' in query_lower or 'master' in query_lower:
                room = 'master_bedroom'
            elif 'office' in query_lower:
                room = 'office'
            logger.info(f"Fast path fan command: action={action}, room={room}")
            return {
                "device_type": "fan",
                "room": room,
                "action": action,
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Garage door / Cover commands
        # Detect "open the garage", "close the garage", "garage door"
        garage_patterns = ['garage', 'garage door']
        is_garage_command = any(p in query_lower for p in garage_patterns)
        is_open = any(p in query_lower for p in ['open', 'opening'])
        is_close = any(p in query_lower for p in ['close', 'closing', 'shut'])
        is_status = any(p in query_lower for p in ['is the garage', 'garage status', 'is the door'])
        if is_garage_command:
            if is_open:
                action = 'open'
            elif is_close:
                action = 'close'
            elif is_status:
                action = 'get_status'
            else:
                action = 'get_status'  # Default to status check
            logger.info(f"Fast path garage command: action={action}")
            return {
                "device_type": "cover",
                "room": "garage",
                "action": action,
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Scene and routine commands
        # These map to HA scenes or scripts
        scene_patterns = {
            # Movie mode
            'movie mode': 'scene.movie_mode',
            'movie time': 'scene.movie_mode',
            'watch a movie': 'scene.movie_mode',
            'start movie': 'scene.movie_mode',
            # Bedtime / Good night
            'good night': 'script.good_night',
            'goodnight': 'script.good_night',
            'bedtime': 'script.good_night',
            'time for bed': 'script.good_night',
            'going to bed': 'script.good_night',
            'night mode': 'script.good_night',
            # Morning
            'good morning': 'script.good_morning',
            'morning mode': 'script.good_morning',
            'wake up': 'script.good_morning',
            # Leaving
            'i am leaving': 'script.leaving',
            "i'm leaving": 'script.leaving',
            'im leaving': 'script.leaving',
            'goodbye': 'script.leaving',
            'leaving home': 'script.leaving',
            'heading out': 'script.leaving',
            # Arriving
            'i am home': 'script.arriving',
            "i'm home": 'script.arriving',
            'im home': 'script.arriving',
            "i'm back": 'script.arriving',
            'im back': 'script.arriving',
            'home now': 'script.arriving',
            # Romantic / Mood
            'romantic mode': 'scene.romantic',
            'date night': 'scene.romantic',
            'set the mood': 'scene.romantic',  # Round 15
            'make it romantic': 'scene.romantic',  # Round 14
            # Round 17: more romantic patterns
            'vibes for my girl': 'scene.romantic',
            'my girl comes over': 'scene.romantic',
            'girlfriend coming': 'scene.romantic',
            'romantic vibes': 'scene.romantic',
            # Relaxation
            'relax mode': 'scene.relax',
            'chill mode': 'scene.relax',
            # Party
            'party mode': 'scene.party',
            'party time': 'scene.party',
            'party vibes': 'scene.party',  # Round 16: "gimme the party vibes"
            'party vibe': 'scene.party',  # Round 16
        }
        for pattern, scene_entity in scene_patterns.items():
            if pattern in query_lower:
                logger.info(f"Fast path scene: pattern='{pattern}', entity={scene_entity}")
                return {
                    "device_type": "scene",
                    "room": None,
                    "action": "activate",
                    "target_scope": "group",
                    "parameters": {"entity_id": scene_entity},
                    "color_description": None
                }

        # FAST PATH: Whole house light commands (all lights on/off, turn everything off)
        # SKIP if there's an exclusion pattern (e.g., "except bedroom") - let exclusion logic handle it
        # Check for various exclusion patterns including "everything but" and "all but"
        import re
        has_exclusion_keywords = any(p in query_lower for p in ['except', 'but not', 'not the', 'excluding'])
        # Also check for "everything but X" or "all lights but X" patterns
        if not has_exclusion_keywords:
            has_exclusion_keywords = bool(re.search(r'(everything|all\s+(the\s+)?lights?)\s+but\s+', query_lower))

        whole_house_light_patterns = [
            ('all lights on', 'turn_on'),
            ('all the lights on', 'turn_on'),
            ('turn on all lights', 'turn_on'),
            ('turn on all the lights', 'turn_on'),
            ('all lights off', 'turn_off'),
            ('all the lights off', 'turn_off'),
            ('turn off all lights', 'turn_off'),
            ('turn off all the lights', 'turn_off'),
            ('turn everything off', 'turn_off'),
            ('turn everything on', 'turn_on'),
            ('everything off', 'turn_off'),
            ('lights off everywhere', 'turn_off'),
            ('lights on everywhere', 'turn_on'),
        ]
        # Only use whole_house fast path if NO exclusion keywords present
        if not has_exclusion_keywords:
            for pattern, action in whole_house_light_patterns:
                if pattern in query_lower:
                    logger.info(f"Fast path whole house lights: pattern='{pattern}', action={action}")
                    return {
                        "device_type": "light",
                        "room": "whole_house",
                        "action": action,
                        "target_scope": "group",
                        "parameters": {},
                        "color_description": None
                    }

        # FAST PATH: Bed warming commands
        # Note: All patterns must be specific to bed/mattress to avoid capturing thermostat commands
        bed_warming_patterns = [
            'warm up the bed', 'warm the bed', 'preheat the bed', 'heat the bed',
            'warm up my bed', 'warm my bed', 'preheat my bed', 'heat my bed',
            'warm the mattress', 'heat the mattress', 'mattress pad',
            'warm my side', 'warm the left', 'warm the right', 'warm left side', 'warm right side',
            'heat my side', 'heat the left', 'heat the right',
            'make the bed warm', 'make my bed warm', 'bed warmer',
            'warmer bed', 'hotter bed', 'turn up the bed',
            'turn on the bed', 'turn off the bed', 'bed on', 'bed off',
            'set the bed to', 'set bed to', 'bed to level', 'bed at level',
            'make the bed warmer', 'make my bed warmer', 'heat up the bed',
            'warm up my side', 'heat up my side'  # Round 14 - "warm up my side of the bed"
        ]
        # REMOVED 'make it warmer' - this should go to thermostat, not bed
        # Bed warming requires explicit "bed" or "mattress" or "my side" reference
        is_bed_warming = any(p in query_lower for p in bed_warming_patterns)

        # Additional safety check: exclude "bedroom" from matching "bed"
        # "bedroom" should NOT trigger bed warming - only standalone "bed" should
        import re
        has_bed_keyword = bool(re.search(r'\bbed\b(?!room)', query_lower))  # Match "bed" but not "bedroom"
        has_mattress_keyword = 'mattress' in query_lower
        has_side_keyword = any(kw in query_lower for kw in ['my side', 'left side', 'right side'])

        if is_bed_warming and not (has_bed_keyword or has_mattress_keyword or has_side_keyword):
            is_bed_warming = False

        if is_bed_warming:
            # FIRST: Check if this is a STATUS QUERY, not a command
            status_query_patterns = [
                'is the bed', 'is my bed', 'is bed',
                'is it on', 'is it off', 'is it running',
                'what is the bed', "what's the bed", 'what level',
                'how warm', 'how hot', 'what temp', 'what setting',
                'check the bed', 'check bed', 'bed status',
                '?'  # Any question about bed warmer
            ]
            # If it's a question (contains ? or status query pattern), return get_status
            is_status_query = '?' in query_lower or any(p in query_lower for p in status_query_patterns)

            if is_status_query:
                logger.info(f"Bed warmer STATUS QUERY detected: '{query[:50]}...'")
                return {
                    "device_type": "bed_warmer",
                    "room": "master_bedroom",
                    "action": "get_status",
                    "target_scope": "group",
                    "parameters": {},
                    "color_description": None
                }

            # Determine action and side for COMMANDS
            action = "warm_bed"
            side = "both"  # default
            level = 1  # default to low (level 1) for warming
            left_level = None  # For dual-side commands
            right_level = None

            if any(x in query_lower for x in ['turn off', 'off', 'stop']):
                action = "turn_off"
            elif any(x in query_lower for x in ['warmer', 'hotter', 'turn up', 'increase', 'higher']):
                action = "increase"
            elif any(x in query_lower for x in ['cooler', 'less', 'turn down', 'decrease', 'lower']):
                action = "decrease"

            # Helper to convert percentage to level (1-10)
            def percent_to_level(pct):
                return max(1, min(10, round(pct / 10)))

            # Check for percentage mentions (e.g., "50%", "at 70 percent")
            import re
            pct_match = re.search(r'(\d+)\s*(?:%|percent)', query_lower)
            if pct_match:
                level = percent_to_level(int(pct_match.group(1)))

            # Check for dual-side level commands like "left to 3 and right to 5"
            # or "right side at 50% and left side at 30%"
            dual_pattern = re.search(
                r'(?:left|my side).*?(?:to|at)\s*(\d+)\s*(%|percent)?.*?(?:right|other side).*?(?:to|at)\s*(\d+)\s*(%|percent)?',
                query_lower
            )
            if not dual_pattern:
                # Try reverse order: right first, then left
                dual_pattern = re.search(
                    r'(?:right|other side).*?(?:to|at)\s*(\d+)\s*(%|percent)?.*?(?:left|my side).*?(?:to|at)\s*(\d+)\s*(%|percent)?',
                    query_lower
                )
                if dual_pattern:
                    # Swap: first match is right, second is left
                    right_val = int(dual_pattern.group(1))
                    right_level = percent_to_level(right_val) if dual_pattern.group(2) else min(10, max(1, right_val))
                    left_val = int(dual_pattern.group(3))
                    left_level = percent_to_level(left_val) if dual_pattern.group(4) else min(10, max(1, left_val))
                    side = "dual"
            else:
                left_val = int(dual_pattern.group(1))
                left_level = percent_to_level(left_val) if dual_pattern.group(2) else min(10, max(1, left_val))
                right_val = int(dual_pattern.group(3))
                right_level = percent_to_level(right_val) if dual_pattern.group(4) else min(10, max(1, right_val))
                side = "dual"

            # Single side detection (if not dual)
            if side != "dual":
                if any(x in query_lower for x in ['left', 'my side', 'side 1', 'side a']):
                    side = "left"
                elif any(x in query_lower for x in ['right', 'other side', 'side 2', 'side b']):
                    side = "right"

            # Check for specific level mentions (if not already set by percentage or dual)
            if not pct_match and side != "dual":
                for i in range(1, 11):
                    if f'level {i}' in query_lower or f'setting {i}' in query_lower:
                        level = i
                        break
                    # Match standalone numbers like "to 5" or "at 7"
                    if re.search(rf'(?:to|at)\s+{i}(?:\s|$|,)', query_lower):
                        level = i
                        break

            # Build parameters
            params = {"side": side, "level": level}
            if side == "dual":
                params["left_level"] = left_level
                params["right_level"] = right_level

            logger.info(f"Fast path bed warming: action={action}, side={side}, level={level}, left={left_level}, right={right_level}")
            return {
                "device_type": "bed_warmer",
                "room": "master_bedroom",
                "action": action,
                "target_scope": "group",
                "parameters": params,
                "color_description": None
            }

        # MOTION CONTROL: Detect motion/lighting automation override requests
        # These control Node-RED motion flow variables for bedrooms/office
        motion_control_patterns = [
            # Leave lights on patterns
            'leave the lights on', 'keep the lights on', 'lights stay on',
            'don\'t turn off the lights', 'stop turning off the lights',
            'leave lights on', 'keep lights on',
            # Leave lights off patterns
            'leave the lights off', 'keep the lights off', 'lights stay off',
            'don\'t turn on the lights', 'stop turning on the lights',
            'leave lights off', 'keep lights off',
            'taking a nap', 'gonna nap', 'going to nap', 'nap time',
            # Disable motion patterns
            'disable motion', 'turn off motion', 'no motion', 'motion off',
            'disable the motion', 'stop motion detection', 'pause motion',
            'disable motion detection', 'turn off motion detection',
            # Enable/resume motion patterns
            'enable motion', 'turn on motion', 'motion on', 'resume motion',
            'enable the motion', 'start motion detection', 'resume normal',
            'turn motion back on', 'motion detection on', 'normal motion',
            'reset motion', 'reset the motion',
            # Brightness override patterns
            'keep it at this brightness', 'lock the brightness', 'stay at this brightness',
            'keep this brightness', 'maintain this brightness', 'brightness override',
            'lock brightness', 'set motion brightness', 'override brightness',
            'keep the brightness', 'stay this bright', 'don\'t change brightness'
        ]
        is_motion_control = any(p in query_lower for p in motion_control_patterns)

        if is_motion_control:
            # Route to LLM for full extraction since guests may phrase things many ways
            return await self._extract_motion_control_intent(query, device_room)

        # FAST PATH: Implicit brightness requests (without explicit "turn on/off")
        # e.g., "I can't see", "too dark", "make it brighter", "dimmer"
        implicit_brighter = [
            "can't see", "cant see", "cannot see", "too dark", "hard to see",
            "brighter", "more light", "brighten", "bump up", "bump it up",
            "brightness higher", "set brightness higher", "raise the lights",
            "lights higher", "higher brightness", "getting dark", "need some light",
            "need light", "need more light", "give me some light", "some light over here",
            "some light please", "bring up the lights", "bring the lights up",
            # Round 13: "kinda dim" / "looking dim" means it's TOO DARK, need more light
            "kinda dim", "looking dim", "its dim", "it's dim", "bit dim", "pretty dim",
            # Round 13: "bring them back up", "back up" = restore/increase brightness
            "bring them back up", "bring it back up", "back up", "bring em back up",
            # Round 14: slang brightness
            "lights weak", "weak af", "not bright enough", "need it brighter",
            # Round 16: "super bright", "make it super bright"
            "super bright", "really bright", "extra bright", "crazy bright"
        ]
        implicit_dimmer = [
            "too bright", "dimmer", "less light", "darker", "softer light",
            "make it cozy", "lower the lights", "lower lights", "way down",
            "turn it down", "brightness lower", "set brightness lower",
            "bring the lights down", "bring down the lights",  # Round 12
            "lights lower", "lower brightness", "too much light", "thats too much",
            "not so bright", "a bit less", "little less light", "darken it up",
            "tone down", "tone it down", "take it easy on my eyes", "easy on my eyes",
            # Round 17: fade patterns
            "fade the lights", "fade lights", "fade down", "fade it down",
            "fade them down", "fade out", "fading"
        ]

        # Round 16: FAST PATH: Light status queries (check BEFORE turn off!)
        # "any lights left on", "which lights are on" = status query, NOT control
        light_status_patterns = [
            'any lights left on', 'any lights on', 'lights left on',
            'which lights are on', 'what lights are on', 'lights still on',
            'lights on upstairs', 'lights on downstairs',
            'are any lights on', 'are the lights on', 'anything left on'
        ]
        if any(p in query_lower for p in light_status_patterns):
            # This is a status query, route to get_status action
            room = None
            if 'upstairs' in query_lower:
                room = 'upstairs'
            elif 'downstairs' in query_lower:
                room = 'downstairs'
            elif 'basement' in query_lower:
                room = 'basement'
            logger.info(f"Fast path light STATUS query: room={room}")
            return {
                "device_type": "light",
                "room": room,
                "action": "get_status",
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Alternative turn off patterns (lights out, kill the lights, cut the lights, no more lights)
        turn_off_phrases = ['lights out', 'kill the light', 'kill the lights', 'cut the lights',
                            'cut the light', 'no more lights', 'no more light', 'shut it off',
                            'shut off the lights', 'shut them off', 'forget the lights', 'forget the light',
                            'off with the lights', 'off with the light',  # Round 12
                            'every light off', 'every light out',  # Round 12
                            'kill all the lights', 'kill all lights', 'kill all',  # Round 12 exclusion support
                            'everything off', 'all off', 'yo everything off']  # Round 14
        # Round 12: Check for exclusion keywords - don't use fast path if exclusion present
        has_exclusion_in_query = any(p in query_lower for p in ['except', 'but not', 'not the', 'excluding', ' but '])
        if any(p in query_lower for p in turn_off_phrases) and not has_exclusion_in_query:
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break
            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)
            logger.info(f"Fast path 'lights out' or 'kill lights': room={final_room}")
            return {
                "device_type": "light",
                "room": final_room,
                "action": "turn_off",
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Simple turn on patterns (hit the lights, lights please)
        turn_on_phrases = ['hit the lights', 'hit the light', 'lights please', 'light please',
                          'just a little light', 'a little light', 'light me up', 'throw on some lights',
                          'throw on the lights', 'flip the lights', 'flip on the lights',
                          'flip em on', 'flip them on',  # Round 12
                          'throw the lights on', 'get the lights', 'lights on low',  # Round 13
                          'light going', 'lights going', 'get the light',  # Round 13: "get the porch light going"
                          'flip the light', 'flip the',  # Round 15: "flip the porch light"
                          'get it lit', 'get the', 'lemme get']  # Round 16: "yo lemme get the kitchen lit"
        if any(p in query_lower for p in turn_on_phrases):
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break
            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)
            # For "just a little light" or "on low" set low brightness
            params = {}
            if 'little light' in query_lower or 'a little light' in query_lower or 'on low' in query_lower:
                params = {"brightness": 30}  # Low brightness for "just a little light" or "on low"
            logger.info(f"Fast path turn ON phrase: room={final_room} params={params}")
            return {
                "device_type": "light",
                "room": final_room,
                "action": "turn_on",
                "target_scope": "group",
                "parameters": params,
                "color_description": None
            }

        # Round 12 FIX: Check for thermostat keywords BEFORE brightness patterns
        # "bump up the heat" should NOT match brightness "bump up"
        thermostat_exclusions = [
            'heat', 'cold', 'temperature', 'thermostat', 'hvac', 'ac ',
            ' ac', 'air conditioning', 'degrees', 'warming', 'cooling',
            'its cold', "it's cold", 'its hot', "it's hot", 'chilly', 'freezing'
        ]
        is_thermostat_context = any(t in query_lower for t in thermostat_exclusions)

        if any(p in query_lower for p in implicit_brighter) and not is_thermostat_context:
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break
            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)
            logger.info(f"Fast path implicit brightness INCREASE: room={final_room} for query='{query[:50]}...'")
            return {
                "device_type": "light",
                "room": final_room,
                "action": "increase",  # Use "increase" to match execute_intent handler
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }
        elif any(p in query_lower for p in implicit_dimmer):
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break
            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)
            logger.info(f"Fast path implicit brightness DECREASE: room={final_room} for query='{query[:50]}...'")
            return {
                "device_type": "light",
                "room": final_room,
                "action": "decrease",  # Use "decrease" to match execute_intent handler
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        if (is_turn_on or is_turn_off) and not has_color:
            # Extract room from query using pattern matching
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]

            # MULTI-ROOM DETECTION: Check for "room1 and room2" patterns
            # e.g., "turn on kitchen and living room lights"
            import re
            multi_room_matches = []
            for room in room_names:
                if room in query_lower:
                    multi_room_matches.append(room)

            # Check if multiple rooms are joined by "and"
            if len(multi_room_matches) >= 2 and ' and ' in query_lower:
                # Verify rooms are actually connected by "and" (not separate phrases)
                # Build a pattern to check: room1...and...room2
                connected = False
                for i, room1 in enumerate(multi_room_matches):
                    for room2 in multi_room_matches[i+1:]:
                        # Check if "and" appears between the two rooms
                        pattern1 = f"{re.escape(room1)}.*\\s+and\\s+.*{re.escape(room2)}"
                        pattern2 = f"{re.escape(room2)}.*\\s+and\\s+.*{re.escape(room1)}"
                        if re.search(pattern1, query_lower) or re.search(pattern2, query_lower):
                            connected = True
                            break
                    if connected:
                        break

                if connected:
                    action = "turn_on" if is_turn_on else "turn_off"
                    # Return multi-room intent with list of rooms
                    logger.info(f"Fast path MULTI-ROOM: action={action}, rooms={multi_room_matches}")
                    return {
                        "device_type": "light",
                        "room": "multi_room",  # Special marker
                        "rooms": multi_room_matches,  # List of rooms to control
                        "action": action,
                        "target_scope": "group",
                        "parameters": {},
                        "color_description": None
                    }

            # SPECIAL CASE: "leave X on but turn off the rest" pattern
            # This is an inverted exclusion where the excluded room is mentioned FIRST
            leave_on_pattern = re.search(r'leave\s+(the\s+)?(\w+(?:\s+\w+)?)\s+(?:light(?:s)?|on)\s+(?:on\s+)?(?:but|and)\s+turn\s+off', query_lower)
            if leave_on_pattern:
                excluded_room_candidate = leave_on_pattern.group(2).strip()
                logger.info(f"'Leave X on but turn off' pattern detected: candidate='{excluded_room_candidate}'")
                # Find matching room
                for room in room_names:
                    if room in excluded_room_candidate or excluded_room_candidate in room:
                        logger.info(f"Leave-on exclusion: turning off all EXCEPT '{room}'")
                        return {
                            "device_type": "light",
                            "room": "whole_house",
                            "excluded_rooms": [room],
                            "action": "turn_off",
                            "target_scope": "group",
                            "parameters": {},
                            "color_description": None
                        }

            # Check for exclusion patterns: "except X", "but not X", "not the X", "everything but X"
            has_exclusion = any(p in query_lower for p in ['except', 'but not', 'not the', 'excluding'])
            # Also check "everything but" or "all lights but" patterns
            if not has_exclusion:
                has_exclusion = bool(re.search(r'(everything|all\s+(the\s+)?lights?)\s+but\s+', query_lower))
            # Round 17: Added "every" and "every light" patterns
            has_all = 'all' in query_lower or 'everything' in query_lower or 'everywhere' in query_lower or 'every light' in query_lower or 'every room' in query_lower

            if has_exclusion and has_all:
                # Extract excluded rooms (rooms mentioned after "except", "but not", etc.)
                excluded_rooms = []
                # Find rooms after exclusion keywords
                exclusion_patterns = [
                    r'except\s+(the\s+)?(\w+\s*\w*)',
                    r'but not\s+(the\s+)?(\w+\s*\w*)',
                    r'not the\s+(\w+\s*\w*)',
                    r'excluding\s+(the\s+)?(\w+\s*\w*)',
                    r'(?:everything|all\s+(?:the\s+)?lights?)\s+but\s+(the\s+)?(\w+\s*\w*)'  # "everything but X" or "all lights but X"
                ]
                logger.info(f"Exclusion detection: query='{query_lower[:60]}...', has_exclusion={has_exclusion}, has_all={has_all}")
                for pattern in exclusion_patterns:
                    match = re.search(pattern, query_lower)
                    if match:
                        # Get the last group (the room name)
                        potential_room = match.group(match.lastindex).strip()
                        logger.info(f"Exclusion pattern match: pattern='{pattern}', potential_room='{potential_room}'")
                        # Validate it's a known room
                        for room in room_names:
                            if room in potential_room or potential_room in room:
                                excluded_rooms.append(room)
                                logger.info(f"Exclusion room found: '{room}'")
                                break

                logger.info(f"Total excluded_rooms: {excluded_rooms}")
                if excluded_rooms:
                    action = "turn_on" if is_turn_on else "turn_off"
                    logger.info(f"Fast path with exclusion: action={action}, room=whole_house, excluded={excluded_rooms}")
                    return {
                        "device_type": "light",
                        "room": "whole_house",
                        "excluded_rooms": excluded_rooms,
                        "action": action,
                        "target_scope": "group",
                        "parameters": {},
                        "color_description": None
                    }

            # Normal room extraction (no exclusion, single room)
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break

            # Use device room as fallback if no room in query
            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)

            action = "turn_on" if is_turn_on else "turn_off"
            logger.info(f"Fast path extract_intent: action={action}, room={final_room} for query='{query[:50]}...'")

            return {
                "device_type": "light",
                "room": final_room,
                "action": action,
                "target_scope": "group",
                "parameters": {},
                "color_description": None
            }

        # FAST PATH: Ambient/creative color commands ("sunset", "ocean", "christmas", "rainbow")
        # These common creative lighting requests are often misinterpreted by LLM as turn_off
        ambient_color_commands = {
            'sunset': {
                'hs_colors': [[20, 100], [35, 90], [10, 95]],
                'description': 'warm sunset oranges and reds'
            },
            # Round 17: Added sunrise for waking up
            'sunrise': {
                'hs_colors': [[35, 80], [45, 70], [25, 90]],
                'description': 'warm sunrise golden tones'
            },
            'ocean': {
                'hs_colors': [[180, 70], [200, 85], [160, 60]],
                'description': 'ocean blues and teals'
            },
            'christmas': {
                'hs_colors': [[0, 100], [120, 100], [0, 100]],
                'description': 'festive red and green'
            },
            'rainbow': {
                'hs_colors': [[0, 100], [60, 100], [120, 100], [180, 100], [240, 100], [300, 100]],
                'description': 'rainbow spectrum colors'
            },
            'forest': {
                'hs_colors': [[120, 80], [100, 70], [140, 60]],
                'description': 'forest greens'
            },
            'fire': {
                'hs_colors': [[10, 100], [25, 95], [0, 100]],
                'description': 'warm fire flickering tones'
            }
        }

        # Check for ambient color keywords - use these as fast path to avoid LLM misinterpretation
        for ambient_name, color_config in ambient_color_commands.items():
            if ambient_name in query_lower:
                # Extract room if present
                room_names = [
                    'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                    'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                    'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                    'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                    'first floor', 'second floor', 'downstairs', 'upstairs', 'whole house'
                ]
                extracted_room = None
                for room in room_names:
                    if room in query_lower:
                        extracted_room = room
                        break

                # If "all" or no room specified, use whole_house for ambient effects
                if 'all' in query_lower or extracted_room is None:
                    extracted_room = 'whole_house'

                logger.info(f"Fast path ambient color: {ambient_name} -> room={extracted_room}")
                return {
                    "device_type": "light",
                    "room": extracted_room,
                    "action": "set_color",
                    "target_scope": "all_individual",
                    "parameters": {"hs_colors": color_config['hs_colors']},
                    "color_description": color_config['description']
                }

        # FAST PATH: Basic color commands (blue, red, green, etc.)
        # These explicit color requests are often misinterpreted by LLM as turn_off
        basic_colors = {
            'blue': (240, 100),
            'red': (0, 100),
            'green': (120, 100),
            'yellow': (60, 100),
            'orange': (30, 100),
            'purple': (280, 100),
            'pink': (330, 100),
            'cyan': (180, 100),
            'magenta': (300, 100),
            'white': (0, 0),
            'warm': (30, 50),  # Warm white
            'cool': (200, 30),  # Cool white
        }

        # Skip color fast path if this looks like a thermostat command
        # "make it warmer" and "make it cooler" should go to thermostat, not lights
        # EXCEPT: "make the lights warmer/cooler" should set light color temperature
        # EXCEPT: Color commands with "in here" like "purple in here" should be color, not thermostat
        thermostat_indicators = [
            'warmer', 'cooler', 'temperature', 'heat', 'ac',
            'thermostat', 'degrees', 'turn up', 'turn down', 'crank'
        ]
        # Round 13: "in here" is thermostat ONLY if no color word present
        has_color_word = any(c in query_lower for c in basic_colors.keys())
        if 'in here' in query_lower and not has_color_word:
            thermostat_indicators.append('in here')
        # Check if this is a light color temperature command (not thermostat)
        is_light_color_temp = 'light' in query_lower and ('warmer' in query_lower or 'cooler' in query_lower)
        is_thermostat_context = any(ind in query_lower for ind in thermostat_indicators) and not is_light_color_temp

        # FAST PATH: Light color temperature commands ("make the lights warmer/cooler")
        if is_light_color_temp:
            # Map warmer -> warm, cooler -> cool
            if 'warmer' in query_lower:
                target_color = 'warm'
                hue, sat = 30, 50  # Warm white
            else:  # cooler
                target_color = 'cool'
                hue, sat = 200, 30  # Cool white

            # Extract room if present
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break

            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)

            logger.info(f"Fast path light color temp: {target_color} -> room={final_room}")
            return {
                "device_type": "light",
                "room": final_room,
                "action": "set_color",
                "target_scope": "all_individual",
                "parameters": {"hs_colors": [[hue, sat], [hue, sat], [hue, sat]]},
                "color_description": target_color
            }

        # Check for basic color in query (skip if thermostat context)
        color_match = None
        if not is_thermostat_context:
            for color_name, hs_value in basic_colors.items():
                if color_name in query_lower:
                    color_match = (color_name, hs_value)
                    break

        if color_match:
            color_name, (hue, sat) = color_match
            # Extract room if present
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break

            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)

            logger.info(f"Fast path basic color: {color_name} -> room={final_room}")
            return {
                "device_type": "light",
                "room": final_room,
                "action": "set_color",
                "target_scope": "all_individual",
                "parameters": {"hs_colors": [[hue, sat], [hue, sat], [hue, sat]]},
                "color_description": color_name
            }

        # FAST PATH: Brightness commands (dim to X%, brighter, dimmer)
        # Pattern: "dim the lights to 50%" or "set brightness to 75" or "make it brighter"
        # Also handles: "dim the living room lights to 50 percent" and "dim to fifty"
        brightness_match = None
        import re

        # Word-to-number mapping for brightness values
        word_to_number = {
            'zero': 0, 'one': 1, 'five': 5, 'ten': 10, 'fifteen': 15,
            'twenty': 20, 'twenty five': 25, 'thirty': 30, 'thirty five': 35,
            'forty': 40, 'forty five': 45, 'fifty': 50, 'fifty five': 55,
            'sixty': 60, 'sixty five': 65, 'seventy': 70, 'seventy five': 75,
            'eighty': 80, 'eighty five': 85, 'ninety': 90, 'ninety five': 95,
            'hundred': 100, 'one hundred': 100, 'full': 100, 'max': 100,
            'half': 50, 'quarter': 25, 'three quarters': 75
        }

        # Dim to specific percentage - flexible pattern that handles room names
        # Match patterns like:
        # - "dim to 50%"
        # - "dim the lights to 50%"
        # - "dim the living room lights to 50 percent"
        # - "set brightness to 75"
        # - "set lights to 100" (no % needed if "lights" present)
        # - "lights 50 percent" / "lights to 50%"
        dim_percent_match = re.search(r'(?:dim|brightness).*?(?:to\s+)?(\d+)\s*(?:%|percent)?', query_lower)
        if not dim_percent_match:
            # Try "set X to Y%" pattern (requires % or percent)
            dim_percent_match = re.search(r'set\s+.*?(?:to\s+)?(\d+)\s*(?:%|percent)', query_lower)
        if not dim_percent_match:
            # Try "set lights to NUMBER" without % (specific brightness value)
            # Only match if "lights" is explicitly in query to avoid thermostat confusion
            if 'light' in query_lower:
                dim_percent_match = re.search(r'(?:set|put)\s+(?:the\s+)?lights?\s+(?:to\s+)?(\d+)', query_lower)
        if not dim_percent_match:
            # Try "lights NUMBER percent" or "lights to NUMBER"
            # e.g., "lights 50 percent", "lights to 80%"
            dim_percent_match = re.search(r'lights?\s+(?:to\s+)?(\d+)\s*(?:%|percent)', query_lower)

        # Try word-based numbers if no digit match
        # Also check for "light" to catch "all lights at half", "lights to fifty"
        if not dim_percent_match and ('dim' in query_lower or 'brightness' in query_lower or 'light' in query_lower):
            for word, num in word_to_number.items():
                if word in query_lower:
                    # Create a fake match result
                    class FakeMatch:
                        def __init__(self, value):
                            self._value = value
                        def group(self, n):
                            return str(self._value)
                    dim_percent_match = FakeMatch(num)
                    break

        if dim_percent_match:
            percent = int(dim_percent_match.group(1))
            # Validate it's a reasonable percentage (not a room number or other value)
            if 1 <= percent <= 100:
                brightness = int((percent / 100) * 255)
                brightness_match = brightness

        # Relative brightness: brighter/dimmer/more light/less light/etc.
        # Patterns that mean "make it brighter"
        brighter_patterns = [
            'brighter', 'more light', 'brighten', 'too dark', 'it is dark',
            'need more light', 'can barely see', 'increase brightness', 'increase light',
            "can't see", "cant see", "cannot see", "hard to see", "difficult to see"
        ]
        # Patterns that mean "make it dimmer"
        dimmer_patterns = [
            'dimmer', 'less light', 'too bright', 'it is too bright',
            'decrease brightness', 'decrease light', 'softer light', 'make it cozy',
            'dim a little', 'dim a bit', 'a little dimmer', 'a bit dimmer',
            'darker', 'make it darker', 'can you dim', 'could you dim'
        ]
        # Also detect "dim the X" without a specific percentage (relative dimming)
        if 'dim the' in query_lower or 'dim ' in query_lower:
            # Check if there's no explicit percentage/number
            import re
            has_percent = bool(re.search(r'\d+\s*(%|percent)', query_lower))
            has_word_number = any(word in query_lower for word in ['fifty', 'twenty', 'thirty', 'forty', 'sixty', 'seventy', 'eighty', 'ninety'])
            if not has_percent and not has_word_number:
                # "dim the office" without percentage = relative dimming
                brightness_match = 'decrease'
        if any(p in query_lower for p in brighter_patterns):
            brightness_match = 'increase'
        elif any(p in query_lower for p in dimmer_patterns):
            brightness_match = 'decrease'

        if brightness_match:
            # Extract room if present
            room_names = [
                'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
                'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
                'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
                'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
                'first floor', 'second floor', 'downstairs', 'upstairs'
            ]
            extracted_room = None
            for room in room_names:
                if room in query_lower:
                    extracted_room = room
                    break

            final_room = extracted_room or (device_room if device_room and device_room not in ["unknown", "guest"] else None)

            if isinstance(brightness_match, int):
                logger.info(f"Fast path brightness: {brightness_match}/255 -> room={final_room}")
                return {
                    "device_type": "light",
                    "room": final_room,
                    "action": "set_brightness",
                    "target_scope": "group",
                    "parameters": {"brightness": brightness_match},
                    "color_description": None
                }
            else:
                action = "increase" if brightness_match == "increase" else "decrease"
                logger.info(f"Fast path brightness: {action} -> room={final_room}")
                return {
                    "device_type": "light",
                    "room": final_room,
                    "action": action,
                    "target_scope": "group",
                    "parameters": {"brightness_step": 50},  # ~20% change
                    "color_description": None
                }

        # FAST PATH: Thermostat commands - route to climate, not lights!
        thermostat_patterns = [
            'thermostat', 'temperature', 'heating', 'cooling', 'hvac',
            'heat to', 'cool to', 'set to', 'degrees',
            # Temperature adjustment patterns
            'make it warmer', 'make it cooler', 'warmer in here', 'cooler in here',
            'turn up the heat', 'turn down the heat', 'turn up the ac', 'turn down the ac',
            'crank the heat', 'crank up the heat', 'more heat', 'less heat',
            # AC status queries
            'is the ac on', 'is the ac off', 'is ac on', 'ac status', 'is the heat on',
            'raise the heat', 'lower the heat', 'bump up the heat',
            # Round 12: Additional temperature adjustment patterns
            'drop the temperature', 'lower the temperature', 'drop temperature',
            'its cold', "it's cold", 'too cold', 'chilly', 'freezing',
            'its hot', "it's hot", 'too hot', 'too warm',
            # Round 13: "warm/cool this place up/down"
            'warm this place', 'cool this place', 'warm it up', 'cool it down',
            'warm up this', 'cool down this',
            # Round 14: slang temperature
            'mad cold', 'mad hot', 'hella cold', 'hella hot', 'so cold', 'so hot',
            'drop the temp', 'drop that temp', 'raise the temp', 'raise that temp',
            # Round 16: indoor temp queries (vs weather)
            'what temp we at', 'temp we at', 'what temperature we at', 'temp inside',
            'indoor temp', 'inside temp', 'temp in here', 'how hot is it in here',
            'how cold is it in here', 'what is the temp in here'
        ]
        is_thermostat = any(p in query_lower for p in thermostat_patterns)
        # Also check for temperature setting pattern
        # Flexible regex: allow text between verb and number (e.g., "set the temperature to 72")
        temp_match = re.search(r'(?:set|heat|cool|thermostat).*?(?:to\s+)?(\d+)\s*(?:degrees)?', query_lower)
        # Fallback: if thermostat context detected, try extracting temp from "to X degrees"
        if not temp_match and is_thermostat:
            temp_match = re.search(r'(?:to\s+)?(\d{2})\s*(?:degrees|fahrenheit|celsius)?', query_lower)

        # Check for warmer/cooler adjustment patterns
        is_warmer = any(p in query_lower for p in ['warmer', 'turn up the heat', 'crank', 'more heat', 'raise the heat', 'bump up the heat',
                                                    'its cold', "it's cold", 'too cold', 'chilly', 'freezing',
                                                    'warm this place', 'warm it up', 'warm up this',  # Round 12, 13
                                                    'mad cold', 'hella cold', 'so cold', 'raise the temp', 'raise that temp'])  # Round 14
        is_cooler = any(p in query_lower for p in ['cooler', 'turn down the heat', 'turn up the ac', 'less heat', 'lower the heat',
                                                   'drop the temperature', 'lower the temperature', 'drop temperature',  # Round 12
                                                   'its hot', "it's hot", 'too hot', 'too warm',
                                                   'cool this place', 'cool it down', 'cool down this',  # Round 13
                                                   'mad hot', 'hella hot', 'so hot', 'drop the temp', 'drop that temp'])  # Round 14

        if is_thermostat or temp_match:
            target_temp = int(temp_match.group(1)) if temp_match else None
            # Determine action based on warmer/cooler patterns
            if is_warmer:
                action = "increase_temperature"
            elif is_cooler:
                action = "decrease_temperature"
            elif target_temp:
                action = "set_temperature"
            else:
                action = "get_status"
            logger.info(f"Fast path thermostat: action={action}, temp={target_temp}")
            return {
                "device_type": "climate",
                "room": None,
                "action": action,
                "target_scope": "group",
                "parameters": {"temperature": target_temp} if target_temp else {},
                "color_description": None
            }

        # Build room context hint if device room is provided
        room_context = ""
        if device_room and device_room not in ["unknown", "guest"]:
            room_context = f"\nDevice location: The user is speaking from the {device_room}. If no room is specified in the query, use \"{device_room}\" as the room."

        # Build conversation context for follow-ups and corrections
        conversation_context = ""
        if prev_query and prev_response:
            # Include previous intent entities so LLM knows what device was controlled
            prev_device_info = ""
            if prev_intent_entities:
                prev_device_type = prev_intent_entities.get('device_type', 'unknown')
                prev_room = prev_intent_entities.get('room', 'unknown')
                prev_action = prev_intent_entities.get('action', 'unknown')
                prev_params = prev_intent_entities.get('parameters', {})
                prev_device_info = f"""
Previous device controlled: {prev_device_type} in {prev_room}
Previous action: {prev_action}
Previous parameters: {prev_params}
"""
            conversation_context = f"""
CONVERSATION CONTEXT (use this to understand corrections and follow-ups):
Previous request: "{prev_query}"
Previous response: "{prev_response}"
{prev_device_info}
Current request: "{query}"

IMPORTANT: If this is a follow-up about the SAME device (e.g., "level 2", "just my side", "make it brighter"),
you MUST use the SAME device_type and room as the previous action. Do NOT change device_type unless explicitly requested.
For example, if the previous action was on "bed_warmer" and user says "level 2", use device_type="bed_warmer", NOT "light".
"""

        intent_prompt = f"""Extract the smart home control intent from this query. Return ONLY valid JSON.
{conversation_context}
Query: "{query}"
Number of lights to control: {light_count}{room_context}

Return JSON with this structure:
{{
    "device_type": "light|switch|scene|climate|oven|fridge|freezer|sensor|media_player|bed_warmer|lock",
    "room": "room name or null (use 'whole_house' for all rooms)",
    "excluded_rooms": ["list of rooms to exclude"] or null,
    "action": "turn_on|turn_off|set_color|set_brightness|set_temperature|set_level|get_status|play|pause|stop|warm_bed|increase|decrease|lock|unlock",
    "target_scope": "group|individual_lights|all_individual",
    "parameters": {{
        "brightness": 0-255 or null,
        "hs_colors": [[hue, saturation], ...] or null,
        "temperature": number or null,
        "level": 1-10 or null,
        "side": "left|right|both" or null
    }},
    "color_description": "brief description of the colors chosen"
}}

EXCLUSIONS: When user says "except [room]" or "but not [room]", set room="whole_house" and excluded_rooms=["room_name"].
Example: "turn on all lights except the bedroom" -> room="whole_house", excluded_rooms=["bedroom"]

DEVICE TYPE DETECTION:
- climate: thermostat, temperature setting, HVAC, heating, cooling
- oven: oven, stove, baking
- fridge: refrigerator, fridge, freezer
- sensor: motion, occupancy, light level, lux, brightness sensor
- media_player: TV, Apple TV, HomePod, Sonos, speaker, music, playing
- light: lights, lamps, lighting
- bed_warmer: bed warmer, mattress pad, bed heater, warm the bed, heated bed
- lock: door lock, lock, unlock, front door lock, back door lock, deadbolt

STATUS QUERIES: For questions like "what is", "what's", "check", "tell me about", use action "get_status".

For hs_colors: hue is 0-360 (red=0, green=120, blue=240), saturation is 0-100.
IMPORTANT:
- For a SINGLE color request (e.g., "turn lights blue", "make it red"), use ONE hs_color pair repeated for all {light_count} lights.
- For VARIED/DIFFERENT colors (e.g., "rainbow", "sunset", "different colors", "random"), generate {light_count} different pairs.

ROOM EXTRACTION: Extract the room name from phrases like "the [room] lights", "[room] to [color]", "in the [room]".
Room groups like "first floor", "downstairs", "upstairs", "second floor" are valid room names.

CRITICAL: Only use action "set_color" when a COLOR is explicitly mentioned. For "turn on", "turn off", "switch on", "switch off" WITHOUT any color, use "turn_on" or "turn_off".

Examples:
"what is the thermostat set to" -> {{"device_type": "climate", "room": null, "action": "get_status", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"check the fridge temperature" -> {{"device_type": "fridge", "room": null, "action": "get_status", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"is the oven on" -> {{"device_type": "oven", "room": null, "action": "get_status", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"where is there motion" -> {{"device_type": "sensor", "room": null, "action": "get_status", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"what's playing on the TV" -> {{"device_type": "media_player", "room": null, "action": "get_status", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn on the office lights" -> {{"device_type": "light", "room": "office", "action": "turn_on", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn on all lights except the bedroom" -> {{"device_type": "light", "room": "whole_house", "excluded_rooms": ["bedroom"], "action": "turn_on", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn off everything but the kitchen" -> {{"device_type": "light", "room": "whole_house", "excluded_rooms": ["kitchen"], "action": "turn_off", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"lock the front door" -> {{"device_type": "lock", "room": "front_door", "action": "lock", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"unlock the back door" -> {{"device_type": "lock", "room": "back_door", "action": "unlock", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"is the front door locked" -> {{"device_type": "lock", "room": "front_door", "action": "get_status", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn on the first floor lights" -> {{"device_type": "light", "room": "first floor", "action": "turn_on", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn off the downstairs lights" -> {{"device_type": "light", "room": "downstairs", "action": "turn_off", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn the upstairs lights on" -> {{"device_type": "light", "room": "upstairs", "action": "turn_on", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"lights on in the kitchen" -> {{"device_type": "light", "room": "kitchen", "action": "turn_on", "target_scope": "group", "parameters": {{}}, "color_description": null}}
"turn the office lights blue" -> {{"device_type": "light", "room": "office", "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[240, 100], [240, 100], [240, 100]]}}, "color_description": "blue"}}
"turn the lights blue" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[240, 100], [240, 100], [240, 100]]}}, "color_description": "blue"}}
"set office to red" -> {{"device_type": "light", "room": "office", "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[0, 100], [0, 100], [0, 100]]}}, "color_description": "red"}}
"make the bedroom lights green" -> {{"device_type": "light", "room": "bedroom", "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[120, 100], [120, 100], [120, 100]]}}, "color_description": "green"}}
"make living room lights sunset colors" -> {{"device_type": "light", "room": "living room", "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[20, 100], [35, 90], [10, 95]]}}, "color_description": "warm sunset oranges and reds"}}
"make it feel like sunset" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[20, 100], [35, 90], [10, 95]]}}, "color_description": "warm sunset oranges and reds"}}
"sunset vibes" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[25, 95], [15, 100], [40, 80]]}}, "color_description": "sunset warm tones"}}
"set kitchen to ocean vibes" -> {{"device_type": "light", "room": "kitchen", "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[180, 70], [200, 85], [160, 60]]}}, "color_description": "ocean blues and teals"}}
"different shades of purple" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[270, 100], [280, 70], [290, 85]]}}, "color_description": "various purple shades"}}
"christmas colors" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[0, 100], [120, 100], [0, 100]]}}, "color_description": "red and green christmas"}}
"random colors" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[0, 100], [120, 100], [240, 100]]}}, "color_description": "vibrant random colors"}}

SPORTS TEAM COLORS (use these hue values for team color requests):
NFL: Ravens(270,45), Steelers(50,0), Browns(30,0), Bengals(30,0), Chiefs(0,50), Bills(240,0), Dolphins(175,30), Patriots(220,0), Jets(120,0), Raiders(0,0), Broncos(30,240), Chargers(50,210), Cowboys(220,0), Eagles(180,0), Giants(240,0), Commanders(45,0), Packers(120,50), Bears(30,220), Lions(210,0), Vikings(270,50), 49ers(0,50), Seahawks(120,220), Cardinals(0,0), Rams(240,50), Falcons(0,0), Panthers(210,0), Saints(50,0), Buccaneers(0,0), Colts(240,0), Titans(210,0), Texans(220,0), Jaguars(175,50)
MLB: Orioles(30), Yankees(220), Red Sox(0), Blue Jays(240), Rays(210), Mets(240,30), Phillies(0), Braves(220,0), Marlins(210), Nationals(0), Cubs(240,0), Cardinals(0), Brewers(50,220), Reds(0), Pirates(50,0), Dodgers(240), Giants(30,0), Padres(50,45), Diamondbacks(0,175), Rockies(270,0), Astros(30,220), Rangers(240,0), Mariners(175,220), Angels(0), Athletics(120,50), Twins(220,0), White Sox(0,0), Tigers(30,220), Royals(240), Indians/Guardians(0,220)
NBA: Lakers(270,50), Celtics(120), Warriors(50,240), Bulls(0), Heat(0,0), Nets(0,0), Knicks(240,30), 76ers(240,0), Suns(30,270), Bucks(120), Hawks(0), Spurs(0,0), Mavericks(220,0), Nuggets(50,220), Clippers(0,240), Kings(270), Blazers(0,0), Jazz(50,120,220), Pacers(50,220), Cavaliers(45,0), Pistons(0,240), Magic(240,0), Hornets(175,270), Wizards(220,0), Grizzlies(210), Pelicans(50,0,220), Timberwolves(220,120), Thunder(240,30), Raptors(0,0)
NHL: Capitals(0,220), Penguins(50,0), Flyers(30,0), Rangers(240,0), Bruins(50,0), Canadiens(0,240), Maple Leafs(240), Red Wings(0), Blackhawks(0,0), Blues(240,50), Avalanche(45,240), Golden Knights(50,0), Kraken(175,0), Sharks(175), Ducks(30,50), Kings(0,0), Flames(0,50), Oilers(30,220), Canucks(120,240), Jets(220), Wild(120,0), Stars(120,0), Predators(50,220), Lightning(240), Panthers(0,220), Devils(0,0), Islanders(30,240), Hurricanes(0,0), Blue Jackets(220,0), Senators(0,50), Sabres(50,220)
College: Michigan(50,240), Ohio State(0), Penn State(220), Alabama(45,0), Georgia(0,0), Clemson(30,270), LSU(50,270), Notre Dame(50,240), Oklahoma(45,0), Texas(210,30), USC(45,0), UCLA(50,240), Florida(30,240), Auburn(30,220), Tennessee(30,0), Wisconsin(0,0), Oregon(120,50), Washington(270,50), Florida State(45,0), Miami(30,120), NC State(0,0), Duke(240), UNC(210), Kentucky(240), Kansas(240,0), Gonzaga(220,0), Villanova(220,0), Syracuse(30), Army(50,0), Navy(220,50), Air Force(240,0), West Point(50,0)
Soccer/MLS: LAFC(50,0), LA Galaxy(220,50), Atlanta United(0,50), Seattle Sounders(120,240), NYCFC(210,30), Inter Miami(0,270), Austin FC(120,0), Nashville SC(50,0), Portland Timbers(120,50), Philadelphia Union(50,220), Columbus Crew(50,0), FC Cincinnati(240,30), Toronto FC(0), Vancouver Whitecaps(220,240), Montreal(0,220)
Premier League: Manchester United(0), Manchester City(210), Liverpool(0), Chelsea(240), Arsenal(0,0), Tottenham(220,0), Newcastle(0,0), Brighton(240), Aston Villa(45,210), West Ham(45,210), Everton(240), Crystal Palace(0,240), Wolves(45,0), Leicester(240), Fulham(0,0), Bournemouth(0,0), Brentford(0,0), Nottingham Forest(0)
World: Barcelona(45,0), Real Madrid(0,0), Bayern Munich(0), Juventus(0,0), PSG(220,0), AC Milan(0,0), Inter Milan(240,0), Dortmund(50,0), Ajax(0,0), Celtic(120,0), Rangers(240)

For team colors, alternate between the team's primary colors. Example:
"michigan wolverines" -> maize(50,100) and blue(240,100) alternating
"ravens" -> purple(270,100) and gold(45,100) alternating
"orioles" -> orange(30,100)

"michigan wolverines colors" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[50, 100], [240, 100], [50, 100]]}}, "color_description": "Michigan maize and blue"}}
"ravens colors" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[270, 100], [45, 100], [270, 100]]}}, "color_description": "Ravens purple and gold"}}
"orioles colors" -> {{"device_type": "light", "room": null, "action": "set_color", "target_scope": "all_individual", "parameters": {{"hs_colors": [[30, 100], [30, 100], [30, 100]]}}, "color_description": "Orioles orange"}}

Return ONLY the JSON, no other text."""

        # Get model from database or use fallback
        admin_client = get_admin_client()
        config = await admin_client.get_component_model("smart_home_control")
        model = config.get("model_name") if config and config.get("enabled") else "llama3.1:8b"

        # Use LLM to extract intent (llama3.1:8b better at structured output than phi3:mini)
        llm_response = await self.llm_router.generate(
            model=model,
            prompt=intent_prompt,
            temperature=0.1,
            max_tokens=400
        )

        # Parse JSON response
        try:
            # Extract text from LLM response dict
            text = llm_response.get("response", "").strip()

            if '```json' in text:
                text = text.split('```json')[1].split('```')[0].strip()
            elif '```' in text:
                text = text.split('```')[1].split('```')[0].strip()

            intent = json.loads(text)
            return intent
        except json.JSONDecodeError as e:
            # Fallback to simple parsing
            return {
                "device_type": "light",
                "room": None,
                "action": "turn_on" if "turn on" in query.lower() else "turn_off",
                "target_scope": "group",
                "parameters": {}
            }
    
    def generate_random_colors(self, count: int) -> List[Tuple[int, int]]:
        """Generate N different random colors in HS format"""
        colors = []
        # Distribute hues evenly around color wheel
        hue_step = 360 / count
        for i in range(count):
            hue = int((i * hue_step) % 360)
            saturation = random.randint(80, 100)  # High saturation for vibrant colors
            colors.append((hue, saturation))

        # Shuffle to avoid predictable patterns
        random.shuffle(colors)
        return colors

    def generate_color_shades(self, base_hue: int, count: int) -> List[Tuple[int, int]]:
        """Generate N different shades of a specific color (same hue, varying saturation)"""
        colors = []
        # Vary saturation from light to vibrant
        sat_min, sat_max = 40, 100
        sat_step = (sat_max - sat_min) / max(count - 1, 1)
        for i in range(count):
            saturation = int(sat_min + (i * sat_step))
            # Also vary hue slightly (+/- 15 degrees) for more interesting shades
            hue_variation = random.randint(-15, 15)
            hue = (base_hue + hue_variation) % 360
            colors.append((hue, saturation))

        random.shuffle(colors)
        return colors
    
    def color_name_to_hs(self, color_name: str) -> Tuple[int, int]:
        """Convert color name to HS values"""
        color_map = {
            'red': (0, 100),
            'orange': (30, 100),
            'yellow': (60, 100),
            'green': (120, 100),
            'cyan': (180, 100),
            'blue': (240, 100),
            'purple': (280, 100),
            'magenta': (300, 100),
            'pink': (330, 100),
            'white': (0, 0),
        }
        return color_map.get(color_name.lower(), (0, 100))

    def detect_sequence_intent(self, query: str) -> bool:
        """
        Detect if a query requires sequence execution.

        Args:
            query: User's natural language query

        Returns:
            True if the query involves delays, loops, or scheduling
        """
        query_lower = query.lower()

        # SCENE EXCLUSION: Don't treat scene/routine commands as sequences
        # These should go through the scene handler, not sequence extraction
        scene_exclusions = [
            'good morning', 'good night', 'goodnight', 'movie mode', 'movie time',
            'bedtime', 'night mode', 'morning mode', 'wake up', 'time for bed',
            'i am leaving', "i'm leaving", 'im leaving', 'goodbye', 'leaving home',
            'i am home', "i'm home", 'im home', "i'm back", 'im back', 'home now',
            'romantic mode', 'date night', 'relax mode', 'chill mode', 'party mode'
        ]
        if any(pattern in query_lower for pattern in scene_exclusions):
            return False  # Not a sequence - let scene handler process it

        # BRIGHTNESS EXCLUSION (Round 11): Simple brightness commands should NOT be sequences
        # "all lights at half" is NOT a schedule, it's an immediate brightness command
        brightness_exclusions = [
            'lights at half', 'light at half', 'lights to half',
            'lights at fifty', 'lights to fifty', 'lights at 50', 'lights to 50',
            'all lights at half', 'all lights to half',
            'at twenty percent', 'at thirty percent', 'at forty percent',
            'at fifty percent', 'at sixty percent', 'at seventy percent',
        ]
        if any(p in query_lower for p in brightness_exclusions):
            return False  # Not a sequence - simple brightness command

        # Round 21-30: CASUAL "THEN" EXCLUSIONS
        # "then" as a filler word, not a sequencing word
        casual_then_patterns = [
            'then genius', 'then dummy', 'then idiot',  # Sarcastic commands
            'well then', 'ok then', 'okay then', 'alright then',  # Filler "then"
            'fine then', 'whatever then',  # Dismissive "then"
            'then please', 'then already',  # Impatient "then"
        ]
        if any(p in query_lower for p in casual_then_patterns):
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
        ]
        if any(p in query_lower for p in emotional_exclusions):
            action_words = ['turn', 'set', 'schedule', 'start', 'run']
            if not any(w in query_lower for w in action_words):
                return False

        # Round 21-30: MQTT/PROTOCOL QUESTIONS - not sequences
        if 'mqtt' in query_lower or 'via mqtt' in query_lower:
            return False  # Technical question, not a sequence

        # Delay/timing patterns
        delay_patterns = [
            'wait', 'then', 'after that',
            'seconds', 'second', 'minutes', 'minute',
            'pause', 'delay'
        ]

        # Loop patterns
        loop_patterns = [
            'times', 'repeat', 'cycle', 'loop', 'again',
            'on and off', 'off and on', 'flash', 'blink',
            'on then off', 'off then on'
        ]

        # Scheduling patterns
        schedule_patterns = [
            ' at ', 'at 6', 'at 7', 'at 8', 'at 9', 'at 10', 'at 11', 'at 12',
            ' pm', ' am', 'o\'clock', 'oclock',
            'tonight', 'tomorrow', 'morning', 'evening', 'noon',
            'midnight', 'schedule'
        ]

        has_delay = any(p in query_lower for p in delay_patterns)
        has_loop = any(p in query_lower for p in loop_patterns)
        has_schedule = any(p in query_lower for p in schedule_patterns)

        return has_delay or has_loop or has_schedule

    async def extract_sequence_intent(
        self,
        query: str,
        device_room: str = None,
        light_count: int = 3
    ) -> Dict:
        """
        Extract a sequence of actions from a complex query.

        Args:
            query: User's natural language query
            device_room: Room where the voice device is located
            light_count: Number of lights for color distribution

        Returns:
            Sequence definition dict with steps
        """
        import logging
        from datetime import datetime
        logger = logging.getLogger(__name__)

        current_time = datetime.now().strftime("%H:%M")
        room = device_room or "unknown"

        sequence_prompt = f"""You are a smart home assistant that creates action sequences.

Parse this request and generate a sequence of steps.

User request: "{query}"
Current room: {room}
Current time: {current_time}

Generate a JSON response:
{{
    "type": "sequence",
    "acknowledge": "Brief spoken acknowledgment",
    "steps": [
        {{
            "action": "turn_on|turn_off|set_color|set_brightness",
            "target": {{
                "device_type": "light|switch|climate|lock|media_player",
                "room": "room name"
            }},
            "parameters": {{
                "brightness": 0-255 or null,
                "hs_color": [hue 0-360, saturation 0-100] or null,
                "color_description": "color name" or null
            }},
            "delay_after": seconds to wait after (0 for none),
            "at_time": "HH:MM" for scheduled time or null
        }}
    ]
}}

Rules:
1. Unroll loops into explicit steps (e.g., "4 times" = 4 separate on/off pairs)
2. For "on and off" patterns, alternate turn_on and turn_off actions
3. For "different colors each time", use distinct colors: red(0), orange(30), yellow(60), green(120), cyan(180), blue(240), purple(280), pink(330)
4. Use delay_after for relative waits (e.g., "wait 3 seconds" → delay_after: 3)
5. Use at_time for scheduled actions (e.g., "at 6pm" → at_time: "18:00")
6. If no room specified, use: {room}
7. Keep acknowledge brief - it will be spoken aloud
8. For "keep the light on for X seconds", use turn_on + delay_after + turn_off

Color reference: red=0, orange=30, yellow=60, green=120, cyan=180, blue=240, purple=280, pink=330

Example: "turn on and off 3 times with 2 second delays" →
8 steps: on, delay 2, off, delay 2, on, delay 2, off, delay 2, on, delay 2, off

Return ONLY valid JSON."""

        # Get model from admin config
        admin_client = get_admin_client()
        config = await admin_client.get_component_model("smart_home_control")
        model = config.get("model_name") if config and config.get("enabled") else "llama3.1:8b"

        logger.info(f"Extracting sequence intent with model {model}")

        llm_response = await self.llm_router.generate(
            model=model,
            prompt=sequence_prompt,
            temperature=0.2,  # Slightly higher for creative sequences
            max_tokens=1500   # Sequences can be longer
        )

        # Parse response
        response_text = llm_response.get('response', '') if isinstance(llm_response, dict) else str(llm_response)

        # Extract JSON from response
        try:
            # Remove markdown code fences if present
            if '```json' in response_text:
                response_text = response_text.split('```json')[1].split('```')[0]
            elif '```' in response_text:
                response_text = response_text.split('```')[1].split('```')[0]

            sequence = json.loads(response_text.strip())
            logger.info(f"Extracted sequence with {len(sequence.get('steps', []))} steps")
            return sequence

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse sequence JSON: {e}")
            # Return a simple single-step sequence as fallback
            return {
                "type": "sequence",
                "acknowledge": "Let me try that.",
                "steps": [{
                    "action": "turn_on" if "on" in query.lower() else "turn_off",
                    "target": {"device_type": "light", "room": room},
                    "parameters": {},
                    "delay_after": 0,
                    "at_time": None
                }]
            }

    def _extract_room_from_query(self, query: str) -> Optional[str]:
        """Fallback room extraction from query using regex patterns"""
        import re
        query_lower = query.lower()

        # Check for whole-house patterns FIRST
        whole_house_patterns = [
            'whole house', 'entire house', 'all the lights', 'all lights',
            'every room', 'all rooms', 'everywhere', 'the house',
            'throughout the house', 'in the house',
            # Added patterns for "everything" and "all lights on/off"
            'everything off', 'turn everything', 'everything on',
            'all lights on', 'all lights off', 'lights on all', 'lights off all',
            # Round 17: "every light" patterns
            'every light', 'on every light'
        ]
        for pattern in whole_house_patterns:
            if pattern in query_lower:
                return 'whole_house'  # Special marker for whole-house control

        # Known room names to match against
        known_rooms = [
            'office', 'kitchen', 'bedroom', 'living room', 'livingroom', 'bathroom',
            'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
            'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
            'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
            # Room group names and aliases
            'first floor', '1st floor', 'main floor', 'ground floor', 'downstairs',
            'second floor', '2nd floor', 'upstairs', 'upper floor',
            'lower level', 'cellar'
        ]

        # First, try to find a known room name directly in the query
        for room in known_rooms:
            if room in query_lower:
                return room

        # Patterns to match room names in queries
        patterns = [
            r'the\s+(\w+(?:\s+\w+)?)\s+lights?',  # "the hallway lights", "the living room lights"
            r'(\w+(?:\s+\w+)?)\s+lights?\s+',      # "hallway lights blue"
            r'set\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+to',  # "set hallway to", "set the kitchen to"
            r'change\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+to',  # "change hallway to", "change the kitchen to"
            r'make\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+',   # "make hallway green", "make the kitchen blue"
            r'turn\s+(?:on|off)\s+(?:the\s+)?(\w+(?:\s+\w+)?)',  # "turn on hallway", "turn off the kitchen"
            r'(?:in|for)\s+(?:the\s+)?(\w+(?:\s+\w+)?)',  # "in the office", "for the kitchen"
        ]

        # Filter out color names and common words that aren't rooms
        non_rooms = ['lights', 'light', 'on', 'off', 'all', 'the', 'red', 'blue', 'green',
                    'yellow', 'orange', 'purple', 'pink', 'white', 'cyan', 'magenta',
                    'random', 'different', 'colors', 'color', 'change', 'set', 'make',
                    'turn', 'it', 'them', 'to', 'christmas', 'sunset', 'ocean', 'rainbow']

        for pattern in patterns:
            match = re.search(pattern, query_lower)
            if match:
                room = match.group(1).strip()
                if room not in non_rooms:
                    return room

        return None

    async def execute_intent(self, intent: Dict, ha_client, original_query: str = None, device_room: str = None) -> str:
        """Execute the extracted intent

        Args:
            intent: The extracted intent dictionary
            ha_client: Home Assistant client
            original_query: The original user query
            device_room: The room the user is speaking from (fallback if room not in intent)
        """

        device_type = intent.get('device_type', 'light')
        room = intent.get('room')
        action = intent.get('action', 'turn_on')
        target_scope = intent.get('target_scope', 'group')
        parameters = intent.get('parameters', {})

        # Check for queries that should NOT execute light control
        query_lower = original_query.lower() if original_query else ""

        # Round 21-30: Reassurance seeking phrases - give supportive response
        reassurance_patterns = ["tomorrow will be better", "will be better right"]
        if any(p in query_lower for p in reassurance_patterns):
            return "Yes, tomorrow is a fresh start! I hope things look up for you. Is there anything I can help with?"

        # Round 21-30: Observational/reaction phrases that aren't commands
        # These get misclassified as CONTROL but user is just commenting/reacting
        observational_patterns = [
            "wow they", "wow it", "they actually", "it actually",  # observations
            "cool thanks", "cool thx", "thanks nerd",  # casual thanks (not color command)
        ]
        if any(p in query_lower for p in observational_patterns):
            return "You're welcome! Is there anything else I can help you with?"

        # Round 21-30: Sarcastic "impressive" comments about capabilities
        # "oh you can control lights thats so impressive" - not a command to control lights
        sarcastic_impressive = [
            "so impressive", "thats impressive", "that's impressive",
            "really impressive", "how impressive", "very impressive",
            "can control lights", "you can control", "you can do",
        ]
        if any(p in query_lower for p in sarcastic_impressive):
            return "Yes, I can help with lights, thermostat, music, finding restaurants, weather, and more. What would you like me to do?"

        # Single-word reactions (exact match to avoid false positives)
        if query_lower.strip() in ["shocking", "surprised", "wow", "amazing", "incredible"]:
            return "Is there anything else I can help you with?"

        # Round 21-30: Impossible physical requests
        impossible_patterns = [
            "make me a sandwich", "make a sandwich", "get me a sandwich",
            "order food", "order pizza", "order for me",
        ]
        if any(p in query_lower for p in impossible_patterns):
            return "I can't do physical tasks like that, but I'm happy to help with smart home controls, finding information, or answering questions!"

        # Round 21-30: Phone call requests - explain we can't make calls
        call_patterns = ["call them", "call him", "call her", "make a call", "phone call"]
        if any(p in query_lower for p in call_patterns):
            return "I can't make phone calls, but I can help you find phone numbers or contact information for businesses."

        # Round 21-30: MQTT/API technical questions - explain capabilities
        mqtt_api_patterns = [
            "via mqtt", "support mqtt", "do you support", "do you even support",
            "api i can hit", "api i can use", "is there an api", "hit directly",
            "toggle via", "control via",
        ]
        if any(p in query_lower for p in mqtt_api_patterns):
            return ("I control devices through Home Assistant's API, which supports various protocols including MQTT, Zigbee, and Z-Wave. "
                   "For direct API access, you can use Home Assistant's REST API or WebSocket API.")

        # IoT/technical queries - should NOT execute light control
        iot_patterns = ["iot", "zigbee", "z-wave", "bandwidth", "latency",
                       "protocol", " api", "endpoint", "devices online", "devices connected"]
        if any(p in query_lower for p in iot_patterns):
            return ("I can control smart home devices like lights, thermostats, locks, and media players. "
                   "For detailed IoT device status, network diagnostics, or protocol-specific queries, "
                   "please check your Home Assistant dashboard or network management tools.")

        # Use device_room as fallback if room not specified in intent
        if not room and device_room and device_room not in ["unknown", "guest"]:
            room = device_room
            intent['room'] = room  # Update intent so downstream handlers have it

        # Handle climate/thermostat queries
        if device_type == 'climate':
            return await self._handle_climate_intent(action, parameters, original_query, ha_client)

        # Handle appliance queries (oven, fridge, freezer)
        if device_type in ['oven', 'fridge', 'freezer', 'appliance']:
            return await self._handle_appliance_intent(device_type, action, parameters, original_query)

        # Handle sensor queries
        if device_type == 'sensor':
            return await self._handle_sensor_intent(device_type, parameters, original_query)

        # Handle media player queries
        if device_type in ['media', 'media_player', 'tv', 'speaker']:
            return await self._handle_media_intent(action, parameters, original_query, room, ha_client)

        # Handle bed warmer / mattress pad
        if device_type == 'bed_warmer':
            return await self._handle_bed_warmer_intent(action, parameters, ha_client, original_query)

        # Handle motion control / lighting automation overrides
        if device_type == 'motion_control':
            return await self._handle_motion_control_intent(action, parameters, ha_client, room, original_query)

        # Handle lock control
        if device_type == 'lock':
            return await self._handle_lock_intent(action, room, ha_client, original_query)

        # Handle fan control
        if device_type == 'fan':
            return await self._handle_fan_intent(action, room, ha_client, original_query)

        # Handle cover/garage door control
        if device_type == 'cover':
            return await self._handle_cover_intent(action, room, ha_client, original_query)

        # Handle scene/routine activation
        if device_type == 'scene':
            return await self._handle_scene_intent(action, parameters, ha_client, original_query)

        if device_type != 'light':
            return "I can only control lights right now. For other devices like security systems, please use the Home Assistant app."

        # Round 16: Handle light STATUS query (check which lights are on)
        if action == 'get_status':
            return await self._handle_light_status_query(room, ha_client, original_query)

        # Fallback room extraction if LLM didn't detect it
        if not room and original_query:
            room = self._extract_room_from_query(original_query)

        if not room:
            return "I couldn't determine which room you want to control."

        # Handle whole-house commands specially
        if room == 'whole_house':
            return await self._execute_whole_house_command(
                action, target_scope, parameters, intent, ha_client, original_query
            )

        # Handle multi-room commands (e.g., "kitchen and living room")
        if room == 'multi_room':
            rooms = intent.get('rooms', [])
            if rooms:
                return await self._execute_multi_room_command(
                    rooms, action, target_scope, parameters, intent, ha_client, original_query
                )

        # Check if room is actually a room group (e.g., "first floor", "downstairs")
        admin_client = get_admin_client()
        room_group = await admin_client.resolve_room_group(room)

        if room_group:
            # This is a room group - execute on all member rooms
            return await self._execute_room_group_command(
                room_group, action, target_scope, parameters, intent, ha_client, original_query
            )

        # Find lights for the room
        light_matches = await self.entity_manager.find_lights_by_room(room)

        if not light_matches:
            return f"I couldn't find any lights for {room}."

        # Check if user wants ALL matching lights (multiple groups)
        # Detect "all" in room name or target_scope indicates multiple targets
        wants_all = "all" in room.lower() or target_scope == "all_individual" or len(light_matches) > 1

        # Collect all target lights and group names
        target_lights = []
        group_names = []

        if wants_all and len(light_matches) > 1:
            # Use ALL matching groups when "all" is specified
            for match in light_matches:
                group_names.append(match['friendly_name'])
                if target_scope == "all_individual" or target_scope == "individual_lights":
                    # Add individual members from each group
                    members = match.get('members', [])
                    if members:
                        target_lights.extend(members)
                    else:
                        target_lights.append(match['entity_id'])
                else:
                    # Add the group entity
                    target_lights.append(match['entity_id'])
            friendly_name = ", ".join(group_names)
        else:
            # Single match - use original behavior
            light_group = light_matches[0]
            friendly_name = light_group['friendly_name']
            group_names = [friendly_name]

            if target_scope == "all_individual" or target_scope == "individual_lights":
                # Work with individual lights
                members = light_group.get('members', [])
                if members:
                    target_lights = members
                else:
                    target_lights = [light_group['entity_id']]
            else:
                # Work with the group
                target_lights = [light_group['entity_id']]
        
        # Execute action based on type
        # Use brief responses suitable for voice output
        # Parallelize HA API calls for faster response
        if action == "turn_on":
            await asyncio.gather(*[
                ha_client.call_service("light", "turn_on", {"entity_id": light})
                for light in target_lights
            ])
            light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
            if len(target_lights) > 3:
                light_names += f" and {len(target_lights) - 3} more"
            return vary_response(LIGHT_ON_RESPONSES, lights=light_names)

        elif action == "turn_off":
            await asyncio.gather(*[
                ha_client.call_service("light", "turn_off", {"entity_id": light})
                for light in target_lights
            ])
            light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
            if len(target_lights) > 3:
                light_names += f" and {len(target_lights) - 3} more"
            return vary_response(LIGHT_OFF_RESPONSES, lights=light_names)
        
        elif action == "set_color":
            # Check for LLM-generated hs_colors first (new flexible approach)
            hs_colors = parameters.get('hs_colors')
            color_description = intent.get('color_description', 'custom colors')

            if hs_colors and len(hs_colors) > 0:
                # Check if this is a single-color request (color_description matches a basic color)
                # If so, use the same color for all lights
                single_color_names = ['red', 'orange', 'yellow', 'green', 'cyan', 'blue', 'purple', 'magenta', 'pink', 'white']

                # Indicators that the user wants VARIED/MULTIPLE colors (not single)
                varied_indicators = ['different', 'random', 'varied', 'various', 'rainbow',
                                    'sunset', 'ocean', 'christmas', 'party', 'disco', 'multi',
                                    'each', 'gradient', 'theme', 'mood', 'vibes',
                                    ' and ', 'maize', 'wolverine', 'team', 'colors']

                # Check for single color - the color name must be in description
                # AND there must be no indicators of varied/multiple colors
                is_single_color = False
                detected_color = None
                if color_description:
                    desc_lower = color_description.lower().strip()

                    # First check if this is explicitly a varied/multi-color request
                    is_varied = any(indicator in desc_lower for indicator in varied_indicators)

                    if not is_varied:
                        # Look for a single color name in the description
                        for color_name in single_color_names:
                            # Check if color name appears in the description
                            if color_name in desc_lower:
                                is_single_color = True
                                detected_color = color_name
                                break

                if is_single_color and detected_color:
                    # Override LLM colors - use the named color for all lights
                    hue, sat = self.color_name_to_hs(detected_color)
                    await asyncio.gather(*[
                        ha_client.call_service(
                            "light",
                            "turn_on",
                            {
                                "entity_id": light,
                                "hs_color": [hue, sat],
                                "brightness": 255
                            }
                        )
                        for light in target_lights
                    ])
                    light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
                    if len(target_lights) > 3:
                        light_names += f" and {len(target_lights) - 3} more"
                    return f"Done! I've set {light_names} to {detected_color}."
                else:
                    # Use LLM-generated colors for varied/themed requests
                    # Build color assignments then execute in parallel
                    async def set_light_color(light, hue, sat):
                        await ha_client.call_service(
                            "light",
                            "turn_on",
                            {
                                "entity_id": light,
                                "hs_color": [hue, sat],
                                "brightness": 255
                            }
                        )

                    tasks = []
                    for i, light in enumerate(target_lights):
                        color_idx = i % len(hs_colors)  # Cycle if fewer colors than lights
                        hue, sat = hs_colors[color_idx]
                        tasks.append(set_light_color(light, hue, sat))
                    await asyncio.gather(*tasks)
                    light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
                    if len(target_lights) > 3:
                        light_names += f" and {len(target_lights) - 3} more"
                    return f"Done! I've set {light_names} to {color_description}."

            # Fallback to old color_mode logic for backwards compatibility
            color_mode = parameters.get('color_mode', 'same')
            color_value = parameters.get('color_value')

            if color_mode in ["different", "random"]:
                if color_value:
                    base_hue, _ = self.color_name_to_hs(color_value)
                    colors = self.generate_color_shades(base_hue, len(target_lights))
                    color_desc = f"different shades of {color_value}"
                else:
                    colors = self.generate_random_colors(len(target_lights))
                    color_desc = "different colors"

                await asyncio.gather(*[
                    ha_client.call_service(
                        "light",
                        "turn_on",
                        {
                            "entity_id": light,
                            "hs_color": [colors[i][0], colors[i][1]],
                            "brightness": 255
                        }
                    )
                    for i, light in enumerate(target_lights)
                ])
                light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
                if len(target_lights) > 3:
                    light_names += f" and {len(target_lights) - 3} more"
                return f"Done! I've set {light_names} to {color_desc}."

            elif color_mode == "specific":
                color_value = parameters.get('color_value', 'white')
                hue, sat = self.color_name_to_hs(color_value)

                await asyncio.gather(*[
                    ha_client.call_service(
                        "light",
                        "turn_on",
                        {
                            "entity_id": light,
                            "hs_color": [hue, sat],
                            "brightness": 255
                        }
                    )
                    for light in target_lights
                ])
                light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
                if len(target_lights) > 3:
                    light_names += f" and {len(target_lights) - 3} more"
                return f"Done! I've set {light_names} to {color_value}."

        elif action == "set_brightness":
            brightness = parameters.get('brightness', 128)  # Default to 50%
            # Ensure brightness is in range 0-255
            brightness = max(0, min(255, brightness))
            percent = int((brightness / 255) * 100)

            await asyncio.gather(*[
                ha_client.call_service(
                    "light",
                    "turn_on",
                    {
                        "entity_id": light,
                        "brightness": brightness
                    }
                )
                for light in target_lights
            ])
            light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
            if len(target_lights) > 3:
                light_names += f" and {len(target_lights) - 3} more"
            return f"Done! I've set {light_names} to {percent}% brightness."

        elif action == "increase":
            # Increase brightness by ~20%
            brightness_step = parameters.get('brightness_step', 50)

            await asyncio.gather(*[
                ha_client.call_service(
                    "light",
                    "turn_on",
                    {
                        "entity_id": light,
                        "brightness_step": brightness_step
                    }
                )
                for light in target_lights
            ])
            light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
            if len(target_lights) > 3:
                light_names += f" and {len(target_lights) - 3} more"
            return f"Done! I've increased brightness for {light_names}."

        elif action == "decrease":
            # Decrease brightness by ~20%
            brightness_step = parameters.get('brightness_step', 50)

            await asyncio.gather(*[
                ha_client.call_service(
                    "light",
                    "turn_on",
                    {
                        "entity_id": light,
                        "brightness_step": -brightness_step
                    }
                )
                for light in target_lights
            ])
            light_names = ', '.join([l.split('.')[-1].replace('_', ' ') for l in target_lights[:3]])
            if len(target_lights) > 3:
                light_names += f" and {len(target_lights) - 3} more"
            return f"Done! I've decreased brightness for {light_names}."

        return "I understood your request but couldn't execute it."

    async def _handle_climate_intent(self, action: str, parameters: Dict, original_query: str = None, ha_client = None) -> str:
        """Handle climate/thermostat queries and control"""
        import logging
        logger = logging.getLogger(__name__)

        # Get climate state from entity manager
        climate_state = await self.entity_manager.get_climate_state()

        if not climate_state:
            return "I couldn't find a thermostat to check."

        query_lower = (original_query or "").lower()

        # Check if this is a status query (what is, what's, check, current, get)
        # IMPORTANT: "set to" is NOT a status query - it's a SET command
        # "what is it set to" is a status query, but "set to 72" is a command
        has_set_command = 'set' in query_lower and any(str(d) in query_lower for d in range(60, 90))
        is_status_query = not has_set_command and any(word in query_lower for word in [
            "what is", "what's", "whats", "check", "current",
            "tell me", "how", "status", "reading"
        ])
        # Also check for specific status patterns that include "get" or "set to" as queries
        if not is_status_query and not has_set_command:
            if "what is it set to" in query_lower or "what's it set to" in query_lower:
                is_status_query = True

        if is_status_query or action in ['get_status', 'check', 'read']:
            # Return current thermostat status
            current_temp = climate_state.get('current_temp')
            target_temp = climate_state.get('target_temp')
            mode = climate_state.get('state', 'unknown')
            hvac_action = climate_state.get('hvac_action', '')
            humidity = climate_state.get('humidity')

            response = f"The thermostat is set to {target_temp}°F"
            if current_temp:
                response += f" and the current temperature is {current_temp}°F"
            if mode and mode != 'unknown':
                response += f". It's in {mode} mode"
            if hvac_action and hvac_action != 'idle':
                response += f" and currently {hvac_action}"
            if humidity:
                response += f". Humidity is {humidity}%"
            response += "."

            logger.info(f"Climate status query: {response}")
            return response

        # Handle temperature adjustment actions
        current_target = climate_state.get('target_temp')
        # Check if dual-setpoint mode (heat_cool)
        is_dual_setpoint = climate_state.get('target_temp_high') is not None and climate_state.get('target_temp_low') is not None

        if action == 'increase_temperature':
            # User wants it warmer - increase by 2 degrees
            entity_id = climate_state.get('entity_id', 'climate.thermostat')
            try:
                if is_dual_setpoint:
                    # Dual-setpoint mode: adjust both high and low
                    old_high = climate_state.get('target_temp_high', 70)
                    old_low = climate_state.get('target_temp_low', 68)
                    new_high = int(old_high) + 2
                    new_low = int(old_low) + 2
                    await ha_client.call_service("climate", "set_temperature", {
                        "entity_id": entity_id,
                        "target_temp_high": new_high,
                        "target_temp_low": new_low
                    })
                    logger.info(f"Thermostat range increased to {new_low}-{new_high}°F")
                    return vary_response(THERMOSTAT_UP_RESPONSES, low=new_low, high=new_high)
                elif current_target:
                    new_temp = int(current_target) + 2
                    await ha_client.call_service("climate", "set_temperature", {
                        "entity_id": entity_id,
                        "temperature": new_temp
                    })
                    logger.info(f"Thermostat increased from {current_target}°F to {new_temp}°F")
                    return vary_response(THERMOSTAT_SET_RESPONSES, temp=new_temp)
                else:
                    return "I couldn't determine the current temperature setting."
            except Exception as e:
                logger.error(f"Failed to increase temperature: {e}")
                return f"I couldn't adjust the thermostat. Error: {e}"

        if action == 'decrease_temperature':
            # User wants it cooler - decrease by 2 degrees
            entity_id = climate_state.get('entity_id', 'climate.thermostat')
            try:
                if is_dual_setpoint:
                    # Dual-setpoint mode: adjust both high and low
                    old_high = climate_state.get('target_temp_high', 70)
                    old_low = climate_state.get('target_temp_low', 68)
                    new_high = int(old_high) - 2
                    new_low = int(old_low) - 2
                    await ha_client.call_service("climate", "set_temperature", {
                        "entity_id": entity_id,
                        "target_temp_high": new_high,
                        "target_temp_low": new_low
                    })
                    logger.info(f"Thermostat range decreased to {new_low}-{new_high}°F")
                    return vary_response(THERMOSTAT_DOWN_RESPONSES, low=new_low, high=new_high)
                elif current_target:
                    new_temp = int(current_target) - 2
                    await ha_client.call_service("climate", "set_temperature", {
                        "entity_id": entity_id,
                        "temperature": new_temp
                    })
                    logger.info(f"Thermostat decreased from {current_target}°F to {new_temp}°F")
                    return vary_response(THERMOSTAT_SET_RESPONSES, temp=new_temp)
                else:
                    return "I couldn't determine the current temperature setting."
            except Exception as e:
                logger.error(f"Failed to decrease temperature: {e}")
                return f"I couldn't adjust the thermostat. Error: {e}"

        if action == 'set_temperature':
            temp = parameters.get('temperature')
            if temp:
                try:
                    entity_id = climate_state.get('entity_id', 'climate.thermostat')
                    temp_int = int(temp)

                    if is_dual_setpoint:
                        # Dual-setpoint mode: set range centered on target temp (±2 degrees)
                        await ha_client.call_service("climate", "set_temperature", {
                            "entity_id": entity_id,
                            "target_temp_high": temp_int + 2,
                            "target_temp_low": temp_int - 2
                        })
                        logger.info(f"Thermostat dual-setpoint set to {temp_int-2}-{temp_int+2}°F")
                        return vary_response(THERMOSTAT_SET_RANGE_RESPONSES, temp=temp_int, low=temp_int-2, high=temp_int+2)
                    else:
                        await ha_client.call_service("climate", "set_temperature", {
                            "entity_id": entity_id,
                            "temperature": temp_int
                        })
                        logger.info(f"Thermostat set to {temp}°F")
                        return vary_response(THERMOSTAT_SET_RESPONSES, temp=temp_int)
                except Exception as e:
                    logger.error(f"Failed to set temperature: {e}")
                    return f"I couldn't change the thermostat, but it's currently set to {current_target}°F."

        return f"The thermostat is currently set to {current_target}°F in {climate_state.get('state')} mode."

    async def _handle_appliance_intent(self, device_type: str, action: str, parameters: Dict, original_query: str = None) -> str:
        """Handle kitchen appliance queries (oven, fridge, freezer)"""
        import httpx
        import logging
        logger = logging.getLogger(__name__)

        # Use jarvis-web API for appliance queries (it has the full implementation)
        jarvis_url = "http://localhost:3001"  # jarvis-web external URL

        query_lower = (original_query or "").lower()

        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                if device_type == 'oven' or 'oven' in query_lower:
                    response = await client.get(f"{jarvis_url}/api/appliances/oven")
                    if response.status_code == 200:
                        data = response.json()
                        state = data.get('state', 'off')
                        current_temp = data.get('current_temp')
                        cook_mode = data.get('cook_mode')
                        time_remaining = data.get('time_remaining')

                        if state == 'Off' or state == 'off':
                            return "The oven is currently off."

                        response_text = f"The oven is {state}"
                        if cook_mode and cook_mode != 'Off':
                            response_text += f" in {cook_mode} mode"
                        if current_temp:
                            response_text += f" at {current_temp}°F"
                        if time_remaining and time_remaining != 'Off':
                            response_text += f" with {time_remaining} remaining"
                        return response_text + "."

                elif device_type == 'fridge' or 'fridge' in query_lower or 'freezer' in query_lower:
                    response = await client.get(f"{jarvis_url}/api/appliances/fridge")
                    if response.status_code == 200:
                        data = response.json()
                        fridge_temp = data.get('fridge_target_temp')
                        freezer_temp = data.get('freezer_target_temp')
                        door_open = data.get('door_open', False)

                        response_text = f"The fridge is set to {fridge_temp}°F and the freezer is set to {freezer_temp}°F"
                        if door_open:
                            response_text += ". Warning: the refrigerator door is open!"
                        return response_text + "."

        except Exception as e:
            logger.error(f"Appliance query error: {e}")
            return "I couldn't check the appliance status right now."

        return "I'm not sure which appliance you're asking about."

    async def _handle_sensor_intent(self, sensor_type: str, parameters: Dict, original_query: str = None) -> str:
        """Handle sensor queries (motion, temperature, illuminance, occupancy)"""
        import httpx
        import logging
        logger = logging.getLogger(__name__)

        jarvis_url = "http://localhost:3001"
        query_lower = (original_query or "").lower()

        # Check for occupancy estimation queries
        occupancy_patterns = [
            'how many people', 'anyone home', 'anybody home', 'someone home',
            'is anyone', 'is anybody', 'who is home', 'who\'s home', 'whos home',
            'people are here', 'people are home', 'occupied', 'occupancy',
            'how many are home', 'is the house empty', 'is anyone in the house',
            'based on motion', 'likely here', 'probably home',
            # Round 15: Additional occupancy patterns
            'anybody in', 'anyone in', 'someone in', 'somebody in',
            'is there anybody', 'is there anyone', 'is there someone',
            'people in the'
        ]
        is_occupancy_query = any(p in query_lower for p in occupancy_patterns)

        # Also check if sensor_type explicitly requests occupancy
        if sensor_type == 'occupancy' or parameters.get('sensor_type') == 'occupancy':
            is_occupancy_query = True

        if is_occupancy_query:
            return await self._estimate_occupancy(original_query)

        # Check for stuck sensor queries
        stuck_patterns = [
            'stuck', 'frozen', 'not working', 'broken', 'faulty', 'malfunctioning',
            'sensor issue', 'sensor problem', 'sensors seem', 'seem to be stuck',
            'any issues', 'sensor health', 'check sensors', 'verify sensors'
        ]
        is_stuck_query = any(p in query_lower for p in stuck_patterns)

        if is_stuck_query:
            return await self._check_stuck_sensors()

        # Check for "last motion" or "when was motion" queries
        last_motion_patterns = [
            'last motion', 'last time motion', 'when was motion', 'where was motion',
            'last movement', 'when was the last', 'most recent motion', 'latest motion',
            'last time someone', 'when was someone', 'who was home', 'who was here',
            'last activity', 'recent activity', 'recent motion'
        ]
        is_last_motion_query = any(p in query_lower for p in last_motion_patterns)

        if is_last_motion_query or 'motion' in query_lower or 'movement' in query_lower:
            # Use smart detection with presence sensor prioritization
            healthy_sensors, stuck_sensors = await self._get_all_motion_sensors_with_stuck_detection()
            if healthy_sensors:
                return await self._format_motion_status(healthy_sensors, is_last_motion_query)

            # Fallback to jarvis-web
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    response = await client.get(f"{jarvis_url}/api/sensors/motion")
                    if response.status_code == 200:
                        data = response.json()
                        active_rooms = data.get('active_rooms', [])
                        if active_rooms:
                            return f"Motion detected in: {', '.join(active_rooms)}."
                        return "No motion detected in any room right now."
            except:
                pass

        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                if False:  # Disabled - motion handled above
                    pass

                elif 'light' in query_lower or 'bright' in query_lower or 'dark' in query_lower or 'lux' in query_lower:
                    response = await client.get(f"{jarvis_url}/api/sensors/illuminance")
                    if response.status_code == 200:
                        data = response.json()
                        sensors = data.get('sensors', [])
                        if sensors:
                            readings = [f"{s['room']}: {s['illuminance']} lux ({s['brightness_level']})" for s in sensors[:5]]
                            return "Light levels - " + ", ".join(readings) + "."
                        return "No light sensors found."

                # Round 13: Window sensor queries
                elif 'window' in query_lower or parameters.get('sensor_type') == 'window':
                    return await self._get_window_sensor_status()

                # Default: get summary
                response = await client.get(f"{jarvis_url}/api/sensors/summary")
                if response.status_code == 200:
                    data = response.json()
                    motion = data.get('motion', {})
                    temp = data.get('temperature', {})
                    light = data.get('illuminance', {})

                    parts = []
                    if motion.get('active_rooms'):
                        parts.append(f"Motion in {', '.join(motion['active_rooms'])}")
                    if temp.get('average'):
                        parts.append(f"Average temp {temp['average']}°F")
                    if light.get('brightest_room'):
                        parts.append(f"Brightest: {light['brightest_room']}")

                    if parts:
                        return ". ".join(parts) + "."
                    return "All sensors are reading normally."

        except Exception as e:
            logger.error(f"Sensor query error: {e}")
            return "I couldn't check the sensor status right now."

        return "I'm not sure which sensor you're asking about."

    async def _estimate_occupancy(self, original_query: str = None) -> str:
        """
        Estimate how many people are home based on motion sensor patterns.
        Uses LLM reasoning with house layout context.
        Excludes stuck sensors that haven't changed state in an unusually long time.
        """
        import httpx
        import logging
        from datetime import datetime, timezone
        logger = logging.getLogger(__name__)

        try:
            # Step 1: Fetch motion sensors with stuck detection
            # This excludes stuck sensors and creates alerts for them
            healthy_sensors, stuck_sensors = await self._get_all_motion_sensors_with_stuck_detection()

            if not healthy_sensors and not stuck_sensors:
                return "I couldn't access the motion sensors to estimate occupancy."

            if not healthy_sensors and stuck_sensors:
                stuck_rooms = [s['room'] for s in stuck_sensors]
                return f"All motion sensors appear to be stuck and have been excluded. Affected sensors: {', '.join(stuck_rooms)}. I can't estimate occupancy until the sensors are fixed."

            # Log if we excluded any stuck sensors
            if stuck_sensors:
                stuck_rooms = [s['room'] for s in stuck_sensors]
                logger.warning(f"Excluded {len(stuck_sensors)} stuck sensors from occupancy: {stuck_rooms}")

            # Step 2: Fetch house layout from admin API
            house_layout = await self._get_house_layout()

            # Step 3: Use LLM to reason about occupancy (only using healthy sensors)
            return await self._llm_occupancy_reasoning(healthy_sensors, house_layout, original_query)

        except Exception as e:
            logger.error(f"Occupancy estimation error: {e}", exc_info=True)
            return "I had trouble estimating occupancy. Please try again."

    async def _get_window_sensor_status(self) -> str:
        """Get status of all window/door contact sensors. (Round 13)"""
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Get all entity states from HA via entity manager
            all_entities = await self.entity_manager.get_entities()

            open_windows = []
            closed_windows = []
            for entity_id, state in all_entities.items():
                entity_lower = entity_id.lower()

                # Look for window or door contact sensors
                if entity_id.startswith('binary_sensor.'):
                    if any(x in entity_lower for x in ['window', 'contact', 'door_sensor', 'door_window']):
                        # Skip door locks (not sensors)
                        if 'lock' in entity_lower:
                            continue

                        sensor_state = state.get('state')
                        friendly_name = state.get('attributes', {}).get('friendly_name', entity_id)

                        # Extract room from entity_id or friendly_name
                        room = None
                        room_names = ['office', 'kitchen', 'bedroom', 'master', 'living', 'bath', 'basement', 'garage', 'porch', 'front', 'back']
                        # Try entity_id first
                        entity_lower = entity_id.lower()
                        for room_name in room_names:
                            if room_name in entity_lower:
                                room = room_name
                                break
                        if not room:
                            # Try friendly name
                            for room_name in room_names:
                                if room_name in friendly_name.lower():
                                    room = room_name
                                    break
                            if not room:
                                room = friendly_name

                        if sensor_state == 'on':  # 'on' typically means open for contact sensors
                            open_windows.append(room)
                        else:
                            closed_windows.append(room)

            if open_windows:
                if len(open_windows) == 1:
                    return f"The {open_windows[0]} window is open."
                else:
                    return f"These windows are open: {', '.join(open_windows)}."
            elif closed_windows:
                return f"All {len(closed_windows)} windows are closed."
            else:
                return "I couldn't find any window sensors in the system."

        except Exception as e:
            logger.error(f"Window sensor query error: {e}")
            return "I had trouble checking the window sensors. Please try again."

    async def _get_all_motion_sensors(self) -> List[Dict]:
        """Fetch all motion/presence sensors from Home Assistant with their states and timestamps."""
        import logging
        from datetime import datetime
        logger = logging.getLogger(__name__)

        try:
            # Get all entity states from HA via entity manager
            all_entities = await self.entity_manager.get_entities()

            motion_sensors = []
            for entity_id, state in all_entities.items():

                # Filter for motion/presence/occupancy sensors
                # Exclude tamper sensors, Ring cameras (outdoor), and other non-indoor sensors
                entity_lower = entity_id.lower()
                if 'tamper' in entity_lower:
                    continue  # Skip tamper sensors
                if 'ring' in entity_lower:
                    continue  # Skip Ring camera/doorbell motion (outdoor)

                if any(x in entity_lower for x in ['motion', 'presence', 'occupancy', 'pir']):
                    if entity_id.startswith('binary_sensor.'):
                        # Extract room name from entity_id or friendly_name
                        friendly_name = state.get('attributes', {}).get('friendly_name', entity_id)
                        room = self._extract_room_from_entity(entity_id, friendly_name)

                        # Skip sensors without a recognized room (e.g., generic "Presence Sensor 1")
                        if room is None:
                            logger.debug(f"Skipping sensor with unknown room: {entity_id} [{friendly_name}]")
                            continue

                        motion_sensors.append({
                            'entity_id': entity_id,
                            'room': room,
                            'state': state.get('state'),  # 'on' or 'off'
                            'last_changed': state.get('last_changed'),
                            'friendly_name': friendly_name
                        })
                        logger.debug(f"Motion sensor: {entity_id} -> room: {room}, state: {state.get('state')}")

            logger.info(f"Found {len(motion_sensors)} motion sensors: {[s['room'] for s in motion_sensors]}")
            return motion_sensors

        except Exception as e:
            logger.error(f"Error fetching motion sensors: {e}")
            return []

    async def _get_all_motion_sensors_with_stuck_detection(self) -> Tuple[List[Dict], List[Dict]]:
        """
        Fetch all motion/presence sensors and detect stuck sensors.
        Returns (healthy_sensors, stuck_sensors) tuple.

        SENSOR HIERARCHY (most reliable to least):
        1. presence/occupancy sensors (mmWave radar) - most accurate for actual human presence
        2. pir_detection sensors - can give false positives from HVAC, heat, etc.
        3. generic motion sensors - legacy sensors, variable reliability

        When a room has multiple sensor types, we prioritize presence/occupancy sensors.
        PIR sensors are only used if no presence sensor exists for that room, or to
        cross-validate presence detection.

        A sensor is considered "stuck" if:
        - State is "on" for more than 4 hours (motion sensors shouldn't stay on that long)

        NOTE: We do NOT flag sensors as stuck for being "off" too long. Being off is the
        normal state when no one is home (e.g., vacation, travel). Only continuous "on"
        state indicates a potential malfunction.
        """
        import logging
        import httpx
        from datetime import datetime, timezone, timedelta
        logger = logging.getLogger(__name__)

        try:
            all_entities = await self.entity_manager.get_entities()

            # Collect sensors by room and type for prioritization
            sensors_by_room = {}  # room -> {'presence': [], 'pir': [], 'motion': []}
            stuck_sensors = []
            now = datetime.now(timezone.utc)

            # Threshold for stuck detection - only "on" state indicates malfunction
            # Being "off" for a long time is normal (vacation, travel, etc.)
            STUCK_ON_HOURS = 4  # Motion sensor "on" for 4+ hours is suspicious

            for entity_id, state in all_entities.items():
                entity_lower = entity_id.lower()
                friendly_name = state.get('attributes', {}).get('friendly_name', entity_id)
                friendly_lower = friendly_name.lower()

                # Skip tamper and Ring sensors
                if 'tamper' in entity_lower or 'ring' in entity_lower:
                    continue

                # Skip outdoor/exterior sensors (check both entity_id and friendly_name)
                # These sensors detect external activity, not home presence
                outdoor_patterns = [
                    'front_door', 'back_door', 'entrance_door', 'entrance',
                    'clinton', 'outdoor', 'driveway', 'porch', 'deck', 'patio',
                    'exterior', 'outside', 'garage_door', 'doorbell'
                ]
                if any(x in entity_lower or x in friendly_lower for x in outdoor_patterns):
                    continue

                if any(x in entity_lower for x in ['motion', 'presence', 'occupancy', 'pir']):
                    if entity_id.startswith('binary_sensor.'):
                        room = self._extract_room_from_entity(entity_id, friendly_name)

                        if room is None:
                            continue

                        sensor_state = state.get('state')
                        last_changed = state.get('last_changed', '')

                        # Determine sensor type for prioritization
                        # SENSOR HIERARCHY (most to least reliable):
                        # 1. mmWave radar presence sensors - most accurate
                        # 2. Generic occupancy sensors - may be PIR-based, less reliable
                        # 3. PIR motion sensors - can give false positives from HVAC
                        #
                        # IMPORTANT: Check PIR first because some entities have both
                        # e.g., "motion_presence_alpha_pir_detection" contains both
                        # "_presence" (from device name) AND "_pir" (sensor type)
                        #
                        # mmWave indicators: "mm_", "_mm_", "fp2", "_presence_sensor"
                        # PIR indicators: "_pir", "pir_detection"
                        # Generic occupancy: ends with "_occupancy" but no mmWave indicator

                        is_mmwave = any(x in entity_lower for x in ['mm_', '_mm_', 'fp2', '_presence_sensor'])
                        is_pir = '_pir' in entity_lower or 'pir_detection' in entity_lower

                        if is_pir:
                            sensor_type = 'pir'  # Low priority - can have false positives
                        elif is_mmwave or entity_lower.endswith('_presence'):
                            sensor_type = 'presence'  # High priority - mmWave/radar
                        elif entity_lower.endswith('_occupancy'):
                            # Generic occupancy without mmWave indicator - treat as medium priority
                            # Could be PIR-based, so not as reliable as true mmWave
                            sensor_type = 'occupancy'  # Medium priority - might be PIR-based
                        elif '_presence' in entity_lower or '_occupancy' in entity_lower:
                            # Has presence/occupancy in name - check for mmWave markers
                            sensor_type = 'presence' if is_mmwave else 'occupancy'
                        else:
                            sensor_type = 'motion'  # Generic motion sensor

                        # Calculate time since last change
                        is_stuck = False
                        hours_unchanged = 0

                        if last_changed:
                            try:
                                changed_time = datetime.fromisoformat(last_changed.replace('Z', '+00:00'))
                                delta = now - changed_time
                                hours_unchanged = delta.total_seconds() / 3600

                                # Only flag as stuck if sensor is ON for too long
                                # Being OFF for a long time is normal (vacation, travel, etc.)
                                if sensor_state == 'on' and hours_unchanged > STUCK_ON_HOURS:
                                    is_stuck = True
                                    logger.warning(
                                        f"Stuck sensor detected: {entity_id} has been 'on' for {hours_unchanged:.1f} hours"
                                    )
                            except Exception as e:
                                logger.debug(f"Error parsing timestamp for {entity_id}: {e}")

                        sensor_data = {
                            'entity_id': entity_id,
                            'room': room,
                            'state': sensor_state,
                            'last_changed': last_changed,
                            'friendly_name': friendly_name,
                            'hours_unchanged': hours_unchanged,
                            'sensor_type': sensor_type  # NEW: Track sensor type
                        }

                        if is_stuck:
                            stuck_sensors.append(sensor_data)
                            # Create alert for stuck sensor
                            await self._create_stuck_sensor_alert(sensor_data)
                        else:
                            # Group healthy sensors by room and type
                            if room not in sensors_by_room:
                                sensors_by_room[room] = {'presence': [], 'occupancy': [], 'pir': [], 'motion': []}
                            sensors_by_room[room][sensor_type].append(sensor_data)

            # Build final healthy_sensors list with prioritization
            # Sensor priority: presence (mmWave) > occupancy > PIR > motion
            healthy_sensors = []
            pir_false_positives = []
            occupancy_deprioritized = []

            for room, sensor_types in sensors_by_room.items():
                presence_sensors = sensor_types['presence']
                occupancy_sensors = sensor_types.get('occupancy', [])
                pir_sensors = sensor_types['pir']
                motion_sensors = sensor_types['motion']

                # If room has presence sensor (mmWave), it's the authority
                if presence_sensors:
                    # Use presence sensor as primary
                    for sensor in presence_sensors:
                        sensor['is_authoritative'] = True
                        healthy_sensors.append(sensor)

                    # Deprioritize occupancy sensors in rooms with mmWave presence
                    # (occupancy sensors might be PIR-based and give false positives)
                    for occ in occupancy_sensors:
                        occ['is_authoritative'] = False
                        occ['deprioritized'] = True
                        occupancy_deprioritized.append(occ)
                        logger.debug(
                            f"Deprioritized occupancy sensor in {room}: {occ['entity_id']} "
                            f"(mmWave presence sensor is authority)"
                        )

                    # Check if PIR triggered but presence didn't (false positive)
                    presence_detected = any(s['state'] == 'on' for s in presence_sensors)
                    for pir in pir_sensors:
                        if pir['state'] == 'on' and not presence_detected:
                            # PIR triggered but presence sensor says no one there
                            pir['is_authoritative'] = False
                            pir['likely_false_positive'] = True
                            pir_false_positives.append(pir)
                            logger.info(
                                f"PIR false positive detected in {room}: {pir['entity_id']} triggered "
                                f"but presence sensor shows no one present"
                            )
                        else:
                            # PIR agrees with presence sensor - but still not authoritative
                            pir['is_authoritative'] = False
                            # Don't add PIR to healthy_sensors if we have mmWave presence

                elif occupancy_sensors:
                    # No mmWave presence, but have occupancy sensors
                    for sensor in occupancy_sensors:
                        sensor['is_authoritative'] = True
                        healthy_sensors.append(sensor)

                    # Deprioritize PIR in rooms with occupancy sensors
                    for pir in pir_sensors:
                        pir['is_authoritative'] = False
                else:
                    # No presence or occupancy sensor, use PIR/motion as fallback
                    for sensor in pir_sensors + motion_sensors:
                        sensor['is_authoritative'] = True  # Only authority for this room
                        healthy_sensors.append(sensor)

            # Log summary
            presence_count = sum(1 for s in healthy_sensors if s.get('sensor_type') == 'presence')
            occupancy_count = sum(1 for s in healthy_sensors if s.get('sensor_type') == 'occupancy')
            pir_count = sum(1 for s in healthy_sensors if s.get('sensor_type') == 'pir')
            logger.info(
                f"Motion sensors: {len(healthy_sensors)} healthy "
                f"({presence_count} mmWave presence, {occupancy_count} occupancy, {pir_count} PIR), "
                f"{len(stuck_sensors)} stuck, {len(pir_false_positives)} PIR false positives, "
                f"{len(occupancy_deprioritized)} occupancy deprioritized",
                extra={"stuck_sensors": [s['entity_id'] for s in stuck_sensors]}
            )

            return healthy_sensors, stuck_sensors

        except Exception as e:
            logger.error(f"Error in stuck sensor detection: {e}")
            return [], []

    async def _create_stuck_sensor_alert(self, sensor_data: Dict) -> None:
        """Create an alert in the admin system for a stuck sensor."""
        import httpx
        import logging
        logger = logging.getLogger(__name__)

        try:
            admin_url = "http://localhost:8080"

            alert_payload = {
                "alert_type": "stuck_sensor",
                "severity": "warning",
                "title": f"Stuck Motion Sensor: {sensor_data['room']}",
                "message": f"Motion sensor {sensor_data['friendly_name']} has been in '{sensor_data['state']}' state for {sensor_data['hours_unchanged']:.1f} hours. This sensor is being excluded from occupancy queries.",
                "entity_id": sensor_data['entity_id'],
                "entity_type": "motion_sensor",
                "alert_data": {
                    "room": sensor_data['room'],
                    "state": sensor_data['state'],
                    "hours_unchanged": sensor_data['hours_unchanged'],
                    "last_changed": sensor_data['last_changed']
                },
                "dedup_key": f"stuck_sensor_{sensor_data['entity_id']}"
            }

            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                response = await client.post(
                    f"{admin_url}/api/alerts/public/create",
                    json=alert_payload
                )

                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Stuck sensor alert created/found: {result.get('id')}")
                else:
                    logger.warning(f"Failed to create stuck sensor alert: {response.status_code}")

        except Exception as e:
            logger.error(f"Error creating stuck sensor alert: {e}")

    async def _resolve_stuck_sensor_alert(self, entity_id: str) -> None:
        """Resolve alert when a previously stuck sensor starts working again."""
        import httpx
        import logging
        logger = logging.getLogger(__name__)

        try:
            admin_url = "http://localhost:8080"

            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                response = await client.post(
                    f"{admin_url}/api/alerts/public/resolve-by-entity",
                    params={
                        "entity_id": entity_id,
                        "alert_type": "stuck_sensor"
                    }
                )

                if response.status_code == 200:
                    result = response.json()
                    if result.get('resolved_count', 0) > 0:
                        logger.info(f"Resolved stuck sensor alert for {entity_id}")

        except Exception as e:
            logger.debug(f"Error resolving stuck sensor alert: {e}")

    async def _check_stuck_sensors(self) -> str:
        """
        Check for stuck motion sensors and report on their status.
        Creates alerts for any stuck sensors found.
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            healthy_sensors, stuck_sensors = await self._get_all_motion_sensors_with_stuck_detection()

            total_sensors = len(healthy_sensors) + len(stuck_sensors)

            if not total_sensors:
                return "I couldn't access the motion sensors to check for issues."

            if not stuck_sensors:
                return f"All {len(healthy_sensors)} motion sensors are working normally. No stuck sensors detected."

            # Build detailed report for stuck sensors
            stuck_details = []
            for sensor in stuck_sensors:
                hours = sensor['hours_unchanged']
                if hours < 24:
                    time_str = f"{hours:.1f} hours"
                else:
                    days = hours / 24
                    time_str = f"{days:.1f} days"

                stuck_details.append(
                    f"- {sensor['room']}: {sensor['friendly_name']} has been '{sensor['state']}' for {time_str}"
                )

            response = f"Found {len(stuck_sensors)} stuck sensor(s) out of {total_sensors} total:\n"
            response += "\n".join(stuck_details)
            response += f"\n\nThese sensors are being excluded from occupancy queries. Alerts have been created in the admin system."

            return response

        except Exception as e:
            logger.error(f"Error checking stuck sensors: {e}")
            return "I had trouble checking the sensor status. Please try again."

    def _extract_room_from_entity(self, entity_id: str, friendly_name: str = None) -> str:
        """Extract room name from motion sensor entity ID or friendly name."""
        # IMPORTANT: Order matters! More specific patterns must come first
        # Patterns are checked in order, first match wins
        room_patterns = [
            # Specific bathroom patterns (before 'master' or 'bath')
            ('master_bath', 'Master Bathroom'),
            ('master bath', 'Master Bathroom'),
            ('masterbath', 'Master Bathroom'),
            ('main_bath', 'Main Bathroom'),
            ('main bath', 'Main Bathroom'),
            ('mainbath', 'Main Bathroom'),
            ('powder', 'Powder Room'),
            ('basement_bath', 'Basement Bathroom'),
            ('basement bath', 'Basement Bathroom'),
            # Master bedroom (after master bath patterns)
            ('master_bed', 'Master Bedroom'),
            ('master bed', 'Master Bedroom'),
            ('masterbed', 'Master Bedroom'),
            ('master_closet', 'Master Closet'),
            ('master closet', 'Master Closet'),
            ('underbed', 'Master Bedroom'),
            # Other bedrooms
            ('alpha', 'Alpha'),
            ('beta', 'Beta'),
            # Living areas
            ('living', 'Living Room'),
            ('dining', 'Dining Room'),
            ('kitchen', 'Kitchen'),
            ('office', 'Office'),
            # Hallways (be specific)
            ('basement_hall', 'Basement Hallway'),
            ('basement hall', 'Basement Hallway'),
            ('hallway_front', 'Front Hallway'),
            ('front_hall', 'Front Hallway'),
            ('hall', 'Hallway'),
            # Basement
            ('basement_stair', 'Basement Stairs'),
            ('basement stair', 'Basement Stairs'),
            ('basement', 'Basement'),
            # Generic patterns (after specific ones)
            ('master', 'Master Bedroom'),  # Fallback if no bath/bed specified
            ('shower', 'Master Bathroom'),
            ('front_door', 'Front Door'),
            ('front door', 'Front Door'),
            ('back_door', 'Back Door'),
            ('entrance', 'Entrance'),
        ]

        # Combine entity_id and friendly_name for matching
        entity_lower = entity_id.lower().replace('binary_sensor.', '')
        search_text = entity_lower
        if friendly_name:
            search_text = f"{entity_lower} {friendly_name.lower()}"

        # Check patterns in order (first match wins)
        for pattern, room_name in room_patterns:
            if pattern in search_text:
                return room_name

        # No known room pattern matched - return None to skip this sensor
        # This filters out generic sensors like "Presence Sensor 1" with hex codes
        return None

    async def _format_motion_status(self, motion_data: List[Dict], is_last_motion_query: bool = False) -> str:
        """
        Format motion sensor data for user response.

        PRIORITIZATION: Presence sensors (mmWave) are more reliable than PIR sensors.
        When determining "last motion", we prefer presence sensor data because PIR
        sensors can give false positives from HVAC, heat changes, etc.
        """
        from datetime import datetime, timezone
        import logging
        logger = logging.getLogger(__name__)

        now = datetime.now(timezone.utc)
        active_rooms = []
        last_motion_info = []

        # Separate sensors by type for prioritization
        # Presence sensors (mmWave) are most reliable, then occupancy, then PIR/motion
        presence_sensors = [s for s in motion_data if s.get('sensor_type') == 'presence']
        other_sensors = [s for s in motion_data if s.get('sensor_type') != 'presence']

        # For rooms with presence sensors, exclude other sensor types
        # This avoids reporting PIR/occupancy false positives as actual presence
        rooms_with_presence = set(s['room'] for s in presence_sensors)
        filtered_other = [s for s in other_sensors if s['room'] not in rooms_with_presence]

        # Combine: presence sensors + other sensors for rooms without presence
        sensors_to_use = presence_sensors + filtered_other

        logger.info(
            f"Motion status: using {len(presence_sensors)} mmWave presence sensors, "
            f"{len(filtered_other)} other sensors (filtered {len(other_sensors) - len(filtered_other)} in rooms with presence)"
        )

        for sensor in sensors_to_use:
            room = sensor['room']
            state = sensor['state']
            last_changed = sensor.get('last_changed', '')
            sensor_type = sensor.get('sensor_type', 'motion')
            is_authoritative = sensor.get('is_authoritative', True)

            # Skip sensors marked as false positives
            if sensor.get('likely_false_positive'):
                logger.debug(f"Skipping likely false positive: {sensor['entity_id']}")
                continue

            try:
                if last_changed:
                    if isinstance(last_changed, str):
                        last_changed = last_changed.replace('Z', '+00:00')
                        changed_time = datetime.fromisoformat(last_changed)
                    else:
                        changed_time = last_changed

                    seconds_ago = (now - changed_time).total_seconds()
                    minutes_ago = int(seconds_ago / 60)

                    if state == 'on':
                        active_rooms.append(room)
                        last_motion_info.append({
                            'room': room,
                            'time': changed_time,
                            'minutes_ago': 0,
                            'status': 'active',
                            'sensor_type': sensor_type,
                            'is_authoritative': is_authoritative
                        })
                    else:
                        last_motion_info.append({
                            'room': room,
                            'time': changed_time,
                            'minutes_ago': minutes_ago,
                            'status': 'inactive',
                            'sensor_type': sensor_type,
                            'is_authoritative': is_authoritative
                        })

            except Exception as e:
                logger.warning(f"Error parsing motion timestamp for {room}: {e}")

        if is_last_motion_query:
            # Sort by most recent motion
            last_motion_info.sort(key=lambda x: x['time'], reverse=True)

            if not last_motion_info:
                return "I couldn't find any motion sensor data."

            most_recent = last_motion_info[0]
            room = most_recent['room']
            minutes_ago = most_recent['minutes_ago']

            if most_recent['status'] == 'active':
                return f"Motion is currently active in the {room}."
            elif minutes_ago < 1:
                return f"The last motion was just detected in the {room}, less than a minute ago."
            elif minutes_ago < 60:
                return f"The last motion was in the {room}, about {minutes_ago} minute{'s' if minutes_ago != 1 else ''} ago."
            else:
                hours_ago = minutes_ago // 60
                return f"The last motion was in the {room}, about {hours_ago} hour{'s' if hours_ago != 1 else ''} ago."

        else:
            # Standard motion query - show active rooms
            if active_rooms:
                return f"Motion detected in: {', '.join(active_rooms)}."
            else:
                # Show most recent if no active
                if last_motion_info:
                    last_motion_info.sort(key=lambda x: x['time'], reverse=True)
                    most_recent = last_motion_info[0]
                    minutes_ago = most_recent['minutes_ago']
                    if minutes_ago < 60:
                        return f"No active motion right now. Last motion was in the {most_recent['room']}, {minutes_ago} minute{'s' if minutes_ago != 1 else ''} ago."
                    else:
                        hours_ago = minutes_ago // 60
                        return f"No active motion right now. Last motion was in the {most_recent['room']}, {hours_ago} hour{'s' if hours_ago != 1 else ''} ago."
                return "No motion detected in any room right now."

    async def _get_house_layout(self) -> str:
        """Fetch house layout description from admin API."""
        import httpx
        import logging
        logger = logging.getLogger(__name__)

        try:
            admin_url = "http://localhost:8080"
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                response = await client.get(f"{admin_url}/api/settings/house-layout")
                if response.status_code == 200:
                    data = response.json()
                    if data.get('has_layout'):
                        return data.get('layout_description', '')

            return ""  # No layout configured

        except Exception as e:
            logger.warning(f"Could not fetch house layout: {e}")
            return ""

    async def _llm_occupancy_reasoning(self, motion_data: List[Dict], house_layout: str, original_query: str = None) -> str:
        """Use LLM to reason about occupancy based on motion data and house layout."""
        import logging
        from datetime import datetime, timezone
        logger = logging.getLogger(__name__)

        # Deduplicate sensors by room - keep most relevant state per room
        # Priority: ON (active) > recently OFF (< 5min) > older OFF
        now = datetime.now(timezone.utc)
        room_states = {}  # room -> {state, changed_time, seconds_ago}

        for sensor in motion_data:
            room = sensor['room']
            state = sensor['state']
            last_changed = sensor.get('last_changed', '')

            try:
                if last_changed:
                    if isinstance(last_changed, str):
                        last_changed = last_changed.replace('Z', '+00:00')
                        changed_time = datetime.fromisoformat(last_changed)
                    else:
                        changed_time = last_changed
                    seconds_ago = (now - changed_time).total_seconds()
                else:
                    seconds_ago = float('inf')

                # Decide if this sensor should replace existing room data
                if room not in room_states:
                    room_states[room] = {'state': state, 'seconds_ago': seconds_ago}
                else:
                    existing = room_states[room]
                    # ON always wins
                    if state == 'on' and existing['state'] != 'on':
                        room_states[room] = {'state': state, 'seconds_ago': seconds_ago}
                    # If both same state, keep more recent
                    elif state == existing['state'] and seconds_ago < existing['seconds_ago']:
                        room_states[room] = {'state': state, 'seconds_ago': seconds_ago}
                    # If new is ON more recently, it wins
                    elif state == 'on' and seconds_ago < existing['seconds_ago']:
                        room_states[room] = {'state': state, 'seconds_ago': seconds_ago}

            except Exception as e:
                logger.warning(f"Error parsing timestamp for {room}: {e}")
                if room not in room_states:
                    room_states[room] = {'state': state, 'seconds_ago': float('inf')}

        # Build summary from deduplicated rooms
        active_rooms = []
        recent_rooms = []  # Motion in last 5 minutes
        all_room_status = []

        for room, data in sorted(room_states.items()):
            state = data['state']
            seconds_ago = data['seconds_ago']
            minutes_ago = int(seconds_ago / 60)

            if state == 'on':
                active_rooms.append(room)
            elif minutes_ago < 5:
                recent_rooms.append(room)
            # We only care about active and recent for occupancy estimation
            # Historical motion (> 5 min ago) is ignored

        # Build separate sections for active vs recent motion
        motion_sections = []
        if active_rooms:
            motion_sections.append(f"CURRENTLY ACTIVE (motion detected now): {', '.join(sorted(active_rooms))}")
        else:
            motion_sections.append("CURRENTLY ACTIVE: None")

        if recent_rooms:
            motion_sections.append(f"RECENT MOTION (within last 5 minutes): {', '.join(sorted(recent_rooms))}")

        motion_summary = "\n".join(motion_sections)

        # Build the LLM prompt
        layout_section = ""
        if house_layout:
            layout_section = f"""
HOUSE LAYOUT:
{house_layout}
"""

        occupancy_prompt = f"""Estimate how many people are likely home based on motion sensor data.
{layout_section}
{motion_summary}

CURRENT TIME: {now.strftime('%I:%M %p')}

IMPORTANT RULES:
- CURRENTLY ACTIVE means the sensor is detecting motion RIGHT NOW
- RECENT MOTION means motion stopped within the last 5 minutes
- Multiple sensors in the same room = still 1 person in that room
- Adjacent rooms (like bedroom + bathroom) could be the same person moving

HOW TO COUNT:
- 1 active room = at least 1 person
- Multiple DISTANT active rooms at the same time = multiple people
- Recent motion + 1 active room = probably just 1 person who moved
- No active motion = could be empty OR people are still/sleeping

Respond with a brief estimate (1-2 sentences) stating:
1. How many people you estimate are home
2. Your confidence level (low/medium/high)

Do NOT mention rooms that have no current or recent motion."""

        # Get model from database or use fallback
        admin_client = get_admin_client()
        config = await admin_client.get_component_model("smart_home_control")
        model = config.get("model_name") if config and config.get("enabled") else "llama3.1:8b"

        try:
            llm_response = await self.llm_router.generate(
                model=model,
                prompt=occupancy_prompt,
                temperature=0.3,
                max_tokens=200
            )

            response_text = llm_response.get("response", "").strip()
            logger.info(f"Occupancy estimation: {response_text[:100]}...")

            # Validate response is a complete sentence, not just a number like "1."
            # LLM sometimes returns just the count instead of a full response
            is_valid = (
                len(response_text) >= 15 and  # Minimum reasonable sentence length
                not response_text.replace('.', '').replace(' ', '').isdigit() and  # Not just a number
                any(c.isalpha() for c in response_text)  # Has at least some letters
            )

            if is_valid:
                return response_text
            else:
                logger.warning(f"LLM returned invalid occupancy response: '{response_text}', using fallback")
                # Fall through to fallback logic below

        except Exception as e:
            logger.error(f"LLM occupancy reasoning error: {e}")

        # Fallback to simple response (both for LLM error and invalid response)
        if active_rooms:
            return f"Based on current motion, there's at least 1 person home. Motion detected in: {', '.join(active_rooms)}."
        elif recent_rooms:
            return f"Someone was recently in {', '.join(recent_rooms)}, but no current motion detected."
        else:
            return "No recent motion detected. The house may be empty or everyone is stationary."

    async def _handle_media_intent(self, action: str, parameters: Dict, original_query: str = None, room: str = None, ha_client = None) -> str:
        """Handle media player queries and control"""
        import httpx
        import logging
        from music_handler import get_room_configs, get_room_display_names
        logger = logging.getLogger(__name__)

        jarvis_url = "http://localhost:3001"
        query_lower = (original_query or "").lower()

        # Handle TV power on/off commands
        if action in ['turn_on', 'turn_off'] and ha_client:
            try:
                # Get all media_player entities
                all_entities = await self.entity_manager.get_entities()
                media_players = {k: v for k, v in all_entities.items() if k.startswith('media_player.')}

                # Find TV entities (shield, nvidia, TV, etc.)
                tv_patterns = ['shield', 'tv', 'nvidia', 'television', 'roku', 'fire', 'chromecast']
                target_tvs = []
                for entity_id, state_data in media_players.items():
                    friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id).lower()
                    entity_lower = entity_id.lower()
                    if any(pattern in friendly_name or pattern in entity_lower for pattern in tv_patterns):
                        target_tvs.append((entity_id, state_data))

                if not target_tvs:
                    return "I couldn't find any TVs in the home automation system."

                # Execute turn_on or turn_off
                service = "turn_on" if action == "turn_on" else "turn_off"
                await asyncio.gather(*[
                    ha_client.call_service("media_player", service, {"entity_id": entity_id})
                    for entity_id, _ in target_tvs
                ])
                tv_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_tvs])
                action_word = "turned on" if action == "turn_on" else "turned off"
                return f"Done! I've {action_word} {tv_names}."

            except Exception as e:
                logger.error(f"TV control error: {e}")
                return "I couldn't control the TV right now."

        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                response = await client.get(f"{jarvis_url}/api/media")
                if response.status_code == 200:
                    data = response.json()
                    players = data.get('players', [])

                    # Filter to Music Assistant entities only (avoid duplicates)
                    # Music Assistant entities don't have numbered suffixes like _2, _3
                    ma_players = []
                    seen_names = set()
                    for p in players:
                        entity_id = p.get('entity_id', '')
                        name = p.get('name', '')
                        # Skip if we've seen this name or if it's a numbered duplicate
                        if name in seen_names:
                            continue
                        # Prefer entities without numbered suffixes (native MA entities)
                        if entity_id.endswith('_2') or entity_id.endswith('_3'):
                            continue
                        seen_names.add(name)
                        ma_players.append(p)

                    playing = [p for p in ma_players if p['state'] == 'playing']

                    if 'what' in query_lower or 'playing' in query_lower or 'status' in query_lower:
                        if playing:
                            descriptions = []
                            for p in playing[:3]:
                                desc = p['name']
                                if p.get('media_title'):
                                    desc += f" is playing {p['media_title']}"
                                    if p.get('media_artist'):
                                        desc += f" by {p['media_artist']}"
                                descriptions.append(desc)
                            return ". ".join(descriptions) + "."
                        return "Nothing is playing right now."

                    # List available rooms from admin config (single source of truth)
                    room_configs = await get_room_configs()
                    available_rooms = get_room_display_names(room_configs)
                    return f"Available rooms: {', '.join(available_rooms)}."

        except Exception as e:
            logger.error(f"Media query error: {e}")
            return "I couldn't check media player status right now."

        return "I'm not sure what media information you need."

    async def _handle_bed_warmer_intent(self, action: str, parameters: Dict, ha_client, original_query: str = None) -> str:
        """Handle bed warmer / mattress pad control (Sunbeam via Tuya)

        Supports:
        - Default warming at level 1 (low)
        - Specific levels 1-10 or percentages (10-100%)
        - Dual-side control with different levels per side
        - Relative adjustments (warmer/cooler)
        """
        import logging
        logger = logging.getLogger(__name__)

        # Sunbeam mattress pad entities
        LEVEL_LEFT = "select.sunbeam_bedding_dual_s2_level_1"
        LEVEL_RIGHT = "select.sunbeam_bedding_dual_s2_level_2"
        POWER_MAIN = "switch.sunbeam_bedding_dual_s2_power"
        POWER_SIDE_A = "switch.sunbeam_bedding_dual_s2_side_a_power"
        POWER_SIDE_B = "switch.sunbeam_bedding_dual_s2_side_b_power"

        side = parameters.get('side', 'both')
        level = parameters.get('level', 1)  # Default to level 1 (low)
        left_level = parameters.get('left_level')
        right_level = parameters.get('right_level')

        try:
            if action == "turn_off":
                # Turn off the mattress pad
                await ha_client.call_service("switch", "turn_off", {"entity_id": POWER_MAIN})
                return "Bed warmer turned off."

            elif action in ["warm_bed", "set_level", "turn_on"]:
                # Turn on and set level (set_level used for follow-up like "level 2")

                if side == "dual" and left_level is not None and right_level is not None:
                    # Dual-side mode: different levels for each side
                    await ha_client.call_service("switch", "turn_on", {"entity_id": POWER_MAIN})
                    await asyncio.gather(
                        ha_client.call_service("select", "select_option", {"entity_id": LEVEL_LEFT, "option": f"level_{left_level}"}),
                        ha_client.call_service("select", "select_option", {"entity_id": LEVEL_RIGHT, "option": f"level_{right_level}"})
                    )
                    return f"Warming the bed: left side at level {left_level}, right side at level {right_level}."

                elif side == "both":
                    # Turn on main power and set both sides to same level
                    level_value = f"level_{level}"
                    await ha_client.call_service("switch", "turn_on", {"entity_id": POWER_MAIN})
                    await asyncio.gather(
                        ha_client.call_service("select", "select_option", {"entity_id": LEVEL_LEFT, "option": level_value}),
                        ha_client.call_service("select", "select_option", {"entity_id": LEVEL_RIGHT, "option": level_value})
                    )
                    if action == "set_level":
                        return f"Set bed warmer to level {level} on both sides."
                    return f"Warming the bed on both sides at level {level}."

                elif side == "left":
                    level_value = f"level_{level}"
                    await ha_client.call_service("switch", "turn_on", {"entity_id": POWER_SIDE_A})
                    await ha_client.call_service("select", "select_option", {"entity_id": LEVEL_LEFT, "option": level_value})
                    if action == "set_level":
                        return f"Set left side to level {level}."
                    return f"Warming the left side at level {level}."

                elif side == "right":
                    level_value = f"level_{level}"
                    await ha_client.call_service("switch", "turn_on", {"entity_id": POWER_SIDE_B})
                    await ha_client.call_service("select", "select_option", {"entity_id": LEVEL_RIGHT, "option": level_value})
                    if action == "set_level":
                        return f"Set right side to level {level}."
                    return f"Warming the right side at level {level}."

            elif action == "increase":
                # Get current levels and increase
                try:
                    if side in ["both", "left"]:
                        left_state = await ha_client.get_state(LEVEL_LEFT)
                        if left_state:
                            current_left = int(left_state.get('state', 'level_3').replace('level_', ''))
                            new_left = min(10, current_left + 1)
                            await ha_client.call_service("select", "select_option", {"entity_id": LEVEL_LEFT, "option": f"level_{new_left}"})

                    if side in ["both", "right"]:
                        right_state = await ha_client.get_state(LEVEL_RIGHT)
                        if right_state:
                            current_right = int(right_state.get('state', 'level_3').replace('level_', ''))
                            new_right = min(10, current_right + 1)
                            await ha_client.call_service("select", "select_option", {"entity_id": LEVEL_RIGHT, "option": f"level_{new_right}"})

                    return "Increased the bed warmer temperature."
                except Exception as e:
                    logger.error(f"Error increasing bed warmer: {e}")
                    return "I couldn't increase the bed warmer temperature."

            elif action == "decrease":
                # Get current levels and decrease
                try:
                    if side in ["both", "left"]:
                        left_state = await ha_client.get_state(LEVEL_LEFT)
                        if left_state:
                            current_left = int(left_state.get('state', 'level_3').replace('level_', ''))
                            new_left = max(1, current_left - 1)
                            await ha_client.call_service("select", "select_option", {"entity_id": LEVEL_LEFT, "option": f"level_{new_left}"})

                    if side in ["both", "right"]:
                        right_state = await ha_client.get_state(LEVEL_RIGHT)
                        if right_state:
                            current_right = int(right_state.get('state', 'level_3').replace('level_', ''))
                            new_right = max(1, current_right - 1)
                            await ha_client.call_service("select", "select_option", {"entity_id": LEVEL_RIGHT, "option": f"level_{new_right}"})

                    return "Decreased the bed warmer temperature."
                except Exception as e:
                    logger.error(f"Error decreasing bed warmer: {e}")
                    return "I couldn't decrease the bed warmer temperature."

        except Exception as e:
            logger.error(f"Bed warmer control error: {e}")
            return "I couldn't control the bed warmer right now."

        return "I'm not sure what you want to do with the bed warmer."

    async def _handle_light_status_query(self, room: str, ha_client, original_query: str = None) -> str:
        """
        Round 16: Handle light status queries like "any lights left on?"
        Returns which lights are currently on in the specified room/area.
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Get all light entities via entity manager
            all_entities = await self.entity_manager.get_entities()
            lights = {k: v for k, v in all_entities.items() if k.startswith('light.')}

            if not lights:
                return "I couldn't find any lights in the home automation system."

            # Filter by room if specified
            lights_on = []
            room_lower = (room or '').lower()

            for entity_id, state_data in lights.items():
                if state_data.get('state') == 'on':
                    friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id.split('.')[-1].replace('_', ' '))
                    entity_lower = entity_id.lower()
                    friendly_lower = friendly_name.lower()

                    # If room specified, filter by room
                    if room:
                        # Room matching - check both entity_id and friendly_name
                        room_variations = [room_lower]
                        if room_lower == 'upstairs':
                            room_variations.extend(['second floor', '2nd floor', 'alpha', 'beta', 'master'])
                        elif room_lower == 'downstairs':
                            room_variations.extend(['first floor', '1st floor', 'living', 'kitchen', 'dining'])

                        if any(r in entity_lower or r in friendly_lower for r in room_variations):
                            lights_on.append(friendly_name)
                    else:
                        lights_on.append(friendly_name)

            if not lights_on:
                if room:
                    return f"No lights are currently on {room}."
                else:
                    return "No lights are currently on anywhere in the house."

            # Format response
            if len(lights_on) == 1:
                return f"The {lights_on[0]} is on."
            elif len(lights_on) <= 5:
                light_list = ", ".join(lights_on[:-1]) + f" and {lights_on[-1]}"
                return f"The following lights are on: {light_list}."
            else:
                # Too many to list, summarize
                return f"{len(lights_on)} lights are currently on, including {', '.join(lights_on[:3])} and {len(lights_on) - 3} more."

        except Exception as e:
            logger.error(f"Light status query error: {e}")
            return "I couldn't check the light status right now."

    async def _handle_lock_intent(self, action: str, room: str, ha_client, original_query: str = None) -> str:
        """Handle lock control and status queries"""
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Get all lock entities from HA
            all_entities = await self.entity_manager.get_entities()
            locks = {k: v for k, v in all_entities.items() if k.startswith('lock.')}

            if not locks:
                return "I couldn't find any locks in the home automation system."

            # Filter by room if specified
            target_locks = []
            query_lower = (original_query or "").lower()

            # Special handling for "all_doors" - include all locks
            if room and room.lower() in ['all_doors', 'all doors', 'all']:
                logger.info(f"Lock intent: targeting all doors ({len(locks)} locks)")
                target_locks = list(locks.items())
            else:
                for entity_id, state_data in locks.items():
                    friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id).lower()
                    entity_lower = entity_id.lower()

                    # Match by room name or lock name
                    if room:
                        room_lower = room.lower().replace('_', ' ')
                        if room_lower in friendly_name or room_lower in entity_lower:
                            target_locks.append((entity_id, state_data))
                    elif 'front' in query_lower and ('front' in friendly_name or 'front' in entity_lower):
                        target_locks.append((entity_id, state_data))
                    elif 'back' in query_lower and ('back' in friendly_name or 'back' in entity_lower):
                        target_locks.append((entity_id, state_data))
                    elif not room:
                        # No specific lock mentioned, include all
                        target_locks.append((entity_id, state_data))

            if not target_locks:
                return f"I couldn't find a lock matching '{room or 'your request'}'."

            if action == "get_status":
                # Report lock status
                statuses = []
                for entity_id, state_data in target_locks:
                    friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id.split('.')[-1].replace('_', ' '))
                    state = state_data.get('state', 'unknown')
                    statuses.append(f"{friendly_name} is {state}")
                return ". ".join(statuses) + "."

            elif action == "lock":
                # Lock the door(s)
                await asyncio.gather(*[
                    ha_client.call_service("lock", "lock", {"entity_id": entity_id})
                    for entity_id, _ in target_locks
                ])
                # If locking all doors, say "all doors" instead of listing them
                if len(target_locks) > 2:
                    return f"Done! I've locked all {len(target_locks)} doors."
                else:
                    lock_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_locks])
                    return f"Done! I've locked {lock_names}."

            elif action == "unlock":
                # Unlock the door(s)
                await asyncio.gather(*[
                    ha_client.call_service("lock", "unlock", {"entity_id": entity_id})
                    for entity_id, _ in target_locks
                ])
                # If unlocking all doors, say "all doors" instead of listing them
                if len(target_locks) > 2:
                    return f"Done! I've unlocked all {len(target_locks)} doors."
                else:
                    lock_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_locks])
                    return f"Done! I've unlocked {lock_names}."

            else:
                return "I'm not sure what you want to do with the lock."

        except Exception as e:
            logger.error(f"Lock control error: {e}")
            return "I couldn't control the lock right now."

    async def _handle_fan_intent(self, action: str, room: str, ha_client, original_query: str = None) -> str:
        """Handle fan control commands"""
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Get all fan entities from HA
            all_entities = await self.entity_manager.get_entities()
            fans = {k: v for k, v in all_entities.items() if k.startswith('fan.')}

            if not fans:
                return "I couldn't find any fans in the home automation system."

            # Filter by room if specified
            target_fans = []
            query_lower = (original_query or "").lower()

            for entity_id, state_data in fans.items():
                friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id).lower()
                entity_lower = entity_id.lower()

                # Match by room name
                if room:
                    room_lower = room.lower().replace('_', ' ')
                    if room_lower in friendly_name or room_lower in entity_lower:
                        target_fans.append((entity_id, state_data))
                else:
                    # Try to match from query
                    if 'ceiling' in query_lower:
                        if 'ceiling' in friendly_name or 'ceiling' in entity_lower:
                            target_fans.append((entity_id, state_data))
                    elif 'living' in query_lower and 'living' in friendly_name:
                        target_fans.append((entity_id, state_data))
                    elif 'bedroom' in query_lower and 'bedroom' in friendly_name:
                        target_fans.append((entity_id, state_data))
                    else:
                        # Include all fans if no specific match
                        target_fans.append((entity_id, state_data))

            if not target_fans:
                return f"I couldn't find a fan matching '{room or 'your request'}'."

            if action == "get_status":
                # Report fan status
                statuses = []
                for entity_id, state_data in target_fans:
                    friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id.split('.')[-1].replace('_', ' '))
                    state = state_data.get('state', 'unknown')
                    statuses.append(f"{friendly_name} is {state}")
                return ". ".join(statuses) + "."

            elif action == "turn_on":
                await asyncio.gather(*[
                    ha_client.call_service("fan", "turn_on", {"entity_id": entity_id})
                    for entity_id, _ in target_fans
                ])
                fan_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_fans])
                return f"Done! I've turned on {fan_names}."

            elif action == "turn_off":
                await asyncio.gather(*[
                    ha_client.call_service("fan", "turn_off", {"entity_id": entity_id})
                    for entity_id, _ in target_fans
                ])
                fan_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_fans])
                return f"Done! I've turned off {fan_names}."

            else:
                return "I'm not sure what you want to do with the fan."

        except Exception as e:
            logger.error(f"Fan control error: {e}")
            return "I couldn't control the fan right now."

    async def _handle_cover_intent(self, action: str, room: str, ha_client, original_query: str = None) -> str:
        """Handle cover/garage door control commands"""
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Get all cover entities from HA
            all_entities = await self.entity_manager.get_entities()
            covers = {k: v for k, v in all_entities.items() if k.startswith('cover.')}

            if not covers:
                return "I couldn't find any garage doors or covers in the home automation system."

            # Filter by room if specified
            target_covers = []
            query_lower = (original_query or "").lower()

            for entity_id, state_data in covers.items():
                friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id).lower()
                entity_lower = entity_id.lower()

                # Match by room name or keyword
                if room:
                    room_lower = room.lower().replace('_', ' ')
                    if room_lower in friendly_name or room_lower in entity_lower:
                        target_covers.append((entity_id, state_data))
                elif 'garage' in query_lower:
                    if 'garage' in friendly_name or 'garage' in entity_lower:
                        target_covers.append((entity_id, state_data))
                else:
                    target_covers.append((entity_id, state_data))

            if not target_covers:
                return f"I couldn't find a garage door or cover matching '{room or 'your request'}'."

            if action == "get_status":
                statuses = []
                for entity_id, state_data in target_covers:
                    friendly_name = state_data.get('attributes', {}).get('friendly_name', entity_id.split('.')[-1].replace('_', ' '))
                    state = state_data.get('state', 'unknown')
                    state_desc = "open" if state == "open" else "closed" if state == "closed" else state
                    statuses.append(f"The {friendly_name} is {state_desc}")
                return ". ".join(statuses) + "."

            elif action == "open":
                await asyncio.gather(*[
                    ha_client.call_service("cover", "open_cover", {"entity_id": entity_id})
                    for entity_id, _ in target_covers
                ])
                cover_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_covers])
                return f"Done! I've opened the {cover_names}."

            elif action == "close":
                await asyncio.gather(*[
                    ha_client.call_service("cover", "close_cover", {"entity_id": entity_id})
                    for entity_id, _ in target_covers
                ])
                cover_names = ', '.join([s.get('attributes', {}).get('friendly_name', e.split('.')[-1].replace('_', ' ')) for e, s in target_covers])
                return f"Done! I've closed the {cover_names}."

            else:
                return "I'm not sure what you want to do with the garage door."

        except Exception as e:
            logger.error(f"Cover control error: {e}")
            return "I couldn't control the garage door right now."

    async def _handle_scene_intent(self, action: str, parameters: Dict, ha_client, original_query: str = None) -> str:
        """Handle scene and routine activation commands"""
        import logging
        logger = logging.getLogger(__name__)

        entity_id = parameters.get('entity_id', '')
        if not entity_id:
            return "I couldn't determine which scene or routine to activate."

        query_lower = (original_query or "").lower()

        try:
            # Determine the domain (scene or script)
            if entity_id.startswith('scene.'):
                domain = 'scene'
                service = 'turn_on'
                entity_type = 'scene'
            elif entity_id.startswith('script.'):
                domain = 'script'
                service = 'turn_on'
                entity_type = 'routine'
            else:
                return f"Unknown scene or routine: {entity_id}"

            # Try to activate the scene/script
            try:
                await ha_client.call_service(domain, service, {"entity_id": entity_id})

                # Generate a friendly response based on the scene/routine
                scene_name = entity_id.split('.')[-1].replace('_', ' ').title()

                # Custom responses based on common patterns
                if 'movie' in entity_id:
                    return "Done! Movie mode activated. Dimming lights and setting the mood."
                elif 'good_night' in entity_id or 'goodnight' in entity_id:
                    return "Good night! I've turned off the lights and locked up for you."
                elif 'good_morning' in entity_id:
                    return "Good morning! I've started your morning routine."
                elif 'leaving' in entity_id:
                    return "Goodbye! I've set everything for while you're away."
                elif 'arriving' in entity_id:
                    return "Welcome home! I've turned on the lights and adjusted the temperature."
                elif 'romantic' in entity_id:
                    return "Done! Romantic mode activated. Dimming lights for ambiance."
                elif 'relax' in entity_id:
                    return "Done! Relaxation mode activated. Dimming lights and creating a calm atmosphere."
                elif 'party' in entity_id:
                    return "Party time! Lights are set for celebration mode."
                else:
                    return f"Done! I've activated the {scene_name} {entity_type}."

            except Exception as e:
                # Scene/script doesn't exist - try a fallback
                logger.warning(f"Scene/script {entity_id} failed: {e}")

                # Provide fallback behavior based on what was requested
                if 'movie' in query_lower:
                    # Dim living room lights as a fallback
                    try:
                        await ha_client.call_service("light", "turn_on", {
                            "entity_id": "light.living_room_all",
                            "brightness_pct": 20
                        })
                        return "Movie mode ready! I've dimmed the living room lights."
                    except:
                        pass
                elif 'good night' in query_lower or 'goodnight' in query_lower:
                    # Turn off all lights as a fallback
                    try:
                        await ha_client.call_service("light", "turn_off", {"entity_id": "all"})
                        return "Good night! I've turned off the lights."
                    except:
                        pass
                elif 'good morning' in query_lower:
                    # Turn on lights as a fallback
                    try:
                        await ha_client.call_service("light", "turn_on", {
                            "entity_id": "light.office_all",
                            "brightness_pct": 100
                        })
                        return "Good morning! I've turned on the lights."
                    except:
                        pass
                elif 'leaving' in query_lower or 'goodbye' in query_lower:
                    # Turn off all lights and lock doors as a fallback
                    try:
                        await ha_client.call_service("light", "turn_off", {"entity_id": "all"})
                        await ha_client.call_service("lock", "lock", {"entity_id": "all"})
                        return "Goodbye! I've turned off the lights and locked the doors."
                    except:
                        pass
                elif 'home' in query_lower:
                    # Turn on some lights as a fallback
                    try:
                        await ha_client.call_service("light", "turn_on", {
                            "entity_id": "light.living_room_all",
                            "brightness_pct": 80
                        })
                        return "Welcome home! I've turned on the lights."
                    except:
                        pass

                return f"I tried to activate {scene_name}, but it may not be configured yet. I'll try a basic version."

        except Exception as e:
            logger.error(f"Scene activation error: {e}")
            return "I couldn't activate the scene or routine right now."

    async def _execute_whole_house_command(
        self, action: str, target_scope: str, parameters: Dict,
        intent: Dict, ha_client, original_query: str
    ) -> str:
        """
        Execute a command across all rooms in the house.
        Handles Christmas themes, whole-house color changes, etc.
        Supports exclusions like "all lights except bedroom".
        All HA API calls are parallelized for faster response.
        """
        import logging
        logger = logging.getLogger(__name__)

        # Get all available light groups from Home Assistant
        all_light_groups = await self.entity_manager.get_all_light_groups()

        if not all_light_groups:
            return "I couldn't find any light groups in the house."

        # Handle exclusions - filter out excluded rooms
        excluded_rooms = intent.get('excluded_rooms', []) or []
        if excluded_rooms:
            excluded_lower = [r.lower().replace('_', ' ') for r in excluded_rooms]
            filtered_groups = []
            excluded_names = []
            for group in all_light_groups:
                group_name = group.get('friendly_name', '').lower()
                entity_id = group.get('entity_id', '').lower()
                # Check if this group should be excluded
                should_exclude = any(
                    exc in group_name or exc in entity_id
                    for exc in excluded_lower
                )
                if should_exclude:
                    excluded_names.append(group.get('friendly_name', 'Unknown'))
                else:
                    filtered_groups.append(group)
            all_light_groups = filtered_groups
            logger.info(f"Excluding rooms: {excluded_names}")

        # Parse the color scheme from the query/intent
        color_description = intent.get('color_description', '')
        hs_colors = parameters.get('hs_colors', [])
        query_lower = original_query.lower() if original_query else ''

        # Detect Christmas theme
        is_christmas = 'christmas' in query_lower or ('red' in query_lower and 'green' in query_lower)
        wants_white_accent = 'white' in query_lower and ('visibility' in query_lower or 'couple' in query_lower or 'some' in query_lower)

        # Collect all tasks first, then execute in parallel
        tasks = []
        global_light_index = 0

        for group in all_light_groups:
            group_name = group.get('friendly_name', 'Unknown')
            members = group.get('members', [])

            if not members:
                # If no members, use the group entity itself
                members = [group.get('entity_id')]

            if action == "turn_on":
                for light in members:
                    tasks.append(ha_client.call_service("light", "turn_on", {"entity_id": light}))

            elif action == "turn_off":
                for light in members:
                    tasks.append(ha_client.call_service("light", "turn_off", {"entity_id": light}))

            elif action == "set_color":
                if is_christmas:
                    # Christmas theme: red and green with optional white accents
                    red_hue, red_sat = 0, 100
                    green_hue, green_sat = 120, 100
                    white_hue, white_sat = 0, 0  # White is 0 saturation

                    for i, light in enumerate(members):
                        if wants_white_accent and len(members) > 2 and i == len(members) // 2:
                            # Put a white light in the middle of each room for visibility
                            tasks.append(ha_client.call_service(
                                "light", "turn_on",
                                {"entity_id": light, "hs_color": [white_hue, white_sat], "brightness": 255}
                            ))
                        elif global_light_index % 2 == 0:
                            # Red lights on even global indices
                            tasks.append(ha_client.call_service(
                                "light", "turn_on",
                                {"entity_id": light, "hs_color": [red_hue, red_sat], "brightness": 255}
                            ))
                        else:
                            # Green lights on odd global indices
                            tasks.append(ha_client.call_service(
                                "light", "turn_on",
                                {"entity_id": light, "hs_color": [green_hue, green_sat], "brightness": 255}
                            ))
                        global_light_index += 1

                elif hs_colors:
                    # Use provided colors from LLM
                    for i, light in enumerate(members):
                        color_idx = global_light_index % len(hs_colors)
                        hue, sat = hs_colors[color_idx]
                        tasks.append(ha_client.call_service(
                            "light", "turn_on",
                            {"entity_id": light, "hs_color": [hue, sat], "brightness": 255}
                        ))
                        global_light_index += 1
                else:
                    # Default to white if no colors specified
                    for light in members:
                        tasks.append(ha_client.call_service(
                            "light", "turn_on",
                            {"entity_id": light, "brightness": 255}
                        ))

        # Execute all HA API calls in parallel
        if tasks:
            await asyncio.gather(*tasks)

        # Return contextual response for voice output
        room_count = len(all_light_groups)
        excluded_info = f", except {', '.join(excluded_rooms)}" if excluded_rooms else ""

        if action == "turn_on":
            return f"Done! I've turned on lights in {room_count} rooms{excluded_info}."
        elif action == "turn_off":
            return f"Done! I've turned off lights in {room_count} rooms{excluded_info}."
        elif action == "set_color":
            color_desc = color_description or "the colors"
            return f"Done! I've set {color_desc} across {room_count} rooms{excluded_info}."
        else:
            return f"Done! Updated lights in {room_count} rooms{excluded_info}."

    async def _execute_multi_room_command(
        self, rooms: list, action: str, target_scope: str, parameters: Dict,
        intent: Dict, ha_client, original_query: str
    ) -> str:
        """
        Execute a command across multiple specific rooms.
        Handles commands like "turn on kitchen and living room lights".
        All HA API calls are parallelized for faster response.
        """
        import structlog
        logger = structlog.get_logger(__name__)

        logger.info(f"Executing multi-room command: action={action}, rooms={rooms}")

        # Collect all lights to control from all rooms
        all_tasks = []
        all_light_names = []
        total_count = 0

        for room_name in rooms:
            # Find lights for this room
            light_matches = await self.entity_manager.find_lights_by_room(room_name)

            if not light_matches:
                logger.warning(f"No lights found for room {room_name} in multi-room command")
                continue

            # Get the primary light group for this room (largest group)
            light_matches_sorted = sorted(
                light_matches,
                key=lambda x: len(x.get('members', [])),
                reverse=True
            )
            light_group = light_matches_sorted[0]
            members = light_group.get('members', [])
            group_name = light_group.get('friendly_name', room_name)

            if not members:
                # If no members, use the group entity itself
                members = [light_group.get('entity_id')]

            # Queue up tasks for this room
            for light in members:
                if action == "turn_on":
                    all_tasks.append(ha_client.call_service("light", "turn_on", {"entity_id": light}))
                elif action == "turn_off":
                    all_tasks.append(ha_client.call_service("light", "turn_off", {"entity_id": light}))

            all_light_names.append(group_name)
            total_count += len(members)

        # Execute all tasks in parallel
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)

        # Build response
        room_list = ' and '.join(all_light_names)
        if action == "turn_on":
            return f"Done! I've turned on the {room_list} lights."
        elif action == "turn_off":
            return f"Done! I've turned off the {room_list} lights."
        else:
            return f"Done! I've updated the {room_list} lights."

    async def _execute_room_group_command(
        self, room_group: Dict, action: str, target_scope: str, parameters: Dict,
        intent: Dict, ha_client, original_query: str
    ) -> str:
        """
        Execute a command across all rooms in a room group.
        Handles commands like "turn on the first floor lights" where first floor
        includes living room, dining room, and kitchen.
        All HA API calls are parallelized for faster response.
        """
        import structlog
        logger = structlog.get_logger(__name__)

        group_name = room_group.get('display_name', room_group.get('name', 'Unknown'))
        members = room_group.get('members', [])

        if not members:
            return f"The room group '{group_name}' has no rooms configured."

        # Parse color parameters
        color_description = intent.get('color_description', '')
        hs_colors = parameters.get('hs_colors', [])
        query_lower = original_query.lower() if original_query else ''

        # Detect special themes
        is_christmas = 'christmas' in query_lower or ('red' in query_lower and 'green' in query_lower)

        # Step 1: Find lights for all rooms in parallel
        async def get_room_lights(member):
            room_name = member.get('room_name')
            if not room_name:
                return None
            light_matches = await self.entity_manager.find_lights_by_room(room_name)
            if not light_matches:
                logger.warning(f"No lights found for room {room_name} in group {group_name}")
                return None
            # Sort and get primary group
            light_matches_sorted = sorted(
                light_matches,
                key=lambda x: len(x.get('members', [])),
                reverse=True
            )
            light_group = light_matches_sorted[0]
            members_lights = light_group.get('members', [])
            if not members_lights:
                members_lights = [light_group.get('entity_id')]
            return members_lights

        # Get all room lights in parallel
        room_lights_results = await asyncio.gather(*[get_room_lights(m) for m in members])

        # Step 2: Collect all HA tasks
        tasks = []
        light_index = 0  # Global index for color cycling

        for members_lights in room_lights_results:
            if not members_lights:
                continue

            if action == "turn_on":
                for light in members_lights:
                    tasks.append(ha_client.call_service("light", "turn_on", {"entity_id": light}))

            elif action == "turn_off":
                for light in members_lights:
                    tasks.append(ha_client.call_service("light", "turn_off", {"entity_id": light}))

            elif action == "set_color":
                if is_christmas:
                    # Christmas theme: alternating red and green
                    red_hue, red_sat = 0, 100
                    green_hue, green_sat = 120, 100

                    for light in members_lights:
                        if light_index % 2 == 0:
                            tasks.append(ha_client.call_service(
                                "light", "turn_on",
                                {"entity_id": light, "hs_color": [red_hue, red_sat], "brightness": 255}
                            ))
                        else:
                            tasks.append(ha_client.call_service(
                                "light", "turn_on",
                                {"entity_id": light, "hs_color": [green_hue, green_sat], "brightness": 255}
                            ))
                        light_index += 1

                elif hs_colors:
                    # Use provided colors, cycling through them
                    for light in members_lights:
                        color_idx = light_index % len(hs_colors)
                        hue, sat = hs_colors[color_idx]
                        tasks.append(ha_client.call_service(
                            "light", "turn_on",
                            {"entity_id": light, "hs_color": [hue, sat], "brightness": 255}
                        ))
                        light_index += 1

                else:
                    # Default to white if no colors specified
                    for light in members_lights:
                        tasks.append(ha_client.call_service(
                            "light", "turn_on",
                            {"entity_id": light, "brightness": 255}
                        ))

        # Execute all HA API calls in parallel
        if tasks:
            await asyncio.gather(*tasks)

        # Return contextual response for voice output
        room_count = len(members)
        if action == "turn_on":
            return f"Done! I've turned on lights on the {group_name}."
        elif action == "turn_off":
            return f"Done! I've turned off lights on the {group_name}."
        elif action == "set_color":
            color_desc = color_description or "the colors"
            return f"Done! I've set {color_desc} on the {group_name}."
        else:
            return f"Done! Updated {group_name} lights."

    async def _extract_motion_control_intent(self, query: str, device_room: str = None) -> Dict:
        """
        Use LLM to extract motion control intent from natural language.
        Controls Node-RED motion flow variables for bedrooms and office.

        Rooms with motion control: office, alpha, beta, master_bedroom

        Variables per room:
        - input_boolean.<room>_leave_lights_on: Keep lights on after motion stops
        - input_boolean.<room>_leave_lights_off: Keep lights off (nap mode)
        - input_boolean.<room>_brightness_override: Use custom brightness
        - input_boolean.<room>_motion_disable: Disable motion detection entirely
        - input_number.<room>_leave_lights_on_minutes: Duration (5-480 min)
        - input_number.<room>_leave_lights_off_minutes: Duration (5-480 min)
        - input_number.<room>_override_brightness: Brightness % (1-100)
        """
        import logging
        logger = logging.getLogger(__name__)

        # Build room context
        room_context = ""
        if device_room and device_room not in ["unknown", "guest"]:
            room_context = f"\nDevice location: The user is speaking from the {device_room}. If no room is specified, use \"{device_room}\" as the room."

        motion_prompt = f"""Extract the motion control intent from this query. Return ONLY valid JSON.

Query: "{query}"{room_context}

CONTEXT: This controls smart lighting automation. Motion sensors detect presence and automatically turn lights on/off.
Users may want to override this behavior temporarily.

AVAILABLE ACTIONS:
- "leave_lights_on": Keep lights ON after motion stops (prevents auto-off). Expires when room becomes unoccupied.
- "leave_lights_off": Keep lights OFF even when motion detected (nap mode). Expires when room becomes unoccupied.
- "disable_motion": Completely disable motion-based lighting. Only physical switch works.
- "enable_motion": Re-enable motion detection and reset all overrides to normal behavior.
- "set_brightness_override": Lock brightness at a specific level when motion triggers lights.
- "keep_current_brightness": Lock brightness at current level (will query current brightness).

ROOMS WITH MOTION CONTROL: office, alpha, beta, master_bedroom
- "alpha" and "beta" are guest bedrooms
- "master_bedroom" can also be called "bedroom", "master", "my room"

Return JSON with this structure:
{{
    "device_type": "motion_control",
    "room": "room name or null",
    "action": "leave_lights_on|leave_lights_off|disable_motion|enable_motion|set_brightness_override|keep_current_brightness",
    "parameters": {{
        "duration_minutes": number or null (for leave_lights_on/off, default 60),
        "brightness_percent": number 1-100 or null (for brightness override)
    }},
    "reason": "brief user-friendly explanation of what will happen"
}}

Examples:
"leave the lights on for 2 hours" -> {{"device_type": "motion_control", "room": null, "action": "leave_lights_on", "parameters": {{"duration_minutes": 120}}, "reason": "Lights will stay on for 2 hours"}}
"keep the lights off, I'm taking a nap" -> {{"device_type": "motion_control", "room": null, "action": "leave_lights_off", "parameters": {{"duration_minutes": 180}}, "reason": "Lights will stay off for your nap"}}
"disable motion in the office" -> {{"device_type": "motion_control", "room": "office", "action": "disable_motion", "parameters": {{}}, "reason": "Motion detection disabled, use switch only"}}
"turn motion back on" -> {{"device_type": "motion_control", "room": null, "action": "enable_motion", "parameters": {{}}, "reason": "Motion detection resumed"}}
"keep the room at this brightness" -> {{"device_type": "motion_control", "room": null, "action": "keep_current_brightness", "parameters": {{}}, "reason": "Brightness locked at current level"}}
"set motion brightness to 50%" -> {{"device_type": "motion_control", "room": null, "action": "set_brightness_override", "parameters": {{"brightness_percent": 50}}, "reason": "Motion will set lights to 50%"}}
"don't turn the lights on when I walk in" -> {{"device_type": "motion_control", "room": null, "action": "leave_lights_off", "parameters": {{"duration_minutes": 60}}, "reason": "Lights will stay off for 1 hour"}}
"stop the lights from turning off" -> {{"device_type": "motion_control", "room": null, "action": "leave_lights_on", "parameters": {{"duration_minutes": 60}}, "reason": "Lights will stay on for 1 hour"}}

DURATION PARSING:
- "for X hours" -> X * 60 minutes
- "for X minutes" -> X minutes
- "for a while" / "for now" -> 60 minutes (default)
- "for the night" / "until morning" -> 480 minutes (8 hours)
- No duration specified -> 60 minutes (default)

Return ONLY the JSON, no other text."""

        # Get model from database or use fallback
        admin_client = get_admin_client()
        config = await admin_client.get_component_model("smart_home_control")
        model = config.get("model_name") if config and config.get("enabled") else "llama3.1:8b"

        llm_response = await self.llm_router.generate(
            model=model,
            prompt=motion_prompt,
            temperature=0.1,
            max_tokens=300
        )

        try:
            text = llm_response.get("response", "").strip()

            if '```json' in text:
                text = text.split('```json')[1].split('```')[0].strip()
            elif '```' in text:
                text = text.split('```')[1].split('```')[0].strip()

            intent = json.loads(text)
            logger.info(f"Motion control intent extracted: {intent}")
            return intent

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse motion control intent: {e}")
            # Fallback to basic parsing
            query_lower = query.lower()

            action = "leave_lights_on"  # default
            if any(x in query_lower for x in ['off', 'nap', 'sleep']):
                action = "leave_lights_off"
            elif any(x in query_lower for x in ['disable', 'stop motion', 'no motion']):
                action = "disable_motion"
            elif any(x in query_lower for x in ['enable', 'resume', 'reset', 'back on']):
                action = "enable_motion"
            elif any(x in query_lower for x in ['brightness', 'bright']):
                action = "keep_current_brightness"

            return {
                "device_type": "motion_control",
                "room": device_room,
                "action": action,
                "parameters": {"duration_minutes": 60},
                "reason": "Motion settings updated"
            }

    async def _handle_motion_control_intent(self, action: str, parameters: Dict, ha_client, room: str = None, original_query: str = None) -> str:
        """
        Handle motion control commands by setting Node-RED motion flow variables.

        Rooms with motion control helpers:
        - office: input_boolean.office_*, input_number.office_*
        - alpha: input_boolean.alpha_*, input_number.alpha_*
        - beta: input_boolean.beta_*, input_number.beta_*
        - master_bedroom: input_boolean.master_bedroom_*, input_number.master_bedroom_*
        """
        import logging
        logger = logging.getLogger(__name__)

        # Normalize room name to match HA entity naming
        room_map = {
            "office": "office",
            "alpha": "alpha",
            "beta": "beta",
            "master_bedroom": "master_bedroom",
            "master bedroom": "master_bedroom",
            "bedroom": "master_bedroom",
            "master": "master_bedroom",
            "my room": "master_bedroom",
            "my bedroom": "master_bedroom"
        }

        normalized_room = room_map.get(room.lower() if room else "", None)

        if not normalized_room:
            # Check if room has motion control
            valid_rooms = ["office", "alpha", "beta", "master_bedroom"]
            return f"Motion control is only available in: {', '.join(valid_rooms)}. Which room would you like to control?"

        # Build entity names
        leave_on_bool = f"input_boolean.{normalized_room}_leave_lights_on"
        leave_off_bool = f"input_boolean.{normalized_room}_leave_lights_off"
        brightness_override_bool = f"input_boolean.{normalized_room}_brightness_override"
        motion_disable_bool = f"input_boolean.{normalized_room}_motion_disable"
        leave_on_minutes = f"input_number.{normalized_room}_leave_lights_on_minutes"
        leave_off_minutes = f"input_number.{normalized_room}_leave_lights_off_minutes"
        override_brightness = f"input_number.{normalized_room}_override_brightness"
        light_entity = f"light.{normalized_room}"

        duration = parameters.get("duration_minutes", 60)
        brightness = parameters.get("brightness_percent")

        try:
            if action == "leave_lights_on":
                # Set duration first, then enable the boolean
                await ha_client.call_service(
                    "input_number", "set_value",
                    {"entity_id": leave_on_minutes, "value": min(480, max(5, duration))}
                )
                await ha_client.call_service(
                    "input_boolean", "turn_on",
                    {"entity_id": leave_on_bool}
                )
                hours = duration // 60
                mins = duration % 60
                time_str = f"{hours} hour{'s' if hours != 1 else ''}" if hours > 0 else ""
                if mins > 0:
                    time_str += f" {mins} minute{'s' if mins != 1 else ''}" if time_str else f"{mins} minute{'s' if mins != 1 else ''}"
                return f"Lights will stay on in the {normalized_room.replace('_', ' ')} for {time_str}."

            elif action == "leave_lights_off":
                # Set duration first, then enable the boolean
                await ha_client.call_service(
                    "input_number", "set_value",
                    {"entity_id": leave_off_minutes, "value": min(480, max(5, duration))}
                )
                await ha_client.call_service(
                    "input_boolean", "turn_on",
                    {"entity_id": leave_off_bool}
                )
                hours = duration // 60
                mins = duration % 60
                time_str = f"{hours} hour{'s' if hours != 1 else ''}" if hours > 0 else ""
                if mins > 0:
                    time_str += f" {mins} minute{'s' if mins != 1 else ''}" if time_str else f"{mins} minute{'s' if mins != 1 else ''}"
                return f"Lights will stay off in the {normalized_room.replace('_', ' ')} for {time_str}."

            elif action == "disable_motion":
                # Disable motion detection - only physical switch works
                await ha_client.call_service(
                    "input_boolean", "turn_on",
                    {"entity_id": motion_disable_bool}
                )
                return f"Motion detection disabled in the {normalized_room.replace('_', ' ')}. Use the light switch to control lights."

            elif action == "enable_motion":
                # Reset all overrides and enable motion
                await asyncio.gather(
                    ha_client.call_service("input_boolean", "turn_off", {"entity_id": motion_disable_bool}),
                    ha_client.call_service("input_boolean", "turn_off", {"entity_id": leave_on_bool}),
                    ha_client.call_service("input_boolean", "turn_off", {"entity_id": leave_off_bool}),
                    ha_client.call_service("input_boolean", "turn_off", {"entity_id": brightness_override_bool})
                )
                return f"Motion detection resumed in the {normalized_room.replace('_', ' ')}. All overrides cleared."

            elif action == "set_brightness_override":
                if brightness is None:
                    return "Please specify a brightness level, for example 'set motion brightness to 50 percent'."

                await ha_client.call_service(
                    "input_number", "set_value",
                    {"entity_id": override_brightness, "value": min(100, max(1, brightness))}
                )
                await ha_client.call_service(
                    "input_boolean", "turn_on",
                    {"entity_id": brightness_override_bool}
                )
                return f"Motion will set lights to {brightness}% brightness in the {normalized_room.replace('_', ' ')}."

            elif action == "keep_current_brightness":
                # Query current brightness from the light
                try:
                    light_state = await ha_client.get_state(light_entity)
                    if light_state and light_state.get("state") == "on":
                        current_brightness = light_state.get("attributes", {}).get("brightness", 255)
                        # Convert from 0-255 to 0-100 percent
                        brightness_percent = int((current_brightness / 255) * 100)

                        await ha_client.call_service(
                            "input_number", "set_value",
                            {"entity_id": override_brightness, "value": brightness_percent}
                        )
                        await ha_client.call_service(
                            "input_boolean", "turn_on",
                            {"entity_id": brightness_override_bool}
                        )
                        return f"Brightness locked at {brightness_percent}% in the {normalized_room.replace('_', ' ')}."
                    else:
                        return f"The {normalized_room.replace('_', ' ')} lights are off. Please turn them on first or specify a brightness level."
                except Exception as e:
                    logger.error(f"Error getting current brightness: {e}")
                    return "I couldn't determine the current brightness. Please specify a level like 'set motion brightness to 50 percent'."

            else:
                return "I didn't understand that motion control command. Try 'leave the lights on', 'disable motion', or 'reset motion settings'."

        except Exception as e:
            logger.error(f"Motion control error: {e}", exc_info=True)
            return f"I had trouble updating the motion settings. Please try again."