"""Add test mode fields to calendar_events and guests tables.

Adds is_test boolean field to support test data creation and filtering.
Test data can be created for development/testing purposes and will be
auto-cleared when real guest reservations overlap.

Revision ID: 029
Revises: 028
Create Date: 2025-12-17
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '029_add_test_mode_fields'
down_revision = '028_add_music_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add is_test column to calendar_events and guests tables."""
    # Add to calendar_events
    op.add_column(
        'calendar_events',
        sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False)
    )
    op.create_index('idx_calendar_events_is_test', 'calendar_events', ['is_test'])

    # Add to guests
    op.add_column(
        'guests',
        sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False)
    )
    op.create_index('idx_guests_is_test', 'guests', ['is_test'])


def downgrade() -> None:
    """Remove is_test columns."""
    op.drop_index('idx_guests_is_test', table_name='guests')
    op.drop_column('guests', 'is_test')
    op.drop_index('idx_calendar_events_is_test', table_name='calendar_events')
    op.drop_column('calendar_events', 'is_test')
