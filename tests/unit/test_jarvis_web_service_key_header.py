"""jarvis-web never sends a service key that can't be a header value.

jarvis-web keeps its key in a module constant (its image has no
``shared.config``), so ``service_key_headers()`` and its unusable-key rule
don't cover it. The HTTP client refuses a value outside visible ASCII with
an exception whose text carries the whole value, and these callers log the
exception text; so the header is left off, the variable is named once, and
admin-backend refuses the call.

Real httpx request building, faked only at the socket. Runs in the
jarvis-web environment (the ``jarvis-web-behaviour`` CI job).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JARVIS_BACKEND = _REPO_ROOT / "apps" / "jarvis-web" / "backend"
sys.path.insert(0, str(_JARVIS_BACKEND))

os.environ.setdefault("SERVICE_API_KEY", "test-key-jarvis-service-key-header")

_spec = importlib.util.spec_from_file_location("_test_jarvis_web_service_key_header_main", _JARVIS_BACKEND / "main.py")
jarvis_main = importlib.util.module_from_spec(_spec)
sys.modules["_test_jarvis_web_service_key_header_main"] = jarvis_main
_spec.loader.exec_module(jarvis_main)

ADMIN_URL = "http://admin-backend:8080"
_REAL_ASYNC_CLIENT = httpx.AsyncClient

# The same eight as tests/unit/test_service_key_headers.py::UNUSABLE_KEYS.
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
VISIBLE_ASCII = "".join(chr(code) for code in range(0x21, 0x7F))


async def _persistent_sessions():
    jarvis_main._feature_cache, jarvis_main._feature_cache_time = {}, 0.0
    return await jarvis_main.get_persistent_sessions_config()


GUEST_PATH = "/api/guest-mode/internal/current-guest"

# (caller, the admin path it requests): every jarvis-web function that sends
# the key to admin-backend and was touched by the route review.
SENDERS = {
    "get_persistent_sessions_config": (_persistent_sessions, "/api/features/public"),
    "get_room_tv_configs": (lambda: jarvis_main.get_room_tv_configs(), "/api/room-tv/internal"),
    "get_current_guest": (lambda: jarvis_main.get_current_guest(), GUEST_PATH),
}


@pytest.fixture
def drive(monkeypatch):
    """(sender name, key, calls) -> the requests recorded at the socket."""
    monkeypatch.setenv("ADMIN_API_URL", ADMIN_URL)
    for constant in ("ADMIN_BACKEND_URL", "ADMIN_INTERNAL_URL"):
        monkeypatch.setattr(jarvis_main, constant, ADMIN_URL)
    monkeypatch.setattr(jarvis_main, "DATABASE_URL", "postgresql://zz")
    monkeypatch.setattr(jarvis_main, "_service_key_unusable_reported", False)
    monkeypatch.setattr(jarvis_main, "get_admin_url", lambda: ADMIN_URL)

    def run(name, key, calls=2, status=200, error=None):
        monkeypatch.setattr(jarvis_main, "SERVICE_API_KEY", key)
        call, path = SENDERS[name]
        recorded = []

        def handler(request):
            recorded.append(request)
            if error is not None:
                raise error(request)
            return httpx.Response(status, json=[] if path != GUEST_PATH else {})

        def client(*args, **kwargs):
            kwargs.pop("verify", None)
            return _REAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        loop = asyncio.new_event_loop()
        results = []
        try:
            for _ in range(calls):
                results.append(loop.run_until_complete(call()))
        finally:
            loop.close()
        assert [request.url.path for request in recorded] == [path] * calls, "every call still goes out"
        run.results = results
        return recorded

    return run


def test_population():
    assert len(SENDERS) == 3 and "get_current_guest" in SENDERS
    assert len(UNUSABLE_KEYS) == 8


@pytest.mark.parametrize("kind", sorted(UNUSABLE_KEYS))
@pytest.mark.parametrize("sender", sorted(SENDERS))
def test_a_key_outside_visible_ascii_is_never_sent(sender, kind, drive, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    recorded = drive(sender, UNUSABLE_KEYS[kind])
    assert all("X-Service-Key" not in request.headers for request in recorded)
    unusable = [r for r in captured_logs if r.get("event") == "service_api_key_unusable"]
    assert unusable == [
        {"event": "service_api_key_unusable", "log_level": "error", "variable": "SERVICE_API_KEY"},
    ], "one line across both calls, naming the variable"
    assert "zz-sentinel" not in repr(captured_logs) and "zz-sentinel" not in caplog.text


@pytest.mark.parametrize("sender", sorted(SENDERS))
def test_every_visible_ascii_character_is_sent(sender, drive, captured_logs):
    """Positive control: the same drive with a usable key sends it."""
    recorded = drive(sender, VISIBLE_ASCII)
    assert [request.headers.get("X-Service-Key") for request in recorded] == [VISIBLE_ASCII] * 2
    assert [r for r in captured_logs if r.get("event") == "service_api_key_unusable"] == []


@pytest.mark.parametrize("sender", sorted(SENDERS))
def test_an_unset_key_sends_no_header(sender, drive, captured_logs):
    recorded = drive(sender, "")
    assert all("X-Service-Key" not in request.headers for request in recorded)
    assert [r for r in captured_logs if r.get("event") == "service_api_key_unusable"] == []


# A refused guest lookup says so ---------------------------------------------
#
# get_current_guest answers None for "no guest staying". A refused key gave
# the same None with nothing logged, so guest recognition went quiet.

GUEST_ROUTE = "/api/guest-mode/internal/current-guest"


def _refusals(logs):
    return [r for r in logs if r.get("event") == "admin_backend_refused"]


@pytest.fixture
def refusal_state(monkeypatch):
    import service_key

    canonical = sys.modules[service_key.note_admin_refusal.__module__]
    canonical._reset_for_tests()
    monkeypatch.setattr(canonical, "_clock", lambda: 1000.0)
    yield
    canonical._reset_for_tests()


@pytest.mark.parametrize("status", [401, 403, 503])
def test_a_refused_guest_lookup_is_logged_once_as_an_error(status, drive, refusal_state, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    recorded = drive("get_current_guest", VISIBLE_ASCII, calls=2, status=status)
    assert drive.results == [None, None], "still reads as no guest"
    assert all(request.headers.get("X-Service-Key") == VISIBLE_ASCII for request in recorded)
    assert _refusals(captured_logs) == [
        {"event": "admin_backend_refused", "log_level": "error", "status": status, "route": GUEST_ROUTE},
    ], "one line for the two refusals in the window"
    assert VISIBLE_ASCII not in repr(captured_logs) and VISIBLE_ASCII not in caplog.text


@pytest.mark.parametrize("status", [404, 500])
def test_another_failed_guest_lookup_is_a_warning_not_a_refusal(status, drive, refusal_state, captured_logs):
    drive("get_current_guest", VISIBLE_ASCII, calls=1, status=status)
    assert drive.results == [None] and _refusals(captured_logs) == []
    assert [(r["event"], r["log_level"], r.get("status")) for r in captured_logs] == [
        ("failed_to_fetch_guest", "warning", status)]


def test_a_guest_lookup_transport_error_is_not_a_refusal(drive, refusal_state, captured_logs):
    drive("get_current_guest", VISIBLE_ASCII, calls=1,
          error=lambda request: httpx.ConnectError("connection refused", request=request))
    assert drive.results == [None] and _refusals(captured_logs) == []
    assert [r["event"] for r in captured_logs] == ["failed_to_fetch_guest"]

