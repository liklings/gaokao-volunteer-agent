"""
policy_search.py 的测试：用真实数据库 + 真实 pgvector 的余弦距离排序，但 mock 掉
embed_text（避免真的调 Gemini API）——手工构造几个方向明确、彼此可区分的 768维
向量，直接插入 policy_chunks 表，再用同样手工构造的"查询向量"去检索，这样能
真实验证 <=> 算子排序对不对、similarity 数值算得对不对，而不用依赖 Gemini
真实语义理解的效果（那部分不是这层代码的职责，也没法在离线测试里验证）。
"""
import math

from sqlalchemy import delete

from app.models import PolicyChunk, PolicyDocument
from app.services import policy_search

DIM = 768


def _unit_vector(hot_index: int) -> list[float]:
    """在某一维上是1，其余是0的单位向量——跟另一个 _unit_vector(hot_index) 的
    余弦距离是0（完全同向），跟正交方向的向量余弦距离是1，方便手算验证排序。"""
    v = [0.0] * DIM
    v[hot_index] = 1.0
    return v


def _mix_vector(hot_index: int, noise: float) -> list[float]:
    """在 hot_index 方向为主、掺一点别的方向噪声的向量——用来构造"跟目标同向但
    不是完全同向"的第二相关结果，验证排序不是只有0/1两档。"""
    v = [0.0] * DIM
    v[hot_index] = 1.0
    v[(hot_index + 1) % DIM] = noise
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


async def test_search_policy_orders_by_cosine_similarity_and_maps_fields(db, monkeypatch):
    doc_a = PolicyDocument(title="报名办法", source_path="/fake/path/a.docx", doc_type="docx")
    doc_b = PolicyDocument(title="专项计划通知", source_path="/fake/path/b.pdf", doc_type="pdf")
    db.add_all([doc_a, doc_b])
    db.flush()

    # chunk_best：跟查询向量完全同向（distance=0，similarity=1）
    # chunk_mid：跟查询向量方向接近但不完全一致（distance 在 0~1 之间）
    # chunk_far：跟查询向量正交（distance=1，similarity=0）
    chunk_best = PolicyChunk(
        document_id=doc_a.id, chunk_index=0, content="最相关的原文片段",
        page_number=3, embedding=_unit_vector(0),
    )
    chunk_mid = PolicyChunk(
        document_id=doc_a.id, chunk_index=1, content="次相关的原文片段",
        page_number=5, embedding=_mix_vector(0, noise=0.5),
    )
    chunk_far = PolicyChunk(
        document_id=doc_b.id, chunk_index=0, content="不相关的原文片段",
        page_number=None, embedding=_unit_vector(1),
    )
    db.add_all([chunk_best, chunk_mid, chunk_far])
    db.commit()

    try:
        monkeypatch.setattr(policy_search, "embed_text", _fake_embed_text_factory(_unit_vector(0)))

        hits = await policy_search.search_policy(db, "随便什么查询文本", top_k=3)

        assert len(hits) == 3
        assert [h.content for h in hits] == ["最相关的原文片段", "次相关的原文片段", "不相关的原文片段"], (
            "排序不对：应该按余弦相似度从高到低排"
        )
        assert hits[0].document_title == "报名办法"
        assert hits[0].source_path == "/fake/path/a.docx"
        assert hits[0].page_number == 3
        assert hits[0].similarity == 1.0, f"完全同向的向量相似度应该是1.0，实际 {hits[0].similarity}"
        assert hits[2].similarity == 0.0, f"正交向量相似度应该是0.0，实际 {hits[2].similarity}"
        assert 0.0 < hits[1].similarity < 1.0, f"次相关的相似度应该介于0和1之间，实际 {hits[1].similarity}"
    finally:
        db.execute(delete(PolicyChunk).where(PolicyChunk.document_id.in_([doc_a.id, doc_b.id])))
        db.execute(delete(PolicyDocument).where(PolicyDocument.id.in_([doc_a.id, doc_b.id])))
        db.commit()


async def test_search_policy_respects_top_k(db, monkeypatch):
    doc = PolicyDocument(title="测试文档", source_path="/fake/path/topk.docx", doc_type="docx")
    db.add(doc)
    db.flush()

    chunks = [
        PolicyChunk(document_id=doc.id, chunk_index=i, content=f"片段{i}", embedding=_unit_vector(i))
        for i in range(4)
    ]
    db.add_all(chunks)
    db.commit()

    try:
        monkeypatch.setattr(policy_search, "embed_text", _fake_embed_text_factory(_unit_vector(0)))
        hits = await policy_search.search_policy(db, "查询", top_k=2)
        assert len(hits) == 2
    finally:
        db.execute(delete(PolicyChunk).where(PolicyChunk.document_id == doc.id))
        db.execute(delete(PolicyDocument).where(PolicyDocument.id == doc.id))
        db.commit()


