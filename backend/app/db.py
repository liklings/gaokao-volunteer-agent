"""
SQLAlchemy 引擎与会话管理。
里程碑1只用到 `get_db` 依赖注入和一个真实的连通性检查（health check 里执行 `SELECT 1`）。
数据模型（models/）会在里程碑2里补上，再配合 alembic 生成迁移。
"""
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类，里程碑2里的 models/*.py 会继承它。"""
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个独立的数据库会话，用完自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection() -> tuple[bool, str | None]:
    """
    真实执行一次 `SELECT 1`，而不是只判断引擎对象是否创建成功——
    这样健康检查接口才能反映数据库是不是真的连得通。
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - health check 需要捕获任何连接异常并原样上报
        return False, str(exc)
