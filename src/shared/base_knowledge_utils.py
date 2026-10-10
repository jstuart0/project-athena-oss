"""
Base Knowledge Utilities

Provides functions to format and inject base knowledge context into LLM prompts.
Handles dynamic placeholders like {dynamic:current_date} and {dynamic:current_time}.
"""
import hashlib
import os
from typing import List, Dict, Any, Optional
import structlog
from shared.config import get_config
from shared.knowledge_tiers import OWNER_CATEGORY, OWNER_NAME_KEYS, KnowledgeAudience, entry_visible
from shared.local_time import local_now

logger = structlog.get_logger()

# OSS-First: resolved at import time from env vars (empty if unconfigured).
_DEFAULT_CITY = get_config().default_city
_DEFAULT_STATE = os.getenv("DEFAULT_STATE", "")
_DEFAULT_LOCATION = ", ".join(p for p in (_DEFAULT_CITY, _DEFAULT_STATE) if p)


def resolve_dynamic_value(value: str) -> str:
    """
    Resolve dynamic placeholders in knowledge values.

    Supported placeholders:
    - {dynamic:current_date} -> "Monday, November 24, 2025"
    - {dynamic:current_time} -> "2:45 PM"

    Args:
        value: Knowledge value that may contain dynamic placeholders

    Returns:
        Value with all placeholders resolved
    """
    if "{dynamic:" not in value:
        return value

    now = local_now()

    # Replace dynamic placeholders
    value = value.replace(
        "{dynamic:current_date}",
        now.strftime("%A, %B %d, %Y")
    )
    value = value.replace(
        "{dynamic:current_time}",
        now.strftime("%-I:%M %p")
    )

    return value


_ignored_guest_name_rows: set = set()


def _reset_for_tests() -> None:
    """Clear the guest_name-row log latch. Tests only."""
    _ignored_guest_name_rows.clear()


def _warn_guest_name_row(entry: Dict[str, Any]) -> None:
    row_id = entry.get("id")
    if row_id in _ignored_guest_name_rows:
        return
    _ignored_guest_name_rows.add(row_id)
    logger.warning("base_knowledge_static_guest_name_ignored", row_id=row_id)


def _category(entry: Dict[str, Any]) -> str:
    """The entry's category, trimmed and lower-cased, so a legacy row stored
    as ' Owner ' is still treated as the owner category."""
    return str(entry.get("category") or "general").strip().lower()


