"""Add dynamic automation system with voice automations table and feature flag.

Revision ID: 041_dynamic_automation_system
Revises: 040_update_preset_feature_flags
Create Date: 2026-01-04

This migration adds:
1. Feature flag for automation_system_mode (pattern_matching vs dynamic_agent)
2. Voice automations table for storing user-created automations
3. Support for guest-scoped automations with archival/restoration
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '041_dynamic_automation_system'
down_revision = '040_preset_feature_flags'
branch_labels = None
depends_on = None


def upgrade():
    # Add feature flag for automation system mode
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority, config, created_at, updated_at)
        VALUES (
            'automation_system_mode',
            'Automation System Mode',
            'Controls which automation system handles sequence commands. "pattern_matching" uses keyword detection and sequence executor. "dynamic_agent" uses LLM with tools for fully dynamic handling.',
            'processing',
            true,
            false,
            50,
            '{"mode": "pattern_matching", "available_modes": ["pattern_matching", "dynamic_agent"]}',
            NOW(),
            NOW()
        )
        ON CONFLICT (name) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            config = EXCLUDED.config,
            updated_at = NOW()
    """)

    # Create voice_automations table
    op.create_table(
        'voice_automations',
        sa.Column('id', sa.Integer(), nullable=False),

        # Identification
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('ha_automation_id', sa.String(255), nullable=True),  # ID in Home Assistant

        # Ownership
        sa.Column('owner_type', sa.String(20), nullable=False),  # 'owner' or 'guest'
        sa.Column('guest_session_id', sa.String(255), nullable=True),
        sa.Column('guest_name', sa.String(255), nullable=True),
        sa.Column('created_by_room', sa.String(100), nullable=True),

        # Automation definition (stored as JSONB for flexibility)
        sa.Column('trigger_config', postgresql.JSONB(), nullable=False),
        sa.Column('conditions_config', postgresql.JSONB(), nullable=True),
        sa.Column('actions_config', postgresql.JSONB(), nullable=False),

        # Scheduling
        sa.Column('is_one_time', sa.Boolean(), default=False, nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),

        # Status
        sa.Column('status', sa.String(20), default='active', nullable=False),  # active, paused, archived, deleted
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('archive_reason', sa.String(100), nullable=True),  # 'guest_departed', 'user_deleted', 'expired', 'one_time_completed'

        # Execution tracking
        sa.Column('last_triggered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('trigger_count', sa.Integer(), default=0, nullable=False),

        # Metadata
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),

        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "(owner_type = 'owner' AND guest_session_id IS NULL) OR (owner_type = 'guest' AND guest_session_id IS NOT NULL)",
            name='valid_owner_constraint'
        )
    )

    # Create indexes for efficient queries
    op.create_index('idx_voice_automations_guest', 'voice_automations', ['guest_session_id'],
                    postgresql_where=sa.text("guest_session_id IS NOT NULL"))
    op.create_index('idx_voice_automations_status', 'voice_automations', ['status'])
    op.create_index('idx_voice_automations_owner', 'voice_automations', ['owner_type'])
    op.create_index('idx_voice_automations_guest_name', 'voice_automations', ['guest_name'],
                    postgresql_where=sa.text("guest_name IS NOT NULL"))
    op.create_index('idx_voice_automations_ha_id', 'voice_automations', ['ha_automation_id'],
                    postgresql_where=sa.text("ha_automation_id IS NOT NULL"))


def downgrade():
    # Drop indexes
    op.drop_index('idx_voice_automations_ha_id', table_name='voice_automations')
    op.drop_index('idx_voice_automations_guest_name', table_name='voice_automations')
    op.drop_index('idx_voice_automations_owner', table_name='voice_automations')
    op.drop_index('idx_voice_automations_status', table_name='voice_automations')
    op.drop_index('idx_voice_automations_guest', table_name='voice_automations')

    # Drop table
    op.drop_table('voice_automations')

    # Remove feature flag
    op.execute("DELETE FROM features WHERE name = 'automation_system_mode'")
