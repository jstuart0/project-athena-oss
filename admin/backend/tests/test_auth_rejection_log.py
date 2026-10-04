"""Every request admin-backend refuses for its credential is logged once, as
``admin_auth_rejected``, with the route's template and nothing that
identifies the caller or the request.

Real app, DEV_MODE's user bypass off, real tokens. Every refusal here is on a
route that was already gated before the route-auth review, so this file
doesn't depend on the review's own guards.

What these tests need from the product (``app/utils/auth_rejections.py``):

- ``AuthRejectionMiddleware(app)``: a pure ASGI middleware, registered on
  ``main.app`` innermost;
- module attributes ``logger`` (structlog), ``_clock`` (a callable returning
  seconds, looked up at call time), ``_reset_for_tests()``,
  ``flush_suppressed()`` (the shutdown hook) and ``tracked_key_count()``
  (how many rate-limit keys are held).

A record is ``event="admin_auth_rejected"`` with ``route`` (the template, or
``"<unmatched>"``), ``method`` (one of a closed set, else ``"OTHER"``),
``status``, ``reason``, ``credential_presented`` (``service_key`` / ``user``
/ ``none``: what the request carried, not who sent it) and ``suppressed``
(refusals with the same route, reason and credential that were not logged
since the last line). One line per such key per minute. The key is the
route template: requests for different ids on one route share it, so the
tests send a different id every time.

The leak checks read stdlib ``logging`` (``caplog``) as well as structlog.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.oidc import create_access_token
from app.database import get_db
from app.models import SMSCostTracking
from shared.config import _clear_cache_for_tests, get_config
from shared.route_walk import iter_api_routes, iter_routes

from tests.conftest import app

EVENT = "admin_auth_rejected"
MODULE = "app.utils.auth_rejections"
FIELDS = {"event", "route", "method", "status", "reason", "credential_presented", "suppressed"}
LOGGER_FIELDS = {"log_level", "level", "timestamp"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"}
REASONS = {
    "no_credential", "service_key_refused", "service_key_unconfigured", "user_credential_refused",
    "insufficient_permission",
}
CREDENTIALS = {"service_key", "user", "none"}

DEVICE_ROUTE = "/api/user-sessions/device/{device_id}"
DEVICE_URL = "/api/user-sessions/device/zz-secret-id?probe=zz-query-value"


def _device_url(n):
    """A different device on the same route."""
    return f"/api/user-sessions/device/zz-secret-id-{n}?probe=zz-query-value-{n}"


LOG_SEND = "/api/sms/internal/log-send"
LOG_SEND_PARAMS = {"phone_number": "+15550100000", "content": "hi", "status": "sent"}
MODE_SET = "/api/ha-pipelines/mode/set"
PIPELINES = {"result": {"pipelines": [
    {"id": "p-simple", "name": "Ollama", "conversation_engine": "conversation.ollama_conversation"},
]}}


# ---------------------------------------------------------------------------
# The served module
# ---------------------------------------------------------------------------

def _served_namespace():
    """Globals of the module whose middleware main.app actually runs
    (another test file evicts and re-imports app.* mid-suite, so a fresh
    import can be a different module object). None until it's registered."""
    for middleware in app.user_middleware:
        if getattr(middleware.cls, "__name__", "") == "AuthRejectionMiddleware":
            return middleware.cls.__call__.__globals__
    return None


def _namespace():
    """The module's namespace, or an explicit failure naming what's missing."""
    served = _served_namespace()
    if served is not None:
        return served
    try:
        return vars(importlib.import_module(MODULE))
    except ModuleNotFoundError:
        pytest.fail(
            f"{MODULE} doesn't exist yet: the rejection log lands in plan step 6.0. "
            "This test needs AuthRejectionMiddleware, logger, _clock, _reset_for_tests and flush_suppressed."
        )


def _reset():
    for namespace in (_served_namespace(), vars(importlib.import_module(MODULE)) if _module_exists() else None):
        if namespace is not None:
            namespace["_reset_for_tests"]()
            # A lazy structlog proxy that was first used under another config
            # keeps writing there; uncached, it rebinds to the capturing one.
            vars(namespace["logger"]).pop("bind", None)


