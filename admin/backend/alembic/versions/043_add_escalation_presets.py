"""Add escalation presets and rules tables.

Revision ID: 043_add_escalation_presets
Revises: 042_hybrid_memory
Create Date: 2026-01-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '043_add_escalation_presets'
down_revision = '042_hybrid_memory'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Escalation presets table
    op.create_table(
        'escalation_presets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('auto_activate_conditions', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_escalation_preset_name')
    )
    op.create_index('idx_escalation_presets_active', 'escalation_presets', ['is_active'])

    # 2. Escalation rules table
    op.create_table(
        'escalation_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('preset_id', sa.Integer(), nullable=False),
        sa.Column('rule_name', sa.String(100), nullable=False),
        sa.Column('trigger_type', sa.String(50), nullable=False),
        sa.Column('trigger_patterns', postgresql.JSONB(), nullable=False),
        sa.Column('escalation_target', sa.String(20), nullable=False),
        sa.Column('escalation_duration', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['preset_id'], ['escalation_presets.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_escalation_rules_preset', 'escalation_rules', ['preset_id'])
    op.create_index('idx_escalation_rules_enabled', 'escalation_rules', ['enabled'])
    op.create_index('idx_escalation_rules_type', 'escalation_rules', ['trigger_type'])

    # 3. Escalation state table (tracks current escalation per session)
    op.create_table(
        'escalation_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(255), nullable=False),
        sa.Column('escalated_to', sa.String(20), nullable=False),
        sa.Column('triggered_by_rule_id', sa.Integer(), nullable=True),
        sa.Column('turns_remaining', sa.Integer(), nullable=True),
        sa.Column('is_manual_override', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('override_reason', sa.Text(), nullable=True),
        sa.Column('escalated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['triggered_by_rule_id'], ['escalation_rules.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('session_id', name='uq_escalation_state_session')
    )
    op.create_index('idx_escalation_state_session', 'escalation_state', ['session_id'])
    op.create_index('idx_escalation_state_manual', 'escalation_state', ['is_manual_override'])

    # 4. Escalation events table (audit log for analytics)
    op.create_table(
        'escalation_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(255), nullable=False),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('from_model', sa.String(20), nullable=True),
        sa.Column('to_model', sa.String(20), nullable=False),
        sa.Column('triggered_by_rule_id', sa.Integer(), nullable=True),
        sa.Column('triggered_by_user', sa.String(100), nullable=True),
        sa.Column('preset_id', sa.Integer(), nullable=True),
        sa.Column('preset_name', sa.String(100), nullable=True),
        sa.Column('trigger_context', postgresql.JSONB(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['triggered_by_rule_id'], ['escalation_rules.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['preset_id'], ['escalation_presets.id'], ondelete='SET NULL'),
    )
    op.create_index('idx_escalation_events_session', 'escalation_events', ['session_id'])
    op.create_index('idx_escalation_events_type', 'escalation_events', ['event_type'])
    op.create_index('idx_escalation_events_created', 'escalation_events', ['created_at'])
    op.create_index('idx_escalation_events_preset', 'escalation_events', ['preset_id'])

    # 5. Seed default presets
    op.execute("""
        INSERT INTO escalation_presets (name, description, is_active, auto_activate_conditions) VALUES
        ('Balanced', 'Default everyday use - reasonable escalation on clear signals', true, NULL),
        ('Conservative', 'Quality first - escalate early, stay high longer', false, NULL),
        ('Efficient', 'Cost/speed conscious - only escalate on clear failures', false, NULL),
        ('Demo Mode', 'Always use best models - for presentations/demos', false, NULL),
        ('Late Night', 'After 11pm - assume tired/terse user, be more forgiving', false, '{"time_range": {"start": "23:00", "end": "06:00"}}'),
        ('Guest Mode', 'For guests - more patient, assume unfamiliar phrasing', false, '{"user_mode": "guest"}')
    """)

    # 6. Seed rules for Balanced preset (id=1)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description) VALUES
        (1, 'Clarification Request', 'clarification', '{"patterns": ["could you clarify", "what do you mean", "can you specify", "i''m not sure what", "could you be more specific"]}', 'complex', 5, 100, 'LLM asked for clarification'),
        (1, 'User Correction', 'user_correction', '{"patterns": ["no,", "no ", "that''s wrong", "that''s not what", "not what I asked", "I meant", "I said"]}', 'complex', 5, 90, 'User corrected the assistant'),
        (1, 'User Frustration', 'user_frustration', '{"patterns": ["you''re confused", "that doesn''t make sense", "try again", "not helpful", "this is wrong"]}', 'super_complex', 5, 80, 'User expressed frustration'),
        (1, 'Empty Tool Results', 'empty_results', '{"check_empty": true, "check_null": true}', 'complex', 3, 70, 'Tool returned no results'),
        (1, 'Tool Failure', 'tool_failure', '{"on_error": true}', 'complex', 3, 60, 'Tool returned an error'),
        (1, 'Explicit Upgrade Request', 'explicit_request', '{"patterns": ["think harder", "be more careful", "think about it", "try a better model"]}', 'super_complex', 3, 110, 'User explicitly asked for better response')
    """)

    # 7. Seed rules for Conservative preset (id=2)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description) VALUES
        (2, 'Any Clarification', 'clarification', '{"patterns": ["could you", "what do you", "can you", "?"], "match_in_response": true}', 'complex', 8, 100, 'Any clarification question in response'),
        (2, 'Short Response', 'short_response', '{"max_length": 50}', 'complex', 5, 90, 'Response was very short'),
        (2, 'User Says No', 'user_correction', '{"patterns": ["no", "nope", "wrong", "incorrect"]}', 'super_complex', 8, 85, 'User said no or wrong'),
        (2, 'Any Frustration Signal', 'user_frustration', '{"patterns": ["confused", "doesn''t", "didn''t", "can''t", "won''t", "not working"]}', 'super_complex', 8, 80, 'Any frustration signal'),
        (2, 'Empty Results', 'empty_results', '{"check_empty": true, "check_null": true}', 'super_complex', 5, 70, 'Empty results - escalate to super_complex'),
        (2, 'Tool Failure', 'tool_failure', '{"on_error": true}', 'super_complex', 5, 60, 'Tool failure - escalate to super_complex'),
        (2, 'Repeated Query', 'repeated_query', '{"similarity_threshold": 0.8}', 'super_complex', 5, 95, 'User repeated similar query')
    """)

    # 8. Seed rules for Efficient preset (id=3)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description) VALUES
        (3, 'Strong Frustration Only', 'user_frustration', '{"patterns": ["completely wrong", "this is broken", "useless", "terrible"]}', 'complex', 3, 100, 'Only escalate on strong frustration'),
        (3, 'Explicit Request', 'explicit_request', '{"patterns": ["use a better model", "think harder", "try harder"]}', 'super_complex', 2, 110, 'User explicitly requested upgrade'),
        (3, 'Multiple Tool Failures', 'tool_failure', '{"consecutive_failures": 2}', 'complex', 2, 80, 'Only after 2 consecutive failures')
    """)

    # 9. Seed rules for Demo Mode preset (id=4)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description) VALUES
        (4, 'Always Escalate', 'always', '{"always": true}', 'super_complex', 999, 1000, 'Always use best model in demo mode')
    """)

    # 10. Seed rules for Late Night preset (id=5)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description) VALUES
        (5, 'Very Short Query', 'short_query', '{"max_words": 3}', 'complex', 5, 100, 'Short queries at night need more help'),
        (5, 'Any Clarification', 'clarification', '{"patterns": ["what", "huh", "?", "clarify"]}', 'complex', 8, 90, 'Be more helpful with clarifications'),
        (5, 'Terse Correction', 'user_correction', '{"patterns": ["no", "wrong", "nope", "not that"]}', 'super_complex', 8, 85, 'Terse corrections need best model'),
        (5, 'Night Frustration', 'user_frustration', '{"patterns": ["ugh", "come on", "seriously", "whatever"]}', 'super_complex', 10, 80, 'Tired user frustration')
    """)

    # 11. Seed rules for Guest Mode preset (id=6)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description) VALUES
        (6, 'Any Question in Response', 'clarification', '{"patterns": ["?"]}', 'complex', 5, 100, 'Any question mark in response'),
        (6, 'Polite Correction', 'user_correction', '{"patterns": ["actually", "I meant", "sorry, I wanted", "I was asking"]}', 'complex', 5, 90, 'Polite guest corrections'),
        (6, 'Any Frustration', 'user_frustration', '{"patterns": ["not working", "doesn''t understand", "wrong", "can''t"]}', 'super_complex', 5, 80, 'Guest frustration'),
        (6, 'Unrecognized Entity', 'entity_unknown', '{"check_location": true, "check_names": true}', 'complex', 3, 85, 'Failed to recognize location or name')
    """)


def downgrade() -> None:
    op.drop_table('escalation_events')
    op.drop_table('escalation_state')
    op.drop_table('escalation_rules')
    op.drop_table('escalation_presets')
