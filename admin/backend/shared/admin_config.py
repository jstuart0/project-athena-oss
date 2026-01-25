"""
Admin Configuration Client

Allows services to fetch configuration and secrets from the admin API.
Uses service-to-service authentication with API key.
"""
import os
import time
import httpx
from typing import Optional, Dict, Any, List
import structlog

logger = structlog.get_logger()


class AdminConfigClient:
    """Client for fetching configuration from admin API."""

    def __init__(
        self,
        admin_url: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        """
        Initialize admin configuration client.

        Args:
            admin_url: Admin API URL (defaults to ADMIN_API_URL env var)
            api_key: Service API key (defaults to SERVICE_API_KEY env var)
        """
        self.admin_url = admin_url or os.getenv(
            "ADMIN_API_URL",
            ""  # Must be set via environment variable
        )
        if not self.admin_url:
            logger.warning(
                "admin_api_url_not_set",
                msg="ADMIN_API_URL not configured - admin features may not work"
            )
        self.api_key = api_key or os.getenv(
            "SERVICE_API_KEY",
            "dev-service-key-change-in-production"
        )
        # Reduced timeout from 10s to 3s - config/room calls should be fast
        # Critical paths like music playback compound multiple API calls
        self.client = httpx.AsyncClient(timeout=3.0)

        # Routing configuration cache (60-second TTL)
        self._cache_ttl = 60
        self._patterns_cache: Optional[Dict[str, List[str]]] = None
        self._patterns_cache_time = 0.0
        self._routing_cache: Optional[Dict[str, Dict]] = None
        self._routing_cache_time = 0.0
        self._providers_cache: Optional[Dict[str, List[str]]] = None
        self._providers_cache_time = 0.0

        # LLM backends and features cache
        self._llm_backends_cache: Optional[List[Dict[str, Any]]] = None
        self._llm_backends_cache_time = 0.0
        self._features_cache: Optional[Dict[str, bool]] = None
        self._features_cache_time = 0.0

        # Tool calling configuration cache
        self._tool_calling_settings_cache: Optional[Dict[str, Any]] = None
        self._tool_calling_settings_cache_time = 0.0
        self._enabled_tools_cache: Optional[List[Dict[str, Any]]] = None
        self._enabled_tools_cache_time = 0.0
        self._fallback_triggers_cache: Optional[List[Dict[str, Any]]] = None
        self._fallback_triggers_cache_time = 0.0

        # Base knowledge cache
        self._base_knowledge_cache: Optional[List[Dict[str, Any]]] = None
        self._base_knowledge_cache_time = 0.0

        # Component model assignment cache
        self._component_model_cache: Dict[str, Dict[str, Any]] = {}
        self._component_model_cache_time: Dict[str, float] = {}

        # Gateway config cache
        self._gateway_config_cache: Optional[Dict[str, Any]] = None
        self._gateway_config_cache_time: float = 0.0

        # Voice config cache
        self._voice_config_stt_cache: Optional[Dict[str, Any]] = None
        self._voice_config_stt_cache_time: float = 0.0
        self._voice_config_tts_cache: Optional[Dict[str, Any]] = None
        self._voice_config_tts_cache_time: float = 0.0
        self._voice_config_all_cache: Optional[Dict[str, Any]] = None
        self._voice_config_all_cache_time: float = 0.0
        self._voice_interface_cache: Dict[str, Dict[str, Any]] = {}
        self._voice_interface_cache_time: Dict[str, float] = {}

        # Escalation preset cache
        self._escalation_preset_cache: Optional[Dict[str, Any]] = None
        self._escalation_preset_cache_time: float = 0.0

        # System settings cache (ollama_url, etc.)
        self._ollama_url_cache: Optional[str] = None
        self._ollama_url_cache_time: float = 0.0

        # Feature flags for safe rollout (local flags, not from DB)
        self._local_feature_flags = {
            "use_database_model_config": True,  # Enabled - models fetched from database
        }

    async def get_secret(self, service_name: str) -> Optional[str]:
        """
        Fetch a secret value from the admin API.

        Args:
            service_name: Name of the service/secret to fetch

        Returns:
            Secret value, or None if not found

        Raises:
            Exception: If API call fails
        """
        try:
            url = f"{self.admin_url}/api/secrets/service/{service_name}"
            headers = {"X-API-Key": self.api_key}

            response = await self.client.get(url, headers=headers)

            if response.status_code == 404:
                logger.warning(
                    "secret_not_found",
                    service_name=service_name,
                    admin_url=self.admin_url
                )
                return None

            response.raise_for_status()
            data = response.json()
            return data.get("value")

        except httpx.HTTPStatusError as e:
            logger.error(
                "admin_api_error",
                service_name=service_name,
                status_code=e.response.status_code,
                error=str(e)
            )
            raise
        except Exception as e:
            logger.error(
                "admin_api_connection_failed",
                service_name=service_name,
                error=str(e)
            )
            raise

    async def get_config(self, key: str, default: Any = None) -> Any:
        """
        Fetch a configuration value.

        For now, this falls back to environment variables. In the future,
        this could fetch from a dedicated configuration store in the admin API.

        Args:
            key: Configuration key
            default: Default value if not found

        Returns:
            Configuration value
        """
        # TODO: Implement admin API endpoint for general config
        # For now, use environment variables
        return os.getenv(key, default)

    async def get_intent_patterns(self) -> Dict[str, List[str]]:
        """
        Fetch intent patterns from Admin API with caching.

        Returns:
            Dict mapping intent_category -> list of keywords
            Returns empty dict if API unavailable (allows hardcoded fallback)
        """
        # Check cache
        if self._patterns_cache and (time.time() - self._patterns_cache_time < self._cache_ttl):
            return self._patterns_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/intent-routing/patterns"
            response = await self.client.get(url)

            if response.status_code == 200:
                data = response.json()

                # Transform API response to Dict[category, List[keywords]]
                patterns: Dict[str, List[str]] = {}
                for item in data:
                    category = item["intent_category"]
                    keyword = item["keyword"]

                    if category not in patterns:
                        patterns[category] = []
                    patterns[category].append(keyword)

                # Cache successful result
                self._patterns_cache = patterns
                self._patterns_cache_time = time.time()

                logger.info(
                    "intent_patterns_loaded_from_db",
                    categories=len(patterns),
                    total_keywords=sum(len(kws) for kws in patterns.values())
                )
                return patterns
            else:
                logger.warning(
                    "intent_patterns_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "intent_patterns_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty dict to trigger hardcoded fallback
        return {}

    async def get_intent_routing(self) -> Dict[str, Dict]:
        """
        Fetch intent routing configuration with caching.

        Returns:
            Dict mapping intent_category -> {use_rag, rag_service_url, use_web_search, use_llm}
            Returns empty dict if API unavailable (allows hardcoded fallback)
        """
        # Check cache
        if self._routing_cache and (time.time() - self._routing_cache_time < self._cache_ttl):
            return self._routing_cache

        # Fetch from API (use public endpoint - no auth required)
        try:
            url = f"{self.admin_url}/api/intent-routing/routing/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                data = response.json()

                # Transform API response to Dict[category, config_dict]
                routing: Dict[str, Dict] = {}
                for item in data:
                    category = item["intent_category"]
                    routing[category] = {
                        "use_rag": item.get("use_rag", False),
                        "rag_service_url": item.get("rag_service_url"),
                        "use_web_search": item.get("use_web_search", False),
                        "use_llm": item.get("use_llm", True),
                        "priority": item.get("priority", 100)
                    }

                # Cache successful result
                self._routing_cache = routing
                self._routing_cache_time = time.time()

                logger.info(
                    "intent_routing_loaded_from_db",
                    categories=len(routing)
                )
                return routing
            else:
                logger.warning(
                    "intent_routing_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "intent_routing_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty dict to trigger hardcoded fallback
        return {}

    async def get_provider_routing(self) -> Dict[str, List[str]]:
        """
        Fetch provider routing with caching (ordered by priority).

        Returns:
            Dict mapping intent_category -> ordered list of provider names
            Returns empty dict if API unavailable (allows hardcoded fallback)
        """
        # Check cache
        if self._providers_cache and (time.time() - self._providers_cache_time < self._cache_ttl):
            return self._providers_cache

        # Fetch from API (use public endpoint - no auth required)
        try:
            url = f"{self.admin_url}/api/intent-routing/providers/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                data = response.json()

                # Group by category and sort by priority
                providers: Dict[str, List[str]] = {}
                for item in data:
                    category = item["intent_category"]
                    provider = item["provider_name"]
                    priority = item.get("priority", 100)

                    if category not in providers:
                        providers[category] = []
                    providers[category].append((provider, priority))

                # Sort by priority and extract provider names
                for category in providers:
                    providers[category] = [
                        p[0] for p in sorted(providers[category], key=lambda x: x[1])
                    ]

                # Cache successful result
                self._providers_cache = providers
                self._providers_cache_time = time.time()

                logger.info(
                    "provider_routing_loaded_from_db",
                    categories=len(providers)
                )
                return providers
            else:
                logger.warning(
                    "provider_routing_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "provider_routing_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty dict to trigger hardcoded fallback
        return {}

    async def get_llm_backends(self) -> List[Dict[str, Any]]:
        """
        Fetch enabled LLM backends from Admin API with caching.

        Returns:
            List of LLM backend configurations sorted by priority
            Returns empty list if API unavailable (allows env var fallback)
        """
        # Check cache
        if self._llm_backends_cache and (time.time() - self._llm_backends_cache_time < self._cache_ttl):
            return self._llm_backends_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/llm-backends/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                backends = response.json()

                # Filter to only enabled backends and sort by priority
                enabled_backends = [b for b in backends if b.get("enabled", False)]
                enabled_backends.sort(key=lambda x: x.get("priority", 999))

                # Cache successful result
                self._llm_backends_cache = enabled_backends
                self._llm_backends_cache_time = time.time()

                logger.info(
                    "llm_backends_loaded_from_db",
                    count=len(enabled_backends),
                    backends=[b.get("model_name") for b in enabled_backends]
                )
                return enabled_backends
            else:
                logger.warning(
                    "llm_backends_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "llm_backends_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty list to trigger env var fallback
        return []

    async def get_feature_flags(self) -> Dict[str, bool]:
        """
        Fetch feature flags from Admin API with caching.

        Returns:
            Dict mapping feature_name -> enabled status
            Returns empty dict if API unavailable (allows hardcoded defaults)
        """
        # Check cache
        if self._features_cache and (time.time() - self._features_cache_time < self._cache_ttl):
            return self._features_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/features/public?enabled_only=false"
            response = await self.client.get(url)

            if response.status_code == 200:
                features = response.json()

                # Transform to dict of name -> enabled
                flags = {f["name"]: f.get("enabled", False) for f in features}

                # Cache successful result
                self._features_cache = flags
                self._features_cache_time = time.time()

                logger.info(
                    "feature_flags_loaded_from_db",
                    count=len(flags),
                    enabled_count=sum(1 for v in flags.values() if v)
                )
                return flags
            else:
                logger.warning(
                    "feature_flags_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "feature_flags_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty dict to trigger hardcoded defaults
        return {}

    async def is_feature_enabled(self, feature_name: str) -> Optional[bool]:
        """
        Check if a specific feature is enabled.

        Args:
            feature_name: Name of the feature to check

        Returns:
            True if enabled, False if disabled, None if not found in DB (use default)
        """
        flags = await self.get_feature_flags()
        return flags.get(feature_name)

    async def get_external_api_key(self, service_name: str) -> Optional[Dict[str, Any]]:
        """
        Fetch external API key from Admin API (decrypted).

        Args:
            service_name: Service identifier (e.g., "brave-search", "api-football")

        Returns:
            Dict with api_key, endpoint_url, rate_limit_per_minute, or None if not found
        """
        try:
            url = f"{self.admin_url}/api/external-api-keys/public/{service_name}/key"
            response = await self.client.get(url)

            if response.status_code == 404:
                logger.debug(
                    "external_api_key_not_found",
                    service_name=service_name
                )
                return None

            response.raise_for_status()
            data = response.json()

            logger.info(
                "external_api_key_fetched",
                service_name=service_name,
                endpoint_url=data.get("endpoint_url")
            )
            return data

        except httpx.HTTPStatusError as e:
            logger.warning(
                "external_api_key_fetch_error",
                service_name=service_name,
                status_code=e.response.status_code,
                error=str(e)
            )
            return None
        except Exception as e:
            logger.warning(
                "external_api_key_connection_failed",
                service_name=service_name,
                error=str(e)
            )
            return None

    async def get_home_assistant_config(self) -> Optional[Dict[str, str]]:
        """
        Fetch Home Assistant configuration from Admin API.

        Looks for service name "home-assistant" in external_api_keys.

        Returns:
            Dict with 'url' and 'token' keys, or None if not configured
        """
        config = await self.get_external_api_key("home-assistant")
        if config:
            return {
                "url": config.get("endpoint_url"),
                "token": config.get("api_key")
            }
        return None

    async def get_tool_api_key_requirements(self, tool_name: str) -> List[Dict[str, Any]]:
        """
        Fetch API key requirements for a specific tool.

        Args:
            tool_name: Name of the tool to get requirements for

        Returns:
            List of API key requirement dicts with api_key_service, is_required, inject_as
            Returns empty list if tool not found or API unavailable
        """
        try:
            url = f"{self.admin_url}/api/tool-calling/tools/by-name/{tool_name}/api-keys/public"
            response = await self.client.get(url)

            if response.status_code == 404:
                logger.debug(
                    "tool_api_key_requirements_not_found",
                    tool_name=tool_name
                )
                return []

            response.raise_for_status()
            requirements = response.json()

            logger.debug(
                "tool_api_key_requirements_fetched",
                tool_name=tool_name,
                requirements_count=len(requirements)
            )
            return requirements

        except httpx.HTTPStatusError as e:
            logger.warning(
                "tool_api_key_requirements_fetch_error",
                tool_name=tool_name,
                status_code=e.response.status_code,
                error=str(e)
            )
            return []
        except Exception as e:
            logger.warning(
                "tool_api_key_requirements_connection_failed",
                tool_name=tool_name,
                error=str(e)
            )
            return []

    async def get_api_keys_for_tool(self, tool_name: str) -> Dict[str, str]:
        """
        Fetch all required API keys for a tool, ready for injection.

        Args:
            tool_name: Name of the tool to get API keys for

        Returns:
            Dict mapping inject_as parameter name -> API key value
            Returns empty dict if no keys required or API unavailable
        """
        # Get requirements for this tool
        requirements = await self.get_tool_api_key_requirements(tool_name)

        if not requirements:
            return {}

        api_keys = {}

        for req in requirements:
            service_name = req.get("api_key_service")
            inject_as = req.get("inject_as")
            is_required = req.get("is_required", True)

            if not service_name:
                continue

            # Fetch the actual API key
            api_key_data = await self.get_external_api_key(service_name)

            if api_key_data and api_key_data.get("api_key"):
                # Use inject_as if specified, otherwise use service_name
                param_name = inject_as or service_name.replace("-", "_") + "_api_key"
                api_keys[param_name] = api_key_data.get("api_key")
                logger.debug(
                    "api_key_fetched_for_tool",
                    tool_name=tool_name,
                    service_name=service_name,
                    inject_as=param_name
                )
            elif is_required:
                logger.warning(
                    "required_api_key_missing",
                    tool_name=tool_name,
                    service_name=service_name
                )

        return api_keys

    async def get_tool_calling_settings(self) -> Dict[str, Any]:
        """
        Fetch tool calling settings from Admin API with caching.

        Returns:
            Dict with tool calling configuration (enabled, llm_model, max_parallel_tools, etc.)
            Returns default settings if API unavailable
        """
        # Check cache
        if self._tool_calling_settings_cache and (time.time() - self._tool_calling_settings_cache_time < self._cache_ttl):
            return self._tool_calling_settings_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/tool-calling/settings/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                settings = response.json()

                # Cache successful result
                self._tool_calling_settings_cache = settings
                self._tool_calling_settings_cache_time = time.time()

                logger.info(
                    "tool_calling_settings_loaded_from_db",
                    enabled=settings.get("enabled"),
                    llm_model=settings.get("llm_model"),
                    llm_backend=settings.get("llm_backend")
                )
                return settings
            else:
                logger.warning(
                    "tool_calling_settings_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "tool_calling_settings_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return default settings if API unavailable
        return {
            "enabled": True,
            "llm_model": "gpt-4o-mini",
            "llm_backend": "openai",
            "max_parallel_tools": 3,
            "tool_call_timeout_seconds": 30,
            "temperature": 0.1,
            "max_tokens": 500,
            "fallback_to_direct_llm": True,
            "cache_results": True,
            "cache_ttl_seconds": 300
        }

    async def get_enabled_tools(self, guest_mode: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch enabled tools from Admin API with caching.

        Args:
            guest_mode: If True, only return guest-mode-allowed tools

        Returns:
            List of tool configurations with function schemas
            Returns empty list if API unavailable
        """
        # Check cache (only use if not in guest_mode or cache was for guest_mode)
        if self._enabled_tools_cache and (time.time() - self._enabled_tools_cache_time < self._cache_ttl):
            tools = self._enabled_tools_cache
            if guest_mode:
                return [t for t in tools if t.get("guest_mode_allowed", False)]
            return tools

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/tool-calling/tools/public?enabled_only=true"
            response = await self.client.get(url)

            if response.status_code == 200:
                tools = response.json()

                # Cache successful result
                self._enabled_tools_cache = tools
                self._enabled_tools_cache_time = time.time()

                logger.info(
                    "enabled_tools_loaded_from_db",
                    count=len(tools),
                    guest_mode_count=sum(1 for t in tools if t.get("guest_mode_allowed", False))
                )

                if guest_mode:
                    return [t for t in tools if t.get("guest_mode_allowed", False)]
                return tools
            else:
                logger.warning(
                    "enabled_tools_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "enabled_tools_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty list if API unavailable
        return []

    async def get_fallback_triggers(self) -> List[Dict[str, Any]]:
        """
        Fetch enabled fallback triggers from Admin API with caching.

        Returns:
            List of trigger configurations sorted by priority
            Returns empty list if API unavailable
        """
        # Check cache
        if self._fallback_triggers_cache and (time.time() - self._fallback_triggers_cache_time < self._cache_ttl):
            return self._fallback_triggers_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/tool-calling/triggers/public?enabled_only=true"
            response = await self.client.get(url)

            if response.status_code == 200:
                triggers = response.json()

                # Sort by priority (higher priority = checked first)
                triggers.sort(key=lambda x: x.get("priority", 0), reverse=True)

                # Cache successful result
                self._fallback_triggers_cache = triggers
                self._fallback_triggers_cache_time = time.time()

                logger.info(
                    "fallback_triggers_loaded_from_db",
                    count=len(triggers),
                    types=[t.get("trigger_type") for t in triggers]
                )
                return triggers
            else:
                logger.warning(
                    "fallback_triggers_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "fallback_triggers_db_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Return empty list if API unavailable
        return []

    # ==========================================================================
    # Escalation Presets
    # ==========================================================================

    async def get_active_escalation_preset(self) -> Optional[Dict[str, Any]]:
        """
        Fetch active escalation preset with rules from Admin API.

        Returns:
            Active preset with rules, or None if unavailable
        """
        # Check cache (60 second TTL)
        if self._escalation_preset_cache and (time.time() - self._escalation_preset_cache_time < self._cache_ttl):
            return self._escalation_preset_cache

        try:
            url = f"{self.admin_url}/api/escalation/presets/active/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                preset = response.json()

                # Sort rules by priority descending
                if preset.get("rules"):
                    preset["rules"].sort(key=lambda x: x.get("priority", 0), reverse=True)

                self._escalation_preset_cache = preset
                self._escalation_preset_cache_time = time.time()

                logger.info(
                    "escalation_preset_loaded",
                    preset_name=preset.get("name"),
                    rules_count=len(preset.get("rules", []))
                )
                return preset
            elif response.status_code == 404:
                logger.warning("no_active_escalation_preset")
                return None
            else:
                logger.warning("escalation_preset_fetch_failed", status=response.status_code)

        except Exception as e:
            logger.warning("escalation_preset_error", error=str(e))

        # Return stale cache if available
        return self._escalation_preset_cache

    async def get_escalation_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get current escalation state for a session."""
        try:
            url = f"{self.admin_url}/api/escalation/state/{session_id}/public"
            response = await self.client.get(url)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.warning("escalation_state_error", error=str(e), session_id=session_id[:8] if session_id else "none")
        return None

    async def update_escalation_state(
        self,
        session_id: str,
        escalated_to: str,
        turns_remaining: int,
        rule_id: Optional[int] = None
    ) -> bool:
        """Update escalation state for a session."""
        try:
            url = f"{self.admin_url}/api/escalation/state/internal"
            response = await self.client.post(url, json={
                "session_id": session_id,
                "escalated_to": escalated_to,
                "turns_remaining": turns_remaining,
                "triggered_by_rule_id": rule_id
            })
            return response.status_code == 200
        except Exception as e:
            logger.warning("escalation_state_update_error", error=str(e))
        return False

    async def get_base_knowledge(self, applies_to: str = "both", enabled_only: bool = True) -> List[Dict[str, Any]]:
        """
        Fetch base knowledge entries from Admin API with caching.

        Args:
            applies_to: Filter by applies_to ('guest', 'owner', 'both')
            enabled_only: If True, only return enabled entries

        Returns:
            List of base knowledge entries sorted by priority (descending)
            Returns empty list if API unavailable
        """
        # Check cache
        if self._base_knowledge_cache and (time.time() - self._base_knowledge_cache_time < self._cache_ttl):
            knowledge = self._base_knowledge_cache
        else:
            # Fetch from API
            try:
                url = f"{self.admin_url}/api/base-knowledge"
                if enabled_only:
                    url += "?enabled=true"

                response = await self.client.get(url)

                if response.status_code == 200:
                    knowledge = response.json()

                    # Cache successful result
                    self._base_knowledge_cache = knowledge
                    self._base_knowledge_cache_time = time.time()

                    logger.info(
                        "base_knowledge_loaded_from_db",
                        count=len(knowledge)
                    )
                else:
                    logger.warning(
                        "base_knowledge_fetch_failed",
                        status_code=response.status_code
                    )
                    return []

            except Exception as e:
                logger.warning(
                    "base_knowledge_db_error",
                    error=str(e),
                    admin_url=self.admin_url
                )
                return []

        # Filter by applies_to (include 'both' + specific mode)
        filtered = [
            k for k in knowledge
            if k.get("applies_to") == "both" or k.get("applies_to") == applies_to
        ]

        # Sort by priority (highest first)
        filtered.sort(key=lambda x: x.get("priority", 0), reverse=True)

        return filtered

    # ==========================================================================
    # Component Model Configuration
    # ==========================================================================

    def _local_feature_flag_enabled(self, flag_name: str) -> bool:
        """Check if a local feature flag is enabled."""
        return self._local_feature_flags.get(flag_name, False)

    def enable_feature_flag(self, flag_name: str):
        """Enable a local feature flag (for gradual rollout)."""
        self._local_feature_flags[flag_name] = True
        logger.info("local_feature_flag_enabled", flag=flag_name)

    def disable_feature_flag(self, flag_name: str):
        """Disable a local feature flag (for quick rollback)."""
        self._local_feature_flags[flag_name] = False
        logger.info("local_feature_flag_disabled", flag=flag_name)

    async def get_component_model(self, component_name: str) -> Optional[Dict[str, Any]]:
        """
        Fetch model assignment for a specific component with caching.

        Args:
            component_name: The component identifier (e.g., "intent_classifier")

        Returns:
            Dict with model_name, backend_type, temperature, etc.
            Returns None if component not found, API unavailable, or feature flag disabled.
        """
        # Check feature flag first - enables safe rollout
        if not self._local_feature_flag_enabled("use_database_model_config"):
            return None  # Callers fall back to hardcoded values

        now = time.time()

        # Check cache (60 second TTL)
        if component_name in self._component_model_cache:
            cache_time = self._component_model_cache_time.get(component_name, 0)
            if now - cache_time < self._cache_ttl:
                return self._component_model_cache[component_name]

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/component-models/component/{component_name}"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()

                # Cache successful result
                self._component_model_cache[component_name] = config
                self._component_model_cache_time[component_name] = now

                logger.debug(
                    "component_model_loaded",
                    component=component_name,
                    model=config.get("model_name")
                )
                return config
            else:
                logger.warning(
                    "component_model_fetch_failed",
                    component=component_name,
                    status_code=response.status_code
                )
                return None

        except Exception as e:
            logger.warning(
                "component_model_fetch_error",
                component=component_name,
                error=str(e)
            )
            return None

    def invalidate_component_model_cache(self, component_name: Optional[str] = None):
        """
        Invalidate component model cache.

        Args:
            component_name: Specific component to invalidate, or None for all
        """
        if component_name:
            self._component_model_cache.pop(component_name, None)
            self._component_model_cache_time.pop(component_name, None)
            logger.info("component_model_cache_invalidated", component=component_name)
        else:
            self._component_model_cache.clear()
            self._component_model_cache_time.clear()
            logger.info("component_model_cache_cleared")

    async def get_all_component_models(self) -> List[Dict[str, Any]]:
        """
        Fetch all component model assignments in a single API call.

        Returns:
            List of component model configurations.
            Returns empty list if API unavailable or feature flag disabled.
        """
        # Check feature flag first
        if not self._local_feature_flag_enabled("use_database_model_config"):
            return []

        try:
            url = f"{self.admin_url}/api/component-models/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                models = response.json()
                logger.debug("all_component_models_loaded", count=len(models))
                return models
            else:
                logger.warning(
                    "all_component_models_fetch_failed",
                    status_code=response.status_code
                )
                return []

        except Exception as e:
            logger.warning("all_component_models_fetch_error", error=str(e))
            return []

    async def record_tool_metric(
        self,
        tool_name: str,
        success: bool,
        latency_ms: int,
        error_message: str = None,
        intent: str = None,
        confidence: float = None,
        guest_mode: bool = False,
        request_id: str = None,
        session_id: str = None
    ) -> bool:
        """
        Record a tool usage metric to the admin backend.

        Args:
            tool_name: Name of the tool that was called
            success: Whether the tool call succeeded
            latency_ms: Time taken in milliseconds
            error_message: Error message if failed
            intent: Detected intent that triggered the tool
            confidence: Confidence score of intent detection
            guest_mode: Whether called in guest mode
            request_id: Unique request identifier
            session_id: Session identifier

        Returns:
            True if metric was recorded successfully, False otherwise
        """
        try:
            url = f"{self.admin_url}/api/tool-calling/metrics/record"
            payload = {
                "tool_name": tool_name,
                "success": success,
                "latency_ms": latency_ms,
                "error_message": error_message,
                "intent": intent,
                "confidence": confidence,
                "guest_mode": guest_mode,
                "request_id": request_id,
                "session_id": session_id
            }

            response = await self.client.post(url, json=payload)

            if response.status_code in (200, 201):
                logger.debug(
                    "tool_metric_recorded",
                    tool_name=tool_name,
                    success=success,
                    latency_ms=latency_ms
                )
                return True
            else:
                logger.warning(
                    "tool_metric_record_failed",
                    tool_name=tool_name,
                    status_code=response.status_code
                )
                return False

        except Exception as e:
            # Don't fail the request if metrics recording fails
            logger.warning(
                "tool_metric_record_error",
                tool_name=tool_name,
                error=str(e)
            )
            return False

    # ==========================================================================
    # Gateway Configuration
    # ==========================================================================

    async def resolve_room_group(self, query_term: str) -> Optional[Dict[str, Any]]:
        """
        Resolve a room group from a query term (name or alias).

        Args:
            query_term: The room name to look up (e.g., "first floor", "1st floor", "downstairs")

        Returns:
            Dict with room group info including member rooms, or None if not a group.
            Example: {
                "id": 1,
                "name": "first_floor",
                "display_name": "First Floor",
                "members": [
                    {"room_name": "living_room", "display_name": "Living Room"},
                    {"room_name": "dining_room", "display_name": "Dining Room"},
                    {"room_name": "kitchen", "display_name": "Kitchen"}
                ]
            }
        """
        try:
            # URL-encode the query term
            import urllib.parse
            encoded_term = urllib.parse.quote(query_term)
            url = f"{self.admin_url}/api/room-groups/resolve/{encoded_term}"
            response = await self.client.get(url)

            if response.status_code == 404:
                # Not a room group - this is normal for individual rooms
                logger.debug(
                    "room_group_not_found",
                    query_term=query_term
                )
                return None

            response.raise_for_status()
            data = response.json()

            # The resolve endpoint returns {"found": bool, "room_group": {...}}
            # Check if the group was actually found
            if not data.get("found", False):
                logger.debug(
                    "room_group_not_found",
                    query_term=query_term
                )
                return None

            # Return the room_group data
            room_group = data.get("room_group")
            if room_group:
                logger.info(
                    "room_group_resolved",
                    query_term=query_term,
                    group_name=room_group.get("name"),
                    member_count=len(room_group.get("members", []))
                )
                return room_group

            return None

        except httpx.HTTPStatusError as e:
            logger.warning(
                "room_group_resolve_error",
                query_term=query_term,
                status_code=e.response.status_code,
                error=str(e)
            )
            return None
        except Exception as e:
            logger.warning(
                "room_group_resolve_connection_failed",
                query_term=query_term,
                error=str(e)
            )
            return None

    async def get_room_groups(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """
        Fetch all room groups with their aliases and members.

        Args:
            enabled_only: If True, only return enabled room groups

        Returns:
            List of room group dicts, each containing:
            - name: Canonical name (e.g., "first_floor")
            - display_name: User-friendly name (e.g., "First Floor")
            - aliases: List of alias strings (e.g., ["downstairs", "1st floor"])
            - members: List of room member dicts
        """
        try:
            params = {}
            if enabled_only:
                params["enabled"] = "true"

            url = f"{self.admin_url}/api/room-groups"
            response = await self.client.get(url, params=params)

            if response.status_code == 200:
                groups = response.json()
                logger.debug(
                    "room_groups_loaded",
                    count=len(groups)
                )
                return groups
            else:
                logger.warning(
                    "room_groups_fetch_failed",
                    status_code=response.status_code
                )
                return []

        except Exception as e:
            logger.warning(
                "room_groups_fetch_error",
                error=str(e)
            )
            return []

    async def get_gateway_config(self) -> Optional[Dict[str, Any]]:
        """
        Fetch gateway configuration from Admin API with caching.

        Returns:
            Dict with gateway configuration, or None if API unavailable.
            Configuration includes orchestrator_url, ollama_fallback_url,
            intent settings, timeouts, session settings, cache TTL,
            rate limiting, and circuit breaker settings.
        """
        now = time.time()

        # Check cache (60 second TTL)
        if self._gateway_config_cache and (now - self._gateway_config_cache_time < self._cache_ttl):
            return self._gateway_config_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/gateway-config/public"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()

                # Cache successful result
                self._gateway_config_cache = config
                self._gateway_config_cache_time = now

                logger.info(
                    "gateway_config_loaded_from_db",
                    orchestrator_url=config.get("orchestrator_url"),
                    intent_model=config.get("intent_model")
                )
                return config
            else:
                logger.warning(
                    "gateway_config_fetch_failed",
                    status_code=response.status_code
                )
                return None

        except Exception as e:
            logger.warning(
                "gateway_config_fetch_error",
                error=str(e),
                admin_url=self.admin_url
            )
            return None

    def invalidate_gateway_config_cache(self):
        """Invalidate gateway config cache to force refresh on next call."""
        self._gateway_config_cache = None
        self._gateway_config_cache_time = 0.0
        logger.info("gateway_config_cache_invalidated")

    # ==========================================================================
    # Multi-Guest User Session Support
    # ==========================================================================

    async def get_user_session_by_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch user session information by device fingerprint.

        Used for multi-guest identification - maps device IDs to guests
        so the orchestrator knows who is making the request.

        Args:
            device_id: Device fingerprint from FingerprintJS

        Returns:
            Dict with session info including guest_id, guest_name, device_type, preferences
            or None if no session found for this device.

            Example: {
                "id": 1,
                "session_id": "abc-123",
                "guest_id": 5,
                "guest_name": "Andre",
                "device_id": "fp_abc123...",
                "device_type": "web",
                "preferences": {},
                "last_activity": "2025-12-12T10:30:00Z"
            }
        """
        try:
            import urllib.parse
            encoded_device_id = urllib.parse.quote(device_id)
            url = f"{self.admin_url}/api/user-sessions/device/{encoded_device_id}"
            response = await self.client.get(url)

            if response.status_code == 404:
                logger.debug(
                    "user_session_not_found",
                    device_id=device_id[:16] + "..." if len(device_id) > 16 else device_id
                )
                return None

            response.raise_for_status()
            data = response.json()

            logger.info(
                "user_session_found",
                device_id=device_id[:16] + "..." if len(device_id) > 16 else device_id,
                guest_id=data.get("guest_id"),
                guest_name=data.get("guest_name")
            )
            return data

        except httpx.HTTPStatusError as e:
            logger.warning(
                "user_session_fetch_error",
                device_id=device_id[:16] + "..." if len(device_id) > 16 else device_id,
                status_code=e.response.status_code,
                error=str(e)
            )
            return None
        except Exception as e:
            logger.warning(
                "user_session_fetch_connection_failed",
                device_id=device_id[:16] + "..." if len(device_id) > 16 else device_id,
                error=str(e)
            )
            return None

    # ==========================================================================
    # Voice Configuration Support
    # ==========================================================================

    async def get_voice_config_stt(self) -> Optional[Dict[str, Any]]:
        """
        Fetch STT configuration from Admin API with caching.

        Returns:
            Dict with STT model info and service connection details:
            {
                "model_id": "whisper-small.en",
                "model_name": "Whisper Small (English)",
                "whisper_model": "small.en",
                "compute_type": "float16",
                "wyoming_host": "localhost",
                "wyoming_port": 10300,
                "wyoming_url": "tcp://localhost:10300",
                "service_type": "stt",
                "service_host": "localhost",
                "service_port": 10300
            }
        """
        now = time.time()

        # Check cache (60 second TTL)
        if self._voice_config_stt_cache and (now - self._voice_config_stt_cache_time < self._cache_ttl):
            return self._voice_config_stt_cache

        try:
            url = f"{self.admin_url}/api/voice-config/internal/stt"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()
                self._voice_config_stt_cache = config
                self._voice_config_stt_cache_time = now

                logger.info(
                    "voice_config_stt_loaded",
                    model_id=config.get("model_id"),
                    wyoming_url=config.get("wyoming_url")
                )
                return config
            else:
                logger.warning(
                    "voice_config_stt_fetch_failed",
                    status_code=response.status_code
                )
                return None

        except Exception as e:
            logger.warning(
                "voice_config_stt_fetch_error",
                error=str(e),
                admin_url=self.admin_url
            )
            return None

    async def get_voice_config_tts(self) -> Optional[Dict[str, Any]]:
        """
        Fetch TTS configuration from Admin API with caching.

        Returns:
            Dict with TTS voice info and service connection details:
            {
                "voice_id": "en_US-lessac-medium",
                "voice_name": "Lessac (US English)",
                "piper_voice": "en_US-lessac-medium",
                "quality": "medium",
                "wyoming_host": "localhost",
                "wyoming_port": 10200,
                "wyoming_url": "tcp://localhost:10200",
                "service_type": "tts",
                "service_host": "localhost",
                "service_port": 10201
            }
        """
        now = time.time()

        # Check cache (60 second TTL)
        if self._voice_config_tts_cache and (now - self._voice_config_tts_cache_time < self._cache_ttl):
            return self._voice_config_tts_cache

        try:
            url = f"{self.admin_url}/api/voice-config/internal/tts"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()
                self._voice_config_tts_cache = config
                self._voice_config_tts_cache_time = now

                logger.info(
                    "voice_config_tts_loaded",
                    voice_id=config.get("voice_id"),
                    wyoming_url=config.get("wyoming_url")
                )
                return config
            else:
                logger.warning(
                    "voice_config_tts_fetch_failed",
                    status_code=response.status_code
                )
                return None

        except Exception as e:
            logger.warning(
                "voice_config_tts_fetch_error",
                error=str(e),
                admin_url=self.admin_url
            )
            return None

    async def get_voice_config_all(self) -> Dict[str, Any]:
        """
        Fetch complete voice configuration (STT + TTS) from Admin API with caching.

        Returns:
            Dict with both stt and tts configs:
            {
                "stt": { ... STT config ... },
                "tts": { ... TTS config ... }
            }
        """
        now = time.time()

        # Check cache (60 second TTL)
        if self._voice_config_all_cache and (now - self._voice_config_all_cache_time < self._cache_ttl):
            return self._voice_config_all_cache

        try:
            url = f"{self.admin_url}/api/voice-config/internal/all"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()
                self._voice_config_all_cache = config
                self._voice_config_all_cache_time = now

                logger.info(
                    "voice_config_all_loaded",
                    stt_model=config.get("stt", {}).get("model_id"),
                    tts_voice=config.get("tts", {}).get("voice_id")
                )
                return config
            else:
                logger.warning(
                    "voice_config_all_fetch_failed",
                    status_code=response.status_code
                )
                return {"stt": None, "tts": None}

        except Exception as e:
            logger.warning(
                "voice_config_all_fetch_error",
                error=str(e),
                admin_url=self.admin_url
            )
            return {"stt": None, "tts": None}

    async def get_voice_interface_config(self, interface_name: str) -> Optional[Dict[str, Any]]:
        """
        Fetch complete voice interface configuration for a specific interface.

        This includes the resolved STT/TTS engine endpoints and per-interface
        behavior settings (continued_conversation, wake_word_enabled, etc.)

        Args:
            interface_name: Interface identifier (e.g., "web_jarvis", "home_assistant", "admin_jarvis")

        Returns:
            Dict with complete interface config:
            {
                "interface_name": "web_jarvis",
                "display_name": "Web Jarvis",
                "enabled": true,
                "stt_engine": { ... engine config ... },
                "tts_engine": { ... engine config ... },
                "continued_conversation": false,
                "wake_word_enabled": false,
                "default_voice_id": "en_US-lessac-medium",
                "timeout_seconds": 30
            }
        """
        now = time.time()

        # Check cache (60 second TTL)
        if interface_name in self._voice_interface_cache:
            cache_time = self._voice_interface_cache_time.get(interface_name, 0)
            if now - cache_time < self._cache_ttl:
                return self._voice_interface_cache[interface_name]

        try:
            url = f"{self.admin_url}/api/voice-interfaces/internal/config/{interface_name}"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()
                self._voice_interface_cache[interface_name] = config
                self._voice_interface_cache_time[interface_name] = now

                logger.info(
                    "voice_interface_config_loaded",
                    interface_name=interface_name,
                    stt_engine=config.get("stt_engine", {}).get("engine_id"),
                    tts_engine=config.get("tts_engine", {}).get("engine_id")
                )
                return config
            elif response.status_code == 404:
                logger.warning(
                    "voice_interface_not_found",
                    interface_name=interface_name
                )
                return None
            else:
                logger.warning(
                    "voice_interface_config_fetch_failed",
                    interface_name=interface_name,
                    status_code=response.status_code
                )
                return None

        except Exception as e:
            logger.warning(
                "voice_interface_config_fetch_error",
                interface_name=interface_name,
                error=str(e),
                admin_url=self.admin_url
            )
            return None

    async def check_voice_services_health(self) -> Dict[str, Any]:
        """
        Check health of voice services (STT/TTS).

        Returns:
            Dict with health status for each service type:
            {
                "stt": {"healthy": true, "message": "Service is responding"},
                "tts": {"healthy": true, "message": "Service is responding"},
                "overall_healthy": true
            }
        """
        try:
            url = f"{self.admin_url}/api/voice-config/health"
            response = await self.client.get(url)

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(
                    "voice_health_check_failed",
                    status_code=response.status_code
                )
                return {
                    "stt": {"healthy": False, "message": f"API returned {response.status_code}"},
                    "tts": {"healthy": False, "message": f"API returned {response.status_code}"},
                    "overall_healthy": False
                }

        except Exception as e:
            logger.warning(
                "voice_health_check_error",
                error=str(e)
            )
            return {
                "stt": {"healthy": False, "message": str(e)},
                "tts": {"healthy": False, "message": str(e)},
                "overall_healthy": False
            }

    def invalidate_voice_config_cache(self):
        """Invalidate all voice config caches to force refresh on next call."""
        self._voice_config_stt_cache = None
        self._voice_config_stt_cache_time = 0.0
        self._voice_config_tts_cache = None
        self._voice_config_tts_cache_time = 0.0
        self._voice_config_all_cache = None
        self._voice_config_all_cache_time = 0.0
        self._voice_interface_cache.clear()
        self._voice_interface_cache_time.clear()
        logger.info("voice_config_cache_invalidated")

    # ==========================================================================
    # Voice Automations (Guest-Scoped with Archival)
    # ==========================================================================

    async def create_voice_automation(self, automation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Create a voice automation record in the database.

        Args:
            automation: Dict containing:
                - name: Automation name
                - ha_automation_id: Home Assistant automation ID (optional)
                - owner_type: 'owner' or 'guest'
                - guest_session_id: Session ID if guest (required for guests)
                - guest_name: Name of guest (optional)
                - created_by_room: Room where created (optional)
                - trigger_config: JSONB trigger configuration
                - conditions_config: JSONB conditions (optional)
                - actions_config: JSONB action configuration
                - is_one_time: Whether this is a one-time automation
                - end_date: Date when automation expires (optional)

        Returns:
            Created automation dict with ID, or None on failure
        """
        try:
            url = f"{self.admin_url}/api/voice-automations"
            response = await self.client.post(url, json=automation)

            if response.status_code in (200, 201):
                data = response.json()
                logger.info(
                    "voice_automation_created",
                    id=data.get("id"),
                    name=data.get("name"),
                    owner_type=data.get("owner_type")
                )
                return data
            else:
                logger.warning(
                    "voice_automation_create_failed",
                    status_code=response.status_code,
                    error=response.text
                )
                return None

        except Exception as e:
            logger.error(
                "voice_automation_create_error",
                error=str(e)
            )
            return None

    async def get_voice_automations(
        self,
        owner_type: Optional[str] = None,
        guest_name: Optional[str] = None,
        include_archived: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Fetch voice automations with optional filters.

        Args:
            owner_type: Filter by 'owner' or 'guest'
            guest_name: Filter by guest name (for returning guests)
            include_archived: Include archived automations

        Returns:
            List of automation dicts
        """
        try:
            params = {}
            if owner_type:
                params["owner_type"] = owner_type
            if guest_name:
                params["guest_name"] = guest_name
            if include_archived:
                params["include_archived"] = "true"

            url = f"{self.admin_url}/api/voice-automations"
            response = await self.client.get(url, params=params)

            if response.status_code == 200:
                automations = response.json()
                logger.debug(
                    "voice_automations_fetched",
                    count=len(automations),
                    owner_type=owner_type
                )
                return automations
            else:
                logger.warning(
                    "voice_automations_fetch_failed",
                    status_code=response.status_code
                )
                return []

        except Exception as e:
            logger.warning(
                "voice_automations_fetch_error",
                error=str(e)
            )
            return []

    async def archive_voice_automation(
        self,
        automation_id: int,
        reason: str = "user_deleted"
    ) -> bool:
        """
        Archive a voice automation (soft delete).

        Args:
            automation_id: ID of automation to archive
            reason: Archive reason ('guest_departed', 'user_deleted', 'expired', 'one_time_completed')

        Returns:
            True if archived successfully
        """
        try:
            url = f"{self.admin_url}/api/voice-automations/{automation_id}/archive"
            response = await self.client.post(url, json={"reason": reason})

            if response.status_code == 200:
                logger.info(
                    "voice_automation_archived",
                    automation_id=automation_id,
                    reason=reason
                )
                return True
            else:
                logger.warning(
                    "voice_automation_archive_failed",
                    automation_id=automation_id,
                    status_code=response.status_code
                )
                return False

        except Exception as e:
            logger.error(
                "voice_automation_archive_error",
                automation_id=automation_id,
                error=str(e)
            )
            return False

    async def restore_voice_automation(self, automation_id: int) -> bool:
        """
        Restore an archived voice automation.

        Args:
            automation_id: ID of automation to restore

        Returns:
            True if restored successfully
        """
        try:
            url = f"{self.admin_url}/api/voice-automations/{automation_id}/restore"
            response = await self.client.post(url)

            if response.status_code == 200:
                logger.info(
                    "voice_automation_restored",
                    automation_id=automation_id
                )
                return True
            else:
                logger.warning(
                    "voice_automation_restore_failed",
                    automation_id=automation_id,
                    status_code=response.status_code
                )
                return False

        except Exception as e:
            logger.error(
                "voice_automation_restore_error",
                automation_id=automation_id,
                error=str(e)
            )
            return False

    async def delete_voice_automation(self, automation_id: int) -> bool:
        """
        Permanently delete a voice automation.

        Args:
            automation_id: ID of automation to delete

        Returns:
            True if deleted successfully
        """
        try:
            url = f"{self.admin_url}/api/voice-automations/{automation_id}"
            response = await self.client.delete(url)

            if response.status_code in (200, 204):
                logger.info(
                    "voice_automation_deleted",
                    automation_id=automation_id
                )
                return True
            else:
                logger.warning(
                    "voice_automation_delete_failed",
                    automation_id=automation_id,
                    status_code=response.status_code
                )
                return False

        except Exception as e:
            logger.error(
                "voice_automation_delete_error",
                automation_id=automation_id,
                error=str(e)
            )
            return False

    async def archive_guest_automations(
        self,
        guest_session_id: Optional[str] = None,
        guest_name: Optional[str] = None
    ) -> int:
        """
        Archive all automations for a guest (when they depart).

        Args:
            guest_session_id: Session ID of the departing guest
            guest_name: Name of the departing guest

        Returns:
            Number of automations archived
        """
        try:
            url = f"{self.admin_url}/api/voice-automations/archive-guest"
            payload = {"reason": "guest_departed"}
            if guest_session_id:
                payload["guest_session_id"] = guest_session_id
            if guest_name:
                payload["guest_name"] = guest_name

            response = await self.client.post(url, json=payload)

            if response.status_code == 200:
                data = response.json()
                count = data.get("archived_count", 0)
                logger.info(
                    "guest_automations_archived",
                    guest_name=guest_name,
                    count=count
                )
                return count
            else:
                logger.warning(
                    "guest_automations_archive_failed",
                    guest_name=guest_name,
                    status_code=response.status_code
                )
                return 0

        except Exception as e:
            logger.error(
                "guest_automations_archive_error",
                guest_name=guest_name,
                error=str(e)
            )
            return 0

    async def restore_guest_automations(self, guest_name: str) -> int:
        """
        Restore all archived automations for a returning guest.

        Args:
            guest_name: Name of the returning guest

        Returns:
            Number of automations restored
        """
        try:
            url = f"{self.admin_url}/api/voice-automations/restore-guest"
            response = await self.client.post(url, json={"guest_name": guest_name})

            if response.status_code == 200:
                data = response.json()
                count = data.get("restored_count", 0)
                logger.info(
                    "guest_automations_restored",
                    guest_name=guest_name,
                    count=count
                )
                return count
            else:
                logger.warning(
                    "guest_automations_restore_failed",
                    guest_name=guest_name,
                    status_code=response.status_code
                )
                return 0

        except Exception as e:
            logger.error(
                "guest_automations_restore_error",
                guest_name=guest_name,
                error=str(e)
            )
            return 0

    # =========================================================================
    # Service Usage Tracking
    # =========================================================================

    async def get_service_usage(self, service_name: str) -> Dict[str, Any]:
        """
        Get current month's usage for a service.

        Used by RAG services (like Bright Data) to check budget before making requests.

        Args:
            service_name: Name of the service (e.g., "bright-data")

        Returns:
            Dict with monthly_count, monthly_limit, remaining
            Returns {"monthly_count": 0} if API unavailable
        """
        try:
            url = f"{self.admin_url}/api/internal/service-usage/{service_name}"
            response = await self.client.get(url)

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(
                    "get_service_usage_failed",
                    service=service_name,
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "get_service_usage_error",
                service=service_name,
                error=str(e)
            )

        return {"monthly_count": 0, "monthly_limit": None, "remaining": None}

    async def record_service_usage(self, service_name: str, count: int = 1) -> Dict[str, Any]:
        """
        Record usage increment for a service.

        Called by RAG services after each API request to track usage.

        Args:
            service_name: Name of the service (e.g., "bright-data")
            count: Number of requests to add (default: 1)

        Returns:
            Dict with updated monthly_count, monthly_limit, remaining
        """
        try:
            url = f"{self.admin_url}/api/internal/service-usage/{service_name}/increment"
            response = await self.client.post(url, params={"count": count})

            if response.status_code == 200:
                result = response.json()
                logger.debug(
                    "service_usage_recorded",
                    service=service_name,
                    count=count,
                    monthly_total=result.get("monthly_count")
                )
                return result
            else:
                logger.warning(
                    "record_service_usage_failed",
                    service=service_name,
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "record_service_usage_error",
                service=service_name,
                error=str(e)
            )

        return {"monthly_count": 0}

    # =========================================================================
    # System Settings (Centralized Configuration)
    # =========================================================================

    async def get_ollama_url(self) -> str:
        """
        Fetch centralized Ollama URL from Admin API with caching.

        This is the single source of truth for the Ollama API endpoint.
        All services should use this method instead of reading OLLAMA_URL directly.

        Returns:
            Ollama API URL (e.g., "http://192.168.10.108:11434")
            Falls back to OLLAMA_URL env var if API unavailable
        """
        now = time.time()

        # Check cache (60 second TTL)
        if self._ollama_url_cache and (now - self._ollama_url_cache_time < self._cache_ttl):
            return self._ollama_url_cache

        # Fetch from API
        try:
            url = f"{self.admin_url}/api/settings/ollama-url/internal"
            response = await self.client.get(url)

            if response.status_code == 200:
                data = response.json()
                ollama_url = data.get("ollama_url")

                if ollama_url:
                    # Cache successful result
                    self._ollama_url_cache = ollama_url
                    self._ollama_url_cache_time = now

                    logger.info(
                        "ollama_url_loaded_from_db",
                        ollama_url=ollama_url
                    )
                    return ollama_url
            else:
                logger.warning(
                    "ollama_url_fetch_failed",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning(
                "ollama_url_fetch_error",
                error=str(e),
                admin_url=self.admin_url
            )

        # Fallback to environment variable
        fallback_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        logger.debug(
            "ollama_url_using_env_fallback",
            ollama_url=fallback_url
        )
        return fallback_url

    def invalidate_ollama_url_cache(self):
        """Invalidate Ollama URL cache to force refresh on next call."""
        self._ollama_url_cache = None
        self._ollama_url_cache_time = 0.0
        logger.info("ollama_url_cache_invalidated")

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# Singleton instance for convenience
_admin_client: Optional[AdminConfigClient] = None


def get_admin_client() -> AdminConfigClient:
    """
    Get or create admin configuration client singleton.

    Returns:
        AdminConfigClient instance
    """
    global _admin_client
    if _admin_client is None:
        _admin_client = AdminConfigClient()
    return _admin_client


async def get_secret(service_name: str) -> Optional[str]:
    """
    Convenience function to fetch a secret.

    Args:
        service_name: Name of the service/secret

    Returns:
        Secret value or None
    """
    client = get_admin_client()
    return await client.get_secret(service_name)


async def get_config(key: str, default: Any = None) -> Any:
    """
    Convenience function to fetch configuration.

    Args:
        key: Configuration key
        default: Default value

    Returns:
        Configuration value
    """
    client = get_admin_client()
    return await client.get_config(key, default)


if __name__ == "__main__":
    import asyncio

    async def test():
        """Test the admin configuration client."""
        client = AdminConfigClient()

        # Test fetching a secret
        print("Testing secret fetch...")
        try:
            ha_token = await client.get_secret("home-assistant")
            if ha_token:
                print(f"✓ Home Assistant token: {ha_token[:20]}...")
            else:
                print("✗ Home Assistant token not found")
        except Exception as e:
            print(f"✗ Error fetching secret: {e}")

        await client.close()

    asyncio.run(test())
