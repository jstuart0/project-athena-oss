"""
Intent Discovery Module

Discovers and clusters novel user intents that don't match known categories.
Uses semantic embeddings to group similar intents together.

When the classifier has low confidence:
1. Generate a canonical intent name via LLM
2. Generate embedding for semantic matching
3. Search for similar existing novel intents
4. Cluster with existing or create new
"""

import os
import json
import hashlib
import asyncio
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import structlog
import httpx

from shared.admin_config import get_admin_client

logger = structlog.get_logger()

# Fallback model if database lookup fails
_FALLBACK_MODEL = os.getenv("ATHENA_INTENT_DISCOVERY_MODEL", "llama3.2:3b")


async def get_intent_discovery_model() -> str:
    """Get intent discovery model from admin config, with fallback."""
    try:
        admin_client = get_admin_client()
        config = await admin_client.get_component_model("intent_discovery")
        if config and config.get("enabled"):
            return config.get("model_name", _FALLBACK_MODEL)
    except Exception as e:
        logger.warning("intent_discovery_model_lookup_failed", error=str(e))
    return _FALLBACK_MODEL


# =============================================================================
# Configuration
# =============================================================================

INTENT_DISCOVERY_CONFIG = {
    "enabled": True,
    "confidence_threshold": 0.7,      # Below this, trigger discovery
    "similarity_threshold": 0.85,     # Above this, cluster together
    "max_sample_queries": 10,         # Per emerging intent
    "min_count_for_review": 5,        # Alert admin after N occurrences
}


# =============================================================================
# Embedding Service
# =============================================================================

class IntentEmbedder:
    """
    Generates semantic embeddings for intent clustering.

    Uses sentence-transformers all-MiniLM-L6-v2 model (384 dimensions).
    Falls back to a simple hash-based approach if model unavailable.
    """

    def __init__(self):
        self.model = None
        self.model_name = "all-MiniLM-L6-v2"
        self._initialized = False

    async def initialize(self) -> bool:
        """Lazy-load the embedding model."""
        if self._initialized:
            return self.model is not None

        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(self.model_name)
            self._initialized = True
            logger.info("intent_embedder_initialized", model=self.model_name)
            return True
        except ImportError:
            logger.warning("sentence_transformers_not_installed",
                         message="Install with: pip install sentence-transformers")
            self._initialized = True
            return False
        except Exception as e:
            logger.error("intent_embedder_init_failed", error=str(e))
            self._initialized = True
            return False

    def embed(self, text: str) -> List[float]:
        """Generate embedding for text."""
        if self.model is None:
            # Fallback: return hash-based pseudo-embedding
            return self._hash_embedding(text)
        return self.model.encode(text).tolist()

    def embed_intent(self, canonical_name: str, description: str) -> List[float]:
        """Generate embedding optimized for intent matching."""
        combined = f"{canonical_name.replace('_', ' ')}: {description}"
        return self.embed(combined)

    def _hash_embedding(self, text: str, dim: int = 384) -> List[float]:
        """
        Fallback embedding using hash function.
        Not ideal for semantic similarity but provides deterministic output.
        """
        # Create a deterministic hash-based vector
        hash_bytes = hashlib.sha512(text.lower().encode()).digest()
        # Convert to floats in [-1, 1] range
        embedding = []
        for i in range(dim):
            byte_val = hash_bytes[i % len(hash_bytes)]
            embedding.append((byte_val / 127.5) - 1.0)
        return embedding

    def cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Compute cosine similarity between two embeddings."""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# Global embedder instance
_embedder: Optional[IntentEmbedder] = None


async def get_embedder() -> IntentEmbedder:
    """Get or create the global embedder instance."""
    global _embedder
    if _embedder is None:
        _embedder = IntentEmbedder()
        await _embedder.initialize()
    return _embedder


# =============================================================================
# Novel Intent Generation (LLM)
# =============================================================================

NOVEL_INTENT_PROMPT = """The user asked: "{query}"

This query doesn't match any known service categories. Generate a canonical intent name for this request.

Requirements:
1. Use snake_case (e.g., package_tracking, medical_appointment)
2. Be specific but not too narrow (good: "package_tracking", bad: "ups_package_tracking")
3. Focus on the user's goal, not the method

Also provide:
- A brief description of what the user wants
- A suggested category (utility, commerce, health, entertainment, travel, home, finance, education, other)
- Potential API sources that could power this feature

