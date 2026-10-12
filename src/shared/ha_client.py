"""Home Assistant API client for Project Athena.

A thin typed layer over ``shared.ha_http.HAChannel``: the channel owns the
credentials, the failed-sign-in gate and the transport, so this client holds no
URL, no token and no HTTP client of its own.
"""

import logging
from typing import List, Optional, Dict, Any

from shared.ha_http import HAChannel, HomeAssistantNotConfiguredError

logger = logging.getLogger(__name__)

__all__ = ["HomeAssistantClient", "HomeAssistantNotConfiguredError"]


class HomeAssistantClient:
    """Client for interacting with Home Assistant API.

    This client supports graceful degradation: when Home Assistant is not
    configured (no address and token under External API Keys or in
    ``HA_URL``/``HA_TOKEN``), ``is_configured`` is false and each method raises
    ``HomeAssistantNotConfiguredError`` with a helpful message.
    """

    def __init__(self, channel: HAChannel):
        self._channel = channel

    def _check_configured(self) -> None:
        """Check if HA is configured and raise helpful error if not."""
        if not self._channel.configured:
            raise HomeAssistantNotConfiguredError(
                "Home Assistant is not configured. "
                "Save its address and a long-lived access token under External API Keys "
                "(home-assistant), or set the HA_URL and HA_TOKEN environment variables."
            )

    @property
    def is_configured(self) -> bool:
        """Check if Home Assistant is configured."""
        return self._channel.configured

    async def get_state(self, entity_id: str) -> Dict[str, Any]:
        """Get the state of an entity."""
        self._check_configured()
        response = await self._channel.request("GET", f"/api/states/{entity_id}")
        response.raise_for_status()
        return response.json()

    async def get_states(self) -> List[Dict[str, Any]]:
        """Get every entity's state in one bulk call (ATHENA-69, D10:
        a read, exposed through PermissionEnforcingHAClient's read
        allowlist so bulk-state callers never need the raw, unwrapped
        client)."""
        self._check_configured()
        response = await self._channel.request("GET", "/api/states")
        response.raise_for_status()
        return response.json()

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Call a Home Assistant service."""
        self._check_configured()
        import structlog
        slog = structlog.get_logger()

        path = f"/api/services/{domain}/{service}"
        slog.info(
            "ha_call_service_request",
            domain=domain,
            service=service,
            service_data=service_data,
            path=path
        )

        try:
            response = await self._channel.request("POST", path, json=service_data or {})
            slog.info(
                "ha_call_service_response",
                domain=domain,
                service=service,
                status_code=response.status_code,
                response_text_len=len(response.text or "")
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            slog.error(
                "ha_call_service_error",
                domain=domain,
                service=service,
                error=str(e)
            )
            raise

    async def create_automation(
        self,
        automation_id: str,
        config: Dict[str, Any]
    ) -> bool:
        """
        Create or update an automation in Home Assistant.

        Uses the HA config API to create automations.

        Args:
            automation_id: Unique ID for the automation
            config: Automation configuration with alias, trigger, condition, action

        Returns:
            True if automation was created successfully
        """
        self._check_configured()
        import structlog
        slog = structlog.get_logger()

        try:
            # Ensure config has the required ID
            config_with_id = {**config, "id": automation_id}

            # HA's config/automation/config endpoint
            path = f"/api/config/automation/config/{automation_id}"

            slog.debug(
                "ha_create_automation_request",
                automation_id=automation_id,
                config=config_with_id
            )

            response = await self._channel.request("POST", path, json=config_with_id)

            if response.status_code in (200, 201):
                # Reload automations to make the new one active
                await self.call_service("automation", "reload")
                return True
            else:
                slog.warning(
                    "ha_create_automation_rejected",
                    automation_id=automation_id,
                    status_code=response.status_code,
                    response_text_len=len(response.text or "")
                )
                return False

        except Exception as e:
            slog.error(
                "ha_create_automation_exception",
                automation_id=automation_id,
                error=str(e)
            )
            return False

    async def delete_automation(self, automation_id: str) -> bool:
        """
        Delete an automation from Home Assistant.

        Args:
            automation_id: ID of the automation to delete

        Returns:
            True if automation was deleted successfully
        """
        self._check_configured()
        try:
            path = f"/api/config/automation/config/{automation_id}"
            response = await self._channel.request("DELETE", path)

            if response.status_code in (200, 204):
                await self.call_service("automation", "reload")
                return True
            return False

        except Exception:
            return False

    async def disable_automation(self, automation_id: str) -> bool:
        """
        Disable an automation in Home Assistant.

        Args:
            automation_id: ID of the automation to disable

        Returns:
            True if automation was disabled successfully
        """
        self._check_configured()
        try:
            # Find the entity_id for this automation
            entity_id = f"automation.{automation_id}"
            await self.call_service("automation", "turn_off", {"entity_id": entity_id})
            return True
        except Exception:
            return False

    async def close(self):
        """Close the channel (its HTTP client and any background refresh)."""
        await self._channel.aclose()
