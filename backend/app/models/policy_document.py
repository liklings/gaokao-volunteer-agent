"""
政策向量检索用的两张表：PolicyDocument（一份政策文档，比如"江苏省2026年普通高校
招生考试报名办法"）+ PolicyChunk（这份文档切出来的一个个文本片段，每个片段配一个
向量）。拆成两张表而不是一张大表，是因为"文档"和"可检索片段"的生命周期和粒度不
一样：重新导入同一份文档时，要能先把它名下的旧片段整批删掉再重新切、重新生成向量
（比如换了 embedding 模型，或者文档改了措辞），不希望这个操作牵连到其他文档。

向量列用 pgvector 的 Vector 类型，维度写死成 settings.policy_embedding_dimensions
（当前 768）——这不是随便选的数字，必须跟 app/services/embeddings.py 里实际调用的
embedding 模型输出维度完全一致，换 embedding 模型时这两处要一起改，而且旧数据全部
作废（不同模型的向量不在同一个语义空间里，没法混着比相似度，这条在实现前已经跟
用户确认过）。
"""
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db import Base

_EMBEDDING_DIM = get_settings().policy_embedding_dimensions


class PolicyDocument(Base):
    """
    一份政策原文的元信息。source_path 记录原始文件在导入时的路径/文件名——纯粹是
    给人看的溯源信息（"这条政策来自哪份文件"），不是给检索用的字段。
    """
    __tablename__ = "policy_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # source_path 加唯一约束是刻意的：同一份文件重新导入时，靠这个字段找到旧文档、
    # 删掉旧 chunk 重新切一遍，而不是每次都新建一份重复的文档记录。
    source_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)  # pdf / docx
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["PolicyChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="PolicyChunk.chunk_index"
    )


class PolicyChunk(Base):
    """
    一份文档切出来的一个文本片段 + 它的向量。page_number 是可选的（docx 没有天然的
    页码概念，pdf 按页提取时会填上），有值的话检索命中之后能精确告诉用户"第几页"，
    没有的话至少还有文档标题可以溯源。
    """
    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("policy_documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 这个片段在文档里的顺序，0起始
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)

    document: Mapped["PolicyDocument"] = relationship(back_populates="chunks")
