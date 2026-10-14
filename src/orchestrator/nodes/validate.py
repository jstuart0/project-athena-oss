"""validate_node — extracted from orchestrator.main during Phase 2.5 (ATHENA-10).

Byte-identical move. See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.
"""
from __future__ import annotations

import json
import logging
import time

from orchestrator.nodes._runtime import get_llm_router
from orchestrator.grounding import (
    DECLINE_TEXT,
    TOOL_OWNED,
    evidence_summary,
    experience_claim,
    grounded,
    knowledge_question,
    local_business_cue,
    prior_tool_context,
)
from orchestrator.state import OrchestratorState, IntentCategory
from orchestrator.helpers import (
    answer_is_too_short,
    get_component_config,
    _component_system_prompt,
)
from orchestrator.metrics import (
    validation_counter,
    hallucination_counter,
    validation_layer_duration,
)
from shared.assistant_profile import get_validation_guardrails

logger = logging.getLogger(__name__)


class _LLMRouterProxy:
    """Forward attribute access to the runtime LLM router at call time.

    validate_node references ``llm_router`` as a bare module global.
    The actual router is registered in _runtime by main.py's lifespan, so we
    cannot bind it at import time.  This proxy resolves the current value on
    every attribute lookup, keeping the function body byte-identical.
    """

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_llm_router(), name)


llm_router = _LLMRouterProxy()


def _decline_if_tool_turn(state: OrchestratorState) -> None:
    """A failed validation on a turn the tool node answered ends in the decline
    sentence: there is no further route to try."""
    if state.tool_call_attempted:
        state.answer = DECLINE_TEXT
        state.is_fallback = True


