"""add overall_rank to schools

Revision ID: ffb6b65c1047
Revises: eaeee67007bb
Create Date: 2026-09-04 06:52:46.365482

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ffb6b65c1047'
down_revision: Union[str, Sequence[str], None] = 'eaeee67007bb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 手动删掉了 autogenerate 生成的 op.drop_index('policy_chunks_embedding_hnsw_idx', ...)——
    # 反射不了 pgvector 的 hnsw 索引类型这个已知坑，之前两次迁移
    # （269cf6791df0、eaeee67007bb）都撞到过，处理方式一致。
    op.add_column('schools', sa.Column('overall_rank', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('schools', 'overall_rank')
