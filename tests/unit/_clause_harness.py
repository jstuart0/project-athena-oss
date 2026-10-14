"""Shared rig for the clause-rule tests (T3): a real controller, guard and scope
behind each route, with a recording Home Assistant client at the channel seam
(``call_service``) and a model stub that reads the query it is shown.

Synthetic utterances and a fictional house only.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest.mock as mock
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from orchestrator import mode_permission as mp
from orchestrator.nodes import _runtime, route_music_node, route_tv_node
from orchestrator.state import IntentCategory, OrchestratorState

from .test_state_question_routing import _drive_route_control, _raw_ha_client_3_6

_QUERY_RE = re.compile(r'\nQuery: "(.*?)"\nNumber of lights', re.S)


def run(coro):
    return asyncio.run(coro)


class House:
    """Lights the stub model can name: a hall, a porch and a kitchen light, and
    twelve office lights (the fan-out threshold's size)."""

    def __init__(self):
        self.entities = {}
        for i in range(12):
            self.entities[f"light.office_{i}"] = {"state": "on", "attributes": {"friendly_name": f"Office Light {i}"}}
        for name in ("hall", "porch", "kitchen"):
            self.entities[f"light.{name}_lamp"] = {
                "state": "off", "attributes": {"friendly_name": f"{name.title()} Lamp"},
            }
        self.entities["media_player.den"] = {"state": "off", "attributes": {"friendly_name": "Den TV"}}

    async def get_entities(self):
        return dict(self.entities)

    async def find_lights_by_room(self, room):
        key = (room or "").lower()
        if "office" in key:
            return [{"entity_id": f"light.office_{i}", "friendly_name": f"Office Light {i}",
                     "members": [], "state": "on", "type": "individual"} for i in range(12)]
        for name in ("hall", "porch", "kitchen"):
            if name in key:
                return [{"entity_id": f"light.{name}_lamp", "friendly_name": f"{name.title()} Lamp",
                         "members": [], "state": "off", "type": "individual"}]
        return []

    async def get_all_light_groups(self):
        return []


class QueryReadingModel:
    """Answers the way a model would for the query in the extraction prompt:
    blue means a colour, "off" means off, else on; the room is the one named.
    Records the query it was shown so a test can see what reached it."""

    def __init__(self, scene_for: Optional[dict] = None):
        self.queries: List[str] = []
        self.prompts: List[str] = []
        self.scene_for = scene_for or {}
        self.generate = AsyncMock(side_effect=self._generate)

    async def _generate(self, **kwargs):
        prompt = kwargs.get("prompt", "")
        self.prompts.append(prompt)
        match = _QUERY_RE.search(prompt)
        query = match.group(1) if match else ""
        self.queries.append(query)
        low = query.lower()
        room = next((r for r in ("hall", "porch", "kitchen", "office") if r in low), None)
        for phrase, entity_id in self.scene_for.items():
            if phrase in low:
                return {"response": json.dumps({
                    "device_type": "scene", "room": None, "action": "activate", "target_scope": "group",
                    "parameters": {"entity_id": entity_id}, "color_description": None,
                })}
        if "blue" in low:
            body = {"device_type": "light", "room": room, "action": "set_color", "target_scope": "all_individual",
                    "parameters": {"hs_colors": [[240, 100]]}, "color_description": "blue"}
        else:
            body = {"device_type": "light", "room": room or "office",
                    "action": "turn_off" if "off" in low else "turn_on", "target_scope": "group", "parameters": {}}
        return {"response": json.dumps(body)}


def drive_control(query: str, *, names=(), model: Optional[QueryReadingModel] = None, scenes=None, room="office"):
    """route_control_node for ``query`` over the house, with the model stub.
    Returns (state, model, client); every write the turn made is in
    ``client.call_service.await_args_list``."""
    model = model or QueryReadingModel(scene_for=scenes)
    with mock.patch("orchestrator.nodes.route_control._configured_assistant_names",
                    new_callable=AsyncMock, return_value=tuple(names)):
        state, _controller, llm, client = _drive_route_control(
            query, llm_router=model, entity_manager=House(), room=room,
        )
    return state, llm, client


def writes(client) -> list:
    """(domain, service, entity ids) of every call_service the channel saw."""
    out = []
    for call in client.call_service.await_args_list:
        data = call.args[2] if len(call.args) > 2 else {}
        eid = (data or {}).get("entity_id")
        out.append((call.args[0], call.args[1], tuple(eid) if isinstance(eid, (list, tuple)) else (eid,)))
    return out


# --- music and TV: handlers that write through the guarded channel ----------------


class ChannelMusic:
    """A music handler whose every write goes through the guarded HA channel."""

    def __init__(self, raw):
        self.channel = mp.ensure_permission_enforcing(raw)
        self.parsed: List[str] = []
        self.control_action = "pause"

    async def parse_music_play_intent(self, query, room=None, interface_type=None):
        self.parsed.append(query)
        return {"media_type": "artist", "media_id": "Someone", "room": "den", "radio_mode": True,
                "play_in_browser": False, "is_room_group": False}

    async def parse_music_control_intent(self, query, room=None):
        self.parsed.append(query)
        return {"action": self.control_action, "room": "den", "volume_level": None}

    async def handle_play(self, **kwargs):
        await self.channel.call_service("media_player", "play_media", {"entity_id": "media_player.den"})
        return "Playing in the den."

    async def handle_room_group_play(self, **kwargs):
        await self.channel.call_service("media_player", "play_media", {"entity_id": "media_player.den"})
        return "Playing."

    async def handle_control(self, action=None, room=None, volume_level=None):
        if action == "now_playing":
            return "Nothing is playing."
        await self.channel.call_service("media_player", "media_pause", {"entity_id": "media_player.den"})
        return "Done."


class ChannelTV:
    def __init__(self, raw, *, action="power", power_action="on"):
        self.channel = mp.ensure_permission_enforcing(raw)
        self.action = action
        self.power_action = power_action
        self.parsed: List[str] = []

    async def parse_tv_intent(self, query, room=None, mode=None):
        self.parsed.append(query)
        return SimpleNamespace(
            action=self.action, app_name="Video", room="den", all_tvs=False, power_action=self.power_action,
            command="play", youtube_video_id=None,
        )

    async def _write(self, service):
        await self.channel.call_service("media_player", service, {"entity_id": "media_player.den"})
        return {"success": True, "message": "Done."}

    async def handle_power(self, **kwargs):
        return await self._write("turn_on")

    async def handle_launch(self, **kwargs):
        return await self._write("select_source")

    async def handle_navigate(self, **kwargs):
        return await self._write("navigate")

    async def handle_playback(self, **kwargs):
        return await self._write("media_play_pause")

    async def handle_launch_everywhere(self, **kwargs):
        return await self._write("select_source")

    async def handle_youtube_video(self, **kwargs):
        return await self._write("play_media")

    async def handle_status(self, **kwargs):
        return {"success": True, "message": "The TV is off."}


def _media_state(query, intent, room="den"):
    state = OrchestratorState(query=query)
    state.session_id = "sess-media"
    state.room = room
    state.intent = intent
    state.mode = "owner"
    state.permissions = {"mode": "owner"}
    state.node_timings = {}
    state.retrieved_data = {}
    return state


def drive_music(query: str, *, names=(), intent=IntentCategory.MUSIC_PLAY, control_action="pause"):
    """route_music_node over a channel-recording music handler."""
    _runtime.reset_for_test()
    raw = _raw_ha_client_3_6()
    handler = ChannelMusic(raw)
    handler.control_action = control_action
    _runtime.set_music_handler(handler)
    state = _media_state(query, intent)
    with mock.patch("orchestrator.nodes.route_music.get_feature_config", new_callable=AsyncMock,
                    return_value={"enabled": False, "config": {}}), \
            mock.patch("orchestrator.nodes.route_music.configured_assistant_names",
                       new_callable=AsyncMock, return_value=tuple(names)), \
            mock.patch("orchestrator.nodes.route_music.store_conversation_context", new_callable=AsyncMock):
        run(route_music_node(state))
    return state, raw, handler


def drive_tv(query: str, *, names=(), action="power", power_action="on"):
    """route_tv_node over a channel-recording TV handler."""
    _runtime.reset_for_test()
    raw = _raw_ha_client_3_6()
    handler = ChannelTV(raw, action=action, power_action=power_action)
    _runtime.set_tv_handler(handler)
    state = _media_state(query, IntentCategory.TV_CONTROL)
    with mock.patch("orchestrator.nodes.route_tv.get_feature_config", new_callable=AsyncMock,
                    return_value={"enabled": False, "config": {}}), \
            mock.patch("orchestrator.nodes.route_tv.configured_assistant_names",
                       new_callable=AsyncMock, return_value=tuple(names)), \
            mock.patch("orchestrator.nodes.route_tv.store_conversation_context", new_callable=AsyncMock):
        run(route_tv_node(state))
    return state, raw, handler


__all__ = [
    "House", "QueryReadingModel", "drive_control", "drive_music", "drive_tv", "writes", "run", "MagicMock",
]
