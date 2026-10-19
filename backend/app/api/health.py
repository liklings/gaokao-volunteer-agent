from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.db import check_db_connection

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    app: str
    environment: str
    database: str  # "connected" | "unreachable"
    database_error: str | None = None


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """
    健康检查接口。刻意真实执行一次数据库查询（见 db.check_db_connection），
    而不是只要进程活着就返回 ok —— 这样"服务能起来"和"服务真的能用"
    是两件分开验证的事，数据库连不上时能立刻从这个接口看出来。
    """
    settings = get_settings()
    db_ok, db_err = check_db_connection()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        app=settings.app_name,
        environment=settings.environment,
        database="connected" if db_ok else "unreachable",
        database_error=db_err,
    )
