"""The Home Assistant card's feed: the orchestrator's passive view of its own
Home Assistant connection, classified for the admin panel.

Nothing here talks to Home Assistant. Two legs run side by side:

- the orchestrator leg asks ``GET /admin/ha-status`` (service key, per request)
  for what the orchestrator replicas last observed, merged through Redis;
- the gateway leg samples one gateway replica's ``/health`` ``ha_registry``
  word. It is best-effort and never changes the card's state or colour; it only
  adds a muted note (``gateway``).

What comes back is untrusted: every field is validated against a closed set,
integers must be non-negative, timestamps must parse, and nothing the
orchestrator sends is passed through as text. A failure on the admin to
orchestrator leg is reported as ``unknown`` with an ``orchestrator_*`` reason
and never reads as a Home Assistant failure.

The state and reason come from ``shared.ha_link.link_state``, the one truth
table (``tests/fixtures/ha_link/truth_table.json``).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Tuple

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from shared import ha_link
from shared.config import get_config

from app.utils.rag_urls import check_ssrf_safe

logger = structlog.get_logger()

LEG_TIMEOUT_SECONDS = 2.0
MAX_BODY_BYTES = 64 * 1024
ORCHESTRATOR_PATH = "/admin/ha-status"
GATEWAY_PATH = "/health"

# Every reason the card can show. The frontend's reason map must have exactly
# these keys (tests/unit/test_admin_frontend_escaping.py reads this literal).
REASONS = frozenset({
    "ok",
    "waiting",
    "token_rejected",
    "forbidden",
    "unreachable",
    "tls_error",
    "timeout",
    "ha_error",
    "registry_refused",
    "registry_unavailable",
    "orchestrator_unreachable",
    "orchestrator_refused",
    "orchestrator_outdated",
    "orchestrator_blocked",
    "not_configured",
    "ip_banned",
})

GATEWAY_VALUES = ("ok", "refused", "unavailable", "disabled", "unknown", "not_checked")
_GATEWAY_REPORTED = frozenset({"ok", "refused", "unavailable", "disabled"})

Outcome = Literal["ok", "auth_rejected", "forbidden", "ha_error", "timeout", "unreachable", "tls_error", "error"]
RegistryState = Literal["refused", "unavailable", "ok", "disabled"]
FailureClass = Literal["auth", "timeout", "unreachable", "other"]
Coverage = Literal["cluster", "replica"]


def _nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _iso_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, value: Any, info) -> Any:
        name = info.field_name
        if name.endswith("_at"):
            return _iso_or_none(value)
        if name in ("consecutive_failures", "areas"):
            return _nonneg_int(value)
        return value


def _real_bool(value: Any) -> bool:
    return value is True


class _Rest(_Section):
    last_outcome: Optional[Outcome]
    last_outcome_at: Optional[str]
    last_success_at: Optional[str]
    consecutive_failures: Optional[int]
    ban_suspected: bool = False

    @field_validator("ban_suspected", mode="before")
    @classmethod
    def _flag(cls, value: Any) -> bool:
        return _real_bool(value)


class _Registry(_Section):
    state: RegistryState
    areas: Optional[int]
    last_success_at: Optional[str]
    last_failure_at: Optional[str]
    last_failure_class: Optional[FailureClass]
    ban_suspected: bool = False

    @field_validator("ban_suspected", mode="before")
    @classmethod
    def _flag(cls, value: Any) -> bool:
        return _real_bool(value)


class _Auth(BaseModel):
    """The shared sign-in state an orchestrator reports. Every field is
    optional and a malformed one reads as absent: an older or newer orchestrator
    must never make the card say ``orchestrator_outdated`` over it."""

    model_config = ConfigDict(extra="ignore")

    latched: bool = False
    latched_at: Optional[str] = None
    banned_at: Optional[str] = None
    budget_spent: bool = False
    budget_spent_at: Optional[str] = None

    @field_validator("latched", "budget_spent", mode="before")
    @classmethod
    def _flag(cls, value: Any) -> bool:
        return _real_bool(value)

    @field_validator("latched_at", "banned_at", "budget_spent_at", mode="before")
    @classmethod
    def _time(cls, value: Any) -> Optional[str]:
        return _iso_or_none(value)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=False)

    configured: bool
    rest: _Rest
    registry: _Registry
    auth: Optional[_Auth] = None
    coverage: Optional[str] = None
    replicas_reporting: Optional[int] = None

    @field_validator("configured", mode="before")
    @classmethod
    def _configured_is_a_real_bool(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("configured must be a boolean")
        return value

    @field_validator("auth", mode="before")
    @classmethod
    def _auth_or_none(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return None
        try:
            return _Auth.model_validate(value)
        except ValidationError:
            return None

    @field_validator("coverage", mode="before")
    @classmethod
    def _coverage(cls, value: Any) -> Optional[str]:
        return value if value in ("cluster", "replica") else None

    @field_validator("replicas_reporting", mode="before")
    @classmethod
    def _replicas(cls, value: Any) -> Optional[int]:
        return _nonneg_int(value)


class HAStatus(BaseModel):
    """What the admin card renders. Enums, a count and timestamps only."""

    state: str
    reason: str
    areas: Optional[int] = None
    last_outcome_at: Optional[str] = None
    last_success_at: Optional[str] = None
    coverage: Optional[str] = None
    replicas_reporting: Optional[int] = None
    checked_at: str
    gateway: str = "not_checked"
    gateway_sampled: bool = True


def _gateway(value: Any) -> str:
    if value is None:
        return "not_checked"
    return value if value in _GATEWAY_REPORTED else "unknown"


def _unavailable(reason: str, checked_at: str, gateway: str) -> HAStatus:
    return HAStatus(state="unknown", reason=reason, checked_at=checked_at, gateway=gateway)


def classify(
    orch: Any,
    orch_error: Optional[str],
    gateway_state: Any,
    now: datetime,
) -> HAStatus:
    """The card's state from the orchestrator leg (``orch`` is the decoded JSON
    body, ``orch_error`` an ``orchestrator_*`` reason when the leg failed) and
    the sampled gateway word. The gateway never changes ``state`` or ``reason``."""
    checked_at = now.astimezone(timezone.utc).isoformat() if now.tzinfo else now.replace(tzinfo=timezone.utc).isoformat()
    gateway = _gateway(gateway_state)
    if orch_error is not None:
        reason = orch_error if orch_error in REASONS and orch_error.startswith("orchestrator_") else "orchestrator_unreachable"
        return _unavailable(reason, checked_at, gateway)
    try:
        payload = _Payload.model_validate(orch)
    except ValidationError:
        return _unavailable("orchestrator_outdated", checked_at, gateway)

    view: Dict[str, Any] = {
        "configured": payload.configured,
        "rest": {**payload.rest.model_dump(), "consecutive_failures": payload.rest.consecutive_failures or 0},
        "registry": payload.registry.model_dump(),
    }
    if payload.auth is not None:
        view["auth"] = payload.auth.model_dump()
    state, reason = ha_link.link_state(view)
    success_at, failure_at, _ = ha_link.link_times(view)
    failure_is_newest = failure_at is not None and (success_at is None or failure_at > success_at)
    newest = failure_at if failure_is_newest else success_at
    return HAStatus(
        state=state,
        reason=reason,
        areas=payload.registry.areas,
        last_outcome_at=newest.isoformat() if newest else None,
        last_success_at=success_at.isoformat() if success_at else None,
        coverage=payload.coverage,
        replicas_reporting=payload.replicas_reporting,
        checked_at=checked_at,
        gateway=gateway,
    )


# ---------------------------------------------------------------------------
# The fetch: two legs, each its own client
# ---------------------------------------------------------------------------

def _base(env_name: str, default: str) -> str:
    return os.getenv(env_name, default).rstrip("/")


async def _read_capped(response: httpx.Response) -> Optional[bytes]:
    """The body, or None when it is longer than ``MAX_BODY_BYTES``."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def _decode_object(body: Optional[bytes]) -> Any:
    import json

    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


