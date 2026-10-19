"""
OCR 兜底评测。在 backend/ 目录、激活 venv 之后运行：

    OCR_ENGINE=paddleocr python -m tests.eval.ocr.run_ocr_eval            # 你本机：PaddleOCR
    OCR_ENGINE=rapidocr  python -m tests.eval.ocr.run_ocr_eval            # 轻量备选
    python -m tests.eval.ocr.run_ocr_eval --variant degraded --max-pages 4  # 只测几页，快

走的是**生产里真实的代码路径**（policy_chunker.extract_pages → 检测到页面没有文字层 → pymupdf 渲染 →
OCR 引擎），不是单独调 OCR 库。每个变体输出三类指标：
1. 字准率（1-CER）：OCR 文字 vs 原 PDF 文字层，只比汉字/字母/数字（见 text_utils.norm_content）；
2. 关键事实召回：RAG 评测集里该文档的证据短语，有多少能在 OCR 出来的文字里原样找到——这才是
   对检索真正有意义的指标（一个字错在不关键的地方不影响命中，数字/专名错了就废了）；
3. 端到端：把 OCR 出来的文字重新切 chunk、用离线 BM25 跑一遍 RAG 评测，对比"原始文字层"的 hit@5。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "eval-only")

from ..rag.corpus_utils import corpus_files, page_texts  # noqa: E402
from ..rag.metrics import evaluate  # noqa: E402
from ..rag.questions import QUESTIONS  # noqa: E402
from ..rag.retrievers import Bm25Retriever, build_corpus_chunks  # noqa: E402
from ..text_utils import levenshtein, norm, norm_content  # noqa: E402
from .scan_fixture import make_scanned_pdf  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"


def evaluate_doc(src: Path, scanned: Path, max_pages: int | None) -> dict:
    from app.services.policy_chunker import extract_pages

    truth = page_texts(src)
    t0 = time.time()
    pages = {p.page_number: p for p in extract_pages(str(scanned))}
    secs = time.time() - t0

    n_pages = min(len(truth), max_pages or len(truth))
    edits = total = 0
    per_page = []
    for i in range(1, n_pages + 1):
        gt = norm_content(truth[i - 1])
        got = norm_content(pages[i].text) if i in pages else ""
        d = levenshtein(got, gt)
        edits, total = edits + d, total + len(gt)
        per_page.append({"page": i, "cer": round(d / max(len(gt), 1), 4), "ocr": bool(i in pages and pages[i].is_ocr)})
    ocr_text = norm("".join(pages[i].text for i in sorted(pages) if i <= n_pages))
    facts = [e for q in QUESTIONS if q.get("doc") == src.name for e in q["evidence"]]
    # 只统计证据所在页在被测范围内的事实
    in_scope = [e for e in facts if any(norm(e) in norm(truth[i]) for i in range(n_pages))]
    recalled = [e for e in in_scope if norm(e) in ocr_text]
    # 宽松版：只比汉字/字母/数字，忽略标点和运算符号。strict 与 content 的差距 = 只错在符号上的事实
    # （公式里的 ÷ × [ ]、"+" 被认成"十"之类），文字本身是对的，但原样展示给用户会误导。
    ocr_content = norm_content("".join(pages[i].text for i in sorted(pages) if i <= n_pages))
    recalled_content = [e for e in in_scope if norm_content(e) in ocr_content]
    return {
        "doc": src.name, "pages": n_pages, "seconds": round(secs, 1),
        "char_accuracy": round(1 - edits / max(total, 1), 4),
        "worst_page_cer": max(p["cer"] for p in per_page),
        "all_pages_ocr_triggered": all(p["ocr"] for p in per_page),
        "key_fact_recall": round(len(recalled) / max(len(in_scope), 1), 3),
        "key_fact_recall_content_only": round(len(recalled_content) / max(len(in_scope), 1), 3),
        "key_facts_total": len(in_scope),
        "key_facts_lost": [e for e in in_scope if e not in recalled],
        "per_page": per_page,
    }


async def end_to_end(scanned_dir: Path, k: int = 5) -> dict:
    chunks = build_corpus_chunks(sorted(scanned_dir.glob("*.pdf")))
    r = Bm25Retriever(); r.build(chunks)
    rep = await evaluate(r, chunks, QUESTIONS, k)
    return {"hit@%d" % k: rep["overall"]["hit@%d" % k], "mrr": rep["overall"]["mrr"],
            "evidence_intact": rep["overall"]["evidence_intact"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["clean", "degraded", "both"], default="both")
    ap.add_argument("--max-pages", type=int, default=None, help="每份文档只测前 N 页（省时间）；端到端指标仅在不限页时计算")
    args = ap.parse_args()

    from app.config import get_settings
    from app.services.ocr import is_available, resolve_engine_name

    engine = get_settings().ocr_engine
    if not is_available(engine):
        raise SystemExit(f"OCR 引擎 {engine!r} 不可用，先 pip install -r requirements-ocr.txt（或设置 OCR_ENGINE=rapidocr）")
    print(f"OCR 引擎：{engine} → {resolve_engine_name(engine)}，渲染 dpi={get_settings().ocr_render_dpi}")

    variants = ["clean", "degraded"] if args.variant == "both" else [args.variant]
    REPORT_DIR.mkdir(exist_ok=True)
    result: dict = {"engine": engine, "variants": {}}
    for variant in variants:
        with tempfile.TemporaryDirectory() as tmp:
            docs = []
            for src in corpus_files():
                scanned = make_scanned_pdf(src, Path(tmp) / src.name, degrade=(variant == "degraded"),
                                           dpi=get_settings().ocr_render_dpi)
                docs.append(evaluate_doc(src, scanned, args.max_pages))
            e2e = asyncio.run(end_to_end(Path(tmp))) if args.max_pages is None else None
        result["variants"][variant] = {"docs": docs, "end_to_end_bm25": e2e}
        print(f"\n--- {variant} ---")
        for d in docs:
            print(f"{d['doc'][:22]:<24} {d['pages']:>2}页 {d['seconds']:>6}s  字准率={d['char_accuracy']:.2%}  最差页CER={d['worst_page_cer']:.1%}  "
                  f"关键事实召回(严格)={d['key_fact_recall']:.0%}({d['key_facts_total'] - len(d['key_facts_lost'])}/{d['key_facts_total']}) 忽略符号={d['key_fact_recall_content_only']:.0%}  全部页触发OCR={d['all_pages_ocr_triggered']}")
            for lost in d["key_facts_lost"]:
                print(f"    丢失：{lost}")
        if e2e:
            print(f"端到端(离线BM25) hit@5={e2e['hit@5']}  MRR={e2e['mrr']}  证据完整={e2e['evidence_intact']}")
    (REPORT_DIR / f"ocr_{engine}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