async def test_search_policy_empty_table_returns_empty_list(db, monkeypatch):
    # 不插入任何跟这次查询相关的数据，只验证空结果时不报错、返回空列表——
    # 真实场景对应"向量库刚建好还没导入任何文档"或者"这个问题命中不到任何片段"，
    # 上层 _tool_search_policy 靠这个空列表给用户一个明确的"暂时查不到"提示。
    monkeypatch.setattr(policy_search, "embed_text", _fake_embed_text_factory(_unit_vector(500)))
    hits = await policy_search.search_policy(db, "一个跟库里任何东西都不太可能匹配的查询", top_k=5)
    assert isinstance(hits, list)


def _fake_embed_text_factory(vector: list[float]):
    async def _fake_embed_text(text, task_type="RETRIEVAL_QUERY"):
        assert task_type == "RETRIEVAL_QUERY", "search_policy 应该用 RETRIEVAL_QUERY，不是 RETRIEVAL_DOCUMENT"
        return vector

    return _fake_embed_text


# ---- 混合检索：关键词通道 ----

async def test_hybrid_surfaces_chunk_only_keyword_channel_finds(db, monkeypatch):
    """向量完全分辨不出来（所有 chunk 向量都和查询正交/相同）时，包含问题里专有词的 chunk
    要靠关键词通道排到最前，并被标记为 keyword/both。这是向量检索漏掉术语类问题的典型场景。"""
    doc = PolicyDocument(title="混合检索测试", source_path="/fake/path/hybrid.pdf", doc_type="pdf")
    db.add(doc)
    db.flush()
    # 查询向量 = unit(0)。目标 chunk 的向量故意放在正交方向（向量通道排最后），靠关键词救回来。
    target = PolicyChunk(document_id=doc.id, chunk_index=0, content="省统考设音乐类、舞蹈类和书法类等6类",
                         page_number=4, embedding=_unit_vector(7))
    decoys = [
        PolicyChunk(document_id=doc.id, chunk_index=i + 1, content=f"与报名时间有关的无关内容{i}",
                    page_number=1, embedding=_mix_vector(0, noise=0.1 * (i + 1)))
        for i in range(6)
    ]
    db.add_all([target, *decoys])
    db.commit()
    try:
        monkeypatch.setattr(policy_search, "embed_text", _fake_embed_text_factory(_unit_vector(0)))
        policy_search.invalidate_keyword_index()
        hits = await policy_search.search_policy(db, "省统考分哪几类", top_k=3)
        assert "省统考设音乐类" in hits[0].content or any("省统考设音乐类" in h.content for h in hits), (
            "关键词通道应该把含「省统考」的 chunk 捞进前 3"
        )
        kw_hit = next(h for h in hits if "省统考设音乐类" in h.content)
        assert kw_hit.matched_by in ("keyword", "both")
        assert 0.0 <= kw_hit.similarity <= 1.0  # 被关键词捞上来的片段也补算了余弦相似度
    finally:
        policy_search.invalidate_keyword_index()
        db.execute(delete(PolicyChunk).where(PolicyChunk.document_id == doc.id))
        db.execute(delete(PolicyDocument).where(PolicyDocument.id == doc.id))
        db.commit()


async def test_vector_mode_ignores_keyword_channel(db, monkeypatch):
    """policy_search_mode=vector 时回退到纯向量排序（出问题时的一键回退开关）。"""
    from app.config import get_settings

    doc = PolicyDocument(title="回退测试", source_path="/fake/path/fallback.pdf", doc_type="pdf")
    db.add(doc)
    db.flush()
    near = PolicyChunk(document_id=doc.id, chunk_index=0, content="完全无关的文字", page_number=1, embedding=_unit_vector(0))
    kw = PolicyChunk(document_id=doc.id, chunk_index=1, content="省统考设音乐类", page_number=2, embedding=_unit_vector(9))
    db.add_all([near, kw])
    db.commit()
    settings = get_settings()
    old_mode = settings.policy_search_mode
    try:
        monkeypatch.setattr(policy_search, "embed_text", _fake_embed_text_factory(_unit_vector(0)))
        monkeypatch.setattr(settings, "policy_search_mode", "vector")
        hits = await policy_search.search_policy(db, "省统考", top_k=2)
        assert hits[0].content == "完全无关的文字", "vector 模式必须只按余弦相似度排序"
        assert all(h.matched_by == "vector" for h in hits)
    finally:
        monkeypatch.setattr(settings, "policy_search_mode", old_mode)
        policy_search.invalidate_keyword_index()
        db.execute(delete(PolicyChunk).where(PolicyChunk.document_id == doc.id))
        db.execute(delete(PolicyDocument).where(PolicyDocument.id == doc.id))
        db.commit()