Respond in JSON only, no other text:
{{
    "canonical_name": "intent_name",
    "display_name": "Human Readable Name",
    "description": "Brief description of what the user wants",
    "suggested_category": "category",
    "suggested_api_sources": ["API 1", "API 2"]
}}"""


async def generate_novel_intent(
    query: str,
    llm_router,
    model: str = None
) -> Optional[Dict[str, Any]]:
    """
    Generate a canonical intent name for a novel query using LLM.

    Args:
        query: The user query that couldn't be classified
        llm_router: The LLM router instance
        model: Optional model override

    Returns:
        Dict with canonical_name, display_name, description, etc.
    """
    try:
        prompt = NOVEL_INTENT_PROMPT.format(query=query)

        # Get model from admin config if not provided
        if not model:
            model = await get_intent_discovery_model()

        result = await llm_router.generate(
            model=model,
            prompt=prompt,
            temperature=0.3,
        )

        response_text = result.get("response", "")

        # Parse JSON response
        # Try to find JSON in response (handle markdown code blocks)
        if "```json" in response_text:
            json_start = response_text.find("```json") + 7
            json_end = response_text.find("```", json_start)
            response_text = response_text[json_start:json_end].strip()
        elif "```" in response_text:
            json_start = response_text.find("```") + 3
            json_end = response_text.find("```", json_start)
            response_text = response_text[json_start:json_end].strip()

        intent_data = json.loads(response_text)

        # Validate required fields
        if not intent_data.get("canonical_name"):
            logger.warning("novel_intent_missing_name", query=query)
            return None

        # Normalize canonical_name
        intent_data["canonical_name"] = (
            intent_data["canonical_name"]
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )

        logger.info("novel_intent_generated",
                   canonical_name=intent_data["canonical_name"],
                   query=query[:50])

        return intent_data

    except json.JSONDecodeError as e:
        logger.error("novel_intent_json_parse_failed", error=str(e), query=query[:50])
        return None
    except Exception as e:
        logger.error("novel_intent_generation_failed", error=str(e), query=query[:50])
        return None


# =============================================================================
# Database Operations via Admin API
# =============================================================================

async def find_similar_emerging_intent(
    embedding: List[float],
    admin_api_url: str,
    threshold: float = 0.85
) -> Optional[Dict[str, Any]]:
    """
    Search for existing novel intents similar to this one.

    Uses admin API to fetch all active emerging intents and computes
    similarity in Python (simpler than requiring pgvector).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{admin_api_url}/api/internal/emerging-intents",
                params={"status": "discovered,reviewed"}
            )

            if response.status_code != 200:
                logger.warning("emerging_intents_fetch_failed", status=response.status_code)
                return None

            intents = response.json()

            if not intents:
                return None

            embedder = await get_embedder()

            # Find most similar
            best_match = None
            best_similarity = 0.0

            for intent in intents:
                if not intent.get("embedding"):
                    continue

                similarity = embedder.cosine_similarity(embedding, intent["embedding"])

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = intent

            if best_match and best_similarity >= threshold:
                logger.info("similar_intent_found",
                          canonical_name=best_match["canonical_name"],
                          similarity=best_similarity)
                return best_match

            return None

    except Exception as e:
        logger.error("similar_intent_search_failed", error=str(e))
        return None


async def create_emerging_intent(
    canonical_name: str,
    display_name: str,
    description: str,
    embedding: List[float],
    suggested_category: str,
    suggested_api_sources: List[str],
    sample_query: str,
    admin_api_url: str
) -> Optional[int]:
    """Create a new emerging intent record."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{admin_api_url}/api/internal/emerging-intents",
                json={
                    "canonical_name": canonical_name,
                    "display_name": display_name,
                    "description": description,
                    "embedding": embedding,
                    "suggested_category": suggested_category,
                    "suggested_api_sources": suggested_api_sources,
                    "sample_queries": [sample_query]
                }
            )

            if response.status_code in (200, 201):
                data = response.json()
                logger.info("emerging_intent_created",
                          id=data.get("id"),
                          canonical_name=canonical_name)
                return data.get("id")
            else:
                logger.error("emerging_intent_create_failed",
                           status=response.status_code,
                           detail=response.text[:200])
                return None

    except Exception as e:
        logger.error("emerging_intent_create_error", error=str(e))
        return None


async def increment_intent_count(
    intent_id: int,
    sample_query: str,
    admin_api_url: str
) -> bool:
    """Increment occurrence count and add sample query."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{admin_api_url}/api/internal/emerging-intents/{intent_id}/increment",
                json={"sample_query": sample_query}
            )

            if response.status_code == 200:
                logger.info("emerging_intent_incremented", id=intent_id)
                return True
            else:
                logger.warning("emerging_intent_increment_failed",
                             id=intent_id, status=response.status_code)
                return False

    except Exception as e:
        logger.error("emerging_intent_increment_error", error=str(e))
        return False


