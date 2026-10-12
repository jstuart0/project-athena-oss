"""The Home Assistant section of docs/CONFIGURATION.md states the gate's numbers.

They are read from shared.ha_auth, so the page can't drift from the code, and
the public text carries no ticket ids.
"""
import re
from pathlib import Path

from shared import ha_auth

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
SECTION = DOC[DOC.index("#### How Athena avoids getting banned"):]
SECTION = SECTION[: SECTION.index("\n### ")]


def test_the_budget_the_probes_and_the_fan_out_are_the_codes():
    assert f"**A budget of {ha_auth.BUDGET}.**" in SECTION
    assert ha_auth.PROBE_SCHEDULE_SECONDS == (120, 21600)
    assert "after 2 minutes and after 6 hours" in SECTION
    assert f"up to {ha_auth.INFLIGHT_CAP} (one full fan-out)" in SECTION
    assert f"older than {int(ha_auth.CANARY_AFTER)} seconds" in SECTION
    assert f"lowers the counter to {ha_auth.BUDGET - 1}" in SECTION


def test_the_rotation_order_is_stated_in_the_page_and_the_changelog():
    assert "Save the new token in Athena, wait two minutes, then delete the old one in Home Assistant." in SECTION
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Save the new token in Athena first, wait two minutes, then delete the old one in Home Assistant." in changelog


def test_the_page_names_every_part_of_athena_the_dashboard_counts():
    from shared import ha_link

    assert len(ha_link.CALLERS) == 6
    for label in ("voice commands", "room list", "voice satellites", "speaker room detection", "admin panel", "voice pipeline settings"):
        assert label in SECTION


def test_public_text_for_this_work_has_no_ticket_ids_or_planning_paths():
    assert not re.search(r"ATHENA-\d+", SECTION)
    for name in ("CHANGELOG.md", "docs/CONFIGURATION.md", ".env.example", "CLAUDE.md"):
        assert ".mozart" not in (ROOT / name).read_text(encoding="utf-8"), name


def test_the_page_states_the_reservation_expiry_the_redis_requirement_and_the_single_row():
    assert f"(2 minutes)" in SECTION and ha_auth.RESERVATION_TTL == 120.0
    assert "single-node Redis" in SECTION and "fails closed" in SECTION
    page = DOC[DOC.index("#### Where Athena finds its Home Assistant address and token"):]
    assert "whatever its key type, is refused" in page and "never handed to a tool" in page
    assert "`169.254.43518`" in page and "trailing dot" in page
