"""admin-backend's own process logs no request URL: no access line, and no
query string on uvicorn's WebSocket handshake line (which carries the Admin
Jarvis ticket).

Boots the real ``python main.py`` (the image's entry point) and probes it.
A control process boots a toy ASGI app under uvicorn's default logging in
the same venv and gets the same probes: both nonces appear there, so this
uvicorn does log them when nothing filters, and the main case can only be
green because of admin-backend's log config.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
READY_TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"process exited early rc={proc.returncode}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise AssertionError("not ready within 60 s")


def _probe(port: int, nonce1: str, nonce2: str) -> None:
    assert httpx.get(f"http://127.0.0.1:{port}/health", params={"q": nonce1}, timeout=5.0).status_code == 200
    import websockets.sync.client as ws_client

    try:
        with ws_client.connect(f"ws://127.0.0.1:{port}/ws/admin-jarvis?token={nonce2}", open_timeout=5) as ws:
            try:
                ws.recv(timeout=2)
            except Exception:
                pass
    except Exception:
        pass  # a 403 at the handshake or a 4001 close; either way the handshake is logged


def _run(cmd, env, port):
    nonce1, nonce2 = f"n1{uuid.uuid4().hex}", f"n2{uuid.uuid4().hex}"
    proc = subprocess.Popen(cmd, cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_ready(port, proc)
        _probe(port, nonce1, nonce2)
        time.sleep(1.0)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    return out, nonce1, nonce2


def _env(port):
    env = dict(os.environ)
    env.update({
        "DEV_MODE": "true",
        "DATABASE_URL": "sqlite:///:memory:",
        "QDRANT_URL": "http://127.0.0.1:1",
        "PORT": str(port),
        "SERVICE_API_KEY": "test-service-key-for-access-log",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def test_admin_backend_logs_no_request_urls():
    port = _free_port()
    out, nonce1, nonce2 = _run([sys.executable, "main.py"], _env(port), port)
    assert "Uvicorn running on" in out, out[-3000:]
    assert out.count(nonce1) == 0, [l for l in out.splitlines() if nonce1 in l]
    assert out.count(nonce2) == 0, [l for l in out.splitlines() if nonce2 in l]
    assert not [l for l in out.splitlines() if '"GET /health' in l]
    handshake = [l for l in out.splitlines() if '"WebSocket /ws/admin-jarvis' in l]
    assert handshake, "the WebSocket handshake line should still log (without its query)"
    assert all("?" not in l.split('"WebSocket ', 1)[1] for l in handshake), handshake


_TOY_APP = r'''
import sys
assert "shared.logging_config" not in sys.modules and "main" not in sys.modules, "control must not load the app"
import uvicorn

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})
    elif scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.close", "code": 4001})

if "shared.logging_config" in sys.modules or "main" in sys.modules:
    sys.exit(3)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]))
'''


def test_control_default_uvicorn_logs_both_nonces():
    port = _free_port()
    out, nonce1, nonce2 = _run([sys.executable, "-c", _TOY_APP, str(port)], _env(port), port)
    assert "Uvicorn running on" in out, out[-3000:]
    assert out.count(nonce1) >= 1, out[-3000:]
    assert out.count(nonce2) >= 1, out[-3000:]
