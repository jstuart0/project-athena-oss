"""
Add guest mode tables

Revision ID: 004
Revises: 003
Create Date: 2025-01-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade():
    """Add tables for guest mode functionality"""

    # 1. Guest Mode Configuration
    op.create_table(
        'guest_mode_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), default=False, nullable=False),
        sa.Column('calendar_source', sa.String(50), default='ical'),
        sa.Column('calendar_url', sa.String(500)),
        sa.Column('calendar_poll_interval_minutes', sa.Integer(), default=10),
        sa.Column('buffer_before_checkin_hours', sa.Integer(), default=2),
        sa.Column('buffer_after_checkout_hours', sa.Integer(), default=1),
        sa.Column('owner_pin', sa.String(128)),
        sa.Column('override_timeout_minutes', sa.Integer(), default=60),
        sa.Column('guest_allowed_intents', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('guest_restricted_entities', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('guest_allowed_domains', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('max_queries_per_minute_guest', sa.Integer(), default=10),
        sa.Column('max_queries_per_minute_owner', sa.Integer(), default=100),
        sa.Column('guest_data_retention_hours', sa.Integer(), default=24),
        sa.Column('auto_purge_enabled', sa.Boolean(), default=True),
        sa.Column('config', postgresql.JSONB, default={}),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), onupdate=sa.text('CURRENT_TIMESTAMP')),
    )

    # Create index
    op.create_index('idx_guest_mode_enabled', 'guest_mode_config', ['enabled'])

    # 2. Calendar Events
    op.create_table(
        'calendar_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('external_id', sa.String(255), unique=True, nullable=False),
        sa.Column('source', sa.String(50), default='ical'),
        sa.Column('title', sa.String(255)),
        sa.Column('checkin', sa.DateTime(timezone=True), nullable=False),
        sa.Column('checkout', sa.DateTime(timezone=True), nullable=False),
        sa.Column('guest_name', sa.String(255)),
        sa.Column('notes', sa.Text()),
        sa.Column('status', sa.String(50), default='confirmed'),
        sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), onupdate=sa.text('CURRENT_TIMESTAMP')),
    )

    # Create indexes
    op.create_index('idx_calendar_checkin', 'calendar_events', ['checkin'])
    op.create_index('idx_calendar_checkout', 'calendar_events', ['checkout'])
    op.create_index('idx_calendar_status', 'calendar_events', ['status'])
    op.create_index('idx_calendar_synced_at', 'calendar_events', ['synced_at'])

    # 3. Mode Overrides
    op.create_table(
        'mode_overrides',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('activated_by', sa.String(50)),
        sa.Column('activated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('expires_at', sa.DateTime(timezone=True)),
        sa.Column('voice_device_id', sa.String(100)),
        sa.Column('ip_address', sa.String(50)),
        sa.Column('deactivated_at', sa.DateTime(timezone=True)),
    )

    # Create index
    op.create_index('idx_mode_override_active', 'mode_overrides', ['activated_at', 'expires_at'])


def downgrade():
    """Remove guest mode tables"""
    op.drop_index('idx_mode_override_active', table_name='mode_overrides')
    op.drop_table('mode_overrides')

    op.drop_index('idx_calendar_synced_at', table_name='calendar_events')
    op.drop_index('idx_calendar_status', table_name='calendar_events')
    op.drop_index('idx_calendar_checkout', table_name='calendar_events')
    op.drop_index('idx_calendar_checkin', table_name='calendar_events')
    op.drop_table('calendar_events')

    op.drop_index('idx_guest_mode_enabled', table_name='guest_mode_config')
    op.drop_table('guest_mode_config')
