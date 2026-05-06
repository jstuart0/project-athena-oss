"""synthesize_node — extracted from orchestrator.main during Phase 2.4 (ATHENA-10).

Byte-identical move. See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.
"""
from __future__ import annotations

import json
import time

import structlog

from orchestrator.nodes._runtime import get_llm_router
from orchestrator.state import OrchestratorState, IntentCategory
from orchestrator.helpers import (
    _direct_general_info_response,
    get_component_config,
    _component_system_prompt,
    store_conversation_context,
)
from orchestrator.utils.constants import DEFAULT_CITY
from shared.admin_config import get_admin_client
from shared.assistant_profile import build_core_assistant_prompt
from shared.base_knowledge_utils import get_knowledge_context_for_user
from sms.content_detector import detect_textable_content, extract_sms_content

# Event system — optional dependency; guarded with EVENTS_AVAILABLE flag below.
try:
    from shared.events import emit_llm_generating, emit_llm_complete
    EVENTS_AVAILABLE = True
except ImportError:
    EVENTS_AVAILABLE = False

logger = structlog.get_logger(__name__)


class _LLMRouterProxy:
    """Forward attribute access to the runtime LLM router at call time.

    synthesize_node references ``llm_router`` as a bare module global.
    The actual router is registered in _runtime by main.py's lifespan, so we
    cannot bind it at import time.  This proxy resolves the current value on
    every attribute lookup, keeping the function body byte-identical.
    """

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_llm_router(), name)


llm_router = _LLMRouterProxy()


