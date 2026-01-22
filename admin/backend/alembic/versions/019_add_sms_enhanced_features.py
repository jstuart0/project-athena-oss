"""Add SMS enhanced features tables.

Revision ID: 019_add_sms_enhanced_features
Revises: 018_add_sms_infrastructure
Create Date: 2025-12-02

Creates tables for:
- tip_prompts: Configurable tips shown to guests
- tip_prompt_history: Tracks which tips were shown to which guests
- sms_templates: Templates for proactive messages
- scheduled_sms: Scheduled/proactive SMS configurations
- scheduled_sms_log: Tracks scheduled SMS sends
- pending_sms: Queue for delayed SMS sends
- sms_incoming: Log of inbound SMS for bidirectional conversations
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '019_add_sms_enhanced_features'
down_revision = '018_add_sms_infrastructure'
branch_labels = None
depends_on = None


def upgrade():
    """Create enhanced SMS feature tables."""

    # Tips system - configurable tips shown to guests
    op.create_table(
        'tip_prompts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tip_type', sa.String(50), nullable=False),  # 'sms_offer', 'feature_hint', 'local_tip'
        sa.Column('title', sa.String(100), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('trigger_condition', sa.String(100), nullable=True),  # 'after_wifi', 'first_question', etc.
        sa.Column('trigger_intent', sa.String(100), nullable=True),  # Specific intent to trigger on
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('max_shows_per_stay', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_tip_prompts_type', 'tip_prompts', ['tip_type'])
    op.create_index('idx_tip_prompts_enabled', 'tip_prompts', ['enabled'])

    # Track which tips have been shown to which guests
    op.create_table(
        'tip_prompt_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tip_id', sa.Integer(), sa.ForeignKey('tip_prompts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=True),
        sa.Column('session_id', sa.String(255), nullable=True),
        sa.Column('shown_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('accepted', sa.Boolean(), nullable=True),  # Did guest act on the tip?
    )
    op.create_index('idx_tip_history_tip', 'tip_prompt_history', ['tip_id'])
    op.create_index('idx_tip_history_event', 'tip_prompt_history', ['calendar_event_id'])

    # SMS templates for proactive messages
    op.create_table(
        'sms_templates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('category', sa.String(50), nullable=True),  # 'welcome', 'checkout', 'reminder', 'custom'
        sa.Column('subject', sa.String(100), nullable=True),  # Brief description
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('variables', JSONB, nullable=True),  # List of variable names: ['guest_name', 'checkin_date']
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_sms_templates_category', 'sms_templates', ['category'])

    # Scheduled/proactive SMS configurations
    op.create_table(
        'scheduled_sms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('trigger_type', sa.String(50), nullable=False),  # 'before_checkin', 'after_checkin', 'before_checkout', 'time_of_day'
        sa.Column('trigger_offset_hours', sa.Integer(), nullable=False, server_default='0'),  # Hours before/after event
        sa.Column('trigger_time', sa.Time(), nullable=True),  # Specific time of day (for time_of_day trigger)
        sa.Column('template_id', sa.Integer(), sa.ForeignKey('sms_templates.id', ondelete='SET NULL'), nullable=True),
        sa.Column('custom_message', sa.Text(), nullable=True),  # Or use custom message instead of template
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('send_to_all_guests', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('min_stay_nights', sa.Integer(), nullable=False, server_default='0'),  # Only for stays >= N nights
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_scheduled_sms_enabled', 'scheduled_sms', ['enabled'])
    op.create_index('idx_scheduled_sms_trigger', 'scheduled_sms', ['trigger_type'])

    # Log of sent scheduled SMS (prevents duplicates)
    op.create_table(
        'scheduled_sms_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('scheduled_sms_id', sa.Integer(), sa.ForeignKey('scheduled_sms.id', ondelete='CASCADE'), nullable=False),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sms_history_id', sa.Integer(), sa.ForeignKey('sms_history.id', ondelete='SET NULL'), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default="'sent'"),
    )
    op.create_index('idx_scheduled_log_combo', 'scheduled_sms_log', ['scheduled_sms_id', 'calendar_event_id'], unique=True)

    # Pending/delayed SMS queue
    op.create_table(
        'pending_sms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=True),
        sa.Column('phone_number', sa.String(50), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('content_type', sa.String(50), nullable=True),
        sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default="'pending'"),  # pending, sent, cancelled, failed
        sa.Column('original_query', sa.Text(), nullable=True),
        sa.Column('session_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sms_history_id', sa.Integer(), sa.ForeignKey('sms_history.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('idx_pending_sms_scheduled', 'pending_sms', ['scheduled_for', 'status'])
    op.create_index('idx_pending_sms_event', 'pending_sms', ['calendar_event_id'])

    # Incoming SMS log for bidirectional conversations
    op.create_table(
        'sms_incoming',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('phone_number', sa.String(50), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('twilio_sid', sa.String(100), nullable=True),
        sa.Column('calendar_event_id', sa.Integer(), sa.ForeignKey('calendar_events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('matched_guest', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('response_sent', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('response_content', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_sms_incoming_phone', 'sms_incoming', ['phone_number'])
    op.create_index('idx_sms_incoming_received', 'sms_incoming', ['received_at'])

    # Insert default SMS tip
    op.execute("""
        INSERT INTO tip_prompts (tip_type, title, message, trigger_condition, enabled, priority, max_shows_per_stay)
        VALUES (
            'sms_offer',
            'SMS Information Tip',
            'I can text you important info like WiFi passwords, door codes, and addresses. Just say "text me that" after any response!',
            'first_question',
            true,
            100,
            1
        );
    """)

    # Insert default welcome template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables)
        VALUES (
            'welcome_message',
            'welcome',
            'Welcome to your stay',
            'Hi {guest_name}! Welcome to your stay. I''m Athena, your AI assistant. Text me anytime if you have questions about the property. WiFi: {wifi_name} / {wifi_password}',
            '["guest_name", "wifi_name", "wifi_password"]'::jsonb
        );
    """)


def downgrade():
    """Remove enhanced SMS feature tables."""
    op.drop_table('sms_incoming')
    op.drop_table('pending_sms')
    op.drop_table('scheduled_sms_log')
    op.drop_table('scheduled_sms')
    op.drop_table('sms_templates')
    op.drop_table('tip_prompt_history')
    op.drop_table('tip_prompts')
