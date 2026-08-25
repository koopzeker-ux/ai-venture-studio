"""make experiment budget_eur nullable

Revision ID: 9b9043140432
Revises: f67d2c164550
Create Date: 2026-08-25 22:09:54.650487

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b9043140432'
down_revision: Union[str, Sequence[str], None] = 'f67d2c164550'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    LEAD decision, M3.3 pre-review: experiments.budget_eur was non-nullable
    with a Python-level default of 0.0, which the Critic module could not
    use honestly -- an unestimated budget would persist as 0.0, reading as
    "free to run" rather than "not estimated" (UNKNOWN != 0). Objectively
    safe additive change: this table has zero writers/readers anywhere in
    the app before this slice and zero rows in the live database, so
    nothing relies on the old default. Pure DROP NOT NULL, no backfill.
    batch_alter_table for SQLite compatibility; renders as a plain ALTER
    COLUMN on Postgres.
    """
    with op.batch_alter_table('experiments') as batch_op:
        batch_op.alter_column('budget_eur', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Backfill any NULLs to the old 0.0 default before re-adding NOT NULL, so
    the downgrade never fails on rows written after the upgrade.
    """
    op.execute("UPDATE experiments SET budget_eur = 0.0 WHERE budget_eur IS NULL")
    with op.batch_alter_table('experiments') as batch_op:
        batch_op.alter_column('budget_eur', existing_type=sa.Float(), nullable=False)
