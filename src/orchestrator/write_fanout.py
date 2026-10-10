"""ATHENA-128 Phase 4 -- the write fan-out confirmation gate (D7-D10, D16).

Bounds how many distinct Home Assistant entities a single utterance may
write without the utterance naming that scope explicitly ("turn off all
the office lights") or being an imperative under the hard limit. Above
the bound, the caller gets back a confirmation prompt (when the surface
and session can carry a follow-up, D14/D16) or an exact rewording
(everyone else) instead of executing.

R2-C3: imports `utterance_kind`, `metrics`, `shared.config`,
`mode_permission` (`noun_for_domains`, `current_ha_scope`) and the shared
yes/no vocabulary (`shared.fast_path_vocab`) -- never `orchestrator.main`.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence, Tuple

from shared.config import get_config
from shared.fast_path_vocab import (  # noqa: F401  (re-exported: the vocabulary's old home)
    BARE_AFFIRMATION_RE,
    BARE_NEGATION_RE,
    normalize_reply,
)
from orchestrator.metrics import ha_write_fanout_confirm_total
from orchestrator.mode_permission import current_ha_scope, noun_for_domains
from orchestrator.utterance_kind import UtteranceKind, classify_utterance


@dataclass(frozen=True)
class PlannedWrite:
    domain: str
    service: str
    entity_ids: Tuple[str, ...]


@dataclass(frozen=True)
class FanoutBlock:
    writes: Tuple[PlannedWrite, ...]
    unbounded: bool
    room: Optional[str] = None


_SCOPE_CUE_RE = re.compile(r"\b(?:all|every|everything|whole|entire|house)\b", re.IGNORECASE)


# 5.3 rules 4/5, matched against normalize_reply() output; the patterns and
# the normalization live in shared.fast_path_vocab, which the deterministic
# fast path must also exclude.


_VERB_FOR_SERVICE = {
    "create": "create",
    "delete": "delete",
    "notify": "send",
    "turn_on": "turn on",
    "turn_off": "turn off",
    "lock": "lock",
    "unlock": "unlock",
    "open_cover": "open",
    "close_cover": "close",
}
# Domains whose "turn_on" is an activation, not a power change.
_ACTIVATED_DOMAINS = frozenset({"scene", "script"})


# Singular/plural for a counted, single-domain target ("1 light", "7
# fans"); anything else uses the guard's domain nouns.
_COUNTED_NOUNS = {
    "light": ("light", "lights"),
    "lock": ("lock", "locks"),
    "cover": ("cover", "covers"),
    "fan": ("fan", "fans"),
    "switch": ("switch", "switches"),
    "media_player": ("media player", "media players"),
    "climate": ("thermostat", "thermostats"),
    "select": ("bed warmer", "bed warmers"),
    "input_boolean": ("motion setting", "motion settings"),
    "scene": ("scene", "scenes"),
    "script": ("routine", "routines"),
    "automation": ("automation", "automations"),
    "notify": ("notification", "notifications"),
}


def _counted_noun(n: int, domains: Iterable[str]) -> str:
    unique = list(dict.fromkeys(domains))
    if len(unique) == 1 and unique[0] in _COUNTED_NOUNS:
        singular, plural = _COUNTED_NOUNS[unique[0]]
        return singular if n == 1 else plural
    return noun_for_domains(unique)


def _verb_for(service: str, domain: Optional[str] = None) -> str:
    if domain in _ACTIVATED_DOMAINS:
        return "activate"
    # Anything else (set_temperature, a bed-warmer level, a motion
    # override) reads as a change to the device.
    return _VERB_FOR_SERVICE.get(service, "change")


def caller_fingerprint(
    caller_trust: Optional[str], device_id: Optional[str], room: Optional[str], permissions_mode: Optional[str]
) -> Optional[str]:
    """D13: a server-side-only fingerprint binding a pending confirmation
    to the caller that created it. Never used for authorization.

    None when the caller supplied no identity at all (neither a trust tag
    nor a device id): room and mode alone are shared by everyone in the
    house, so such a caller is treated as a non-follow-up surface."""
    import hashlib

    if not caller_trust and not device_id:
        return None
    parts = "|".join([caller_trust or "", device_id or "", room or "", permissions_mode or ""])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


@contextlib.contextmanager
def pending_carrier(scope, enabled: bool):
    """D16: the only setter of `PermissionScope.can_carry_pending`. Sets
    the flag for the duration of the block and restores False in
    `finally`, even if the block raises."""
    if scope is None:
        yield
        return
    previous = scope.can_carry_pending
    scope.can_carry_pending = bool(enabled)
    try:
        yield
    finally:
        scope.can_carry_pending = previous if previous else False


@contextlib.contextmanager
def confirmed(entity_ids: Iterable[str]):
    """Marks the given entity ids as confirmed-for-this-call on the
    current scope, so a replay's gate() call for exactly those ids (or a
    bounded subset) proceeds without re-prompting."""
    scope = current_ha_scope()
    if scope is None:
        yield
        return
    previous = scope.confirmed_entity_ids
    scope.confirmed_entity_ids = frozenset(entity_ids)
    try:
        yield
    finally:
        scope.confirmed_entity_ids = previous


def take_block() -> Optional[FanoutBlock]:
    """Pop `scope.fanout_block` (or None if no scope / no block)."""
    scope = current_ha_scope()
    if scope is None:
        return None
    block = scope.fanout_block
    scope.fanout_block = None
    return block


def _has_explicit_scope_cue(original_query: Optional[str], scope_hint: Any) -> bool:
    q = (original_query or "").lower()
    if _SCOPE_CUE_RE.search(q):
        return True
    if isinstance(scope_hint, tuple) and len(scope_hint) == 2:
        kind, value = scope_hint
        if kind == "room_group" and value and str(value).lower() in q:
            return True
        if kind == "multi_room" and value:
            matched = sum(1 for r in value if r and str(r).lower() in q)
            if matched >= 2:
                return True
    return False


def _room_text(room: Optional[str], scope_hint: Any) -> str:
    if room:
        return f" in the {room}"
    if isinstance(scope_hint, tuple) and scope_hint and scope_hint[0] == "room_group":
        return f" in the {scope_hint[1]}"
    if scope_hint == "whole_house":
        return " in the house"
    return ""


def _command_text(write: PlannedWrite, room: Optional[str] = None) -> str:
    verb = _verb_for(write.service, write.domain)
    where = f"{room.replace('_', ' ')} " if room else ""
    if len(write.entity_ids) == 1 and "all" not in write.entity_ids:
        return f"{verb} the {where}{_counted_noun(1, [write.domain])}"
    noun = noun_for_domains([write.domain])
    return f"{verb} all the {where}{noun}"


def rewording(block: FanoutBlock) -> str:
    """A user-facing rewording that would pass the gate. Never ends in
    '?'. One command per planned write, joined with ', then say:'."""
    room = None if block.unbounded else block.room
    commands = [_command_text(w, room) for w in block.writes]
    say = "To do it, say: " + (", then say: ".join(commands)) + "."
    noun = noun_for_domains([w.domain for w in block.writes])
    if block.unbounded:
        lead = f"That would affect all the {noun}."
    else:
        n = sum(len(w.entity_ids) for w in block.writes)
        verb = _verb_for(block.writes[0].service, block.writes[0].domain) if block.writes else "control"
        lead = f"That would {verb} {n} {_counted_noun(n, [w.domain for w in block.writes])}{f' in the {block.room}' if block.room else ''}."
    return f"{lead} {say}"


def _resolve_uk(original_query: Optional[str]):
    scope = current_ha_scope()
    if scope is not None and scope.utterance is not None:
        return scope.utterance
    return classify_utterance(original_query)


def _gate_writes(
    writes: Tuple[PlannedWrite, ...],
    original_query: Optional[str],
    *,
    unbounded: bool,
    scope_hint: Any,
    room: Optional[str],
) -> Optional[str]:
    scope = current_ha_scope()
    uk = _resolve_uk(original_query)

    all_ids = set()
    for w in writes:
        all_ids.update(w.entity_ids)
    is_unbounded = bool(unbounded) or any("all" in w.entity_ids for w in writes)
    domain_for_metric = writes[0].domain if writes else "unknown"

    # Replay bypass (D9): a bounded, already-confirmed subset proceeds
    # without re-prompting. An unbounded block is never bypassed (D7
    # rule 0) -- confirmed() can't make an "all" target safe.
    if scope is not None and scope.confirmed_entity_ids is not None and not is_unbounded:
        if all_ids and all_ids.issubset(scope.confirmed_entity_ids):
            return None
        return _block(scope, writes, room, scope_hint, domain_for_metric, "reasked", "reasked")

    # D7 rule 1: an explicit scope cue in the utterance text always
    # proceeds, evaluated before the unbounded/imperative/threshold
    # checks. Never for a question ("are all the lights on?"): a question
    # reaches a write only when routing was bypassed (kill switch), and
    # "all" there names what is asked about, not what to change.
    if uk.kind != UtteranceKind.STATE_QUESTION and _has_explicit_scope_cue(original_query, scope_hint):
        ha_write_fanout_confirm_total.labels(domain=domain_for_metric, outcome="exempt_scope").inc()
        return None

    # D7 rule 0: an unbounded target that isn't exempt under rule 1 is
    # always refused with the rewording, whatever the surface -- it can
    # never become a confirmable pending.
    if is_unbounded:
        block = FanoutBlock(writes=writes, unbounded=True, room=room)
        if scope is not None:
            scope.fanout_block = block
        ha_write_fanout_confirm_total.labels(domain=domain_for_metric, outcome="reworded").inc()
        return rewording(block)

    n = len(all_ids)

    # A real question never writes silently. It reaches a write only with
    # routing reverted (kill switch), where the limits would let up to t
    # entities change unasked: every write is confirmed (or reworded)
    # instead, so a command misread as a question still works after "yes"
    # or the rewording. Not tied to the limits -- 0/0 doesn't turn it off.
    if uk.kind == UtteranceKind.STATE_QUESTION and n >= 1:
        return _block(scope, writes, room, scope_hint, domain_for_metric, "requested", "reworded")

    cfg = get_config()

    if uk.kind == UtteranceKind.IMPERATIVE:
        hard_limit = cfg.ha_write_fanout_hard_limit
        if hard_limit == 0 or n <= hard_limit:
            ha_write_fanout_confirm_total.labels(domain=domain_for_metric, outcome="exempt_imperative").inc()
            return None
    else:
        threshold = cfg.ha_write_fanout_confirm_threshold
        if threshold == 0 or n <= threshold:
            return None

    return _block(scope, writes, room, scope_hint, domain_for_metric, "requested", "reworded")


def _block(scope, writes, room, scope_hint, domain_for_metric: str, prompt_outcome: str, reword_outcome: str) -> str:
    """Record a bounded block on the scope; return the confirmation prompt
    when the scope can carry a pending (D16), else the rewording."""
    block = FanoutBlock(writes=writes, unbounded=False, room=room)
    if scope is not None:
        scope.fanout_block = block

    if scope is not None and scope.can_carry_pending:
        ha_write_fanout_confirm_total.labels(domain=domain_for_metric, outcome=prompt_outcome).inc()
        n = len({e for w in writes for e in w.entity_ids})
        verb = _verb_for(writes[0].service, writes[0].domain)
        return f"That would {verb} {n} {_counted_noun(n, [w.domain for w in writes])}{_room_text(room, scope_hint)}. Should I go ahead?"
    ha_write_fanout_confirm_total.labels(domain=domain_for_metric, outcome=reword_outcome).inc()
    return rewording(block)


def question_refusal(domain: str, service: str, entity_ids: Iterable[str]) -> Optional[str]:
    """For writers without a pending carrier or a fan-out count (the
    automation agent's tools): the rewording when the request's real
    classification is a STATE_QUESTION, else None -- commands (IMPERATIVE,
    UNKNOWN) pass exactly as before, whatever the limits. A read-only scope
    is left to the permission guard, whose refusal already applies. Records
    no block: the caller returns the text as its result."""
    scope = current_ha_scope()
    if scope is None or scope.read_only or scope.utterance is None:
        return None
    if getattr(scope.utterance, "kind", None) != UtteranceKind.STATE_QUESTION:
        return None
    ids = tuple(entity_ids) or (domain,)
    block = FanoutBlock(writes=(PlannedWrite(domain, service, ids),), unbounded="all" in ids)
    ha_write_fanout_confirm_total.labels(domain=domain, outcome="reworded").inc()
    return rewording(block)


def gate(
    domain,
    service=None,
    entity_ids=None,
    original_query: Optional[str] = None,
    *,
    unbounded: bool = False,
    scope_hint: Any = None,
    room: Optional[str] = None,
) -> Optional[str]:
    """The single-write shorthand. Accepts either
    `gate(domain, service, entity_ids, original_query, ...)` or
    `gate([(domain, service, entity_ids), ...], original_query, ...)`
    (a pre-built writes sequence, for callers that already assembled one).
    Returns None (proceed) or a user-facing answer string (block)."""
    if service is None and entity_ids is None:
        raise TypeError("write_fanout.gate: expected (domain, service, entity_ids, ...)")
    if isinstance(domain, (list, tuple)) and domain and isinstance(domain[0], PlannedWrite):
        writes = tuple(domain)
        original_query = service  # gate(writes, original_query)
        return _gate_writes(writes, original_query, unbounded=unbounded, scope_hint=scope_hint, room=room)
    writes = (PlannedWrite(domain, service, tuple(entity_ids)),)
    return _gate_writes(writes, original_query, unbounded=unbounded, scope_hint=scope_hint, room=room)


def gate_many(
    writes: Sequence[PlannedWrite],
    original_query: Optional[str],
    *,
    unbounded: bool = False,
    scope_hint: Any = None,
    room: Optional[str] = None,
) -> Optional[str]:
    """Multi-write gate (the scene "leaving" fallback: light.turn_off all
    + lock.lock all in one utterance)."""
    return _gate_writes(tuple(writes), original_query, unbounded=unbounded, scope_hint=scope_hint, room=room)