def _module_exists():
    try:
        return importlib.util.find_spec(MODULE) is not None
    except ModuleNotFoundError:
        return False


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _install_clock(monkeypatch):
    fake = Clock()
    monkeypatch.setitem(_namespace(), "_clock", fake)
    return fake


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    (mode_set,) = [w.route.endpoint for w in iter_api_routes(app) if w.path == MODE_SET]
    monkeypatch.setitem(mode_set.__globals__, "ha_websocket_command", AsyncMock(return_value=PIPELINES))
    _clear_cache_for_tests()
    _reset()
    yield mode_set.__globals__
    _reset()
    _clear_cache_for_tests()


@pytest.fixture
def config_env():
    """Set a config environment variable for one test. Undone in this order:
    the variable first, then the config cache, so the next test (in this or
    a later file) can't inherit a config object built from the patched
    value."""
    patch = pytest.MonkeyPatch()

    def set_variable(name, value):
        patch.setenv(name, value)
        _clear_cache_for_tests()

    yield set_variable
    patch.undo()
    _clear_cache_for_tests()


@pytest.fixture(scope="module", autouse=True)
def _leaves_the_config_as_the_environment_says():
    """After the last test here, whatever config object is cached (or would
    be built) matches the environment: a later file can't inherit the
    zeroed login delay or the empty service key."""
    yield
    config = get_config()
    assert config.service_api_key == os.environ.get("SERVICE_API_KEY", "") != ""
    assert config.login_minimum_delay_ms == int(os.environ.get("LOGIN_MINIMUM_DELAY_MS", "400")) > 0
    assert config.dev_mode is (os.environ.get("DEV_MODE", "").lower() == "true")


def _stdlib_text(caplog):
    """Everything the app wrote through stdlib logging. The test client's
    own request line (logger `httpx`) is the client's, not the app's."""
    return "\n".join(
        f"{record.name} {record.getMessage()} {sorted(vars(record).items())!r}"
        for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore"))
    )


