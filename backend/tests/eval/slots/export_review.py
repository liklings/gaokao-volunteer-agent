"""导出人工复核表：python -m tests.eval.slots.export_review

输出 tests/eval/slots/reports/review_sheet.csv，列：id、tag、对话、标准答案、是否抽检。
抽检比例默认 25%（≥20% 的要求），按 tag 分层、固定种子，保证每类都有人看。
复核人只需检查"标准答案"是否真的是这段话里说了的内容（尤其是推断出来的槽位和被否定的槽位），
有问题的在最后一列填写，再回来改 cases.py / gen_cases.py。
"""
from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path

from .cases import CASES

OUT = Path(__file__).parent / "reports" / "review_sheet.csv"


def pick_sample(cases, ratio: float = 0.25, seed: int = 7) -> set[str]:
    rng = random.Random(seed)
    by_tag = defaultdict(list)
    for c in cases:
        by_tag[c.tag].append(c.id)
    picked: set[str] = set()
    for ids in by_tag.values():
        k = max(1, round(len(ids) * ratio))
        picked.update(rng.sample(ids, min(k, len(ids))))
    return picked


def main() -> None:
    picked = pick_sample(CASES)
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig：Excel 直接打开不乱码
        w = csv.writer(f)
        w.writerow(["id", "tag", "对话（按轮）", "标准答案", "抽检", "复核结论（填 对/错 + 说明）"])
        for c in CASES:
            w.writerow([c.id, c.tag, " ‖ ".join(c.turns), json.dumps(c.expected, ensure_ascii=False),
                        "是" if c.id in picked else "", ""])
    print(f"共 {len(CASES)} 条，抽检 {len(picked)} 条（{len(picked) / len(CASES):.0%}）→ {OUT}")


if __name__ == "__main__":
    main()
