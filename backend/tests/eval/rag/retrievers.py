"""
评测用的检索器。统一接口：build(chunks) 建索引，search(question, k) 返回按相关度从高到低的 Hit 列表。

- GeminiVectorRetriever：**系统真实基线**。用项目真实的 chunker + 真实的 Gemini embedding（跟
  policy_ingest/policy_search 同一个模型、同样的 RETRIEVAL_DOCUMENT/RETRIEVAL_QUERY 任务类型），
  只是相似度在内存里用 numpy 算余弦，不读写数据库——跟 pgvector 的 cosine_distance 排序等价（hnsw 是
  近似索引，这么小的库上跟精确结果没有差别），好处是评测不会污染你本地的 policy_documents 表、
  也不依赖 Postgres 在跑。向量按文本哈希缓存在 .cache/，重复跑不重复花 embedding 额度。
- Bm25Retriever：纯离线的字符二元组 BM25，**不是系统基线**，只是个无需密钥的参照物：
  ① 让评测框架在没有 API Key 的环境也能跑通；② 先看看"关键词通道"在这套问题上大概什么水平，
  给后面做混合检索时对比用。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .corpus_utils import corpus_files

CACHE_DIR = Path(__file__).parent / ".cache"


@dataclass
class CorpusChunk:
    doc: str
    chunk_index: int
    page: int | None
    text: str
    is_ocr: bool = False


@dataclass
class Hit:
    doc: str
    page: int | None
    text: str
    score: float


def build_corpus_chunks(files: list[Path] | None = None, structured: bool = True) -> list[CorpusChunk]:
    """用项目真实的 chunker 把语料切成 chunk——chunker 以后改了（标题切分、页眉清洗等），
    这里自动跟着变，评测集因为用证据短语标注，不需要改。"""
    from app.services.policy_chunker import chunk_document

    chunks: list[CorpusChunk] = []
    for path in files or corpus_files():
        for c in chunk_document(str(path), structured=structured):
            chunks.append(CorpusChunk(path.name, c.chunk_index, c.page_number, c.content, c.is_ocr))
    return chunks


class Bm25Retriever:
    """关键词通道。直接用线上同一份实现（app/services/bm25.py），评测数字就是线上关键词通道的数字。"""
    name = "bm25-bigram"

    def build(self, chunks: list[CorpusChunk]) -> None:
        from app.services.bm25 import Bm25Index

        self.chunks = chunks
        self.index = Bm25Index([c.text for c in chunks])

    def scores(self, question: str) -> list[float]:
        return self.index.scores(question)

    async def search(self, question: str, k: int) -> list[Hit]:
        sc = self.scores(question)
        order = sorted(range(len(sc)), key=lambda i: (-sc[i], i))[:k]
        return [Hit(self.chunks[i].doc, self.chunks[i].page, self.chunks[i].text, sc[i]) for i in order]


class GeminiVectorRetriever:
    name = "gemini-vector"

    def __init__(self, offline: bool = False) -> None:
        """offline=True：只读 .cache/embeddings.json，不调用 Gemini、不需要 Key；缓存里没有的文本直接报错。
        用于在没有密钥的环境（CI/沙箱）里复算已经跑过的向量基线。"""
        from app.config import get_settings

        s = get_settings()
        self.offline = offline
        if not offline and (not s.gemini_api_key or s.gemini_api_key.startswith("fake-key")):
            raise RuntimeError("需要真实的 GEMINI_API_KEY（backend/.env 里配置），当前没有或是测试占位值。")
        self._model_tag = f"{s.policy_embedding_model}|{s.policy_embedding_dimensions}"
        self._cache_path = CACHE_DIR / "embeddings.json"
        self._cache: dict[str, list[float]] = (
            json.loads(self._cache_path.read_text()) if self._cache_path.exists() else {}
        )

    def _key(self, task: str, text: str) -> str:
        return hashlib.sha1(f"{self._model_tag}|{task}|{text}".encode()).hexdigest()

    def _save_cache(self) -> None:
        CACHE_DIR.mkdir(exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache))

    async def build_async(self, chunks: list[CorpusChunk]) -> None:
        import numpy as np

        from app.services.embeddings import embed_texts

        self.chunks = chunks
        missing = [c.text for c in chunks if self._key("D", c.text) not in self._cache]
        if missing and self.offline:
            raise RuntimeError(f"离线模式：缓存里缺 {len(missing)} 个 chunk 的向量（切分变了？）——需要带 Key 重跑一次")
        if missing:
            vectors = await embed_texts(missing, task_type="RETRIEVAL_DOCUMENT")
            for text, vec in zip(missing, vectors):
                self._cache[self._key("D", text)] = vec
            self._save_cache()
        mat = np.array([self._cache[self._key("D", c.text)] for c in chunks], dtype=float)
        self.mat = mat / np.linalg.norm(mat, axis=1, keepdims=True)

    async def scores_async(self, question: str):
        import numpy as np

        from app.services.embeddings import embed_text

        key = self._key("Q", question)
        if key not in self._cache:
            if self.offline:
                raise RuntimeError(f"离线模式：缓存里没有这个问题的向量：{question!r}")
            self._cache[key] = await embed_text(question, task_type="RETRIEVAL_QUERY")
            self._save_cache()
        qv = np.array(self._cache[key], dtype=float)
        return self.mat @ (qv / np.linalg.norm(qv))

    async def search(self, question: str, k: int) -> list[Hit]:
        import numpy as np

        sims = await self.scores_async(question)
        order = np.argsort(-sims)[:k]
        return [Hit(self.chunks[i].doc, self.chunks[i].page, self.chunks[i].text, float(sims[i])) for i in order]


class HybridRetriever:
    """向量 + 关键词，RRF 融合——和 app/services/policy_search.py 里线上做法一致（候选池、k 同款）。
    分数是 RRF 融合分，不是余弦相似度，所以"负例分离度/相似度下限"那两个指标对它没有可比意义。"""

    def __init__(self, vector: GeminiVectorRetriever, keyword: Bm25Retriever, rrf_k: int = 60, pool: int = 20):
        self.vector, self.keyword, self.rrf_k, self.pool = vector, keyword, rrf_k, pool
        self.name = f"hybrid-rrf{rrf_k}"
        self.chunks = vector.chunks

    async def search(self, question: str, k: int) -> list[Hit]:
        import numpy as np

        from app.services.bm25 import rrf_fuse

        vs = await self.vector.scores_async(question)
        v_rank = [int(i) for i in np.argsort(-vs)[: max(self.pool, k * 4)]]
        ks = self.keyword.scores(question)
        k_rank = [i for i in sorted(range(len(ks)), key=lambda i: (-ks[i], i)) if ks[i] > 0][: max(self.pool, k * 4)]
        fused = rrf_fuse([v_rank, k_rank], k=self.rrf_k)[:k]
        return [Hit(self.chunks[i].doc, self.chunks[i].page, self.chunks[i].text, score) for i, score in fused]
