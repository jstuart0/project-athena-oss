"""
Lightweight intent classification for Gateway pre-routing.

Uses a fast LLM (qwen2.5:1.5b) for classification to route queries:
- SIMPLE: Greetings, time, basic Q&A -> Direct LLM response
- HOME: Device control -> HA API (via simple_commands)
- COMPLEX: Weather, dining, directions -> Full orchestrator

This allows simple queries to skip the full orchestrator pipeline,
reducing latency significantly for common interactions.
"""

import os
from typing import Literal, Optional
import httpx
import structlog

logger = structlog.get_logger("gateway.intent_prerouter")

IntentType = Literal["SIMPLE", "HOME", "COMPLEX"]

# Classification prompt - designed for fast, accurate routing
CLASSIFICATION_PROMPT = """Classify this voice query into one category:
- SIMPLE: Greetings, time questions, basic chitchat, thank you, goodbyes
- HOME: Turn on/off devices, set temperature, control home devices, lights
- COMPLEX: Weather, restaurants, directions, sports, news, anything requiring search or data

Query: "{query}"

Respond with only one word: SIMPLE, HOME, or COMPLEX"""

# Default Ollama URL
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# Fast model for classification
CLASSIFICATION_MODEL = "qwen2.5:1.5b"

# Response model for simple intents
SIMPLE_RESPONSE_MODEL = "qwen2.5:3b"


async def classify_intent(query: str, ollama_url: Optional[str] = None) -> IntentType:
    """
    Fast intent classification using small model.

    Uses qwen2.5:1.5b for ~100-200ms classification to determine
    the appropriate routing path for a query.

    Args:
        query: The user's voice query text
        ollama_url: Optional Ollama URL override

    Returns:
        Intent type: "SIMPLE", "HOME", or "COMPLEX"
    """
    url = ollama_url or OLLAMA_URL

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                f"{url}/api/generate",
                json={
                    "model": CLASSIFICATION_MODEL,
                    "prompt": CLASSIFICATION_PROMPT.format(query=query),
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 10
                    }
                }
            )

            if response.status_code == 200:
                result = response.json().get("response", "").strip().upper()

                # Extract just the intent word
                for intent in ["SIMPLE", "HOME", "COMPLEX"]:
                    if intent in result:
                        logger.debug("intent_classified", query=query[:50], intent=intent)
                        return intent

        # Default to COMPLEX if unclear (safest option)
        logger.warning("intent_classification_unclear", query=query[:50])
        return "COMPLEX"

    except Exception as e:
        # On error, default to full orchestrator (safest)
        logger.warning("intent_classification_error", error=str(e), query=query[:50])
        return "COMPLEX"


async def handle_simple_intent(
    query: str,
    ollama_url: Optional[str] = None
) -> str:
    """
    Generate response for simple intents using fast LLM.

    For greetings, time questions, and basic chitchat, we can
    respond directly without the full orchestrator pipeline.

    Args:
        query: The user's voice query
        ollama_url: Optional Ollama URL override

    Returns:
        Response text for the simple query
    """
    url = ollama_url or OLLAMA_URL

    prompt = f"""You are Athena, a helpful voice assistant. Give a brief, friendly response.
Keep your response to 1-2 sentences maximum.

User: {query}
Athena:"""

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{url}/api/generate",
                json={
                    "model": SIMPLE_RESPONSE_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.7,
                        "num_predict": 100
                    }
                }
            )

            if response.status_code == 200:
                answer = response.json().get("response", "").strip()
                if answer:
                    logger.info("simple_intent_handled", query=query[:50], response_length=len(answer))
                    return answer

        return "I'm not sure how to respond to that."

    except Exception as e:
        logger.warning("simple_intent_error", error=str(e), query=query[:50])
        return "I'm having trouble responding right now."
