"""
Unified Tool Registry - Hybrid Static + Dynamic Tools

Implements priority-based tool resolution:
1. Static tools (Admin UI configured) - Priority 10
2. MCP tools (n8n/dynamic discovery) - Priority 50
3. Legacy tools (hardcoded fallback) - Priority 100

Open Source Ready:
- All features toggleable via feature flags
- Sensible defaults work out of the box
- No hard dependencies on n8n/MCP
"""

import os
import time
import asyncio
from typing import Dict, List, Any, Optional
from enum import Enum
from dataclasses import dataclass, field
import structlog

logger = structlog.get_logger()


class ToolSource(Enum):
    """Tool source types with priority (lower = higher priority)"""
    STATIC = ("static", 10)      # Manually configured via Admin UI
    MCP = ("mcp", 50)            # Discovered from n8n via MCP
    LEGACY = ("legacy", 100)     # Hardcoded fallback from rag_tools.py

    @property
    def name_str(self) -> str:
        return self.value[0]

    @property
    def priority(self) -> int:
        return self.value[1]


@dataclass
class Tool:
    """Unified tool representation"""
    name: str
    display_name: str
    description: str
    function_schema: Dict[str, Any]
    service_url: Optional[str] = None
    source: ToolSource = ToolSource.LEGACY
    priority: int = 100
    enabled: bool = True
    guest_mode_allowed: bool = True
    requires_api_key: bool = False
    api_key_service: Optional[str] = None
    timeout_seconds: int = 20
    metadata: Dict[str, Any] = field(default_factory=dict)


