"""add admission_category to admission_groups

Revision ID: eaeee67007bb
Revises: 269cf6791df0
Create Date: 2026-09-04 06:31:27.488385

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eaeee67007bb'
down_revision: Union[str, Sequence[str], None] = '269cf6791df0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 手动删掉了 autogenerate 生成的 op.drop_index('policy_chunks_embedding_hnsw_idx', ...)——
    # SQLAlchemy 反射不了 pgvector 的 hnsw 索引类型，autogenerate 就误以为模型里没定义、
    # 想把这个真实存在、已经建好的 RAG 向量检索索引删掉。跟上一次
    # 269cf6791df0（add_home_province_only_to_profiles）撞到的是同一个已知坑。
    op.add_column('admission_groups', sa.Column('admission_category', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('admission_groups', 'admission_category')
