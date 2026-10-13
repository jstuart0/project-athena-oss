"""The admin "Check sensors now" button's call to the orchestrator.

The orchestrator re-reads Home Assistant, decides which sensors are stuck and
asks ``stuck_sensor_alerts.reconcile`` (through admin-backend) to update the
alerts. This module only relays the request and turns every failure into a
closed phrase: nothing the orchestrator or the network said is passed on.
"""
from typing import Optional, Tuple

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from app.services import service_urls
from app.utils.rag_urls import check_ssrf_safe
from shared.config import get_config

logger = structlog.get_logger()

PATH = "/admin/sensors/stuck-check"
# The orchestrator waits on a fresh Home Assistant read, then on this
# service's own reconcile call.
TIMEOUT_SECONDS = 30.0
MAX_BODY_BYTES = 256 * 1024

# code -> (HTTP status this service answers with, phrase for the user)
FAILURES = {
    "orchestrator_blocked": (502, "The orchestrator address isn't allowed by the network safety rules."),
    "orchestrator_unreachable": (502, "Couldn't reach the orchestrator. Check that it's running."),
    "orchestrator_outdated": (502, "The orchestrator doesn't support this check yet. Update it and try again."),
    "orchestrator_refused": (502, "The orchestrator refused the request. Check the service key."),
    "unexpected_response": (502, "The orchestrator sent an answer this page can't read."),
    "home_assistant_not_configured": (503, "Home Assistant isn't set up, so there are no sensors to check."),
    "home_assistant_unavailable": (503, "Couldn't read Home Assistant. Check its connection and try again."),
    "no_entities_reported": (503, "Home Assistant reported no devices, so no alerts were changed."),
}
# The orchestrator's own 503 codes.
ORCHESTRATOR_CODES = ("home_assistant_not_configured", "home_assistant_unavailable", "no_entities_reported")


class _Sensor(BaseModel):
    entity_id: str = Field(max_length=255)
    room: str = Field(max_length=100)
    friendly_name: str = Field(max_length=255)
    sensor_type: str = Field(max_length=20)
    hours_on: float


class _Reconcile(BaseModel):
    created: int
    updated: int
    reopened: int
    resolved: int


class StuckCheckSummary(BaseModel):
    sensors_checked: int
    stuck_count: int
    stuck: list[_Sensor] = Field(max_length=500)
    cached: bool = False
    checked_at: Optional[str] = Field(default=None, max_length=64)
    reconciled: bool
    reconcile: Optional[_Reconcile] = None


async def _post(url: str, transport: Optional[httpx.AsyncBaseTransport]) -> Tuple[Optional[int], Optional[bytes]]:
    """``(status, body)``; status None when the request itself failed."""
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport,
            trust_env=False,  # a proxy variable must never carry the service key
        ) as client:
            async with client.stream(
                "POST", url, headers={"X-Service-Key": get_config().service_api_key}
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        return response.status_code, None
                return response.status_code, bytes(body)
    except Exception as exc:
        logger.warning("stuck_sensor_check_failed", error_class=type(exc).__name__)
        return None, None


def _failure_code(status: Optional[int], body: Optional[bytes]) -> str:
    import json

    if status is None:
        return "orchestrator_unreachable"
    if status == 404:
        return "orchestrator_outdated"
    if status in (401, 403) or 300 <= status < 400:
        return "orchestrator_refused"
    if status == 503 and body is not None:
        try:
            code = json.loads(body).get("error")
        except (ValueError, AttributeError):
            code = None
        if code in ORCHESTRATOR_CODES:
            return code
    return "orchestrator_unreachable"


async def run_live_check(
    *, transport: Optional[httpx.AsyncBaseTransport] = None
) -> Tuple[Optional[StuckCheckSummary], Optional[str]]:
    """``(summary, None)`` or ``(None, failure code)``. Never raises."""
    url = service_urls.orchestrator_url() + PATH
    allowed, _ = await check_ssrf_safe(url)
    if not allowed:
        logger.warning("stuck_sensor_check_blocked")
        return None, "orchestrator_blocked"
    status, body = await _post(url, transport)
    if status != 200 or body is None:
        code = _failure_code(status, body)
        logger.warning("stuck_sensor_check_refused", reason=code, status_code=status)
        return None, code
    try:
        summary = StuckCheckSummary.model_validate_json(body)
    except ValidationError:
        logger.warning("stuck_sensor_check_unreadable")
        return None, "unexpected_response"
    logger.info("stuck_sensor_check", checked=summary.sensors_checked, stuck=summary.stuck_count)
    return summary, None
