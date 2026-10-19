from pydantic import BaseModel


class ProfileOut(BaseModel):
    id: int
    province: str | None
    year: int | None
    track: str | None
    subjects: list[str] | None
    score: int | None
    rank: int | None
    major_keyword: str | None
    city_pref: str | None
    budget_max: int | None
    home_province_only: bool

    class Config:
        from_attributes = True


class ChatRequest(BaseModel):
    profile_id: int | None = None  # 第一次对话不传，服务器会创建一个新 Profile 并返回其 id
    message: str


class ChatResponse(BaseModel):
    profile_id: int
    reply: str
    profile: ProfileOut
    missing: list[str]
    conflicts: list[str]
    ready: bool  # missing 为空即 ready=True，服务器计算，不采信模型自报
