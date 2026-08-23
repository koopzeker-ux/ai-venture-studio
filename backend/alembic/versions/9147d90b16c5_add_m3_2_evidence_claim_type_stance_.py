"""add M3.2 evidence claim_type stance found_at source_reliability duplicate_of; opportunity research_summary

Revision ID: 9147d90b16c5
Revises: 6b1c524e1012
Create Date: 2026-08-23 11:44:27.484082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9147d90b16c5'
down_revision: Union[str, Sequence[str], None] = '6b1c524e1012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch_alter_table: SQLite can't ALTER a constraint onto an existing
    # table (only CREATE TABLE ... FOREIGN KEY at creation time), so adding
    # duplicate_of_evidence_id's self-FK needs Alembic's copy-and-move batch
    # mode here -- same reasoning as the approvals/cost_events task_id FK in
    # the M4.1 migration. On Postgres this renders as a plain ALTER TABLE
    # ADD COLUMN / ADD CONSTRAINT.
    with op.batch_alter_table('evidence') as batch_op:
        batch_op.add_column(sa.Column('claim_type', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('stance', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('found_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('source_reliability', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('duplicate_of_evidence_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            op.f('ix_evidence_duplicate_of_evidence_id'), ['duplicate_of_evidence_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_evidence_duplicate_of_evidence_id_evidence',
            'evidence',
            ['duplicate_of_evidence_id'],
            ['id'],
            ondelete='SET NULL',
        )
    op.add_column('opportunities', sa.Column('research_summary', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('opportunities', 'research_summary')
    with op.batch_alter_table('evidence') as batch_op:
        batch_op.drop_constraint('fk_evidence_duplicate_of_evidence_id_evidence', type_='foreignkey')
        batch_op.drop_index(op.f('ix_evidence_duplicate_of_evidence_id'))
        batch_op.drop_column('duplicate_of_evidence_id')
        batch_op.drop_column('source_reliability')
        batch_op.drop_column('found_at')
        batch_op.drop_column('stance')
        batch_op.drop_column('claim_type')
