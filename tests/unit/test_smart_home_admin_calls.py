"""The smart-home controller's own calls to admin-backend: a service key that
can't be a header value is never sent or logged, and a call that can't
connect is visible in the log.

Requests go to a loopback listener, so the header is written by the HTTP
library itself.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestrator.nodes import _runtime  # noqa: E402,F401  (first orchestrator import)
from orchestrator.smart_home_controller import SmartHomeController  # noqa: E402
from shared import admin_url, service_key  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402

SENTINEL = "zz-sentinel"
UNUSABLE_KEY = "zz-sentinel-key\n"
USABLE_KEY = "zz-usable-key"
LAYOUT = b'{"has_layout": true, "layout_description": "two floors"}'


class _Listener:
    """Answers every request with the layout and keeps each request's head."""

    def __init__(self):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.heads: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                continue
            with connection:
                connection.settimeout(2.0)
                received = b""
                try:
                    while b"\r\n\r\n" not in received:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        received += chunk
                    self.heads.append(received.decode("latin-1").lower())
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: "
                        + str(len(LAYOUT)).encode() + b"\r\nconnection: close\r\n\r\n" + LAYOUT
                    )
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._socket.close()


@pytest.fixture
def listener():
    server = _Listener()
    yield server
    server.close()


def _fresh():
    _clear_cache_for_tests()
    admin_url._clear_cache_for_tests()
    service_key._reset_for_tests()


@pytest.fixture
def configure(monkeypatch):
    def apply(admin, key):
        monkeypatch.setenv("ADMIN_API_URL", admin)
        monkeypatch.setenv("SERVICE_API_KEY", key)
        _fresh()

    yield apply
    _fresh()


def _controller():
    # The methods under test read nothing from the instance.
    return SmartHomeController.__new__(SmartHomeController)


def test_house_layout_fetch_never_sends_or_logs_an_unusable_key(listener, configure, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    configure(listener.url, UNUSABLE_KEY)

    layout = asyncio.run(_controller()._get_house_layout())

    assert len(listener.heads) == 1, "the request was really sent, without a credential"
    assert "x-service-key" not in listener.heads[0] and SENTINEL not in listener.heads[0]
    assert layout == "two floors"
    assert SENTINEL not in caplog.text and SENTINEL not in repr(captured_logs)


def test_house_layout_fetch_sends_a_usable_key(listener, configure):
    """Positive control for the test above."""
    configure(listener.url, USABLE_KEY)

    assert asyncio.run(_controller()._get_house_layout()) == "two floors"
    assert f"x-service-key: {USABLE_KEY}" in listener.heads[0]


def test_a_failed_house_layout_fetch_logs_the_error_type_only(configure, caplog):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    caplog.set_level(logging.DEBUG)
    configure(f"http://127.0.0.1:{port}", USABLE_KEY)

    assert asyncio.run(_controller()._get_house_layout()) == ""

    lines = [r for r in caplog.records if "house layout" in r.getMessage()]
    assert [(r.levelname, r.getMessage()) for r in lines] == [
        ("WARNING", "Could not fetch house layout: ConnectError"),
    ]


def test_a_stuck_sensor_resolve_that_cannot_connect_is_a_warning(configure, caplog):
    """A transport failure on this keyed call used to be DEBUG only, so a TLS
    or connection problem after an upgrade left no trace at the default level."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    caplog.set_level(logging.DEBUG)
    configure(f"http://127.0.0.1:{port}", USABLE_KEY)

    asyncio.run(_controller()._resolve_stuck_sensor_alert("binary_sensor.hall_motion"))

    lines = [r for r in caplog.records if "stuck sensor alert" in r.getMessage()]
    assert [(r.levelname, r.getMessage()) for r in lines] == [
        ("WARNING", "Error resolving stuck sensor alert: ConnectError"),
    ]
    assert str(port) not in caplog.text.replace("httpcore", ""), "no URL in the line"
