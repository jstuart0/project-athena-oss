"""
Parallel search orchestrator with intent-based routing.

Executes multiple search providers in parallel based on query intent.
"""

import asyncio
from typing import List, Dict, Optional, Tuple
import logging
import os

from .base import SearchResult, SearchProvider
from .intent_classifier import IntentClassifier
from .provider_router import ProviderRouter

logger = logging.getLogger(__name__)


class ParallelSearchEngine:
    """
    Orchestrates parallel execution of multiple search providers with intent-based routing.

    Features:
    - Classifies query intent (event_search, general, news, etc.)
    - Routes to appropriate provider set based on intent
    - Launches searches in parallel (not sequential)
    - Handles timeouts and failures gracefully
    - Aggregates results from all providers
    """

    def __init__(
        self,
        intent_classifier: IntentClassifier,
        provider_router: ProviderRouter,
        timeout: float = 3.0
    ):
        """
        Initialize parallel search engine with intent-based routing.

        Args:
            intent_classifier: Intent classifier for query routing
            provider_router: Provider router for intent-to-provider mapping
            timeout: Maximum wait time for all providers (seconds)
        """
        self.intent_classifier = intent_classifier
        self.provider_router = provider_router
        self.timeout = timeout

        logger.info(f"ParallelSearchEngine initialized with intent-based routing")

    async def search(
        self,
        query: str,
        location: Optional[str] = "Baltimore, MD",
        limit_per_provider: int = 5,
        force_search: bool = False,
        **kwargs
    ) -> Tuple[str, List[SearchResult]]:
        """
        Execute intent-based parallel search.

        Args:
            query: Search query
            location: Location for search (used by event providers)
            limit_per_provider: Max results per provider
            force_search: Force web search even for RAG intents (for fallback mode)
            **kwargs: Provider-specific parameters

        Returns:
            Tuple of (intent, results) where:
            - intent: Classified query intent
            - results: Aggregated list of SearchResult objects from selected providers
        """
        # Step 1: Classify query intent
        intent, confidence = self.intent_classifier.classify_with_confidence(query)
        logger.info(f"Classified query intent: '{intent}' (confidence: {confidence:.2f}) for query: '{query}'")

        # Step 2: Check if this intent should use RAG instead of web search
        # UNLESS we're in force_search mode (fallback from failed RAG)
        if not force_search and self.provider_router.should_use_rag(intent):
            logger.info(f"Intent '{intent}' handled by RAG service, skipping web search")
            return (intent, [])

        # Step 3: Get providers for this intent
        providers = self.provider_router.get_providers_for_intent(intent)

        if not providers:
            logger.error(f"No providers available for intent '{intent}'")
            return (intent, [])

        logger.info(f"Starting parallel search: query='{query}', intent='{intent}', providers={[p.name for p in providers]}")

        # Step 4: Launch all provider searches in parallel
        tasks = []
        for provider in providers:
            task = asyncio.create_task(
                self._search_with_timeout(provider, query, location, limit_per_provider, **kwargs)
            )
            tasks.append(task)

        # Step 5: Wait for all tasks with global timeout
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.timeout,
                return_when=asyncio.ALL_COMPLETED
            )

            # Cancel any still-pending tasks
            for task in pending:
                task.cancel()
                logger.warning(f"Search task timed out and was cancelled")

        except Exception as e:
            logger.error(f"Error waiting for search tasks: {e}")
            done = []

        # Step 6: Gather results from completed tasks
        all_results = []
        provider_results = {}

        for task in done:
            try:
                provider_name, results = await task
                provider_results[provider_name] = results
                all_results.extend(results)
                logger.info(f"Provider '{provider_name}' returned {len(results)} results")
            except Exception as e:
                logger.warning(f"Failed to get results from task: {e}")

        logger.info(f"Parallel search completed: {len(all_results)} total results from {len(provider_results)} providers")

        # Log provider performance
        for provider_name, results in provider_results.items():
            logger.info(f"  - {provider_name}: {len(results)} results")

        return (intent, all_results)

    async def _search_with_timeout(
        self,
        provider: SearchProvider,
        query: str,
        location: Optional[str],
        limit: int,
        **kwargs
    ) -> tuple[str, List[SearchResult]]:
        """
        Execute search for a single provider with error handling.

        Args:
            provider: SearchProvider instance
            query: Search query
            location: Location for search
            limit: Max results
            **kwargs: Provider-specific parameters

        Returns:
            Tuple of (provider_name, results)
        """
        provider_name = provider.name
        try:
            logger.info(f"Starting search for provider: {provider_name}")
            results = await provider.search(query, location=location, limit=limit, **kwargs)
            logger.info(f"Provider '{provider_name}' completed successfully: {len(results)} results")
            return (provider_name, results)

        except asyncio.CancelledError:
            logger.warning(f"Provider '{provider_name}' search was cancelled")
            return (provider_name, [])

        except Exception as e:
            logger.warning(f"Provider '{provider_name}' search failed: {e}")
            return (provider_name, [])

    async def close_all(self):
        """Close all provider HTTP clients."""
        await self.provider_router.close_all()

    @classmethod
    async def from_environment(cls, **kwargs) -> "ParallelSearchEngine":
        """
        Create ParallelSearchEngine from environment variables and admin database.

        Environment variables:
            TICKETMASTER_API_KEY: Ticketmaster API key (fallback)
            EVENTBRITE_API_KEY: Eventbrite API key (fallback)
            BRAVE_SEARCH_API_KEY: Brave Search API key (fallback)
            SEARCH_TIMEOUT: Global timeout in seconds (default 3.0)
            ENABLE_TICKETMASTER: Enable Ticketmaster provider (default: true)
            ENABLE_EVENTBRITE: Enable Eventbrite provider (default: true)
            ENABLE_BRAVE_SEARCH: Enable Brave Search provider (default: true)
            ENABLE_DUCKDUCKGO: Enable DuckDuckGo provider (default: true)

        Args:
            **kwargs: Override default settings

        Returns:
            Configured ParallelSearchEngine instance with intent-based routing
        """
        timeout = float(os.getenv("SEARCH_TIMEOUT", "3.0"))

        # Initialize intent classifier
        intent_classifier = IntentClassifier()

        # Initialize provider router from environment and admin database
        provider_router = await ProviderRouter.from_environment()

        return cls(
            intent_classifier=intent_classifier,
            provider_router=provider_router,
            timeout=kwargs.get("timeout", timeout)
        )
