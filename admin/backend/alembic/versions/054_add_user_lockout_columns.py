"""Add failed_login_count and locked_until columns to users table.

Supports Phase 4 (ATHENA-14) account-lockout logic: the login handler
increments failed_login_count on bad password and sets locked_until on
threshold breach. Phase 4 uses UPDATE...RETURNING (SQLite >= 3.35, March
2021) for atomic increment; the startup_event SQLite guard enforces this
before the login path is reached.

Existing rows backfill to 0 / NULL via server_default / nullable semantics.

Revision ID: 054
Revises: 053
Create Date: 2026-05-07
"""

from alembic import op
import sqlalchemy as sa

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "locked_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
