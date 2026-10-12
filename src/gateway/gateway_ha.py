"""The gateway's only way to reach Home Assistant over REST.

The gateway reads Home Assistant's states (to find assist satellites) and sends
one kind of write: an announcement on an assist satellite. It does not control
devices; the orchestrator authorizes every device write. So the shared
``HAChannel`` (which can call any API path) is held privately here, and the
gateway gets exactly two operations, with the announcement payload validated
before anything is sent.

``GatewayHA`` is the one module-level holder of Home Assistant access in the
gateway; the resolver, the channel and the latch are lifespan locals and live
nowhere else.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from shared.ha_http import HAChannel
from shared.output_channel import SPEECH_SINK_MAX_CHARS

_SATELLITE = re.compile(r"assist_satellite\.[a-z0-9_]+")
_ANNOUNCE_KEYS = {"entity_id", "message", "preannounce"}
_READ_TIMEOUT = 3.0
_ANNOUNCE_TIMEOUT = 5.0


def _check_announcement(payload: Any) -> None:
    """``ValueError`` unless ``payload`` is an announcement to one or more
    assist satellites: exactly ``entity_id`` and ``message`` (and an optional
    boolean ``preannounce``), no list-wide or wildcard targets."""
    if not isinstance(payload, dict) or not {"entity_id", "message"} <= set(payload) <= _ANNOUNCE_KEYS:
        raise ValueError("an announcement carries entity_id and message, and optionally preannounce")
    target = payload["entity_id"]
    targets = [target] if isinstance(target, str) else target
    if not isinstance(targets, list) or not targets or not all(
        isinstance(item, str) and _SATELLITE.fullmatch(item) for item in targets
    ):
        raise ValueError("an announcement targets assist satellites only")
    message = payload["message"]
    if not isinstance(message, str) or not 1 <= len(message) <= SPEECH_SINK_MAX_CHARS:
        raise ValueError("an announcement message is 1 to %d characters" % SPEECH_SINK_MAX_CHARS)
    if "preannounce" in payload and not isinstance(payload["preannounce"], bool):
        raise ValueError("preannounce is a boolean")


class GatewayHA:
    """``read_states`` and ``announce``, nothing else (``configured`` and
    ``aclose`` are lifecycle)."""

    __slots__ = ("__channel",)

    def __init__(self, channel: HAChannel):
        self.__channel = channel

    @property
    def configured(self) -> bool:
        return self.__channel.configured

    async def read_states(self) -> List[Dict[str, Any]]:
        """Every entity's state, or ``[]`` when Home Assistant isn't configured
        or didn't answer with a list (including a withheld request)."""
        if not self.__channel.configured:
            return []
        response = await self.__channel.request("GET", "/api/states", timeout=_READ_TIMEOUT)
        if response.status_code != 200:
            return []
        states = response.json()
        return states if isinstance(states, list) else []

    async def announce(self, payload: Dict[str, Any]) -> bool:
        """Announce on assist satellites. ``ValueError`` (nothing sent) for a
        payload that isn't an announcement; ``False`` when Home Assistant isn't
        configured or refused."""
        _check_announcement(payload)
        if not self.__channel.configured:
            return False
        response = await self.__channel.request(
            "POST", "/api/services/assist_satellite/announce", json=payload, timeout=_ANNOUNCE_TIMEOUT,
        )
        return response.status_code == 200

    async def aclose(self) -> None:
        await self.__channel.aclose()

    def __getattr__(self, name: str) -> Any:
        raise AttributeError("GatewayHA exposes only read_states and announce")
