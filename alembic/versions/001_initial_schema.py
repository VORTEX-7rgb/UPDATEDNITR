"""Initial schema: users, snapshots, events

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-05-20 23:48:25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create 'users' table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('roll_number', sa.String(length=50), nullable=False),
        sa.Column('encrypted_password', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('telegram_id')
    )
    op.create_index(op.f('ix_users_roll_number'), 'users', ['roll_number'], unique=False)

    # 2. Create 'snapshots' table
    op.create_table(
        'snapshots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('module_name', sa.String(length=100), nullable=False),
        sa.Column('snapshot_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('snapshot_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_snapshots_snapshot_hash'), 'snapshots', ['snapshot_hash'], unique=False)
    op.create_index(op.f('ix_snapshots_user_id'), 'snapshots', ['user_id'], unique=False)
    op.create_index('idx_snapshots_user_module', 'snapshots', ['user_id', 'module_name'], unique=False)

    # 3. Create 'events' table
    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('sent', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_events_sent'), 'events', ['sent'], unique=False)
    op.create_index(op.f('ix_events_user_id'), 'events', ['user_id'], unique=False)
    op.create_index('idx_events_user_sent', 'events', ['user_id', 'sent'], unique=False)


def downgrade() -> None:
    # Drop indexes and tables in reverse order of creation
    op.drop_index('idx_events_user_sent', table_name='events')
    op.drop_index(op.f('ix_events_user_id'), table_name='events')
    op.drop_index(op.f('ix_events_sent'), table_name='events')
    op.drop_table('events')

    op.drop_index('idx_snapshots_user_module', table_name='snapshots')
    op.drop_index(op.f('ix_snapshots_user_id'), table_name='snapshots')
    op.drop_index(op.f('ix_snapshots_snapshot_hash'), table_name='snapshots')
    op.drop_table('snapshots')

    op.drop_index(op.f('ix_users_roll_number'), table_name='users')
    op.drop_table('users')
