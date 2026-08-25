"""add opportunity critic_summary

Revision ID: f67d2c164550
Revises: a943ce8ca51f
Create Date: 2026-08-25 21:21:45.575675

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f67d2c164550'
down_revision: Union[str, Sequence[str], None] = 'a943ce8ca51f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    M3.3 BUILDER persistence slice: adds Opportunity.critic_summary, a
    nullable free-text column for the (future) Opportunity Evaluation /
    Critic memo. Purely additive -- no backfill, no other column touched.
    batch_alter_table for SQLite compatibility; renders as a plain
    ADD COLUMN on Postgres.
    """
    with op.batch_alter_table('opportunities') as batch_op:
        batch_op.add_column(sa.Column('critic_summary', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops only critic_summary -- no other Opportunity column is touched.
    """
    with op.batch_alter_table('opportunities') as batch_op:
        batch_op.drop_column('critic_summary')
