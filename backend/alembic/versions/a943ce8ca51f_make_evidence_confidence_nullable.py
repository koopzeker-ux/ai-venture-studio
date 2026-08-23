"""make evidence confidence nullable

Revision ID: a943ce8ca51f
Revises: 9147d90b16c5
Create Date: 2026-08-23 12:57:12.558242

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a943ce8ca51f'
down_revision: Union[str, Sequence[str], None] = '9147d90b16c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    LEAD decision, M3.2 REVIEWER finding 1 (MEDIUM): evidence.confidence
    was non-nullable with a Python-level default of 0.5, which made a
    genuinely-estimated 0.5 indistinguishable from "no responsible
    confidence could be assigned" -- the only trace of that distinction
    lived in AgentRun.output_summary free text, keyed by JSON-array
    position, not the Evidence row itself. This is a pure DROP NOT NULL:
    additive, no backfill, no data loss -- every existing row already has
    a real numeric value, and no code path in this repo relied on the
    implicit ORM-level default (every current writer sets confidence
    explicitly). batch_alter_table for SQLite compatibility (ALTER COLUMN
    isn't supported directly); renders as a plain ALTER COLUMN on
    Postgres.
    """
    with op.batch_alter_table('evidence') as batch_op:
        batch_op.alter_column('confidence', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Backfill any NULLs to the old 0.5 default before re-adding NOT NULL,
    so the downgrade never fails on rows written after the upgrade.
    """
    op.execute("UPDATE evidence SET confidence = 0.5 WHERE confidence IS NULL")
    with op.batch_alter_table('evidence') as batch_op:
        batch_op.alter_column('confidence', existing_type=sa.Float(), nullable=False)
