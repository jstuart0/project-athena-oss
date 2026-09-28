"""shared.guest_policy — the floor/baseline merge rules for guest permissions.

Used by both the mode service (``src/mode_service/main.py``) and the
orchestrator (``src/orchestrator/mode_permission.py``) so the two services
apply an identical guest floor even when the mode service's admin-backed
config differs from the orchestrator's own degraded-guest fallback
(ATHENA-69 D6/D8/D22).

``apply_guest_baseline`` is pure and idempotent: calling it twice on its own
output returns the same dict. It only acts on ``permissions["mode"] ==
"guest"`` -- any other mode is returned as an unmodified copy, never
upgraded or downgraded.

An empty (or absent) admin-configured list means "use the baseline", never
"no restriction" -- an empty ``guest_allowed_intents``/``guest_allowed_domains``
must not silently mean "allow everything" for a guest. ``restricted_entities``
is always the floor patterns unioned with whatever the deployer configured;
there is no way to configure a guest floor away, only add to it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from shared.config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Baseline defaults (D8, D22). These are the built-in floor/allowlists used
# whenever the corresponding AthenaConfig JSON-array env var is unset (empty
# string) or fails to parse. For GUEST_BASELINE_RESTRICTED_ENTITIES and
# GUEST_BASELINE_ALLOWED_INTENTS, an explicit "[]" from the deployer is a
# valid, intentional opt-out and is honoured as an empty list -- only an
# unset or malformed value falls back to these defaults.
# GUEST_BASELINE_ALLOWED_DOMAINS is the one exception (Pass H, codex
# full-diff, Low): an empty allowed_domains list has no safe meaning
# downstream (check_entity_permission's `if allowed_domains and ...` check
# would skip domain filtering entirely and allow every domain -- the
# opposite of what setting it to "[]" is meant to express), so
# _baseline_allowed_domains treats an explicit "[]" there the same as
# unset. There is no way to configure allowed_domains down to zero
# domains via this env var.
# ---------------------------------------------------------------------------

GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT: List[str] = [
    r"^lock\.",
    r"^cover\.",
    r"^alarm_control_panel\.",
    r"^camera\.",
    r"^automation\.",
    r"^script\.",
    r"^scene\.",
]

GUEST_BASELINE_ALLOWED_INTENTS_DEFAULT: List[str] = [
    "weather",
    "time",
    "general_info",
    "news",
    "recipes",
    "streaming",
]

GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT: List[str] = [
    "light",
    "media_player",
    "switch",
    "climate",
]


def parse_json_array_env(raw: Optional[str], default: List[str]) -> List[str]:
    """Parse a JSON-array-of-strings env value, never returning ``[]`` on error.

    - Unset / empty string -> ``default`` (the built-in baseline).
    - Valid JSON array of strings (including ``"[]"``) -> that list, honoured
      as-is -- this is how a deployer opts out of a baseline entirely.
    - Anything else (invalid JSON, wrong shape, non-string elements) ->
      ``default``, logged once per call site as a warning. Never ``[]`` for
      a malformed value -- that would silently disable the floor.
    """
    if not raw:
        return list(default)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("guest_policy_invalid_json_env", extra={"raw": raw[:200]})
        return list(default)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        logger.warning("guest_policy_invalid_json_shape", extra={"raw": raw[:200]})
        return list(default)
    return parsed


def _floor_restricted_entities() -> List[str]:
    return parse_json_array_env(
        get_config().guest_baseline_restricted_entities,
        GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT,
    )


def _baseline_allowed_intents() -> List[str]:
    return parse_json_array_env(
        get_config().guest_baseline_allowed_intents,
        GUEST_BASELINE_ALLOWED_INTENTS_DEFAULT,
    )


def _baseline_allowed_domains() -> List[str]:
    parsed = parse_json_array_env(
        get_config().guest_baseline_allowed_domains,
        GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT,
    )
    if not parsed:
        # ATHENA-69 Pass H (codex full-diff, Low): unlike
        # restricted_entities (always unioned, never opt-outable) and
        # allowed_intents (an empty allow-list has the safe deny-by-
        # default meaning "nothing extra allowed"), an empty
        # allowed_domains has NO safe meaning downstream --
        # check_entity_permission's `if allowed_domains and ...` skips the
        # domain check entirely when the list is empty, so a deployer
        # setting GUEST_BASELINE_ALLOWED_DOMAINS=[] intending "no domains
        # beyond the floor" instead got "every domain allowed". Treat an
        # explicit "[]" here the same as unset.
        return list(GUEST_BASELINE_ALLOWED_DOMAINS_DEFAULT)
    return parsed


def baseline_allowed_domains() -> List[str]:
    """Public: the effective baseline allowed-domains list (see
    _baseline_allowed_domains's Pass H note on why an explicit "[]"
    override is treated as unset here but not for the other two baseline
    lists). Used by mode_permission.check_entity_permission as the
    fallback when a permissions dict's own allowed_domains is empty."""
    return _baseline_allowed_domains()


def apply_guest_baseline(permissions: Dict[str, Any]) -> Dict[str, Any]:
    """Union the guest floor into ``permissions`` and fill empty allowlists.

    Pure and idempotent. Only acts when ``permissions.get("mode") ==
    "guest"``; anything else (owner, degraded, or a mode-less dict) is
    returned as an unmodified copy.
    """
    if not isinstance(permissions, dict) or permissions.get("mode") != "guest":
        return dict(permissions) if isinstance(permissions, dict) else {}

    floor = _floor_restricted_entities()
    configured_entities = permissions.get("restricted_entities") or []
    merged_entities: List[str] = list(floor)
    for pattern in configured_entities:
        if pattern not in merged_entities:
            merged_entities.append(pattern)

    configured_intents = permissions.get("allowed_intents") or []
    allowed_intents = list(configured_intents) if configured_intents else _baseline_allowed_intents()

    configured_domains = permissions.get("allowed_domains") or []
    allowed_domains = list(configured_domains) if configured_domains else _baseline_allowed_domains()

    result = dict(permissions)
    result["restricted_entities"] = merged_entities
    result["allowed_intents"] = allowed_intents
    result["allowed_domains"] = allowed_domains
    result["restricted_intents"] = list(permissions.get("restricted_intents") or [])
    return result


def guest_baseline() -> Dict[str, Any]:
    """The guest permissions dict built purely from the baseline defaults.

    Used by the mode service's hardcoded defaults and the orchestrator's
    degraded-guest fallback (D6).
    """
    return apply_guest_baseline({"mode": "guest"})
