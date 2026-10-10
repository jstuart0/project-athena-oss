"""knowledge_tiers: the tier-to-audience matrix, row visibility and write rules.

Stdlib only, so it runs on the unit-min CI requirements.
"""
from __future__ import annotations

import itertools

import pytest

from shared.knowledge_tiers import (
    KNOWLEDGE_TIERS,
    OWNER_NAME_KEYS,
    OWNER_TIER,
    WRITABLE_TIERS,
    KnowledgeAudience,
    entry_visible,
    validate_entry_fields,
)

BOTH = frozenset({"both"})


def _aud(mode, degraded=False, public=False, owner_caller=False, owner_proven=False):
    return KnowledgeAudience(mode=mode, degraded=degraded, public=public,
                             owner_caller=owner_caller, owner_proven=owner_proven)


def test_vocabulary():
    assert KNOWLEDGE_TIERS == ("both", "guest", "household", "owner")
    assert WRITABLE_TIERS == frozenset(KNOWLEDGE_TIERS)
    assert OWNER_TIER == "owner"
    assert OWNER_NAME_KEYS == frozenset({"owner_name", "name"})


MATRIX = [
    # (audience kwargs, visible tiers) -- one case per row of the plan's table
    (dict(mode="owner", public=True), set()),
    (dict(mode="guest", public=True), set()),
    (dict(mode="guest"), {"both", "guest"}),
    (dict(mode="guest", degraded=True), {"both"}),
    (dict(mode="guest", owner_caller=True), {"both", "guest"}),
    (dict(mode="owner"), {"both", "household"}),
    (dict(mode="owner", owner_caller=True), {"both", "household"}),
    (dict(mode="owner", owner_caller=True, owner_proven=True), {"both", "household", "owner"}),
    (dict(mode="owner", degraded=True), {"both"}),
    (dict(mode="owner", degraded=True, owner_caller=True), {"both"}),
    (dict(mode=None), set()),
]


@pytest.mark.parametrize("kwargs,expected", MATRIX)
def test_visible_tiers_matrix(kwargs, expected):
    assert _aud(**kwargs).visible_tiers() == frozenset(expected)


def test_matrix_floor_and_named_member():
    assert len(MATRIX) >= 11
    proven = _aud("owner", owner_caller=True, owner_proven=True)
    assert "owner" in proven.visible_tiers()
    assert "owner" not in _aud("owner", owner_caller=True).visible_tiers()


def test_owner_tier_only_ever_from_proof():
    for mode, degraded, public, caller in itertools.product(
        (None, "owner", "guest"), (False, True), (False, True), (False, True)
    ):
        if public and caller:
            continue
        assert "owner" not in _aud(mode, degraded, public, caller).visible_tiers()


def test_unresolved_fails_closed():
    assert KnowledgeAudience.UNRESOLVED == _aud(None, degraded=True)
    assert KnowledgeAudience.UNRESOLVED.visible_tiers() == frozenset()
    assert KnowledgeAudience.UNRESOLVED.visible_tiers() == frozenset()


@pytest.mark.parametrize("kwargs", [
    dict(mode="owner", owner_proven=True),                                   # not an owner caller
    dict(mode="guest", owner_caller=True, owner_proven=True),                # wrong mode
    dict(mode=None, owner_caller=True, owner_proven=True),                   # no mode
    dict(mode="owner", degraded=True, owner_caller=True, owner_proven=True), # degraded
    dict(mode="owner", public=True, owner_caller=True),                      # public owner caller
    dict(mode="owner", public=True, owner_caller=True, owner_proven=True),
])
def test_invariant_violations_raise(kwargs):
    with pytest.raises(ValueError):
        _aud(**kwargs)


@pytest.mark.parametrize("kwargs", [
    dict(mode="owner", degraded="false"),
    dict(mode="owner", degraded=0),
    dict(mode="owner", public=1),
    dict(mode="owner", owner_caller="true"),
    dict(mode="owner", owner_caller=None),
    dict(mode="owner", owner_caller=True, owner_proven=1),
    dict(mode="owner "),
    dict(mode="Owner"),
    dict(mode=""),
    dict(mode="other"),
    dict(mode=1),
])
def test_type_checks_reject(kwargs):
    with pytest.raises(ValueError):
        _aud(**kwargs)


def test_degraded_sees_only_both():
    for mode in ("owner", "guest"):
        assert _aud(mode, degraded=True).visible_tiers() == frozenset({"both"})


def test_audience_is_frozen():
    with pytest.raises(Exception):
        _aud("owner").owner_proven = True  # type: ignore[misc]


@pytest.mark.parametrize("entry,tiers,expected", [
    ({"applies_to": "both"}, BOTH, True),
    ({"applies_to": "owner"}, BOTH, False),
    ({}, BOTH, False),
    ({"applies_to": None}, BOTH, False),
    ({"applies_to": 7}, BOTH, False),
    ({"applies_to": "OWNER"}, frozenset({"owner"}), False),
    ({"applies_to": "Both"}, BOTH, False),
    ({"applies_to": "chat"}, frozenset(KNOWLEDGE_TIERS), False),
    ({"applies_to": ["both"]}, BOTH, False),
    ({"applies_to": "both"}, frozenset(), False),
    ({"applies_to": "owner"}, frozenset({"owner"}), True),
])
def test_entry_visible(entry, tiers, expected):
    assert entry_visible(entry, tiers) is expected


@pytest.mark.parametrize("tier", KNOWLEDGE_TIERS)
def test_valid_tiers_accepted(tier):
    cat, key = ("property", "wifi")
    assert validate_entry_fields(cat, key, tier) is None


@pytest.mark.parametrize("tier", ["chat", "OWNER", "", "owner_private", None, 3, "both "])
def test_bad_tier_rejected(tier):
    detail = validate_entry_fields("property", "wifi", tier)
    assert isinstance(detail, str) and detail.startswith("applies_to: ")


@pytest.mark.parametrize("category", [
    "Career ", "a" * 51, "car\neer", "", "1abc", "_abc", "Career", "career\n", "car eer", None, 5,
])
def test_bad_category_rejected(category):
    detail = validate_entry_fields(category, "k", "both")
    assert isinstance(detail, str) and detail.startswith("category: ")


@pytest.mark.parametrize("category", ["a", "a" * 50, "career", "a_1"])
def test_good_category_edges(category):
    assert validate_entry_fields(category, "k", "both") is None


@pytest.mark.parametrize("key", [
    "", "-abc", ".abc", "_abc", "a b", "a" * 101, "ke\ny", "key\n", "k/ey", None, 1,
])
def test_bad_key_rejected(key):
    detail = validate_entry_fields("property", key, "both")
    assert isinstance(detail, str) and detail.startswith("key: ")


@pytest.mark.parametrize("key", ["a", "A", "0", "a" * 100, "wifi.pass:2-b_c"])
def test_good_key_edges(key):
    assert validate_entry_fields("property", key, "both") is None


def test_owner_category_rule_named_member():
    detail = validate_entry_fields("owner", "employer", "household")
    assert detail is not None and detail.startswith("applies_to: ")


@pytest.mark.parametrize("tier", ["both", "guest", "household"])
def test_owner_category_non_owner_tier_rejected(tier):
    assert validate_entry_fields("owner", "employer", tier) is not None


def test_owner_category_allowed_cases():
    assert validate_entry_fields("owner", "employer", "owner") is None
    for key in OWNER_NAME_KEYS:
        for tier in KNOWLEDGE_TIERS:
            assert validate_entry_fields("owner", key, tier) is None
    assert validate_entry_fields("property", "employer", "household") is None
