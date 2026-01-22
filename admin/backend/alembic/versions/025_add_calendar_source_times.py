"""Add check-in/check-out time fields to calendar_sources.

The Lodgify API only provides arrival/departure dates, not times.
These fields allow configuring the default check-in/check-out times
per calendar source since they vary by property.

Revision ID: 025
Revises: 024
Create Date: 2024-12-13
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '025_add_calendar_source_times'
down_revision = '024_add_calendar_sources'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add default check-in/check-out time columns."""
    # Add check-in time (default 4:00 PM / 16:00)
    op.add_column(
        'calendar_sources',
        sa.Column('default_checkin_time', sa.String(5), server_default='16:00', nullable=True)
    )

    # Add check-out time (default 11:00 AM)
    op.add_column(
        'calendar_sources',
        sa.Column('default_checkout_time', sa.String(5), server_default='11:00', nullable=True)
    )


def downgrade() -> None:
    """Remove check-in/check-out time columns."""
    op.drop_column('calendar_sources', 'default_checkout_time')
    op.drop_column('calendar_sources', 'default_checkin_time')
