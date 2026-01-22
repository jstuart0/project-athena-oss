"""
Add anti-hallucination and multi-intent configuration tables

Revision ID: 003
Revises: 002
Create Date: 2025-11-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '003'
down_revision = None  # Base migration
branch_labels = None
depends_on = None


def upgrade():
    """Add tables for anti-hallucination and multi-intent configuration"""

    # 0. Intent Categories (must be first for foreign keys)
    op.create_table(
        'intent_categories',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('parent_id', sa.Integer(), sa.ForeignKey('intent_categories.id')),
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('priority', sa.Integer(), default=100),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # Create indexes for intent_categories
    op.create_index('idx_intent_categories_enabled', 'intent_categories', ['enabled'])
    op.create_index('idx_intent_categories_parent_id', 'intent_categories', ['parent_id'])

    # Seed initial intent categories
    op.execute("""
        INSERT INTO intent_categories
        (name, display_name, description, enabled, priority)
        VALUES
        ('control', 'Device Control', 'Home automation device control intents', true, 100),
        ('query', 'Information Query', 'General information and knowledge queries', true, 90),
        ('rag', 'RAG Queries', 'Queries requiring external data sources', true, 80),
        ('weather', 'Weather', 'Weather information queries', true, 70),
        ('sports', 'Sports', 'Sports scores and information', true, 70),
        ('flights', 'Flights', 'Flight tracking and airport information', true, 70),
        ('routine', 'Routines', 'Multi-step automated routines', true, 60)
    """)

    # 1. Anti-Hallucination Validation Rules (Enhanced)
    op.create_table(
        'hallucination_checks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('check_type', sa.String(50), nullable=False),
        # Types: 'required_elements', 'fact_checking', 'confidence_threshold', 'cross_validation'
        sa.Column('applies_to_categories', postgresql.ARRAY(sa.String), default=[]),
        # Empty array means applies to all categories
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('severity', sa.String(20), default='warning'),
        # 'error' (blocks response), 'warning' (logs but allows), 'info'
        sa.Column('configuration', postgresql.JSONB, nullable=False),
        # Flexible JSON config for different check types
        sa.Column('error_message_template', sa.Text()),
        sa.Column('auto_fix_enabled', sa.Boolean(), default=False),
        sa.Column('auto_fix_prompt_template', sa.Text()),
        sa.Column('require_cross_model_validation', sa.Boolean(), default=False),
        sa.Column('confidence_threshold', sa.Float(), default=0.7),
        sa.Column('priority', sa.Integer(), default=100),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('created_by', sa.String(100))
    )

    # 2. Cross-Model Validation Configuration
    op.create_table(
        'cross_validation_models',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('model_id', sa.String(100), nullable=False),
        # e.g., 'phi3:mini', 'llama3.1:8b-q4'
        sa.Column('model_type', sa.String(50), nullable=False),
        # 'primary', 'validation', 'fallback'
        sa.Column('endpoint_url', sa.String(500)),
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('use_for_categories', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('temperature', sa.Float(), default=0.1),
        sa.Column('max_tokens', sa.Integer(), default=200),
        sa.Column('timeout_seconds', sa.Integer(), default=30),
        sa.Column('weight', sa.Float(), default=1.0),
        # Weight for ensemble validation
        sa.Column('min_confidence_required', sa.Float(), default=0.5),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # 3. Multi-Intent Configuration
    op.create_table(
        'multi_intent_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('max_intents_per_query', sa.Integer(), default=3),
        sa.Column('separators', postgresql.ARRAY(sa.String),
                  default=[' and ', ' then ', ' also ', ', then ', '; ']),
        sa.Column('context_preservation', sa.Boolean(), default=True),
        # Whether to preserve context between split intents
        sa.Column('parallel_processing', sa.Boolean(), default=False),
        # Process intents in parallel vs sequential
        sa.Column('combination_strategy', sa.String(50), default='concatenate'),
        # 'concatenate', 'summarize', 'hierarchical'
        sa.Column('min_words_per_intent', sa.Integer(), default=2),
        sa.Column('context_words_to_preserve', postgresql.ARRAY(sa.String), default=[]),
        # Words to carry forward if missing in split intent
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # 4. Intent Chain Rules
    op.create_table(
        'intent_chain_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('trigger_pattern', sa.String(500)),
        # Regex pattern that triggers this chain
        sa.Column('intent_sequence', postgresql.ARRAY(sa.String), nullable=False),
        # Ordered list of intents to execute
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('description', sa.Text()),
        sa.Column('examples', postgresql.ARRAY(sa.String)),
        sa.Column('require_all', sa.Boolean(), default=False),
        # Whether all intents in chain must succeed
        sa.Column('stop_on_error', sa.Boolean(), default=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # 5. Validation Test Scenarios
    op.create_table(
        'validation_test_scenarios',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('test_query', sa.Text(), nullable=False),
        sa.Column('initial_response', sa.Text(), nullable=False),
        sa.Column('expected_validation_result', sa.String(20)),
        # 'pass', 'fail', 'warning'
        sa.Column('expected_checks_triggered', postgresql.ARRAY(sa.String)),
        sa.Column('expected_final_response', sa.Text()),
        sa.Column('category', sa.String(50)),
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('last_run_result', postgresql.JSONB),
        sa.Column('last_run_date', sa.TIMESTAMP()),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # 6. Confidence Score Rules
    op.create_table(
        'confidence_score_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('category_id', sa.Integer(),
                  sa.ForeignKey('intent_categories.id', ondelete='CASCADE')),
        sa.Column('factor_name', sa.String(100), nullable=False),
        # 'pattern_match_count', 'entity_presence', 'query_length', etc.
        sa.Column('factor_type', sa.String(50), nullable=False),
        # 'boost', 'penalty', 'multiplier'
        sa.Column('condition', postgresql.JSONB),
        # e.g., {"min_matches": 2, "required_entities": ["room", "device"]}
        sa.Column('adjustment_value', sa.Float(), nullable=False),
        # Amount to adjust confidence by
        sa.Column('max_impact', sa.Float(), default=0.2),
        # Maximum impact this rule can have
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # 7. Response Enhancement Rules
    op.create_table(
        'response_enhancement_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('category_id', sa.Integer(),
                  sa.ForeignKey('intent_categories.id', ondelete='CASCADE')),
        sa.Column('enhancement_type', sa.String(50), nullable=False),
        # 'add_context', 'format_data', 'add_suggestions', 'clarify_ambiguity'
        sa.Column('trigger_condition', postgresql.JSONB),
        # When to apply this enhancement
        sa.Column('enhancement_template', sa.Text()),
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('priority', sa.Integer(), default=100),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'))
    )

    # Seed initial data
    op.execute("""
        INSERT INTO hallucination_checks
        (name, display_name, check_type, configuration, error_message_template, severity, enabled)
        VALUES
        ('score_check', 'Sports Score Validation', 'required_elements',
         '{"patterns": ["\\\\d+", "won", "lost", "beat", "defeated"], "query_patterns": ["score", "result", "game"]}',
         'Response must include actual scores or game results', 'error', true),

        ('time_check', 'Time Information Validation', 'required_elements',
         '{"patterns": ["\\\\d{1,2}:\\\\d{2}", "am", "pm", "morning", "evening"], "query_patterns": ["when", "what time", "schedule"]}',
         'Response must include time or schedule information', 'warning', true),

        ('weather_check', 'Weather Data Validation', 'required_elements',
         '{"patterns": ["degrees", "°", "sunny", "cloudy", "rain"], "query_patterns": ["weather", "temperature", "forecast"]}',
         'Response must include weather information', 'warning', true),

        ('location_check', 'Location Information Validation', 'required_elements',
         '{"patterns": ["street", "miles", "blocks", "near", "located"], "query_patterns": ["where", "location", "address", "how far"]}',
         'Response must include location or distance information', 'warning', true),

        ('confidence_threshold_check', 'Low Confidence Detection', 'confidence_threshold',
         '{"min_confidence": 0.5, "require_cross_validation_below": 0.3}',
         'Response confidence too low, additional validation required', 'warning', true),

        ('fact_consistency', 'Fact Consistency Check', 'fact_checking',
         '{"check_numbers": true, "check_entities": true, "check_dates": true}',
         'Response contains potentially inconsistent information', 'warning', true)
    """)

    op.execute("""
        INSERT INTO cross_validation_models
        (name, model_id, model_type, enabled, temperature, weight, min_confidence_required)
        VALUES
        ('Primary Model', 'phi3:mini-q8', 'primary', true, 0.7, 1.0, 0.0),
        ('Validation Model', 'phi3:mini', 'validation', true, 0.1, 0.8, 0.5),
        ('Fallback Model', 'llama3.1:8b-q4', 'fallback', true, 0.5, 0.6, 0.3)
    """)

    op.execute("""
        INSERT INTO multi_intent_config
        (enabled, max_intents_per_query, parallel_processing, combination_strategy)
        VALUES
        (true, 3, false, 'concatenate')
    """)

    op.execute("""
        INSERT INTO intent_chain_rules
        (name, trigger_pattern, intent_sequence, description, enabled)
        VALUES
        ('Goodnight Routine', 'goodnight|good night|bedtime',
         ARRAY['control', 'control', 'control'],
         'Turns off lights, locks doors, sets thermostat', true),

        ('Morning Routine', 'good morning|wake up|morning routine',
         ARRAY['control', 'weather', 'control'],
         'Turns on lights, gets weather, starts coffee', true),

        ('Leaving Home', 'leaving|going out|bye',
         ARRAY['control', 'control', 'location'],
         'Locks doors, turns off lights, sets away mode', true)
    """)

    # Add indexes
    op.create_index('idx_hallucination_checks_enabled', 'hallucination_checks', ['enabled'])
    op.create_index('idx_hallucination_checks_categories', 'hallucination_checks', ['applies_to_categories'])
    op.create_index('idx_cross_validation_enabled', 'cross_validation_models', ['enabled', 'model_type'])
    op.create_index('idx_chain_rules_enabled', 'intent_chain_rules', ['enabled'])
    op.create_index('idx_confidence_rules_category', 'confidence_score_rules', ['category_id', 'enabled'])


def downgrade():
    """Remove anti-hallucination and multi-intent tables"""
    op.drop_table('response_enhancement_rules')
    op.drop_table('confidence_score_rules')
    op.drop_table('validation_test_scenarios')
    op.drop_table('intent_chain_rules')
    op.drop_table('multi_intent_config')
    op.drop_table('cross_validation_models')
    op.drop_table('hallucination_checks')
    op.drop_table('intent_categories')