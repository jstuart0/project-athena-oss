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
import os
import subprocess
import sys
from hashlib import sha1
from pathlib import Path
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
BASE = "https://sms.example.test"

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
    url = BASE + path
    if query:
        url += "?" + query
    return url


def _sign(url, params, token: str = TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, params)


def _encode_form(pairs) -> bytes:
    return urlencode(list(pairs)).encode("utf-8")


@pytest.fixture(autouse=True)
def _default_base_url(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", BASE, raising=False)


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


# ---------------------------------------------------------------------------
# Phase 2: validate against the configured external URL, never the Host
# header (P2 #1-#19)
# ---------------------------------------------------------------------------

MALFORMED_BASES = {
    "no_scheme": "sms.example.test",
    "ftp_scheme": "ftp://sms.example.test",
    "has_query": "https://sms.example.test/?x=1",
    "has_fragment": "https://sms.example.test#rp=5xx",
    "empty_netloc": "https://",
    "non_numeric_port": "https://sms.example.test:abc",
    "port_out_of_range": "https://sms.example.test:99999",
    "has_userinfo": "https://twilio-user:hunter2-not-real@sms.example.test",
}


def test_validation_url_uses_configured_base_not_host(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    url = _signing_url(INCOMING_PATH)
    sig = _sign(url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())

    async def _do():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = {
                "content-type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": sig,
                "Host": "attacker.example",
                "X-Forwarded-Proto": "http",
            }
            return await client.post(INCOMING_PATH, content=body, headers=headers)

    resp = asyncio.run(_do())
    assert resp.status_code == 200


def test_signature_over_request_url_rejected(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    request_url = "http://testserver" + INCOMING_PATH
    sig = _sign(request_url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())
    resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 403


def test_query_string_included(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    path_with_query = INCOMING_PATH + "?tenant=a"
    body = _encode_form(INCOMING_PARAMS.items())

    signed_with_query = _sign(_signing_url(INCOMING_PATH, "tenant=a"), INCOMING_PARAMS)
    resp_ok = post(path_with_query, body, signed_with_query)
    assert resp_ok.status_code == 200

    signed_without_query = _sign(_signing_url(INCOMING_PATH), INCOMING_PARAMS)
    resp_rejected = post(path_with_query, body, signed_without_query)
    assert resp_rejected.status_code == 403


BASE_PREFIX_CASES = {
    "prefix": "https://sms.example.test/athena",
    "trailing_slash": "https://sms.example.test/",
}


@pytest.mark.parametrize("case", list(BASE_PREFIX_CASES))
def test_base_url_prefix_and_trailing_slash(case, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    base = BASE_PREFIX_CASES[case]
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", base)
    url = base.rstrip("/") + INCOMING_PATH
    sig = _sign(url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())
    resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 200


def test_base_url_with_port(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    base = "https://sms.example.test:8443"
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", base)
    url = base + INCOMING_PATH
    sig = _sign(url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())
    resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 200


def test_token_set_base_unset_rejects_503(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", "")
    url = "http://testserver" + INCOMING_PATH
    sig = _sign(url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())
    with structlog.testing.capture_logs() as cap:
        resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 503
    events = [e.get("event") for e in cap]
    assert "twilio_webhook_base_url_not_configured" in events


@pytest.mark.parametrize("case", list(MALFORMED_BASES))
def test_token_set_base_malformed_rejects_503(case, monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    base = MALFORMED_BASES[case]
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", base)
    # Signed as Phase-1 code would have accepted (Host-derived), so a
    # regression back to Phase-1 behavior would show as 200, not 503.
    url = "http://testserver" + INCOMING_PATH
    sig = _sign(url, INCOMING_PARAMS)
    body = _encode_form(INCOMING_PARAMS.items())
    with structlog.testing.capture_logs() as cap:
        resp = post(INCOMING_PATH, body, sig)
    assert resp.status_code == 503
    events = [e.get("event") for e in cap]
    assert "twilio_webhook_base_url_not_configured" in events
    for entry in cap:
        for key, value in entry.items():
            assert base not in str(key) and base not in str(value)
    if case == "has_userinfo":
        for entry in cap:
            for key, value in entry.items():
                assert "hunter2-not-real" not in str(key)
                assert "hunter2-not-real" not in str(value)


def test_token_unset_ignores_base_url(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(sw, "TWILIO_WEBHOOK_BASE_URL", "")
    body = _encode_form(INCOMING_PARAMS.items())
    resp = post(INCOMING_PATH, body, signature=None)
    assert resp.status_code == 200


def test_rejection_logs_use_path_not_url(monkeypatch):
    monkeypatch.setattr(sw, "TWILIO_AUTH_TOKEN", TOKEN)
    body = _encode_form(INCOMING_PARAMS.items())
    wrong_sig = _sign(_signing_url(INCOMING_PATH), INCOMING_PARAMS, token="a-different-token-not-real")
    with structlog.testing.capture_logs() as cap:
        resp_missing = post(INCOMING_PATH, body, signature=None)
        resp_wrong = post(INCOMING_PATH, body, wrong_sig)
    assert resp_missing.status_code == 403
    assert resp_wrong.status_code == 403
    by_event = {e.get("event"): e for e in cap}
    assert by_event["twilio_signature_header_missing"]["path"] == INCOMING_PATH
    assert by_event["twilio_signature_invalid"]["path"] == INCOMING_PATH
    for entry in cap:
        assert "url" not in entry
        for value in entry.values():
            assert "://" not in str(value)


IMPORT_MISCONFIG_CASES = {
    "base_unset": None,
    "base_valid": "https://sms.example.test",
}


@pytest.mark.parametrize("case", list(IMPORT_MISCONFIG_CASES))
def test_import_time_misconfig_logged(case):
    base_env = IMPORT_MISCONFIG_CASES[case]
    env = dict(os.environ)
    env["DEV_MODE"] = "true"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["TWILIO_AUTH_TOKEN"] = TOKEN
    if base_env is None:
        env.pop("TWILIO_WEBHOOK_BASE_URL", None)
    else:
        env["TWILIO_WEBHOOK_BASE_URL"] = base_env

    backend_dir = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import app.routes.sms_webhook"],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0
    if case == "base_unset":
        assert "twilio_webhook_base_url_not_configured" in combined
        assert TOKEN not in combined
    else:
        assert "twilio_webhook_base_url_not_configured" not in combined