async def synthesize_node(state: OrchestratorState) -> OrchestratorState:
    """
    Generate natural language response using LLM with retrieved data and conversation history.
    """
    start = time.time()

    # SKIP SYNTHESIS OPTIMIZATION (2026-01-12)
    # If skip_synthesis flag is set, we already have a templated response
    # (e.g., from status query optimization) - skip LLM synthesis entirely
    if state.skip_synthesis and state.answer:
        logger.info(
            "synthesis_skipped",
            reason="skip_synthesis_flag",
            answer_length=len(state.answer)
        )
        state.node_timings["synthesize"] = time.time() - start
        return state

    try:
        if state.intent == IntentCategory.GENERAL_INFO and not state.retrieved_data:
            direct_response = _direct_general_info_response(state.query)
            if direct_response:
                duration = time.time() - start
                state.answer = direct_response
                state.skip_synthesis = True
                state.llm_tokens = 0
                state.llm_tokens_per_second = 0.0
                state.node_timings["synthesize"] = duration
                if state.timing_tracker:
                    state.timing_tracker.track_substage("graph", "synthesize", "direct_fast_path", duration)
                logger.info("synthesis_skipped", reason="direct_general_info_fast_path", query=state.query[:40])
                return state

        # Check if this is a continuation response (user answering a question from Athena)
        ref_info = state.context_ref_info or {}
        is_continuation = ref_info.get("is_continuation", False)

        # Build synthesis prompt based on context
        if state.retrieved_data:
            context = json.dumps(state.retrieved_data, indent=2)
            synthesis_prompt = f"""Answer the following question using ONLY the provided context.

Question: {state.query}

Context Data:
{context}

CRITICAL ANTI-HALLUCINATION INSTRUCTIONS:
1. ONLY use facts from the Context Data above - NO EXCEPTIONS
2. If the context doesn't have specific information, say "I don't have information about that"
3. NEVER INVENT OR MAKE UP:
   - Business names, restaurant names, or venue names
   - Addresses or locations
   - Phone numbers or hours
   - Prices or ratings
   - Event names or dates
   - Any specific factual details not in the context
4. If asked for recommendations but context is empty, say "I couldn't find current information for that request"
5. Be concise and only state facts that appear in the Context Data
6. If context contains errors or no results, acknowledge that honestly

Response:"""
        elif is_continuation and state.conversation_history:
            # Continuation response - user is answering Athena's question or continuing conversation
            synthesis_prompt = f"""The user is continuing a conversation with you. Their response: "{state.query}"

Based on the conversation history above, understand what the user means and respond appropriately.

INSTRUCTIONS:
1. Look at your previous question/statement in the conversation history
2. Understand what "{state.query}" means in that context
3. If they answered a question you asked, proceed with what they requested originally
4. If they declined something or said "no preference", continue with reasonable defaults
5. Be helpful and continue the task they originally requested

Your response:"""
            logger.info(f"Using continuation prompt for '{state.query}' with {len(state.conversation_history)} history messages")
        elif state.intent == IntentCategory.GENERAL_INFO:
            synthesis_prompt = f"""Question: {state.query}

Respond naturally as a helpful assistant.

INSTRUCTIONS:
1. For greetings, thanks, farewells, and casual conversation, respond conversationally.
2. Do not mention the current local time or date unless the user explicitly asked for it.
3. If the user asks for the current local time or current date, answer directly using the provided current local time context.
4. You may answer using your built-in knowledge and the provided assistant context.
5. Do not claim to have current web data unless it was actually provided in context.
6. For other time-sensitive or highly specific current facts that were not provided, say you don't have current information.
7. Keep the response concise and direct.
8. SAFETY: If this question asks for harmful information (weapon/drug synthesis, lethal doses, hacking, self-harm methods, etc.), follow the safety guardrails in your system instructions. Decline clearly but helpfully — acknowledge the underlying concern and redirect to a legitimate resource. Do NOT simply say "I can't help with that." Offer something constructive.

Response:"""
        else:
            # No RAG data retrieved — use conversation context and general knowledge
            # If conversation history is available, the user may be referencing prior
            # discussion (e.g., "what city was that restaurant in?"). Allow the LLM to
            # use conversation history to answer, while still being honest about lacking
            # external data.
            has_conversation_context = bool(state.conversation_history or state.history_summary)
            if has_conversation_context:
                synthesis_prompt = f"""Question: {state.query}

Respond naturally as a helpful assistant using the conversation history and your built-in knowledge.

INSTRUCTIONS:
1. Use the previous conversation context to understand references like "the younger one", "that restaurant", "my project", etc.
2. If the user is referring to something they mentioned earlier, answer based on what they told you.
3. You may answer using your built-in knowledge and the provided assistant context.
4. Do not claim to have current web data unless it was actually provided in context.
5. For time-sensitive or highly specific current facts that were not provided, say you don't have current information.
6. Keep the response concise and direct.
7. SAFETY: If this question asks for harmful information, follow the safety guardrails in your system instructions.

Response:"""
            else:
                synthesis_prompt = f"""Question: {state.query}

CRITICAL: You do NOT have access to current or specific information to answer this question.

You must respond with:
1. Acknowledge you don't have current/specific information
2. Suggest where the user can find this information
3. NEVER make up specific facts, dates, names, numbers, or events

Respond honestly about your limitations.

Response:"""

        guest_name = state.context.get("guest_name") if state.context else None

        # Resolve owner_name from base knowledge for owner-mode requests
        owner_name = None
        if state.mode == "owner":
            try:
                _admin_client = get_admin_client()
                _bk_entries = await _admin_client.get_base_knowledge(applies_to="owner", enabled_only=True)
                for _entry in (_bk_entries or []):
                    if _entry.get("category") in ("owner", "user") and _entry.get("key") in ("owner_name", "name"):
                        owner_name = _entry.get("value", "").strip() or None
                        break
            except Exception as e:
                logger.warning("synthesize_node_owner_name_failed", error=str(e))

        system_context = await build_core_assistant_prompt(
            include_voice_formatting=state.interface_type != "chat",
            guest_name=guest_name,
            owner_name=owner_name,
            interface_type=state.interface_type,
        ) + "\n"

        # Inject base knowledge context from Admin API
        try:
            admin_client = get_admin_client()
            user_mode = state.mode if state.mode else "guest"
            knowledge_context = await get_knowledge_context_for_user(admin_client, user_mode)
            if knowledge_context:
                system_context += knowledge_context
                state.base_knowledge_populated = True
                logger.info(f"Base knowledge context injected for mode={user_mode}")
        except Exception as e:
            logger.warning(f"Failed to fetch base knowledge context: {e}")
            # Continue without base knowledge - not critical

        if guest_name:
            logger.info(f"Guest context injected for personalization: {guest_name}")

        # Inject relevant memories for context augmentation
        if state.memory_context:
            system_context += state.memory_context
            logger.info("Memory context injected into LLM prompt")

        # When conversation history exists, instruct the LLM to distinguish
        # between the assistant owner's profile and the current conversation partner
        if state.conversation_history or state.history_summary:
            system_context += """
IMPORTANT: The CONTEXT INFORMATION above describes this assistant's owner and
environment. The CONVERSATION CONTEXT below contains facts stated by the CURRENT
USER in this session. When the user asks about themselves ("my name", "my cats",
"my project", etc.), answer from the conversation context. When they ask about
the assistant's owner or environment, answer from the context information above.
Do not conflate the two — the current user may not be the owner.

"""

        # Barge-in: If user interrupted previous response, acknowledge naturally
        if state.interruption_context:
            interrupted_response = state.interruption_context.get("interrupted_response", "")
            previous_query = state.interruption_context.get("previous_query", "")
            audio_position_ms = state.interruption_context.get("audio_position_ms", 0)

            # Only acknowledge if they interrupted meaningfully (not just silence detection)
            if interrupted_response:
                system_context += f"""
IMPORTANT: The user just interrupted you while you were responding.
- You were answering: "{previous_query}"
- You had said (approximately): "{interrupted_response[:200]}..."
- They interrupted around {audio_position_ms}ms into your response

Acknowledge naturally that they interrupted (e.g., "Sure, go ahead", "Yes?", "Of course")
and then address their new query. Don't repeat what you were saying unless they ask.
Keep your acknowledgment brief - don't dwell on the interruption.

"""
                logger.info("interruption_context_injected",
                           previous_query=previous_query[:30],
                           audio_position_ms=audio_position_ms)

        # Format conversation history for LLM context
        history_context = ""
        if state.history_summary:
            history_context = f"""
CONVERSATION CONTEXT (use this to resolve references like "my", "the", "that", pronouns, etc.):
{state.history_summary}

"""
            logger.info("Using summarized history context")
        elif state.conversation_history:
            logger.info(f"Including {len(state.conversation_history)} previous messages in context")
            history_context = "CONVERSATION CONTEXT (use this to resolve references like \"my\", \"the\", \"that\", pronouns, etc.):\n"
            for msg in state.conversation_history:
                role = msg["role"].capitalize()
                content = msg["content"]
                history_context += f"{role}: {content}\n"
            history_context += "\n"

        # Combine system context, history, and synthesis prompt
        # Place history right before the synthesis prompt so it's closest to the question
        full_prompt = system_context + history_context + synthesis_prompt

        # Get synthesis model from database or use fallback
        synthesis_config = await get_component_config("response_synthesis")
        synthesis_model = synthesis_config["model_name"]
        state.model_used = synthesis_model  # persist for analytics

        # Emit LLM generating event for Admin Jarvis monitoring
        llm_start_time = time.time()
        if EVENTS_AVAILABLE and state.session_id:
            await emit_llm_generating(
                session_id=state.session_id,
                model=synthesis_model,
                interface=state.interface_type
            )

        result = await llm_router.generate(
            model=synthesis_model,
            prompt=full_prompt,
            temperature=state.temperature,
            system_prompt=_component_system_prompt(synthesis_config),
            request_id=state.request_id,
            session_id=state.session_id,
            user_id=state.mode,
            zone=state.room,
            intent=state.intent.value if state.intent else None,
            stage="synthesize"
        )

        state.answer = result.get("response", "")

        # Capture token metrics for frontend display
        llm_duration = time.time() - llm_start_time
        state.llm_tokens = result.get("eval_count", 0)
        if state.llm_tokens > 0 and llm_duration > 0:
            state.llm_tokens_per_second = state.llm_tokens / llm_duration
        else:
            state.llm_tokens_per_second = 0.0

        # Track LLM call in timing tracker
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "synthesize", "llm_inference", llm_duration)
            state.timing_tracker.record_llm_call("synthesize", synthesis_model, state.llm_tokens, int(llm_duration * 1000))

        # Emit LLM complete event
        if EVENTS_AVAILABLE and state.session_id:
            llm_duration_ms = int((time.time() - llm_start_time) * 1000)
            await emit_llm_complete(
                session_id=state.session_id,
                model=synthesis_model,
                tokens=result.get("tokens", 0),
                duration_ms=llm_duration_ms,
                interface=state.interface_type
            )

        # Log data attribution for debugging (not shown to user)
        if state.citations:
            logger.debug(f"Citations: {', '.join(set(state.citations))}")

        logger.info(f"Synthesized response using {state.model_tier}")

        # SMS Integration: Detect textable content in response
        # Only offer SMS for voice interface when response contains textable info
        if state.interface_type == "voice" and state.answer:
            try:
                should_offer, detected_items, reason = detect_textable_content(state.answer)
                if should_offer and detected_items:
                    state.offer_sms = True
                    state.sms_content_type = detected_items[0].content_type  # Primary content type
                    state.sms_content = extract_sms_content(state.answer, detected_items)
                    logger.info(
                        f"SMS content detected: type={state.sms_content_type}, "
                        f"reason='{reason}', offer_sms=True"
                    )
            except Exception as sms_err:
                logger.warning(f"SMS content detection failed: {sms_err}")
                # Non-critical - continue without SMS offer

        # Store conversation context for follow-up queries
        # This enables "what about tomorrow?" for weather, "how about the Lakers?" for sports, etc.
        if state.session_id and state.answer and state.intent:
            try:
                # Extract entities based on intent type
                context_entities = {}
                context_params = {}

                if state.intent == IntentCategory.WEATHER:
                    # Extract location from query or use default
                    context_entities["location"] = state.entities.get("location", DEFAULT_CITY)
                    context_entities["query_type"] = "weather"
                    if state.retrieved_data:
                        context_params["last_data"] = state.retrieved_data

                elif state.intent == IntentCategory.SPORTS:
                    # Extract team/sport info
                    context_entities["team"] = state.entities.get("team")
                    context_entities["sport"] = state.entities.get("sport")
                    context_entities["query_type"] = "sports"

                elif state.intent == IntentCategory.DINING:
                    # Extract cuisine/location preferences
                    context_entities["cuisine"] = state.entities.get("cuisine")
                    context_entities["location"] = state.entities.get("location", DEFAULT_CITY)
                    context_entities["query_type"] = "dining"

                elif state.intent == IntentCategory.NEWS:
                    # Extract topic
                    context_entities["topic"] = state.entities.get("topic")
                    context_entities["query_type"] = "news"

                elif state.intent == IntentCategory.EVENTS:
                    # Extract event type/location
                    context_entities["event_type"] = state.entities.get("event_type")
                    context_entities["location"] = state.entities.get("location", DEFAULT_CITY)
                    context_entities["query_type"] = "events"

                elif state.intent == IntentCategory.STREAMING:
                    # Extract movie/show info
                    context_entities["title"] = state.entities.get("title")
                    context_entities["query_type"] = "streaming"

                elif state.intent == IntentCategory.STOCKS:
                    # Extract stock symbol
                    context_entities["symbol"] = state.entities.get("symbol")
                    context_entities["query_type"] = "stocks"

                elif state.intent == IntentCategory.FLIGHTS:
                    # Extract flight info
                    context_entities["origin"] = state.entities.get("origin")
                    context_entities["destination"] = state.entities.get("destination")
                    context_entities["query_type"] = "flights"

                elif state.intent == IntentCategory.DIRECTIONS:
                    # Extract directions info
                    context_entities["origin"] = state.entities.get("origin")
                    context_entities["destination"] = state.entities.get("destination")
                    context_entities["travel_mode"] = state.entities.get("travel_mode", "driving")
                    context_entities["query_type"] = "directions"

                # Store context for all RAG-based intents
                await store_conversation_context(
                    session_id=state.session_id,
                    intent=state.intent.value,
                    query=state.query,
                    entities=context_entities,
                    parameters=context_params,
                    response=state.answer or "",  # Store full response for conversation continuity
                    ttl=300  # 5 minute TTL
                )
            except Exception as ctx_err:
                logger.warning(f"Failed to store synthesis context: {ctx_err}")

    except Exception as e:
        logger.error(f"Synthesis error: {e}", exc_info=True)
        state.answer = "I apologize, but I'm having trouble generating a response. Please try again."
        state.error = f"Synthesis failed: {str(e)}"

    synthesize_duration = time.time() - start
    state.node_timings["synthesize"] = synthesize_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "synthesize", synthesize_duration)
    return state
