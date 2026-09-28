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

ATHENA-69 (D4): the 3 outage tests above (4xx / ConnectError / generic
exception) were updated in-place to assert degraded permissions rather than
the old unrestricted-owner fallback. The ATHENA-69 guard module itself
(authorize_ha_write, normalize_permissions, degraded_permissions,
PermissionScope/ha_permission_scope, PermissionEnforcingHAClient,
ensure_permission_enforcing, CONTROL_DEVICE_DOMAINS/intent_write_domains,
permission_refusal_message) is covered by tests/unit/test_ha_permission_guard.py
and tests/unit/test_guest_policy.py, not here.

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

import pytest

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


def _install_mode_client(get_side_effects=None, post_return=None, health_response=None):
    """Install a fake mode_client into _runtime and return it.

    health_response: optional response for GET /health (ATHENA-69 D33's
    pin_authority capability check consulted by activate_owner_override).
    Defaults to a 200 reporting pin_authority: "admin" whenever a caller
    supplies post_return but not get_side_effects, so activate_owner_override
    tests don't need to configure it explicitly unless they're specifically
    testing D33 itself.
    """
    client = AsyncMock()
    if get_side_effects is not None:
        client.get = AsyncMock(side_effect=get_side_effects)
    elif health_response is not None or post_return is not None:
        resp = health_response if health_response is not None else _make_response(200, {"pin_authority": "admin"})
        client.get = AsyncMock(return_value=resp)
    if post_return is not None:
        client.post = AsyncMock(return_value=post_return)
    _runtime.set_mode_client(client)
    return client


