"""The SMS conversation id is keyed on SERVICE_API_KEY and never carries the
phone number (it's logged by the orchestrator's session logs)."""
from __future__ import annotations

import hashlib
import hmac
import re
from types import SimpleNamespace

import pytest

NUMBER = "+15555550100"


@pytest.fixture
def key(monkeypatch):
    """Patch the module's own get_config: other tests reload shared.config,
    so clearing its cache wouldn't reach the function sms_webhook holds."""
    import app.routes.sms_webhook as sms_webhook

    def use(value: str):
        monkeypatch.setattr(sms_webhook, "get_config", lambda: SimpleNamespace(service_api_key=value))

    use("sms-session-test-key-one")
    return use


def _session_id(number: str) -> str:
    from app.routes.sms_webhook import sms_session_id

    return sms_session_id(number)


def test_shape_and_no_number(key):
    session_id = _session_id(NUMBER)
    assert re.fullmatch(r"sms_[0-9a-f]{24}", session_id), session_id
    assert "5555550100" not in session_id
    assert "15555550100" not in session_id


def test_stable_across_formatting(key):
    assert _session_id(NUMBER) == _session_id("+1 (555) 555-0100")


def test_differs_across_numbers_and_keys(key):
    first = _session_id(NUMBER)
    assert first != _session_id("+15555550101")
    key("sms-session-test-key-two")
    assert _session_id(NUMBER) != first


def test_message_is_domain_separated(key):
    plain = hmac.new(b"sms-session-test-key-one", NUMBER.encode(), hashlib.sha256).hexdigest()[:24]
    assert _session_id(NUMBER) != "sms_" + plain
    separated = hmac.new(
        b"sms-session-test-key-one", b"athena-sms-session-v1|" + NUMBER.encode(), hashlib.sha256,
    ).hexdigest()[:24]
    assert _session_id(NUMBER) == "sms_" + separated


_DOMAIN = b"athena-sms-session-v1|"


def test_golden_vector_national_number(key):
    expected = hmac.new(b"sms-session-test-key-one", _DOMAIN + b"5555550100", hashlib.sha256).hexdigest()[:24]
    assert _session_id("5555550100") == "sms_" + expected


def test_golden_vector_alphanumeric_sender(key):
    """An alphanumeric sender normalises to nothing; its id is stable and
    doesn't raise."""
    expected = hmac.new(b"sms-session-test-key-one", _DOMAIN + b"", hashlib.sha256).hexdigest()[:24]
    assert _session_id("ATHENA") == "sms_" + expected

