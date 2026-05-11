"""Merge llm_performance_metrics branch into main migration chain.

Revision ID: 054b
Revises: 054, 004a
Create Date: 2026-05-08

The llm_performance_metrics branch (004a, descended from 93bea4659785 / 004)
diverged from the main chain at revision 003.  This merge revision joins it
back so that migration 055 has a single head to extend.

No schema changes — pure merge bookkeeping.
"""
from alembic import op
import sqlalchemy as sa  # noqa: F401  (required by alembic env template)

# revision identifiers, used by Alembic.
revision = '054b'
down_revision = ('054', '004a')  # merges both heads
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pure merge — no DDL.
    pass


def downgrade() -> None:
    # Pure merge — no DDL.
    pass