async def record_intent_metric(
    intent: str,
    confidence: float,
    raw_query: str,
    session_id: str,
    mode: str,
    room: str,
    request_id: str,
    processing_time_ms: int,
    is_novel: bool = False,
    emerging_intent_id: Optional[int] = None,
    complexity: str = "simple",
    admin_api_url: str = None
) -> bool:
    """Record an intent classification metric."""
    if not admin_api_url:
        return False

    try:
        query_hash = hashlib.md5(raw_query.lower().encode()).hexdigest()

        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                f"{admin_api_url}/api/internal/intent-metrics",
                json={
                    "intent": intent,
                    "confidence": confidence,
                    "complexity": complexity,
                    "is_novel": is_novel,
                    "emerging_intent_id": emerging_intent_id,
                    "raw_query": raw_query,
                    "query_hash": query_hash,
                    "session_id": session_id,
                    "mode": mode,
                    "room": room,
                    "request_id": request_id,
                    "processing_time_ms": processing_time_ms
                }
            )

            return response.status_code in (200, 201)

    except Exception as e:
        logger.warning("intent_metric_record_failed", error=str(e))
        return False


# =============================================================================
# Main Discovery Flow
# =============================================================================

@dataclass
class IntentDiscoveryResult:
    """Result of intent discovery process."""
    is_novel: bool
    canonical_name: Optional[str]
    emerging_intent_id: Optional[int]
    confidence_boost: float  # How much to boost confidence if clustered


async def discover_intent(
    query: str,
    current_intent: str,
    current_confidence: float,
    llm_router,
    admin_api_url: str,
    config: Dict[str, Any] = None
) -> IntentDiscoveryResult:
    """
    Main intent discovery flow.

    Called when classification confidence is low to discover novel intents.

    Args:
        query: The user query
        current_intent: What the classifier returned
        current_confidence: Classification confidence
        llm_router: LLM router for generating intent names
        admin_api_url: Admin API base URL
        config: Optional config overrides

    Returns:
        IntentDiscoveryResult with discovery information
    """
    cfg = {**INTENT_DISCOVERY_CONFIG, **(config or {})}

    # Check if discovery is enabled and needed
    if not cfg["enabled"]:
        return IntentDiscoveryResult(
            is_novel=False,
            canonical_name=None,
            emerging_intent_id=None,
            confidence_boost=0.0
        )

    if current_confidence > cfg["confidence_threshold"]:
        return IntentDiscoveryResult(
            is_novel=False,
            canonical_name=None,
            emerging_intent_id=None,
            confidence_boost=0.0
        )

    # Generate canonical intent name via LLM
    novel_intent = await generate_novel_intent(query, llm_router)

    if not novel_intent:
        logger.warning("intent_discovery_failed_to_generate", query=query[:50])
        return IntentDiscoveryResult(
            is_novel=False,
            canonical_name=None,
            emerging_intent_id=None,
            confidence_boost=0.0
        )

    # Generate embedding
    embedder = await get_embedder()
    embedding = embedder.embed_intent(
        novel_intent["canonical_name"],
        novel_intent.get("description", "")
    )

    # Search for similar existing novel intent
    similar = await find_similar_emerging_intent(
        embedding,
        admin_api_url,
        threshold=cfg["similarity_threshold"]
    )

    if similar:
        # Cluster with existing intent
        await increment_intent_count(similar["id"], query, admin_api_url)

        logger.info("intent_clustered",
                   canonical_name=similar["canonical_name"],
                   count=similar.get("occurrence_count", 0) + 1)

        return IntentDiscoveryResult(
            is_novel=True,
            canonical_name=similar["canonical_name"],
            emerging_intent_id=similar["id"],
            confidence_boost=0.3  # Boost confidence when clustering
        )
    else:
        # Create new novel intent
        intent_id = await create_emerging_intent(
            canonical_name=novel_intent["canonical_name"],
            display_name=novel_intent.get("display_name", novel_intent["canonical_name"]),
            description=novel_intent.get("description", ""),
            embedding=embedding,
            suggested_category=novel_intent.get("suggested_category", "other"),
            suggested_api_sources=novel_intent.get("suggested_api_sources", []),
            sample_query=query,
            admin_api_url=admin_api_url
        )

        logger.info("novel_intent_created",
                   canonical_name=novel_intent["canonical_name"],
                   id=intent_id)

        return IntentDiscoveryResult(
            is_novel=True,
            canonical_name=novel_intent["canonical_name"],
            emerging_intent_id=intent_id,
            confidence_boost=0.0  # No boost for new discoveries
        )
