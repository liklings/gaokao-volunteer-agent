"""add batch to score_rank_rows

Revision ID: a7c1d2e3f4b5
Revises: cdcc85b08ed9
Create Date: 2026-10-06 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c1d2e3f4b5'
down_revision: Union[str, Sequence[str], None] = 'cdcc85b08ed9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 手写迁移，只加这一列（没有用 autogenerate，避免 pgvector hnsw 索引的已知假阳性）。
    op.add_column('score_rank_rows', sa.Column('batch', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('score_rank_rows', 'batch')
