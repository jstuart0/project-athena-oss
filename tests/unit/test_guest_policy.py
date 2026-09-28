"""Unit tests for shared.guest_policy (ATHENA-69 D8/D22).

Covers apply_guest_baseline, guest_baseline, and parse_json_array_env:

 1.  test_empty_admin_lists_get_baseline — guest with [] intents/domains/
     entities -> baseline allowlists and full floor.
 2.  test_floor_unioned_with_configured — floor first, configured appended,
     deduped.
 3.  test_idempotent
 4.  test_non_guest_untouched — owner and degraded dicts unchanged.
 5.  test_scene_in_default_floor — named member: ^scene\\. present.
 6.  "test_invalid_json_env_uses_default_not_empty" is, despite the
     singular name inherited from the original plan item, actually FOUR
     separate test methods on TestParseJsonArrayEnvInvalidUsesDefaultNotEmpty
     below -- one per JSON field (the 3 guest_policy fields, plus
     mode_permission's ha_permission_fallback_restricted_entities, which
     shares the same parse_json_array_env helper):
     test_restricted_entities_invalid_json_uses_default,
     test_allowed_intents_invalid_json_uses_default,
     test_allowed_domains_invalid_json_uses_default,
     test_ha_permission_fallback_restricted_entities_invalid_json_uses_default.
 7.  TestAllowedDomainsEmptyEnvTreatedAsUnset (Pass H, codex full-diff,
     Low) — GUEST_BASELINE_ALLOWED_DOMAINS is the one baseline env var
     where an explicit "[]" is treated the same as unset, unlike the other
     two (see class docstring for why).

Patching strategy: AthenaConfig fields are set via monkeypatch.setenv +
shared.config._clear_cache_for_tests(), matching the pattern in
tests/unit/test_config.py.
"""
from __future__ import annotations

import sys
import unittest.mock as mock

# Stub heavy deps before any orchestrator import (mode_permission pulls in
# the orchestrator.nodes package for the D4 outage-test cross-check below).
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import pytest

from shared import config as config_module
from shared import guest_policy


@pytest.fixture(autouse=True)
def _clear_config_cache():
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


class TestApplyGuestBaseline:
    def test_empty_admin_lists_get_baseline(self):
        result = guest_policy.apply_guest_baseline({
            "mode": "guest",
            "allowed_intents": [],
            "allowed_domains": [],
            "restricted_entities": [],
        })
        assert result["allowed_intents"] == guest_policy.GUEST_BASELINE_ALLOWED_INTENTS_DEFAULT
        assert result["allowed_domains"] == guest_policy.GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT
        assert result["restricted_entities"] == guest_policy.GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT

    def test_floor_unioned_with_configured(self):
        result = guest_policy.apply_guest_baseline({
            "mode": "guest",
            "restricted_entities": [r"^sensor\.tesla"],
        })
        # Floor first, configured appended, deduped.
        assert result["restricted_entities"][: len(guest_policy.GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT)] == (
            guest_policy.GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT
        )
        assert result["restricted_entities"][-1] == r"^sensor\.tesla"
        assert result["restricted_entities"].count(r"^lock\.") == 1

    def test_floor_pattern_already_configured_is_deduped(self):
        result = guest_policy.apply_guest_baseline({
            "mode": "guest",
            "restricted_entities": [r"^lock\.", r"^cover\."],
        })
        assert result["restricted_entities"].count(r"^lock\.") == 1
        assert result["restricted_entities"].count(r"^cover\.") == 1

    def test_idempotent(self):
        once = guest_policy.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^custom\."]})
        twice = guest_policy.apply_guest_baseline(once)
        assert once == twice

    def test_non_guest_untouched(self):
        owner_perms = {"mode": "owner", "allowed_intents": [], "allowed_domains": []}
        assert guest_policy.apply_guest_baseline(owner_perms) == owner_perms

        degraded_perms = {"mode": "degraded", "restricted_entities": [r"^lock\."]}
        assert guest_policy.apply_guest_baseline(degraded_perms) == degraded_perms

    def test_scene_in_default_floor(self):
        assert r"^scene\." in guest_policy.GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT

    def test_non_dict_input_returns_empty_dict(self):
        assert guest_policy.apply_guest_baseline(None) == {}  # type: ignore[arg-type]

    def test_configured_restricted_intents_preserved(self):
        result = guest_policy.apply_guest_baseline({"mode": "guest", "restricted_intents": ["tesla"]})
        assert result["restricted_intents"] == ["tesla"]


class TestGuestBaseline:
    def test_guest_baseline_shape(self):
        baseline = guest_policy.guest_baseline()
        assert baseline["mode"] == "guest"
        assert baseline["allowed_intents"] == guest_policy.GUEST_BASELINE_ALLOWED_INTENTS_DEFAULT
        assert baseline["allowed_domains"] == guest_policy.GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT
        assert baseline["restricted_entities"] == guest_policy.GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT


