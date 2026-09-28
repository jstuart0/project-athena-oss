"""ATHENA-69 Pass D wiring/drift guard.

Split out of tests/unit/test_ha_permission_wiring.py deliberately: Passes B
and E both append to that shared file on their own branches, and this
campaign's passes are built in parallel worktrees merged later, so a fourth
concurrent editor of the exact same file just multiplies merge conflicts
(mozart, 2026-09-28).

Pass D members:
 - test_sms_webhook_tags_caller_trust_sms
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestSmsWebhookTagsCallerTrust:
    """The SMS webhook's orchestrator /query payload dict literal (AST)
    carries "caller_trust": "sms" (D24)."""

    def test_sms_webhook_tags_caller_trust_sms(self):
        path = REPO_ROOT / "admin" / "backend" / "app" / "routes" / "sms_webhook.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "caller_trust"
                    and isinstance(value, ast.Constant)
                    and value.value == "sms"
                ):
                    found = True
        assert found, "sms_webhook.py's orchestrator payload must set caller_trust=\"sms\""
