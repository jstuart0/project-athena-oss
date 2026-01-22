"""Add calendar_sources table for configurable iCal sync.

Revision ID: 024_add_calendar_sources
Revises: 023_add_multi_guest_support
Create Date: 2025-12-13

Adds:
- calendar_sources: Configurable iCal feed sources for guest mode
- source_id FK on calendar_events to track which source an event came from

Supports dynamic calendar source management via Admin UI.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '024_add_calendar_sources'
down_revision = '023_add_multi_guest_support'
branch_labels = None
depends_on = None


def upgrade():
    """Create calendar_sources table and add source_id to calendar_events."""

    # Create calendar_sources table
    op.create_table(
        'calendar_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('ical_url', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('sync_interval_minutes', sa.Integer(), server_default='30', nullable=False),
        sa.Column('priority', sa.Integer(), server_default='1', nullable=False),
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_sync_status', sa.String(length=50), server_default='pending', nullable=True),
        sa.Column('last_sync_error', sa.Text(), nullable=True),
        sa.Column('last_event_count', sa.Integer(), server_default='0', nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_calendar_source_enabled', 'calendar_sources', ['enabled'])
    op.create_index('idx_calendar_source_type', 'calendar_sources', ['source_type'])
    op.create_index('idx_calendar_source_last_sync', 'calendar_sources', ['last_sync_at'])

    # Add source_id column to calendar_events
    op.add_column('calendar_events', sa.Column('source_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_calendar_events_source_id',
        'calendar_events',
        'calendar_sources',
        ['source_id'],
        ['id'],
        ondelete='SET NULL'
    )
    op.create_index('idx_calendar_source_id', 'calendar_events', ['source_id'])


def downgrade():
    """Remove calendar_sources table and source_id from calendar_events."""
    op.drop_index('idx_calendar_source_id', table_name='calendar_events')
    op.drop_constraint('fk_calendar_events_source_id', 'calendar_events', type_='foreignkey')
    op.drop_column('calendar_events', 'source_id')
    op.drop_table('calendar_sources')
