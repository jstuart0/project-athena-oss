"""Add long_query escalation rules to default presets.

Revision ID: 044_add_long_query_rules
Revises: 043_add_escalation_presets
Create Date: 2026-01-09
"""
from alembic import op

revision = '044_add_long_query_rules'
down_revision = '043_add_escalation_presets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add long_query rules to appropriate presets
    # Long queries (40+ words) suggest complex, multi-part questions that benefit from more capable models

    # Balanced preset (id=1) - escalate to complex for long queries
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description)
        VALUES (1, 'Long Query', 'long_query', '{"min_words": 40}', 'complex', 5, 75, 'Long detailed queries need more reasoning capacity')
    """)

    # Conservative preset (id=2) - escalate to super_complex for long queries (quality first)
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description)
        VALUES (2, 'Long Query', 'long_query', '{"min_words": 30}', 'super_complex', 8, 75, 'Long queries get best model for quality')
    """)

    # Efficient preset (id=3) - only escalate for very long queries
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description)
        VALUES (3, 'Very Long Query', 'long_query', '{"min_words": 60}', 'complex', 3, 50, 'Only escalate for very long queries')
    """)

    # Late Night preset (id=5) - lower threshold, user might be explaining more when tired
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description)
        VALUES (5, 'Long Query', 'long_query', '{"min_words": 35}', 'complex', 5, 70, 'Detailed late-night queries need help')
    """)

    # Guest Mode preset (id=6) - lower threshold, guests might explain more
    op.execute("""
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description)
        VALUES (6, 'Long Query', 'long_query', '{"min_words": 35}', 'complex', 5, 75, 'Guest providing detailed context')
    """)


def downgrade() -> None:
    # Remove long_query rules
    op.execute("DELETE FROM escalation_rules WHERE trigger_type = 'long_query'")