def build_knowledge_context(knowledge_entries: List[Dict[str, Any]], *, audience: KnowledgeAudience) -> str:
    """
    Build formatted context string from base knowledge entries.

    Every entry is re-checked against ``audience.visible_tiers()`` (defense
    in depth: the loader already filtered), then sorted into the prompt.

    Names and owner facts: a static ``guest_name`` is never rendered (the
    addressed guest comes from the live stay, per caller). Every other
    user/owner key containing "name", and ``owner_name``/``name`` themselves,
    render only in owner mode with a trustworthy mode service. Every
    other ``owner``-category row renders only for a proven owner
    (``audience.owner_proven``), whatever tier it is stored under.

    Args:
        knowledge_entries: List of knowledge entries from Admin API
        audience: Who the prompt is for, as proven by the server

    Returns:
        Formatted context string ready for injection into system prompt
    """
    tiers = audience.visible_tiers()
    owner_facts = audience.mode == "owner" and not audience.degraded
    knowledge_entries = [
        e for e in (knowledge_entries or [])
        if entry_visible(e, tiers)
        and not (
            _category(e) == OWNER_CATEGORY
            and e.get("key") not in OWNER_NAME_KEYS
            and not audience.owner_proven
        )
    ]
    if not knowledge_entries:
        return ""

    # Separate instruction entries from regular context entries
    instruction_entries = [e for e in knowledge_entries if _category(e) == "instruction"]
    context_entries = [e for e in knowledge_entries if _category(e) != "instruction"]

    context_lines = []

    # Behavioral instructions go FIRST, prominently separated
    if instruction_entries:
        context_lines.append("BEHAVIORAL INSTRUCTIONS:")
        for entry in instruction_entries:
            value = entry.get("value", "")
            resolved_value = resolve_dynamic_value(value)
            context_lines.append(f"  {resolved_value}")
        context_lines.append("")

    # Regular context information follows
    if context_entries:
        context_lines.append("CONTEXT INFORMATION:")
        for entry in context_entries:
            # Get value and resolve any dynamic placeholders
            value = entry.get("value", "")
            resolved_value = resolve_dynamic_value(value)

            # Format based on category
            category = _category(entry)

            if category == "property":
                context_lines.append(f"• Property: {resolved_value}")
            elif category == "location":
                key = entry.get("key", "")
                if "default" in key:
                    context_lines.append(f"• Default Location: {resolved_value}")
                else:
                    context_lines.append(f"• Location: {resolved_value}")
            elif category in ("user", "owner"):
                key = entry.get("key", "")
                if key == "guest_name":
                    _warn_guest_name_row(entry)
                elif (category == "owner" or "name" in key) and not owner_facts:
                    continue
                elif key in ("owner_name", "name"):
                    context_lines.append(f"• Property owner's name: {resolved_value}")
                else:
                    context_lines.append(f"• User Context: {resolved_value}")
            elif category == "temporal":
                key = entry.get("key", "")
                if "date" in key:
                    context_lines.append(f"• Current Date: {resolved_value}")
                elif "time" in key:
                    context_lines.append(f"• Current Time: {resolved_value}")
                else:
                    context_lines.append(f"• {resolved_value}")
            elif category == "general":
                key = entry.get("key", "")
                if "assistant_name" in key:
                    context_lines.append(f"• Your Name: {resolved_value}")
                elif "location_context" in key:
                    context_lines.append(f"• {resolved_value}")
                else:
                    context_lines.append(f"• {resolved_value}")
            else:
                # Generic formatting for unknown categories
                context_lines.append(f"• {resolved_value}")

    # Join with newlines and add trailing newline
    context = "\n".join(context_lines)
    context += "\n\n"

    logger.info(
        "base_knowledge_context_built",
        entry_count=len(knowledge_entries),
        instruction_count=len(instruction_entries),
        context_entry_count=len(context_entries),
        context_length=len(context)
    )

    return context


def extract_home_address(knowledge_entries: List[Dict[str, Any]]) -> str:
    """
    Extract the home/property address from base knowledge entries.

    Looks for entries with category="property" and key="address" to find
    the user's home address for proximity queries.

    Args:
        knowledge_entries: List of knowledge entries from Admin API

    Returns:
        The home address string, or the DEFAULT_LOCATION env var as fallback (empty if unconfigured).
    """
    if not knowledge_entries:
        return _DEFAULT_LOCATION

    # Look for property address entry
    for entry in knowledge_entries:
        category = entry.get("category", "")
        key = entry.get("key", "")
        value = entry.get("value", "")

        if category == "property" and key == "address" and value:
            logger.info("home_address_extracted", address_set=True)
            return value

    # Fallback to default location entries
    for entry in knowledge_entries:
        category = entry.get("category", "")
        key = entry.get("key", "")
        value = entry.get("value", "")

        if category == "location" and "default" in key and value:
            logger.info(
                "default_location_extracted",
                location_set=True
            )
            return value

    logger.warning("no_home_address_found_using_fallback")
    return _DEFAULT_LOCATION


def extract_owner_name(knowledge_entries: List[Dict[str, Any]]) -> Optional[str]:
    """The owner's name from already-filtered entries.

    Among ``owner``/``user``-category rows with a key in ``OWNER_NAME_KEYS``
    the tier order is owner > household > both, then priority (highest
    first), so a more specific audience wins over a general one.
    """
    tier_rank = {"owner": 0, "household": 1, "both": 2}
    candidates = [
        e for e in (knowledge_entries or [])
        if _category(e) in ("owner", "user")
        and e.get("key") in OWNER_NAME_KEYS
        and e.get("applies_to") in tier_rank
        and (e.get("value") or "").strip()
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda e: (tier_rank[e["applies_to"]], -(e.get("priority") or 0)))
    return best["value"].strip()


