"""
RAG 基线评测入口。在 backend/ 目录、激活 venv 之后运行：

    python -m tests.eval.rag.run_baseline --retriever vector   # 系统真实基线（需要 backend/.env 里的 GEMINI_API_KEY）
    python -m tests.eval.rag.run_baseline --retriever bm25     # 离线参照物，不需要任何密钥
    python -m tests.eval.rag.run_baseline --retriever hybrid   # 向量 + 关键词(RRF)，需要 Key（或 --offline-cache）
    python -m tests.eval.rag.run_baseline --retriever all

    --chunking legacy|structured   旧切法（逐页按段落累积）/ 新切法（标题感知 + 面包屑），默认 structured
    --offline-cache                向量只读 .cache，不调 Gemini（缓存里没有的文本会报错）

结果打印成表，同时写入 tests/eval/rag/reports/baseline_<retriever>.json（建议把 vector 的那份提交进
仓库，作为以后做混合检索/OCR 之后对比用的基线）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "eval-only")  # Settings 校验需要；评测不涉及认证

from .metrics import evaluate  # noqa: E402
from .questions import QUESTIONS  # noqa: E402
from .retrievers import Bm25Retriever, GeminiVectorRetriever, HybridRetriever, build_corpus_chunks  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"


def print_report(rep: dict) -> None:
    k = rep["k"]
    print(f"\n=== {rep['retriever']}  （{rep['n_chunks']} 个 chunk，hit@k 的 k={k}）===")
    header = f"{'类型':<11}{'n':>3}  {'hit@1':>6} {'hit@3':>6} {'hit@'+str(k):>6} {'MRR':>6} {'文档对':>6} {'证据完整':>8}"
    print(header)
    for name, s in [("ALL", rep["overall"]), *rep["by_tag"].items()]:
        print(f"{name:<11}{s['n']:>3}  {s['hit@1']:>6} {s['hit@3']:>6} {s[f'hit@{k}']:>6} {s['mrr']:>6} {s['doc_top1']:>6} {s['evidence_intact']:>8}")
    if rep["retriever"].startswith("hybrid"):
        misses = [r for r in rep["rows"] if r["tag"] != "negative" and not r["rank"]]
        print(f"\n（融合分是 RRF 名次分，不是相似度，负例分离度/相似度下限对它没有意义，不输出）")
        print(f"前{k}名没命中的问题（{len(misses)} 条）：" + (", ".join(r["id"] for r in misses) or "无"))
        return
    sep = rep["negative_separation"]
    print(f"\n无答案问题的区分度：AUC={sep['auc_top1_pos_vs_neg']}  正例top1均分={sep['mean_top1_pos']}  负例top1均分={sep['mean_top1_neg']}")
    print(f"若把相似度下限设为负例最高分({sep['max_negative_top1']})，仍能通过的有答案问题占比：{sep['answerable_passing_if_threshold_is_max_negative']}")
    misses = [r for r in rep["rows"] if r["tag"] != "negative" and not r["rank"]]
    print(f"\n前{k}名没命中的问题（{len(misses)} 条）：" + (", ".join(f"{r['id']}{'(证据被切断)' if not r['evidence_intact'] else ''}" for r in misses) or "无"))


async def run(which: str, k: int, chunking: str = "structured", offline: bool = False, rrf_k: int = 60) -> None:
    chunks = build_corpus_chunks(structured=(chunking == "structured"))
    REPORT_DIR.mkdir(exist_ok=True)
    retrievers = []
    bm = None
    if which in ("bm25", "hybrid", "all"):
        bm = Bm25Retriever(); bm.build(chunks)
        if which != "hybrid":
            retrievers.append(bm)
    vec = None
    if which in ("vector", "hybrid", "all"):
        vec = GeminiVectorRetriever(offline=offline); await vec.build_async(chunks)
        if which != "hybrid":
            retrievers.append(vec)
    if which in ("hybrid", "all"):
        retrievers.append(HybridRetriever(vec, bm, rrf_k=rrf_k))
    suffix = "" if chunking == "legacy" else "@structured"  # legacy 的报告文件名保持原样，已提交的基线不动
    for r in retrievers:
        rep = await evaluate(r, chunks, QUESTIONS, k)
        rep["chunking"] = chunking
        print_report(rep)
        (REPORT_DIR / f"baseline_{r.name}{suffix}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retriever", choices=["vector", "bm25", "hybrid", "all"], default="all")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--chunking", choices=["legacy", "structured"], default="structured")
    ap.add_argument("--offline-cache", action="store_true")
    ap.add_argument("--rrf-k", type=int, default=60)
    args = ap.parse_args()
    asyncio.run(run(args.retriever, args.k, args.chunking, args.offline_cache, args.rrf_k))


if __name__ == "__main__":
    main()
