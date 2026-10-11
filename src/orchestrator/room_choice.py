"""The "which one?" question and the pending that carries it to the answer.

When a command names a room only in part ("bathroom" for the north, south and
lower bathrooms), or names none and the speaker's room isn't known, the
controller asks instead of guessing. ``ask`` returns the question and, on a
write turn, records a ``RoomChoice`` on the request's permission scope; the
route stores it as ``awaiting_room_choice`` beside the pending write
confirmation (same fingerprint, expiry and one-use nonce rules). A read-only
turn records a ``Clarification`` instead, answered through the existing
state-question seam, so a question is never the start of a write.

R2-C3: imports ``mode_permission.current_ha_scope``, ``room_context`` and
``shared.config`` only.

A reply is read strictly (``match_reply``): the reply's words, less filler,
must all belong to one candidate's name. A question or command cancels the
pending and runs fresh.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from orchestrator import room_context
from orchestrator.mode_permission import current_ha_scope

PENDING_KEY = "awaiting_room_choice"
TTL = 60
KIND_WHICH = "which"
KIND_ROOM = "room"

_now = time.time


@dataclass(frozen=True)
class RoomChoice:
    """``intent`` is the JSON-safe intent to complete; ``candidates`` are the
    ``(key, label)`` rooms the question named (empty for "which room?");
    ``kind`` is ``which`` or ``room``."""
    intent: Mapping[str, Any]
    candidates: Tuple[Tuple[str, str], ...]
    phrase: str
    kind: str


@dataclass(frozen=True)
class Clarification:
    candidates: Tuple[Tuple[str, str], ...]
    phrase: str
    text: str


def storable_intent(intent: Mapping[str, Any]) -> Dict[str, Any]:
    """The intent as plain JSON: any key whose value isn't JSON-safe (an
    object that crept in) is dropped rather than stored as something that
    comes back as the wrong type."""
    kept: Dict[str, Any] = {}
    for key, value in dict(intent).items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        kept[str(key)] = value
    return kept


def ask(intent: Mapping[str, Any], decision, *, record: bool = True, read: bool = False) -> str:
    """The question for an ``AskWhich`` or ``AskRoom``. On a write turn, and
    only when ``record`` (a turn with the user's own words to replay), the
    choice is recorded on the scope for the route to store; a read-only turn
    records a ``Clarification`` (as does any ``read`` command, so a question
    is never the start of a write). Nothing is recorded for a caller with no
    scope."""
    text = room_context.ask_text(decision)
    scope = current_ha_scope()
    if scope is None:
        return text
    if isinstance(decision, room_context.AskWhich):
        candidates = tuple((c.key, c.label) for c in decision.candidates)
        phrase, kind = decision.phrase, KIND_WHICH
    else:
        candidates, phrase, kind = (), "", KIND_ROOM
    if scope.read_only or read:
        scope.room_clarification = Clarification(candidates, phrase, text)
    elif record:
        scope.room_choice = RoomChoice(storable_intent(intent), candidates, phrase, kind)
    return text


def take() -> Optional[RoomChoice]:
    """Pop the choice this turn asked (None when it asked none)."""
    scope = current_ha_scope()
    if scope is None:
        return None
    choice, scope.room_choice = scope.room_choice, None
    return choice


def take_clarification() -> Optional[Clarification]:
    """Pop the clarification a read-only turn asked."""
    scope = current_ha_scope()
    if scope is None:
        return None
    clarification, scope.room_clarification = scope.room_clarification, None
    return clarification


def new_pending(choice: RoomChoice, *, fingerprint: Optional[str], nonce: str, query: str) -> Dict[str, Any]:
    """The JSON stored under ``awaiting_room_choice``."""
    return {
        "intent": dict(choice.intent),
        "candidates": [list(c) for c in choice.candidates],
        "phrase": choice.phrase,
        "kind": choice.kind,
        "query": query,
        "fingerprint": fingerprint,
        "nonce": nonce,
        "expires_at": _now() + TTL,
        "reasked": False,
    }


def is_live(pending: Mapping[str, Any]) -> bool:
    return float(pending.get("expires_at") or 0) > _now()


def candidate_refs(pending: Mapping[str, Any]) -> Tuple[room_context.RoomRef, ...]:
    return tuple(room_context.RoomRef(str(c[0]), str(c[1])) for c in (pending.get("candidates") or []) if len(c) >= 2)


def read_reply(pending: Mapping[str, Any], reply: str, directory) -> room_context.MatchResult:
    """The pending's answer in ``reply``. For "which one?" the strict token
    subset of exactly one candidate; for "which room?" a single room the
    directory names, said on its own (a command or other words make it no
    answer)."""
    if pending.get("kind") == KIND_WHICH:
        return room_context.match_reply(reply, candidate_refs(pending))
    words = room_context.tokens(reply)
    mention = room_context.find_room_mention(reply, directory)
    if mention.kind == "none" or mention.in_exclusion:
        return room_context.MatchResult("none")
    covered = set(range(mention.start, mention.end))
    leftover = [w for i, w in enumerate(words) if i not in covered and w not in room_context._REPLY_FILLER]
    if leftover or any(w in room_context._COMMAND_VERBS for w in words):
        return room_context.MatchResult("none")
    if len(mention.candidates) == 1:
        return room_context.MatchResult("one", mention.candidates[0], mention.candidates)
    return room_context.MatchResult("several", None, mention.candidates)


def reply_fits(pending: Optional[Mapping[str, Any]], reply: str, directory, *, is_question: bool) -> bool:
    """Whether ``reply`` plausibly answers a live pending: an answer, or an
    ambiguous one (to be re-asked). A question (``is_question``, decided by
    the caller from ``classify_utterance``) never fits."""
    if not pending or is_question or not is_live(pending):
        return False
    return read_reply(pending, reply, directory).kind in ("one", "several")
