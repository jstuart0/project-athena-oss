"""Add performance_presets table

Revision ID: 039
Revises: 038
Create Date: 2026-01-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '039_performance_presets'
down_revision = '038_rag_tool_ports'
branch_labels = None
depends_on = None


def upgrade():
    # Create performance_presets table
    op.create_table(
        'performance_presets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_system', sa.Boolean(), nullable=False, default=False),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=False),
        sa.Column('settings', JSONB, nullable=False, default=dict),
        sa.Column('estimated_latency_ms', sa.Integer(), nullable=True),
        sa.Column('icon', sa.String(10), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    # Create index on is_active for fast lookup
    op.create_index('ix_performance_presets_is_active', 'performance_presets', ['is_active'])
    op.create_index('ix_performance_presets_is_system', 'performance_presets', ['is_system'])

    # Seed system presets
    op.execute("""
        INSERT INTO performance_presets (name, description, is_system, is_active, settings, estimated_latency_ms, icon) VALUES
        (
            'Maximum Speed',
            'Optimized for fastest response times. Uses lightweight models and aggressive caching. Best for simple queries.',
            true, false,
            '{
                "gateway_intent_model": "phi3:mini",
                "gateway_intent_temperature": 0.1,
                "gateway_intent_max_tokens": 10,
                "intent_classifier_model": "qwen2.5:1.5b",
                "tool_calling_simple_model": "phi3:mini",
                "tool_calling_complex_model": "phi3:mini",
                "tool_calling_super_complex_model": "qwen2.5:7b",
                "response_synthesis_model": "phi3:mini",
                "llm_temperature": 0.3,
                "llm_max_tokens": 256,
                "llm_keep_alive_seconds": -1,
                "history_mode": "none",
                "max_llm_history_messages": 0,
                "feature_flags": {
                    "ha_room_detection_cache": true,
                    "ha_simple_command_fastpath": true,
                    "ha_parallel_init": true,
                    "ha_precomputed_summaries": true,
                    "ha_session_warmup": true,
                    "ha_intent_prerouting": true
                }
            }',
            1500,
            E'\\u26A1'
        ),
        (
            'Balanced',
            'Good balance of speed and accuracy. Default recommendation for most users.',
            true, false,
            '{
                "gateway_intent_model": "phi3:mini",
                "gateway_intent_temperature": 0.1,
                "gateway_intent_max_tokens": 10,
                "intent_classifier_model": "qwen2.5:1.5b",
                "tool_calling_simple_model": "qwen2.5:7b",
                "tool_calling_complex_model": "qwen2.5:7b",
                "tool_calling_super_complex_model": "qwen2.5:14b-instruct-q4_K_M",
                "response_synthesis_model": "qwen2.5:7b",
                "llm_temperature": 0.5,
                "llm_max_tokens": 512,
                "llm_keep_alive_seconds": -1,
                "history_mode": "summarized",
                "max_llm_history_messages": 5,
                "feature_flags": {
                    "ha_room_detection_cache": true,
                    "ha_simple_command_fastpath": true,
                    "ha_parallel_init": true,
                    "ha_precomputed_summaries": true,
                    "ha_session_warmup": false,
                    "ha_intent_prerouting": false
                }
            }',
            2500,
            E'\\u2696'
        ),
        (
            'Maximum Accuracy',
            'Prioritizes response quality over speed. Uses larger models and full conversation history.',
            true, false,
            '{
                "gateway_intent_model": "qwen2.5:7b",
                "gateway_intent_temperature": 0.1,
                "gateway_intent_max_tokens": 15,
                "intent_classifier_model": "qwen2.5:7b",
                "tool_calling_simple_model": "qwen2.5:14b-instruct-q4_K_M",
                "tool_calling_complex_model": "qwen2.5:14b-instruct-q4_K_M",
                "tool_calling_super_complex_model": "qwen2.5:14b-instruct-q4_K_M",
                "response_synthesis_model": "qwen2.5:14b-instruct-q4_K_M",
                "llm_temperature": 0.7,
                "llm_max_tokens": 1024,
                "llm_keep_alive_seconds": -1,
                "history_mode": "full",
                "max_llm_history_messages": 10,
                "feature_flags": {
                    "ha_room_detection_cache": true,
                    "ha_simple_command_fastpath": false,
                    "ha_parallel_init": true,
                    "ha_precomputed_summaries": false,
                    "ha_session_warmup": false,
                    "ha_intent_prerouting": false
                }
            }',
            4500,
            E'\\U0001F3AF'
        ),
        (
            'Pre-existing Configuration',
            'The original configuration before presets were introduced. Maintains compatibility with previous settings.',
            true, true,
            '{
                "gateway_intent_model": "phi3:mini",
                "gateway_intent_temperature": 0.1,
                "gateway_intent_max_tokens": 10,
                "intent_classifier_model": "qwen2.5:1.5b",
                "tool_calling_simple_model": "qwen2.5:7b",
                "tool_calling_complex_model": "qwen2.5:7b",
                "tool_calling_super_complex_model": "qwen2.5:14b-instruct-q4_K_M",
                "response_synthesis_model": "qwen2.5:7b",
                "llm_temperature": 0.7,
                "llm_max_tokens": 512,
                "llm_keep_alive_seconds": -1,
                "history_mode": "full",
                "max_llm_history_messages": 10,
                "feature_flags": {
                    "ha_room_detection_cache": true,
                    "ha_simple_command_fastpath": true,
                    "ha_parallel_init": true,
                    "ha_precomputed_summaries": true,
                    "ha_session_warmup": true,
                    "ha_intent_prerouting": true
                }
            }',
            2800,
            E'\\U0001F4BE'
        );
    """)


def downgrade():
    op.drop_index('ix_performance_presets_is_system')
    op.drop_index('ix_performance_presets_is_active')
    op.drop_table('performance_presets')
