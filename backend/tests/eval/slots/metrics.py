"""Slot F1 的评分逻辑——纯函数，不依赖 Gemini/数据库，可以离线单测。

口径（写在这里，避免以后对"F1 到底怎么算的"各说各话）：

- 评分对象是"最终槽位状态"：把每一轮 Agent 抽出的 profile 按生产代码同样的
  规则合并（非 null 覆盖、null 跳过，见 orchestrator._merge_profile_update），
  得到一份最终槽位，再跟标准答案比。
- 标准答案 expected 里**只写这句话/这段对话明确表达了的槽位**；没写的槽位，
  标准答案视为"应当为 null"。Agent 凭空填了值 = 误报（FP），这正是"幻觉填槽"
  要被惩罚的地方。
- 每个 (槽位, 值) 是一个判断单元：
  TP = 标准里有且预测等价；FN = 标准里有但预测缺失或不等价；
  FP = 预测有值，但标准里没有，或值不等价。
  （值错了同时记 1 FP + 1 FN，这是 slot-F1 的通用做法。）
- 整体用 micro-F1（把所有单元累加再算），同时给每个槽位单独的 P/R/F1。
- year 字段不计分：它是 Agent 可能顺手推断的辅助字段，不是用户需求槽位。
- 等价判断是"宽松但有界"的：省份去掉省/市/自治区后缀；类别"物理"="物理类"；
  选科按集合比较并归一同义写法（思想政治=政治）；专业/城市偏好允许一方包含
  另一方（"计算机"≈"计算机类"）；数字必须完全相等。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

SCORED_SLOTS = [
    "province", "track", "subjects", "score", "rank", "major_keyword",
    "city_pref", "budget_max", "home_province_only",
    "accept_sino_foreign", "accept_special_program",
]
REQUIRED_SLOTS = ["province", "track", "subjects", "score_or_rank", "major_keyword"]

_SUBJECT_ALIASES = {"思想政治": "政治", "思政": "政治", "政": "政治", "史": "历史", "地": "地理",
                    "物": "物理", "化": "化学", "生": "生物"}


def _norm_province(v) -> str:
    s = str(v).strip()
    for suf in ("省", "市", "壮族自治区", "回族自治区", "维吾尔自治区", "自治区"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def _norm_track(v) -> str:
    s = str(v).strip()
    return s if s.endswith("类") else s + "类"


def _norm_subjects(v) -> frozenset:
    return frozenset(_SUBJECT_ALIASES.get(str(x).strip(), str(x).strip()) for x in (v or []))


def _text_match(a, b) -> bool:
    a, b = str(a).strip(), str(b).strip()
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def is_empty(v) -> bool:
    return v is None or v == [] or v == ""


def values_match(slot: str, expected, got) -> bool:
    if slot == "province":
        return _norm_province(expected) == _norm_province(got)
    if slot == "track":
        return _norm_track(expected) == _norm_track(got)
    if slot == "subjects":
        return _norm_subjects(expected) == _norm_subjects(got)
    if slot in ("major_keyword", "city_pref"):
        return _text_match(expected, got)
    if slot in ("score", "rank", "budget_max"):
        try:
            return int(expected) == int(got)
        except (TypeError, ValueError):
            return False
    return expected == got  # 布尔开关：严格相等


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: "Counts") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn

    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    def f1(self) -> float | None:
        p, r = self.precision(), self.recall()
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if p + r else 0.0


def score_case(expected: dict, got: dict) -> dict[str, Counts]:
    """返回 {槽位: Counts}，只包含 SCORED_SLOTS。"""
    out: dict[str, Counts] = {}
    for slot in SCORED_SLOTS:
        c = Counts()
        exp = expected.get(slot)
        pred = got.get(slot)
        if slot in _FALSE_EQUALS_UNSET:
            exp = exp or None   # False → 未表态
            pred = pred or None
        exp_has, pred_has = not is_empty(exp), not is_empty(pred)
        if exp_has and pred_has:
            if values_match(slot, exp, pred):
                c.tp = 1
            else:
                c.fp = 1
                c.fn = 1
        elif exp_has:
            c.fn = 1
        elif pred_has:
            c.fp = 1
        out[slot] = c
    return out


# home_province_only 在 ProfileIn / Profile 里是普通 bool（默认 False，没有 None 这一态），
# "没表态"和"明确说外省也可以"在下游效果完全一样（都不过滤）。Agent 对没提到的情况返回 false
# 不影响任何行为，所以这个槽位把"未表态"和 False 视为等价；只有 True（明确要求只看省内）
# 才是有意义的值。accept_sino_foreign / accept_special_program 是真三态（None≠False），仍严格比较。
_FALSE_EQUALS_UNSET = {"home_province_only"}


def merge_update(state: dict, update: dict) -> dict:
    """跟 orchestrator._merge_profile_update 同一规则：非 null 覆盖，null 跳过。"""
    for k, v in (update or {}).items():
        if v is not None:
            state[k] = v
    return state


def missing_required(state: dict) -> list[str]:
    """跟 profile_rules.compute_missing 同一规则（用 dict 版，免依赖数据库模型）。"""
    miss = []
    for slot in ("province", "track", "subjects", "major_keyword"):
        if is_empty(state.get(slot)):
            miss.append(slot)
    if not state.get("score") and not state.get("rank"):
        miss.append("score_or_rank")
    return miss


_ASK_MARKERS = ("请告知", "请告诉", "请问", "请提供", "请补充", "请说明", "请确认", "请您", "能否", "可以告诉", "是否", "吗",
                "还需要", "需要了解", "需要您", "想了解", "告知我")


def has_question(reply: str) -> bool:
    """回复里有没有在向用户要信息：问号，或"请告知/请问…"这类祈使式追问
    （中文里"请告知您的省份"没有问号也是追问，只认问号会误判漏追问）。"""
    return "？" in reply or "?" in reply or any(m in reply for m in _ASK_MARKERS)


@dataclass
class SlotReport:
    overall: Counts = field(default_factory=Counts)
    by_slot: dict = field(default_factory=lambda: defaultdict(Counts))
    by_tag: dict = field(default_factory=lambda: defaultdict(Counts))
    n_cases: int = 0
    n_exact: int = 0
    followup_total: int = 0
    followup_ok: int = 0
    rows: list = field(default_factory=list)


def summarize(results: list[dict]) -> SlotReport:
    """results: [{id, tag, expected, state, followup: [(turn, ok)], error?}]"""
    rep = SlotReport()
    for r in results:
        rep.n_cases += 1
        if r.get("error"):
            # 调用失败：标准答案里的每个槽位都算漏检，不能悄悄略过
            counts = score_case(r["expected"], {})
        else:
            counts = score_case(r["expected"], r["state"])
        exact = all(c.fp == 0 and c.fn == 0 for c in counts.values())
        rep.n_exact += exact
        for slot, c in counts.items():
            rep.overall.add(c)
            rep.by_slot[slot].add(c)
            rep.by_tag[r["tag"]].add(c)
        for _, ok in r.get("followup", []):
            rep.followup_total += 1
            rep.followup_ok += bool(ok)
        rep.rows.append({
            "id": r["id"], "tag": r["tag"], "exact": exact, "error": r.get("error"),
            "followup_failed": [
                {"turn": t, "reply": (r.get("replies") or [""] * (t + 1))[t]}
                for t, ok in r.get("followup", []) if not ok
            ],
            "errors": [
                {"slot": s, "expected": r["expected"].get(s), "got": (r.get("state") or {}).get(s)}
                for s, c in counts.items() if c.fp or c.fn
            ],
        })
    return rep