async def validate_node(state: OrchestratorState) -> OrchestratorState:
    """
    Multi-layer anti-hallucination validation.

    Layer 1: Basic checks (length, error patterns)
    Layer 2: Pattern detection (specific facts without data)
    Layer 3: LLM-based fact checking
    Layer 4: Uncertainty marker detection
    """
    start = time.time()

    validation_guardrails = await get_validation_guardrails()
    min_response_chars = validation_guardrails["min_response_chars"]
    max_response_chars = validation_guardrails["max_response_chars"]

    # A tool-node turn with nothing to validate keeps the route it had before
    # validation was wired in: finalize speaks its own fallback.
    if state.tool_call_attempted and not (state.answer or "").strip():
        state.node_timings["validate"] = time.time() - start
        return state

    # Layer 1: Basic validation
    basic_start = time.time()
    # A deterministic answer ("Okay.") is the answer however short; an LLM
    # answer is too short only by the rule in answer_is_too_short.
    deterministic = bool(state.skip_synthesis and (state.answer or "").strip())
    exempt_from_short = deterministic or state.tool_call_attempted
    if not state.answer or (not exempt_from_short and answer_is_too_short(state.answer, min_response_chars)):
        state.validation_passed = False
        state.validation_reason = "Response too short"
        logger.warning(f"Validation failed: {state.validation_reason}")
        validate_duration = time.time() - start
        state.node_timings["validate"] = validate_duration
        # Track metrics
        validation_counter.labels(passed="false", reason="too_short").inc()
        validation_layer_duration.labels(layer="basic").observe(time.time() - basic_start)
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "basic_check", validate_duration)
        return state

    # Spoken answers are capped by answer_max_tokens already, so the length rule
    # only ever rejected a text answer; a tool-node answer (a recipe, an
    # itinerary) is long on purpose.
    if len(state.answer) > max_response_chars and not state.tool_call_attempted:
        state.validation_passed = False
        state.validation_reason = "Response too long"
        logger.warning(f"Validation failed: {state.validation_reason}")
        validate_duration = time.time() - start
        state.node_timings["validate"] = validate_duration
        # Track metrics
        validation_counter.labels(passed="false", reason="too_long").inc()
        validation_layer_duration.labels(layer="basic").observe(time.time() - basic_start)
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "basic_check", validate_duration)
        return state

    validation_layer_duration.labels(layer="basic").observe(time.time() - basic_start)

    if state.answer == DECLINE_TEXT:
        state.validation_passed = True
        state.validation_reason = None
        state.node_timings["validate"] = time.time() - start
        validation_counter.labels(passed="true", reason="decline_text").inc()
        return state

    # Layer 2: Pattern detection for hallucinations
    # Look for specific patterns that indicate fabricated information
    import re
    pattern_start = time.time()

    # Detect specific dates (Month DD, YYYY or MM/DD/YYYY)
    date_patterns = re.findall(r'(\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b)', state.answer)

    # Detect specific times (HH:MM AM/PM)
    time_patterns = re.findall(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b', state.answer)

    # Detect specific dollar amounts
    money_patterns = re.findall(r'\$\d+(?:,\d{3})*(?:\.\d{2})?', state.answer)

    # Detect phone numbers
    phone_patterns = re.findall(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', state.answer)

    has_specific_facts = bool(date_patterns or time_patterns or money_patterns or phone_patterns)
    validation_layer_duration.labels(layer="pattern").observe(time.time() - pattern_start)

    # A first-person experience ("I've eaten there") is never true of the
    # assistant, whatever data the turn holds; a story told in the first person
    # is the one exception (experience_claim reads the request). Checked on a
    # tool-node turn on every path; the streamed synthesis has no validation, so
    # the check stays off for synthesized turns on the graph too, and the
    # persona's fixed line carries them.
    if state.tool_call_attempted and experience_claim(state.answer, state.query):
        state.validation_passed = False
        state.validation_reason = "Hallucination detected: first-person experience claim"
        state.validation_details = ["first-person experience"]
        hallucination_counter.labels(layer="pattern_detection", type="experience_claim").inc()
        validation_counter.labels(passed="false", reason="experience_claim").inc()
        _decline_if_tool_turn(state)
        state.node_timings["validate"] = time.time() - start
        return state

    query_lower = state.query.lower()
    is_low_risk_chitchat = state.intent == IntentCategory.GENERAL_INFO and any(
        phrase in query_lower for phrase in [
            "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
            "thanks", "thank you", "bye", "goodbye", "see you", "how are you",
            "tell me about yourself", "who are you"
        ]
    )
    is_builtin_time_or_date_query = state.intent == IntentCategory.GENERAL_INFO and any(
        phrase in query_lower for phrase in [
            "what time", "time is it", "current time", "what's the time", "whats the time",
            "what date", "today's date", "current date", "what day", "what month", "what year",
        ]
    )

    if is_low_risk_chitchat:
        state.validation_passed = True
        state.validation_reason = None
        state.node_timings["validate"] = time.time() - start
        validation_counter.labels(passed="true", reason="low_risk_chitchat").inc()
        validation_layer_duration.labels(layer="pattern").observe(time.time() - pattern_start)
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "low_risk_bypass", time.time() - start)
        logger.info("Validation bypassed for low-risk chitchat")
        return state

    if is_builtin_time_or_date_query:
        state.validation_passed = True
        state.validation_reason = None
        state.node_timings["validate"] = time.time() - start
        validation_counter.labels(passed="true", reason="builtin_time_or_date").inc()
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "builtin_time_or_date_bypass", time.time() - start)
        logger.info("Validation bypassed for built-in time/date query")
        return state

    # Track what patterns were detected (for hallucination analysis)
    if date_patterns:
        logger.info(f"Pattern detection: found {len(date_patterns)} date patterns")
    if time_patterns:
        logger.info(f"Pattern detection: found {len(time_patterns)} time patterns")
    if money_patterns:
        logger.info(f"Pattern detection: found {len(money_patterns)} money patterns")
    if phone_patterns:
        logger.info(f"Pattern detection: found {len(phone_patterns)} phone patterns")

    # Training-knowledge fallback bypass (ATHENA-39)
    # Mirrors synthesize_node's two training-knowledge-permitted branches:
    #   synthesize.py:129-144  — intent == GENERAL_INFO, no retrieved_data: "you may answer
    #                             using your built-in knowledge"
    #   synthesize.py:152-166  — any intent with conversation context: "Use the previous
    #                             conversation context... you may answer using your built-in
    #                             knowledge"
    # The anti-fabrication branch (synthesize.py:167-179) is explicitly NOT bypassed:
    # that prompt tells the LLM "NEVER make up specific facts, dates, names, numbers, or
    # events", so Layer 4 protection is correctly aligned there.
    # WEBSEARCH is carved out even with conversation context because the user explicitly
    # requested fresh data; a training-knowledge answer should not bypass validation there.
    is_training_knowledge_path = (
        state.intent == IntentCategory.GENERAL_INFO
        or bool(state.conversation_history or state.history_summary)
    )
    is_training_knowledge_fallback = (
        is_training_knowledge_path
        and not state.retrieved_data
        and not state.base_knowledge_populated
        and state.intent != IntentCategory.WEBSEARCH
        and (state.intent not in TOOL_OWNED or prior_tool_context(state))
    )
    if is_training_knowledge_fallback:
        state.validation_passed = True
        state.validation_reason = None
        state.node_timings["validate"] = time.time() - start
        validation_counter.labels(passed="true", reason="training_knowledge_fallback").inc()
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "training_knowledge_bypass", time.time() - start)
        logger.info("Validation bypassed for training-knowledge fallback", extra={"intent": state.intent.value if state.intent else None})
        return state

    # A definition, how-it-works or why question: its worked example (a company
    # and a price) is not a claim about the world, whatever the intent.
    if knowledge_question(state.query) and not local_business_cue(state.query):
        state.validation_passed = True
        state.validation_reason = None
        state.node_timings["validate"] = time.time() - start
        validation_counter.labels(passed="true", reason="knowledge_question").inc()
        return state

    # Layer 3: Check if we have data to support specific facts.
    # Base knowledge injected into the system prompt is authoritative — it counts as
    # supporting data. The LLM fact-checker has no visibility into the system prompt, so
    # without this flag it would wrongly flag dates from base knowledge as hallucinations.
    # A data intent's own base-knowledge rows don't vouch for it (the house's
    # facts say nothing about a restaurant), and an error object isn't data.
    has_supporting_data = (
        grounded(state)
        or prior_tool_context(state)
        or (state.base_knowledge_populated and state.intent not in TOOL_OWNED)
    )

    if has_specific_facts and not has_supporting_data and not is_builtin_time_or_date_query:
        logger.warning(f"Response contains specific facts but no supporting data retrieved")
        logger.warning(f"Dates: {len(date_patterns)}, Times: {len(time_patterns)}, Money: {len(money_patterns)}, Phones: {len(phone_patterns)}")

        # Track suspicious patterns found (potential hallucinations without supporting data)
        if date_patterns:
            hallucination_counter.labels(layer="pattern_detection", type="date_unsupported").inc(len(date_patterns))
        if time_patterns:
            hallucination_counter.labels(layer="pattern_detection", type="time_unsupported").inc(len(time_patterns))
        if money_patterns:
            hallucination_counter.labels(layer="pattern_detection", type="money_unsupported").inc(len(money_patterns))
        if phone_patterns:
            hallucination_counter.labels(layer="pattern_detection", type="phone_unsupported").inc(len(phone_patterns))

        # Layer 4: LLM-based fact checking
        llm_fact_check_start = time.time()
        evidence = evidence_summary(state)
        try:
            fact_check_prompt = f"""You are a fact-checking assistant. Analyze this response for hallucinations.

Original Query: {state.query}

Retrieved Data Available: {'Yes' if evidence else 'No'}
{f"Retrieved Data: {evidence}" if evidence else "No data was retrieved from external sources."}

Generated Response:
{state.answer}

Question: Does this response contain specific factual claims (dates, times, names, phone numbers, prices, events) that are NOT present in the Retrieved Data?

IMPORTANT: If no Retrieved Data is available, ANY specific factual claims are likely hallucinations.

Respond ONLY with valid JSON:
{{"contains_hallucinations": true/false, "reason": "brief explanation", "specific_claims": ["list of suspicious claims"]}}"""

            # Combine system and user prompts
            full_fact_check_prompt = f"You are a precise fact-checking assistant. Always respond with valid JSON.\n\n{fact_check_prompt}"

            # Get validation model from database or use fallback
            validation_config = await get_component_config("fact_check_validation")
            validation_model = validation_config["model_name"]

            validation_start = time.time()
            result = await llm_router.generate(
                model=validation_model,
                prompt=full_fact_check_prompt,
                temperature=0.1,  # Low temperature for consistent checking
                system_prompt=_component_system_prompt(validation_config),
                request_id=state.request_id,
                session_id=state.session_id,
                user_id=state.mode,
                zone=state.room,
                intent=state.intent.value if state.intent else None,
                stage="validation"
            )
            validation_duration = time.time() - validation_start

            # Track LLM call for metrics
            if state.timing_tracker:
                tokens = result.get("eval_count", 0)
                state.timing_tracker.record_llm_call(
                    "validation", validation_model, tokens, int(validation_duration * 1000), "fact_check"
                )

            fact_check_response = result.get("response", "")

            # Parse fact check response
            try:
                # Extract JSON from response (handle markdown code blocks)
                json_match = re.search(r'\{.*\}', fact_check_response, re.DOTALL)
                if json_match:
                    fact_check_result = json.loads(json_match.group())

                    if fact_check_result.get("contains_hallucinations", False):
                        state.validation_passed = False
                        state.validation_reason = f"Hallucination detected: {fact_check_result.get('reason', 'Unknown')}"
                        state.validation_details = fact_check_result.get("specific_claims", [])
                        logger.warning(f"Hallucination detected by LLM fact checker: {state.validation_reason}")
                        logger.warning(f"Suspicious claims: {state.validation_details}")
                        # Track LLM-detected hallucinations
                        hallucination_counter.labels(layer="llm_fact_check", type="confirmed").inc()
                        validation_counter.labels(passed="false", reason="hallucination_llm").inc()
                    else:
                        state.validation_passed = True
                        logger.info("Response passed LLM fact checking")
                        validation_counter.labels(passed="true", reason="llm_verified").inc()
                else:
                    logger.warning(f"Could not parse fact check response as JSON: {fact_check_response}")
                    # Default to failing validation if we can't parse
                    state.validation_passed = False
                    state.validation_reason = "Could not verify response accuracy"
                    validation_counter.labels(passed="false", reason="parse_error").inc()

            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse fact check JSON: {e}")
                # Default to failing validation if we can't parse
                state.validation_passed = False
                state.validation_reason = "Could not verify response accuracy"
                validation_counter.labels(passed="false", reason="json_error").inc()

            # Track LLM fact check duration
            validation_layer_duration.labels(layer="llm_fact_check").observe(time.time() - llm_fact_check_start)

        except Exception as e:
            logger.error(f"Fact checking error: {e}", exc_info=True)
            # If fact checking fails, be conservative and fail validation
            state.validation_passed = False
            state.validation_reason = f"Validation error: {str(e)}"
            validation_counter.labels(passed="false", reason="exception").inc()
            validation_layer_duration.labels(layer="llm_fact_check").observe(time.time() - llm_fact_check_start)

    else:
        # No specific facts or we have supporting data
        state.validation_passed = True
        if has_supporting_data:
            validation_counter.labels(passed="true", reason="has_supporting_data").inc()
        else:
            validation_counter.labels(passed="true", reason="no_specific_facts").inc()
        logger.info("Response passed validation (no specific facts or has supporting data)")

    if not state.validation_passed:
        _decline_if_tool_turn(state)

    validate_duration = time.time() - start
    state.node_timings["validate"] = validate_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "validate", validate_duration)
    return state
