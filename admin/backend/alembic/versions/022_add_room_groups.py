"""Add room groups and aliases for logical room grouping.

Revision ID: 022_add_room_groups
Revises: 021_add_default_sms_templates
Create Date: 2025-12-10

Adds room groups feature:
- room_groups: Logical groupings like "first floor", "downstairs"
- room_group_aliases: Alternative names like "1st floor", "main floor"
- room_group_members: Individual rooms belonging to each group
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '022_add_room_groups'
down_revision = '021_add_default_sms_templates'
branch_labels = None
depends_on = None


def upgrade():
    """Create room groups tables and seed default data."""

    # Create room_groups table
    op.create_table(
        'room_groups',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), unique=True, nullable=False, index=True),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), default=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_room_groups_enabled', 'room_groups', ['enabled'])

    # Create room_group_aliases table
    op.create_table(
        'room_group_aliases',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('room_group_id', sa.Integer(), sa.ForeignKey('room_groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('alias', sa.String(200), nullable=False, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint('uq_room_group_alias', 'room_group_aliases', ['alias'])

    # Create room_group_members table
    op.create_table(
        'room_group_members',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('room_group_id', sa.Integer(), sa.ForeignKey('room_groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('room_name', sa.String(100), nullable=False),
        sa.Column('display_name', sa.String(200), nullable=True),
        sa.Column('ha_entity_pattern', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint('uq_room_group_member', 'room_group_members', ['room_group_id', 'room_name'])
    op.create_index('idx_room_group_members_room_name', 'room_group_members', ['room_name'])

    # Seed default room groups for typical home layout
    # First Floor / Main Floor
    op.execute("""
        INSERT INTO room_groups (name, display_name, description, enabled)
        VALUES (
            'first_floor',
            'First Floor',
            'Main living level - typically includes living room, dining room, kitchen',
            true
        );
    """)

    # Get the ID and add aliases and members
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, '1st floor' FROM room_groups WHERE name = 'first_floor';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'main floor' FROM room_groups WHERE name = 'first_floor';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'ground floor' FROM room_groups WHERE name = 'first_floor';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'downstairs' FROM room_groups WHERE name = 'first_floor';
    """)

    # Default first floor members
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'living_room', 'Living Room' FROM room_groups WHERE name = 'first_floor';
    """)
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'dining_room', 'Dining Room' FROM room_groups WHERE name = 'first_floor';
    """)
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'kitchen', 'Kitchen' FROM room_groups WHERE name = 'first_floor';
    """)

    # Second Floor
    op.execute("""
        INSERT INTO room_groups (name, display_name, description, enabled)
        VALUES (
            'second_floor',
            'Second Floor',
            'Upper level - typically bedrooms and bathrooms',
            true
        );
    """)

    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, '2nd floor' FROM room_groups WHERE name = 'second_floor';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'upstairs' FROM room_groups WHERE name = 'second_floor';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'upper floor' FROM room_groups WHERE name = 'second_floor';
    """)

    # Default second floor members
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'master_bedroom', 'Master Bedroom' FROM room_groups WHERE name = 'second_floor';
    """)
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'guest_bedroom', 'Guest Bedroom' FROM room_groups WHERE name = 'second_floor';
    """)
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'bathroom', 'Bathroom' FROM room_groups WHERE name = 'second_floor';
    """)
    op.execute("""
        INSERT INTO room_group_members (room_group_id, room_name, display_name)
        SELECT id, 'office', 'Office' FROM room_groups WHERE name = 'second_floor';
    """)

    # Basement
    op.execute("""
        INSERT INTO room_groups (name, display_name, description, enabled)
        VALUES (
            'basement',
            'Basement',
            'Lower level - recreation, storage, utility',
            true
        );
    """)

    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'lower level' FROM room_groups WHERE name = 'basement';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'cellar' FROM room_groups WHERE name = 'basement';
    """)

    # All Lights group (whole house)
    op.execute("""
        INSERT INTO room_groups (name, display_name, description, enabled)
        VALUES (
            'whole_house',
            'Whole House',
            'All rooms in the house',
            true
        );
    """)

    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'entire house' FROM room_groups WHERE name = 'whole_house';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'all rooms' FROM room_groups WHERE name = 'whole_house';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'everywhere' FROM room_groups WHERE name = 'whole_house';
    """)
    op.execute("""
        INSERT INTO room_group_aliases (room_group_id, alias)
        SELECT id, 'the house' FROM room_groups WHERE name = 'whole_house';
    """)


def downgrade():
    """Remove room groups tables."""
    op.drop_table('room_group_members')
    op.drop_table('room_group_aliases')
    op.drop_table('room_groups')
