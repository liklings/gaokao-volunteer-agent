from __future__ import annotations

from collections import defaultdict

from ..text_utils import norm
from .retrievers import CorpusChunk, Hit


def is_gold_hit(hit: Hit, doc: str, evidence: list[str]) -> bool:
    nt = norm(hit.text)
    return hit.doc == doc and any(norm(e) in nt for e in evidence)


def evidence_intact(chunks: list[CorpusChunk], doc: str, evidence: list[str]) -> bool:
    """评测集里这条问题的证据短语，是否完整地落在至少一个 chunk 里。False 说明切分把证据切断了——
    这时检索再准也命中不了，属于"切分问题"而不是"检索问题"，单独统计避免误判。"""
    return any(c.doc == doc and any(norm(e) in norm(c.text) for e in evidence) for c in chunks)


def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


async def evaluate(retriever, chunks: list[CorpusChunk], questions: list[dict], k: int = 5) -> dict:
    rows, pos_top1, neg_top1 = [], [], []
    for q in questions:
        hits = await retriever.search(q["q"], k)
        top1 = hits[0].score if hits else 0.0
        if q["tag"] == "negative":
            neg_top1.append(top1)
            rows.append({"id": q["id"], "tag": "negative", "top1_score": round(top1, 4),
                         "top1_doc_page": f"{hits[0].doc}#p{hits[0].page}" if hits else None})
            continue
        rank = next((i for i, h in enumerate(hits, 1) if is_gold_hit(h, q["doc"], q["evidence"])), None)
        pos_top1.append(top1)
        rows.append({
            "id": q["id"], "tag": q["tag"], "rank": rank, "top1_score": round(top1, 4),
            "evidence_intact": evidence_intact(chunks, q["doc"], q["evidence"]),
            "doc_top1_correct": bool(hits) and hits[0].doc == q["doc"],
            "top1_doc_page": f"{hits[0].doc}#p{hits[0].page}" if hits else None,
        })

    def summarize(subset: list[dict]) -> dict:
        n = len(subset)
        if not n:
            return {"n": 0}
        def at(kk): return sum(1 for r in subset if r["rank"] and r["rank"] <= kk) / n
        return {
            "n": n,
            "hit@1": round(at(1), 3), "hit@3": round(at(3), 3), f"hit@{k}": round(at(k), 3),
            "mrr": round(sum(1 / r["rank"] for r in subset if r["rank"]) / n, 3),
            "doc_top1": round(sum(r["doc_top1_correct"] for r in subset) / n, 3),
            "evidence_intact": round(sum(r["evidence_intact"] for r in subset) / n, 3),
        }

    positives = [r for r in rows if r["tag"] != "negative"]
    by_tag = defaultdict(list)
    for r in positives:
        by_tag[r["tag"]].append(r)
    threshold = max(neg_top1) if neg_top1 else None
    return {
        "retriever": retriever.name, "k": k, "n_chunks": len(chunks),
        "overall": summarize(positives),
        "by_tag": {t: summarize(rs) for t, rs in sorted(by_tag.items())},
        "negative_separation": {
            "auc_top1_pos_vs_neg": auc(pos_top1, neg_top1),
            "max_negative_top1": threshold,
            "answerable_passing_if_threshold_is_max_negative": (
                round(sum(s > threshold for s in pos_top1) / len(pos_top1), 3) if threshold is not None and pos_top1 else None
            ),
            "mean_top1_pos": round(sum(pos_top1) / len(pos_top1), 4) if pos_top1 else None,
            "mean_top1_neg": round(sum(neg_top1) / len(neg_top1), 4) if neg_top1 else None,
        },
        "rows": rows,
    }