@pytest.fixture
def api(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _reset()  # nothing from startup counts
            yield client
    finally:
        app.dependency_overrides.clear()


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _key():
    return {"X-Service-Key": get_config().service_api_key}


def _rejections(logs):
    return [r for r in logs if r.get("event") == EVENT]


def _shape(record):
    return {k: record.get(k) for k in ("route", "method", "status", "reason", "credential_presented")}


def _one(logs, **expected):
    records = _rejections(logs)
    assert len(records) == 1, f"{len(records)} {EVENT} record(s): {records}"
    assert _shape(records[0]) == expected
    return records[0]


# ---------------------------------------------------------------------------
# Scenarios (each returns the request's response and every captured record;
# the field test at the end replays them all)
# ---------------------------------------------------------------------------

def _anonymous(api, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.get(DEVICE_URL)
    return response, logs


def _wrong_key(api, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/room-groups", headers={"X-Service-Key": "zz-wrong-key"})
    return response, logs


def _key_on_a_user_only_route(api, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/guests", headers=_key())
    return response, logs


def _key_unconfigured(api, config_env, **_users):
    config_env("SERVICE_API_KEY", "")
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/room-groups", headers={"X-Service-Key": "zz-any-key"})
    return response, logs


def _viewer(api, viewer, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/guests", headers=_bearer(viewer))
    return response, logs


def _garbage_bearer(api, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/guests", headers={"Authorization": "Bearer zz-garbage-token"})
    return response, logs


def _unknown_api_key(api, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/guests", headers={"X-API-Key": "zz-unknown-api-key"})
    return response, logs


def _keyless_service_only(api, **_users):
    with structlog.testing.capture_logs() as logs:
        response = api.post(LOG_SEND, params=LOG_SEND_PARAMS)
    return response, logs


# L1 -----------------------------------------------------------------------

def test_an_anonymous_request_is_logged_by_route_template(api, caplog):
    caplog.set_level(logging.DEBUG)
    response, logs = _anonymous(api)
    assert response.status_code == 401
    _one(logs, route=DEVICE_ROUTE, method="GET", status=401, reason="no_credential", credential_presented="none")
    for text in (repr(logs), _stdlib_text(caplog)):
        assert "zz-secret-id" not in text
        assert "zz-query-value" not in text


# L2 -----------------------------------------------------------------------

def test_a_wrong_service_key_is_logged(api):
    response, logs = _wrong_key(api)
    assert response.status_code == 401
    _one(logs, route="/api/room-groups", method="GET", status=401, reason="service_key_refused",
         credential_presented="service_key")


# L3 -----------------------------------------------------------------------

def test_a_valid_key_on_a_user_only_route_is_logged(api):
    response, logs = _key_on_a_user_only_route(api)
    assert response.status_code == 401
    _one(logs, route="/api/guests", method="GET", status=401, reason="service_key_refused",
         credential_presented="service_key")


# L4 -----------------------------------------------------------------------

def test_a_key_sent_while_none_is_configured_is_logged(api, config_env):
    response, logs = _key_unconfigured(api, config_env)
    assert response.status_code == 503
    _one(logs, route="/api/room-groups", method="GET", status=503, reason="service_key_unconfigured",
         credential_presented="service_key")


# L5 -----------------------------------------------------------------------

def test_a_user_without_the_permission_is_logged(api, viewer_user):
    response, logs = _viewer(api, viewer_user)
    assert response.status_code == 403
    _one(logs, route="/api/guests", method="GET", status=403, reason="insufficient_permission",
         credential_presented="user")


# L6 -----------------------------------------------------------------------

@pytest.mark.parametrize("scenario", [_garbage_bearer, _unknown_api_key], ids=["bearer", "x_api_key"])
def test_a_refused_user_credential_is_logged(scenario, api):
    response, logs = scenario(api)
    assert response.status_code == 401
    _one(logs, route="/api/guests", method="GET", status=401, reason="user_credential_refused",
         credential_presented="user")


# L7 -----------------------------------------------------------------------

def test_a_keyless_call_to_a_service_only_route_is_logged(api):
    response, logs = _keyless_service_only(api)
    assert response.status_code == 422
    _one(logs, route=LOG_SEND, method="POST", status=422, reason="no_credential", credential_presented="none")


# L8 -----------------------------------------------------------------------

def test_accepted_requests_are_not_logged(api, db, operator_user):
    db.add(SMSCostTracking(month=datetime.now(timezone.utc).date().replace(day=1), message_count=0, segment_count=0,
                           incoming_count=0, outgoing_count=0, estimated_cost_cents=0,
                           outgoing_sms_cents=0, incoming_sms_cents=0))
    db.commit()
    with structlog.testing.capture_logs() as logs:
        statuses = [
            api.get("/api/room-groups", headers=_key()).status_code,
            api.get("/api/guests", headers=_bearer(operator_user)).status_code,
            api.post(LOG_SEND, params=LOG_SEND_PARAMS, headers=_key()).status_code,
        ]
    assert statuses == [200, 200, 200]
    assert _rejections(logs) == []


# L9 -----------------------------------------------------------------------

def test_a_handler_s_own_401_is_not_an_auth_rejection(api, operator_user, _production_auth, monkeypatch):
    monkeypatch.setitem(_production_auth, "ha_websocket_command",
                        AsyncMock(side_effect=HTTPException(status_code=401, detail="Home Assistant said no")))
    with structlog.testing.capture_logs() as logs:
        response = api.post(MODE_SET, json={"mode": "simple"}, headers=_bearer(operator_user))
    assert response.status_code == 401
    assert "Home Assistant said no" in response.text
    assert _rejections(logs) == []


# L10 ----------------------------------------------------------------------

def test_other_4xx_answers_are_not_auth_rejections(api, operator_user):
    with structlog.testing.capture_logs() as logs:
        statuses = [
            api.post(MODE_SET, json={"zz": 1}, headers=_bearer(operator_user)).status_code,
            api.get("/api/zz-no-such-route").status_code,
            api.request("PROPFIND", "/api/guests").status_code,
        ]
    assert statuses == [422, 404, 405]
    assert _rejections(logs) == []


# L5 and L10 on a small app ----------------------------------------------------
#
# Two rules the real app has no route for today: every role that reaches a
# guard's own permission check holds the permission (a scoped role is turned
# away earlier, by path), and every route that needs a body is guarded.

@pytest.fixture
def toy(db, operator_user):
    """The middleware and the served user guard around two routes: one that
    needs a permission no operator holds, one with a body and no guard."""
    from fastapi import Depends, FastAPI
    from pydantic import BaseModel

    (guests,) = [w for w in iter_api_routes(app) if w.path == "/api/guests" and "GET" in w.methods]
    (guard,) = [d.call for d in guests.route.dependant.dependencies
                if getattr(d.call, "__qualname__", "").startswith("require_user_permission.")]
    require_user_permission = guard.__globals__["require_user_permission"]

    class Body(BaseModel):
        name: str

    small = FastAPI()
    small.add_middleware(_namespace()["AuthRejectionMiddleware"])

    @small.get("/api/zz-owner-only/{item_id}", dependencies=[Depends(require_user_permission("manage_users"))])
    async def owner_only(item_id: str):
        return {}

    @small.post("/api/zz-unguarded")
    async def unguarded(body: Body):
        return {}

    small.dependency_overrides[get_db] = lambda: db
    with TestClient(small, raise_server_exceptions=False) as client:
        _reset()
        yield client


def test_a_permission_the_guard_itself_refuses_is_logged(toy, operator_user):
    """The guard records an accepted caller only after its permission check."""
    with structlog.testing.capture_logs() as logs:
        response = toy.get("/api/zz-owner-only/zz-secret-id", headers=_bearer(operator_user))
    assert response.status_code == 403
    _one(logs, route="/api/zz-owner-only/{item_id}", method="GET", status=403, reason="insufficient_permission",
         credential_presented="user")


def test_a_keyless_422_is_reported_only_behind_the_service_only_guard(toy):
    with structlog.testing.capture_logs() as logs:
        response = toy.post("/api/zz-unguarded", json={})
    assert response.status_code == 422
    assert _rejections(logs) == []


# L11 ----------------------------------------------------------------------

BURST = 5


def _burst_then_one_after_the_window(api, clock):
    """BURST refusals for BURST different devices inside one window, then
    one for another device after it. ([in-window logs], [later logs])"""
    with structlog.testing.capture_logs() as inside:
        for n in range(BURST):
            clock.now = float(n)
            assert api.get(_device_url(n)).status_code == 401
    with structlog.testing.capture_logs() as after:
        clock.now = 61.0
        assert api.get(_device_url(BURST)).status_code == 401
    return inside, after


def test_repeated_refusals_are_rate_limited_and_counted(api, monkeypatch):
    clock = _install_clock(monkeypatch)
    inside, after = _burst_then_one_after_the_window(api, clock)
    # One line for the route, however many different ids were asked for.
    first = _one(inside, route=DEVICE_ROUTE, method="GET", status=401, reason="no_credential",
                 credential_presented="none")
    assert first["suppressed"] == 0
    second = _one(after, route=DEVICE_ROUTE, method="GET", status=401, reason="no_credential",
                  credential_presented="none")
    assert second["suppressed"] == BURST - 1


def test_distinct_ids_on_one_route_hold_one_limiter_key(api, monkeypatch):
    """The rate-limit table is keyed on the route template, so a caller
    walking ids can't grow it (or get a log line per id)."""
    clock = _install_clock(monkeypatch)
    tracked = _namespace()["tracked_key_count"]
    assert tracked() == 0
    with structlog.testing.capture_logs() as logs:
        for n in range(25):
            clock.now = n / 10
            assert api.get(_device_url(n)).status_code == 401
    assert len(_rejections(logs)) == 1
    assert tracked() == 1
    assert api.get("/api/guests").status_code == 401
    assert tracked() == 2


# L13 ----------------------------------------------------------------------

def test_failed_password_logins_are_not_logged(api, config_env, caplog):
    caplog.set_level(logging.DEBUG)
    # The login timing floor (0.4 s a failure) isn't what's under test.
    config_env("LOGIN_MINIMUM_DELAY_MS", "0")
    with structlog.testing.capture_logs() as logs:
        statuses = [
            api.post("/api/auth/local-login",
                     json={"username": "zz-login-name", "password": "zz-wrong-password"}).status_code
            for _ in range(4)
        ]
    assert statuses == [401, 401, 401, 401]
    assert _rejections(logs) == []
    for text in (repr(logs), _stdlib_text(caplog)):
        assert "zz-login-name" not in text
        assert "zz-wrong-password" not in text


def test_the_login_exclusion_is_one_route_not_a_path_prefix(api):
    """GET /api/auth/session-token also starts with /api/auth/ and answers
    401 to an anonymous caller; it is still reported."""
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/auth/session-token")
    assert response.status_code == 401
    _one(logs, route="/api/auth/session-token", method="GET", status=401, reason="no_credential",
         credential_presented="none")


# L15 ----------------------------------------------------------------------

def test_a_websocket_connection_is_left_alone(api, config_env):
    (ws_globals,) = [
        w.route.endpoint.__globals__ for w in iter_routes(app) if w.path == "/ws/admin-jarvis"
    ]
    config_env("DEV_MODE", "false")
    ws_globals["get_config"].cache_clear()
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(WebSocketDisconnect) as closed:
                with api.websocket_connect("/ws/admin-jarvis") as ws:
                    ws.send_json({"type": "ping"})
                    ws.receive_json()
    finally:
        ws_globals["get_config"].cache_clear()
    assert closed.value.code == 4001
    assert _rejections(logs) == []


# L16 ----------------------------------------------------------------------

def test_a_failing_logger_does_not_change_the_response(api, monkeypatch):
    expected = api.get(DEVICE_URL)
    attempts = []

    class _Raising:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                attempts.append(name)
                raise RuntimeError("logger is broken")
            return fail

    namespace = _namespace()
    namespace["_reset_for_tests"]()
    monkeypatch.setitem(namespace, "logger", _Raising())
    response = api.get(DEVICE_URL)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json()) == (
        401, {"detail": "Not authenticated"})
    assert attempts, "the raising logger was really called"


# L17a ---------------------------------------------------------------------

def _expired_key_is_flushed_by_another(api, clock):
    with structlog.testing.capture_logs() as logs:
        for n in range(BURST):
            clock.now = float(n)
            assert api.get(_device_url(n)).status_code == 401
        clock.now = 61.0
        assert api.get("/api/guests").status_code == 401
    return logs


def test_suppressed_counts_are_flushed_when_another_key_is_processed(api, monkeypatch):
    clock = _install_clock(monkeypatch)
    records = _rejections(_expired_key_is_flushed_by_another(api, clock))
    assert sorted((r["route"], r["suppressed"]) for r in records) == [
        ("/api/guests", 0), (DEVICE_ROUTE, 0), (DEVICE_ROUTE, 4),
    ]
    (flush,) = [r for r in records if r["suppressed"] == 4]
    assert _shape(flush) == dict(route=DEVICE_ROUTE, method="GET", status=401, reason="no_credential",
                                 credential_presented="none")


# L17b ---------------------------------------------------------------------

def test_suppressed_counts_are_flushed_once_at_shutdown(api, monkeypatch):
    clock = _install_clock(monkeypatch)
    flush = _namespace()["flush_suppressed"]
    with structlog.testing.capture_logs() as logs:
        for n in (0, 1, 2):
            clock.now = float(n)
            assert api.get(_device_url(n)).status_code == 401
        before = len(_rejections(logs))
        flush()
        after_first = _rejections(logs)[before:]
        flush()
        after_second = _rejections(logs)[before + len(after_first):]
    assert before == 1
    assert [(r["route"], r["suppressed"]) for r in after_first] == [(DEVICE_ROUTE, 2)]
    assert after_second == []


# L18 ----------------------------------------------------------------------

LONG_VERB = "Z" * 2000


def _drive(middleware_class, method):
    """One HTTP request with this verb through the middleware around a stub
    app that answers 401 to anything. Through the real app a non-standard
    verb is a 405 before any guard runs, so only a stub shows the mapping."""

    async def stub(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": "/zz/stub", "raw_path": b"/zz/stub", "query_string": b"",
        "headers": [], "client": ("192.0.2.7", 4000), "server": ("testserver", 80),
    }
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(middleware_class(stub)(scope, receive, send))
    finally:
        loop.close()
    assert sent[0]["status"] == 401, "the response passes through unchanged"


def _unusual_methods():
    namespace = _namespace()
    out = []
    for method in ("PROPFIND", "get", LONG_VERB):
        namespace["_reset_for_tests"]()
        with structlog.testing.capture_logs() as logs:
            _drive(namespace["AuthRejectionMiddleware"], method)
        out.append(logs)
    return out


def test_the_method_field_is_a_closed_set():
    logs = _unusual_methods()
    records = [_one(captured, route="<unmatched>", method=method, status=401, reason="no_credential",
                    credential_presented="none")
               for captured, method in zip(logs, ("OTHER", "GET", "OTHER"))]
    assert len(records) == 3
    assert LONG_VERB not in repr(logs) and "PROPFIND" not in repr(logs)


# L12 (last: it replays every scenario above) --------------------------------

SECRETS = (
    "zz-secret-id", "zz-query-value", "zz-wrong-key", "zz-any-key", "zz-garbage-token",
    "zz-unknown-api-key", "+15550100000", "testclient", "192.0.2.7", "Bearer", LONG_VERB,
)


def test_the_stdlib_leak_check_sees_a_stdlib_line(caplog):
    """Positive control for the caplog half of the leak checks."""
    caplog.set_level(logging.DEBUG)
    logging.getLogger("app.zz_planted").info("GET /api/user-sessions/device/zz-secret-id refused")
    logging.getLogger("app.zz_planted").warning("refused", extra={"path": "/x?probe=zz-query-value"})
    logging.getLogger("httpx").warning("HTTP Request: GET http://testserver/zz-client-side-line")
    text = _stdlib_text(caplog)
    assert "zz-secret-id" in text and "zz-query-value" in text
    assert "zz-client-side-line" not in text


STDLIB_SECRETS = (
    "zz-secret-id", "zz-query-value", "zz-wrong-key", "zz-any-key", "zz-garbage-token", "zz-unknown-api-key",
    LONG_VERB,
)


def test_records_carry_only_the_agreed_fields(api, viewer_user, monkeypatch, config_env, caplog):
    caplog.set_level(logging.DEBUG)
    clock = _install_clock(monkeypatch)
    server_key = get_config().service_api_key
    captured = []
    for scenario in (_anonymous, _wrong_key, _key_on_a_user_only_route, _viewer, _garbage_bearer,
                     _unknown_api_key, _keyless_service_only):
        _reset()
        captured.extend(scenario(api, viewer=viewer_user)[1])
    _reset()
    inside, after = _burst_then_one_after_the_window(api, clock)
    captured.extend(inside + after)
    _reset()
    captured.extend(_expired_key_is_flushed_by_another(api, clock))
    for logs in _unusual_methods():
        captured.extend(logs)
    _reset()
    captured.extend(_key_unconfigured(api, config_env)[1])

    records = _rejections(captured)
    assert len(records) >= 12, f"{len(records)} {EVENT} record(s) examined"
    for record in records:
        assert set(record) <= FIELDS | LOGGER_FIELDS, sorted(set(record) - FIELDS - LOGGER_FIELDS)
        assert "caller_kind" not in record
        assert record["credential_presented"] in CREDENTIALS
        assert record["method"] in METHODS
        assert record["reason"] in REASONS
        assert isinstance(record["status"], int) and isinstance(record["suppressed"], int)
        assert record["route"] == "<unmatched>" or record["route"].startswith("/api/")
        assert "?" not in record["route"]
        text = repr(sorted(record.items()))
        assert server_key not in text
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} reached a record: {record}"
    # Nothing the requests carried reaches stdlib logging either (the
    # address and "Bearer" aside: other loggers may name those).
    stdlib = _stdlib_text(caplog)
    assert server_key not in stdlib
    for secret in STDLIB_SECRETS:
        assert secret not in stdlib, f"{secret!r} reached stdlib logging"


def test_case_population():
    """23 tests: L1-L13 and L15-L18, with L11, L13 and L17 as two tests
    each, the two small-app cases for L5 and L10, and the positive control
    for the stdlib leak check. L14 is in the matrix file, with the route it
    needs."""
    tests = [name for name in vars(sys.modules[__name__])
             if name.startswith("test_") and name != "test_case_population"]
    assert len(tests) == 23, sorted(tests)
    assert "test_an_anonymous_request_is_logged_by_route_template" in tests
