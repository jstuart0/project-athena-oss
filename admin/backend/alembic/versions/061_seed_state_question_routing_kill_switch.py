"""Seed the state_question_routing_kill_switch feature row (ATHENA-128, D15).

The orchestrator answers device-state questions with a read under a
read-only scope. This flag is the emergency revert. It's inverted (a kill
switch, seeded disabled) because the orchestrator's get_feature_config
reports enabled=False for a missing flag and during an admin-API outage;
an enable-style flag would silently turn the fix off in exactly those
cases.

Data-only seed, 059's pattern: ON CONFLICT DO NOTHING, so an operator's
own row (enabled or disabled) is never overwritten.

Revision ID: 061
Revises: 060
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None

KILL_SWITCH_NAME = "state_question_routing_kill_switch"
KILL_SWITCH_DISPLAY_NAME = "State-Question Routing Kill Switch"
KILL_SWITCH_DESCRIPTION = (
    "Enable to DISABLE state-question routing (emergency revert). While "
    "enabled, questions like 'are the office lights on?' go back to the "
    "legacy smart-home path instead of a guaranteed read; if that path would "
    "change a device, the change is never made silently: the user gets a "
    "confirmation or the exact command to say."
)


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("features"):
        print("WARNING: features table not found — skipping state_question_routing_kill_switch seed (migration 061).")
        return

    bind.execute(
        sa.text(
            """
            INSERT INTO features
                (name, display_name, description, category, enabled, avg_latency_ms, required, priority)
            VALUES
                (:name, :display_name, :description, 'routing', false, 0, false, 70)
            ON CONFLICT (name) DO NOTHING;
            """
        ),
        {
            "name": KILL_SWITCH_NAME,
            "display_name": KILL_SWITCH_DISPLAY_NAME,
            "description": KILL_SWITCH_DESCRIPTION,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM features WHERE name = :name"), {"name": KILL_SWITCH_NAME})
