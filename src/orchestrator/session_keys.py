"""session_keys.py -- one id -> class function, and every storage key derived from it.

Stdlib only; imports nothing from ``orchestrator.main`` (R2-C3), so the session
manager, helpers, main and context storage all share it.

A session id carries its caller class in its prefix: ``pub-`` for the public
audience, ``own-`` for a server-proven owner, ``gst-`` for a guest-audience
turn (the server's own guest mode or a device-matched stay), anything else for
the household.
``id_class`` is the only place that reads the prefix. Redis keys for a
session and for its follow-up context both come from that one answer, so an
owner session and its context live in their own namespaces
(``athena:owner_session:`` / ``athena:owner_context:``). A process that
predates this module reads only the ``athena:session:`` / ``athena:context:``
namespaces and therefore can't load an owner session or its context at all.

Nothing outside this module builds a session or context key: a drift guard
(tests/unit/test_session_key_drift.py) forbids it.
"""
from __future__ import annotations

import uuid
from typing import Optional

CALLER_CLASS_PUBLIC = "public"
CALLER_CLASS_OWNER = "owner"
CALLER_CLASS_GUEST = "guest"
CALLER_CLASS_OTHER = "other"

PUBLIC_SESSION_PREFIX = "pub-"
OWNER_SESSION_PREFIX = "own-"
GUEST_SESSION_PREFIX = "gst-"

_ID_PREFIXES = {
    CALLER_CLASS_PUBLIC: PUBLIC_SESSION_PREFIX,
    CALLER_CLASS_OWNER: OWNER_SESSION_PREFIX,
    CALLER_CLASS_GUEST: GUEST_SESSION_PREFIX,
}

# class -> (session key prefix, conversation-context key prefix)
_NAMESPACES = {
    CALLER_CLASS_PUBLIC: ("athena:session:", "athena:context:"),
    CALLER_CLASS_OWNER: ("athena:owner_session:", "athena:owner_context:"),
    CALLER_CLASS_GUEST: ("athena:guest_session:", "athena:guest_context:"),
    CALLER_CLASS_OTHER: ("athena:session:", "athena:context:"),
}

# Redis sorted-set index of OpenAI-compatible session ids (ATHENA-88).
OAI_SESSION_INDEX_KEY = "athena:session:oai_index"


def id_class(session_id: Optional[str]) -> str:
    """The caller class an id belongs to, from its prefix alone."""
    sid = session_id or ""
    if sid.startswith(OWNER_SESSION_PREFIX):
        return CALLER_CLASS_OWNER
    if sid.startswith(PUBLIC_SESSION_PREFIX):
        return CALLER_CLASS_PUBLIC
    if sid.startswith(GUEST_SESSION_PREFIX):
        return CALLER_CLASS_GUEST
    return CALLER_CLASS_OTHER


def new_session_id(caller_class: str) -> str:
    """A fresh id carrying ``caller_class``'s prefix."""
    return f"{_ID_PREFIXES.get(caller_class, '')}{uuid.uuid4()}"


def usable_session_id(session_id: Optional[str], caller_class: str) -> str:
    """``session_id`` when its prefix matches ``caller_class``, else a fresh id.

    A caller never adopts (or overwrites) an id that belongs to another class.
    """
    if session_id and id_class(session_id) == caller_class:
        return session_id
    return new_session_id(caller_class)


def guest_session_id(session_id: str) -> str:
    """The guest-class id for a presented id: the id itself when it is already a
    guest id, else the same id under the guest prefix.

    Callers that fix their own session id (SMS, a voice satellite) keep one
    conversation per audience this way: the household turn uses ``X``, the guest
    turn ``gst-X``, deterministically, with no cooperation from the caller.
    """
    if id_class(session_id) == CALLER_CLASS_GUEST:
        return session_id
    if id_class(session_id) != CALLER_CLASS_OTHER:
        return new_session_id(CALLER_CLASS_GUEST)  # an own-/pub- id is never re-labelled
    return GUEST_SESSION_PREFIX + session_id


def session_storage_key(session_id: str) -> str:
    return _NAMESPACES[id_class(session_id)][0] + session_id


def context_storage_key(session_id: str) -> str:
    return _NAMESPACES[id_class(session_id)][1] + session_id
