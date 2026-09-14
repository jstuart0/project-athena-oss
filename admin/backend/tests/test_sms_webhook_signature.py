"""
Integration tests for Twilio SMS webhook signature validation.

Drives the real FastAPI app over ASGI with genuinely signed requests
(httpx.ASGITransport, no TestClient — see D5 in the ATHENA-72 plan: the
committed `main` pins break TestClient under httpx 0.28). Only `get_db` is
overridden (the conftest `db` fixture); `sms_webhook.TWILIO_AUTH_TOKEN` (and,
in Phase 2, `sms_webhook.TWILIO_WEBHOOK_BASE_URL`) are patched via
monkeypatch. Nothing patches `validate_twilio_signature`, `RequestValidator`,
`Request`, `request.form`/`body`/`stream`, or the handlers.
"""

import asyncio
import base64
import hmac
from hashlib import sha1
from urllib.parse import urlencode

import httpx
import pytest
import structlog
from fastapi.routing import APIRoute
from starlette.datastructures import ImmutableMultiDict
from twilio.request_validator import RequestValidator

import app.routes.sms_webhook as sw
from app.database import get_db
from main import app

TOKEN = "test-twilio-auth-token-not-real"

INCOMING_PATH = "/api/sms/webhook/incoming"
STATUS_PATH = "/api/sms/webhook/status"

INCOMING_PARAMS = {
    "From": "+15550001111",
    "Body": "hi",
    "MessageSid": "SM0000000000000000000000000000test",
    "To": "",
    "NumMedia": "0",
}
STATUS_PARAMS = {
    "MessageSid": "SM0000000000000000000000000000test",
    "MessageStatus": "delivered",
    "To": "",
    "ErrorCode": "",
}
ROUTE_FIXTURES = {
    "incoming": (INCOMING_PATH, INCOMING_PARAMS),
    "status": (STATUS_PATH, STATUS_PARAMS),
}
TAMPER_FIXTURES = {
    "incoming": (INCOMING_PATH, INCOMING_PARAMS, dict(INCOMING_PARAMS, Body="bye")),
    "status": (STATUS_PATH, STATUS_PARAMS, dict(STATUS_PARAMS, MessageStatus="failed")),
}


def _signing_url(path: str, query: str = "") -> str:
    """Literal string join for test signing — never calls production code."""
    url = "http://testserver" + path
    if query:
        url += "?" + query
    return url


def _sign(url, params, token: str = TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, params)


def _encode_form(pairs) -> bytes:
    return urlencode(list(pairs)).encode("utf-8")


@pytest.fixture(autouse=True)
def _override_get_db(db):
    def _get_db_override():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _get_db_override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _post(path, body: bytes, signature=None, content_type="application/x-www-form-urlencoded"):
    headers = {"content-type": content_type}
    if signature is not None:
        headers["X-Twilio-Signature"] = signature
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, content=body, headers=headers)


def post(path, body: bytes, signature=None, content_type="application/x-www-form-urlencoded"):
    return asyncio.run(_post(path, body, signature, content_type))


