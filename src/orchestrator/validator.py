"""
Response Validation System with Anti-Hallucination
Migrated from Jetson's two-layer validation system
"""

import re
import time
import logging
from typing import Tuple, Dict, Any, Optional
import httpx
import asyncio

from shared.admin_config import get_admin_client

logger = logging.getLogger(__name__)


class ResponseValidator:
    """
    Two-layer anti-hallucination validation system:
    Layer 1: Self-validation to ensure response addresses query
    Layer 2: Cross-model validation for confidence scoring

    IMPORTANT: Uses direct HTTP calls to Ollama for isolation from main LLM pipeline.
    Only model NAMES are configurable via database - HTTP architecture is preserved.
    """

    def __init__(
        self,
        llm_service_url: str = "http://localhost:11434",
        primary_model: str = None,  # Changed: Now uses database lookup if None
        validation_model: str = None  # Changed: Now uses database lookup if None
    ):
        self.llm_service_url = llm_service_url
        self._primary_model_config = primary_model
        self._validation_model_config = validation_model
        self.client = httpx.AsyncClient(timeout=30.0)

        # Cache for database model lookups
        self._cached_primary: Optional[str] = None
        self._cached_validation: Optional[str] = None
        self._model_cache_time = 0
        self._model_cache_ttl = 60  # 60 second cache

    async def _get_primary_model(self) -> str:
        """Get primary validation model from database or fallback."""
        if self._primary_model_config:
            return self._primary_model_config  # Use explicit config

        # Try database lookup with cache
        now = time.time()
        if now - self._model_cache_time > self._model_cache_ttl:
            try:
                admin_client = get_admin_client()
                config = await admin_client.get_component_model("response_validator_primary")
                if config and config.get("enabled"):
                    self._cached_primary = config.get("model_name")
                    self._model_cache_time = now
            except Exception as e:
                logger.warning(f"validator_primary_model_fetch_failed: {e}")

        return self._cached_primary or "phi3:mini"

    async def _get_validation_model(self) -> str:
        """Get secondary validation model from database or fallback."""
        if self._validation_model_config:
            return self._validation_model_config  # Use explicit config

        # Try database lookup (shares cache timing with primary)
        try:
            admin_client = get_admin_client()
            config = await admin_client.get_component_model("response_validator_secondary")
            if config and config.get("enabled"):
                return config.get("model_name")
        except Exception as e:
            logger.warning(f"validator_secondary_model_fetch_failed: {e}")

        return "phi3:mini"

    async def validate_response(
        self,
        query: str,
        response: str,
        intent_category: Optional[str] = None,
        enable_cross_check: bool = True,
        require_high_confidence: bool = False
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Full validation pipeline with two layers.

        Args:
            query: Original user query
            response: Generated response to validate
            intent_category: Type of query (sports, weather, etc.)
            enable_cross_check: Whether to perform Layer 2 validation
            require_high_confidence: Whether to enforce strict validation

        Returns:
            Tuple of (is_valid, final_response, metadata)
        """
        metadata = {
            'layer1': {'passed': False, 'modified': False, 'checks': []},
            'layer2': {
                'enabled': enable_cross_check,
                'passed': False,
                'confidence': 0.0
            },
            'intent_category': intent_category
        }

        # Layer 1: Self-validation
        try:
            is_valid, validated, layer1_checks = await self._self_validate(
                query,
                response,
                intent_category
            )
            metadata['layer1']['passed'] = is_valid
            metadata['layer1']['modified'] = (validated != response)
            metadata['layer1']['checks'] = layer1_checks

            if not is_valid:
                logger.warning(
                    f"Layer 1 validation failed for query: {query[:50]}... "
                    f"Checks failed: {[c['name'] for c in layer1_checks if not c['passed']]}"
                )

                # Try to fix with validation prompt
                validated = await self._attempt_fix(query, response, layer1_checks)
                if validated != response:
                    metadata['layer1']['modified'] = True

        except Exception as e:
            logger.error(f"Layer 1 validation error: {e}")
            validated = response
            metadata['layer1']['error'] = str(e)

        # Layer 2: Cross-model validation (optional)
        if enable_cross_check and (require_high_confidence or not is_valid):
            try:
                is_consistent, final, confidence = await self._cross_check(
                    query,
                    validated,
                    intent_category
                )
                metadata['layer2']['passed'] = is_consistent
                metadata['layer2']['confidence'] = confidence

                # Strict validation for high-confidence requirements
                if require_high_confidence and confidence < 0.7:
                    logger.warning(
                        f"Layer 2 high-confidence check failed: {confidence:.2f} < 0.7"
                    )
                    return (False, final, metadata)

                # Regular validation
                if confidence < 0.5:
                    logger.warning(
                        f"Layer 2 low confidence: {confidence:.2f} for query: {query[:50]}..."
                    )
                    return (False, final, metadata)

                return (True, final, metadata)

            except Exception as e:
                logger.error(f"Layer 2 validation error: {e}")
                metadata['layer2']['error'] = str(e)

        return (is_valid, validated, metadata)

    async def _self_validate(
        self,
        query: str,
        response: str,
        intent_category: Optional[str] = None
    ) -> Tuple[bool, str, list]:
        """
        Layer 1: Validate that response addresses the query.
        Returns (is_valid, response, checks_performed)
        """
        query_lower = query.lower()
        response_lower = response.lower()
        checks = []

        # Score/result check
        if any(word in query_lower for word in ['score', 'result', 'won', 'lost', 'beat', 'final']):
            has_numbers = bool(re.search(r'\d+', response))
            has_team_result = any(
                phrase in response_lower
                for phrase in ['won', 'lost', 'beat', 'defeated', 'tie', 'draw']
            )
            check_passed = has_numbers or has_team_result

            checks.append({
                'name': 'score_check',
                'passed': check_passed,
                'reason': 'Response must include scores or game result'
            })

            if not check_passed:
                return (False, response, checks)

        # Time/schedule check
        if any(word in query_lower for word in ['when', 'what time', 'schedule', 'hours']):
            time_indicators = [
                'am', 'pm', ':', 'o\'clock', 'noon', 'midnight',
                'morning', 'afternoon', 'evening',
                'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
                'today', 'tomorrow', 'yesterday'
            ]
            has_time = any(indicator in response_lower for indicator in time_indicators)
            has_time_numbers = bool(re.search(r'\d{1,2}(:\d{2})?\s*(am|pm)', response_lower))

            check_passed = has_time or has_time_numbers

            checks.append({
                'name': 'time_check',
                'passed': check_passed,
                'reason': 'Response must include time or date information'
            })

            if not check_passed:
                return (False, response, checks)

        # Location/distance check
        if any(word in query_lower for word in ['where', 'location', 'address', 'how far', 'distance']):
            location_indicators = [
                'street', 'avenue', 'road', 'drive', 'lane', 'way',
                'miles', 'blocks', 'minutes', 'near', 'at', 'located'
            ]
            has_location = any(indicator in response_lower for indicator in location_indicators)
            has_address_number = bool(re.search(r'\d+\s+[A-Z]', response))

            check_passed = has_location or has_address_number

            checks.append({
                'name': 'location_check',
                'passed': check_passed,
                'reason': 'Response must include location or address information'
            })

            if not check_passed:
                return (False, response, checks)

        # Weather-specific check
        if intent_category == "weather" or 'weather' in query_lower:
            weather_terms = [
                'degrees', '°', 'fahrenheit', 'celsius',
                'sunny', 'cloudy', 'rain', 'snow', 'clear',
                'humid', 'wind', 'storm', 'fog'
            ]
            has_weather = any(term in response_lower for term in weather_terms)
            has_temperature = bool(re.search(r'\d+\s*(?:degrees|°)', response_lower))

            check_passed = has_weather or has_temperature

            checks.append({
                'name': 'weather_check',
                'passed': check_passed,
                'reason': 'Response must include weather information'
            })

            if not check_passed:
                return (False, response, checks)

        # Flight/airport check
        if intent_category == "airports" or any(word in query_lower for word in ['flight', 'airport', 'gate']):
            flight_terms = [
                'flight', 'gate', 'terminal', 'delayed', 'on time',
                'boarding', 'departure', 'arrival', 'airline'
            ]
            has_flight_info = any(term in response_lower for term in flight_terms)
            has_flight_number = bool(re.search(r'[A-Z]{2}\s*\d+', response))

            check_passed = has_flight_info or has_flight_number

            checks.append({
                'name': 'flight_check',
                'passed': check_passed,
                'reason': 'Response must include flight information'
            })

            if not check_passed:
                return (False, response, checks)

        # Entity presence check - ensure response mentions key entities from query
        query_entities = self._extract_key_entities(query)
        response_entities = self._extract_key_entities(response)
        entity_overlap = len(query_entities & response_entities) / max(len(query_entities), 1)

        if entity_overlap < 0.3 and len(query_entities) > 0:
            checks.append({
                'name': 'entity_check',
                'passed': False,
                'reason': f'Response missing key entities: {query_entities - response_entities}'
            })
            return (False, response, checks)

        # All checks passed
        if checks:
            checks.append({
                'name': 'overall',
                'passed': True,
                'reason': 'All validation checks passed'
            })

        return (True, response, checks)

    async def _cross_check(
        self,
        query: str,
        response: str,
        intent_category: Optional[str] = None
    ) -> Tuple[bool, str, float]:
        """
        Layer 2: Cross-model validation for consistency.
        Uses a different/smaller model to verify the response.
        Returns (is_consistent, final_response, confidence_score)
        """
        try:
            # Use validation model to verify
            verification_prompt = f"""
            Verify if this answer is correct and appropriate:

            Question: {query}
            Answer: {response}

            Provide ONLY a confidence score from 0.0 to 1.0 where:
            - 1.0 = Perfect, accurate answer
            - 0.8-0.9 = Good answer with minor issues
            - 0.5-0.7 = Acceptable but could be better
            - 0.0-0.4 = Poor or incorrect answer

            Also provide a one-line assessment.

            Format: CONFIDENCE: X.X | ASSESSMENT: <one line>
            """

            # Get validation model from database or fallback
            validation_model = await self._get_validation_model()

            # Call validation model
            validation_response = await self.client.post(
                f"{self.llm_service_url}/api/generate",
                json={
                    "model": validation_model,
                    "prompt": verification_prompt,
                    "temperature": 0.1,
                    "stream": False
                }
            )

            if validation_response.status_code == 200:
                result = validation_response.json()
                validation_text = result.get("response", "")

                # Parse confidence score
                confidence_match = re.search(r'CONFIDENCE:\s*(\d\.\d+)', validation_text)
                if confidence_match:
                    confidence = float(confidence_match.group(1))
                else:
                    # Fallback: calculate based on response similarity
                    confidence = self._calculate_similarity_confidence(response, validation_text)

                # Get assessment
                assessment_match = re.search(r'ASSESSMENT:\s*(.+)', validation_text)
                assessment = assessment_match.group(1) if assessment_match else ""

                logger.debug(f"Cross-check confidence: {confidence:.2f} - {assessment}")

                # If confidence is very low, try to get a better answer
                if confidence < 0.4:
                    better_response = await self._get_better_response(query, response, assessment)
                    return (False, better_response, confidence)

                return (confidence >= 0.5, response, confidence)

            else:
                logger.error(f"Validation model returned status {validation_response.status_code}")
                # Default to trusting original response
                return (True, response, 0.7)

        except Exception as e:
            logger.error(f"Cross-check validation error: {e}")
            # Default to medium confidence
            return (True, response, 0.6)

    async def _attempt_fix(
        self,
        query: str,
        response: str,
        failed_checks: list
    ) -> str:
        """Attempt to fix a response that failed validation"""
        # Build fix prompt based on failed checks
        issues = [check['reason'] for check in failed_checks if not check.get('passed', True)]

        fix_prompt = f"""
        The following response has validation issues:

        User Query: {query}
        Original Response: {response}

        Issues found:
        {' '.join(f'- {issue}' for issue in issues)}

        Provide a corrected response that addresses these issues.
        Be concise and ensure all required information is included.
        """

        try:
            # Get primary model from database or fallback
            primary_model = await self._get_primary_model()

            fix_response = await self.client.post(
                f"{self.llm_service_url}/api/generate",
                json={
                    "model": primary_model,
                    "prompt": fix_prompt,
                    "temperature": 0.3,
                    "stream": False
                }
            )

            if fix_response.status_code == 200:
                result = fix_response.json()
                fixed = result.get("response", response)
                logger.info(f"Response fixed via validation prompt")
                return fixed

        except Exception as e:
            logger.error(f"Fix attempt failed: {e}")

        return response

    async def _get_better_response(
        self,
        query: str,
        poor_response: str,
        assessment: str
    ) -> str:
        """Get a better response when confidence is very low"""
        better_prompt = f"""
        Generate a better response for this query:

        Query: {query}

        Previous response was poor: {poor_response}
        Problem: {assessment}

        Provide a clear, accurate, and complete answer.
        """

        try:
            # Get primary model from database or fallback
            primary_model = await self._get_primary_model()

            better_response = await self.client.post(
                f"{self.llm_service_url}/api/generate",
                json={
                    "model": primary_model,
                    "prompt": better_prompt,
                    "temperature": 0.5,
                    "stream": False
                }
            )

            if better_response.status_code == 200:
                result = better_response.json()
                return result.get("response", poor_response)

        except Exception as e:
            logger.error(f"Failed to get better response: {e}")

        return poor_response

    def _extract_key_entities(self, text: str) -> set:
        """Extract key entities from text for comparison"""
        text_lower = text.lower()

        # Extract significant words (not stopwords)
        stopwords = {
            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'been',
            'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
            'could', 'should', 'may', 'might', 'can', 'must', 'shall',
            'to', 'of', 'in', 'on', 'at', 'by', 'for', 'with', 'from',
            'up', 'about', 'into', 'through', 'during', 'before', 'after',
            'above', 'below', 'between', 'under', 'again', 'further',
            'then', 'once', 'what', 'where', 'when', 'why', 'how',
            'all', 'both', 'each', 'few', 'more', 'most', 'other',
            'some', 'such', 'only', 'own', 'same', 'so', 'than',
            'too', 'very', 'just', 'and', 'or', 'but', 'if', 'nor',
            'as', 'because', 'that', 'this', 'these', 'those', 'it',
            'its', 'itself', 'they', 'them', 'their', 'themselves',
            'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves',
            'you', 'your', 'yours', 'yourself', 'he', 'him', 'his',
            'himself', 'she', 'her', 'hers', 'herself'
        }

        # Extract words
        words = re.findall(r'\b[a-z]+\b', text_lower)
        significant_words = {w for w in words if w not in stopwords and len(w) > 2}

        # Extract numbers
        numbers = set(re.findall(r'\b\d+\b', text))

        # Extract specific entities (teams, locations, etc.)
        entities = significant_words | numbers

        return entities

    def _calculate_similarity_confidence(
        self,
        response1: str,
        response2: str
    ) -> float:
        """Calculate confidence based on response similarity"""
        # Extract key facts from both responses
        entities1 = self._extract_key_entities(response1)
        entities2 = self._extract_key_entities(response2)

        # Calculate Jaccard similarity
        if not entities1 and not entities2:
            return 0.5  # Both empty, neutral confidence

        intersection = len(entities1 & entities2)
        union = len(entities1 | entities2)

        if union == 0:
            return 0.5

        similarity = intersection / union

        # Convert to confidence score (0.0 to 1.0)
        # High similarity = high confidence
        confidence = 0.3 + (similarity * 0.7)

        return min(max(confidence, 0.0), 1.0)

    async def close(self):
        """Clean up resources"""
        await self.client.aclose()