"""Home Assistant API client for Project Athena"""

import os
import httpx
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class HomeAssistantNotConfiguredError(Exception):
    """Raised when Home Assistant is not configured but a method is called."""
    pass


class HomeAssistantClient:
    """Client for interacting with Home Assistant API.

    This client supports graceful degradation - if HA_URL and HA_TOKEN are not
    provided, the client will be marked as disabled and methods will raise
    HomeAssistantNotConfiguredError with a helpful message.
    """

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None):
        # Get URL from parameter, env var, or leave empty (no hardcoded default)
        self.url = url or os.getenv("HA_URL", "")
        self.token = token or os.getenv("HA_TOKEN", "")

        # Determine if HA is configured
        self._disabled = not (self.url and self.token)

        if self._disabled:
            logger.warning(
                "ha_not_configured",
                extra={"msg": "HomeAssistantClient will not function. Set HA_URL and HA_TOKEN to enable."}
            )
            self.headers = {}
            self.client = None
        else:
            self.headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            self.client = httpx.AsyncClient(
                base_url=self.url,
                headers=self.headers,
                verify=False,  # Self-signed cert
                timeout=30.0
            )

    def _check_configured(self) -> None:
        """Check if HA is configured and raise helpful error if not."""
        if self._disabled:
            raise HomeAssistantNotConfiguredError(
                "Home Assistant is not configured. "
                "Set HA_URL and HA_TOKEN environment variables, or disable the Home Assistant module "
                "by setting MODULE_HOME_ASSISTANT=false."
            )

    @property
    def is_configured(self) -> bool:
        """Check if Home Assistant is configured."""
        return not self._disabled
    
    async def get_state(self, entity_id: str) -> Dict[str, Any]:
        """Get the state of an entity."""
        self._check_configured()
        response = await self.client.get(f"/api/states/{entity_id}")
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

        url = f"/api/services/{domain}/{service}"
        slog.info(
            "ha_call_service_request",
            domain=domain,
            service=service,
            service_data=service_data,
            url=f"{self.url}{url}"
        )

        try:
            response = await self.client.post(url, json=service_data or {})
            slog.info(
                "ha_call_service_response",
                domain=domain,
                service=service,
                status_code=response.status_code,
                response_text=response.text[:200] if response.text else "empty"
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
    
    async def health_check(self) -> bool:
        """Check if Home Assistant is reachable.

        Returns False if not configured or if HA is unreachable.
        """
        if self._disabled:
            return False
        try:
            response = await self.client.get("/api/")
            return response.status_code == 200
        except Exception:
            return False

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
            url = f"/api/config/automation/config/{automation_id}"

            slog.debug(
                "ha_create_automation_request",
                automation_id=automation_id,
                config=config_with_id
            )

            response = await self.client.post(url, json=config_with_id)

            if response.status_code in (200, 201):
                # Reload automations to make the new one active
                await self.call_service("automation", "reload")
                return True
            else:
                slog.warning(
                    "ha_create_automation_rejected",
                    automation_id=automation_id,
                    status_code=response.status_code,
                    response_text=response.text[:500] if response.text else "empty"
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
            url = f"/api/config/automation/config/{automation_id}"
            response = await self.client.delete(url)

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
        """Close the HTTP client."""
        if self.client:
            await self.client.aclose()
