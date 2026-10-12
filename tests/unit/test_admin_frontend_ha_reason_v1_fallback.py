"""An admin frontend from before the status vocabulary still copes with ip_banned.

The fixture is a frozen copy of the Home Assistant card block of app.js at the
base commit. The backend can now send ``reason: ip_banned``; that frontend has
no copy for it and must render "Status unknown", not throw, during the minutes
between the backend roll and the admin-frontend roll.
"""
from __future__ import annotations

import json

from .frontend_node import FRONTEND, REPO, requires_node, run_scripts

pytestmark = requires_node

FROZEN = REPO / "tests" / "fixtures" / "admin_frontend" / "app_ha_block_9e80241.js"
ESCAPE = FRONTEND / "escape-html.js"
PAYLOAD = {"state": "failing", "reason": "ip_banned", "areas": None, "checked_at": "2026-10-11T12:00:00+00:00",
           "last_outcome_at": "2026-10-11T11:58:00+00:00", "last_success_at": None, "coverage": "cluster", "gateway": "ok",
           "token_withheld": True, "next_retry_at": None, "auth_failures": [{"caller": "admin_ws", "count": 2}]}


def frozen(expression: str):
    return run_scripts([ESCAPE, FROZEN], expression)


def test_the_frozen_block_has_no_copy_for_the_new_reason_and_says_status_unknown():
    assert "ip_banned" not in FROZEN.read_text(encoding="utf-8")
    out = frozen(f"summarizeHomeAssistant({json.dumps(PAYLOAD)})")
    assert out["headline"] == "Status unknown"
    assert out["detail"] is None and out["action"] is None
    assert out["dotClass"] == "bg-red-500"


def test_the_frozen_card_renders_the_new_payload_without_throwing_or_echoing_the_extra_keys():
    html = frozen(f"renderHomeAssistantCard(summarizeHomeAssistant({json.dumps(PAYLOAD)}))")
    assert "Status unknown" in html
    assert "ip_banned" not in html and "admin_ws" not in html and "Failed sign-ins" not in html


def test_the_frozen_block_is_the_base_vocabulary_not_the_shared_one():
    text = FROZEN.read_text(encoding="utf-8")
    assert "function statusLabel(" in text and "const HA_REASON_COPY" in text
    assert "statusInfo" not in text
