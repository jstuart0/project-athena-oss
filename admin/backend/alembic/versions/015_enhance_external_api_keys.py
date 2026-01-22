"""enhance external api keys for oauth and multiple keys

Revision ID: 015
Revises: 014
Create Date: 2025-11-25

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = '015'
down_revision = '014'
branch_labels = None
depends_on = None


def upgrade():
    """Add OAuth and multiple keys support to external_api_keys."""

    # Add new columns for OAuth support
    op.add_column('external_api_keys', sa.Column('client_id_encrypted', sa.Text(), nullable=True))
    op.add_column('external_api_keys', sa.Column('client_secret_encrypted', sa.Text(), nullable=True))
    op.add_column('external_api_keys', sa.Column('oauth_token_url', sa.Text(), nullable=True))
    op.add_column('external_api_keys', sa.Column('oauth_scopes', sa.Text(), nullable=True))

    # Add support for multiple keys
    op.add_column('external_api_keys', sa.Column('key_type', sa.String(50), nullable=True))
    op.add_column('external_api_keys', sa.Column('key_purpose', sa.Text(), nullable=True))

    # Add additional API keys (key2, key3) for services that need multiple
    op.add_column('external_api_keys', sa.Column('api_key2_encrypted', sa.Text(), nullable=True))
    op.add_column('external_api_keys', sa.Column('api_key2_label', sa.String(100), nullable=True))
    op.add_column('external_api_keys', sa.Column('api_key3_encrypted', sa.Text(), nullable=True))
    op.add_column('external_api_keys', sa.Column('api_key3_label', sa.String(100), nullable=True))

    # Add flexible metadata for additional configuration
    op.add_column('external_api_keys', sa.Column('extra_config', JSONB, nullable=True))

    # Drop unique constraint on service_name if it exists (it may not exist in all deployments)
    # Use try/except or check constraint existence
    from sqlalchemy import inspect
    from sqlalchemy.engine import reflection

    bind = op.get_bind()
    inspector = inspect(bind)
    constraints = [c['name'] for c in inspector.get_unique_constraints('external_api_keys')]

    if 'external_api_keys_service_name_key' in constraints:
        op.drop_constraint('external_api_keys_service_name_key', 'external_api_keys', type_='unique')

    # Create composite unique constraint on service_name + key_type
    if 'uq_external_api_keys_service_key_type' not in constraints:
        op.create_unique_constraint(
            'uq_external_api_keys_service_key_type',
            'external_api_keys',
            ['service_name', 'key_type']
        )

    # Create index on key_type for faster queries
    op.create_index('idx_external_api_keys_key_type', 'external_api_keys', ['key_type'])


def downgrade():
    """Remove OAuth and multiple keys support from external_api_keys."""

    # Drop new indexes and constraints
    op.drop_index('idx_external_api_keys_key_type', 'external_api_keys')
    op.drop_constraint('uq_external_api_keys_service_key_type', 'external_api_keys', type_='unique')

    # Restore original unique constraint
    op.create_unique_constraint('external_api_keys_service_name_key', 'external_api_keys', ['service_name'])

    # Drop new columns
    op.drop_column('external_api_keys', 'extra_config')
    op.drop_column('external_api_keys', 'api_key3_label')
    op.drop_column('external_api_keys', 'api_key3_encrypted')
    op.drop_column('external_api_keys', 'api_key2_label')
    op.drop_column('external_api_keys', 'api_key2_encrypted')
    op.drop_column('external_api_keys', 'key_purpose')
    op.drop_column('external_api_keys', 'key_type')
    op.drop_column('external_api_keys', 'oauth_scopes')
    op.drop_column('external_api_keys', 'oauth_token_url')
    op.drop_column('external_api_keys', 'client_secret_encrypted')
    op.drop_column('external_api_keys', 'client_id_encrypted')