async def load_visible_knowledge(admin_client, *, audience: KnowledgeAudience) -> List[Dict[str, Any]]:
    """Enabled entries the audience may see, highest priority first.

    An audience that sees no tier returns [] without a fetch; a failed fetch
    returns [] too.
    """
    tiers = audience.visible_tiers()
    if not tiers:
        return []
    try:
        entries = await admin_client.get_base_knowledge(tiers=tiers, enabled_only=True)
    except Exception as e:
        logger.error("failed_to_load_base_knowledge", error=str(e))
        return []
    return sorted((e for e in (entries or []) if entry_visible(e, tiers)),
                  key=lambda e: e.get("priority", 0), reverse=True)


def knowledge_digest(entries: List[Dict[str, Any]]) -> str:
    """A short digest of exactly the rows an audience can see.

    Order-independent. It covers each row's identity, audience, state, last
    update and a hash of its value, so narrowing a row (re-tier, disable,
    delete) or editing its value changes the digest. The semantic cache puts
    it in every key, so an answer derived from knowledge that has since
    changed is never replayed.
    """
    parts = sorted(
        "|".join((
            str(e.get("id")), str(e.get("category")), str(e.get("key")), str(e.get("applies_to")),
            str(e.get("enabled")), str(e.get("priority")), str(e.get("updated_at")),
            hashlib.sha256(str(e.get("value")).encode("utf-8")).hexdigest()[:16],
        ))
        for e in entries
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


async def knowledge_cache_digest(admin_client, *, audience: KnowledgeAudience) -> Optional[str]:
    """The digest of the rows ``audience`` can see, or None when they could not
    be loaded (the caller then skips the semantic cache entirely)."""
    entries = await load_visible_knowledge(admin_client, audience=audience)
    if not audience.visible_tiers():
        return knowledge_digest([])
    if getattr(admin_client, "base_knowledge_fetch_ok", True) is False:
        return None
    return knowledge_digest(entries)


async def get_knowledge_context_for_user(admin_client, *, audience: KnowledgeAudience) -> str:
    """
    Fetch and format base knowledge context for one audience.

    Args:
        admin_client: AdminConfigClient instance
        audience: Who the prompt is for (see KnowledgeAudience)

    Returns:
        Formatted context string ready for system prompt injection
    """
    try:
        knowledge_entries = await load_visible_knowledge(admin_client, audience=audience)
        if not knowledge_entries:
            logger.info("no_base_knowledge_entries_found")
            return ""

        context = build_knowledge_context(knowledge_entries, audience=audience)

        logger.info(
            "knowledge_context_generated",
            entry_count=len(knowledge_entries),
            context_length=len(context)
        )

        return context

    except Exception as e:
        logger.error("failed_to_build_knowledge_context", error=str(e))
        return ""


if __name__ == "__main__":
    # Test dynamic value resolution
    test_values = [
        "{dynamic:current_date}",
        "{dynamic:current_time}",
        "The current date is {dynamic:current_date} and the time is {dynamic:current_time}",
        "No dynamic values here"
    ]

    print("Testing dynamic value resolution:")
    for test_val in test_values:
        resolved = resolve_dynamic_value(test_val)
        print(f"  Input:  {test_val}")
        print(f"  Output: {resolved}")
        print()

    # Test context building
    test_knowledge = [
        {
            "category": "property",
            "key": "address",
            "value": "123 Example St, Denver, CO 80202",
            "priority": 100
        },
        {
            "category": "user",
            "key": "user_type",
            "value": "You are an Airbnb guest staying at this property",
            "priority": 95
        },
        {
            "category": "temporal",
            "key": "current_date",
            "value": "{dynamic:current_date}",
            "priority": 80
        },
        {
            "category": "general",
            "key": "assistant_name",
            "value": "Athena",
            "priority": 70
        }
    ]

    print("\nTesting context building:")
    context = build_knowledge_context(
        [{**k, "applies_to": "both"} for k in test_knowledge],
        audience=KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=False, owner_proven=False),
    )
    print(context)
