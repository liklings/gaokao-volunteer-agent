from datetime import datetime

from pydantic import BaseModel


class PlanConfirmRequest(BaseModel):
    profile_id: int
    group_ids: list[str]  # 用户在候选卡片里选中、要写进方案的专业组


class PlanCandidateOut(BaseModel):
    group_id: str
    university: str
    city: str
    group_name: str | None
    code: str | None
    tuition: int | None
    rank_diff: int | None
    category: str | None
    category_label: str | None
    all_hard_pass: bool  # 存进方案时依然如实记录是否通过硬性规则，不因为用户选了就默认合格

    class Config:
        from_attributes = True


class DiffEntry(BaseModel):
    kind: str  # "add" | "remove" | "change"
    group_id: str
    university: str
    detail: str


class PlanOut(BaseModel):
    id: int
    profile_id: int
    version_n: int
    created_at: datetime
    candidates: list[PlanCandidateOut]
    diff: list[DiffEntry]  # 与上一版本相比的差异；第一版为空列表

    class Config:
        from_attributes = True