async def _get(
    url: str, headers: Dict[str, str], transport: Optional[httpx.AsyncBaseTransport]
) -> Tuple[Optional[int], Optional[bytes]]:
    """``(status, body)``; status None when the request itself failed."""
    try:
        async with httpx.AsyncClient(
            timeout=LEG_TIMEOUT_SECONDS, follow_redirects=False, transport=transport,
            trust_env=False,  # a proxy variable must never carry the service key
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    return response.status_code, None
                return 200, await _read_capped(response)
    except Exception as exc:
        logger.warning("ha_status_leg_failed", error_class=type(exc).__name__)
        return None, None


async def _orchestrator_leg(transport: Optional[httpx.AsyncBaseTransport]) -> Tuple[Any, Optional[str]]:
    url = _base("ORCHESTRATOR_URL", "http://localhost:8001") + ORCHESTRATOR_PATH
    allowed, _ = await check_ssrf_safe(url)
    if not allowed:
        logger.warning("ha_status_orchestrator_leg", reason="orchestrator_blocked")
        return None, "orchestrator_blocked"
    status, body = await _get(url, {"X-Service-Key": get_config().service_api_key}, transport)
    if status is None:
        return None, "orchestrator_unreachable"
    if status != 200:
        reason = (
            "orchestrator_outdated" if status == 404
            else "orchestrator_refused" if status in (401, 403) or 300 <= status < 400
            else "orchestrator_unreachable"
        )
        logger.warning("ha_status_orchestrator_leg", reason=reason, status_code=status)
        return None, reason
    decoded = _decode_object(body)
    if not isinstance(decoded, dict):
        logger.warning("ha_status_orchestrator_leg", reason="orchestrator_unreachable", status_code=status)
        return None, "orchestrator_unreachable"
    return decoded, None


async def _gateway_leg(transport: Optional[httpx.AsyncBaseTransport]) -> Optional[str]:
    """The gateway's ``ha_registry`` word, or None when it couldn't be read. No
    credentials are sent."""
    url = _base("GATEWAY_URL", "http://localhost:8000") + GATEWAY_PATH
    allowed, _ = await check_ssrf_safe(url)
    if not allowed:
        return None
    status, body = await _get(url, {}, transport)
    decoded = _decode_object(body) if status == 200 else None
    word = decoded.get("ha_registry") if isinstance(decoded, dict) else None
    return word if isinstance(word, str) else None


async def fetch_ha_link(
    *, transport: Optional[httpx.AsyncBaseTransport] = None, now: Optional[datetime] = None
) -> HAStatus:
    """Run both legs concurrently and classify the result. Never raises."""
    (orch, orch_error), gateway_word = await asyncio.gather(_orchestrator_leg(transport), _gateway_leg(transport))
    return classify(orch, orch_error, gateway_word, now or datetime.now(timezone.utc))
