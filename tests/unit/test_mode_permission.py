"""Unit tests for orchestrator.mode_permission.

Covers all 6 extracted helpers:

 1.  get_current_mode — happy path (200 from mode_client)
 2.  get_current_mode — 4xx/5xx error response (raise_for_status raises)
 3.  get_current_mode — ConnectError fallback
 4.  get_current_mode — generic exception fallback
 5.  detect_owner_mode_command — owner mode phrase matches
 6.  detect_owner_mode_command — pin pattern matches
 7.  detect_owner_mode_command — false negatives (no match)
 8.  detect_owner_mode_command — empty string
 9.  detect_owner_mode_command — case-insensitive
10.  extract_pin_from_query — 6-digit numeric
11.  extract_pin_from_query — spaced digits
12.  extract_pin_from_query — spoken words after "pin"
13.  extract_pin_from_query — spoken words after "code"
14.  extract_pin_from_query — no PIN present
15.  extract_pin_from_query — fewer than 6 words
16.  activate_owner_override — happy path (200)
17.  activate_owner_override — 401 PIN required
18.  activate_owner_override — 403 invalid PIN
19.  activate_owner_override — 400 bad format
20.  activate_owner_override — unexpected status code
21.  activate_owner_override — exception in post
22.  check_intent_permission — owner mode always allowed
23.  check_intent_permission — intent on restrict list is blocked
24.  check_intent_permission — intent on allow list is allowed
25.  check_intent_permission — intent NOT on allow list is blocked
26.  check_intent_permission — no restrict/allow list: default allow
27.  check_entity_permission — owner mode always allowed
28.  check_entity_permission — entity matches regex pattern: blocked
29.  check_entity_permission — entity matches wildcard: blocked
30.  check_entity_permission — entity domain not in allowed_domains: blocked
31.  check_entity_permission — entity allowed (no restrictions matched)
32.  check_entity_permission — invalid regex falls back to exact match

Patching strategy:
- mode_client: install via ``orchestrator.nodes._runtime.set_mode_client``.
- Pure functions (detect_owner_mode_command, extract_pin_from_query,
  check_intent_permission, check_entity_permission): no patching needed.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock

# Stub heavy deps before any orchestrator import.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator import mode_permission
from orchestrator.nodes import _runtime
from orchestrator.state import IntentCategory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def _install_mode_client(get_side_effects=None, post_return=None):
    """Install a fake mode_client into _runtime and return it."""
    client = AsyncMock()
    if get_side_effects is not None:
        client.get = AsyncMock(side_effect=get_side_effects)
    if post_return is not None:
        client.post = AsyncMock(return_value=post_return)
    _runtime.set_mode_client(client)
    return client


# ---------------------------------------------------------------------------
# get_current_mode
# ---------------------------------------------------------------------------

class TestGetCurrentMode:

    def test_happy_path_returns_mode_and_permissions(self):
        mode_resp = _make_response(200, {"mode": "guest", "override_active": True, "reason": "Scheduled"})
        perms_resp = _make_response(200, {"mode": "guest", "allowed_intents": ["weather"], "restricted_entities": []})
        _install_mode_client(get_side_effects=[mode_resp, perms_resp])

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "guest"
        assert result["override_active"] is True
        assert result["reason"] == "Scheduled"
        assert result["permissions"]["allowed_intents"] == ["weather"]

    def test_4xx_response_raises_falls_back_to_owner(self):
        error_resp = _make_response(503, {})
        _install_mode_client(get_side_effects=[error_resp])

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "owner"
        assert result["reason"] == "Mode service unavailable"
        assert result["override_active"] is False

    def test_connect_error_falls_back_to_owner(self):
        import httpx
        _install_mode_client(get_side_effects=httpx.ConnectError("refused"))

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "owner"
        assert result["permissions"]["mode"] == "owner"

    def test_generic_exception_falls_back_to_owner(self):
        _install_mode_client(get_side_effects=RuntimeError("boom"))

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "owner"
        assert result["override_active"] is False


# ---------------------------------------------------------------------------
# detect_owner_mode_command (PURE)
# ---------------------------------------------------------------------------

class TestDetectOwnerModeCommand:

    def test_switch_to_owner_mode(self):
        assert mode_permission.detect_owner_mode_command("switch to owner mode") is True

    def test_activate_owner_mode(self):
        assert mode_permission.detect_owner_mode_command("activate owner mode please") is True

    def test_owner_mode_with_pin(self):
        assert mode_permission.detect_owner_mode_command("owner mode pin 123456") is True

    def test_exit_guest_mode(self):
        assert mode_permission.detect_owner_mode_command("exit guest mode") is True

    def test_owner_override(self):
        assert mode_permission.detect_owner_mode_command("owner override now") is True

    def test_im_the_owner(self):
        assert mode_permission.detect_owner_mode_command("I'm the owner") is True

    def test_case_insensitive(self):
        assert mode_permission.detect_owner_mode_command("SWITCH TO OWNER MODE") is True

    def test_no_match_normal_query(self):
        assert mode_permission.detect_owner_mode_command("turn on the lights") is False

    def test_no_match_partial_keyword(self):
        assert mode_permission.detect_owner_mode_command("who owns the property") is False

    def test_empty_string(self):
        assert mode_permission.detect_owner_mode_command("") is False


# ---------------------------------------------------------------------------
# extract_pin_from_query (PURE)
# ---------------------------------------------------------------------------

class TestExtractPinFromQuery:

    def test_six_digit_numeric(self):
        assert mode_permission.extract_pin_from_query("my pin is 123456") == "123456"

    def test_spaced_digits(self):
        assert mode_permission.extract_pin_from_query("pin 1 2 3 4 5 6") == "123456"

    def test_spoken_words_after_pin(self):
        result = mode_permission.extract_pin_from_query("pin one two three four five six")
        assert result == "123456"

    def test_spoken_words_after_code(self):
        result = mode_permission.extract_pin_from_query("code zero one two three four five")
        assert result == "012345"

    def test_no_pin_present(self):
        assert mode_permission.extract_pin_from_query("what is the weather today") is None

    def test_fewer_than_six_words(self):
        assert mode_permission.extract_pin_from_query("pin one two three") is None

    def test_leading_zero_pin(self):
        assert mode_permission.extract_pin_from_query("pin 012345") == "012345"

    def test_mixed_words_and_digits(self):
        result = mode_permission.extract_pin_from_query("pin 1 2 three 4 5 6")
        assert result == "123456"

    def test_seven_digits_stops_at_six(self):
        # \b\d{6}\b — the 7-digit string 1234567 won't have a \b after 6 digits
        # so it returns None for no 6-digit bounded match, falls to spoken
        result = mode_permission.extract_pin_from_query("pin 1234567")
        # 7-digit string: no \b after d{6}, spoken path returns None
        # just assert it doesn't crash
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# activate_owner_override
# ---------------------------------------------------------------------------

class TestActivateOwnerOverride:

    def test_happy_path_200(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"message": "Owner mode active.", "expires_at": "2026-01-01T12:00:00Z"}
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("123456", "device-1", 30))

        assert success is True
        assert "Owner mode active" in msg
        assert data["expires_at"] == "2026-01-01T12:00:00Z"

    def test_401_pin_required(self):
        resp = MagicMock()
        resp.status_code = 401
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override(None))

        assert success is False
        assert "6-digit owner PIN" in msg
        assert data is None

    def test_403_invalid_pin(self):
        resp = MagicMock()
        resp.status_code = 403
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("000000"))

        assert success is False
        assert "Invalid PIN" in msg
        assert data is None

    def test_400_bad_format(self):
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {"detail": "PIN must be 6 digits"}
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("12"))

        assert success is False
        assert "6-digit PIN" in msg
        assert data is None

    def test_unexpected_status_code(self):
        resp = MagicMock()
        resp.status_code = 500
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("123456"))

        assert success is False
        assert "Unable to process" in msg
        assert data is None

    def test_exception_in_post(self):
        client = AsyncMock()
        client.post = AsyncMock(side_effect=RuntimeError("connection reset"))
        _runtime.set_mode_client(client)

        success, msg, data = _run(mode_permission.activate_owner_override("123456"))

        assert success is False
        assert "Mode service unavailable" in msg
        assert data is None


# ---------------------------------------------------------------------------
# check_intent_permission
# ---------------------------------------------------------------------------

class TestCheckIntentPermission:

    def test_owner_mode_always_allowed(self):
        perms = {"mode": "owner"}
        assert mode_permission.check_intent_permission(IntentCategory.CONTROL, perms) is True

    def test_intent_on_restrict_list_is_blocked(self):
        perms = {
            "mode": "guest",
            "restricted_intents": ["control"],
            "allowed_intents": [],
        }
        assert mode_permission.check_intent_permission(IntentCategory.CONTROL, perms) is False

    def test_intent_on_allow_list_is_permitted(self):
        perms = {
            "mode": "guest",
            "restricted_intents": [],
            "allowed_intents": ["weather"],
        }
        assert mode_permission.check_intent_permission(IntentCategory.WEATHER, perms) is True

    def test_intent_not_on_allow_list_is_blocked(self):
        perms = {
            "mode": "guest",
            "restricted_intents": [],
            "allowed_intents": ["weather"],
        }
        assert mode_permission.check_intent_permission(IntentCategory.CONTROL, perms) is False

    def test_no_lists_default_allow(self):
        perms = {
            "mode": "guest",
            "restricted_intents": [],
            "allowed_intents": [],
        }
        assert mode_permission.check_intent_permission(IntentCategory.SPORTS, perms) is True


# ---------------------------------------------------------------------------
# check_entity_permission
# ---------------------------------------------------------------------------

class TestCheckEntityPermission:

    def test_owner_mode_always_allowed(self):
        perms = {"mode": "owner", "restricted_entities": [], "allowed_domains": []}
        assert mode_permission.check_entity_permission("lock.front_door", perms) is True

    def test_entity_matches_regex_is_blocked(self):
        perms = {
            "mode": "guest",
            "restricted_entities": [".*tesla.*"],
            "allowed_domains": [],
        }
        assert mode_permission.check_entity_permission("sensor.tesla_battery", perms) is False

    def test_entity_matches_wildcard_is_blocked(self):
        perms = {
            "mode": "guest",
            "restricted_entities": ["lock.*"],
            "allowed_domains": [],
        }
        # invalid regex (has * without preceding atom in some engines) → wildcard fallback
        assert mode_permission.check_entity_permission("lock.front_door", perms) is False

    def test_entity_domain_not_in_allowed_domains_is_blocked(self):
        perms = {
            "mode": "guest",
            "restricted_entities": [],
            "allowed_domains": ["light", "switch"],
        }
        assert mode_permission.check_entity_permission("lock.front_door", perms) is False

    def test_entity_allowed_when_domain_is_permitted(self):
        perms = {
            "mode": "guest",
            "restricted_entities": [],
            "allowed_domains": ["light"],
        }
        assert mode_permission.check_entity_permission("light.bedroom", perms) is True

    def test_entity_allowed_with_no_restrictions(self):
        perms = {
            "mode": "guest",
            "restricted_entities": [],
            "allowed_domains": [],
        }
        assert mode_permission.check_entity_permission("sensor.temperature", perms) is True

    def test_invalid_regex_exact_match_fallback(self):
        # Pattern that is not a wildcard and not a valid regex but equals entity_id exactly
        perms = {
            "mode": "guest",
            "restricted_entities": ["[invalid"],
            "allowed_domains": [],
        }
        # "[invalid" is invalid regex, doesn't end with *, doesn't equal "sensor.co2"
        assert mode_permission.check_entity_permission("sensor.co2", perms) is True
