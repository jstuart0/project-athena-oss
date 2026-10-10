"""Base-knowledge audience tiers and who may hear them.

Stdlib only, pure, no I/O and no logging: orchestrator, directions RAG and
admin-backend all import it, and none of them restates the rules below.

Stored meaning of ``BaseKnowledge.applies_to``:

- ``both``      everyone: guests and household.
- ``guest``     guests only, while a stay is active.
- ``household`` anyone at home when no stay is active (voice satellites, the
                home network, SMS, signed-in members). Visitors at home hear it.
- ``owner``     OWNER ONLY. The stored value ``owner`` is redefined by this
                module: before, it meant "owner mode" (no active stay), which
                every household caller shared. Now it reaches only a
                server-proven owner (``KnowledgeAudience.owner_proven``).
                ``household`` is the new value for the old shared meaning, so
                an old orchestrator that has never heard of it fails closed.

A KnowledgeAudience is never persisted (Redis, cache, session): it is rebuilt
from server inputs on every request.

Any other stored value (for example the legacy ``chat``) is never visible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Optional

KNOWLEDGE_TIERS = ("both", "guest", "household", "owner")
WRITABLE_TIERS = frozenset(KNOWLEDGE_TIERS)
OWNER_TIER = "owner"
OWNER_CATEGORY = "owner"
OWNER_NAME_KEYS = frozenset({"owner_name", "name"})
# The only categories whose owner_name/name rows are read as the owner's name.
NAME_KEY_CATEGORIES = frozenset({"owner", "user"})

_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_]{0,49}")
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")

_MODE_GUEST = "guest"
_MODE_OWNER = "owner"


@dataclass(frozen=True)
class KnowledgeAudience:
    """Who a prompt is being built for, as proven by the server.

    ``owner_caller``: an authenticated owner-signed-in request. ``owner_proven``
    additionally requires owner mode and a healthy mode service. The
    constructor refuses any combination that would let a flag claim more than
    its inputs allow.
    """

    mode: Optional[str]
    degraded: bool
    public: bool
    owner_caller: bool
    owner_proven: bool
    # Guest mode backed by something the server knows: its own guest mode or a
    # device-matched stay. A caller's "mode=guest" hint alone narrows who they
    # are, but doesn't make them a guest, so the guest tier stays hidden.
    guest_verified: bool = True

    UNRESOLVED: ClassVar["KnowledgeAudience"]

    def __post_init__(self) -> None:
        for flag in (self.degraded, self.public, self.owner_caller, self.owner_proven, self.guest_verified):
            if not isinstance(flag, bool):
                raise ValueError("audience flags must be bool")
        if self.mode is not None and self.mode not in (_MODE_GUEST, _MODE_OWNER):
            raise ValueError("audience mode must be None, 'guest' or 'owner'")
        if self.public and self.owner_caller:
            raise ValueError("a public audience cannot be an owner caller")
        if self.owner_proven and not (
            self.owner_caller
            and self.mode == _MODE_OWNER
            and not self.degraded
            and not self.public
        ):
            raise ValueError(
                "owner_proven requires owner_caller, owner mode, a healthy mode service and a non-public audience"
            )

    def visible_tiers(self) -> frozenset:
        if self.public:
            return frozenset()
        if self.mode not in (_MODE_GUEST, _MODE_OWNER):
            return frozenset()
        if self.degraded:
            return frozenset({"both"})
        if self.mode == _MODE_GUEST:
            return frozenset({"both", "guest"}) if self.guest_verified else frozenset({"both"})
        if self.owner_proven:
            return frozenset({"both", "household", "owner"})
        return frozenset({"both", "household"})


KnowledgeAudience.UNRESOLVED = KnowledgeAudience(
    mode=None, degraded=True, public=False, owner_caller=False, owner_proven=False
)


def entry_visible(entry: Mapping[str, Any], tiers: frozenset) -> bool:
    """True only when the entry's ``applies_to`` is a str that is in ``tiers``."""
    tier = entry.get("applies_to")
    return isinstance(tier, str) and tier in tiers


def validate_entry_fields(category: Any, key: Any, applies_to: Any) -> Optional[str]:
    """Return a single-string 422 detail ("<field>: <reason>"), or None if valid."""
    if not isinstance(applies_to, str) or applies_to not in WRITABLE_TIERS:
        return "applies_to: must be one of " + ", ".join(KNOWLEDGE_TIERS)
    if not isinstance(category, str) or not _CATEGORY_RE.fullmatch(category):
        return "category: must be 1-50 characters, lowercase letters, digits and underscores, starting with a letter"
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return "key: must be 1-100 characters of letters, digits and _ . : -, starting with a letter or digit"
    if key in OWNER_NAME_KEYS and category not in NAME_KEY_CATEGORIES:
        return "key: owner_name and name are only valid in the owner or user category"
    if category == OWNER_CATEGORY and applies_to != OWNER_TIER and key not in OWNER_NAME_KEYS:
        return "applies_to: an entry in the owner category must be Owner only, except owner_name and name"
    return None
