"""
Module status API endpoints.

Provides endpoints for checking module status and getting enabled admin tabs.
Used by the admin frontend to dynamically show/hide tabs based on enabled modules.
"""
from fastapi import APIRouter
from typing import List, Dict, Any
import sys
import os

# Add src to path so we can import shared modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..', 'src'))

from shared.module_registry import module_registry, MODULES, ModuleStatus

router = APIRouter(prefix="/api/modules", tags=["modules"])


@router.get("/")
async def list_modules() -> List[Dict[str, Any]]:
    """
    List all available modules with their status.

    Returns a list of module objects with:
    - id: Module identifier
    - name: Human-readable name
    - description: Module description
    - enabled: Whether the module is enabled (from env var)
    - status: Current status (enabled, disabled, unavailable)
    - components: List of module components
    """
    result = []
    for module_id, module in MODULES.items():
        status = await module_registry.get_status(module_id)
        result.append({
            "id": module.id,
            "name": module.name,
            "description": module.description,
            "enabled": module_registry.is_enabled(module_id),
            "status": status.value,
            "env_var": module.env_var,
            "default_enabled": module.default_enabled,
            "components": [
                {
                    "name": c.name,
                    "type": c.component_type,
                    "admin_tab_id": c.admin_tab_id,
                    "service_port": c.service_port,
                    "health_endpoint": c.health_endpoint,
                }
                for c in module.components
            ]
        })
    return result


@router.get("/admin-tabs")
async def get_enabled_admin_tabs() -> List[str]:
    """
    Get list of admin tabs that should be visible.

    Returns a list of tab IDs for enabled modules. The admin frontend
    uses this to show/hide tabs based on which modules are enabled.

    Example response: ["room-audio", "room-tv", "voice-pipelines", "guest-mode", "notifications"]
    """
    return module_registry.get_enabled_admin_tabs()


@router.get("/enabled")
async def get_enabled_modules() -> List[str]:
    """
    Get list of enabled module IDs.

    Returns a simple list of module IDs that are currently enabled.
    Example response: ["home_assistant", "guest_mode", "notifications"]
    """
    return module_registry.get_enabled_modules()


@router.get("/{module_id}")
async def get_module_status(module_id: str) -> Dict[str, Any]:
    """
    Get status of a specific module.

    Args:
        module_id: The module identifier (e.g., "home_assistant", "guest_mode")

    Returns:
        Module information including id, name, enabled status, and current status.
        Returns error object if module not found.
    """
    if module_id not in MODULES:
        return {"error": f"Module '{module_id}' not found", "available_modules": list(MODULES.keys())}

    module = MODULES[module_id]
    status = await module_registry.get_status(module_id)

    # Get health info for components with health endpoints
    component_health = []
    for component in module.components:
        if component.health_endpoint:
            cached_health = module_registry.get_cached_health(component.name)
            component_health.append({
                "name": component.name,
                "port": component.service_port,
                "health_endpoint": component.health_endpoint,
                "cached_status": cached_health.status.value if cached_health else None,
                "last_checked": cached_health.checked_at if cached_health else None,
                "response_time_ms": cached_health.response_time_ms if cached_health else None,
                "error": cached_health.error_message if cached_health else None,
            })

    return {
        "id": module.id,
        "name": module.name,
        "description": module.description,
        "enabled": module_registry.is_enabled(module_id),
        "status": status.value,
        "env_var": module.env_var,
        "component_health": component_health,
    }


@router.post("/{module_id}/refresh")
async def refresh_module_health(module_id: str) -> Dict[str, Any]:
    """
    Force refresh health checks for a module.

    Invalidates the health cache for the specified module and performs
    fresh health checks on all service components.

    Args:
        module_id: The module identifier

    Returns:
        Updated module status after refresh
    """
    if module_id not in MODULES:
        return {"error": f"Module '{module_id}' not found"}

    # Invalidate cache for this module
    module_registry.invalidate_cache(module_id)

    # Get fresh status (will perform new health checks)
    status = await module_registry.get_status(module_id, use_cache=False)

    return {
        "id": module_id,
        "status": status.value,
        "refreshed": True,
    }


@router.post("/refresh-all")
async def refresh_all_module_health() -> Dict[str, Any]:
    """
    Force refresh health checks for all modules.

    Invalidates the entire health cache and returns updated status
    for all modules.
    """
    # Invalidate all cache
    module_registry.invalidate_cache()

    # Get fresh status for all modules
    results = {}
    for module_id in MODULES.keys():
        status = await module_registry.get_status(module_id, use_cache=False)
        results[module_id] = status.value

    return {
        "refreshed": True,
        "modules": results,
    }
