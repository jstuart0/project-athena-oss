"""
Module Registry for Project Athena.
Defines available modules, their components, and provides enable/disable checks.

This module provides a centralized registry for all Athena modules (Home Assistant,
Guest Mode, Notifications, etc.) with health checking and admin tab visibility control.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum
import os
import time
import logging

logger = logging.getLogger(__name__)


class ModuleStatus(Enum):
    """Status of a module."""
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"  # Enabled but service not responding


@dataclass
class ModuleComponent:
    """A component (service, UI tab, database table) belonging to a module."""
    name: str
    component_type: str  # "service", "admin_tab", "database", "api_route"
    service_port: Optional[int] = None
    health_endpoint: Optional[str] = None
    admin_tab_id: Optional[str] = None
    database_tables: List[str] = field(default_factory=list)


@dataclass
class Module:
    """Definition of an Athena module."""
    id: str
    name: str
    description: str
    env_var: str  # Environment variable to enable/disable
    components: List[ModuleComponent]
    dependencies: List[str] = field(default_factory=list)  # Other module IDs
    default_enabled: bool = True


# Module Definitions
MODULES: Dict[str, Module] = {
    "home_assistant": Module(
        id="home_assistant",
        name="Home Assistant Integration",
        description="Smart home control, music playback, TV control, and automation",
        env_var="MODULE_HOME_ASSISTANT",
        components=[
            ModuleComponent("ha_client", "service"),
            ModuleComponent("smart_controller", "service"),
            ModuleComponent("music_handler", "service"),
            ModuleComponent("tv_handler", "service"),
            ModuleComponent("automation_agent", "service"),
            ModuleComponent("follow_me_audio", "service"),
            ModuleComponent("room_audio", "admin_tab", admin_tab_id="room-audio"),
            ModuleComponent("room_tv", "admin_tab", admin_tab_id="room-tv"),
            ModuleComponent("ha_pipelines", "admin_tab", admin_tab_id="voice-pipelines"),
            ModuleComponent("follow_me", "admin_tab", admin_tab_id="follow-me"),
            ModuleComponent("music_config", "admin_tab", admin_tab_id="music-config"),
        ],
        default_enabled=True
    ),
    "guest_mode": Module(
        id="guest_mode",
        name="Guest Mode",
        description="Airbnb/vacation rental guest restrictions and calendar integration",
        env_var="MODULE_GUEST_MODE",
        components=[
            ModuleComponent("mode_service", "service", service_port=8022, health_endpoint="/health"),
            ModuleComponent("guest_mode_tab", "admin_tab", admin_tab_id="guest-mode"),
            ModuleComponent("guest_mode_db", "database", database_tables=[
                "guest_mode_config", "calendar_events", "mode_overrides",
                "mode_audit_log", "guest_mode_config_history", "calendar_sources",
                "guests", "guest_sessions"
            ]),
        ],
        default_enabled=True
    ),
    "notifications": Module(
        id="notifications",
        name="Proactive Notifications",
        description="Context-aware notifications via voice TTS",
        env_var="MODULE_NOTIFICATIONS",
        components=[
            ModuleComponent("notifications_service", "service", service_port=8050, health_endpoint="/health"),
            ModuleComponent("notifications_tab", "admin_tab", admin_tab_id="notifications"),
            ModuleComponent("notifications_db", "database", database_tables=[
                "notif_rules", "notif_preferences", "notif_history",
                "notif_templates", "notif_rooms", "notif_cooldowns"
            ]),
        ],
        default_enabled=True
    ),
    "monitoring": Module(
        id="monitoring",
        name="Monitoring & Observability",
        description="Grafana dashboards and Prometheus metrics",
        env_var="MODULE_MONITORING",
        components=[
            ModuleComponent("prometheus", "service", service_port=9090, health_endpoint="/-/healthy"),
            ModuleComponent("grafana", "service", service_port=3000, health_endpoint="/api/health"),
            ModuleComponent("monitoring_tab", "admin_tab", admin_tab_id="monitoring"),
        ],
        default_enabled=False  # Not deployed by default
    ),
    "jarvis_web": Module(
        id="jarvis_web",
        name="Jarvis Web Interface",
        description="Browser-based voice interface with real-time pipeline monitoring",
        env_var="MODULE_JARVIS_WEB",
        components=[
            ModuleComponent("jarvis_web_app", "service", service_port=3001, health_endpoint="/health"),
            ModuleComponent("admin_jarvis", "admin_tab", admin_tab_id="admin-jarvis"),
            ModuleComponent("livekit_integration", "service"),
            ModuleComponent("browser_music_player", "service"),
        ],
        default_enabled=True
    ),
}


@dataclass
class HealthCheckResult:
    """Cached health check result."""
    status: ModuleStatus
    checked_at: float  # timestamp
    response_time_ms: Optional[float] = None
    error_message: Optional[str] = None


class ModuleRegistry:
    """
    Registry for checking module status and availability.

    Features:
    - Service URL discovery (not hardcoded localhost)
    - Health check caching with configurable TTL
    - Support for Docker/Kubernetes service DNS
    """

    # Cache TTL in seconds (configurable via env)
    CACHE_TTL = int(os.getenv("MODULE_HEALTH_CACHE_TTL", "30"))

    def __init__(self):
        self._health_cache: Dict[str, HealthCheckResult] = {}
        self._service_urls: Dict[str, str] = {}  # Override URLs from config
        self._http_client: Optional[Any] = None

    async def _get_client(self) -> Any:
        """Get or create HTTP client for health checks."""
        if self._http_client is None:
            try:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=5.0)
            except ImportError:
                logger.warning("httpx not available for health checks")
                return None
        return self._http_client

    def configure_service_url(self, component_name: str, url: str):
        """
        Configure custom URL for a service component.
        Use this to override default localhost URLs with Docker/K8s service names.

        Example:
            registry.configure_service_url("mode_service", "http://mode-service:8022")
            registry.configure_service_url("notifications_service", "http://notifications.athena.svc.cluster.local:8050")
        """
        self._service_urls[component_name] = url
        # Invalidate cache for this component
        if component_name in self._health_cache:
            del self._health_cache[component_name]

    def _get_service_url(self, component: ModuleComponent) -> str:
        """
        Get URL for a service component.

        Priority:
        1. Explicitly configured URL via configure_service_url()
        2. Environment variable: {COMPONENT_NAME}_URL (e.g., MODE_SERVICE_URL)
        3. Default: http://localhost:{port}
        """
        # Check explicit configuration
        if component.name in self._service_urls:
            return self._service_urls[component.name]

        # Check environment variable
        env_var = f"{component.name.upper()}_URL"
        env_url = os.getenv(env_var)
        if env_url:
            return env_url

        # Fall back to localhost (development default)
        if component.service_port:
            return f"http://localhost:{component.service_port}"

        return ""

    def is_enabled(self, module_id: str) -> bool:
        """Check if a module is enabled via environment variable."""
        if module_id not in MODULES:
            return False
        module = MODULES[module_id]
        return os.getenv(module.env_var, str(module.default_enabled)).lower() == "true"

    async def get_status(self, module_id: str, use_cache: bool = True) -> ModuleStatus:
        """
        Get module status including service health check.

        Args:
            module_id: ID of module to check
            use_cache: Whether to use cached results (default True)

        Returns:
            ModuleStatus enum value
        """
        if not self.is_enabled(module_id):
            return ModuleStatus.DISABLED

        module = MODULES[module_id]
        for component in module.components:
            if component.service_port and component.health_endpoint:
                status = await self._check_component_health(component, use_cache)
                if status != ModuleStatus.ENABLED:
                    return status

        return ModuleStatus.ENABLED

    def get_status_sync(self, module_id: str) -> ModuleStatus:
        """
        Get module status synchronously (no health checks).

        Returns ENABLED or DISABLED based only on environment variable.
        """
        if not self.is_enabled(module_id):
            return ModuleStatus.DISABLED
        return ModuleStatus.ENABLED

    async def _check_component_health(
        self, component: ModuleComponent, use_cache: bool = True
    ) -> ModuleStatus:
        """Check health of a single component with caching."""
        cache_key = component.name

        # Check cache
        if use_cache and cache_key in self._health_cache:
            cached = self._health_cache[cache_key]
            age = time.time() - cached.checked_at
            if age < self.CACHE_TTL:
                return cached.status

        # Perform health check
        service_url = self._get_service_url(component)
        if not service_url:
            return ModuleStatus.UNAVAILABLE

        health_url = f"{service_url.rstrip('/')}{component.health_endpoint}"

        try:
            client = await self._get_client()
            if client is None:
                return ModuleStatus.ENABLED  # Assume enabled if can't check

            start = time.time()
            resp = await client.get(health_url)
            response_time = (time.time() - start) * 1000  # ms

            if resp.status_code == 200:
                status = ModuleStatus.ENABLED
                error = None
            else:
                status = ModuleStatus.UNAVAILABLE
                error = f"HTTP {resp.status_code}"

        except Exception as e:
            status = ModuleStatus.UNAVAILABLE
            error = str(e)
            response_time = None

        # Cache result
        self._health_cache[cache_key] = HealthCheckResult(
            status=status,
            checked_at=time.time(),
            response_time_ms=response_time if 'response_time' in dir() else None,
            error_message=error if 'error' in dir() else None
        )

        return status

    def get_cached_health(self, component_name: str) -> Optional[HealthCheckResult]:
        """Get cached health result for a component (for admin UI display)."""
        return self._health_cache.get(component_name)

    def invalidate_cache(self, module_id: Optional[str] = None):
        """
        Invalidate health cache.

        Args:
            module_id: If provided, only invalidate cache for this module's components.
                      If None, invalidate all cache.
        """
        if module_id is None:
            self._health_cache.clear()
        elif module_id in MODULES:
            module = MODULES[module_id]
            for component in module.components:
                if component.name in self._health_cache:
                    del self._health_cache[component.name]

    def get_enabled_modules(self) -> List[str]:
        """Get list of enabled module IDs."""
        return [mid for mid in MODULES.keys() if self.is_enabled(mid)]

    def get_enabled_admin_tabs(self) -> List[str]:
        """Get list of admin tab IDs for enabled modules."""
        tabs = []
        for module_id, module in MODULES.items():
            if self.is_enabled(module_id):
                for component in module.components:
                    if component.admin_tab_id:
                        tabs.append(component.admin_tab_id)
        return tabs

    def get_module_info(self, module_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a specific module."""
        if module_id not in MODULES:
            return None

        module = MODULES[module_id]
        return {
            "id": module.id,
            "name": module.name,
            "description": module.description,
            "enabled": self.is_enabled(module_id),
            "env_var": module.env_var,
            "default_enabled": module.default_enabled,
            "components": [
                {
                    "name": c.name,
                    "type": c.component_type,
                    "admin_tab_id": c.admin_tab_id,
                    "service_port": c.service_port,
                }
                for c in module.components
            ]
        }

    def get_all_modules_info(self) -> List[Dict[str, Any]]:
        """Get information about all modules."""
        return [self.get_module_info(mid) for mid in MODULES.keys()]

    async def close(self):
        """Close HTTP client on shutdown."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# Singleton
module_registry = ModuleRegistry()


# =========================================================================
# Service URL Configuration (call at startup)
# =========================================================================
def configure_module_urls_from_env():
    """
    Configure module service URLs from environment variables.
    Call this at application startup.

    Recognized environment variables:
    - MODE_SERVICE_URL: URL for guest mode service
    - NOTIFICATIONS_SERVICE_URL: URL for notifications service
    - PROMETHEUS_URL: URL for Prometheus
    - GRAFANA_URL: URL for Grafana
    """
    url_mappings = {
        "mode_service": "MODE_SERVICE_URL",
        "notifications_service": "NOTIFICATIONS_SERVICE_URL",
        "prometheus": "PROMETHEUS_URL",
        "grafana": "GRAFANA_URL",
    }

    for component_name, env_var in url_mappings.items():
        url = os.getenv(env_var)
        if url:
            module_registry.configure_service_url(component_name, url)
            logger.info(f"Configured {component_name} URL: {url}")


def get_module_registry() -> ModuleRegistry:
    """Get the singleton module registry instance."""
    return module_registry