class UnifiedToolRegistry:
    """
    Manages tools from multiple sources with priority resolution.

    Usage:
        registry = await ToolRegistryFactory.create()
        tools = registry.get_all_tools(guest_mode=False)
        schemas = registry.get_tool_schemas()
    """

    def __init__(self):
        self._static_tools: Dict[str, Tool] = {}
        self._mcp_tools: Dict[str, Tool] = {}
        self._legacy_tools: Dict[str, Tool] = {}
        self._cache_time = 0
        self._cache_ttl = 30  # 30 second refresh
        self._feature_flags: Dict[str, bool] = {}
        self._initialized = False

    async def initialize(self):
        """Initialize registry and load feature flags."""
        if self._initialized:
            return

        await self._load_feature_flags()
        await self.refresh()
        self._initialized = True

    async def _load_feature_flags(self):
        """Load feature flags from Admin API."""
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            # get_feature_flags() returns Dict[str, bool] directly
            flags = await client.get_feature_flags()
            self._feature_flags = flags
            logger.info("Feature flags loaded", flags=list(self._feature_flags.keys()))
        except Exception as e:
            logger.warning(f"Failed to load feature flags, using defaults: {e}")
            self._feature_flags = {
                'tool_system_enabled': True,
                'mcp_integration': False,
                'n8n_integration': False,
                'legacy_tools_fallback': True,
            }

    def is_feature_enabled(self, flag_name: str) -> bool:
        """Check if a feature flag is enabled."""
        return self._feature_flags.get(flag_name, False)

    async def refresh(self):
        """Refresh tools from all enabled sources."""
        start_time = time.time()
        success = True

        try:
            tasks = []

            # Always load static tools from Admin API
            tasks.append(self._load_static_tools())

            # Load MCP/n8n tools only if enabled
            if self.is_feature_enabled('mcp_integration') or self.is_feature_enabled('n8n_integration'):
                tasks.append(self._load_mcp_tools())

            # Load legacy fallback if enabled (default True for backwards compatibility)
            if self._feature_flags.get('legacy_tools_fallback', True):
                tasks.append(self._load_legacy_tools())

            await asyncio.gather(*tasks, return_exceptions=True)
            self._cache_time = time.time()

            logger.info(
                "Tool registry refreshed",
                static_count=len(self._static_tools),
                mcp_count=len(self._mcp_tools),
                legacy_count=len(self._legacy_tools),
            )

            # Update metrics
            try:
                from shared.metrics import update_registry_size, record_registry_refresh
                update_registry_size(
                    static_count=len(self._static_tools),
                    mcp_count=len(self._mcp_tools),
                    legacy_count=len(self._legacy_tools),
                )
                record_registry_refresh(success=True, latency_seconds=time.time() - start_time)
            except ImportError:
                pass  # Metrics module not available

        except Exception as e:
            success = False
            logger.error("Tool registry refresh failed", error=str(e))

            try:
                from shared.metrics import record_registry_refresh
                record_registry_refresh(success=False, latency_seconds=time.time() - start_time)
            except ImportError:
                pass

    async def _load_static_tools(self):
        """Load manually configured tools from Admin API."""
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            tools = await client.get_enabled_tools()

            self._static_tools = {}
            for t in tools:
                if not t.get('enabled', True):
                    continue

                tool_name = t.get('tool_name', t.get('name', ''))
                self._static_tools[tool_name] = Tool(
                    name=tool_name,
                    display_name=t.get('display_name', tool_name),
                    description=t.get('description', ''),
                    function_schema=t.get('function_schema', {}),
                    service_url=t.get('service_url'),
                    source=ToolSource.STATIC,
                    priority=ToolSource.STATIC.priority,
                    enabled=True,
                    guest_mode_allowed=t.get('guest_mode_allowed', True),
                    requires_api_key=t.get('requires_api_key', False),
                    api_key_service=t.get('api_key_service'),
                    timeout_seconds=t.get('timeout_seconds', 20),
                )

            logger.info(f"Loaded {len(self._static_tools)} static tools from Admin API")
        except Exception as e:
            logger.warning(f"Failed to load static tools: {e}")

    async def _load_mcp_tools(self):
        """Discover tools from n8n via MCP protocol."""
        # Check environment variable first
        mcp_url = os.getenv("N8N_MCP_URL")

        if not mcp_url:
            # Try to get from feature flag config
            flag_config = await self._get_flag_config('mcp_integration')
            mcp_url = flag_config.get('mcp_url') if flag_config else None

        if not mcp_url:
            # Default to Thor's central n8n service
            mcp_url = "http://localhost:5678/mcp"
            logger.debug("Using default Thor n8n MCP URL")

        # Security: Check if MCP URL domain is allowed
        mcp_security = await self._get_mcp_security()
        if not self._is_domain_allowed(mcp_url, mcp_security):
            logger.warning(
                "mcp_url_blocked",
                mcp_url=mcp_url,
                reason="Domain not in allowlist"
            )
            return

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                # MCP tools/list endpoint
                response = await client.post(
                    f"{mcp_url}/mcp/tools/list",
                    json={}
                )

                if response.status_code == 200:
                    data = response.json()
                    mcp_tools = data.get('tools', [])

                    self._mcp_tools = {}
                    blocked_count = 0

                    for t in mcp_tools:
                        tool_name = t.get('name', '')
                        webhook_url = t.get('webhook_url')

                        # Security: Check if tool webhook domain is allowed
                        if webhook_url and not self._is_domain_allowed(webhook_url, mcp_security):
                            logger.warning(
                                "mcp_tool_blocked",
                                tool_name=tool_name,
                                webhook_url=webhook_url,
                                reason="Webhook domain not in allowlist"
                            )
                            blocked_count += 1
                            continue

                        self._mcp_tools[tool_name] = Tool(
                            name=tool_name,
                            display_name=t.get('description', tool_name)[:50],
                            description=t.get('description', ''),
                            function_schema=self._mcp_to_openai_schema(t),
                            service_url=webhook_url,
                            source=ToolSource.MCP,
                            priority=ToolSource.MCP.priority,
                            guest_mode_allowed=True,  # MCP tools default to guest-safe
                            metadata={'mcp_raw': t},
                        )

                    logger.info(
                        f"Discovered {len(self._mcp_tools)} MCP tools from n8n",
                        blocked_count=blocked_count
                    )
                else:
                    logger.warning(f"MCP discovery failed: {response.status_code}")
        except Exception as e:
            logger.debug(f"MCP discovery skipped: {e}")

    async def _load_legacy_tools(self):
        """Load hardcoded tools as fallback."""
        try:
            from orchestrator.rag_tools import TOOL_DEFINITIONS

            self._legacy_tools = {}
            for t in TOOL_DEFINITIONS:
                tool_name = t['tool_name']
                self._legacy_tools[tool_name] = Tool(
                    name=tool_name,
                    display_name=t.get('display_name', tool_name),
                    description=t.get('description', ''),
                    function_schema=t.get('function_schema', {}),
                    service_url=t.get('service_url'),
                    source=ToolSource.LEGACY,
                    priority=ToolSource.LEGACY.priority,
                    guest_mode_allowed=t.get('guest_mode_allowed', True),
                    timeout_seconds=t.get('timeout_seconds', 20),
                )

            logger.debug(f"Loaded {len(self._legacy_tools)} legacy fallback tools")
        except ImportError:
            logger.warning("Legacy tools not available (rag_tools.py not found)")

    async def _get_flag_config(self, flag_name: str) -> Optional[Dict]:
        """Get configuration for a feature flag."""
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            # Need to fetch full feature info for config
            # The current API returns Dict[str, bool], we need the config field
            # For now, return None and rely on env vars
            return None
        except Exception:
            pass
        return None

    async def _get_mcp_security(self) -> Optional[Dict]:
        """Fetch MCP security configuration from Admin API."""
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            url = f"{client.admin_url}/api/mcp-security/public"
            response = await client.client.get(url)

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Failed to fetch MCP security config: {response.status_code}")
        except Exception as e:
            logger.warning(f"Failed to fetch MCP security config: {e}")

        # Return default restrictive config
        return {
            'allowed_domains': ['localhost', '127.0.0.1'],
            'blocked_domains': [],
            'max_execution_time_ms': 30000,
            'max_concurrent_tools': 5,
        }

    async def get_execution_timeout_seconds(self) -> float:
        """
        Get the maximum execution timeout in seconds from MCP security config.

        This is used to enforce timeout on tool executions.

        Returns:
            Timeout in seconds (default: 30.0)
        """
        security_config = await self._get_mcp_security()
        if security_config:
            max_ms = security_config.get('max_execution_time_ms', 30000)
            return max_ms / 1000.0
        return 30.0

    async def get_max_concurrent_tools(self) -> int:
        """
        Get the maximum number of concurrent tool executions from MCP security config.

        Returns:
            Max concurrent tools (default: 5)
        """
        security_config = await self._get_mcp_security()
        if security_config:
            return security_config.get('max_concurrent_tools', 5)
        return 5

    def _is_domain_allowed(self, url: str, security_config: Optional[Dict]) -> bool:
        """
        Check if a URL's domain is allowed based on MCP security config.

        Args:
            url: The URL to check
            security_config: MCP security configuration dict

        Returns:
            True if domain is allowed, False otherwise
        """
        from urllib.parse import urlparse

        if not security_config:
            # No security config - only allow localhost
            return self._extract_domain(url) in ['localhost', '127.0.0.1']

        domain = self._extract_domain(url)
        if not domain:
            return False

        # Check blocklist first (takes precedence)
        blocked_domains = security_config.get('blocked_domains', [])
        if self._domain_matches(domain, blocked_domains):
            return False

        # Check allowlist
        allowed_domains = security_config.get('allowed_domains', [])
        if not allowed_domains:
            # Empty allowlist means only localhost
            return domain in ['localhost', '127.0.0.1']

        return self._domain_matches(domain, allowed_domains)

    def _extract_domain(self, url: str) -> Optional[str]:
        """Extract domain from URL."""
        from urllib.parse import urlparse

        try:
            # Handle URLs without scheme
            if not url.startswith(('http://', 'https://')):
                url = 'http://' + url

            parsed = urlparse(url)
            domain = parsed.hostname
            return domain.lower() if domain else None
        except Exception:
            return None

    def _domain_matches(self, domain: str, patterns: List[str]) -> bool:
        """Check if domain matches any pattern in list (supports wildcards)."""
        domain = domain.lower()

        for pattern in patterns:
            pattern = pattern.lower()

            # Exact match
            if domain == pattern:
                return True

            # Wildcard match (*.example.com matches sub.example.com but not notexample.com)
            if pattern.startswith("*."):
                suffix = pattern[2:]  # Remove "*."
                # Must be exact suffix match or have a dot before the suffix
                if domain == suffix:
                    return True
                if domain.endswith("." + suffix):
                    return True

        return False

    def _mcp_to_openai_schema(self, mcp_tool: Dict) -> Dict:
        """Convert MCP tool schema to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": mcp_tool.get('name', 'unknown'),
                "description": mcp_tool.get('description', ''),
                "parameters": mcp_tool.get('inputSchema', {
                    "type": "object",
                    "properties": {},
                    "required": []
                })
            }
        }

    def get_all_tools(self, guest_mode: bool = False) -> List[Tool]:
        """
        Get merged tool list with priority resolution.
        Static tools override MCP tools override legacy tools.
        """
        if not self._feature_flags.get('tool_system_enabled', True):
            return []

        # Start with legacy (lowest priority)
        merged: Dict[str, Tool] = dict(self._legacy_tools)

        # Override with MCP (medium priority)
        merged.update(self._mcp_tools)

        # Override with static (highest priority)
        merged.update(self._static_tools)

        tools = list(merged.values())

        # Filter by guest mode if needed
        if guest_mode:
            tools = [t for t in tools if t.guest_mode_allowed]

        # Filter by enabled
        tools = [t for t in tools if t.enabled]

        # Sort by priority (lower = first)
        tools.sort(key=lambda t: t.priority)

        return tools

    def get_tool(self, tool_name: str) -> Optional[Tool]:
        """Get a specific tool with priority resolution."""
        # Check in priority order
        if tool_name in self._static_tools:
            return self._static_tools[tool_name]
        if tool_name in self._mcp_tools:
            return self._mcp_tools[tool_name]
        if tool_name in self._legacy_tools:
            return self._legacy_tools[tool_name]
        return None

    def get_tool_schemas(self, guest_mode: bool = False) -> List[Dict]:
        """Get OpenAI function schemas for LLM tool calling."""
        tools = self.get_all_tools(guest_mode)
        return [t.function_schema for t in tools if t.function_schema]

    def get_tools_by_source(self, source: ToolSource) -> List[Tool]:
        """Get tools filtered by source."""
        if source == ToolSource.STATIC:
            return list(self._static_tools.values())
        elif source == ToolSource.MCP:
            return list(self._mcp_tools.values())
        elif source == ToolSource.LEGACY:
            return list(self._legacy_tools.values())
        return []

    def get_tool_stats(self) -> Dict[str, Any]:
        """Get statistics about loaded tools."""
        return {
            'static_count': len(self._static_tools),
            'mcp_count': len(self._mcp_tools),
            'legacy_count': len(self._legacy_tools),
            'total_unique': len(self.get_all_tools()),
            'cache_age_seconds': time.time() - self._cache_time if self._cache_time else None,
            'features': {
                'mcp_enabled': self.is_feature_enabled('mcp_integration'),
                'n8n_enabled': self.is_feature_enabled('n8n_integration'),
                'legacy_fallback': self._feature_flags.get('legacy_tools_fallback', True),
            }
        }


# ============================================================================
# Factory Pattern for Dependency Injection
# ============================================================================

class ToolRegistryFactory:
    """
    Factory for creating tool registry instances.

    Why not singleton?
    - Singletons make unit testing difficult (global state)
    - Singletons prevent horizontal scaling
    - Singletons hide dependencies

    Usage in FastAPI:
        from fastapi import Depends

        async def get_registry(request: Request) -> UnifiedToolRegistry:
            return request.app.state.tool_registry

        @app.post("/query")
        async def query(registry: UnifiedToolRegistry = Depends(get_registry)):
            tools = registry.get_all_tools()
    """

    _instances: Dict[str, UnifiedToolRegistry] = {}

    @classmethod
    async def create(cls, instance_id: str = "default") -> UnifiedToolRegistry:
        """Create or get a named registry instance."""
        if instance_id not in cls._instances:
            registry = UnifiedToolRegistry()
            await registry.initialize()
            cls._instances[instance_id] = registry
        return cls._instances[instance_id]

    @classmethod
    def get(cls, instance_id: str = "default") -> Optional[UnifiedToolRegistry]:
        """Get existing registry (returns None if not created)."""
        return cls._instances.get(instance_id)

    @classmethod
    async def refresh(cls, instance_id: str = "default"):
        """Refresh a registry instance."""
        if instance_id in cls._instances:
            await cls._instances[instance_id].refresh()

    @classmethod
    def clear(cls):
        """Clear all instances (for testing)."""
        cls._instances.clear()


# ============================================================================
# FastAPI Dependency Injection Helper
# ============================================================================

async def get_tool_registry_dependency(request) -> UnifiedToolRegistry:
    """
    FastAPI dependency for tool registry.

    Usage:
        @app.post("/query")
        async def query(registry = Depends(get_tool_registry_dependency)):
            ...
    """
    # Prefer app state (set during lifespan)
    if hasattr(request.app.state, 'tool_registry'):
        return request.app.state.tool_registry

    # Fallback to factory
    return await ToolRegistryFactory.create()


# ============================================================================
# Backwards Compatibility (Deprecated)
# ============================================================================

async def get_tool_registry() -> UnifiedToolRegistry:
    """
    DEPRECATED: Use ToolRegistryFactory.create() or dependency injection.
    Kept for backwards compatibility during migration.
    """
    import warnings
    warnings.warn(
        "get_tool_registry() is deprecated. Use ToolRegistryFactory.create() or dependency injection.",
        DeprecationWarning
    )
    return await ToolRegistryFactory.create()


# ============================================================================
# Testing/CLI Entry Point
# ============================================================================

if __name__ == "__main__":
    import asyncio

    async def test():
        """Test the unified tool registry."""
        print("Testing Unified Tool Registry...")
        print("-" * 50)

        registry = await ToolRegistryFactory.create()
        stats = registry.get_tool_stats()

        print(f"Static tools: {stats['static_count']}")
        print(f"MCP tools: {stats['mcp_count']}")
        print(f"Legacy tools: {stats['legacy_count']}")
        print(f"Total unique: {stats['total_unique']}")
        print()

        print("All tools:")
        for tool in registry.get_all_tools():
            source_label = tool.source.name_str.upper()
            print(f"  [{source_label}] {tool.name}: {tool.description[:50]}...")

        print()
        print("Feature flags:")
        for flag, enabled in stats['features'].items():
            status = "ON" if enabled else "OFF"
            print(f"  {flag}: {status}")

    asyncio.run(test())
