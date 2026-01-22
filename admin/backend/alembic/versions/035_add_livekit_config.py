"""Add LiveKit WebRTC configuration.

This migration adds support for LiveKit WebRTC streaming:
- livekit_config table for service configuration
- Enables the livekit_webrtc feature flag

Revision ID: 035_add_livekit_config
Revises: 034_add_tool_proposals
Create Date: 2025-12-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '035_add_livekit_config'
down_revision = '034_add_tool_proposals'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # LiveKit Configuration Table
    # =========================================================================
    op.create_table(
        'livekit_config',
        sa.Column('id', sa.Integer(), nullable=False),

        # Connection settings
        sa.Column('livekit_url', sa.String(255), nullable=False),
        sa.Column('api_key_encrypted', sa.Text()),  # Encrypted API key
        sa.Column('api_secret_encrypted', sa.Text()),  # Encrypted API secret

        # Room settings
        sa.Column('room_empty_timeout', sa.Integer(), server_default='300'),  # 5 minutes
        sa.Column('max_participants', sa.Integer(), server_default='2'),  # User + Athena

        # Audio settings
        sa.Column('sample_rate', sa.Integer(), server_default='16000'),
        sa.Column('channels', sa.Integer(), server_default='1'),

        # Wake word settings
        sa.Column('wake_words', postgresql.JSONB(), server_default='["jarvis", "athena"]'),
        sa.Column('wake_word_threshold', sa.Float(), server_default='0.5'),

        # VAD settings
        sa.Column('vad_enabled', sa.Boolean(), server_default='true'),
        sa.Column('silence_timeout_ms', sa.Integer(), server_default='2000'),
        sa.Column('max_query_duration_ms', sa.Integer(), server_default='30000'),

        # Feature toggles
        sa.Column('server_side_wake_word', sa.Boolean(), server_default='true'),
        sa.Column('client_side_vad', sa.Boolean(), server_default='true'),
        sa.Column('interruption_enabled', sa.Boolean(), server_default='true'),

        # Status
        sa.Column('enabled', sa.Boolean(), server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),

        sa.PrimaryKeyConstraint('id')
    )

    # Insert default configuration (disabled by default)
    op.execute("""
        INSERT INTO livekit_config (livekit_url, enabled)
        VALUES ('wss://athena.livekit.cloud', false)
    """)

    # =========================================================================
    # Voice Interface for LiveKit
    # =========================================================================
    # Add LiveKit as a voice interface option
    op.execute("""
        INSERT INTO voice_interfaces (interface_name, display_name, protocol, enabled, config)
        VALUES (
            'livekit_webrtc',
            'LiveKit WebRTC',
            'webrtc',
            false,
            '{"description": "Browser-based WebRTC audio streaming via LiveKit"}'::jsonb
        )
        ON CONFLICT (interface_name) DO NOTHING
    """)

    # =========================================================================
    # Enable Feature Flag
    # =========================================================================
    # The livekit_webrtc feature flag was already created in migration 033
    # Just update it to reference the config table
    op.execute("""
        UPDATE feature_flags
        SET metadata = '{"config_table": "livekit_config", "requires_api_keys": true}'::jsonb
        WHERE flag_name = 'livekit_webrtc'
    """)


def downgrade() -> None:
    # Remove voice interface
    op.execute("""
        DELETE FROM voice_interfaces WHERE interface_name = 'livekit_webrtc'
    """)

    # Drop config table
    op.drop_table('livekit_config')

    # Reset feature flag metadata
    op.execute("""
        UPDATE feature_flags
        SET metadata = '{}'::jsonb
        WHERE flag_name = 'livekit_webrtc'
    """)
