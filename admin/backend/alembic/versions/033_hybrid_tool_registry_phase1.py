"""Hybrid Tool Registry and Admin Jarvis - Phase 1 Foundation.

This migration adds the foundational schema for:
- Voice interfaces (per-interface STT/TTS routing)
- STT/TTS engine registries
- Enhanced feature flags with config
- MCP tool integration (source tracking, approval queue)
- Pipeline events for Admin Jarvis real-time visualization
- MCP security configuration

Revision ID: 033_hybrid_tool_registry_phase1
Revises: 032_add_stt_tts_config
Create Date: 2025-12-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '033_hybrid_tool_registry_phase1'
down_revision = '032_add_stt_tts_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # 1. Voice Interfaces Table
    # Define voice interfaces and their per-interface STT/TTS configurations
    # =========================================================================
    op.create_table(
        'voice_interfaces',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('interface_name', sa.String(100), nullable=False),  # 'web_jarvis', 'home_assistant', 'admin_jarvis'
        sa.Column('display_name', sa.String(200)),
        sa.Column('description', sa.Text()),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),

        # STT Configuration
        sa.Column('stt_engine', sa.String(50), nullable=False, server_default='faster-whisper'),
        sa.Column('stt_config', postgresql.JSONB(), server_default='{}'),  # Engine-specific config

        # TTS Configuration
        sa.Column('tts_engine', sa.String(50), nullable=False, server_default='piper'),
        sa.Column('tts_config', postgresql.JSONB(), server_default='{}'),  # Engine-specific config

        # Behavior Configuration
        sa.Column('wake_word_enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('wake_word', sa.String(100)),  # 'jarvis', 'athena', custom
        sa.Column('continuous_conversation', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('debug_mode', sa.Boolean(), server_default=sa.false(), nullable=False),

        # Rate Limiting
        sa.Column('max_requests_per_minute', sa.Integer(), server_default='30', nullable=False),

        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('interface_name'),
    )
    op.create_index('idx_voice_interfaces_enabled', 'voice_interfaces', ['enabled'])
    op.create_index('idx_voice_interfaces_name', 'voice_interfaces', ['interface_name'])

    # Seed default interfaces
    op.execute("""
        INSERT INTO voice_interfaces
            (interface_name, display_name, description, stt_engine, stt_config, tts_engine, tts_config, wake_word_enabled, wake_word)
        VALUES
            ('web_jarvis', 'Web Jarvis', 'Browser-based push-to-talk interface', 'faster-whisper',
             '{"model": "base.en", "language": "en"}',
             'piper', '{"voice": "en_US-amy-medium", "speed": 1.0}',
             false, null),

            ('home_assistant', 'Home Assistant', 'Wyoming protocol voice satellites', 'faster-whisper',
             '{"model": "small.en", "language": "en"}',
             'piper', '{"voice": "en_US-amy-medium", "speed": 1.0}',
             true, 'jarvis'),

            ('admin_jarvis', 'Admin Jarvis', 'Debug interface with real-time visualization', 'faster-whisper',
             '{"model": "base.en", "language": "en"}',
             'piper', '{"voice": "en_US-amy-medium", "speed": 1.0}',
             false, null)
    """)

    # =========================================================================
    # 2. STT Engines Table
    # Registry of available STT engine types
    # =========================================================================
    op.create_table(
        'stt_engines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('engine_name', sa.String(50), nullable=False),
        sa.Column('display_name', sa.String(100)),
        sa.Column('description', sa.Text()),
        sa.Column('endpoint_url', sa.String(500)),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('requires_gpu', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('is_cloud', sa.Boolean(), server_default=sa.false(), nullable=False),  # True for OpenAI, etc.
        sa.Column('default_config', postgresql.JSONB(), server_default='{}'),
        sa.Column('supported_languages', postgresql.JSONB(), server_default='["en"]'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('engine_name'),
    )
    op.create_index('idx_stt_engines_enabled', 'stt_engines', ['enabled'])

    # Seed default STT engines
    op.execute("""
        INSERT INTO stt_engines (engine_name, display_name, description, endpoint_url, requires_gpu, is_cloud, default_config) VALUES
            ('faster-whisper', 'Faster Whisper', 'CTranslate2-optimized Whisper (recommended)', 'http://localhost:8000', true, false,
             '{"model": "small.en", "language": "en", "beam_size": 5}'),
            ('speaches', 'Speaches', 'OpenAI-compatible Whisper server (Docker)', 'http://localhost:8000', true, false,
             '{"model": "Systran/faster-whisper-small", "language": "en"}'),
            ('whisper-cpp', 'Whisper.cpp', 'CPU-optimized Whisper (no GPU required)', 'http://localhost:8001', false, false,
             '{"model": "base.en"}'),
            ('openai-whisper', 'OpenAI Whisper API', 'Cloud-based Whisper API', 'https://api.openai.com/v1/audio/transcriptions', false, true,
             '{"model": "whisper-1"}'),
            ('web-speech', 'Web Speech API', 'Browser-native speech recognition', 'browser', false, false,
             '{"continuous": false, "interimResults": true}')
    """)

    # =========================================================================
    # 3. TTS Engines Table
    # Registry of available TTS engine types
    # =========================================================================
    op.create_table(
        'tts_engines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('engine_name', sa.String(50), nullable=False),
        sa.Column('display_name', sa.String(100)),
        sa.Column('description', sa.Text()),
        sa.Column('endpoint_url', sa.String(500)),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('requires_gpu', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('is_cloud', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('default_config', postgresql.JSONB(), server_default='{}'),
        sa.Column('available_voices', postgresql.JSONB(), server_default='[]'),  # List of voice IDs
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('engine_name'),
    )
    op.create_index('idx_tts_engines_enabled', 'tts_engines', ['enabled'])

    # Seed default TTS engines
    op.execute("""
        INSERT INTO tts_engines (engine_name, display_name, description, endpoint_url, requires_gpu, is_cloud, default_config, available_voices) VALUES
            ('piper', 'Piper TTS', 'Fast neural TTS with natural voices (recommended)', 'http://localhost:8880', false, false,
             '{"speed": 1.0, "noise_scale": 0.667}',
             '["en_US-amy-medium", "en_US-lessac-medium", "en_GB-alan-medium"]'),
            ('kokoro', 'Kokoro TTS', 'Ultra-low latency TTS (40-70ms)', 'http://localhost:8880', true, false,
             '{"voice": "am_puck"}',
             '["am_puck", "af_bella", "am_adam"]'),
            ('openai-tts', 'OpenAI TTS', 'Premium cloud TTS with natural voices', 'https://api.openai.com/v1/audio/speech', false, true,
             '{"model": "tts-1", "speed": 1.0}',
             '["alloy", "echo", "fable", "onyx", "nova", "shimmer"]'),
            ('elevenlabs', 'ElevenLabs', 'Premium voice cloning and synthesis', 'https://api.elevenlabs.io/v1/text-to-speech', false, true,
             '{"stability": 0.5, "similarity_boost": 0.75}',
             '[]'),
            ('web-speech', 'Web Speech API', 'Browser-native text-to-speech', 'browser', false, false,
             '{"rate": 1.0, "pitch": 1.0}',
             '[]')
    """)

    # =========================================================================
    # 4. Enhance Feature Table with config column
    # Add config JSONB for feature-specific configuration
    # =========================================================================
    op.add_column('features', sa.Column('config', postgresql.JSONB(), server_default='{}'))
    op.add_column('features', sa.Column('requires_restart', sa.Boolean(), server_default=sa.false()))

    # Add new feature flags for this plan
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority, config) VALUES
            -- Tool System
            ('tool_system_enabled', 'Tool System', 'Master switch for tool calling system', 'integration', true, true, 10, '{}'),
            ('mcp_integration', 'MCP Integration', 'Enable MCP protocol for dynamic tool discovery', 'integration', false, false, 50, '{"mcp_url": ""}'),
            ('n8n_integration', 'n8n Integration', 'Enable n8n workflow discovery via MCP', 'integration', false, false, 50, '{"n8n_url": "", "api_key": ""}'),
            ('legacy_tools_fallback', 'Legacy Tools Fallback', 'Use hardcoded tools as fallback', 'integration', true, false, 100, '{}'),

            -- Admin Features
            ('admin_jarvis', 'Admin Jarvis', 'Enable Admin Jarvis debug interface', 'admin', true, false, 10, '{}'),
            ('real_time_events', 'Real-Time Events', 'Emit real-time events for tool/intent tracking', 'admin', true, false, 20, '{}'),
            ('intent_visualization', 'Intent Visualization', 'Show intent classification in Admin Jarvis', 'admin', true, false, 30, '{}'),

            -- Experimental
            ('self_building_tools', 'Self-Building Tools', 'Allow LLM to create n8n workflows', 'experimental', false, false, 100, '{}'),
            ('livekit_webrtc', 'LiveKit WebRTC', 'Use LiveKit for real-time audio', 'experimental', false, false, 100, '{}')
        ON CONFLICT (name) DO NOTHING
    """)

    # =========================================================================
    # 5. Enhance ToolRegistry with MCP columns
    # Add source tracking for static/mcp/legacy tools
    # =========================================================================
    op.add_column('tool_registry', sa.Column('source', sa.String(20), server_default='static'))
    op.add_column('tool_registry', sa.Column('mcp_endpoint', sa.String(500)))
    op.add_column('tool_registry', sa.Column('last_discovered_at', sa.DateTime(timezone=True)))
    op.add_column('tool_registry', sa.Column('discovery_metadata', postgresql.JSONB(), server_default='{}'))

    op.create_index('idx_tool_registry_source', 'tool_registry', ['source'])

    # =========================================================================
    # 6. MCP Security Configuration Table
    # Security settings for MCP tool discovery
    # =========================================================================
    op.create_table(
        'mcp_security',
        sa.Column('id', sa.Integer(), nullable=False),

        # Domain restrictions
        sa.Column('allowed_domains', postgresql.JSONB(), server_default='[]'),  # e.g., ["localhost", "localhost"]
        sa.Column('blocked_domains', postgresql.JSONB(), server_default='[]'),

        # Execution limits
        sa.Column('max_execution_time_ms', sa.Integer(), server_default='30000', nullable=False),  # 30 second timeout
        sa.Column('max_concurrent_tools', sa.Integer(), server_default='5', nullable=False),

        # Approval workflow
        sa.Column('require_owner_approval', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('auto_approve_patterns', postgresql.JSONB(), server_default='[]'),  # Tool name patterns for auto-approval

        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    # Insert default security config
    op.execute("""
        INSERT INTO mcp_security
            (allowed_domains, blocked_domains, max_execution_time_ms, max_concurrent_tools, require_owner_approval, auto_approve_patterns)
        VALUES
            ('["localhost", "localhost"]', '[]', 30000, 5, true, '[]')
    """)

    # =========================================================================
    # 7. Tool Approval Queue Table
    # Queue for approving discovered MCP tools
    # =========================================================================
    op.create_table(
        'tool_approval_queue',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tool_name', sa.String(200), nullable=False),
        sa.Column('tool_source', sa.String(50), nullable=False),  # 'mcp', 'n8n'
        sa.Column('discovery_url', sa.String(500)),
        sa.Column('input_schema', postgresql.JSONB()),
        sa.Column('description', sa.Text()),
        sa.Column('status', sa.String(20), server_default='pending', nullable=False),  # 'pending', 'approved', 'rejected'
        sa.Column('approved_by_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('rejection_reason', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_tool_approval_status', 'tool_approval_queue', ['status'])
    op.create_index('idx_tool_approval_source', 'tool_approval_queue', ['tool_source'])

    # =========================================================================
    # 8. Pipeline Events Table
    # Store real-time events for Admin Jarvis visualization
    # =========================================================================
    op.create_table(
        'pipeline_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(100), nullable=False),
        sa.Column('event_type', sa.String(50), nullable=False),  # 'stt_start', 'intent_classified', 'tool_executing', etc.
        sa.Column('event_data', postgresql.JSONB(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('interface', sa.String(50)),  # 'web_jarvis', 'home_assistant', 'admin_jarvis'
        sa.Column('duration_ms', sa.Integer()),  # Time since previous event
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_pipeline_events_session', 'pipeline_events', ['session_id'])
    op.create_index('idx_pipeline_events_timestamp', 'pipeline_events', ['timestamp'])
    op.create_index('idx_pipeline_events_type', 'pipeline_events', ['event_type'])

    # =========================================================================
    # 9. Add API key rotation tracking to external_api_keys
    # =========================================================================
    op.add_column('external_api_keys', sa.Column('last_rotated_at', sa.DateTime(timezone=True)))
    op.add_column('external_api_keys', sa.Column('rotation_reminder_days', sa.Integer(), server_default='90'))
    op.add_column('external_api_keys', sa.Column('expires_at', sa.DateTime(timezone=True)))


def downgrade() -> None:
    # Remove API key rotation columns
    op.drop_column('external_api_keys', 'expires_at')
    op.drop_column('external_api_keys', 'rotation_reminder_days')
    op.drop_column('external_api_keys', 'last_rotated_at')

    # Drop pipeline_events
    op.drop_index('idx_pipeline_events_type')
    op.drop_index('idx_pipeline_events_timestamp')
    op.drop_index('idx_pipeline_events_session')
    op.drop_table('pipeline_events')

    # Drop tool_approval_queue
    op.drop_index('idx_tool_approval_source')
    op.drop_index('idx_tool_approval_status')
    op.drop_table('tool_approval_queue')

    # Drop mcp_security
    op.drop_table('mcp_security')

    # Remove ToolRegistry MCP columns
    op.drop_index('idx_tool_registry_source')
    op.drop_column('tool_registry', 'discovery_metadata')
    op.drop_column('tool_registry', 'last_discovered_at')
    op.drop_column('tool_registry', 'mcp_endpoint')
    op.drop_column('tool_registry', 'source')

    # Remove new feature flags (can't easily remove rows, so leave them)

    # Remove Feature enhancements
    op.drop_column('features', 'requires_restart')
    op.drop_column('features', 'config')

    # Drop tts_engines
    op.drop_index('idx_tts_engines_enabled')
    op.drop_table('tts_engines')

    # Drop stt_engines
    op.drop_index('idx_stt_engines_enabled')
    op.drop_table('stt_engines')

    # Drop voice_interfaces
    op.drop_index('idx_voice_interfaces_name')
    op.drop_index('idx_voice_interfaces_enabled')
    op.drop_table('voice_interfaces')
