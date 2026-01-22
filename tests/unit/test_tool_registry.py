"""
Unit tests for Unified Tool Registry.

Tests priority resolution, domain filtering, and tool source merging.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from dataclasses import dataclass

# Import the tool registry components
import sys
sys.path.insert(0, '/Users/jaystuart/dev/project-athena/src')

from shared.tool_registry import (
    Tool,
    ToolSource,
    UnifiedToolRegistry,
    ToolRegistryFactory,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_admin_client():
    """Mock admin config client."""
    mock = AsyncMock()
    mock.get_feature_flags = AsyncMock(return_value={
        'tool_system_enabled': True,
        'mcp_integration': False,
        'n8n_integration': False,
        'legacy_tools_fallback': True,
    })
    mock.get_enabled_tools = AsyncMock(return_value=[])
    mock.admin_url = "http://localhost:8080"
    mock.client = AsyncMock()
    return mock


@pytest.fixture
def sample_static_tools():
    """Sample static tools from Admin API."""
    return [
        {
            'tool_name': 'get_weather',
            'display_name': 'Weather Forecast',
            'description': 'Get weather for a location',
            'function_schema': {'type': 'function', 'function': {'name': 'get_weather'}},
            'service_url': 'http://localhost:8010',
            'enabled': True,
            'guest_mode_allowed': True,
            'timeout_seconds': 15,
        },
        {
            'tool_name': 'get_sports_scores',
            'display_name': 'Sports Scores',
            'description': 'Get sports scores',
            'function_schema': {'type': 'function', 'function': {'name': 'get_sports_scores'}},
            'service_url': 'http://localhost:8017',
            'enabled': True,
            'guest_mode_allowed': True,
            'timeout_seconds': 20,
        },
    ]


@pytest.fixture
def sample_legacy_tools():
    """Sample legacy tools from rag_tools.py."""
    return [
        {
            'tool_name': 'get_weather',
            'display_name': 'Weather (Legacy)',
            'description': 'Legacy weather tool',
            'function_schema': {'type': 'function', 'function': {'name': 'get_weather'}},
            'service_url': 'http://localhost:8010',
            'guest_mode_allowed': True,
            'timeout_seconds': 30,
        },
        {
            'tool_name': 'search_web',
            'display_name': 'Web Search',
            'description': 'Search the web',
            'function_schema': {'type': 'function', 'function': {'name': 'search_web'}},
            'service_url': 'http://localhost:8018',
            'guest_mode_allowed': True,
            'timeout_seconds': 20,
        },
    ]


# =============================================================================
# ToolSource Tests
# =============================================================================

class TestToolSource:
    """Tests for ToolSource enum."""

    def test_static_has_highest_priority(self):
        """Static tools should have lowest priority number (highest priority)."""
        assert ToolSource.STATIC.priority < ToolSource.MCP.priority
        assert ToolSource.STATIC.priority < ToolSource.LEGACY.priority

    def test_mcp_has_medium_priority(self):
        """MCP tools should have medium priority."""
        assert ToolSource.MCP.priority > ToolSource.STATIC.priority
        assert ToolSource.MCP.priority < ToolSource.LEGACY.priority

    def test_legacy_has_lowest_priority(self):
        """Legacy tools should have highest priority number (lowest priority)."""
        assert ToolSource.LEGACY.priority > ToolSource.STATIC.priority
        assert ToolSource.LEGACY.priority > ToolSource.MCP.priority

    def test_priority_values(self):
        """Verify specific priority values."""
        assert ToolSource.STATIC.priority == 10
        assert ToolSource.MCP.priority == 50
        assert ToolSource.LEGACY.priority == 100

    def test_name_string(self):
        """Verify name string values."""
        assert ToolSource.STATIC.name_str == "static"
        assert ToolSource.MCP.name_str == "mcp"
        assert ToolSource.LEGACY.name_str == "legacy"


# =============================================================================
# Tool Dataclass Tests
# =============================================================================

class TestTool:
    """Tests for Tool dataclass."""

    def test_tool_defaults(self):
        """Test Tool dataclass default values."""
        tool = Tool(
            name="test_tool",
            display_name="Test Tool",
            description="A test tool",
            function_schema={"type": "function"},
        )

        assert tool.source == ToolSource.LEGACY
        assert tool.priority == 100
        assert tool.enabled is True
        assert tool.guest_mode_allowed is True
        assert tool.requires_api_key is False
        assert tool.api_key_service is None
        assert tool.timeout_seconds == 20
        assert tool.metadata == {}

    def test_tool_custom_values(self):
        """Test Tool dataclass with custom values."""
        tool = Tool(
            name="custom_tool",
            display_name="Custom Tool",
            description="A custom tool",
            function_schema={"type": "function"},
            source=ToolSource.STATIC,
            priority=10,
            enabled=False,
            guest_mode_allowed=False,
            requires_api_key=True,
            api_key_service="google-places",
            timeout_seconds=30,
            metadata={"custom": "data"},
        )

        assert tool.source == ToolSource.STATIC
        assert tool.priority == 10
        assert tool.enabled is False
        assert tool.guest_mode_allowed is False
        assert tool.requires_api_key is True
        assert tool.api_key_service == "google-places"
        assert tool.timeout_seconds == 30
        assert tool.metadata == {"custom": "data"}


# =============================================================================
# Priority Resolution Tests
# =============================================================================

class TestPriorityResolution:
    """Tests for priority-based tool resolution."""

    @pytest.fixture
    def registry_with_tools(self):
        """Create a registry with pre-loaded tools."""
        registry = UnifiedToolRegistry()
        registry._initialized = True
        registry._feature_flags = {'tool_system_enabled': True}

        # Add tools from all three sources with overlapping names
        registry._legacy_tools = {
            'get_weather': Tool(
                name='get_weather',
                display_name='Weather (Legacy)',
                description='Legacy weather',
                function_schema={},
                source=ToolSource.LEGACY,
                priority=100,
            ),
            'search_web': Tool(
                name='search_web',
                display_name='Web Search (Legacy)',
                description='Legacy web search',
                function_schema={},
                source=ToolSource.LEGACY,
                priority=100,
            ),
        }

        registry._mcp_tools = {
            'get_weather': Tool(
                name='get_weather',
                display_name='Weather (MCP)',
                description='MCP weather',
                function_schema={},
                source=ToolSource.MCP,
                priority=50,
            ),
            'mcp_only_tool': Tool(
                name='mcp_only_tool',
                display_name='MCP Only Tool',
                description='Only in MCP',
                function_schema={},
                source=ToolSource.MCP,
                priority=50,
            ),
        }

        registry._static_tools = {
            'get_weather': Tool(
                name='get_weather',
                display_name='Weather (Static)',
                description='Static weather',
                function_schema={},
                source=ToolSource.STATIC,
                priority=10,
            ),
            'static_only_tool': Tool(
                name='static_only_tool',
                display_name='Static Only Tool',
                description='Only in static',
                function_schema={},
                source=ToolSource.STATIC,
                priority=10,
            ),
        }

        return registry

    def test_static_overrides_mcp(self, registry_with_tools):
        """Static tools should override MCP tools with same name."""
        tool = registry_with_tools.get_tool('get_weather')

        assert tool is not None
        assert tool.source == ToolSource.STATIC
        assert tool.display_name == 'Weather (Static)'

    def test_mcp_overrides_legacy(self, registry_with_tools):
        """MCP tools should override legacy tools with same name (when no static)."""
        # Remove static version
        del registry_with_tools._static_tools['get_weather']

        tool = registry_with_tools.get_tool('get_weather')

        assert tool is not None
        assert tool.source == ToolSource.MCP
        assert tool.display_name == 'Weather (MCP)'

    def test_legacy_used_when_no_override(self, registry_with_tools):
        """Legacy tools should be used when no higher priority version exists."""
        tool = registry_with_tools.get_tool('search_web')

        assert tool is not None
        assert tool.source == ToolSource.LEGACY
        assert tool.display_name == 'Web Search (Legacy)'

    def test_unique_tools_from_each_source(self, registry_with_tools):
        """Tools unique to each source should be included."""
        tools = registry_with_tools.get_all_tools()
        tool_names = [t.name for t in tools]

        assert 'static_only_tool' in tool_names
        assert 'mcp_only_tool' in tool_names
        assert 'search_web' in tool_names

    def test_merged_list_sorted_by_priority(self, registry_with_tools):
        """Merged tool list should be sorted by priority (lower first)."""
        tools = registry_with_tools.get_all_tools()

        for i in range(len(tools) - 1):
            assert tools[i].priority <= tools[i + 1].priority

    def test_total_unique_count(self, registry_with_tools):
        """Should have correct count of unique tools after merge."""
        tools = registry_with_tools.get_all_tools()

        # get_weather (deduplicated), search_web, mcp_only_tool, static_only_tool
        assert len(tools) == 4


# =============================================================================
# Domain Filtering Tests
# =============================================================================

class TestDomainFiltering:
    """Tests for MCP domain allowlist/blocklist filtering."""

    @pytest.fixture
    def registry(self):
        """Create a basic registry instance."""
        return UnifiedToolRegistry()

    def test_extract_domain_with_scheme(self, registry):
        """Test domain extraction from URLs with scheme."""
        assert registry._extract_domain('https://example.com/path') == 'example.com'
        assert registry._extract_domain('http://api.example.com:8080/path') == 'api.example.com'

    def test_extract_domain_without_scheme(self, registry):
        """Test domain extraction from URLs without scheme."""
        assert registry._extract_domain('example.com/path') == 'example.com'
        assert registry._extract_domain('localhost:8080') == 'localhost'

    def test_extract_domain_ip_address(self, registry):
        """Test domain extraction from IP addresses."""
        assert registry._extract_domain('http://localhost:8000') == 'localhost'
        assert registry._extract_domain('127.0.0.1:3000') == '127.0.0.1'

    def test_domain_matches_exact(self, registry):
        """Test exact domain matching."""
        patterns = ['example.com', 'api.test.com']

        assert registry._domain_matches('example.com', patterns) is True
        assert registry._domain_matches('api.test.com', patterns) is True
        assert registry._domain_matches('other.com', patterns) is False

    def test_domain_matches_wildcard(self, registry):
        """Test wildcard domain matching."""
        patterns = ['*.example.com']

        assert registry._domain_matches('api.example.com', patterns) is True
        assert registry._domain_matches('sub.api.example.com', patterns) is True
        assert registry._domain_matches('example.com', patterns) is True  # Suffix match
        assert registry._domain_matches('notexample.com', patterns) is False

    def test_is_domain_allowed_default_localhost(self, registry):
        """Test that localhost is allowed by default."""
        security_config = None

        assert registry._is_domain_allowed('http://localhost:8080', security_config) is True
        assert registry._is_domain_allowed('http://127.0.0.1:8000', security_config) is True
        assert registry._is_domain_allowed('http://example.com', security_config) is False

    def test_is_domain_allowed_allowlist(self, registry):
        """Test allowlist filtering."""
        security_config = {
            'allowed_domains': ['example.com', 'api.test.com', '*.internal.net'],
            'blocked_domains': [],
        }

        assert registry._is_domain_allowed('https://example.com', security_config) is True
        assert registry._is_domain_allowed('https://api.test.com', security_config) is True
        assert registry._is_domain_allowed('https://svc.internal.net', security_config) is True
        assert registry._is_domain_allowed('https://other.com', security_config) is False

    def test_is_domain_blocked_takes_precedence(self, registry):
        """Test that blocklist takes precedence over allowlist."""
        security_config = {
            'allowed_domains': ['*.example.com'],
            'blocked_domains': ['malicious.example.com'],
        }

        assert registry._is_domain_allowed('https://good.example.com', security_config) is True
        assert registry._is_domain_allowed('https://malicious.example.com', security_config) is False


# =============================================================================
# Guest Mode Filtering Tests
# =============================================================================

class TestGuestModeFiltering:
    """Tests for guest mode tool filtering."""

    @pytest.fixture
    def registry_with_mixed_tools(self):
        """Create a registry with guest and non-guest tools."""
        registry = UnifiedToolRegistry()
        registry._initialized = True
        registry._feature_flags = {'tool_system_enabled': True}

        registry._static_tools = {
            'public_tool': Tool(
                name='public_tool',
                display_name='Public Tool',
                description='Available to guests',
                function_schema={},
                source=ToolSource.STATIC,
                priority=10,
                guest_mode_allowed=True,
            ),
            'private_tool': Tool(
                name='private_tool',
                display_name='Private Tool',
                description='Owner only',
                function_schema={},
                source=ToolSource.STATIC,
                priority=10,
                guest_mode_allowed=False,
            ),
        }
        registry._mcp_tools = {}
        registry._legacy_tools = {}

        return registry

    def test_all_tools_without_guest_filter(self, registry_with_mixed_tools):
        """All tools should be returned when not filtering for guest mode."""
        tools = registry_with_mixed_tools.get_all_tools(guest_mode=False)

        assert len(tools) == 2

    def test_only_guest_tools_with_filter(self, registry_with_mixed_tools):
        """Only guest-allowed tools should be returned with guest_mode=True."""
        tools = registry_with_mixed_tools.get_all_tools(guest_mode=True)

        assert len(tools) == 1
        assert tools[0].name == 'public_tool'


# =============================================================================
# Feature Flag Tests
# =============================================================================

class TestFeatureFlags:
    """Tests for feature flag behavior."""

    def test_tool_system_disabled_returns_empty(self):
        """When tool_system_enabled is False, get_all_tools returns empty."""
        registry = UnifiedToolRegistry()
        registry._initialized = True
        registry._feature_flags = {'tool_system_enabled': False}
        registry._static_tools = {
            'test': Tool(name='test', display_name='Test', description='Test', function_schema={})
        }
        registry._mcp_tools = {}
        registry._legacy_tools = {}

        tools = registry.get_all_tools()

        assert len(tools) == 0

    def test_tool_system_enabled_returns_tools(self):
        """When tool_system_enabled is True, tools are returned."""
        registry = UnifiedToolRegistry()
        registry._initialized = True
        registry._feature_flags = {'tool_system_enabled': True}
        registry._static_tools = {
            'test': Tool(name='test', display_name='Test', description='Test', function_schema={})
        }
        registry._mcp_tools = {}
        registry._legacy_tools = {}

        tools = registry.get_all_tools()

        assert len(tools) == 1


# =============================================================================
# Factory Tests
# =============================================================================

class TestToolRegistryFactory:
    """Tests for ToolRegistryFactory."""

    def test_clear_removes_all_instances(self):
        """Factory.clear() should remove all cached instances."""
        ToolRegistryFactory._instances['test'] = UnifiedToolRegistry()
        ToolRegistryFactory._instances['test2'] = UnifiedToolRegistry()

        ToolRegistryFactory.clear()

        assert len(ToolRegistryFactory._instances) == 0

    def test_get_returns_none_for_missing(self):
        """Factory.get() should return None for non-existent instances."""
        ToolRegistryFactory.clear()

        result = ToolRegistryFactory.get('nonexistent')

        assert result is None


# =============================================================================
# Tool Stats Tests
# =============================================================================

class TestToolStats:
    """Tests for tool statistics."""

    @pytest.fixture
    def registry_with_various_tools(self):
        """Create a registry with tools from various sources."""
        registry = UnifiedToolRegistry()
        registry._initialized = True
        registry._feature_flags = {
            'tool_system_enabled': True,
            'mcp_integration': True,
            'legacy_tools_fallback': True,
        }
        registry._cache_time = 1234567890.0

        registry._static_tools = {
            'tool1': Tool(name='tool1', display_name='T1', description='', function_schema={}, source=ToolSource.STATIC),
            'tool2': Tool(name='tool2', display_name='T2', description='', function_schema={}, source=ToolSource.STATIC),
        }
        registry._mcp_tools = {
            'tool3': Tool(name='tool3', display_name='T3', description='', function_schema={}, source=ToolSource.MCP),
        }
        registry._legacy_tools = {
            'tool4': Tool(name='tool4', display_name='T4', description='', function_schema={}, source=ToolSource.LEGACY),
            'tool5': Tool(name='tool5', display_name='T5', description='', function_schema={}, source=ToolSource.LEGACY),
            'tool6': Tool(name='tool6', display_name='T6', description='', function_schema={}, source=ToolSource.LEGACY),
        }

        return registry

    def test_stats_counts_by_source(self, registry_with_various_tools):
        """Stats should correctly count tools by source."""
        stats = registry_with_various_tools.get_tool_stats()

        assert stats['static_count'] == 2
        assert stats['mcp_count'] == 1
        assert stats['legacy_count'] == 3

    def test_stats_total_unique(self, registry_with_various_tools):
        """Stats should show total unique tools after merge."""
        stats = registry_with_various_tools.get_tool_stats()

        assert stats['total_unique'] == 6

    def test_stats_cache_age(self, registry_with_various_tools):
        """Stats should include cache age."""
        stats = registry_with_various_tools.get_tool_stats()

        assert stats['cache_age_seconds'] is not None
        assert stats['cache_age_seconds'] > 0

    def test_stats_feature_flags(self, registry_with_various_tools):
        """Stats should include feature flag status."""
        stats = registry_with_various_tools.get_tool_stats()

        assert 'features' in stats
        assert stats['features']['mcp_enabled'] is True
        assert stats['features']['legacy_fallback'] is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
