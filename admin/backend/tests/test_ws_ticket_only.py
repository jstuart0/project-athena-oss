"""The Admin Jarvis WebSocket accepts only a single-use ws-ticket.

A session JWT in ``?token=`` is closed with 4001 like any other non-ticket
token (it would otherwise sit in URLs and logs for its whole lifetime). A
minted ticket connects once; a reused one is 4001.

DEV_MODE is off for the WebSocket (via the get_config its module holds) and
for the ticket mint (via the oidc module the served app holds): another test
file evicts and re-imports app.* mid-suite, so both are resolved through
the served app, never by a fresh import.
"""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from shared.route_walk import iter_routes
from tests.conftest import app, get_current_user as served_get_current_user

WS_PATH = "/ws/admin-jarvis"


def _ws_globals():
    for walked in iter_routes(app):
        if walked.path == WS_PATH:
            return walked.route.endpoint.__globals__
    raise AssertionError("no WebSocket route")


@pytest.fixture
def production(monkeypatch, client):
    ws_globals = _ws_globals()
    monkeypatch.setenv("DEV_MODE", "false")
    ws_globals["get_config"].cache_clear()
    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    yield ws_globals
    ws_globals["get_config"].cache_clear()


def _session_jwt(user):
    create_access_token = served_get_current_user.__globals__["create_access_token"]
    return create_access_token({"user_id": user.id, "username": user.username, "role": user.role})


def _close_code(client, token):
    """The close code of a refused connection. An accepted one answers the
    ping instead, so pytest.raises fails fast rather than waiting for the
    server's 60 s heartbeat."""
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"{WS_PATH}?token={token}") as ws:
            ws.send_json({"type": "ping"})
            ws.receive_json()
    return closed.value.code


def _mint(client, user):
    resp = client.post("/api/auth/ws-ticket", headers={"Authorization": f"Bearer {_session_jwt(user)}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["ticket"]


def test_a_session_jwt_is_refused(client, production, test_user):
    assert _close_code(client, _session_jwt(test_user)) == 4001


def test_garbage_is_refused(client, production):
    assert _close_code(client, "not-a-jwt") == 4001


def test_a_minted_ticket_connects_once(client, production, test_user):
    ticket = _mint(client, test_user)
    with client.websocket_connect(f"{WS_PATH}?token={ticket}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json().get("event_type") == "pong"
    assert _close_code(client, ticket) == 4001
