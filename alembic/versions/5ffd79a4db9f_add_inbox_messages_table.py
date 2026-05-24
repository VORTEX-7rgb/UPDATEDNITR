"""add_inbox_messages_table

Revision ID: 5ffd79a4db9f
Revises: 4ffd79a4db9f
Create Date: 2026-05-24 00:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5ffd79a4db9f'
down_revision: Union[str, Sequence[str], None] = '4ffd79a4db9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'inbox_messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('portal_message_id', sa.BigInteger(), nullable=False),
        sa.Column('token', sa.String(length=200), nullable=False),
        sa.Column('sender', sa.String(length=200), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=False),
        sa.Column('body', sa.String(), nullable=True),
        sa.Column('attachment_url', sa.String(length=1000), nullable=True),
        sa.Column('telegram_file_id', sa.String(length=500), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('sent_on', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_inbox_messages_is_read'), 'inbox_messages', ['is_read'], unique=False)
    op.create_index(op.f('ix_inbox_messages_user_id'), 'inbox_messages', ['user_id'], unique=False)
    op.create_index('idx_inbox_user_token', 'inbox_messages', ['user_id', 'token'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_inbox_user_token', table_name='inbox_messages')
    op.drop_index(op.f('ix_inbox_messages_user_id'), table_name='inbox_messages')
    op.drop_index(op.f('ix_inbox_messages_is_read'), table_name='inbox_messages')
    op.drop_table('inbox_messages')