class TestParseJsonArrayEnvInvalidUsesDefaultNotEmpty:
    """test_invalid_json_env_uses_default_not_empty — for all four JSON
    fields: the 3 guest_policy baseline fields, plus mode_permission's
    ha_permission_fallback_restricted_entities (D4), which shares this
    same parse_json_array_env helper."""

    def test_restricted_entities_invalid_json_uses_default(self, monkeypatch):
        monkeypatch.setenv("GUEST_BASELINE_RESTRICTED_ENTITIES", "not json")
        config_module._clear_cache_for_tests()
        result = guest_policy.apply_guest_baseline({"mode": "guest"})
        assert result["restricted_entities"] == guest_policy.GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT

    def test_allowed_intents_invalid_json_uses_default(self, monkeypatch):
        monkeypatch.setenv("GUEST_BASELINE_ALLOWED_INTENTS", "{not a list}")
        config_module._clear_cache_for_tests()
        result = guest_policy.apply_guest_baseline({"mode": "guest"})
        assert result["allowed_intents"] == guest_policy.GUEST_BASELINE_ALLOWED_INTENTS_DEFAULT

    def test_allowed_domains_invalid_json_uses_default(self, monkeypatch):
        monkeypatch.setenv("GUEST_BASELINE_ALLOWED_DOMAINS", '["light", 5]')
        config_module._clear_cache_for_tests()
        result = guest_policy.apply_guest_baseline({"mode": "guest"})
        assert result["allowed_domains"] == guest_policy.GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT

    def test_ha_permission_fallback_restricted_entities_invalid_json_uses_default(self, monkeypatch):
        monkeypatch.setenv("HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES", "garbage")
        config_module._clear_cache_for_tests()
        from orchestrator import mode_permission

        result = mode_permission.degraded_permissions()
        assert result["restricted_entities"] == [
            r"^lock\.", r"^cover\.", r"^alarm_control_panel\.", r"^camera\.",
            r"^automation\.", r"^script\.", r"^scene\.",
        ]

    def test_explicit_empty_array_is_honoured_not_default(self, monkeypatch):
        """parse_json_array_env's own contract: an explicit "[]" is a
        deliberate opt-out, distinct from an unset/invalid value falling
        back to the default. This is the low-level primitive's contract
        and is unchanged by Pass H -- see TestAllowedDomainsEmptyEnvTreatedAsUnset
        below for why GUEST_BASELINE_ALLOWED_DOMAINS specifically doesn't
        pass an explicit "[]" through to this primitive at all anymore."""
        monkeypatch.setenv("GUEST_BASELINE_ALLOWED_DOMAINS", "[]")
        config_module._clear_cache_for_tests()
        assert guest_policy.parse_json_array_env("[]", ["default"]) == []
        assert guest_policy.parse_json_array_env("", ["default"]) == ["default"]
        assert guest_policy.parse_json_array_env(None, ["default"]) == ["default"]
        assert guest_policy.parse_json_array_env("not json", ["default"]) == ["default"]


class TestAllowedDomainsEmptyEnvTreatedAsUnset:
    """ATHENA-69 Pass H (codex full-diff, Low): GUEST_BASELINE_ALLOWED_DOMAINS
    is the one baseline env var where an explicit "[]" has no safe meaning
    downstream -- check_entity_permission's `if allowed_domains and ...`
    check would skip domain filtering entirely for an empty list and allow
    every domain, the opposite of what a deployer setting it to "[]" would
    intend. Unlike GUEST_BASELINE_RESTRICTED_ENTITIES (floor-disabling is a
    legitimate, logged opt-out) and GUEST_BASELINE_ALLOWED_INTENTS (an
    empty allow-list already has the safe deny-by-default meaning), an
    explicit "[]" for allowed_domains is now treated the same as unset."""

    def test_baseline_allowed_domains_explicit_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("GUEST_BASELINE_ALLOWED_DOMAINS", "[]")
        config_module._clear_cache_for_tests()
        assert guest_policy.baseline_allowed_domains() == guest_policy.GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT

    def test_apply_guest_baseline_explicit_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("GUEST_BASELINE_ALLOWED_DOMAINS", "[]")
        config_module._clear_cache_for_tests()
        result = guest_policy.apply_guest_baseline({"mode": "guest"})
        assert result["allowed_domains"] == guest_policy.GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT

    def test_baseline_allowed_domains_normal_override_still_honoured(self, monkeypatch):
        """A non-empty override is unaffected by the empty-env carve-out."""
        monkeypatch.setenv("GUEST_BASELINE_ALLOWED_DOMAINS", '["light", "fan"]')
        config_module._clear_cache_for_tests()
        assert guest_policy.baseline_allowed_domains() == ["light", "fan"]
