"""
纯 Python 的字符二元组 BM25——政策检索里的"关键词通道"。

为什么是二元组而不是分词：中文分词要引入 jieba 之类的依赖（且词典对"院校专业组""省统考""平行志愿"这类
招生术语未必切得对），而字符二元组不需要任何词典，对专有名词/数字/术语的精确匹配一样有效——正是
向量检索最容易漏掉的那类问题（"3+1+2""省统考""20 个院校专业组"）。
为什么不放进数据库（tsvector/pg_trgm）：政策库只有几十到几百个 chunk，内存里算一遍是毫秒级，不需要
新的迁移和扩展；评测里用的就是这份代码，线上线下是同一个算法，数字可以直接对照。
库规模涨到几万 chunk 以后再考虑换成数据库侧的全文检索。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

_PAGE_MARKER = re.compile(r"[—─\-－]\s*\d{1,3}\s*[—─\-－]")
_NON_CONTENT = re.compile(r"[^\w]", re.UNICODE)


def normalize_for_search(text: str) -> str:
    """NFKC（全角半角统一）→ 去页码标记 → 去全部空白和标点。PDF 文字层常在汉字和数字之间夹空格
    （"外语 3门"），不先抹平会导致同一个词在文档和问题里切出不同的二元组。"""
    t = unicodedata.normalize("NFKC", text)
    t = _PAGE_MARKER.sub("", t)
    return _NON_CONTENT.sub("", t)


def char_bigrams(text: str) -> list[str]:
    t = normalize_for_search(text)
    return [t[i:i + 2] for i in range(len(t) - 1)] or list(t)


class Bm25Index:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(char_bigrams(d)) for d in documents]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg_len = sum(self.lens) / max(len(self.lens), 1)
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(documents)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query: str) -> list[float]:
        """每篇文档对 query 的 BM25 分数（与构造时的文档顺序一致）；没有任何二元组命中则为 0。"""
        q_tokens = set(char_bigrams(query))
        out: list[float] = []
        for tf, length in zip(self.tfs, self.lens):
            s = 0.0
            for t in q_tokens:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg_len))
            out.append(s)
        return out


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：每个通道只提供"排名"（不看原始分数），score = Σ 1/(k+rank)。
    向量余弦分和 BM25 分量纲完全不同，没法直接加权；RRF 只用名次，不需要调权重/归一化。
    rankings：每个通道一个"文档下标，按相关度从高到低"的列表。返回 [(下标, 融合分)]，按融合分降序。"""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, 1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
