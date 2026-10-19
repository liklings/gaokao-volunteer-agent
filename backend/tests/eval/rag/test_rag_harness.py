"""
RAG 评测框架自身的离线测试（不需要 Gemini Key、不需要数据库）。

这里测的不是"检索准不准"（那是 run_baseline.py 的事，结果是一张报告，不是 pass/fail），而是评测的
地基是否可靠——标注写错了或者 harness 有 bug，后面所有对比数字都没有意义：
1. 每条问题的证据短语确实出现在所声称的文档里（标注没写错）；
2. 每条证据落在单页之内（评测按页切 chunk，跨页的证据永远命中不了，是标注问题不是检索问题）；
3. 离线 BM25 参照检索器能跑通整条评测流水线、产出完整指标。
"""
import asyncio
import os

os.environ.setdefault("JWT_SECRET", "eval-only")

import pytest

from .corpus_utils import corpus_files, evidence_pages
from .metrics import evaluate
from .questions import QUESTIONS
from .retrievers import Bm25Retriever, build_corpus_chunks

POSITIVES = [q for q in QUESTIONS if q["tag"] != "negative"]


def test_corpus_contains_exactly_the_two_official_policy_files():
    # 刻意把"热门专业分析报告"那份非官方文件排除在语料之外（search_policy 的定位是官方政策原文）。
    assert {p.name for p in corpus_files()} == {
        "江苏省2025年普通高校艺术类专业招生办法.pdf",
        "江苏省高等职业院校考试招生制度改革实施方案.pdf",
    }


@pytest.mark.parametrize("q", POSITIVES, ids=[q["id"] for q in POSITIVES])
def test_gold_evidence_exists_in_corpus(q):
    for e in q["evidence"]:
        pages = evidence_pages(q["doc"], [e])
        assert pages, f"{q['id']} 的证据短语在 {q['doc']} 里找不到（标注写错了？）：{e!r}"


def test_question_ids_are_unique_and_tags_known():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))
    assert {q["tag"] for q in QUESTIONS} <= {"term", "numeric", "paraphrase", "negative"}
    assert all(not q["evidence"] for q in QUESTIONS if q["tag"] == "negative")


def test_bm25_reference_runs_end_to_end():
    chunks = build_corpus_chunks()
    retriever = Bm25Retriever()
    retriever.build(chunks)
    report = asyncio.run(evaluate(retriever, chunks, QUESTIONS, k=5))
    assert report["overall"]["n"] == len(POSITIVES)
    assert report["overall"]["evidence_intact"] == 1.0, "有证据被 chunk 切断了：评测集和当前切分不兼容"
    # 宽松的冒烟下限，只防"检索器/指标彻底坏了"，不是质量门槛（语料只有 ~30 个 chunk，门槛没有统计意义）
    assert report["overall"]["hit@5"] >= 0.5
    assert report["negative_separation"]["auc_top1_pos_vs_neg"] is not None
