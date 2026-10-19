"""add run_id to agent_runs

Revision ID: b3f21c9a7d44
Revises: 7040a8d70b92
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3f21c9a7d44'
down_revision: Union[str, Sequence[str], None] = '7040a8d70b92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 允许为空：加这个字段之前已经存在的 agent_runs 记录没有 run_id，
    # 不强行回填、也不因为这次迁移让历史数据不合法。
    op.add_column('agent_runs', sa.Column('run_id', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agent_runs', 'run_id')
