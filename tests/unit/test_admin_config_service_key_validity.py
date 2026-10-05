"""``AdminConfigClient`` and a service key that can't be an HTTP header value.

httpx refuses such a value with ``Illegal header value b'<the whole key>'``
and the client's callers log ``error=str(e)``, so the key would land in a
log. The client must send no header built from it, per call or as a default.

Requests go to a loopback listener rather than a mock transport, so the
header really is written by the HTTP library and the listener sees the bytes.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shared import service_key  # noqa: E402
from shared.admin_config import AdminConfigClient, voice_automation_headers  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402

SENTINEL = "zz-sentinel"
UNUSABLE_KEYS = {
    "trailing_newline": "zz-sentinel-key\n",
    "carriage_return": "zz-sentinel-key\r",
    "leading_space": " zz-sentinel-key",
    "trailing_space": "zz-sentinel-key ",
    "inner_space": "zz-sentinel key",
    "tab": "zz-sentinel\tkey",
    "delete": "zz-sentinel-key\x7f",
    "non_ascii": "zz-sentinel-keyÿ",
}
USABLE_KEY = "zz-usable-key"


class _Listener:
    """Answers every request with 401 and keeps each request's head."""

    def __init__(self):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
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
                        b"HTTP/1.1 401 Unauthorized\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
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


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    service_key._reset_for_tests()
    _clear_cache_for_tests()
    yield
    service_key._reset_for_tests()
    _clear_cache_for_tests()


def _call(listener, api_key, method="get_feature_flags", *args, **kwargs):
    async def run():
        client = AdminConfigClient(admin_url=listener.url, api_key=api_key)
        try:
            return client, await getattr(client, method)(*args, **kwargs)
        finally:
            await client.close()

    return asyncio.run(run())


# The check itself ------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(UNUSABLE_KEYS))
def test_the_validity_check_refuses_a_key_outside_visible_ascii(kind):
    assert service_key.is_header_safe(UNUSABLE_KEYS[kind]) is False


def test_the_validity_check_accepts_every_visible_ascii_character():
    assert service_key.is_header_safe("".join(chr(code) for code in range(0x21, 0x7F))) is True


