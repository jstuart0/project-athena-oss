"""Add owner_pin_attempts table (ATHENA-69 D25/D35).

Per-tier lockout counter backing the admin-backend-held owner-PIN
verification endpoint (POST /api/internal/guest-mode/verify-pin). `tier` is
the primary key so a lockout in one caller-trust tier
(household/sms/web_authenticated/unknown) never blocks another.

Guarded by `has_table`, same philosophy as 058's ENCRYPTION_KEY pre-check
and 059's tool_registry check: production `init_db()`'s `create_all` may
have already created this table from the SQLAlchemy model
(`app/models.py::OwnerPinAttempt`) before this migration ever runs,
particularly on a fresh DEV_MODE/SQLite process.

Revision ID: 060
Revises: 059
Create Date: 2026-09-28

ATHENA-69 (Campaign: 2026-09-28-deliver-athena-ha-permission-gap)
"""
from alembic import op
import sqlalchemy as sa

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("owner_pin_attempts"):
        return
    op.create_table(
        "owner_pin_attempts",
        sa.Column("tier", sa.String(32), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tier"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("owner_pin_attempts"):
        return
    op.drop_table("owner_pin_attempts")
