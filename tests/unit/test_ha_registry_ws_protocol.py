"""The real WebSocket adapter of shared.ha_registry against a loopback server
that speaks Home Assistant's handshake: auth_required -> auth -> auth_ok /
auth_invalid, then result frames for command ids 1-3.

The server runs in-process on an ephemeral port. Every test has a hard
timeout; nothing sleeps to wait for an outcome.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shared import ha_registry as hr  # noqa: E402

RAW = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json").read_text(encoding="utf-8")
)
RESULT_BY_TYPE = {
    "config/area_registry/list": RAW["areas"],
    "config/device_registry/list": RAW["devices"],
    "config/entity_registry/list": RAW["entities"],
}
TOKEN = "loopback-token-" + "z" * 24
HARD_TIMEOUT = 10


@pytest.fixture(autouse=True)
def _restore_websockets_log_levels():
    names = ("websockets", "websockets.client")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def _silent():
    quiet = logging.getLogger("athena.tests.ws_server")
    quiet.disabled = True
    return quiet


class HAServer:
    """A loopback Home Assistant. `mode` picks the failure to play."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.received_types = []
        self.url = ""

    async def handler(self, ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.9.0"}))
        auth = json.loads(await ws.recv())
        self.received_types.append(auth["type"])
        if self.mode == "invalid":
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.9.0"}))
        commands = []
        for _ in range(3):
            command = json.loads(await ws.recv())
            self.received_types.append(command["type"])
            commands.append(command)
        for position, command in enumerate(commands):
            if self.mode == "slow":
                await ws.wait_closed()
                return
            if self.mode == "fail" and position == 1:
                await ws.send(json.dumps({
                    "id": command["id"], "type": "result", "success": False,
                    "error": {"code": "unknown_command", "message": "no"},
                }))
                continue
            if self.mode == "close_mid" and position == 1:
                await ws.close()
                return
            result = RESULT_BY_TYPE[command["type"]]
            if self.mode == "oversize" and position == 2:
                result = [{"entity_id": f"light.pad_{i}", "pad": "x" * 600} for i in range(400)]
            if self.mode == "noise" and position == 0:
                await ws.send(json.dumps({"type": "event", "id": 99, "event": {}}))
            await ws.send(json.dumps({"id": command["id"], "type": "result", "success": True, "result": result}))
        if self.mode == "unknown_shape":
            return
        await ws.wait_closed()


def serve(mode="ok"):
    """Run `body(server)` against a running server; returns its value."""
    from websockets.asyncio.server import serve as ws_serve

    def decorate(body):
        async def go():
            server = HAServer(mode)
            async with ws_serve(server.handler, "127.0.0.1", 0, logger=_silent()) as running:
                port = running.sockets[0].getsockname()[1]
                server.url = f"http://127.0.0.1:{port}"
                return await asyncio.wait_for(body(server), HARD_TIMEOUT)

        return asyncio.run(go())

    return decorate


def new_registry(server, token=TOKEN, **kwargs):
    return hr.HARegistry(hr.websocket_fetcher(server.url, token), **kwargs)


def test_the_happy_path_reads_the_three_registries_into_a_snapshot():
    @serve("ok")
    async def outcome(server):
        reg = new_registry(server)
        snap = await reg.ensure(5)
        return reg.state, snap, server.received_types

    state, snap, received = outcome
    assert state == hr.STATE_OK
    assert snap.area_for("light.chain_lamp").area_id == "area_north_bath"
    assert len(snap.areas) == len(RAW["areas"])
    assert snap.satellite_entities(RAW["devices_by_name"]["study_sat"])


def test_unrelated_frames_between_results_are_ignored():
    @serve("noise")
    async def state(server):
        reg = new_registry(server)
        await reg.ensure(5)
        return reg.state

    assert state == hr.STATE_OK


def test_auth_invalid_latches_refused_and_keeps_no_snapshot():
    @serve("invalid")
    async def outcome(server):
        reg = new_registry(server)
        snap = await reg.ensure(5)
        return reg.state, snap

    state, snap = outcome
    assert state == hr.STATE_REFUSED and snap is None


def test_success_false_reads_unavailable():
    @serve("fail")
    async def outcome(server):
        reg = new_registry(server)
        await reg.ensure(5)
        return reg.state, reg.current()

    assert outcome == (hr.STATE_UNAVAILABLE, None)


def test_a_close_in_mid_stream_reads_unavailable():
    @serve("close_mid")
    async def outcome(server):
        reg = new_registry(server)
        await reg.ensure(5)
        return reg.state, reg.current()

    assert outcome == (hr.STATE_UNAVAILABLE, None)


def test_a_responder_slower_than_the_fetch_timeout_reads_unavailable_within_the_deadline():
    @serve("slow")
    async def outcome(server):
        reg = new_registry(server, fetch_timeout=0.3)
        await reg.ensure(5)
        return reg.state, reg.current()

    assert outcome == (hr.STATE_UNAVAILABLE, None)


def test_an_oversize_frame_reads_unavailable(monkeypatch):
    monkeypatch.setattr(hr, "WS_MAX_SIZE", 64 * 1024)

    @serve("oversize")
    async def outcome(server):
        reg = new_registry(server)
        await reg.ensure(5)
        return reg.state, reg.current()

    assert outcome == (hr.STATE_UNAVAILABLE, None)


def test_a_refused_connection_reads_unavailable():
    async def go():
        reg = hr.HARegistry(hr.websocket_fetcher("http://127.0.0.1:1", TOKEN))
        await reg.ensure(5)
        return reg.state

    assert asyncio.run(asyncio.wait_for(go(), HARD_TIMEOUT)) == hr.STATE_UNAVAILABLE


def test_the_server_receives_only_allowlisted_frame_types():
    @serve("ok")
    async def received(server):
        await new_registry(server).ensure(5)
        return server.received_types

    assert received[0] == "auth"
    assert sorted(received[1:]) == sorted(RESULT_BY_TYPE)
    assert set(received) <= hr._ALLOWED_TYPES


def test_neither_the_token_nor_the_url_reaches_a_log_line_at_debug(caplog):
    marker = "ctl-token-1"

    @serve("ok")
    async def outcome(server):
        with caplog.at_level(logging.DEBUG):
            await new_registry(server, token=marker).ensure(5)
            await new_registry(server, token=marker).ensure(5)
        return server.url

    assert marker not in caplog.text and outcome not in caplog.text
    assert "127.0.0.1" not in caplog.text


def test_positive_control_a_plain_client_does_log_the_token_at_debug(caplog):
    """The negative assertion above can only pass for the right reason if a
    default websockets client does show a token at DEBUG. The library cuts a
    frame's text short, so the control token is a short one."""
    short_token = "ctl-token-1"
    from websockets.asyncio.client import connect

    @serve("ok")
    async def seen(server):
        logging.getLogger("websockets").setLevel(logging.NOTSET)
        logging.getLogger("websockets.client").setLevel(logging.NOTSET)
        with caplog.at_level(logging.DEBUG):
            async with connect("ws://" + server.url.split("//")[1] + "/api/websocket") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": short_token}))
                await ws.recv()
        return caplog.text

    assert short_token in seen