# AdminConfigClient -----------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(UNUSABLE_KEYS))
def test_an_unusable_key_reaches_no_header_and_no_log(kind, listener, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    client, flags = _call(listener, UNUSABLE_KEYS[kind])

    assert client.api_key == UNUSABLE_KEYS[kind], "the client really holds the unusable value"
    assert flags == {}, "the documented default"
    assert len(listener.heads) == 1, "the request was really sent, without a credential"
    assert "x-service-key" not in listener.heads[0]
    assert "x-api-key" not in listener.heads[0]
    assert SENTINEL not in listener.heads[0]
    assert SENTINEL not in repr(captured_logs) and SENTINEL not in caplog.text
    unusable = [r for r in captured_logs if r.get("event") == "service_api_key_unusable"]
    assert unusable == [
        {"event": "service_api_key_unusable", "log_level": "error", "variable": "SERVICE_API_KEY"},
    ], "one line, naming the variable"


def test_a_usable_key_is_sent_per_call_and_is_not_a_client_default(listener, captured_logs):
    """Positive control for the test above: the same path does send a key."""
    client, flags = _call(listener, USABLE_KEY)

    assert flags == {}
    assert len(listener.heads) == 1
    assert f"x-service-key: {USABLE_KEY}" in listener.heads[0]
    assert f"x-api-key: {USABLE_KEY}" in listener.heads[0], "the client's default header is kept"
    assert "x-service-key" not in {name.lower() for name in client.client.headers}
    assert [r for r in captured_logs if r.get("event") == "service_api_key_unusable"] == []


def test_a_user_route_call_carries_no_service_key(listener):
    """A user-only route refuses any request that carries the service key, so
    the key is passed on each reviewed call and never on the others."""
    _client, patterns = _call(listener, USABLE_KEY, method="get_intent_patterns")

    assert patterns == {}
    assert len(listener.heads) == 1
    assert "/api/intent-routing/patterns" in listener.heads[0]
    assert "x-service-key" not in listener.heads[0]


def test_an_unset_key_sends_no_service_key_header(listener):
    client, flags = _call(listener, "")

    assert client.api_key == ""
    assert flags == {}
    assert len(listener.heads) == 1
    assert "x-service-key" not in listener.heads[0], "no header at all, not an empty one"
    assert "x-api-key" not in listener.heads[0], "nor an empty default"
    assert "x-api-key" not in {name.lower() for name in client.client.headers}


# The senders that were keyed before this rule existed ------------------------
# (method, positional arguments, keyword arguments); each sends one request.

OLDER_SENDERS = {
    "get_external_api_key": ("get_external_api_key", ("some-service",), {}),
    "get_enabled_tools": ("get_enabled_tools", (), {}),
    "get_base_knowledge": ("get_base_knowledge", (), {}),
    "record_tool_metric": ("record_tool_metric", ("some_tool", True, 12), {}),
    "resolve_room_group": ("resolve_room_group", ("downstairs",), {}),
    "get_room_groups": ("get_room_groups", (), {}),
    "get_user_session_by_device": ("get_user_session_by_device", ("device-1",), {}),
    "delete_voice_automation": ("delete_voice_automation", (7,), {}),
    "get_service_usage": ("get_service_usage", ("some-service",), {}),
    "record_service_usage": ("record_service_usage", ("some-service",), {}),
    "get_voice_automations": ("get_voice_automations", (), {"caller_mode": "owner", "caller_guest_name": None, "caller_guest_stay": None}),
    "archive_voice_automation": ("archive_voice_automation", (7,), {"caller_mode": "owner", "caller_guest_name": None, "caller_guest_stay": None}),
}


def _older(listener, api_key, sender):
    method, args, kwargs = OLDER_SENDERS[sender]
    return _call(listener, api_key, method, *args, **kwargs)


@pytest.mark.parametrize("sender", sorted(OLDER_SENDERS))
def test_an_older_sender_never_sends_or_logs_an_unusable_key(sender, listener, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    _older(listener, UNUSABLE_KEYS["trailing_newline"], sender)

    assert len(listener.heads) == 1, "the request was really sent, without a credential"
    assert "x-service-key" not in listener.heads[0] and SENTINEL not in listener.heads[0]
    assert SENTINEL not in repr(captured_logs) and SENTINEL not in caplog.text


@pytest.mark.parametrize("sender", sorted(OLDER_SENDERS))
def test_an_older_sender_still_sends_a_usable_key(sender, listener):
    """Positive control: the same calls carry a usable key as before."""
    _older(listener, USABLE_KEY, sender)

    assert len(listener.heads) == 1
    assert f"x-service-key: {USABLE_KEY}" in listener.heads[0]


def test_get_secret_never_sends_or_logs_an_unusable_key(listener, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(Exception) as raised:
        _call(listener, UNUSABLE_KEYS["trailing_newline"], "get_secret", "some-service")

    assert len(listener.heads) == 1 and "x-service-key" not in listener.heads[0]
    assert SENTINEL not in str(raised.value)
    assert SENTINEL not in repr(captured_logs) and SENTINEL not in caplog.text


class _RecordingLogger:
    """Stands in for the module's logger, whatever structlog configuration an
    earlier test left behind."""

    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def log(event, **fields):
            self.records.append({"event": event, "log_level": level, **fields})
        return log


def test_a_failed_external_key_fetch_logs_no_exception_text(listener, monkeypatch):
    """The answer to this call holds another service's API key; its failure
    is logged by type and status, never by the exception's text."""
    import shared.admin_config as admin_config_module

    recorder = _RecordingLogger()
    monkeypatch.setattr(admin_config_module, "logger", recorder)
    _client, result = _call(listener, USABLE_KEY, "get_external_api_key", "some-service")

    assert result is None
    assert [r for r in recorder.records if r["event"].startswith("external_api_key_")] == [{
        "event": "external_api_key_fetch_error",
        "log_level": "warning",
        "service_name": "some-service",
        "status_code": 401,
        "error_type": "HTTPStatusError",
    }]


@pytest.mark.parametrize("kind", sorted(UNUSABLE_KEYS))
def test_scoped_voice_headers_leave_out_an_unusable_key(kind):
    headers = voice_automation_headers(UNUSABLE_KEYS[kind], "guest", "Zed", 5)

    assert headers == {"X-Athena-Caller-Mode": "guest", "X-Athena-Guest-Name": "Zed", "X-Athena-Guest-Stay": "5"}


def test_scoped_voice_headers_carry_a_usable_key():
    assert voice_automation_headers(USABLE_KEY, "owner", None) == {
        "X-Service-Key": USABLE_KEY, "X-Athena-Caller-Mode": "owner",
    }
    assert "X-Service-Key" not in voice_automation_headers("", "owner", None)
