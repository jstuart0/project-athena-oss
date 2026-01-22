"""Add SMS infrastructure tables.

Revision ID: 018_add_sms_infrastructure
Revises: 017_add_tool_api_key_requirements
Create Date: 2025-12-02

Creates tables for:
- sms_settings: Global SMS configuration
- guest_sms_preferences: Per-stay SMS preferences
- sms_history: Log of all sent SMS messages
- sms_cost_tracking: Cost tracking per stay and monthly
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '018_add_sms_infrastructure'
down_revision = '017_add_tool_api_key_requirements'
branch_labels = None
depends_on = None


def upgrade():
    """Create SMS infrastructure tables."""

    # SMS feature settings (singleton table)
    op.create_table(
        'sms_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('test_mode', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('auto_offer_mode', sa.String(20), nullable=False, server_default="'smart'"),
        # auto_offer_mode options: 'smart' (detect content), 'always', 'never'
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('rate_limit_per_stay', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('from_number', sa.String(20), nullable=True),  # Cached from external_api_keys
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # Insert default settings row
    op.execute("""
        INSERT INTO sms_settings (id, enabled, test_mode, auto_offer_mode, rate_limit_per_minute, rate_limit_per_stay)
        VALUES (1, false, true, 'smart', 10, 50)
        ON CONFLICT (id) DO NOTHING;
    """)

    # Guest SMS preferences (per-stay)
    op.create_table(
        'guest_sms_preferences',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sms_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('dont_ask_again', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('preferred_phone', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_guest_sms_prefs_event', 'guest_sms_preferences', ['calendar_event_id'], unique=True)

    # SMS history log
    op.create_table(
        'sms_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('phone_number', sa.String(50), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('content_summary', sa.String(255), nullable=True),
        sa.Column('content_type', sa.String(50), nullable=True),  # 'wifi', 'address', 'link', 'custom', etc.
        sa.Column('triggered_by', sa.String(50), nullable=True),  # 'user_request', 'auto_offer', 'scheduled', 'admin'
        sa.Column('original_query', sa.Text(), nullable=True),
        sa.Column('session_id', sa.String(255), nullable=True),
        sa.Column('twilio_sid', sa.String(100), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default="'queued'"),
        # status options: 'queued', 'sent', 'delivered', 'failed', 'undelivered'
        sa.Column('error_code', sa.String(20), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('segment_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_sms_history_event', 'sms_history', ['calendar_event_id'])
    op.create_index('idx_sms_history_created', 'sms_history', ['created_at'])
    op.create_index('idx_sms_history_status', 'sms_history', ['status'])
    op.create_index('idx_sms_history_type', 'sms_history', ['content_type'])
    op.create_index('idx_sms_history_phone', 'sms_history', ['phone_number'])

    # SMS cost tracking (per-stay and monthly aggregates)
    op.create_table(
        'sms_cost_tracking',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('month', sa.Date(), nullable=True),  # First day of month for monthly aggregation
        # Either calendar_event_id OR month is set, not both
        sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('segment_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('incoming_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('outgoing_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_cost_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('outgoing_sms_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('incoming_sms_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_sms_cost_event', 'sms_cost_tracking', ['calendar_event_id'])
    op.create_index('idx_sms_cost_month', 'sms_cost_tracking', ['month'])


def downgrade():
    """Remove SMS infrastructure tables."""
    op.drop_table('sms_cost_tracking')
    op.drop_table('sms_history')
    op.drop_table('guest_sms_preferences')
    op.drop_table('sms_settings')
