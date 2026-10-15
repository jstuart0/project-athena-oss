"""Pickers for the Rooms settings cards: the lights and voice satellites Home Assistant reports.

``GET /api/settings/room-lights/candidates`` lists ``light.*`` entities and
``GET /api/settings/speaker-rooms/satellites`` lists ``assist_satellite.*``
entities, both read from Home Assistant's states through the shared channel.
A friendly name is Home Assistant's text and is shown, never logged; logs carry
counts. The saved values go through the generic household settings routes.
"""
from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, Depends, HTTPException

from app.services import ha_access
from app.utils.service_auth import require_user_permission

logger = structlog.get_logger()

router = APIRouter(prefix="/api/settings", tags=["room-settings"])

MAX_PICKER_ITEMS = 1000
MAX_NAME_LENGTH = 100


async def _states(domain: str) -> List[Dict[str, Any]]:
    """The states of one domain, or the closed-phrase HTTP error for a failure."""
    try:
        response = await ha_access.channel().request("GET", "/api/states", timeout=10.0)
        if response.status_code != 200:
            raise ha_access.http_error(response)
        states = response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise ha_access.http_error(exc)
    if not isinstance(states, list):
        raise ha_access.http_error(ValueError("unexpected states shape"))
    prefix = domain + "."
    return [s for s in states if isinstance(s, dict) and isinstance(s.get("entity_id"), str) and s["entity_id"].startswith(prefix)]


def _friendly_name(state: Dict[str, Any]) -> str:
    attributes = state.get("attributes")
    name = attributes.get("friendly_name") if isinstance(attributes, dict) else None
    return (name if isinstance(name, str) and name else state["entity_id"])[:MAX_NAME_LENGTH]


def _ordered(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=lambda i: (i["friendly_name"].casefold(), i["entity_id"]))[:MAX_PICKER_ITEMS]


@router.get("/room-lights/candidates", dependencies=[Depends(require_user_permission("read"))])
async def room_light_candidates() -> List[Dict[str, Any]]:
    """Every light, with ``group`` true for a light group (it answers as its bulbs)."""
    items = []
    for state in await _states("light"):
        attributes = state.get("attributes")
        members = attributes.get("entity_id") if isinstance(attributes, dict) else None
        items.append({
            "entity_id": state["entity_id"],
            "friendly_name": _friendly_name(state),
            "group": isinstance(members, list),
        })
    logger.info("room_light_candidates_listed", count=len(items))
    return _ordered(items)


@router.get("/speaker-rooms/satellites", dependencies=[Depends(require_user_permission("read"))])
async def speaker_room_satellites() -> List[Dict[str, Any]]:
    items = [
        {"entity_id": state["entity_id"], "friendly_name": _friendly_name(state)}
        for state in await _states("assist_satellite")
    ]
    logger.info("speaker_room_satellites_listed", count=len(items))
    return _ordered(items)