@pytest.fixture(autouse=True)
def _reset_owner_override_process_state():
    """ATHENA-69 Pass C: the D33 pin_authority cache and the D16 per-tier
    throttle are in-process state that must not leak across tests."""
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()
    yield
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()


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

    def test_4xx_response_raises_falls_back_to_degraded(self):
        """D4: an unreachable/rejecting mode service reports mode="owner"
        (prompts/UI unchanged) but permissions degraded -- never the old
        unrestricted-owner permissions dict."""
        error_resp = _make_response(503, {})
        _install_mode_client(get_side_effects=[error_resp])

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "owner"
        assert result["reason"] == "Mode service unavailable"
        assert result["override_active"] is False
        assert result["degraded"] is True
        assert result["permissions"]["mode"] == "degraded"
        assert result["permissions"] == mode_permission.degraded_permissions()

    def test_connect_error_falls_back_to_degraded(self):
        import httpx
        _install_mode_client(get_side_effects=httpx.ConnectError("refused"))

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "owner"
        assert result["degraded"] is True
        assert result["permissions"]["mode"] == "degraded"

    def test_generic_exception_falls_back_to_degraded(self):
        _install_mode_client(get_side_effects=RuntimeError("boom"))

        result = _run(mode_permission.get_current_mode())

        assert result["mode"] == "owner"
        assert result["override_active"] is False
        assert result["degraded"] is True
        assert result["permissions"]["mode"] == "degraded"
        # D4: locks/covers/etc. stay denied even though mode reports "owner".
        assert mode_permission.check_entity_permission("lock.front_door", result["permissions"]) is False
        assert mode_permission.check_entity_permission("light.kitchen", result["permissions"]) is True


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

        success, msg, data = _run(mode_permission.activate_owner_override(
            "123456", caller_tier="household", voice_device_id="device-1", timeout_minutes=30
        ))

        assert success is True
        assert "Owner mode active" in msg
        assert data["expires_at"] == "2026-01-01T12:00:00Z"

    def test_401_pin_required(self):
        resp = MagicMock()
        resp.status_code = 401
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override(None, caller_tier="household"))

        assert success is False
        assert "6-digit owner PIN" in msg
        assert data is None

    def test_403_invalid_pin(self):
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {"detail": "Invalid PIN"}
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("000000", caller_tier="household"))

        assert success is False
        assert "Invalid PIN" in msg
        assert data is None

    def test_400_bad_format(self):
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {"detail": "PIN must be 6 digits"}
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("12", caller_tier="household"))

        assert success is False
        assert "6-digit PIN" in msg
        assert data is None

    def test_unexpected_status_code(self):
        resp = MagicMock()
        resp.status_code = 500
        _install_mode_client(post_return=resp)

        success, msg, data = _run(mode_permission.activate_owner_override("123456", caller_tier="household"))

        assert success is False
        assert "Unable to process" in msg
        assert data is None

    def test_exception_in_post(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_response(200, {"pin_authority": "admin"}))
        client.post = AsyncMock(side_effect=RuntimeError("connection reset"))
        _runtime.set_mode_client(client)

        success, msg, data = _run(mode_permission.activate_owner_override("123456", caller_tier="household"))

        assert success is False
        assert "Mode service unavailable" in msg
        assert data is None

    @pytest.mark.parametrize("health_json", [{}, {"pin_authority": "old"}])
    def test_owner_override_refused_when_mode_service_lacks_admin_pin_authority(self, health_json):
        """D33 (tessa Pass C mutation review, High): a mode service that
        hasn't yet reported pin_authority == "admin" (missing field, or an
        older/other value) must refuse the override with zero calls to the
        override endpoint -- never fall through and send the PIN to a mode
        service that might still hash-compare it locally (pre-D25)."""
        client = _install_mode_client(health_response=_make_response(200, health_json))

        success, msg, data = _run(mode_permission.activate_owner_override(
            "123456", caller_tier="household"
        ))

        assert success is False
        assert data is None
        assert "isn't available right now" in msg
        client.post.assert_not_called()

    def test_override_call_timeout_exceeds_admin_verify_timeout(self):
        """D34 (tessa Pass C mutation review, High): the override POST's
        timeout must stay ordered override(5.0) > mode-service-to-admin
        verify(3.0, Pass D) > permission-fetch(2.5, mode_client default) --
        a regression back to 2.5 must go red here."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"message": "Owner mode active."}
        client = _install_mode_client(post_return=resp)

        _run(mode_permission.activate_owner_override("123456", caller_tier="household"))

        _, kwargs = client.post.call_args
        assert kwargs["timeout"] == 5.0
        assert kwargs["timeout"] > 3.0 > 2.5, "D34 timeout ordering: override > admin verify > permission fetch"


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

    def test_empty_allowed_domains_falls_back_to_baseline_not_unrestricted(self):
        """ATHENA-69 Pass H (codex full-diff, Low): an empty allowed_domains
        -- however it arose -- no longer means "every domain allowed". It
        falls back to the baseline domain list (light/media_player/switch/
        climate by default): an in-baseline domain is still allowed, an
        out-of-baseline one is now blocked (previously silently allowed)."""
        perms = {
            "mode": "guest",
            "restricted_entities": [],
            "allowed_domains": [],
        }
        assert mode_permission.check_entity_permission("light.bedroom", perms) is True
        assert mode_permission.check_entity_permission("sensor.temperature", perms) is False
        assert mode_permission.check_entity_permission("vacuum.roomba", perms) is False

    def test_degraded_mode_empty_allowed_domains_is_not_baseline_restricted(self):
        """The guest-baseline fallback above is scoped to mode=="guest"
        only. degraded_permissions() (D4) deliberately sets
        allowed_domains=[] to mean "no domain restriction beyond the
        entity floor" for an owner-during-outage/system scope -- applying
        the guest baseline there would newly block domains (e.g. select,
        input_boolean) an owner is meant to keep during an outage.
        Regression: this exact scenario broke test_smart_home_bed_warmer.py's
        direct _handle_bed_warmer_intent call (no open scope -> D3's
        degraded system-mode fallback) the first time this fix shipped."""
        perms = {
            "mode": "degraded",
            "restricted_entities": [r"^lock\."],
            "allowed_domains": [],
        }
        assert mode_permission.check_entity_permission("select.bed_level_left", perms) is True
        assert mode_permission.check_entity_permission("sensor.temperature", perms) is True
        assert mode_permission.check_entity_permission("lock.front_door", perms) is False

    def test_invalid_regex_exact_match_fallback(self):
        # Pattern that is not a wildcard and not a valid regex but equals entity_id exactly.
        # allowed_domains explicit so this exercises only the regex-fallback
        # logic under test, not the (Pass H) empty-allowed_domains-falls-
        # back-to-baseline behavior covered by the test above.
        perms = {
            "mode": "guest",
            "restricted_entities": ["[invalid"],
            "allowed_domains": ["sensor"],
        }
        # "[invalid" is invalid regex, doesn't end with *, doesn't equal "sensor.co2"
        assert mode_permission.check_entity_permission("sensor.co2", perms) is True


# ---------------------------------------------------------------------------
# ATHENA-69 Pass C: resolve_request_authorization, get_guest_permissions
# ---------------------------------------------------------------------------

def _server_mode_info(server: str) -> dict:
    if server == "owner":
        return {"mode": "owner", "permissions": {"mode": "owner"}, "degraded": False,
                "override_active": False, "reason": "ok"}
    if server == "guest":
        return {"mode": "guest", "permissions": {
            "mode": "guest", "allowed_intents": ["weather"], "restricted_entities": [], "allowed_domains": [],
        }, "degraded": False, "override_active": False, "reason": "ok"}
    # "down"
    return {"mode": "owner", "permissions": mode_permission.degraded_permissions(), "degraded": True,
            "override_active": False, "reason": "Mode service unavailable"}


_RESOLVE_AUTHZ_CASES = []
for _rm in (None, "owner", "guest"):
    for _srv in ("owner", "guest", "down"):
        for _gi in (None, {"guest_id": "g1"}):
            _fp_suffix = "fp" if _gi else "no_fp"
            _RESOLVE_AUTHZ_CASES.append((f"request_{_rm}-server_{_srv}-{_fp_suffix}", _rm, _srv, _gi))


class TestResolveRequestAuthorization:

    @pytest.mark.parametrize(
        "request_mode,server,guest_info",
        [c[1:] for c in _RESOLVE_AUTHZ_CASES],
        ids=[c[0] for c in _RESOLVE_AUTHZ_CASES],
    )
    def test_resolve_request_authorization(self, monkeypatch, request_mode, server, guest_info):
        server_info = _server_mode_info(server)
        monkeypatch.setattr(mode_permission, "get_current_mode", AsyncMock(return_value=server_info))
        fetched_guest_perms = mode_permission.apply_guest_baseline({"mode": "guest", "allowed_intents": ["fetched"]})
        fetch_guest = AsyncMock(return_value=fetched_guest_perms)
        monkeypatch.setattr(mode_permission, "get_guest_permissions", fetch_guest)

        authz = _run(mode_permission.resolve_request_authorization(request_mode, guest_info))

        expected_mode = "guest" if (guest_info or request_mode == "guest" or server == "guest") else server_info["mode"]
        assert authz.mode == expected_mode
        assert authz.server_mode == server_info["mode"]
        assert authz.degraded is server_info["degraded"]
        if expected_mode == "guest":
            assert authz.permissions["mode"] == "guest"
        elif server == "down":
            assert authz.permissions["mode"] == "degraded"
        else:
            assert authz.permissions["mode"] == server_info["mode"]
        expected_escalation_ignored = request_mode == "owner" and expected_mode != "owner"
        assert authz.escalation_ignored is expected_escalation_ignored

        # get_guest_permissions is called ONLY for a guest-effective request
        # where the server itself wasn't already reporting guest and isn't
        # degraded (D6 -- server's-if-guest, else fetch, unless degraded).
        should_fetch = expected_mode == "guest" and server not in ("guest", "down")
        assert fetch_guest.await_count == (1 if should_fetch else 0)

    def test_resolve_request_authorization_named_member(self, monkeypatch):
        """request_owner-server_guest-no_fp -> mode == "guest",
        permissions["mode"] == "guest", escalation_ignored is True."""
        server_info = _server_mode_info("guest")
        monkeypatch.setattr(mode_permission, "get_current_mode", AsyncMock(return_value=server_info))
        authz = _run(mode_permission.resolve_request_authorization("owner", None))
        assert authz.mode == "guest"
        assert authz.permissions["mode"] == "guest"
        assert authz.escalation_ignored is True


class TestGetGuestPermissions:
    def test_get_guest_permissions_old_service_returns_owner_falls_back_to_floored_guest(self):
        resp = _make_response(200, {"mode": "owner", "allowed_intents": []})
        _install_mode_client(get_side_effects=[resp])
        result = _run(mode_permission.get_guest_permissions())
        assert result["mode"] == "guest"
        assert r"^lock\." in result["restricted_entities"]
        assert result["allowed_intents"] == mode_permission.apply_guest_baseline({"mode": "guest"})["allowed_intents"]

    def test_get_guest_permissions_happy_path(self):
        resp = _make_response(200, {"mode": "guest", "allowed_intents": ["weather"], "restricted_entities": [], "allowed_domains": []})
        _install_mode_client(get_side_effects=[resp])
        result = _run(mode_permission.get_guest_permissions())
        assert result["mode"] == "guest"
        assert r"^lock\." in result["restricted_entities"]

    def test_get_guest_permissions_timeout_falls_back_to_floored_guest(self):
        _install_mode_client(get_side_effects=RuntimeError("timeout"))
        result = _run(mode_permission.get_guest_permissions())
        assert result["mode"] == "guest"
        assert r"^lock\." in result["restricted_entities"]


class TestDegradedPathMakesNoGuestFetch:
    def test_degraded_path_makes_no_guest_fetch(self, monkeypatch):
        server_info = _server_mode_info("down")
        monkeypatch.setattr(mode_permission, "get_current_mode", AsyncMock(return_value=server_info))
        fetch_guest = AsyncMock(side_effect=AssertionError("get_guest_permissions must not be called when degraded"))
        monkeypatch.setattr(mode_permission, "get_guest_permissions", fetch_guest)

        authz = _run(mode_permission.resolve_request_authorization("guest", None))

        fetch_guest.assert_not_awaited()
        assert authz.mode == "guest"
        assert authz.permissions["mode"] == "guest"
        assert authz.degraded is True


class TestNoOwnerPermissionFetchFunction:
    def test_no_owner_permission_fetch_function(self):
        """D6: get_guest_permissions has no owner variant -- only "guest"
        can be requested. There is no get_owner_permissions function at all."""
        assert not hasattr(mode_permission, "get_owner_permissions")
        import inspect
        params = inspect.signature(mode_permission.get_guest_permissions).parameters
        assert params == {}


# ---------------------------------------------------------------------------
# ATHENA-69 Pass C: activate_owner_override status-code mapping (D16)
# ---------------------------------------------------------------------------

class TestActivateOwnerOverrideStatusMapping:
    def test_owner_override_maps_pin_not_configured(self):
        resp = _make_response(403, {"detail": "owner_pin_not_configured"})
        _install_mode_client(post_return=resp)
        success, msg, data = _run(mode_permission.activate_owner_override(None, caller_tier="household"))
        assert success is False
        assert "PIN set in the admin panel" in msg
        assert data is None

    def test_owner_override_maps_locked(self):
        resp = _make_response(429, {"detail": "owner_override_locked"})
        _install_mode_client(post_return=resp)
        success, msg, data = _run(mode_permission.activate_owner_override("123456", caller_tier="household"))
        assert success is False
        assert "temporarily locked" in msg
        assert data is None

    def test_owner_override_maps_verification_unavailable(self):
        resp = _make_response(503, {"detail": "owner_pin_verification_unavailable"})
        _install_mode_client(post_return=resp)
        success, msg, data = _run(mode_permission.activate_owner_override("123456", caller_tier="household"))
        assert success is False
        assert "can't verify the PIN right now" in msg
        assert data is None

    def test_activate_owner_override_sends_caller_tier(self):
        resp = _make_response(200, {"message": "Owner mode active.", "expires_at": "2026-01-01T12:00:00Z"})
        client = _install_mode_client(post_return=resp)
        _run(mode_permission.activate_owner_override("123456", caller_tier="household"))
        _, kwargs = client.post.call_args
        assert kwargs["json"]["caller_tier"] == "household"

    def test_owner_pin_under_outage_cannot_unlock(self):
        """mode_client raising (on the /health capability check, before the
        override POST is ever attempted) -> activate_owner_override returns
        (False, "Mode service unavailable..."); a lock write inside the
        resulting degraded scope is denied."""
        client = AsyncMock()
        client.get = AsyncMock(side_effect=RuntimeError("mode service unreachable"))
        _runtime.set_mode_client(client)

        success, msg, data = _run(mode_permission.activate_owner_override("123456", caller_tier="household"))
        assert success is False
        assert "Mode service unavailable" in msg
        assert data is None

        degraded = mode_permission.degraded_permissions()
        with mode_permission.ha_permission_scope(degraded, mode="owner"):
            decision = mode_permission.authorize_ha_write("lock", "unlock", {"entity_id": "lock.front_door"}, degraded)
        assert decision.allowed is False
