"""
Provider routing based on query intent.

Routes queries to appropriate search provider sets based on classified intent.
"""

from typing import Dict, List, Optional
import logging
import os
import asyncio

from .base import SearchProvider
from .duckduckgo import DuckDuckGoProvider
from .brave import BraveSearchProvider
from .searxng import SearXNGProvider
from .ticketmaster import TicketmasterProvider
from .eventbrite import EventbriteProvider
from shared.admin_config import get_admin_client

logger = logging.getLogger(__name__)


class ProviderRouter:
    """
    Routes queries to appropriate provider sets based on intent.

    Intent-based routing ensures:
    - Event queries use event-specific APIs (Ticketmaster, Eventbrite)
    - General queries use general web search (DuckDuckGo, Brave)
    - No wasted API calls to irrelevant providers
    """

    # Intent-to-provider mapping
    # Each intent gets a list of provider names to use
    INTENT_PROVIDER_SETS: Dict[str, List[str]] = {
        "event_search": [
            "ticketmaster",    # Official event data
            "eventbrite",      # Local community events
            "duckduckgo",      # General web search backup
            "brave",           # Additional web search coverage
            "searxng"          # Metasearch aggregator (multiple engines)
        ],
        "general": [
            "duckduckgo",      # Free unlimited
            "brave",           # 2,000/month free
            "searxng"          # Metasearch aggregator (multiple engines)
        ],
        "news": [
            "brave",           # Excellent news search
            "duckduckgo",      # General news coverage
            "searxng"          # Metasearch aggregator (multiple engines)
        ],
        "local_business": [
            "brave",           # Good local search
            "duckduckgo",      # General search
            "searxng"          # Metasearch aggregator (multiple engines)
        ],
        "sports": [
            "duckduckgo",      # Primary sports search
            "brave",           # Sports news coverage
            "searxng"          # Metasearch aggregator (multiple engines)
        ],
        "weather": [
            "duckduckgo",      # Weather information
            "brave",           # Weather coverage
            "searxng"          # Metasearch aggregator (multiple engines)
        ]
    }

    # Intents that should be handled by RAG services, not web search
    RAG_INTENTS = {"weather", "sports"}

    def __init__(
        self,
        ticketmaster_api_key: Optional[str] = None,
        eventbrite_api_key: Optional[str] = None,
        brave_api_key: Optional[str] = None,
        searxng_base_url: Optional[str] = None,
        enable_ticketmaster: bool = True,
        enable_eventbrite: bool = True,
        enable_brave: bool = True,
        enable_duckduckgo: bool = True,
        enable_searxng: bool = True
    ):
        """
        Initialize provider router.

        Args:
            ticketmaster_api_key: Ticketmaster API key
            eventbrite_api_key: Eventbrite API key
            brave_api_key: Brave Search API key
            searxng_base_url: SearXNG instance base URL (defaults to internal cluster service)
            enable_ticketmaster: Enable Ticketmaster provider
            enable_eventbrite: Enable Eventbrite provider
            enable_brave: Enable Brave Search provider
            enable_duckduckgo: Enable DuckDuckGo provider
            enable_searxng: Enable SearXNG metasearch provider
        """
        self.all_providers: Dict[str, SearchProvider] = {}

        # Initialize DuckDuckGo (no API key needed)
        if enable_duckduckgo:
            try:
                self.all_providers["duckduckgo"] = DuckDuckGoProvider()
                logger.info("Initialized DuckDuckGo provider")
            except Exception as e:
                logger.error(f"Failed to initialize DuckDuckGo provider: {e}")

        # Initialize SearXNG (no API key needed)
        if enable_searxng:
            try:
                self.all_providers["searxng"] = SearXNGProvider(base_url=searxng_base_url)
                logger.info("Initialized SearXNG provider")
            except Exception as e:
                logger.error(f"Failed to initialize SearXNG provider: {e}")

        # Initialize Brave Search
        if enable_brave and brave_api_key:
            try:
                self.all_providers["brave"] = BraveSearchProvider(api_key=brave_api_key)
                logger.info("Initialized Brave Search provider")
            except Exception as e:
                logger.error(f"Failed to initialize Brave Search provider: {e}")
        elif enable_brave and not brave_api_key:
            logger.warning("Brave Search enabled but no API key provided")

        # Initialize Ticketmaster
        if enable_ticketmaster and ticketmaster_api_key:
            try:
                self.all_providers["ticketmaster"] = TicketmasterProvider(api_key=ticketmaster_api_key)
                logger.info("Initialized Ticketmaster provider")
            except Exception as e:
                logger.error(f"Failed to initialize Ticketmaster provider: {e}")
        elif enable_ticketmaster and not ticketmaster_api_key:
            logger.warning("Ticketmaster enabled but no API key provided")

        # Initialize Eventbrite
        if enable_eventbrite and eventbrite_api_key:
            try:
                self.all_providers["eventbrite"] = EventbriteProvider(api_key=eventbrite_api_key)
                logger.info("Initialized Eventbrite provider")
            except Exception as e:
                logger.error(f"Failed to initialize Eventbrite provider: {e}")
        elif enable_eventbrite and not eventbrite_api_key:
            logger.warning("Eventbrite enabled but no API key provided")

        logger.info(f"Provider router initialized with {len(self.all_providers)} providers: {list(self.all_providers.keys())}")

        # Database routing cache (loaded lazily on first use)
        self._db_provider_routing: Optional[Dict[str, List[str]]] = None
        self._db_routing_config: Optional[Dict[str, Dict]] = None
        self._db_load_attempted = False
        self._db_load_task: Optional[asyncio.Task] = None

    def _ensure_db_loading_started(self):
        """
        Ensure database loading has been started (non-blocking).
        Creates a background task on first call to load routing from database.
        """
        if not self._db_load_attempted:
            self._db_load_attempted = True
            try:
                # Try to get the current event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Create background task to load routing
                    self._db_load_task = loop.create_task(self._load_db_routing_async())
                    logger.info("Started background task to load routing configuration from database")
                else:
                    logger.info("No running event loop, using hardcoded routing configuration")
            except RuntimeError:
                logger.info("No event loop available, using hardcoded routing configuration")

    async def _load_db_routing_async(self):
        """Background task to load routing configuration from database."""
        try:
            result = await self._fetch_db_routing()
            if result:
                self._db_provider_routing, self._db_routing_config = result
                logger.info(
                    f"Loaded routing config from database: "
                    f"{len(self._db_provider_routing or {})} provider mappings, "
                    f"{len(self._db_routing_config or {})} routing configs"
                )
            else:
                logger.info("Database routing configuration not available, using hardcoded fallback")
        except Exception as e:
            logger.warning(f"Failed to load routing from database: {e}. Using hardcoded fallback.")

    async def _fetch_db_routing(self) -> Optional[tuple]:
        """
        Fetch routing configuration from Admin API.
        Returns (provider_routing, routing_config) or None on error.
        """
        try:
            client = get_admin_client()
            provider_routing = await client.get_provider_routing()
            routing_config = await client.get_intent_routing()

            if not provider_routing and not routing_config:
                return None

            return (provider_routing or {}, routing_config or {})

        except Exception as e:
            logger.warning(f"Error fetching DB routing: {e}")
            return None

    def get_providers_for_intent(self, intent: str) -> List[SearchProvider]:
        """
        Get provider instances for given intent.
        Loads from database if available, otherwise uses hardcoded configuration.

        Args:
            intent: Query intent type

        Returns:
            List of SearchProvider instances appropriate for this intent
        """
        # Ensure database loading has started (lazy loading)
        self._ensure_db_loading_started()

        # Try database configuration first
        provider_names = None
        if self._db_provider_routing and intent in self._db_provider_routing:
            provider_names = self._db_provider_routing[intent]
            logger.debug(f"Using DB provider routing for intent '{intent}': {provider_names}")
        else:
            # Fall back to hardcoded configuration
            provider_names = self.INTENT_PROVIDER_SETS.get(intent, ["duckduckgo"])
            logger.debug(f"Using hardcoded provider routing for intent '{intent}': {provider_names}")

        # Filter to only available providers
        providers = []
        for name in provider_names:
            if name in self.all_providers:
                providers.append(self.all_providers[name])
            else:
                logger.warning(f"Provider '{name}' requested for intent '{intent}' but not available")

        if not providers:
            # Fallback to DuckDuckGo if no providers available
            logger.warning(f"No providers available for intent '{intent}', falling back to DuckDuckGo")
            if "duckduckgo" in self.all_providers:
                providers = [self.all_providers["duckduckgo"]]
            else:
                logger.error("DuckDuckGo provider not available - no search providers!")

        logger.info(f"Selected {len(providers)} providers for intent '{intent}': {[p.name for p in providers]}")
        return providers

    def should_use_rag(self, intent: str) -> bool:
        """
        Check if intent should be handled by RAG service instead of web search.
        Loads from database if available, otherwise uses hardcoded configuration.

        Args:
            intent: Classified intent

        Returns:
            True if RAG should handle, False if web search should handle
        """
        # Ensure database loading has started (lazy loading)
        self._ensure_db_loading_started()

        # Try database configuration first
        is_rag = False
        if self._db_routing_config and intent in self._db_routing_config:
            is_rag = self._db_routing_config[intent].get("use_rag", False)
            logger.debug(f"Using DB routing config for intent '{intent}': use_rag={is_rag}")
        else:
            # Fall back to hardcoded configuration
            is_rag = intent in self.RAG_INTENTS
            logger.debug(f"Using hardcoded RAG config for intent '{intent}': use_rag={is_rag}")

        if is_rag:
            logger.info(f"Intent '{intent}' should be handled by RAG service")
        return is_rag

    def get_available_providers(self) -> List[str]:
        """
        Get list of available provider names.

        Returns:
            List of provider names that were successfully initialized
        """
        return list(self.all_providers.keys())

    @classmethod
    async def from_environment(cls) -> "ProviderRouter":
        """
        Create ProviderRouter from environment variables and admin database.

        Fetches API keys from admin database first, falls back to environment variables.

        Environment variables:
        - TICKETMASTER_API_KEY: Ticketmaster API key (fallback)
        - EVENTBRITE_API_KEY: Eventbrite API key (fallback)
        - BRAVE_SEARCH_API_KEY: Brave Search API key (fallback)
        - SEARXNG_BASE_URL: SearXNG instance base URL (default: cluster-local)
        - ENABLE_TICKETMASTER: Enable Ticketmaster (default: true)
        - ENABLE_EVENTBRITE: Enable Eventbrite (default: true)
        - ENABLE_BRAVE_SEARCH: Enable Brave Search (default: true)
        - ENABLE_DUCKDUCKGO: Enable DuckDuckGo (default: true)
        - ENABLE_SEARXNG: Enable SearXNG metasearch (default: true)

        Returns:
            Configured ProviderRouter instance
        """
        # Try to fetch API keys from admin database
        admin_client = get_admin_client()

        # Brave Search API key
        brave_api_key = os.getenv("BRAVE_SEARCH_API_KEY")
        try:
            brave_key_data = await admin_client.get_external_api_key("brave-search")
            if brave_key_data and brave_key_data.get("api_key"):
                brave_api_key = brave_key_data["api_key"]
                logger.info("brave_api_key_loaded_from_database")
        except Exception as e:
            logger.warning(f"Failed to fetch Brave API key from database: {e}. Using environment variable.")

        # Ticketmaster API key
        ticketmaster_api_key = os.getenv("TICKETMASTER_API_KEY")
        try:
            ticketmaster_key_data = await admin_client.get_external_api_key("api-ticketmaster")
            if ticketmaster_key_data and ticketmaster_key_data.get("api_key"):
                ticketmaster_api_key = ticketmaster_key_data["api_key"]
                logger.info("ticketmaster_api_key_loaded_from_database")
        except Exception as e:
            logger.warning(f"Failed to fetch Ticketmaster API key from database: {e}. Using environment variable.")

        # Eventbrite API key
        eventbrite_api_key = os.getenv("EVENTBRITE_API_KEY")
        try:
            eventbrite_key_data = await admin_client.get_external_api_key("api-eventbrite")
            if eventbrite_key_data and eventbrite_key_data.get("api_key"):
                eventbrite_api_key = eventbrite_key_data["api_key"]
                logger.info("eventbrite_api_key_loaded_from_database")
        except Exception as e:
            logger.warning(f"Failed to fetch Eventbrite API key from database: {e}. Using environment variable.")

        # SearXNG base URL (no API key needed)
        searxng_base_url = os.getenv("SEARXNG_BASE_URL")  # Defaults to cluster-local in provider

        return cls(
            ticketmaster_api_key=ticketmaster_api_key,
            eventbrite_api_key=eventbrite_api_key,
            brave_api_key=brave_api_key,
            searxng_base_url=searxng_base_url,
            enable_ticketmaster=os.getenv("ENABLE_TICKETMASTER", "true").lower() == "true",
            enable_eventbrite=os.getenv("ENABLE_EVENTBRITE", "true").lower() == "true",
            enable_brave=os.getenv("ENABLE_BRAVE_SEARCH", "true").lower() == "true",
            enable_duckduckgo=os.getenv("ENABLE_DUCKDUCKGO", "true").lower() == "true",
            enable_searxng=os.getenv("ENABLE_SEARXNG", "true").lower() == "true"
        )

    async def close_all(self):
        """Close all provider HTTP clients."""
        for provider in self.all_providers.values():
            try:
                await provider.close()
            except Exception as e:
                logger.error(f"Error closing provider {provider.name}: {e}")
