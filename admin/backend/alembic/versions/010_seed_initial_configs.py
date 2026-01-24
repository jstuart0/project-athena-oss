"""Seed initial configuration data

Revision ID: 010
Revises: 009
Create Date: 2025-11-17

This migration seeds initial configuration data for:
- LLM backends (qwen3:4b - default OSS model)
- Cross-validation models
- Hallucination checks
- Multi-intent configuration

This ensures the admin UI has data to display and edit on first deployment.
Note: Additional models (llama3.2:3b, phi3:mini) are seeded in migration 050.
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime
import os

# revision identifiers, used by Alembic
revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None

# Get Ollama URL from environment (NOT hardcoded localhost)
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')


def upgrade():
    """Seed initial configuration data."""

    # ============================================================================
    # 1. Seed LLM Backends (minimal - full seeding in migration 050)
    # ============================================================================
    # Only seed qwen3:4b here as the default OSS model.
    # Migration 050 adds llama3.2:3b, phi3:mini, and full model configurations.
    ollama_url = OLLAMA_URL
    op.execute(f"""
        INSERT INTO llm_backends (model_name, backend_type, endpoint_url, enabled, priority,
                                 max_tokens, temperature_default, timeout_seconds, keep_alive_seconds,
                                 description, created_at, updated_at, total_requests, total_errors)
        VALUES
            ('qwen3:4b', 'ollama', '{ollama_url}', true, 50,
             4096, 0.7, 90, -1,
             'Qwen 3 4B - Default OSS model with good reasoning capabilities',
             NOW(), NOW(), 0, 0)
        ON CONFLICT (model_name) DO NOTHING
    """)

    # ============================================================================
    # 2. Seed Cross-Validation Models
    # ============================================================================
    op.execute(f"""
        INSERT INTO cross_validation_models (name, model_id, model_type, endpoint_url, enabled,
                                            use_for_categories, temperature, max_tokens,
                                            timeout_seconds, weight, min_confidence_required, created_at)
        VALUES
            ('qwen3-primary', 'qwen3:4b', 'primary', '{ollama_url}', true,
             ARRAY['home_control', 'weather', 'sports']::text[], 0.1, 200, 30, 1.0, 0.7, NOW()),

            ('qwen3-validation', 'qwen3:4b', 'validation', '{ollama_url}', true,
             ARRAY['home_control']::text[], 0.1, 200, 30, 0.8, 0.6, NOW())
        ON CONFLICT (name) DO NOTHING
    """)

    # ============================================================================
    # 3. Seed Hallucination Checks
    # ============================================================================
    op.execute("""
        INSERT INTO hallucination_checks (name, display_name, description, check_type,
                                         applies_to_categories, enabled, severity, configuration,
                                         error_message_template, auto_fix_enabled,
                                         require_cross_model_validation, confidence_threshold,
                                         priority, created_at, updated_at)
        VALUES
            ('weather_location_required', 'Weather Location Check',
             'Ensures weather queries include a valid location', 'required_elements',
             ARRAY['weather']::text[], true, 'error',
             '{"required_fields": ["location"]}'::jsonb,
             'Weather query must include a location. Please specify where.',
             false, false, 0.7, 100, NOW(), NOW()),

            ('home_device_validation', 'Home Device Validation',
             'Validates that referenced devices exist in Home Assistant', 'fact_checking',
             ARRAY['home_control']::text[], true, 'warning',
             '{"check_ha_entities": true, "allow_fuzzy_match": true}'::jsonb,
             'Device not found in your home. Did you mean: {suggestions}?',
             true, false, 0.8, 90, NOW(), NOW()),

            ('confidence_threshold', 'Confidence Threshold Check',
             'Requires minimum confidence score for all responses', 'confidence_threshold',
             ARRAY[]::text[], true, 'warning',
             '{"min_confidence": 0.6, "reject_low_confidence": false}'::jsonb,
             'Response confidence is low. Consider asking for clarification.',
             false, false, 0.6, 80, NOW(), NOW()),

            ('cross_model_agreement', 'Cross-Model Agreement',
             'Validates responses using multiple models for critical actions', 'cross_validation',
             ARRAY['home_control']::text[], true, 'error',
             '{"require_agreement": true, "min_models": 2, "agreement_threshold": 0.8}'::jsonb,
             'Multiple models disagree on this action. Validation failed.',
             false, true, 0.8, 95, NOW(), NOW())
        ON CONFLICT (name) DO NOTHING
    """)

    # ============================================================================
    # 4. Seed Multi-Intent Configuration
    # ============================================================================
    # Check if config already exists
    op.execute("""
        INSERT INTO multi_intent_config (id, enabled, max_intents_per_query, separators,
                                        context_preservation, parallel_processing,
                                        combination_strategy, min_words_per_intent,
                                        context_words_to_preserve, updated_at)
        SELECT 1, true, 3,
               ARRAY[' and ', ' then ', ' also ', ', then ', '; ']::text[],
               true, false, 'concatenate', 2,
               ARRAY['the', 'my', 'in', 'at', 'to']::text[],
               NOW()
        WHERE NOT EXISTS (SELECT 1 FROM multi_intent_config WHERE id = 1)
    """)

    # ============================================================================
    # 5. Seed Intent Chain Rules
    # ============================================================================
    # Delete existing rules first (no unique constraint on name)
    op.execute("""
        DELETE FROM intent_chain_rules WHERE name IN ('goodnight_routine', 'leaving_home')
    """)

    op.execute("""
        INSERT INTO intent_chain_rules (name, trigger_pattern, intent_sequence, enabled,
                                       description, examples, require_all, stop_on_error, created_at)
        VALUES
            ('goodnight_routine', '(?i)goodnight|good night',
             ARRAY['turn_off_lights', 'lock_doors', 'set_thermostat_night']::text[], true,
             'Automated goodnight routine - turns off lights, locks doors, adjusts thermostat',
             ARRAY['goodnight', 'good night', 'time for bed']::text[],
             false, true, NOW()),

            ('leaving_home', '(?i)(leaving|heading out|going out)',
             ARRAY['lock_doors', 'turn_off_lights', 'set_thermostat_away']::text[], true,
             'Leaving home routine - secures house and saves energy',
             ARRAY['I''m leaving', 'heading out', 'leaving now']::text[],
             false, true, NOW())
    """)

    print("✓ Seeded initial configuration data:")
    print("  - 1 LLM backend (qwen3:4b - default)")
    print("  - 2 Cross-validation models")
    print("  - 4 Hallucination checks")
    print("  - 1 Multi-intent config")
    print("  - 2 Intent chain rules")
    print("  Note: Additional models seeded in migration 050")


def downgrade():
    """Remove seeded configuration data."""

    # Remove in reverse order due to foreign key constraints
    op.execute("DELETE FROM intent_chain_rules WHERE name IN ('goodnight_routine', 'leaving_home')")
    op.execute("DELETE FROM multi_intent_config WHERE id = 1")
    op.execute("DELETE FROM hallucination_checks WHERE name IN ('weather_location_required', 'home_device_validation', 'confidence_threshold', 'cross_model_agreement')")
    op.execute("DELETE FROM cross_validation_models WHERE name IN ('qwen3-primary', 'qwen3-validation')")
    op.execute("DELETE FROM llm_backends WHERE model_name = 'qwen3:4b'")

    print("✓ Removed seeded configuration data")
