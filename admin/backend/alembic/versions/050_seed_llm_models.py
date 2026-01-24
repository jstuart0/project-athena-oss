"""Seed LLM models and component assignments

Revision ID: 050
Revises: 049
Create Date: 2025-01-24

This migration seeds complete LLM configuration:
- LLM backends (llama3.2:3b, phi3:mini, qwen3:4b)
- Model configurations with full parameters
- Component model assignments for all orchestrator components

All models point to the OLLAMA_URL environment variable endpoint.
Default model is configurable via ATHENA_DEFAULT_MODEL env var.
"""

import os
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers, used by Alembic
revision = '050'
down_revision = '049'
branch_labels = None
depends_on = None

# Get default model from environment, fallback to qwen3:4b for OSS
DEFAULT_MODEL = os.getenv('ATHENA_DEFAULT_MODEL', 'qwen3:4b')


def upgrade():
    """Seed LLM models, configurations, and component assignments."""

    # ============================================================================
    # 1. Seed LLM Backends
    # ============================================================================
    # These are the available models that can be used by the system.
    # endpoint_url uses localhost as placeholder - actual URL comes from OLLAMA_URL env var
    op.execute("""
        INSERT INTO llm_backends (
            model_name, backend_type, endpoint_url, enabled, priority,
            max_tokens, temperature_default, timeout_seconds, keep_alive_seconds,
            description, created_at, updated_at, total_requests, total_errors
        )
        VALUES
            ('qwen3:4b', 'ollama', 'http://localhost:11434', true, 50,
             4096, 0.7, 90, -1,
             'Qwen 3 4B - Default OSS model with good reasoning capabilities',
             NOW(), NOW(), 0, 0),

            ('llama3.2:3b', 'ollama', 'http://localhost:11434', true, 50,
             4096, 0.7, 90, -1,
             'Llama 3.2 3B - Fast, efficient model for general tasks',
             NOW(), NOW(), 0, 0),

            ('phi3:mini', 'ollama', 'http://localhost:11434', true, 50,
             4096, 0.7, 90, -1,
             'Phi-3 Mini - Microsoft small model, good for classification',
             NOW(), NOW(), 0, 0)
        ON CONFLICT (model_name) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            description = EXCLUDED.description,
            updated_at = NOW()
    """)

    # ============================================================================
    # 2. Seed Model Configurations
    # ============================================================================
    # Detailed per-model settings including Ollama-specific options
    op.execute("""
        INSERT INTO model_configurations (
            model_name, display_name, backend_type, enabled,
            temperature, max_tokens, timeout_seconds, keep_alive_seconds,
            ollama_options, mlx_options, description, priority, created_at, updated_at
        )
        VALUES
            ('qwen3:4b', 'Qwen 3 4B', 'ollama', true,
             0.7, 4096, 90, -1,
             '{"num_ctx": 4096}'::jsonb, '{}'::jsonb,
             'Alibaba Qwen 3 4B - Default OSS model with strong reasoning capabilities',
             50, NOW(), NOW()),

            ('llama3.2:3b', 'Llama 3.2 3B', 'ollama', true,
             0.7, 4096, 90, -1,
             '{"num_ctx": 4096}'::jsonb, '{}'::jsonb,
             'Meta Llama 3.2 3B - Fast and efficient for general tasks',
             50, NOW(), NOW()),

            ('phi3:mini', 'Phi-3 Mini', 'ollama', true,
             0.7, 4096, 90, -1,
             '{"num_ctx": 4096}'::jsonb, '{}'::jsonb,
             'Microsoft Phi-3 Mini - Excellent for classification tasks',
             50, NOW(), NOW()),

            ('_default', 'Default Configuration', 'auto', true,
             0.7, 4096, 90, -1,
             '{}'::jsonb, '{}'::jsonb,
             'Fallback configuration for unregistered models',
             0, NOW(), NOW())
        ON CONFLICT (model_name) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            enabled = EXCLUDED.enabled,
            temperature = EXCLUDED.temperature,
            max_tokens = EXCLUDED.max_tokens,
            timeout_seconds = EXCLUDED.timeout_seconds,
            keep_alive_seconds = EXCLUDED.keep_alive_seconds,
            ollama_options = EXCLUDED.ollama_options,
            description = EXCLUDED.description,
            updated_at = NOW()
    """)

    # ============================================================================
    # 3. Seed Component Model Assignments
    # ============================================================================
    # Map each orchestrator component to its default model.
    # Using parameterized default model from environment variable.
    default_model = DEFAULT_MODEL

    components = [
        ('intent_classifier', 'Intent Classifier', 'Classifies user queries into intent categories', 'orchestrator'),
        ('intent_discovery', 'Intent Discovery', 'Discovers novel intents not matching known categories', 'orchestrator'),
        ('response_synthesis', 'Response Synthesis', 'Generates final responses from retrieved data', 'orchestrator'),
        ('conversation_summarizer', 'Conversation Summarizer', 'Summarizes conversation history for context', 'orchestrator'),
        ('tool_calling_simple', 'Tool Calling (Simple)', 'Handles simple single-tool invocations', 'orchestrator'),
        ('tool_calling_complex', 'Tool Calling (Complex)', 'Handles multi-step tool orchestration', 'orchestrator'),
        ('tool_calling_super_complex', 'Tool Calling (Super Complex)', 'Handles advanced reasoning with multiple tools', 'orchestrator'),
        ('smart_home_control', 'Smart Home Control', 'Processes Home Assistant commands', 'control'),
        ('response_validator_primary', 'Response Validator (Primary)', 'Primary validation of generated responses', 'validation'),
        ('response_validator_secondary', 'Response Validator (Secondary)', 'Secondary validation for critical responses', 'validation'),
        ('fact_check_validation', 'Fact Check Validation', 'Validates factual accuracy of responses', 'validation'),
    ]

    for component_name, display_name, description, category in components:
        op.execute(f"""
            INSERT INTO component_model_assignments (
                component_name, display_name, description, category,
                model_name, backend_type, enabled, created_at, updated_at
            )
            VALUES (
                '{component_name}', '{display_name}', '{description}', '{category}',
                '{default_model}', 'ollama', true, NOW(), NOW()
            )
            ON CONFLICT (component_name) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                description = EXCLUDED.description,
                category = EXCLUDED.category,
                updated_at = NOW()
        """)

    print("✓ Seeded LLM configuration:")
    print("  - 3 LLM backends (qwen3:4b, llama3.2:3b, phi3:mini)")
    print("  - 4 Model configurations (including _default)")
    print(f"  - 11 Component assignments (default model: {default_model})")


def downgrade():
    """Remove seeded LLM configuration data."""

    # Remove component assignments
    components = [
        'intent_classifier', 'intent_discovery', 'response_synthesis',
        'conversation_summarizer', 'tool_calling_simple', 'tool_calling_complex',
        'tool_calling_super_complex', 'smart_home_control',
        'response_validator_primary', 'response_validator_secondary', 'fact_check_validation'
    ]
    component_list = "', '".join(components)
    op.execute(f"DELETE FROM component_model_assignments WHERE component_name IN ('{component_list}')")

    # Remove model configurations
    op.execute("DELETE FROM model_configurations WHERE model_name IN ('qwen3:4b', 'llama3.2:3b', 'phi3:mini', '_default')")

    # Remove LLM backends
    op.execute("DELETE FROM llm_backends WHERE model_name IN ('qwen3:4b', 'llama3.2:3b', 'phi3:mini')")

    print("✓ Removed seeded LLM configuration data")