# ---------------------------------------------------------------------------
# Acceptance shapes (P1 #1-#6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["incoming", "status"])
def test_signed_request_accepted(route, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    path, params = ROUTE_FIXTURES[route]
    url = _signing_url(path)
    sig = _sign(url, params)
    body = _encode_form(params.items())
    resp = post(path, body, sig)
    assert resp.status_code == 200
    if route == "incoming":
        assert resp.headers["content-type"].startswith("application/xml")
        assert "<Response>" in resp.text
    else:
        assert resp.json() == {"status": "ok"}


def test_signed_request_with_blank_params_accepted(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    params = dict(INCOMING_PARAMS, FromCity="")
    url = _signing_url(INCOMING_PATH)
    sig = _sign(url, params)
    body = _encode_form(params.items())
    resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 200


def test_content_type_with_charset_accepted(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    url = _signing_url(INCOMING_PATH)
    sig = _sign(url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())
    resp = post(
        INCOMING_PATH,
        body,
        sig,
        content_type="application/x-www-form-urlencoded; charset=utf-8",
    )
    assert resp.status_code == 200


def test_unicode_body_accepted(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    params = dict(INCOMING_PARAMS, Body="héllo 👋 wörld")
    url = _signing_url(INCOMING_PATH)
    # Signature is computed over the decoded str params, matching what
    # Starlette's form parser hands back after decoding the wire bytes.
    sig = _sign(url, params)
    body = _encode_form(params.items())
    resp = post(
        INCOMING_PATH,
        body,
        sig,
        content_type="application/x-www-form-urlencoded; charset=utf-8",
    )
    assert resp.status_code == 200


def test_duplicate_param_values_signed_correctly(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    pairs = list(INCOMING_PARAMS.items()) + [("FromCountry", "US"), ("FromCountry", "USA")]
    url = _signing_url(INCOMING_PATH)
    sig = _sign(url, ImmutableMultiDict(pairs))
    body = _encode_form(pairs)
    resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rejections (P1 #7-#14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["incoming", "status"])
def test_missing_signature_rejected(route, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    path, params = ROUTE_FIXTURES[route]
    body = _encode_form(params.items())
    resp = post(path, body, signature=None)
    assert resp.status_code == 403


@pytest.mark.parametrize("route", ["incoming", "status"])
def test_wrong_signature_rejected(route, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    path, params = ROUTE_FIXTURES[route]
    url = _signing_url(path)
    wrong_sig = _sign(url, params, token="a-different-token-not-real")
    body = _encode_form(params.items())
    resp = post(path, body, wrong_sig)
    assert resp.status_code == 403


@pytest.mark.parametrize("route", ["incoming", "status"])
def test_tampered_param_rejected(route, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    path, signed_params, sent_params = TAMPER_FIXTURES[route]
    url = _signing_url(path)
    sig = _sign(url, signed_params)
    body = _encode_form(sent_params.items())
    resp = post(path, body, sig)
    assert resp.status_code == 403


@pytest.mark.parametrize("route", ["incoming", "status"])
def test_multipart_rejected_not_500(route, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    path, params = ROUTE_FIXTURES[route]

    async def _do():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            files = {"Attachment": ("evidence.txt", b"not a twilio field", "text/plain")}
            return await client.post(
                path,
                data=dict(params),
                files=files,
                headers={"X-Twilio-Signature": "irrelevant-not-checked-before-media-type"},
            )

    resp = asyncio.run(_do())
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Skip/log behavior (P1 #15-#16)
# ---------------------------------------------------------------------------


def test_token_unset_accepts_and_warns(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", "")
    body = _encode_form(INCOMING_PARAMS.items())
    with structlog.testing.capture_logs() as cap:
        resp = post(INCOMING_PATH, body, signature=None)
    assert resp.status_code == 200
    events = [e.get("event") for e in cap]
    assert "twilio_auth_token_not_configured_skipping_validation" in events


def test_rejection_logs_omit_signature_and_token(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    url = _signing_url(INCOMING_PATH)
    wrong_sig = _sign(url, INCOMING_PARAMS, token="a-different-token-not-real")
    body = _encode_form(INCOMING_PARAMS.items())
    with structlog.testing.capture_logs() as cap:
        resp_missing = post(INCOMING_PATH, body, signature=None)
        resp_wrong = post(INCOMING_PATH, body, wrong_sig)
    assert resp_missing.status_code == 403
    assert resp_wrong.status_code == 403
    events = {e.get("event") for e in cap}
    assert "twilio_signature_invalid" in events
    assert "twilio_signature_header_missing" in events
    for entry in cap:
        for key, value in entry.items():
            assert wrong_sig not in str(key) and wrong_sig not in str(value)
            assert TOKEN not in str(key) and TOKEN not in str(value)


# ---------------------------------------------------------------------------
# Drift guard and oracle (P1 #17-#18)
# ---------------------------------------------------------------------------


def _iter_api_routes(routes):
    """Flatten APIRoute objects out of app.routes.

    fastapi 0.104.1 (MAIN) lists APIRoute objects directly in app.routes.
    fastapi 0.141.1 (A63) wraps each include_router() call in an internal
    router object exposing the original APIRouter as .original_router;
    recurse through that to reach the real APIRoute objects on either stack.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            sub_router = getattr(route, "original_router", None)
            if sub_router is not None:
                yield from _iter_api_routes(sub_router.routes)


def test_webhook_routes_all_declare_signature_dependency():
    def _dependant_calls(dependant):
        calls = [dependant.call]
        for sub in dependant.dependencies:
            calls.extend(_dependant_calls(sub))
        return calls

    webhook_routes = [
        route
        for route in _iter_api_routes(app.routes)
        if route.path.startswith("/api/sms/webhook")
    ]
    assert len(webhook_routes) >= 2
    paths = {route.path for route in webhook_routes}
    assert {"/api/sms/webhook/incoming", "/api/sms/webhook/status"} <= paths
    for route in webhook_routes:
        calls = _dependant_calls(route.dependant)
        assert sw.validate_twilio_signature in calls, route.path


def test_signature_cross_check_against_stdlib_hmac():
    params = {k: v for k, v in INCOMING_PARAMS.items() if k != "To"}
    url = _signing_url(INCOMING_PATH)
    library_sig = RequestValidator(TOKEN).compute_signature(url, params)
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    expected = base64.b64encode(
        hmac.new(TOKEN.encode("utf-8"), payload.encode("utf-8"), sha1).digest()
    ).decode("utf-8")
    assert library_sig == expected
