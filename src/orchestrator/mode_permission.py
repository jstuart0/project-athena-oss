"""mode_permission — extracted from orchestrator.main during Phase 3.1 (ATHENA-10).

Byte-identical move of 6 mode/permission helpers:
  - get_current_mode         (Pattern 1 — uses _runtime.get_mode_client at call time)
  - detect_owner_mode_command (Pattern 1 PURE)
  - extract_pin_from_query   (Pattern 1 PURE)
  - activate_owner_override  (Pattern 1)
  - check_intent_permission  (Pattern 1)
  - check_entity_permission  (Pattern 2 — accepts permissions dict)

See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md Phase 3.1.

ATHENA-69 (HA write authorization) adds the guard module below the 6
original helpers: ``HAWriteDecision``/``HADenial``/``HAWritePermissionDenied``,
``authorize_ha_write``, ``normalize_permissions``, ``degraded_permissions``,
the per-request ``PermissionScope``/``ha_permission_scope``/
``current_ha_scope`` contextvar machinery, ``PermissionEnforcingHAClient``
(the chokepoint wrapper around ``HomeAssistantClient``),
``ensure_permission_enforcing``, ``CONTROL_DEVICE_DOMAINS``/
``intent_write_domains`` (the coarse per-device-type domain map used for the
node-level pre-check), and ``permission_refusal_message`` plus the
``GUEST_INTENT_REFUSAL``/``DEGRADED_INTENT_REFUSAL`` constants. Pass A lands
the guard with ``call_service`` interception only (read allowlist, no
automation methods); Pass B adds ``authorize_automation_config``,
``authorize_sequence``, and the three automation methods on the guard, then
wires it into lifespan. See
.mozart/plans/active/2026-09-28-deliver-athena-ha-permission-gap.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import structlog

from orchestrator.metrics import ha_write_denied_total, intent_gate_refused_total
from orchestrator.state import IntentCategory
from shared.config import get_config
from shared.knowledge_tiers import KnowledgeAudience
from shared.guest_policy import apply_guest_baseline, baseline_allowed_domains, guest_baseline, parse_json_array_env

logger = structlog.get_logger(__name__)


class _ModeClientProxy:
    """Forward attribute access to the runtime mode client at call time.

    get_current_mode and activate_owner_override reference ``mode_client`` as a
    bare module global.  The actual client is registered in _runtime by
    main.py's lifespan, so we cannot bind it at import time.  This proxy
    resolves the current value on every attribute lookup, keeping the function
    bodies byte-identical.

    ATHENA-69: ``get_mode_client`` is imported lazily inside ``__getattr__``
    rather than at module scope. ``orchestrator.nodes._runtime`` is a
    submodule of the ``orchestrator.nodes`` package, and importing it forces
    ``orchestrator/nodes/__init__.py`` to run first if it hasn't already --
    that ``__init__.py`` imports ``route_control_node``, which imports names
    from this module. Whichever of ``orchestrator.mode_permission`` /
    ``orchestrator.nodes`` is imported *first* in a process determines
    whether that cycle resolves cleanly or raises ImportError on a
    partially-initialized module -- a real, pre-existing hazard this module
    already had before ATHENA-69, now surfaced by tests that import
    ``orchestrator.mode_permission`` directly and in isolation. Deferring
    the import to call time (this proxy already promises "resolves the
    current value on every attribute lookup") removes the module-scope
    forward reference entirely, independent of test collection order.
    """

    def __getattr__(self, name: str):  # type: ignore[override]
        from orchestrator.nodes._runtime import get_mode_client
        return getattr(get_mode_client(), name)


mode_client = _ModeClientProxy()


# Patterns for detecting owner mode commands
OWNER_MODE_PATTERNS = [
    r"\b(switch|change|enable|activate)\s+(to\s+)?owner\s*mode\b",
    r"\bowner\s*mode\b.*\bpin\b",
    r"\bpin\b.*\bowner\s*mode\b",
    r"\bi'?m\s+(the\s+)?owner\b",
    r"\bexit\s+guest\s*mode\b",
    r"\bdeactivate\s+guest\s*mode\b",
    r"\bowner\s+override\b",
]


async def get_current_mode() -> Dict[str, Any]:
    """
    Get current mode from mode service (Phase 2: Guest Mode).

    Fetches mode (guest vs owner) and permission settings from the mode service.
    Falls back to owner mode if service unavailable (safe default).

    Returns:
        Dict with mode, permissions, and metadata
    """
    try:
        response = await mode_client.get("/mode")
        response.raise_for_status()
        mode_data = response.json()

        mode_value = mode_data.get("mode", "owner")
        if mode_value == "degraded":
            # D38 cold start (valerie r1, High): the mode service answered
            # successfully but hasn't completed its first successful
            # admin-config load yet, and reports mode="degraded" -- a
            # value OrchestratorState/QueryRequest never accept (only
            # owner/guest), so letting it through 500s every /query* call
            # from the first request after a mode-service restart until
            # that first load completes. Treat it exactly like the D4
            # outage branch below: never propagate "degraded" as a mode
            # string.
            logger.warning(
                "mode_service_reports_degraded",
                reason=mode_data.get("reason", "Unknown"),
            )
            return {
                "mode": "owner",
                "permissions": degraded_permissions(),
                "override_active": False,
                "degraded": True,
                "reason": mode_data.get("reason", "Mode service cold start"),
            }

        # Get permissions for current mode
        perms_response = await mode_client.get("/mode/permissions")
        perms_response.raise_for_status()
        permissions = perms_response.json()

        logger.info(
            "mode_fetched",
            mode=mode_data.get("mode", "owner"),
            override_active=mode_data.get("override_active", False)
        )

        return {
            "mode": mode_data.get("mode", "owner"),
            "permissions": permissions,
            "override_active": mode_data.get("override_active", False),
            "reason": mode_data.get("reason", "Unknown")
        }
    except Exception as e:
        logger.warning(f"Failed to get mode from mode service: {e}")
        # D4: the mode service is unreachable or rejecting. This is NOT a
        # safe-default-to-owner: an unreachable mode service must not grant
        # unrestricted HA writes. The reported "mode" stays "owner" (so
        # prompts/memory/UI copy are unchanged), but "permissions" is the
        # degraded set -- physical-security domains stay denied even though
        # the household is nominally in owner mode. See
        # degraded_permissions() below and D4 in the ATHENA-69 plan.
        return {
            "mode": "owner",
            "permissions": degraded_permissions(),
            "override_active": False,
            "degraded": True,
            "reason": "Mode service unavailable"
        }


# ============================================================================
# Phase 4: Voice PIN Override Detection and Handling
# ============================================================================

def detect_owner_mode_command(query: str) -> bool:
    """
    Detect if the query is an owner mode command (Phase 4: Voice PIN Override).

    Args:
        query: User query string

    Returns:
        True if query appears to be an owner mode command
    """
    query_lower = query.lower()
    for pattern in OWNER_MODE_PATTERNS:
        if re.search(pattern, query_lower):
            return True
    return False


def extract_pin_from_query(query: str) -> Optional[str]:
    """
    Extract a 6-digit PIN from a query (Phase 4: Voice PIN Override).

    Handles various spoken formats:
    - "pin 123456"
    - "pin one two three four five six"
    - "code 123456"
    - "123456"

    Args:
        query: User query string

    Returns:
        6-digit PIN string or None if not found
    """
    # Word to digit mapping for spoken numbers
    word_to_digit = {
        "zero": "0", "oh": "0", "o": "0",
        "one": "1", "won": "1",
        "two": "2", "to": "2", "too": "2",
        "three": "3", "tree": "3",
        "four": "4", "for": "4", "fore": "4",
        "five": "5",
        "six": "6", "sicks": "6", "sex": "6",
        "seven": "7",
        "eight": "8", "ate": "8",
        "nine": "9", "niner": "9",
    }

    query_lower = query.lower()

    # First try to find numeric digits directly
    # Match 6 consecutive digits
    numeric_match = re.search(r'\b(\d{6})\b', query_lower)
    if numeric_match:
        return numeric_match.group(1)

    # Match 6 digits with spaces between them (e.g., "1 2 3 4 5 6")
    spaced_match = re.search(r'\b(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\b', query_lower)
    if spaced_match:
        return ''.join(spaced_match.groups())

    # Try to find spoken digits after "pin" or "code"
    pin_context = re.search(r'(?:pin|code)\s+(.+)', query_lower)
    if pin_context:
        digit_portion = pin_context.group(1)
        digits = []

        # Split by spaces and convert words to digits
        words = digit_portion.split()
        for word in words:
            # Clean punctuation
            word_clean = re.sub(r'[^\w]', '', word)
            if word_clean.isdigit():
                digits.append(word_clean)
            elif word_clean in word_to_digit:
                digits.append(word_to_digit[word_clean])

            # Stop if we have 6 digits
            if len(digits) >= 6:
                break

        if len(digits) == 6:
            return ''.join(digits)

    return None


# ============================================================================
# ATHENA-69 D33: mixed-version override safety
# ============================================================================
#
# An orchestrator built against Pass D's admin-verified PIN contract must
# never send a PIN to an OLDER mode service that still hashes and compares
# it locally (pre-D25) -- that would silently downgrade to the weaker,
# unsalted-SHA-256 verification path. The mode service's own /health
# (Pass D) reports pin_authority: "admin" once it's running the new
# contract; this is cached in-process for up to 30 s so the capability
# check doesn't add a network round-trip to every override attempt.

_PIN_AUTHORITY_CACHE_SECONDS = 30.0
_pin_authority_cache: Dict[str, Any] = {"checked_at": float("-inf"), "ok": False}


async def _mode_service_supports_admin_pin_authority() -> bool:
    """True iff the mode service's cached /health reports pin_authority ==
    "admin". Raises (does not swallow) on a transport failure -- callers
    that want outage-vs-mixed-version to produce different refusal text
    must catch around this themselves; a mixed-version response (a
    successful /health missing or misreporting pin_authority) returns
    False normally, without raising."""
    now = time.monotonic()
    if now - _pin_authority_cache["checked_at"] < _PIN_AUTHORITY_CACHE_SECONDS:
        return _pin_authority_cache["ok"]
    response = await mode_client.get("/health")
    response.raise_for_status()
    data = response.json()
    ok = data.get("pin_authority") == "admin"
    _pin_authority_cache["checked_at"] = now
    _pin_authority_cache["ok"] = ok
    return ok


def _reset_pin_authority_cache_for_tests() -> None:
    """PRIVATE — test isolation only. Production code never calls this."""
    _pin_authority_cache["checked_at"] = float("-inf")
    _pin_authority_cache["ok"] = False


async def activate_owner_override(
    pin: Optional[str],
    *,
    caller_tier: str,
    voice_device_id: Optional[str] = None,
    timeout_minutes: Optional[int] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Activate owner mode override via mode service (Phase 4: Voice PIN Override;
    ATHENA-69 D16/D33/D34).

    Args:
        pin: 6-digit PIN or None
        caller_tier: the caller's trust tier (D16/D24) -- sent to the mode
            service in the request body; also the D25 lockout key.
        voice_device_id: Optional device identifier (log-only)
        timeout_minutes: Override duration

    Returns:
        Tuple of (success, message, response_data)
    """
    try:
        if not await _mode_service_supports_admin_pin_authority():
            logger.warning("override_unavailable_mode_service_version", caller_tier=caller_tier)
            return (
                False,
                "Owner mode isn't available right now -- the system is finishing an update. Please try again in a minute.",
                None,
            )

        request_data = {
            "mode": "owner",
            "voice_pin": pin,
            "timeout_minutes": timeout_minutes,
            "voice_device_id": voice_device_id,
            "caller_tier": caller_tier,
        }

        # D34: the override POST gets its own 5 s timeout, distinct from
        # mode_client's 2.5 s default (used for permission/health fetches).
        response = await mode_client.post("/mode/override", json=request_data, timeout=5.0)

        if response.status_code == 200:
            data = response.json()
            logger.info(
                "owner_override_activated",
                expires_at=data.get("expires_at"),
                device=voice_device_id,
                caller_tier=caller_tier,
            )
            return True, data.get("message", "Owner mode activated."), data

        elif response.status_code == 401:
            # PIN required but not provided
            logger.info("owner_override_pin_required", device=voice_device_id)
            return False, "Please provide your 6-digit owner PIN. Say 'owner mode' followed by your PIN.", None

        elif response.status_code == 403:
            detail = ""
            try:
                detail = response.json().get("detail", "") or ""
            except Exception:
                detail = ""
            if "owner_pin_not_configured" in detail:
                logger.warning("owner_override_pin_not_configured", caller_tier=caller_tier)
                return False, "Owner mode needs a PIN set in the admin panel.", None
            # Invalid PIN
            logger.warning("owner_override_pin_invalid", device=voice_device_id)
            return False, "Invalid PIN. Access denied.", None

        elif response.status_code == 400:
            # Invalid PIN format
            detail = response.json().get("detail", "Invalid PIN format")
            logger.warning("owner_override_invalid_format", detail=detail, device=voice_device_id)
            return False, f"{detail}. Please provide a 6-digit PIN.", None

        elif response.status_code == 429:
            logger.warning("owner_override_locked", caller_tier=caller_tier)
            return False, "Owner mode is temporarily locked after too many attempts. Try again later.", None

        elif response.status_code == 503:
            logger.warning("owner_pin_verification_unavailable", caller_tier=caller_tier)
            return False, "I can't verify the PIN right now. Please try again in a minute.", None

        else:
            logger.error(
                "owner_override_unexpected_error",
                status_code=response.status_code,
                device=voice_device_id
            )
            return False, "Unable to process owner mode request. Please try again.", None

    except Exception as e:
        logger.error(f"owner_override_failed: {e}")
        return False, "Mode service unavailable. Owner mode request could not be processed.", None


# ============================================================================
# ATHENA-69 D16/D24: owner-override surface gating and per-tier throttle
# ============================================================================

# The one caller_trust that can become a proven owner: jarvis-web sends it only
# for a Bearer-authenticated admin owner. It counts only together with an
# authenticated service hop (see resolve_request_authorization).
OWNER_PROOF_TRUST = "web_owner"
# Signed-in browser callers. A web_owner is a signed-in caller too, but a
# Bearer caller never carries a display name (speaker_first_name).
SIGNED_IN_TRUST = frozenset({"web_authenticated", OWNER_PROOF_TRUST})
PIN_TRUSTED_TIERS = frozenset({"household", "sms"}) | SIGNED_IN_TRUST


def _pin_tier(caller_trust: Optional[str]) -> Optional[str]:
    """The tier the throttle and the mode/admin services see: a web_owner is a
    signed-in browser caller, so their caller_tier Literals stay unchanged."""
    return "web_authenticated" if caller_trust == OWNER_PROOF_TRUST else caller_trust


class OwnerOverrideThrottle:
    """In-process per-tier rate limit on owner-override attempts (D16).

    Keyed ONLY on ``tier`` -- never ``session_id``, ``room``, ``device_id``,
    or ``source``, all of which are caller-controlled and would let an
    anonymous caller reset or evade the limit by rotating them. 10 attempts
    per tier per 10-minute sliding window.
    """

    _MAX_ATTEMPTS = 10
    _WINDOW_SECONDS = 600.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._attempts: Dict[str, List[float]] = {}

    def check(self, tier: str) -> bool:
        """Record an attempt for `tier` and return whether it's allowed."""
        now = self._clock()
        window_start = now - self._WINDOW_SECONDS
        history = self._attempts.setdefault(tier, [])
        history[:] = [t for t in history if t > window_start]
        if len(history) >= self._MAX_ATTEMPTS:
            return False
        history.append(now)
        return True


_owner_override_throttle = OwnerOverrideThrottle()


def _reset_owner_override_throttle_for_tests() -> None:
    """PRIVATE — test isolation only. Production code never calls this."""
    global _owner_override_throttle
    _owner_override_throttle = OwnerOverrideThrottle()


@dataclass(frozen=True)
class OwnerOverrideOutcome:
    """The result of `handle_owner_mode_utterance` when the query WAS an
    owner-mode command (None means it wasn't one at all)."""
    success: bool
    message: str
    override_data: Optional[Dict[str, Any]]
    refused_reason: Optional[str] = None


async def handle_owner_mode_utterance(
    query: str, caller_trust: Optional[str], room: Optional[str]
) -> Optional["OwnerOverrideOutcome"]:
    """The single orchestrator entry point for the owner-PIN voice/utterance
    path (D16, D24). Returns None when `query` isn't an owner-mode command
    at all. Otherwise, in order:

    1. `caller_trust not in PIN_TRUSTED_TIERS` -> refused "untrusted_surface",
       before any throttle consumption or mode-service call (bob r2 H2 --
       an anonymous internet caller must never be able to touch the
       per-tier throttle or lock out a real tier).
    2. Throttle check for `caller_trust` -> refused "throttled" on failure.
    3. `extract_pin_from_query` -> `activate_owner_override`.
    """
    if not detect_owner_mode_command(query):
        return None

    if caller_trust not in PIN_TRUSTED_TIERS:
        logger.warning("owner_override_refused_untrusted_surface", caller_trust=caller_trust)
        return OwnerOverrideOutcome(
            success=False,
            message="Owner mode isn't available from here.",
            override_data=None,
            refused_reason="untrusted_surface",
        )

    tier = _pin_tier(caller_trust)
    if not _owner_override_throttle.check(tier):
        logger.warning("owner_override_throttled", caller_trust=caller_trust)
        return OwnerOverrideOutcome(
            success=False,
            message="Owner mode is temporarily locked after too many attempts. Try again later.",
            override_data=None,
            refused_reason="throttled",
        )

    pin = extract_pin_from_query(query)
    success, message, override_data = await activate_owner_override(
        pin, caller_tier=tier, voice_device_id=room
    )
    return OwnerOverrideOutcome(success=success, message=message, override_data=override_data, refused_reason=None)


def check_intent_permission(intent: IntentCategory, permissions: Dict[str, Any]) -> bool:
    """
    Check if intent is allowed based on current permissions (Phase 2: Guest Mode).

    Uses a deny-list approach: intents in restricted_intents are blocked,
    everything else is allowed (unless allowed_intents is populated).

    Args:
        intent: Intent category
        permissions: Permissions from mode service

    Returns:
        True if allowed, False otherwise
    """
    mode = permissions.get("mode", "owner")

    # Owner mode: everything allowed
    if mode == "owner":
        return True

    intent_value = intent.value.lower()

    # Primary check: restricted_intents (deny list)
    restricted_intents = permissions.get("restricted_intents", [])
    if restricted_intents:
        is_restricted = intent_value in [i.lower() for i in restricted_intents]
        if is_restricted:
            logger.info(
                "intent_blocked_restricted",
                intent=intent_value,
                mode=mode,
                restricted_intents=restricted_intents
            )
            return False

    # Secondary check: allowed_intents (allow list) if populated
    allowed_intents = permissions.get("allowed_intents", [])
    if allowed_intents:
        is_allowed = intent_value in [i.lower() for i in allowed_intents]
        logger.info(
            "intent_permission_check",
            intent=intent_value,
            mode=mode,
            allowed=is_allowed,
            allowed_intents=allowed_intents
        )
        return is_allowed

    # No allow list specified - allow by default (only restricted_intents blocked)
    logger.info(
        "intent_allowed_default",
        intent=intent_value,
        mode=mode
    )
    return True


def check_entity_permission(entity_id: str, permissions: Dict[str, Any]) -> bool:
    """
    Check if entity access is allowed based on current permissions (Phase 2: Guest Mode).

    Uses regex patterns for restricted_entities (e.g., ".*tesla.*", ".*vehicle.*").
    Falls back to domain-based allow list if no match.

    Args:
        entity_id: Home Assistant entity ID (e.g., "light.bedroom")
        permissions: Permissions from mode service

    Returns:
        True if allowed, False otherwise
    """
    import re

    mode = permissions.get("mode", "owner")

    # Owner mode: everything allowed
    if mode == "owner":
        return True

    # Guest mode: check restrictions
    restricted_entities = permissions.get("restricted_entities", [])
    allowed_domains = permissions.get("allowed_domains", [])

    # Check if entity matches restricted pattern (supports regex)
    for pattern in restricted_entities:
        try:
            if re.match(pattern, entity_id, re.IGNORECASE):
                logger.info("entity_blocked_by_regex", kind="regex", mode=mode)
                return False
        except re.error:
            # Invalid regex - try simple wildcard match as fallback
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if entity_id.startswith(prefix):
                    logger.info("entity_blocked_by_wildcard", kind="wildcard", mode=mode)
                    return False
            elif pattern == entity_id:
                logger.info("entity_blocked_exact", kind="exact", mode=mode)
                return False

    # Check if entity domain is allowed. An empty allowed_domains has no
    # safe "allow everything" meaning for a GUEST specifically -- fall
    # back to the baseline domain list rather than skip the check
    # entirely (Pass H, codex full-diff, Low: this previously let an
    # empty allowed_domains, however it arose, silently allow every
    # domain for a guest). Scoped to mode=="guest": degraded_permissions()
    # (D4) deliberately sets allowed_domains=[] to mean "no domain
    # restriction beyond the entity floor" for an outage/system scope --
    # applying the guest baseline there would newly block domains
    # (select, input_boolean, ...) an owner-during-outage is meant to
    # keep.
    entity_domain = entity_id.split(".")[0] if "." in entity_id else entity_id
    # Logs carry the domain, never an id: an id with no domain part is a name.
    logged_domain = entity_domain if "." in entity_id else "none"
    if mode == "guest":
        effective_allowed_domains = allowed_domains or baseline_allowed_domains()
    else:
        effective_allowed_domains = allowed_domains
    if effective_allowed_domains and entity_domain not in effective_allowed_domains:
        logger.info("entity_blocked_domain", kind="domain", domain=logged_domain, mode=mode)
        return False

    logger.info("entity_allowed", domain=logged_domain, mode=mode)
    return True


# ============================================================================
# ATHENA-69: HA write authorization guard
# ============================================================================
#
# Everything below this line is new for ATHENA-69 (Pass A). See the module
# docstring and the plan's Design section for the full contract. Pass A
# lands: HAWriteDecision/HADenial/HAWritePermissionDenied, authorize_ha_write,
# normalize_permissions, degraded_permissions, PermissionScope/
# ha_permission_scope/current_ha_scope, PermissionEnforcingHAClient (call_service
# interception + read allowlist only -- automation methods are Pass B),
# ensure_permission_enforcing, CONTROL_DEVICE_DOMAINS/intent_write_domains,
# permission_refusal_message, GUEST_INTENT_REFUSAL, DEGRADED_INTENT_REFUSAL.

_ha_permission_scope_var: "contextvars.ContextVar[Optional[PermissionScope]]" = contextvars.ContextVar(
    "athena_ha_permission_scope", default=None
)

# Identity sentinel for ensure_permission_enforcing (D3/D1). A module-level
# object() rather than a class-level True/False flag so identity comparison
# (`is _GUARD_SENTINEL`) can't be spoofed by a MagicMock's auto-attribute
# behavior -- a MagicMock will happily return a *new* MagicMock for
# `.athena_ha_guard`, but it can never equal this specific object by identity
# unless it's actually the guard (or a proxy that forwards to it).
_GUARD_SENTINEL = object()


@dataclass(frozen=True)
class HAWriteDecision:
    """The result of authorizing one HA write (authorize_ha_write /
    authorize_automation_config / authorize_sequence)."""
    allowed: bool
    denied_targets: Tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class HADenial:
    """One recorded denial on a PermissionScope."""
    domain: str
    service: str
    targets: Tuple[str, ...]
    reason: str


class HAWritePermissionDenied(Exception):
    """Raised by PermissionEnforcingHAClient when a write is denied.

    The only new exception type ATHENA-69 introduces. Callers that swallow
    exceptions broadly (e.g. _handle_scene_intent's activation try/except)
    must re-raise this one ahead of their catch-all so a denial can't start
    a fallback write chain (D2, wired in Pass B).

    ``reason`` is the denial's reason code (the same value recorded on the
    scope), so a caller that catches this can pick the matching refusal
    phrase without reading the scope.
    """

    def __init__(self, message: str = "", *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class PermissionScope:
    """Per-request authorization state, held in a ContextVar for the
    lifetime of one `with ha_permission_scope(...):` block.

    ``permissions`` is always the *normalized* dict (see
    normalize_permissions) -- never the raw, possibly-empty/mode-less dict a
    caller passed in. ``mode`` is the scope's declared mode (typically
    ``state.mode``, or "system" for an unscoped/background write) and can
    legitimately differ from ``permissions["mode"]`` (e.g. a degraded outage
    reports mode="owner" at the OrchestratorState level but
    permissions["mode"] == "degraded").
    """
    permissions: Dict[str, Any]
    mode: str
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    denials: List[HADenial] = field(default_factory=list)
    allowed_writes: int = 0
    halted: bool = False
    # ATHENA-128: structural read-only guarantee (D4) and write fan-out
    # confirmation carriage (D9, D16). ``utterance`` holds the
    # UtteranceClassification the gate reads (real classification even
    # under the D15 kill switch); ``fanout_block`` and
    # ``confirmed_entity_ids`` are set/read by write_fanout.py only.
    read_only: bool = False
    utterance: Any = None
    fanout_block: Any = None
    can_carry_pending: bool = False
    confirmed_entity_ids: Optional[frozenset] = None
    # Set by room_choice only: the "which one?" a write turn asked, and the
    # same question asked by a read-only (state question) turn.
    room_choice: Any = None
    room_clarification: Any = None
    # Server-set: the gateway judged this a trusted voice caller. Only then
    # does an owner's "all the lights" reach every area's lamps.
    trusted_voice: bool = False
    # D14 rule C: the lowercased ids of the doors this request has already
    # tried to open (every lock an opening call named, every access cover).
    # Recorded before the write is awaited, so a failed write still counts,
    # and shared with a nested scope (ha_permission_scope) and with every
    # task that copied this scope's context.
    opened: Set[str] = field(default_factory=set)


def degraded_permissions() -> Dict[str, Any]:
    """The permission set used when the mode service is unreachable/
    rejecting, or when `permissions` is missing/empty/mode-less (D4, D5).

    Never "owner": entity-level writes to the D4 fallback domains (locks,
    covers, alarm panels, cameras, automations, scripts, scenes by default)
    stay denied; intents are NOT restricted by default (allowed_intents and
    restricted_intents are both empty), matching D4's "owners keep lights,
    climate, media during an outage" -- only entity/domain-level physical-
    security writes are floored.
    """
    fallback_entities = parse_json_array_env(
        get_config().ha_permission_fallback_restricted_entities,
        [
            r"^lock\.", r"^cover\.", r"^alarm_control_panel\.", r"^camera\.",
            r"^automation\.", r"^script\.", r"^scene\.",
        ],
    )
    return {
        "mode": "degraded",
        "restricted_entities": fallback_entities,
        "allowed_domains": [],
        "allowed_intents": [],
        "restricted_intents": [],
    }


# ---------------------------------------------------------------------------
# The public audience (anonymous embed visitors)
# ---------------------------------------------------------------------------

PUBLIC_CALLER_TRUST = "web_public"

PUBLIC_ALLOWED_INTENTS = frozenset({
    IntentCategory.WEATHER.value,
    IntentCategory.GENERAL_INFO.value,
    IntentCategory.NEWS.value,
    IntentCategory.RECIPES.value,
    IntentCategory.STREAMING.value,
})

PUBLIC_ALLOWED_TOOLS = frozenset({"get_weather", "get_news", "search_recipes", "search_streaming"})

PUBLIC_INTENT_REFUSAL = (
    "Sorry, I can't help with that here. I can answer general questions, "
    "or help with the weather, news, recipes, and what's streaming."
)


def public_permissions() -> Dict[str, Any]:
    """The permission set for an anonymous public caller (an embedded
    website chatbot, relayed by jarvis-web).

    Hard-coded and never fetched: not the mode service's guest profile,
    not the degraded baseline, not GUEST_BASELINE_* env. Widening what a
    rental guest may do must never widen what the internet may do.

    It's a guest dict, so every existing guest branch still fires, and the
    ``audience`` marker only narrows further. ``restricted_entities [".*"]``
    denies every HA target before domains are consulted; the ``__none__``
    domain sentinel stops apply_guest_baseline refilling the baseline
    domains. Returns a fresh dict each call.
    """
    return {
        "mode": "guest",
        "audience": "public",
        "allowed_intents": sorted(PUBLIC_ALLOWED_INTENTS),
        "restricted_intents": [],
        "restricted_entities": [".*"],
        "allowed_domains": ["__none__"],
    }


# ---------------------------------------------------------------------------
# SMS outside the current stay (answer-only)
# ---------------------------------------------------------------------------

# The one stay phase in which an SMS may change anything.
SMS_WRITE_STAY_PHASES = frozenset({"current"})

# Outside the current stay an SMS may ask travel and checkout questions,
# which read nothing in the house (general_info carries the checkout FAQ
# through base knowledge). Everything else, CONTROL included (so lock,
# alarm, presence and camera state questions), is refused.
OFF_STAY_ALLOWED_INTENTS = frozenset({
    IntentCategory.GENERAL_INFO.value,
    IntentCategory.WEATHER.value,
    IntentCategory.DIRECTIONS.value,
    IntentCategory.DINING.value,
    IntentCategory.EVENTS.value,
    IntentCategory.AIRPORTS.value,
    IntentCategory.FLIGHTS.value,
})

OFF_STAY_ALLOWED_TOOLS = frozenset({
    "get_weather", "get_directions", "search_restaurants", "search_events",
    "get_airport_info", "search_flights", "search_transit", "get_train_schedule",
})

STAY_READ_ONLY_REFUSAL = (
    "I can answer questions about your trip, but I can't change anything for the "
    "house outside your stay dates."
)


def off_stay_overlay(permissions: Dict[str, Any], phase: Optional[str]) -> Dict[str, Any]:
    """Guest permissions for an SMS from outside the current stay. Pure.

    - every HA write is refused (``restricted_entities [".*"]``, the
      ``__none__`` domain sentinel), by the existing authorize_* checks;
    - ``stay_read_only`` makes the admin-DB writers refuse (automations,
      notification preferences, SMS send; see is_stay_read_only);
    - intents and offered tools narrow to OFF_STAY_* (house reads refused).
    """
    allowed = set(permissions.get("allowed_intents") or OFF_STAY_ALLOWED_INTENTS)
    return {
        **permissions,
        "restricted_entities": [".*"],
        "allowed_domains": ["__none__"],
        "stay_read_only": True,
        "sms_stay_phase": phase or "unknown",
        "allowed_intents": sorted(allowed & OFF_STAY_ALLOWED_INTENTS),
        "restricted_intents": sorted({i.value for i in IntentCategory} - OFF_STAY_ALLOWED_INTENTS),
    }


def is_stay_read_only(permissions: Optional[Dict[str, Any]]) -> bool:
    """True when every admin-DB write must be refused for this request (an
    SMS from outside the current stay). The one question admin-DB writers
    ask; empty or missing permissions never block (owner/household)."""
    return bool((permissions or {}).get("stay_read_only"))


def offered_tools(tools: Optional[List[Dict[str, Any]]], permissions: Optional[Dict[str, Any]]):
    """The tools this caller is offered (and may execute): the public
    audience's allowlist, then the off-stay narrowing. Returns a new list
    when it narrows, so cached schemas are never narrowed for later callers."""
    if not tools:
        return tools
    if is_public_audience(permissions):
        tools = [t for t in tools if t["function"]["name"] in PUBLIC_ALLOWED_TOOLS]
    if is_stay_read_only(permissions):
        tools = [t for t in tools if t["function"]["name"] in OFF_STAY_ALLOWED_TOOLS]
    return tools


def is_public_audience(permissions: Optional[Dict[str, Any]]) -> bool:
    """True when ``permissions`` belong to the public audience. The single
    way code asks this question after authorization."""
    return isinstance(permissions, dict) and permissions.get("audience") == "public"


def is_public_caller(caller_trust: Optional[str]) -> bool:
    """True when the calling service classified this request as public.
    Used before authorization (device lookup, context, session); after
    authorization ask ``is_public_audience``."""
    return caller_trust == PUBLIC_CALLER_TRUST


def normalize_permissions(permissions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize a raw permissions dict for use by the guard (D5).

    Missing / empty / no "mode" key -> degraded_permissions(). Never
    manufactures "owner" from nothing. ``mode == "guest"`` gets the guest
    floor/baseline unioned in via apply_guest_baseline (D8/D22). Any other
    mode (including an already-normalized "owner" or "degraded") is
    returned as a shallow copy, unmodified.
    """
    if not permissions or not permissions.get("mode"):
        return degraded_permissions()
    if permissions.get("mode") == "guest":
        return apply_guest_baseline(permissions)
    return dict(permissions)


@contextlib.contextmanager
def ha_permission_scope(
    permissions: Optional[Dict[str, Any]],
    *,
    mode: str = "system",
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    read_only: bool = False,
    utterance: Any = None,
    trusted_voice: bool = False,
):
    """Open a new PermissionScope for the duration of the with-block.

    ``permissions`` is normalized on entry (D5) -- callers pass the raw
    ``state.permissions`` and never need to normalize it themselves.
    ``ha_permission_scope(None, mode="system")`` opens a baseline
    (degraded) scope for a system-initiated write with no request context
    (D3) -- e.g. a background sequence step or the follow-me service.

    A scope opened inside another one is the same request: it shares the
    outer scope's ``opened`` set, so nesting can never reset the one-door
    allowance.

    A scope must never span an ``await`` that yields control back to a
    different logical request's context without a per-task copy (asyncio
    tasks/gather copy the current context, so this holds for
    create_task/gather fan-outs); it must never span an actual generator
    ``yield`` (resetting a contextvars.Token set in a different context
    raises) -- a drift test asserts no bare `yield` appears inside a `with
    ha_permission_scope(` block anywhere in the tree.
    """
    outer = _ha_permission_scope_var.get()
    scope = PermissionScope(
        permissions=normalize_permissions(permissions),
        mode=mode,
        request_id=request_id,
        session_id=session_id,
        read_only=read_only,
        utterance=utterance,
        trusted_voice=trusted_voice,
        opened=outer.opened if outer is not None else set(),
    )
    token = _ha_permission_scope_var.set(scope)
    try:
        yield scope
    finally:
        _ha_permission_scope_var.reset(token)


def current_ha_scope() -> Optional[PermissionScope]:
    """The PermissionScope open in the current context, or None."""
    return _ha_permission_scope_var.get()


# ---------------------------------------------------------------------------
# Target normalization + authorize_ha_write
# ---------------------------------------------------------------------------

_AREA_DEVICE_FLOOR_LABEL_KEYS = ("area_id", "device_id", "floor_id", "label_id")


def _split_entity_ids(value: Any) -> List[str]:
    """Split an entity_id value (str, comma-separated str, or list) into a
    list of stripped entity-id strings. Non-string list elements are
    dropped rather than raising -- HA payloads are caller-controlled."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _has_area_device_floor_label(d: Any) -> bool:
    return isinstance(d, dict) and any(key in d for key in _AREA_DEVICE_FLOOR_LABEL_KEYS)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_targets(domain: str, service_data: Optional[Dict[str, Any]]) -> List[str]:
    """Normalize a call_service payload into a list of pseudo-targets.

    See the Design section's "Target normalization (hidden)" for the full
    contract: entity_id is read from the top level, ``target``, ``data`` and
    ``data.target`` and the ids of all four are counted together (a second
    id hidden in another block can't slip past a one-door rule); "all"/a
    missing entity/an area-device-floor-label target all become
    ``f"{domain}.all"``; a bare id without a domain becomes
    ``f"{domain}.{id}"``.
    """
    data = _as_dict(service_data)
    target_block = _as_dict(data.get("target"))
    data_block = _as_dict(data.get("data"))
    data_target_block = _as_dict(data_block.get("target"))

    if (
        _has_area_device_floor_label(data)
        or _has_area_device_floor_label(target_block)
        or _has_area_device_floor_label(data_block)
        or _has_area_device_floor_label(data_target_block)
    ):
        return [f"{domain}.all"]

    entity_values: List[str] = []
    for block in (data, target_block, data_block, data_target_block):
        for value in _split_entity_ids(block.get("entity_id")):
            if value not in entity_values:
                entity_values.append(value)

    if not entity_values:
        return [f"{domain}.all"]

    targets: List[str] = []
    for raw in entity_values:
        if raw.lower() == "all":
            pseudo = f"{domain}.all"
            if pseudo not in targets:
                targets.append(pseudo)
            continue
        if "." in raw:
            targets.append(raw)
        else:
            targets.append(f"{domain}.{raw}")
    return targets


# ---------------------------------------------------------------------------
# Openings (ATHENA-233, D14): a way in is one concrete door, once per request
# ---------------------------------------------------------------------------
#
# An opening is an unlock, or the opening of a cover that is not clearly a
# window covering. The controller grounds these in the user's words; this is
# the backstop that holds when a higher layer is wrong. The sets below repeat
# access_target's (mode_permission may not import the room modules), and
# tests/unit/test_ha_permission_guard.py pins them equal.

OPENING_NEEDS_NAMED_TARGET = "opening_needs_named_target"
UNLOCK_NEEDS_ONE_LOCK = "unlock_needs_one_lock"
ONE_OPENING_PER_REQUEST = "one_opening_per_request"
TEMPLATED_SERVICE = "templated_service"

# Services that can let someone in, per domain. HA has no lock.toggle or
# lock.turn_off; they are listed so an invented one is treated as an opening.
_OPENING_SERVICES: Dict[str, frozenset] = {
    "lock": frozenset({"unlock", "open", "turn_off", "toggle"}),
    "cover": frozenset({"open", "open_cover", "turn_on", "toggle", "set_cover_position"}),
}
# homeassistant.* maps onto the target's own domain: turn_off unlocks a lock,
# turn_on opens a cover, toggle does either.
_HOMEASSISTANT_OPENING_SERVICES: Dict[str, frozenset] = {
    "lock": frozenset({"turn_off", "toggle"}),
    "cover": frozenset({"turn_on", "toggle"}),
}
_HOMEASSISTANT_OPENING_CAPABLE = frozenset({"turn_off", "turn_on", "toggle"})
NON_ACCESS_DEVICE_CLASSES = frozenset({
    "awning", "blind", "curtain", "damper", "shade", "shutter", "window",
})
# Device-form automation steps: ``domain`` + ``type`` -> the service they run.
_DEVICE_OPENING_SERVICES: Dict[Tuple[str, str], str] = {
    ("lock", "unlock"): "unlock",
    ("lock", "open"): "open",
    ("lock", "toggle"): "toggle",
    ("cover", "open"): "open_cover",
    ("cover", "toggle"): "toggle",
    ("cover", "set_position"): "set_cover_position",
}

_CONCRETE_DOOR_RE = re.compile(r"(lock|cover)\.[a-z0-9_]+")
_CONCRETE_ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


def _normalized_targets(domain: str, service_data: Optional[Dict[str, Any]]) -> List[str]:
    """``_extract_targets`` output, lowercased, stripped and de-duplicated."""
    seen: List[str] = []
    for target in _extract_targets(str(domain).strip().lower(), service_data):
        norm = target.strip().lower()
        if norm not in seen:
            seen.append(norm)
    return seen


def _is_concrete_target(target: str, *, doors_only: bool) -> bool:
    """One real entity id: not a ``.all`` pseudo-target and, outside the door
    domains, not a bare id that was only given ``homeassistant.`` as a prefix."""
    pattern = _CONCRETE_DOOR_RE if doors_only else _CONCRETE_ENTITY_RE
    if pattern.fullmatch(target) is None:
        return False
    domain_part, _, object_part = target.partition(".")
    return object_part != "all" and (doors_only or domain_part != "homeassistant")


def _is_opening_capable(domain: str, service: str) -> bool:
    if domain == "homeassistant":
        return service in _HOMEASSISTANT_OPENING_CAPABLE
    return service in _OPENING_SERVICES.get(domain, ())


def _opens(domain: str, service: str, target_domain: str) -> bool:
    """Whether ``domain.service`` opens an entity of ``target_domain``."""
    if domain == "homeassistant":
        return service in _HOMEASSISTANT_OPENING_SERVICES.get(target_domain, ())
    return service in _OPENING_SERVICES.get(domain, ())


def _opening_ids_of(
    domain: str, service: str, targets: Iterable[str], cover_access: Optional[Mapping[str, bool]]
) -> List[str]:
    """The doors an opening call opens: every lock, every cover not known to
    be a window covering (a cover missing from ``cover_access`` is a way in)."""
    ids: List[str] = []
    for target in targets:
        target_domain = target.split(".", 1)[0]
        if target_domain not in ("lock", "cover") or not _opens(domain, service, target_domain):
            continue
        if target_domain == "cover" and cover_access is not None and not cover_access.get(target, True):
            continue
        if target not in ids:
            ids.append(target)
    return ids


def opening_cover_targets(domain: str, service: str, service_data: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """The concrete cover ids of an opening call, i.e. the ones whose
    ``device_class`` must be read before the call can be judged."""
    d, svc = str(domain).strip().lower(), str(service).strip().lower()
    return tuple(
        t for t in _normalized_targets(d, service_data)
        if t.startswith("cover.") and _is_concrete_target(t, doors_only=True) and _opens(d, svc, "cover")
    )


def opening_ids(
    domain: str,
    service: str,
    service_data: Optional[Dict[str, Any]],
    cover_access: Optional[Mapping[str, bool]] = None,
) -> Tuple[str, ...]:
    """The lowercased ids of the doors ``domain.service`` would open (D14 C)."""
    d, svc = str(domain).strip().lower(), str(service).strip().lower()
    return tuple(_opening_ids_of(d, svc, _normalized_targets(d, service_data), cover_access))


def _opening_denial(
    domain: str, service: str, targets: List[str], cover_access: Optional[Mapping[str, bool]]
) -> Optional[str]:
    """Rules A and B for one call, on the targets as given (before the
    cross-domain fallback adds any). Returns the denial reason or None."""
    if not _is_opening_capable(domain, service):
        return None
    doors_only = domain != "homeassistant"
    if not all(_is_concrete_target(t, doors_only=doors_only) for t in targets):
        return OPENING_NEEDS_NAMED_TARGET
    ids = _opening_ids_of(domain, service, targets, cover_access)
    if sum(1 for i in ids if i.startswith("lock.")) > 1:
        return UNLOCK_NEEDS_ONE_LOCK
    if any(i.startswith("cover.") for i in ids) and len(targets) != 1:
        return OPENING_NEEDS_NAMED_TARGET
    return None


def authorize_ha_write(
    domain: str,
    service: str,
    service_data: Optional[Dict[str, Any]],
    permissions: Dict[str, Any],
    *,
    cover_access: Optional[Mapping[str, bool]] = None,
) -> HAWriteDecision:
    """Authorize one HA write (call_service-shaped) against `permissions`.

    Pure. Normalizes ``permissions`` itself (D5) so a caller can pass a raw
    or already-normalized dict interchangeably. Every normalized target
    (see _extract_targets) goes through check_entity_permission; any
    denial denies the whole call. When the target's own domain differs
    from the service's declared domain (e.g. `homeassistant.turn_on` with
    `lock.front`), both the entity itself and `f"{domain}.all"` (the
    service's own declared domain, as a pseudo-target) are checked -- a
    generic dispatch service can never reach an entity via a domain that
    wouldn't itself be authorized.

    Openings (D14 A and B), once the entity checks pass: every target of an
    unlock or door opening must be one concrete ``lock.x``/``cover.x`` id (no
    ``all``, area, device, floor, label or bare id); at most one lock; and a
    call that opens an access cover must name exactly one target.
    ``cover_access`` maps a cover id to whether it is a way in; a cover
    missing from it is one.
    """
    perms = normalize_permissions(permissions)
    targets = _extract_targets(domain, service_data)
    named_targets = _normalized_targets(domain, service_data)

    extra_targets: List[str] = []
    for target in targets:
        target_domain = target.split(".")[0] if "." in target else domain
        if target_domain != domain:
            fallback = f"{domain}.all"
            if fallback not in targets and fallback not in extra_targets:
                extra_targets.append(fallback)
    targets = targets + extra_targets

    denied = [t for t in targets if not check_entity_permission(t, perms)]
    if denied:
        return HAWriteDecision(allowed=False, denied_targets=tuple(denied), reason="entity_or_domain_denied")
    reason = _opening_denial(str(domain).strip().lower(), str(service).strip().lower(), named_targets, cover_access)
    if reason:
        return HAWriteDecision(allowed=False, denied_targets=tuple(named_targets), reason=reason)
    return HAWriteDecision(allowed=True, denied_targets=(), reason="")


# ---------------------------------------------------------------------------
# authorize_automation_config (Pass B, D1)
# ---------------------------------------------------------------------------

_AUTOMATION_INERT_STEP_KEYS = frozenset({
    "delay", "wait_template", "wait_for_trigger", "condition", "conditions",
    "alias", "enabled", "continue_on_error", "stop", "variables",
})

# Keys whose value is itself an action list/steps to recurse into. A step
# carrying any of these has no domain.service of its own -- it's a
# control-flow container (choose/parallel/repeat/if-then-else), not a leaf
# action -- so it is never itself passed to _authorize_automation_step.
_AUTOMATION_CONTAINER_KEYS = ("sequence", "default", "then", "else", "parallel")


def _walk_automation_steps(steps: Any) -> Iterable[Dict[str, Any]]:
    """Yield every leaf action-step dict in an automation action tree,
    recursing through sequence/choose[].sequence/default/then/else/
    parallel/repeat.sequence containers. A container step's `if` condition
    list is never recursed into -- conditions aren't actions."""
    if steps is None:
        return
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, (list, tuple)):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        is_container = False
        for key in _AUTOMATION_CONTAINER_KEYS:
            if key in step:
                is_container = True
                yield from _walk_automation_steps(step[key])
        if isinstance(step.get("choose"), list):
            is_container = True
            for choice in step["choose"]:
                if isinstance(choice, dict) and "sequence" in choice:
                    yield from _walk_automation_steps(choice["sequence"])
        repeat = step.get("repeat")
        if isinstance(repeat, dict) and "sequence" in repeat:
            is_container = True
            yield from _walk_automation_steps(repeat["sequence"])
        if not is_container:
            yield step


def _has_template(value: Any) -> bool:
    """Whether a config value carries a Jinja template anywhere inside it."""
    if isinstance(value, str):
        return "{{" in value or "{%" in value or "{#" in value
    if isinstance(value, dict):
        return any(_has_template(k) or _has_template(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_template(v) for v in value)
    return False


def _step_template_targets(step: Dict[str, Any]) -> bool:
    return any(_has_template(step.get(key)) for key in ("entity_id", "target", "data"))


def _step_service(step: Dict[str, Any]) -> Any:
    return step.get("service") or step.get("action")


def _step_call(step: Dict[str, Any]) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """The ``(domain, service, service_data)`` a leaf step runs, lowercased,
    for the two forms that can open a door: ``service: domain.name`` and the
    device form (``domain`` + ``type``). None for anything else."""
    service = _step_service(step)
    if isinstance(service, str) and "." in service and not _has_template(service):
        domain, svc = service.split(".", 1)
        return domain.strip().lower(), svc.strip().lower(), step
    device_domain, device_type = step.get("domain"), step.get("type")
    if service is None and isinstance(device_domain, str) and isinstance(device_type, str):
        mapped = _DEVICE_OPENING_SERVICES.get((device_domain.strip().lower(), device_type.strip().lower()))
        if mapped:
            return device_domain.strip().lower(), mapped, {"entity_id": step.get("entity_id")}
    return None


def _authorize_automation_step(
    step: Dict[str, Any], perms: Dict[str, Any], cover_access: Optional[Mapping[str, bool]] = None
) -> HAWriteDecision:
    """Classify and authorize a single leaf automation step (see
    _walk_automation_steps for what counts as a leaf).

    D14 D: a templated service (or a templated target on an opening service)
    is denied, as its door can't be known until it runs; the device form
    (``domain: lock|cover``, ``type: unlock|open``) is an opening and needs
    one concrete entity, never only a ``device_id``.
    """
    if set(step.keys()) <= _AUTOMATION_INERT_STEP_KEYS:
        return HAWriteDecision(allowed=True)

    service = _step_service(step)
    if service and (not isinstance(service, str) or _has_template(service)):
        return HAWriteDecision(allowed=False, denied_targets=(), reason=TEMPLATED_SERVICE)

    call = _step_call(step)
    if call is not None:
        domain, svc, data = call
        decision = authorize_ha_write(domain, svc, data, perms, cover_access=cover_access)
        if (
            not decision.allowed
            and decision.reason != "entity_or_domain_denied"
            and _step_template_targets(step)
        ):
            return HAWriteDecision(allowed=False, denied_targets=decision.denied_targets, reason=TEMPLATED_SERVICE)
        return decision

    if isinstance(service, str) and "." in service:
        domain, svc = service.split(".", 1)
        return authorize_ha_write(domain, svc, step, perms)

    if "scene" in step:
        return authorize_ha_write("scene", "turn_on", {"entity_id": step["scene"]}, perms)

    if "device_id" in step and "domain" in step:
        return authorize_ha_write(step["domain"], "_precheck", None, perms)

    # Any other step (including "event") is denied unless the scope is owner.
    if perms.get("mode") == "owner":
        return HAWriteDecision(allowed=True)
    return HAWriteDecision(allowed=False, denied_targets=(), reason="unknown_automation_step")


def _config_steps(config: Any) -> Iterable[Dict[str, Any]]:
    cfg = config if isinstance(config, dict) else {}
    actions = cfg.get("action")
    if actions is None:
        actions = cfg.get("actions")
    return _walk_automation_steps(actions)


def automation_opening_cover_targets(config: Dict[str, Any]) -> Tuple[str, ...]:
    """Every concrete cover id an opening step of ``config`` names: the ids
    whose ``device_class`` must be read before the config can be judged."""
    found: List[str] = []
    for step in _config_steps(config):
        call = _step_call(step)
        if call is None:
            continue
        for target in opening_cover_targets(*call):
            if target not in found:
                found.append(target)
    return tuple(found)


def automation_opening_ids(
    config: Dict[str, Any], cover_access: Optional[Mapping[str, bool]] = None
) -> Tuple[str, ...]:
    """The lowercased ids of every door the steps of ``config`` would open."""
    found: List[str] = []
    for step in _config_steps(config):
        call = _step_call(step)
        if call is None:
            continue
        for door in opening_ids(*call, cover_access=cover_access):
            if door not in found:
                found.append(door)
    return tuple(found)


def authorize_automation_config(
    automation_id: str,
    config: Dict[str, Any],
    permissions: Dict[str, Any],
    *,
    cover_access: Optional[Mapping[str, bool]] = None,
) -> HAWriteDecision:
    """Authorize creating/updating an HA automation (create_automation).

    Requires `automation.<automation_id>` to pass, then walks every leaf
    step in `config["action"]` (or `config["actions"]`) and authorizes
    each individually; the first denial denies the whole automation. Then,
    across the whole config, at most one door may be opened (D14 D).
    """
    perms = normalize_permissions(permissions)

    entity_decision = authorize_ha_write(
        "automation", "_precheck", {"entity_id": f"automation.{automation_id}"}, perms
    )
    if not entity_decision.allowed:
        return entity_decision

    for step in _config_steps(config):
        decision = _authorize_automation_step(step, perms, cover_access)
        if not decision.allowed:
            return decision

    doors = automation_opening_ids(config, cover_access)
    if len(doors) > 1:
        return HAWriteDecision(allowed=False, denied_targets=doors, reason=ONE_OPENING_PER_REQUEST)
    return HAWriteDecision(allowed=True)


# ---------------------------------------------------------------------------
# Coarse per-device-type domain map (D14)
# ---------------------------------------------------------------------------

# Maps a SmartHomeController `device_type` (as it appears on the intent dict
# route_control_node builds) to *exactly* the set of HA domains that
# device_type's handler writes via call_service, derived from each handler's
# literal call_service first-argument domains (smart_home_controller.py).
# whole_house is not a real device_type (it's the room=="whole_house" marker
# nested inside the "light" dispatch) but is listed separately here because
# it's used by name in the sequence/automation walkers (Pass B) where a step
# can name it directly. A device_type absent from this map (including an
# unrecognized/missing one, which the real controller refuses with "I can
# only control lights right now") maps to `()` -- no known write, so the
# coarse pre-check never blocks a read-only or unrecognized intent.
CONTROL_DEVICE_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "light": ("light",),
    "whole_house": ("light",),
    "climate": ("climate",),
    "oven": (),
    "fridge": (),
    "freezer": (),
    "appliance": (),
    "sensor": (),
    "media": ("media_player",),
    "media_player": ("media_player",),
    "tv": ("media_player",),
    "speaker": ("media_player",),
    "bed_warmer": ("switch", "select"),
    "motion_control": ("input_boolean", "input_number"),
    "lock": ("lock",),
    "fan": ("fan",),
    "cover": ("cover",),
    "scene": ("scene", "script", "light", "lock"),
}


def intent_write_domains(intent: Dict[str, Any]) -> Tuple[str, ...]:
    """The HA domains `intent` would write, for the node-level coarse
    pre-check (D14) and (Pass B) the sequence walker's device-type steps.

    `()` for a missing/unrecognized device_type or a read-only one (e.g.
    `sensor`, `appliance`) -- the coarse check never blocks those.
    """
    device_type = intent.get("device_type") if isinstance(intent, dict) else None
    if not device_type:
        return ()
    return CONTROL_DEVICE_DOMAINS.get(device_type, ())


# ---------------------------------------------------------------------------
# authorize_sequence (Pass B, D21)
# ---------------------------------------------------------------------------

def authorize_sequence(sequence: List[Dict[str, Any]], permissions: Dict[str, Any]) -> HAWriteDecision:
    """Pre-authorize an entire SequenceExecutor step list before scheduling
    (D21) -- a guest can't schedule a step they aren't allowed to run now.

    For each step: `target.entity_id` present -> authorize_ha_write on that
    entity's own domain; otherwise a recognized `target.device_type` ->
    every domain that device_type writes, each checked as `f"{d}.all"`. A
    step with neither a resolvable entity nor a recognized device_type is
    denied unless the scope is owner. Denies the whole sequence on the
    first denied step.
    """
    perms = normalize_permissions(permissions)

    for step in sequence or []:
        if not isinstance(step, dict):
            continue
        target = step.get("target")
        target = target if isinstance(target, dict) else {}
        action = step.get("action", "turn_on")
        entity_id = target.get("entity_id")

        if entity_id:
            entity_domain = entity_id.split(".")[0] if "." in entity_id else (target.get("device_type") or "light")
            decision = authorize_ha_write(entity_domain, "_precheck", {"entity_id": entity_id}, perms)
            if not decision.allowed:
                return decision
            continue

        device_type = target.get("device_type")
        if device_type and device_type in CONTROL_DEVICE_DOMAINS:
            for domain in CONTROL_DEVICE_DOMAINS[device_type]:
                decision = authorize_ha_write(domain, "_precheck", None, perms)
                if not decision.allowed:
                    return decision
            continue

        # Neither a resolvable entity nor a recognized device_type.
        if perms.get("mode") != "owner":
            return HAWriteDecision(allowed=False, denied_targets=(), reason="unresolvable_sequence_step")

    return HAWriteDecision(allowed=True)


# ---------------------------------------------------------------------------
# Refusal text (D2, D9)
# ---------------------------------------------------------------------------

GUEST_INTENT_REFUSAL = "Sorry, I can't do that in guest mode."
DEGRADED_INTENT_REFUSAL = "Sorry, I can't do that right now because I couldn't verify permissions."

_DOMAIN_NOUNS: Dict[str, str] = {
    "light": "lights",
    "lock": "locks",
    "cover": "covers",
    "climate": "thermostat",
    "media_player": "media player",
    "switch": "switch",
    "select": "bed warmer",
    "scene": "scene",
    "script": "routine",
    "fan": "fan",
    "input_boolean": "motion settings",
    "input_number": "motion settings",
    "alarm_control_panel": "alarm",
    "camera": "camera",
    "automation": "automation",
}


def _noun_for_domains(domains: Iterable[str]) -> str:
    names: List[str] = []
    seen = set()
    for d in domains:
        noun = _DOMAIN_NOUNS.get(d, d)
        if noun not in seen:
            seen.add(noun)
            names.append(noun)
    if not names:
        return "device"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# Public alias (L5): write_fanout.py needs the noun-for-domains mapping
# and must not import a private name.
noun_for_domains = _noun_for_domains

READ_ONLY_REFUSAL = "I couldn't check that right now."

# Intents whose node refuses them itself, with a domain-specific message,
# before any dispatch. The intent gate leaves them to their node.
SELF_GATED_INTENTS = frozenset({
    IntentCategory.CONTROL,
    IntentCategory.MUSIC_PLAY,
    IntentCategory.MUSIC_CONTROL,
    IntentCategory.TV_CONTROL,
    IntentCategory.NOTIFICATION_PREF,
})
_SELF_GATED_VALUES = frozenset(i.value for i in SELF_GATED_INTENTS)


def intent_refusal_message(permissions: Optional[Dict[str, Any]]) -> str:
    """The one source of intent-refusal wording: public, off-stay SMS,
    degraded, or guest."""
    if is_public_audience(permissions):
        return PUBLIC_INTENT_REFUSAL
    if is_stay_read_only(permissions):
        return STAY_READ_ONLY_REFUSAL
    if (permissions or {}).get("mode") == "degraded":
        return DEGRADED_INTENT_REFUSAL
    return GUEST_INTENT_REFUSAL


def intent_gate_refusal(intent: Any, permissions: Optional[Dict[str, Any]]) -> Optional[str]:
    """The refusal text when ``intent`` isn't allowed, else None. Pure.

    Exempt: no intent and UNKNOWN (chit-chat reaches only the audience's
    already-narrowed tools). SELF_GATED_INTENTS are left to their node
    (which refuses before any dispatch with a domain-specific message),
    except for the public audience and an SMS from outside the current
    stay: they never enter those nodes at all, since some of them answer
    house-state reads before their own check.
    Every entry path asks this before routing: the graph router, the
    streaming runner and /query's post-graph check.
    """
    if intent is None:
        return None
    value = getattr(intent, "value", intent)
    if value == IntentCategory.UNKNOWN.value:
        return None
    if is_public_audience(permissions):
        return None if value in PUBLIC_ALLOWED_INTENTS else PUBLIC_INTENT_REFUSAL
    subject = intent if hasattr(intent, "value") else SimpleNamespace(value=str(value))
    if is_stay_read_only(permissions):
        return None if check_intent_permission(subject, permissions or {}) else STAY_READ_ONLY_REFUSAL
    if value in _SELF_GATED_VALUES:
        return None
    if check_intent_permission(subject, permissions or {}):
        return None
    return intent_refusal_message(permissions)


def record_intent_gate_refusal(intent: Any, permissions: Optional[Dict[str, Any]]) -> None:
    audience = "public" if is_public_audience(permissions) else (permissions or {}).get("mode", "unknown")
    value = getattr(intent, "value", intent)
    intent_gate_refused_total.labels(audience=audience, intent=str(value)).inc()
    logger.info("intent_gate_refused", audience=audience, intent=str(value))


def permission_refusal_message(
    domains: Iterable[str],
    scope: "PermissionScope",
    *,
    partial: bool = False,
) -> str:
    """A TTS-safe refusal for a denied HA write. Never includes entity ids.

    ``domains`` should exclude any denial whose reason is
    "halted_after_denial" -- the latch's downstream denials describe the
    scope being closed, not a fresh reason worth naming. Phrasing is keyed
    on ``scope.permissions["mode"]`` (the normalized permission mode, which
    can be "degraded" even when ``scope.mode`` reports "owner" during a D4
    outage), not ``scope.mode`` itself.
    """
    if scope is not None and scope.read_only:
        return READ_ONLY_REFUSAL
    noun = _noun_for_domains(domains)
    if scope is not None and is_public_audience(scope.permissions):
        if partial:
            return f"I did part of that, but I can't control the {noun} from here."
        return f"Sorry, I can't control the {noun} from here."
    perm_mode = scope.permissions.get("mode") if scope and scope.permissions else "guest"
    if perm_mode == "degraded":
        if partial:
            return f"I did part of that, but I can't control the {noun} right now because I couldn't verify permissions."
        return f"Sorry, I can't control the {noun} right now because I couldn't verify permissions."
    if partial:
        return f"I did part of that, but I can't control the {noun} in guest mode."
    return f"Sorry, I can't control the {noun} in guest mode."


# D20: the phrase for a denial reason that is about HOW a door was asked for,
# not about who may touch it. A refusal from the guard (or sequence
# pre-authorization, or the agent) stores no pending question, so these state
# the requirement and never ask "Which one?"; only the controller's own
# which-door question (room_choice) asks.
ONE_DOOR_REFUSAL = "I can unlock or open one door at a time. Name the door, for example 'unlock the front door'."
TEMPLATE_REFUSAL = "I can't set that up with a template."
# The first reason present wins when one turn is denied for several.
_DOOR_REASON_ORDER = (
    OPENING_NEEDS_NAMED_TARGET,
    UNLOCK_NEEDS_ONE_LOCK,
    ONE_OPENING_PER_REQUEST,
    TEMPLATED_SERVICE,
)
_DOOR_REASON_PHRASES: Dict[str, str] = {
    OPENING_NEEDS_NAMED_TARGET: ONE_DOOR_REFUSAL,
    UNLOCK_NEEDS_ONE_LOCK: ONE_DOOR_REFUSAL,
    ONE_OPENING_PER_REQUEST: ONE_DOOR_REFUSAL,
    TEMPLATED_SERVICE: TEMPLATE_REFUSAL,
}


def _door_reason_phrase(reasons: Iterable[str], scope: Optional["PermissionScope"]) -> Optional[str]:
    """The phrase for the first door reason present, or None. A read-only,
    public, degraded or guest scope never gets one: that refusal outranks
    these, so a guest hears the guest refusal and not "name the door"."""
    if scope is None or scope.read_only:
        return None
    permissions = scope.permissions or {}
    if is_public_audience(permissions) or is_stay_read_only(permissions) or permissions.get("mode") != "owner":
        return None
    present = set(reasons)
    for reason in _DOOR_REASON_ORDER:
        if reason in present:
            return _DOOR_REASON_PHRASES[reason]
    return None


def refusal_message(denials: Iterable[HADenial], scope: Optional["PermissionScope"], *, partial: bool = False) -> str:
    """The one refusal wording for the denials a dispatch recorded.

    ``denials`` is the slice of ``scope.denials`` recorded since the dispatch
    began. A "halted_after_denial" entry only says the scope was closed, so
    it is never a reason. Precedence (D20): read-only, public, degraded and
    guest wording first; then the door reasons in ``_DOOR_REASON_ORDER``;
    anything else is ``permission_refusal_message`` over the denied domains.
    """
    live = [d for d in denials if d.reason != "halted_after_denial"]
    phrase = _door_reason_phrase((d.reason for d in live), scope)
    if phrase is not None:
        return phrase
    return permission_refusal_message(tuple(d.domain for d in live), scope, partial=partial)


def sequence_refusal_message(decision: HAWriteDecision, scope: "PermissionScope") -> str:
    """D21: refusal text for a sequence denied at pre-authorization time
    (before scheduling) -- distinct phrasing from permission_refusal_message
    since nothing has been attempted yet ("schedule", not "control").
    A door reason (D20) uses its fixed phrase unless the scope outranks it.
    """
    door_phrase = _door_reason_phrase((decision.reason,), scope)
    if door_phrase is not None:
        return door_phrase
    domain = None
    if decision.denied_targets:
        first = decision.denied_targets[0]
        domain = first.split(".")[0] if "." in first else first
    noun = _noun_for_domains([domain] if domain else [])
    if scope is not None and is_public_audience(scope.permissions):
        return f"Sorry, I can't schedule that from here -- it includes the {noun}."
    perm_mode = scope.permissions.get("mode") if scope and scope.permissions else "guest"
    if perm_mode == "degraded":
        return f"Sorry, I can't schedule that right now because I couldn't verify permissions -- it includes the {noun}."
    return f"Sorry, I can't schedule that in guest mode -- it includes the {noun}."


# ---------------------------------------------------------------------------
# PermissionEnforcingHAClient (D1, D20)
# ---------------------------------------------------------------------------

# D14 C: how a cover is classified before an opening call is judged.
COVER_CLASS_READ_TIMEOUT_SECONDS = 5.0
MAX_CLASSIFIED_COVERS = 16


def _record_denial(
    scope: PermissionScope, domain: str, service: str, targets: Iterable[str], reason: str
) -> None:
    targets = tuple(targets)
    scope.denials.append(HADenial(domain=domain, service=service, targets=targets, reason=reason))
    # The log carries the reason, the domain and how many targets, never
    # which ones, and no session id (the denial record on the scope keeps the
    # targets in memory for the caller).
    logger.warning(
        "ha_write_denied",
        domain=domain,
        service=service,
        target_count=len(targets),
        scope_mode=scope.mode,
        permissions_mode=scope.permissions.get("mode"),
        reason=reason,
        request_id=scope.request_id,
    )
    ha_write_denied_total.labels(domain=domain, scope_mode=scope.mode).inc()


def record_precheck_denial(
    scope: Optional[PermissionScope],
    domain: str,
    service: str,
    targets: Iterable[str],
    reason: str = "entity_or_domain_denied",
) -> None:
    """Record a denial a caller found before dispatch, without halting the scope.

    Same record, log line and ``ha_write_denied_total`` increment as the
    guard's own denial, but the scope stays open and nothing is raised: the
    caller drops the denied targets and still writes the permitted ones, which
    a halted scope would refuse. A ``None`` scope is a no-op.
    """
    if scope is None:
        return
    _record_denial(scope, domain, service, targets, reason)


class PermissionEnforcingHAClient:
    """Wraps a HomeAssistantClient (or any compatible object) and
    authorizes every write against the current request's PermissionScope
    before forwarding it to the inner client.

    Intercepts all four HomeAssistantClient write methods: ``call_service``
    (Pass A) plus ``create_automation``/``delete_automation``/
    ``disable_automation`` (Pass B, via ``authorize_automation_config`` for
    create and the ``automation.<id>`` entity check for delete/disable).
    Wired into main.py's lifespan and every HA holder constructor in Pass B
    (``ensure_permission_enforcing`` is idempotent, so double-wrapping is a
    no-op).

    The read allowlist is deliberately narrow: only ``get_state``,
    ``get_states``, ``close``, and ``is_configured`` pass
    through. Any other attribute access (``_channel``, an unintercepted write
    method) raises AttributeError -- the authenticated channel is never
    reachable through the guard. ``get_states`` is the bulk ``/api/states``
    read used by the entity manager and music-player-discovery callers;
    there is no unwrapped client reference anywhere in ``src/orchestrator``.
    """

    _athena_ha_guard = _GUARD_SENTINEL

    _READ_ALLOWLIST = frozenset({"get_state", "get_states", "close", "is_configured"})

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in self._READ_ALLOWLIST:
            return getattr(self._inner, name)
        raise AttributeError(
            f"PermissionEnforcingHAClient does not expose '{name}'. Only "
            f"{sorted(self._READ_ALLOWLIST)} pass through unauthorized; "
            "writes go through call_service (and, from Pass B, "
            "create_automation/delete_automation/disable_automation)."
        )

    def _resolve_scope(self) -> PermissionScope:
        scope = current_ha_scope()
        if scope is None:
            # D3: a bare guard call with no open scope (e.g. a call made
            # before the caller opened one) gets a fresh throwaway baseline
            # scope used only for this decision and its log -- never a
            # process-global scope object.
            scope = PermissionScope(permissions=degraded_permissions(), mode="system")
        return scope

    def _deny(self, scope: PermissionScope, domain: str, service: str, targets: Tuple[str, ...], reason: str) -> None:
        scope.halted = True
        _record_denial(scope, domain, service, targets, reason)
        raise HAWritePermissionDenied(f"{domain}.{service} denied ({reason})", reason=reason)

    async def _cover_is_access(self, entity_id: str) -> bool:
        """Whether a cover is a way in. Fails closed: a failed or malformed
        read, or a missing device class, means it is."""
        try:
            state = await asyncio.wait_for(self._inner.get_state(entity_id), COVER_CLASS_READ_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("cover_class_read_failed", error_class=type(exc).__name__)
            return True
        attributes = state.get("attributes") if isinstance(state, Mapping) else None
        device_class = attributes.get("device_class") if isinstance(attributes, Mapping) else None
        return not (
            isinstance(device_class, str) and device_class.strip().lower() in NON_ACCESS_DEVICE_CLASSES
        )

    async def _classify_covers(self, cover_ids: Iterable[str]) -> Dict[str, bool]:
        """One read per cover (D14 C step 1), before anything is checked. A
        cover past the read cap is left out of the map, and so counts as a
        way in."""
        ids = tuple(dict.fromkeys(cover_ids))[:MAX_CLASSIFIED_COVERS]
        if not ids:
            return {}
        return dict(zip(ids, await asyncio.gather(*(self._cover_is_access(i) for i in ids))))

    def _record_openings(
        self, scope: PermissionScope, domain: str, service: str, doors: Tuple[str, ...]
    ) -> None:
        """D14 C step 2-3. Called with no ``await`` between it and the check
        that precedes it, and before the write is awaited: the check, the
        record and the dispatch are one step as far as other tasks go, so two
        concurrent opens can't both see an empty set. A failed write keeps
        its ids recorded."""
        if not doors:
            return
        if len(scope.opened | set(doors)) > 1:
            self._deny(scope, domain, service, doors, ONE_OPENING_PER_REQUEST)
        scope.opened.update(doors)

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        scope = self._resolve_scope()
        if scope.read_only:
            self._deny(scope, domain, service, tuple(_extract_targets(domain, service_data)), "read_only_scope")
        if scope.halted:
            self._deny(scope, domain, service, (), "halted_after_denial")
        covers = opening_cover_targets(domain, service, service_data)
        cover_access = await self._classify_covers(covers) if covers else {}
        # No await from here to the inner call (D14 C).
        if scope.halted:
            self._deny(scope, domain, service, (), "halted_after_denial")
        decision = authorize_ha_write(domain, service, service_data, scope.permissions, cover_access=cover_access)
        if not decision.allowed:
            self._deny(scope, domain, service, decision.denied_targets, decision.reason)
        self._record_openings(scope, domain, service, opening_ids(domain, service, service_data, cover_access))
        scope.allowed_writes += 1
        return await self._inner.call_service(domain, service, service_data)

    async def create_automation(self, automation_id: str, config: Dict[str, Any]) -> bool:
        scope = self._resolve_scope()
        if scope.read_only:
            self._deny(scope, "automation", "create", (automation_id,), "read_only_scope")
        if scope.halted:
            self._deny(scope, "automation", "create", (), "halted_after_denial")
        covers = automation_opening_cover_targets(config)
        cover_access = await self._classify_covers(covers) if covers else {}
        # No await from here to the inner call (D14 C).
        if scope.halted:
            self._deny(scope, "automation", "create", (), "halted_after_denial")
        decision = authorize_automation_config(automation_id, config, scope.permissions, cover_access=cover_access)
        if not decision.allowed:
            self._deny(scope, "automation", "create", decision.denied_targets, decision.reason)
        self._record_openings(scope, "automation", "create", automation_opening_ids(config, cover_access))
        scope.allowed_writes += 1
        return await self._inner.create_automation(automation_id, config)

    async def delete_automation(self, automation_id: str) -> bool:
        scope = self._resolve_scope()
        if scope.read_only:
            self._deny(scope, "automation", "delete", (automation_id,), "read_only_scope")
        if scope.halted:
            self._deny(scope, "automation", "delete", (), "halted_after_denial")
        decision = authorize_ha_write(
            "automation", "delete", {"entity_id": f"automation.{automation_id}"}, scope.permissions
        )
        if not decision.allowed:
            self._deny(scope, "automation", "delete", decision.denied_targets, decision.reason)
        scope.allowed_writes += 1
        return await self._inner.delete_automation(automation_id)

    async def disable_automation(self, automation_id: str) -> bool:
        scope = self._resolve_scope()
        if scope.read_only:
            self._deny(scope, "automation", "disable", (automation_id,), "read_only_scope")
        if scope.halted:
            self._deny(scope, "automation", "disable", (), "halted_after_denial")
        decision = authorize_ha_write(
            "automation", "disable", {"entity_id": f"automation.{automation_id}"}, scope.permissions
        )
        if not decision.allowed:
            self._deny(scope, "automation", "disable", decision.denied_targets, decision.reason)
        scope.allowed_writes += 1
        return await self._inner.disable_automation(automation_id)


def ensure_permission_enforcing(client: Any) -> Any:
    """Idempotently wrap `client` in a PermissionEnforcingHAClient.

    None -> None. Already the guard (or a proxy that forwards attribute
    access to it, e.g. _HAClientProxy) -> the same object, by identity via
    `_athena_ha_guard is _GUARD_SENTINEL` -- a plain MagicMock can't match
    this because it manufactures a *new* mock attribute rather than the
    real sentinel object. Anything else -> wrapped.
    """
    if client is None:
        return None
    if getattr(client, "_athena_ha_guard", None) is _GUARD_SENTINEL:
        return client
    return PermissionEnforcingHAClient(client)


# ============================================================================
# ATHENA-69 Pass C (D6, D7): server-derived mode at every entry point
# ============================================================================

async def get_guest_permissions() -> Dict[str, Any]:
    """D6: fetch guest permissions explicitly from the mode service,
    regardless of the household's current (possibly owner) mode. On any
    mismatch, error, or timeout, falls back to the floored baseline --
    never returns owner. No owner variant exists; only "guest" can be
    requested.
    """
    try:
        response = await mode_client.get("/mode/permissions", params={"mode": "guest"})
        response.raise_for_status()
        data = response.json()
        if data.get("mode") != "guest":
            raise ValueError("mode service returned non-guest permissions for a guest request")
        return normalize_permissions(data)
    except Exception as e:
        logger.warning(f"get_guest_permissions falling back to floored baseline: {e}")
        baseline = guest_baseline()
        fallback_floor = degraded_permissions()["restricted_entities"]
        merged_entities = list(baseline["restricted_entities"])
        for pattern in fallback_floor:
            if pattern not in merged_entities:
                merged_entities.append(pattern)
        return normalize_permissions({**baseline, "restricted_entities": merged_entities})


@dataclass(frozen=True)
class RequestAuthorization:
    """The resolved mode/permissions for one request, from
    resolve_request_authorization."""
    mode: str
    permissions: Dict[str, Any]
    server_mode: str
    degraded: bool
    escalation_ignored: bool
    mode_info: Dict[str, Any]
    knowledge_audience: KnowledgeAudience


def _knowledge_audience(
    effective_mode: str, degraded: bool, public: bool, *,
    owner_caller: bool = False, owner_proven: bool = False, guest_verified: bool = True,
) -> KnowledgeAudience:
    """The base-knowledge audience for a resolved request. A mode other than
    owner/guest (the mode service answered something unexpected) is treated as
    unknown and sees nothing. The only place a KnowledgeAudience is built with
    owner flags: they come from server inputs (the authenticated hop, the
    server's mode), never from a request field."""
    if effective_mode not in ("owner", "guest"):
        return KnowledgeAudience.UNRESOLVED
    return KnowledgeAudience(
        mode=effective_mode, degraded=degraded, public=public,
        owner_caller=owner_caller, owner_proven=owner_proven, guest_verified=guest_verified,
    )


async def resolve_request_authorization(
    request_mode: Optional[str],
    guest_info: Optional[Dict[str, Any]],
    caller_trust: Optional[str] = None,
    *,
    service_authenticated: bool,
    sms_stay_phase: Optional[str] = None,
) -> RequestAuthorization:
    """The single mode/permissions resolution path for every orchestrator
    entry point (D7, D6, D5).

    Effective mode is "guest" if `guest_info` is present (device-
    fingerprinted guest), `request_mode == "guest"`, or the server's own
    mode is "guest"; otherwise it's the server's mode. `request_mode ==
    "owner"` never changes anything -- narrowing only, never escalation.

    Guest permissions: the server's own permissions when the server is
    already in guest mode (no extra fetch needed); the D4 degraded
    baseline (no network call) when the mode service is degraded;
    otherwise an explicit `get_guest_permissions()` fetch (a narrowing
    guest -- fingerprinted or request-asserted -- while the house is
    nominally owner needs the REAL guest allowlist, not the owner
    permissions the server returned for its own mode).

    A public caller (``is_public_caller(caller_trust)``) always gets
    ``mode="guest"`` with ``public_permissions()``, whatever the server's
    mode, the guest profile or a degraded mode service say. The mode
    service is still read, for ``mode_info`` only.

    An SMS (``caller_trust == "sms"``) from outside the current stay
    (``sms_stay_phase`` anything but "current", missing or unknown
    included: fail closed) gets off_stay_overlay on top: answer-only.
    ``sms_stay_phase`` comes from the admin-backend webhook's context; the
    caller is trusted server code for it, like caller_trust. It's ignored
    for every other caller.

    Owner proof (``KnowledgeAudience``): ``owner_caller`` needs
    ``caller_trust == "web_owner"`` AND ``service_authenticated is True`` (the
    request carried a valid X-Service-Key) and a non-public caller.
    ``owner_proven`` additionally needs the effective mode and the server's
    own mode both ``owner`` and a healthy mode service. During a stay with the
    PIN override active the mode service reports owner, so a signed-in owner
    is proven then: the proof is the Bearer, not the house state.
    """
    mode_info = await get_current_mode()
    server_mode = mode_info.get("mode", "owner")
    degraded = bool(mode_info.get("degraded", False))
    owner_caller = (
        caller_trust == OWNER_PROOF_TRUST
        and service_authenticated is True
        and not is_public_caller(caller_trust)
    )

    if is_public_caller(caller_trust):
        escalation_ignored = request_mode == "owner"
        if escalation_ignored:
            logger.info(
                "request_mode_escalation_ignored",
                request_mode=request_mode,
                effective_mode="guest",
                server_mode=server_mode,
            )
        logger.info("public_audience_resolved", server_mode=server_mode, degraded=degraded)
        return RequestAuthorization(
            mode="guest",
            permissions=normalize_permissions(public_permissions()),
            server_mode=server_mode,
            degraded=degraded,
            escalation_ignored=escalation_ignored,
            mode_info=mode_info,
            knowledge_audience=_knowledge_audience("guest", degraded, public=True),
        )

    effective_mode = "guest" if (guest_info or request_mode == "guest" or server_mode == "guest") else server_mode

    escalation_ignored = request_mode == "owner" and effective_mode != "owner"
    if escalation_ignored:
        logger.info(
            "request_mode_escalation_ignored",
            request_mode=request_mode,
            effective_mode=effective_mode,
            server_mode=server_mode,
        )

    if effective_mode == "guest":
        if degraded:
            permissions = normalize_permissions({**degraded_permissions(), "mode": "guest"})
        elif server_mode == "guest":
            permissions = normalize_permissions(mode_info.get("permissions", {}))
        else:
            permissions = await get_guest_permissions()
    else:
        permissions = normalize_permissions(mode_info.get("permissions", {}))

    if caller_trust == "sms" and sms_stay_phase not in SMS_WRITE_STAY_PHASES:
        permissions = normalize_permissions(off_stay_overlay(permissions, sms_stay_phase))
        logger.info("sms_stay_phase_read_only", phase=sms_stay_phase or "unknown")

    owner_proven = owner_caller and effective_mode == "owner" and server_mode == "owner" and not degraded
    logger.info("owner_proof_resolved", owner_caller=owner_caller, owner_proven=owner_proven)
    return RequestAuthorization(
        mode=effective_mode,
        permissions=permissions,
        server_mode=server_mode,
        degraded=degraded,
        escalation_ignored=escalation_ignored,
        mode_info=mode_info,
        knowledge_audience=_knowledge_audience(
            effective_mode, degraded, public=False, owner_caller=owner_caller, owner_proven=owner_proven,
            guest_verified=bool(guest_info) or server_mode == "guest",
        ),
    )
