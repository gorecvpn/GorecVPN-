"""raffle enhancement: ticket source + reminder dedupe

Revision ID: 0128
Revises: 0127
Create Date: 2026-09-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0128'
down_revision: Union[str, None] = '0127'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'raffle_tickets',
        sa.Column('source', sa.String(length=32), nullable=False, server_default='purchase'),
    )
    op.add_column('raffle_tickets', sa.Column('source_ref', sa.String(length=128), nullable=True))
    op.create_index('ix_raffle_tickets_campaign_source', 'raffle_tickets', ['campaign_id', 'source'])

    op.create_table(
        'raffle_reminder_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'campaign_id', sa.Integer(), sa.ForeignKey('raffle_campaigns.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reminder_type', sa.String(length=32), nullable=False, server_default='ends_24h'),
        sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.UniqueConstraint('campaign_id', 'user_id', 'reminder_type', name='uq_raffle_reminder_campaign_user_type'),
    )
    op.create_index('ix_raffle_reminder_logs_campaign', 'raffle_reminder_logs', ['campaign_id'])


def downgrade() -> None:
    op.drop_index('ix_raffle_reminder_logs_campaign', table_name='raffle_reminder_logs')
    op.drop_table('raffle_reminder_logs')
    op.drop_index('ix_raffle_tickets_campaign_source', table_name='raffle_tickets')
    op.drop_column('raffle_tickets', 'source_ref')
    op.drop_column('raffle_tickets', 'source')
