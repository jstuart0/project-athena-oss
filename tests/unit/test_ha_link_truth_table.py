"""shared.ha_link.link_state against the one truth table.

The same fixture (tests/fixtures/ha_link/truth_table.json) is read by
admin-backend's classifier tests, so the orchestrator's /health bool and the
admin card can't drift apart. Every row of the plan's table has cases here.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shared import ha_link  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ha_link" / "truth_table.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_the_fixture_covers_every_row_of_the_table_and_is_not_trivially_small():
    assert len(CASES) >= 60
    assert {c["row"] for c in CASES} == set(range(1, 11)) | {"2a"}
    assert len({c["id"] for c in CASES}) == len(CASES)


NEW_CASE_IDS = [
    "ban_rest_read_403_failing_at_once", "rest_services_403_without_ban_evidence_stays_attention",
    "ban_registry_handshake_403_no_success", "ban_registry_handshake_403_after_older_success",
    "ban_older_than_success_is_connected", "latched_newer_than_success", "latched_older_than_success_is_ignored",
    "latched_with_nothing_else", "ban_beats_token_rejected_when_newest", "failures_counted_while_connected_stay_connected",
    "v1_view_without_new_keys", "ban_flag_stale_but_newest_failure_is_connect_error",
    "ban_rest_older_than_registry_other_failure", "ban_registry_flag_but_registry_state_ok", "latched_not_configured",
    "token_rejected_beats_older_ban", "latched_at_equals_success_at", "latched_without_latched_at",
    "ban_seen_by_gateway_only", "banned_at_older_than_success", "budget_spent_newer_than_success",
    "budget_spent_older_than_success_is_connected", "budget_spent_without_spent_at",
]


def test_the_binding_case_ids_are_all_present():
    assert set(NEW_CASE_IDS) <= {c["id"] for c in CASES}


def _reasons_link_state_can_return():
    tree = ast.parse((SRC / "shared" / "ha_link.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "link_state")
    words, via_name = set(), False
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2:
            reason = node.value.elts[1]
            if isinstance(reason, ast.Constant):
                words.add(reason.value)
            elif isinstance(reason, ast.Name):
                via_name = True
    assert via_name
    for node in ast.walk(function):  # the variable ``reason``: assigned a literal, a choice of literals or a table lookup
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "reason" for t in node.targets):
            words |= {n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    table = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_REST_FAILURE_REASON")
    words |= {v.value for v in table.value.values}
    return words


def test_every_reason_in_the_fixture_is_one_link_state_can_return_and_the_reverse():
    assert {c["reason"] for c in CASES} == _reasons_link_state_can_return()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_link_state_matches_the_fixture(case):
    assert ha_link.link_state(case["view"]) == (case["state"], case["reason"])
    assert ha_link.home_assistant_reachable(case["view"]) is case["home_assistant"]


def test_the_health_bool_is_true_exactly_for_connected_and_attention():
    by_state = {}
    for case in CASES:
        by_state.setdefault(case["state"], set()).add(case["home_assistant"])
    assert by_state == {
        "not_configured": {False},
        "unknown": {False},
        "failing": {False},
        "attention": {True},
        "connected": {True},
    }


def test_a_missing_registry_or_rest_section_reads_like_a_disabled_registry():
    view = {"configured": True, "rest": {"last_outcome": "ok", "last_outcome_at": "2026-10-11T11:59:00+00:00",
                                         "last_success_at": "2026-10-11T11:59:00+00:00", "consecutive_failures": 0}}
    assert ha_link.link_state(view) == ("connected", "ok")
