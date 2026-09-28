"""Unit tests for orchestrator.mode_permission.handle_owner_mode_utterance
(ATHENA-69 D16/D24 — the owner-PIN voice/utterance trust gate).

 - test_owner_override_refused_for_untrusted_surface (parametrized None,
   "web_public"); named member `web_public`.
 - test_owner_override_allowed_for_trusted_tiers (parametrized over
   PIN_TRUSTED_TIERS).
 - test_anonymous_attempts_do_not_consume_household_throttle
 - test_throttle_keyed_by_tier_not_session_or_room
 - test_throttle_signature_has_no_caller_controlled_key
 - test_non_owner_mode_utterance_returns_none

Drives the real `handle_owner_mode_utterance` with a fake mode_client
installed via `_runtime.set_mode_client`, and resets the D16 per-tier
throttle + D33 pin_authority cache between tests (both in-process state).
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import pytest

from orchestrator import mode_permission
from orchestrator.nodes import _runtime


def _run(coro):
    return asyncio.run(coro)


def _make_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _install_fake_mode_client(*, override_response=None):
    """A fake mode_client: GET /health always reports pin_authority=admin
    (D33 passes trivially -- this file drives the D16/D24 trust gate, not
    D33); POST /mode/override returns `override_response` (default: a
    successful 200)."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(200, {"pin_authority": "admin"}))
    client.post = AsyncMock(
        return_value=override_response or _make_response(200, {"message": "Owner mode active.", "expires_at": "later"})
    )
    _runtime.set_mode_client(client)
    return client


@pytest.fixture(autouse=True)
def _reset_process_state():
    _runtime.reset_for_test()
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()
    yield
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()


class TestOwnerOverrideRefusedForUntrustedSurface:
    @pytest.mark.parametrize("caller_trust", [None, "web_public"], ids=["none", "web_public"])
    def test_owner_override_refused_for_untrusted_surface(self, caller_trust):
        client = _install_fake_mode_client()

        outcome = _run(mode_permission.handle_owner_mode_utterance(
            "switch to owner mode pin 123456", caller_trust, "living_room"
        ))

        assert outcome is not None
        assert outcome.success is False
        assert outcome.message == "Owner mode isn't available from here."
        assert outcome.refused_reason == "untrusted_surface"
        client.get.assert_not_awaited()
        client.post.assert_not_awaited()

    def test_named_member_web_public(self):
        client = _install_fake_mode_client()
        outcome = _run(mode_permission.handle_owner_mode_utterance(
            "switch to owner mode pin 123456", "web_public", None
        ))
        assert outcome.refused_reason == "untrusted_surface"
        client.post.assert_not_awaited()

    def test_untrusted_surface_leaves_throttle_counts_unchanged(self):
        client = _install_fake_mode_client()
        throttle = mode_permission._owner_override_throttle

        for _ in range(5):
            _run(mode_permission.handle_owner_mode_utterance("switch to owner mode pin 123456", "web_public", None))

        # The untrusted-surface refusal must never touch the throttle at all.
        assert throttle._attempts.get("web_public", []) == []
        assert throttle._attempts.get(None, []) == []


class TestOwnerOverrideAllowedForTrustedTiers:
    @pytest.mark.parametrize("tier", sorted(mode_permission.PIN_TRUSTED_TIERS))
    def test_owner_override_allowed_for_trusted_tiers(self, tier):
        client = _install_fake_mode_client()

        outcome = _run(mode_permission.handle_owner_mode_utterance(
            "switch to owner mode pin 123456", tier, "kitchen"
        ))

        assert outcome is not None
        assert outcome.success is True
        client.post.assert_awaited_once()
        _, kwargs = client.post.call_args
        assert kwargs["json"]["caller_tier"] == tier

    def test_population_set_equal_to_pin_trusted_tiers(self):
        assert set(mode_permission.PIN_TRUSTED_TIERS) == {"household", "sms", "web_authenticated"}


class TestAnonymousAttemptsDoNotConsumeHouseholdThrottle:
    def test_anonymous_attempts_do_not_consume_household_throttle(self):
        client = _install_fake_mode_client()

        for _ in range(50):
            _run(mode_permission.handle_owner_mode_utterance("switch to owner mode pin 123456", "web_public", None))
        client.post.assert_not_awaited()

        outcome = _run(mode_permission.handle_owner_mode_utterance("switch to owner mode pin 123456", "household", None))
        assert outcome.success is True
        client.post.assert_awaited_once()


class TestThrottleKeyedByTierNotSessionOrRoom:
    def test_throttle_keyed_by_tier_not_session_or_room(self):
        client = _install_fake_mode_client()

        for i in range(10):
            outcome = _run(mode_permission.handle_owner_mode_utterance(
                "switch to owner mode pin 123456", "household", f"room-{i}"
            ))
            assert outcome.success is True
        assert client.post.await_count == 10

        eleventh = _run(mode_permission.handle_owner_mode_utterance(
            "switch to owner mode pin 123456", "household", "room-11"
        ))
        assert eleventh.success is False
        assert eleventh.refused_reason == "throttled"
        assert client.post.await_count == 10  # no new call

        sms_outcome = _run(mode_permission.handle_owner_mode_utterance("switch to owner mode pin 123456", "sms", "room-x"))
        assert sms_outcome.success is True
        assert client.post.await_count == 11


class TestThrottleSignatureHasNoCallerControlledKey:
    def test_throttle_signature_has_no_caller_controlled_key(self):
        params = inspect.signature(mode_permission.OwnerOverrideThrottle.check).parameters
        names = [p for p in params if p != "self"]
        assert names == ["tier"]


class TestNonOwnerModeUtteranceReturnsNone:
    def test_non_owner_mode_utterance_returns_none(self):
        _install_fake_mode_client()
        outcome = _run(mode_permission.handle_owner_mode_utterance(
            "what's the weather today", "household", None
        ))
        assert outcome is None
