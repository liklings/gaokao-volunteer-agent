"""
把所有 ORM 模型集中在这里 import 一次——
Base.metadata 需要"看到"每个模型类才能生成正确的迁移，
在别处只 `from app.db import Base` 而不 import 具体模型，
metadata 里就会漏掉表。alembic/env.py 也依赖这个文件把模型注册齐。
"""
from app.models.school import School
from app.models.admission_group import AdmissionGroup
from app.models.admission_record import AdmissionRecord
from app.models.score_rank_table import ScoreRankRow
from app.models.user import User
from app.models.profile import Profile
from app.models.plan import Plan
from app.models.plan_candidate import PlanCandidate
from app.models.chat_message import ChatMessage
from app.models.agent_run import AgentRun
from app.models.agent_step import AgentStep
from app.models.policy_document import PolicyDocument, PolicyChunk

__all__ = [
    "School",
    "AdmissionGroup",
    "AdmissionRecord",
    "ScoreRankRow",
    "User",
    "Profile",
    "Plan",
    "PlanCandidate",
    "ChatMessage",
    "AgentRun",
    "AgentStep",
    "PolicyDocument",
    "PolicyChunk",
]
