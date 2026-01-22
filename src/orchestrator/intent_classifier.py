"""
Enhanced Intent Classification System
Migrated from Jetson facade implementations with 43 iterations of refinement
"""

from typing import List, Dict, Any, Optional, Tuple
from enum import Enum
import re
import logging
import asyncio
from shared.admin_config import get_admin_client

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    """Comprehensive intent categories from Jetson facades"""
    CONTROL = "control"
    WEATHER = "weather"
    SPORTS = "sports"
    AIRPORTS = "airports"
    TRANSIT = "transit"
    EMERGENCY = "emergency"
    FOOD = "food"
    EVENTS = "events"
    LOCATION = "location"
    DIRECTIONS = "directions"  # Navigation and route planning
    GENERAL_INFO = "general_info"
    UNKNOWN = "unknown"


class IntentClassification:
    """Intent classification result with confidence and entities"""

    def __init__(self):
        self.category: IntentCategory = IntentCategory.UNKNOWN
        self.confidence: float = 0.0
        self.entities: Dict[str, Any] = {}
        self.requires_llm: bool = False
        self.cache_key: Optional[str] = None
        self.sub_intents: List['IntentClassification'] = []  # For multi-intent


class EnhancedIntentClassifier:
    """
    Sophisticated intent classification from Jetson facades.
    Implements layered approach: pattern matching -> LLM fallback -> entity extraction
    """

    def __init__(self):
        # Control patterns (from Baltimore facades)
        self.control_patterns = {
            "basic": ["turn on", "turn off", "toggle", "switch"],
            "dimming": ["dim", "brighten", "set brightness", "darker", "lighter"],
            "temperature": ["set temperature", "warmer", "cooler", "heat", "cool", "thermostat"],
            "scenes": ["scene", "mood", "movie mode", "dinner mode", "goodnight", "good morning"],
            "locks": ["lock", "unlock", "secure", "is locked"],
            "covers": ["open", "close", "raise", "lower", "blinds", "shades", "curtain"],
            "fans": ["fan on", "fan off", "fan speed", "ceiling fan"],
            "media": ["play", "pause", "stop", "volume", "mute", "next", "previous"],
            "colors": ["set to", "change to", "make it", "random colors", "different colors"],
            # Presence/occupancy queries (current and past)
            "presence": [
                "anyone home", "anybody home", "someone home", "who's home", "who is home",
                "is anyone", "is anybody", "is someone", "anyone there", "anybody there",
                "someone was home", "anyone was home", "anybody was home",
                "last time someone", "last time anyone", "last time somebody",
                "when was someone", "when was anyone", "when was the last",
                "last motion", "last movement", "last activity",
                "recent motion", "recent activity", "who was home", "who was here",
                "motion sensor", "occupancy"
            ]
        }

        # Color names for room+color detection (light control without "lights" keyword)
        self.color_entities = [
            "red", "blue", "green", "white", "yellow", "orange", "purple",
            "pink", "cyan", "magenta", "warm", "cool", "rainbow", "sunset",
            "random colors", "different colors", "christmas colors", "ocean"
        ]

        # Information patterns (comprehensive from Baltimore facades)
        self.info_patterns = {
            IntentCategory.SPORTS: [
                "ravens", "orioles", "score", "game", "won", "lost", "beat",
                "stadium", "m&t bank", "camden yards", "tickets", "playoff",
                "touchdown", "home run", "innings", "quarter", "halftime"
            ],
            IntentCategory.WEATHER: [
                "weather", "temperature", "rain", "snow", "forecast", "sunny",
                "humid", "cold", "hot", "storm", "wind", "cloudy", "precipitation",
                "feels like", "humidity", "pressure", "visibility", "uv index"
            ],
            IntentCategory.AIRPORTS: [
                "airport", "bwi", "dca", "iad", "dulles", "reagan", "philadelphia",
                "flight", "delayed", "gate", "terminal", "tsa", "departure",
                "arrival", "baggage", "airline", "boarding", "layover"
            ],
            IntentCategory.TRANSIT: [
                "bus", "train", "marc", "light rail", "metro", "subway",
                "uber", "lyft", "taxi", "water taxi", "circulator", "ride",
                "schedule", "route", "station", "transit", "commute",
                "amtrak", "acela", "northeast regional", "penn station", "nyc", "nyp"
            ],
            IntentCategory.FOOD: [
                "restaurant", "food", "eat", "hungry", "crab", "crab cake",
                "seafood", "coffee", "breakfast", "lunch", "dinner", "brunch",
                "reservation", "menu", "cuisine", "takeout", "delivery",
                "koco", "g&m", "pappas", "captain james", "thames street",
                "steakhouse", "steak house", "steak", "rated", "dining", "dine"
            ],
            IntentCategory.EMERGENCY: [
                "emergency", "911", "hospital", "doctor", "urgent", "medical",
                "police", "fire", "ambulance", "pharmacy", "clinic", "help",
                "poison", "injury", "accident", "crisis"
            ],
            IntentCategory.EVENTS: [
                "event", "concert", "show", "museum", "tonight", "festival",
                "weekend", "things to do", "entertainment", "tickets",
                "exhibition", "performance", "theater", "movie", "art"
            ],
            IntentCategory.LOCATION: [
                "where", "address", "how far", "distance",
                "neighborhood", "nearby", "closest",
                "miles", "minutes", "location", "map", "gps"
            ],
            IntentCategory.DIRECTIONS: [
                "directions", "route", "navigate", "how do i get to",
                "how to get to", "get me to", "take me to", "drive to",
                "walk to", "bike to", "transit to", "train to", "bus to",
                "fastest way", "best way", "quickest route", "shortest route",
                "driving directions", "walking directions", "biking directions",
                "stop for", "stop at", "food along the way", "gas along the way",
                "coffee along the way", "stop halfway", "with a stop"
            ]
        }

        # Complex indicators requiring LLM processing
        self.complex_indicators = [
            "explain", "why", "how does", "what is the difference",
            "should i", "recommend", "help me understand", "compare",
            "tell me about", "describe", "what are the pros and cons",
            "which is better", "analyze", "summarize", "elaborate"
        ]

        # Action keywords for entity extraction
        self.action_keywords = {
            "on": ["turn on", "switch on", "enable", "activate", "start"],
            "off": ["turn off", "switch off", "disable", "deactivate", "stop"],
            "increase": ["increase", "raise", "higher", "up", "more", "louder", "brighter"],
            "decrease": ["decrease", "lower", "down", "less", "quieter", "dimmer"],
            "set": ["set to", "change to", "adjust to", "make it"]
        }

        # Room/location entities
        self.room_entities = [
            "bedroom", "kitchen", "office", "living room", "bathroom",
            "master bedroom", "guest room", "hallway", "basement", "attic",
            "garage", "porch", "deck", "patio", "dining room", "den",
            "family room", "study", "library", "laundry room"
        ]

        # Device entities
        self.device_entities = [
            "lights", "light", "lamp", "fan", "tv", "television",
            "thermostat", "ac", "heater", "lock", "door", "blinds",
            "shades", "curtains", "speaker", "music", "radio",
            "outlet", "switch", "plug", "camera", "doorbell"
        ]

        # Database pattern loading (lazy-loaded on first use)
        self._db_patterns: Optional[Dict[IntentCategory, List[str]]] = None
        self._db_load_attempted = False
        self._db_load_task: Optional[asyncio.Task] = None

    def _ensure_db_loading_started(self):
        """
        Ensure database loading has been started (non-blocking).
        Creates a background task on first call to load patterns from database.
        """
        if not self._db_load_attempted:
            self._db_load_attempted = True
            try:
                # Try to get the current event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Create background task to load patterns
                    self._db_load_task = loop.create_task(self._load_db_patterns_async())
                    logger.info("Started background task to load intent patterns from database")
                else:
                    logger.info("No running event loop, using hardcoded intent patterns")
            except RuntimeError:
                logger.info("No event loop available, using hardcoded intent patterns")

    async def _load_db_patterns_async(self):
        """Background task to load intent patterns from database."""
        try:
            db_patterns = await self._fetch_db_patterns()
            if db_patterns:
                # Merge/replace hardcoded patterns with database patterns
                self._db_patterns = db_patterns
                self.info_patterns.update(db_patterns)
                logger.info(
                    f"Loaded {sum(len(kws) for kws in db_patterns.values())} intent patterns "
                    f"from database across {len(db_patterns)} categories"
                )
            else:
                logger.info("Database patterns not available, using hardcoded fallback")
        except Exception as e:
            logger.warning(f"Failed to load patterns from database: {e}. Using hardcoded fallback.")

    async def _fetch_db_patterns(self) -> Dict[IntentCategory, List[str]]:
        """
        Fetch patterns from Admin API and convert to classifier format.
        Returns Dict[IntentCategory, List[str]] or empty dict on error.
        """
        try:
            client = get_admin_client()
            raw_patterns = await client.get_intent_patterns()

            if not raw_patterns:
                return {}

            # Convert from Dict[category_string, List[keywords]]
            # to Dict[IntentCategory, List[keywords]]
            converted: Dict[IntentCategory, List[str]] = {}
            category_map = {
                "control": IntentCategory.CONTROL,
                "weather": IntentCategory.WEATHER,
                "sports": IntentCategory.SPORTS,
                "airports": IntentCategory.AIRPORTS,
                "transit": IntentCategory.TRANSIT,
                "food": IntentCategory.FOOD,
                "emergency": IntentCategory.EMERGENCY,
                "events": IntentCategory.EVENTS,
                "location": IntentCategory.LOCATION,
                "general_info": IntentCategory.GENERAL_INFO
            }

            for category_str, keywords in raw_patterns.items():
                intent_cat = category_map.get(category_str)
                if intent_cat:
                    converted[intent_cat] = keywords

            return converted

        except Exception as e:
            logger.warning(f"Error fetching DB patterns: {e}")
            return {}

    async def classify(self, query: str) -> IntentClassification:
        """
        Classify intent using layered approach:
        1. Fast path pattern matching
        2. Entity extraction
        3. Complexity detection for LLM routing
        """
        # Ensure database loading has started (lazy loading)
        self._ensure_db_loading_started()

        result = IntentClassification()
        query_lower = query.lower().strip()

        # Layer 1: Fast path pattern matching
        pattern_result = self._pattern_match(query_lower)
        if pattern_result:
            result.category, result.confidence = pattern_result

            # High confidence pattern match - skip LLM
            if result.confidence >= 0.8:
                result.entities = self._extract_entities(query_lower, result.category)
                result.cache_key = self._generate_cache_key(query_lower, result.category)
                logger.debug(
                    f"High confidence classification: {result.category.value} "
                    f"(confidence: {result.confidence:.2f})"
                )
                return result

        # Check if complex query needing LLM
        if self._is_complex(query_lower):
            result.requires_llm = True
            result.confidence = min(result.confidence, 0.5)  # Cap confidence for complex queries

        # Layer 2: Entity extraction regardless of classification
        result.entities = self._extract_entities(query_lower, result.category)
        result.cache_key = self._generate_cache_key(query_lower, result.category)

        # If no pattern match and not complex, mark as unknown
        if result.category == IntentCategory.UNKNOWN and not result.requires_llm:
            result.confidence = 0.0

        logger.debug(
            f"Classification complete: {result.category.value} "
            f"(confidence: {result.confidence:.2f}, requires_llm: {result.requires_llm})"
        )

        return result

    def _pattern_match(self, query: str) -> Optional[Tuple[IntentCategory, float]]:
        """
        Pattern-based classification with confidence scoring.
        Returns (category, confidence) or None
        """

        # Check control patterns first (highest priority)
        control_score = 0
        matched_patterns = []

        for pattern_type, patterns in self.control_patterns.items():
            for pattern in patterns:
                if pattern in query:
                    control_score += 1
                    matched_patterns.append(pattern)

        # Check for room+color combination (strong indicator of light control)
        # This allows commands like "change office to white" without requiring "lights"
        has_room = any(room in query for room in self.room_entities)
        has_color = any(color in query for color in self.color_entities)
        has_light_device = any(d in query for d in ["lights", "light", "lamp", "lamps"])

        # Check for themed color requests (team colors, holiday colors, etc.)
        themed_color_patterns = [
            "colors of the", "colors of", "team colors", "the colors",
            "themed", "theme", "christmas", "halloween", "holiday",
            "wolverines", "ravens", "orioles", "steelers", "patriots",  # Common teams
            "color scheme", "color palette"
        ]
        has_themed_colors = any(pattern in query for pattern in themed_color_patterns)

        # DEBUG: Log room+color detection
        logger.info(f"Room+color check: query='{query[:50]}', has_room={has_room}, has_color={has_color}, has_light={has_light_device}, has_themed={has_themed_colors}")

        if has_room and has_color:
            # Room + color = almost certainly a light control command
            logger.info(f"Detected room+color pattern in query: '{query[:50]}...'")
            control_score += 2  # Strong boost for room+color combination

        # Room + lights = light control command
        if has_room and has_light_device:
            logger.info(f"Detected room+lights pattern in query: '{query[:50]}...'")
            control_score += 2  # Strong boost for room+lights

        # Themed color request with room or lights = light control
        if has_themed_colors and (has_room or has_light_device):
            logger.info(f"Detected themed color request in query: '{query[:50]}...'")
            control_score += 2  # Strong boost for themed colors

        if control_score > 0:
            # Calculate confidence based on match strength
            # More matches = higher confidence
            base_confidence = 0.6
            confidence_boost = min(control_score * 0.15, 0.35)
            confidence = base_confidence + confidence_boost

            # Boost confidence if we have both action and device
            has_action = any(
                action in query
                for actions in self.action_keywords.values()
                for action in actions
            )
            has_device = any(device in query for device in self.device_entities)

            if has_action and has_device:
                confidence = min(confidence + 0.1, 0.95)

            # Also boost for room+color even without explicit device mention
            if has_room and has_color:
                confidence = min(confidence + 0.15, 0.95)

            return (IntentCategory.CONTROL, confidence)

        # Check information patterns
        best_match = None
        best_score = 0
        total_patterns_checked = 0

        for category, patterns in self.info_patterns.items():
            score = sum(1 for p in patterns if p in query)
            total_patterns_checked += len(patterns)

            if score > best_score:
                best_score = score
                best_match = category

        if best_match and best_score > 0:
            # Special handling: Exclude "how to" questions from weather classification
            # Questions like "How do you snowboard?" should not be classified as weather
            # even though they contain weather-related words like "snow"
            if best_match == IntentCategory.WEATHER:
                # Check for "how to/do/does" patterns
                how_to_patterns = [
                    r'\bhow (to|do|does|can|should)\b',
                    r'\bwhat (is|are|was|were)\b.*\b(snowboard|ski|brain|drain)\b',
                ]

                is_how_to = any(re.search(pattern, query, re.IGNORECASE) for pattern in how_to_patterns)

                if is_how_to:
                    # This is a "how to" or "what is" question, not a weather query
                    # Reclassify as general_info with moderate confidence
                    logger.debug(f"Reclassifying '{query[:50]}...' from weather to general_info (how-to pattern detected)")
                    return (IntentCategory.GENERAL_INFO, 0.6)

            # Calculate confidence based on match density
            # More pattern matches relative to pattern count = higher confidence
            pattern_count = len(self.info_patterns[best_match])
            match_ratio = best_score / max(pattern_count, 1)

            # Base confidence starts at 0.5
            base_confidence = 0.5

            # Add confidence based on match ratio
            # If we match 20% of patterns, add 0.2 confidence
            confidence_boost = min(match_ratio * 1.0, 0.45)

            confidence = base_confidence + confidence_boost

            # Additional boost for very specific matches
            if best_score >= 3:
                confidence = min(confidence + 0.1, 0.95)

            return (best_match, confidence)

        return None

    def _is_complex(self, query: str) -> bool:
        """Check if query requires complex LLM processing"""
        # Check for complex indicators
        for indicator in self.complex_indicators:
            if indicator in query:
                return True

        # Check for questions that need reasoning
        question_words = ["why", "how", "what", "when", "where", "who", "which"]
        has_question = any(query.startswith(word) for word in question_words)

        # Check for length and complexity
        word_count = len(query.split())
        is_long = word_count > 15

        # Multiple conditions or comparisons
        has_multiple_conditions = " if " in query or " unless " in query
        has_comparison = " versus " in query or " vs " in query or " or " in query

        return (has_question and word_count > 5) or is_long or has_multiple_conditions or has_comparison

    def _extract_entities(self, query: str, category: IntentCategory) -> Dict[str, Any]:
        """Extract relevant entities based on intent category"""
        entities = {}

        if category == IntentCategory.CONTROL:
            # Extract room/location
            for room in self.room_entities:
                if room in query:
                    entities["room"] = room
                    break

            # Extract device
            for device in self.device_entities:
                if device in query:
                    entities["device"] = device
                    break

            # Extract action
            for action, keywords in self.action_keywords.items():
                if any(keyword in query for keyword in keywords):
                    entities["action"] = action
                    break

            # Extract numeric values
            # Temperature
            temp_match = re.search(r'(\d+)\s*(?:degrees?|°)', query)
            if temp_match:
                entities["temperature"] = int(temp_match.group(1))

            # Brightness/percentage
            percent_match = re.search(r'(\d+)\s*(?:%|percent)', query)
            if percent_match:
                entities["brightness"] = int(percent_match.group(1))

            # Color
            colors = ["red", "blue", "green", "white", "warm", "cool", "yellow", "purple", "orange"]
            for color in colors:
                if color in query:
                    entities["color"] = color
                    break

        elif category == IntentCategory.WEATHER:
            # Extract time references
            time_refs = {
                "today": "today",
                "tonight": "tonight",
                "tomorrow": "tomorrow",
                "weekend": "weekend",
                "next week": "next_week",
                "this week": "this_week"
            }
            for ref, value in time_refs.items():
                if ref in query:
                    entities["timeframe"] = value
                    break

            # Mark if forecast is needed (future timeframes or keywords like "will", "going to")
            future_keywords = [
                "tomorrow", "weekend", "next week", "this week",
                "will it", "going to", "is it going to",
                "when will", "when is it going to"
            ]

            # Check if question is about future weather
            if any(keyword in query for keyword in future_keywords):
                entities["forecast"] = True
            # Default to current weather
            elif "timeframe" not in entities or entities["timeframe"] == "today":
                entities["forecast"] = False

            # Extract location if specified (works with lowercase queries)
            # Patterns: "in chicago", "in new york", "in los angeles", "weather in seattle"
            location_match = re.search(r'in\s+([a-z]+(?:\s+[a-z]+)?(?:\s+[a-z]+)?)', query)
            if location_match:
                # Capitalize each word in the location
                location = location_match.group(1).strip()
                # Title case the location (Chicago, New York, Los Angeles)
                entities["location"] = ' '.join(word.capitalize() for word in location.split())

        elif category == IntentCategory.SPORTS:
            # Extract team names
            teams = {
                "ravens": "Baltimore Ravens",
                "orioles": "Baltimore Orioles",
                "terps": "Maryland Terrapins",
                "caps": "Washington Capitals",
                "commanders": "Washington Commanders"
            }
            for team_key, team_name in teams.items():
                if team_key in query:
                    entities["team"] = team_name
                    break

            # Extract time references
            if "last" in query or "yesterday" in query:
                entities["timeframe"] = "past"
            elif "next" in query or "upcoming" in query:
                entities["timeframe"] = "future"
            elif "today" in query or "tonight" in query:
                entities["timeframe"] = "current"

            # Check for specific info requested
            if "score" in query:
                entities["info_type"] = "score"
            elif "schedule" in query:
                entities["info_type"] = "schedule"
            elif "tickets" in query:
                entities["info_type"] = "tickets"

        elif category == IntentCategory.AIRPORTS:
            # Extract airport codes
            airports = {
                "bwi": "BWI",
                "dulles": "IAD",
                "reagan": "DCA",
                "national": "DCA",
                "philadelphia": "PHL"
            }
            for airport_key, code in airports.items():
                if airport_key in query:
                    entities["airport"] = code
                    break

            # Extract flight info
            if "arrival" in query:
                entities["info_type"] = "arrival"
            elif "departure" in query:
                entities["info_type"] = "departure"
            elif "delay" in query:
                entities["info_type"] = "delay"

            # Extract flight number if present
            flight_match = re.search(r'([A-Z]{2})\s*(\d+)', query)
            if flight_match:
                entities["flight"] = f"{flight_match.group(1)}{flight_match.group(2)}"

        return entities

    def _generate_cache_key(self, query: str, category: IntentCategory) -> str:
        """Generate a cache key for the query"""
        # Normalize query for caching
        normalized = re.sub(r'\s+', ' ', query.lower().strip())
        # Remove common words that don't affect intent
        stopwords = ["the", "a", "an", "is", "are", "what", "whats", "please", "can", "you"]
        words = normalized.split()
        filtered = [w for w in words if w not in stopwords]
        key_base = "_".join(filtered[:5])  # Use first 5 significant words
        return f"{category.value}:{key_base}"

    def detect_multi_intent(self, query: str) -> List[str]:
        """
        Detect if query contains multiple intents and split them.
        Returns list of sub-queries.
        """
        # Compound query indicators
        separators = [
            " and ",
            " then ",
            " also ",
            " after that ",
            ", then ",
            "; ",
            " plus "
        ]

        # Check if any separator exists
        query_lower = query.lower()
        found_separators = [sep for sep in separators if sep in query_lower]

        if not found_separators:
            return [query]  # Single intent

        # Split on separators while preserving context
        parts = [query]
        for separator in found_separators:
            new_parts = []
            for part in parts:
                if separator in part.lower():
                    split_parts = part.split(separator)
                    # Preserve context words if needed
                    for i, split_part in enumerate(split_parts):
                        if i > 0 and not self._has_action_word(split_part):
                            # Might need context from previous part
                            split_part = self._add_context(split_parts[i-1], split_part)
                        new_parts.append(split_part.strip())
                else:
                    new_parts.append(part)
            parts = new_parts

        # Filter out empty or too-short parts
        valid_parts = [p for p in parts if len(p.split()) >= 2]

        return valid_parts if valid_parts else [query]

    def _has_action_word(self, text: str) -> bool:
        """Check if text has an action word"""
        action_words = [
            "turn", "set", "get", "what", "check", "show", "tell",
            "switch", "enable", "disable", "open", "close", "play", "stop"
        ]
        text_lower = text.lower()
        return any(word in text_lower for word in action_words)

    def _add_context(self, previous: str, current: str) -> str:
        """Add context from previous part if current lacks it"""
        # Extract subject from previous if current lacks it
        if "lights" in previous.lower() and "lights" not in current.lower():
            if "on" in current.lower() or "off" in current.lower():
                current = f"lights {current}"

        return current