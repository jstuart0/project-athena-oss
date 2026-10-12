"""shared.ha_link.link_state against the one truth table.

The same fixture (tests/fixtures/ha_link/truth_table.json) is read by
admin-backend's classifier tests, so the orchestrator's /health bool and the
admin card can't drift apart. Every row of the plan's table has cases here.
"""
from __future__ import annotations

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
    assert len(CASES) >= 30
    assert {c["row"] for c in CASES} == set(range(1, 11))
    assert len({c["id"] for c in CASES}) == len(CASES)


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
