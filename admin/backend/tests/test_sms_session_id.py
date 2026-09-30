"""The SMS conversation id is keyed on SERVICE_API_KEY and never carries the
phone number (it's logged by the orchestrator's session logs)."""
from __future__ import annotations

import hashlib
import hmac
import re

import pytest

from shared.config import _clear_cache_for_tests

NUMBER = "+15555550100"


@pytest.fixture
def key(monkeypatch):
    def use(value: str):
        monkeypatch.setenv("SERVICE_API_KEY", value)
        _clear_cache_for_tests()

    use("sms-session-test-key-one")
    yield use
    _clear_cache_for_tests()


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
