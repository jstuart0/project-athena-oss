"""ATHENA-69 D17 -- the gateway simple-command fast path is owner-only.

execute_simple_command() (simple_commands.py) bypasses the orchestrator --
and therefore every server-derived permission check in
src/orchestrator/mode_permission.py -- to call Home Assistant directly.
Before this fix that path ran unconditionally whenever the
ha_simple_command_fastpath feature flag was on: a Wyoming satellite, an
unauthenticated HA companion-app conversation, or a guest booking could all
flip a light through it with zero permission check.

fast_path_allowed() is the sole gate. It asks the mode service -- the same
server-derived source of truth the orchestrator itself uses -- whether the
house is currently in owner mode, and fails closed: any transport error,
timeout, non-200 response, non-owner mode, unparseable body, or an unset
MODE_SERVICE_URL all return False. There is no local caching of "owner"
across requests -- a stale cache could keep the fast path open after a
guest booking starts. On any False outcome the caller (execute_simple_command)
returns None and its call sites already fall through to the orchestrator,
which re-derives mode/permissions independently.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx
import structlog

from shared.config import get_config

logger = structlog.get_logger("gateway.mode_gate")

_FAST_PATH_TIMEOUT_SECONDS = 2.0

# Lazy module-level client: created on first real call (not at import time)
# so a test can set MODE_SERVICE_URL / monkeypatch this client before the
# first fast_path_allowed() call, and so an unset MODE_SERVICE_URL never
# even constructs an httpx.AsyncClient.
_client: Optional[httpx.AsyncClient] = None
_warned_mode_service_unset = False


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_FAST_PATH_TIMEOUT_SECONDS)
    return _client


async def fast_path_allowed() -> bool:
    """True only when the mode service confirms owner mode within the
    timeout. Fails closed -- skips the fast path -- on every other
    outcome, logging gateway_fast_path_skipped with the reason."""
    global _warned_mode_service_unset

    mode_service_url = os.getenv("MODE_SERVICE_URL", "")
    if not mode_service_url:
        if not _warned_mode_service_unset:
            logger.warning("gateway_fast_path_mode_service_unset")
            _warned_mode_service_unset = True
        return False

    try:
        response = await _get_client().get(
            f"{mode_service_url}/mode/permissions",
            headers={"X-Service-Key": get_config().service_api_key},
            timeout=_FAST_PATH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("gateway_fast_path_skipped", reason=type(exc).__name__)
        return False

    if response.status_code != 200:
        logger.warning(
            "gateway_fast_path_skipped",
            reason="non_200_status",
            status_code=response.status_code,
        )
        return False

    try:
        body = response.json()
    except ValueError:
        logger.warning("gateway_fast_path_skipped", reason="invalid_json_body")
        return False

    mode = body.get("mode") if isinstance(body, dict) else None
    if mode != "owner":
        logger.warning("gateway_fast_path_skipped", reason="not_owner_mode", mode=mode)
        return False

    return True
