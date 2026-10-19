"""
政策检索：向量（语义）+ 关键词（BM25）两个通道，用 RRF 融合后返回最相关的几个片段，连同它们所属
文档的标题（+ 页码，如果有）一起返回——这个"来源标注"就是当初要建向量库的目的：政策解读不能只给
一句结论，要能让用户知道"这句话是从哪份文件的第几页来的"，可以自己去核对原文。

为什么要两个通道：向量检索擅长"意思一样、说法不同"，但对专有名词/数字/术语的精确匹配很弱（评测里
"省统考分哪几类""首选再选怎么选"这类问题，向量把正确段落排到第 8～12 名）；关键词检索正好相反。
两路各取前 N，按名次做 RRF 融合（见 bm25.rrf_fuse），不需要调两种分数的权重。
settings.policy_search_mode = "vector" 可一键回退到只用向量。

<=> 是 pgvector 的余弦距离算子，值越小表示越相似；这里转成 1 - distance 当"相似度分数"返回，纯粹是
为了给使用方一个更符合直觉的数字（越大越相关），不影响排序结果本身。similarity 始终是余弦相似度——
即使这个片段是被关键词通道捞上来的，也会补算它跟问题的余弦相似度，方便使用方判断"语义上到底像不像"。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PolicyChunk, PolicyDocument
from app.services.bm25 import Bm25Index, rrf_fuse
from app.services.embeddings import embed_text

DEFAULT_TOP_K = 5
CANDIDATE_POOL = 20  # 每个通道各取前多少名参与融合


@dataclass
class PolicySearchHit:
    document_title: str
    source_path: str
    page_number: int | None
    content: str
    similarity: float  # 与问题的余弦相似度（0~1，越大越相似）
    matched_by: str = "vector"  # vector | keyword | both（hybrid 模式下被哪个通道捞到）


# ---- 关键词索引缓存：按 (chunk 总数, 最大 id) 判断库是否变过 ----
# 重新导入文档是"删旧 chunk 再插新 chunk"，新 chunk 的 id 一定更大，所以这个键足以发现变化。
_index_cache: dict = {"key": None, "ids": [], "index": None}


def invalidate_keyword_index() -> None:
    _index_cache["key"] = None


def _keyword_index(db: Session) -> tuple[list[int], Bm25Index | None]:
    count, max_id = db.execute(select(func.count(PolicyChunk.id), func.max(PolicyChunk.id))).one()
    key = (count, max_id)
    if _index_cache["key"] != key:
        rows = db.execute(select(PolicyChunk.id, PolicyChunk.content).order_by(PolicyChunk.id)).all()
        _index_cache["ids"] = [r[0] for r in rows]
        _index_cache["index"] = Bm25Index([r[1] for r in rows]) if rows else None
        _index_cache["key"] = key
    return _index_cache["ids"], _index_cache["index"]


def _keyword_ranking(db: Session, query: str, pool: int) -> list[int]:
    """关键词通道：返回 chunk id，按 BM25 分数从高到低，分数为 0（没有任何二元组命中）的不参与。"""
    ids, index = _keyword_index(db)
    if index is None:
        return []
    scored = [(s, cid) for s, cid in zip(index.scores(query), ids) if s > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [cid for _, cid in scored[:pool]]


async def search_policy(db: Session, query: str, top_k: int = DEFAULT_TOP_K) -> list[PolicySearchHit]:
    settings = get_settings()
    query_vector = await embed_text(query, task_type="RETRIEVAL_QUERY")
    distance = PolicyChunk.embedding.cosine_distance(query_vector)

    if settings.policy_search_mode != "hybrid":
        return _to_hits(db, _vector_rows(db, distance, top_k), {})

    pool = max(CANDIDATE_POOL, top_k * 4)
    vector_rows = _vector_rows(db, distance, pool)
    vector_ids = [chunk.id for chunk, _, _ in vector_rows]
    keyword_ids = _keyword_ranking(db, query, pool)

    fused = rrf_fuse([vector_ids, keyword_ids], k=settings.policy_rrf_k)
    top_ids = [cid for cid, _ in fused[:top_k]]
    by_vec, by_kw = set(vector_ids), set(keyword_ids)
    matched = {cid: "both" if cid in by_vec and cid in by_kw else ("keyword" if cid in by_kw else "vector") for cid in top_ids}

    rows_by_id = {chunk.id: (chunk, doc, dist) for chunk, doc, dist in vector_rows}
    missing = [cid for cid in top_ids if cid not in rows_by_id]
    if missing:  # 只被关键词通道捞到的片段：补算余弦距离和所属文档
        extra = db.execute(
            select(PolicyChunk, PolicyDocument, distance.label("distance"))
            .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
            .where(PolicyChunk.id.in_(missing))
        ).all()
        rows_by_id.update({chunk.id: (chunk, doc, dist) for chunk, doc, dist in extra})
    ordered = [rows_by_id[cid] for cid in top_ids if cid in rows_by_id]
    return _to_hits(db, ordered, matched)


def _vector_rows(db: Session, distance, limit: int):
    stmt = (
        select(PolicyChunk, PolicyDocument, distance.label("distance"))
        .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
        .order_by(distance)
        .limit(limit)
    )
    return db.execute(stmt).all()


def _to_hits(db: Session, rows, matched: dict[int, str]) -> list[PolicySearchHit]:
    return [
        PolicySearchHit(
            document_title=document.title,
            source_path=document.source_path,
            page_number=chunk.page_number,
            content=chunk.content,
            similarity=round(1 - dist, 4),
            matched_by=matched.get(chunk.id, "vector"),
        )
        for chunk, document, dist in rows
    ]
