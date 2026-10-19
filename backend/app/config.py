"""
应用配置。所有环境相关的值都从环境变量读取，不在代码里硬编码。
本地开发时从 backend/.env 加载（见 .env.example）；
docker-compose 场景下由 compose 文件注入环境变量。
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 数据库 ---
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/gaokao_agent"

    # --- Gemini（预留，里程碑1暂不接入对话接口，这里先占位方便后面直接用） ---
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash-lite"

    # --- 政策向量检索（policy_search）---
    # embedding 模型和对话模型（gemini_model）故意分开配置，即使以后对话模型换掉
    # （比如换成 Qwen），也不影响这里——只要 policy_embedding_model 不变，已经
    # 入库的向量就还能用。反过来，如果哪天要换 embedding 模型本身，policy_embedding_dimensions
    # 也要跟着改成新模型的输出维度，且必须把 policy_chunks 表清空重新导入一遍——
    # 不同模型算出来的向量不在同一个语义空间里，没法混用或直接比较相似度。
    policy_embedding_model: str = "gemini-embedding-001"
    policy_embedding_dimensions: int = 768
    # 检索模式：hybrid = 向量 + 关键词(BM25) 用 RRF 融合；vector = 只用向量（原行为，出问题时可一键回退）
    policy_search_mode: str = "hybrid"
    policy_rrf_k: int = 60

    # --- OCR（扫描版 PDF 兜底，见 app/services/ocr.py）---
    # PDF 某一页用 pypdf 提取出的文字少于 ocr_min_page_chars 个字符时，认为这页是扫描图片，
    # 渲染成图片交给 OCR 引擎识别。ocr_engine: paddleocr | rapidocr | auto。
    ocr_engine: str = "paddleocr"
    ocr_min_page_chars: int = 30
    ocr_render_dpi: int = 200

    # --- 认证（方向⑤：账号体系 + 多用户隔离）---
    # jwt_secret 没有默认值——生产环境必须在 .env / 部署环境变量里显式配置一个
    # 随机字符串，不能让所有部署共用同一个硬编码密钥（那样等于没加密，任何人
    # 拿到源码就能伪造任意用户的登录态）。本地没配的话 Settings() 初始化时
    # 直接报错，这个报错比"线上 token 被伪造"划算得多。
    # 生成一个：python -c "import secrets; print(secrets.token_urlsafe(32))"
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7天，SPA场景不用做得太短，配合以后可能加的刷新流程再收紧

    # --- 应用元信息 ---
    app_name: str = "志愿领航 Agent Backend"
    environment: str = "local"  # local | docker | production
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080", "http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
