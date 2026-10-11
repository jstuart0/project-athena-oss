"""The happy-path end to end for a spoken command: a Home Assistant request
with the device marker goes through the real gateway code (marker, trust,
speaker room, payload), the real orchestrator /v1 handler and the real
control node over a fictional house; only Home Assistant (states, the
service call) and the registry are fakes.

  "turn on the lights" -> the speaker's room, exact entity ids
  "turn on the bathroom lights" -> "Which bathroom: lower, north or south?"
  "north" -> the north bathroom's lamps, and only those

and the same flow from a caller the gateway does not trust."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest.mock as mock

import httpx
import pytest
from starlette.requests import Request

from . import _fast_path_harness as fp
from . import _public_audience_harness as h
from . import _room_chat_harness as rch
from ._room_chat_harness import Chat
from .test_room_light_area_tier import _isolated  # noqa: F401

from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.state import IntentCategory  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

sys.modules.setdefault("prometheus_client", mock.MagicMock())
import gateway.main as gw  # noqa: E402
from gateway import speaker_room  # noqa: E402
from shared.output_channel import OutputChannel  # noqa: E402

DEVICE = "ab" * 16
GATEWAY_KEY = "gateway-key-for-the-pipeline-test"
STUDY = {f"light.study_lamp_{i}" for i in range(3)}
NORTH = {"light.north_lamp_0", "light.north_lamp_1"}
SESSION = "explicit-pipeline-1"
QUESTION = "Which bathroom: lower, north or south?"


def _http_request(trusted: bool) -> Request:
    headers = [(b"authorization", f"Bearer {GATEWAY_KEY}".encode())] if trusted else []
    return Request({"type": "http", "headers": headers, "client": ("192.0.2.77", 40000)})


class Pipeline:
    def __init__(self, monkeypatch, *, trusted: bool):
        self.trusted = trusted
        self.rig = fp.install(monkeypatch, slow="record")
        cache = _runtime.get_cache_client()
        self.chat = Chat(monkeypatch)
        _runtime.set_cache_client(cache)
        monkeypatch.setattr(gw, "API_KEY", GATEWAY_KEY)
        monkeypatch.setattr(gw, "_DEVICE_CLIENT_NETWORKS", ())
        monkeypatch.setattr(gw, "ha_registry", None)
        monkeypatch.setattr(
            gw, "_get_satellite_room_map", lambda: hr.SatelliteRoomMap(device_rooms={DEVICE: "study"}))
        speaker_room.reset_for_test()
        self.states = []
        chat = self.chat

        async def run_control(state):
            self.states.append(state)
            state.prev_context = chat.store.get(state.session_id)
            state.context_ref_info = {}
            state.node_timings = state.node_timings or {}
            state.intent = IntentCategory.CONTROL
            return await rch.route_control_node(state)

        monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", run_control)

    def body(self, text: str) -> dict:
        async def build():
            messages = [
                gw.ChatMessage(role="system", content=f"You are a voice assistant.\nathena_device_id: {DEVICE}"),
                gw.ChatMessage(role="user", content=text),
            ]
            marker, cleaned, _, rejected = speaker_room.extract_device_marker(messages)
            request = gw.ChatCompletionRequest(model="m", stream=True, messages=cleaned, session_id=SESSION)
            raw = _http_request(self.trusted)
            room, voice_device_id = await gw._speaker_room_for_request(
                raw, device_id=marker, marker_rejected=rejected)
            return gw._orchestrator_openai_payload(
                request, room, stream=True, channel=OutputChannel.SPEECH, voice_device_id=voice_device_id,
                trusted_voice=gw._is_trusted_voice(raw))

        return asyncio.run(build())

    def say(self, text: str) -> str:
        body = self.body(text)
        assert DEVICE not in json.dumps(body["messages"]), "the marker never reaches the orchestrator's messages"

        async def post():
            transport = httpx.ASGITransport(app=h.main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post("/v1/chat/completions", json=body, headers=h.service_headers())

        with self.chat.patched():
            response = asyncio.run(post())
        return fp.answer_of("v1_stream", response)

    @property
    def written(self):
        return self.chat.written


def test_the_spoken_flow_lights_then_which_bathroom_then_north(monkeypatch):
    p = Pipeline(monkeypatch, trusted=True)

    first = p.say("turn on the lights")
    assert set(p.written) == STUDY, (p.written, first)
    assert p.states[-1].room == "study" and p.states[-1].supports_followup is True

    p.chat.reset_writes()
    asked = p.say("turn on the bathroom lights")
    assert asked == QUESTION and p.written == []
    assert "awaiting_room_choice" in p.chat.stored_parameters(SESSION)

    p.chat.reset_writes()
    done = p.say("north")
    assert set(p.written) == NORTH, (p.written, done)
    assert "north bathroom" in done.lower()
    assert "awaiting_room_choice" not in p.chat.stored_parameters(SESSION)


def test_the_same_flow_from_an_untrusted_caller_knows_no_room_and_carries_no_question(monkeypatch):
    p = Pipeline(monkeypatch, trusted=False)

    asked = p.say("turn on the lights")
    assert p.written == [] and asked.startswith("Which room"), asked
    state = p.states[-1]
    assert state.room == "unknown" and state.supports_followup is False and state.caller_fingerprint is None

    p.say("turn on the bathroom lights")
    assert p.written == []
    assert "awaiting_room_choice" not in p.chat.stored_parameters(SESSION), "no follow-up surface, so nothing is stored"
