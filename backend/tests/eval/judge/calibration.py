"""评审员校准集：人工标注"好回复/坏回复"，检验评审员自己靠不靠谱。

LLM 评审员本身会有偏差（偏爱长回复、被华丽措辞带偏、该扣分的不扣）。在拿它的分数下结论之前，
先让它给这批人工标好对错的回复打分，看和人工标注的一致率：
- agreement ≥ CALIBRATION_MIN_AGREEMENT 才认为评审员可信；
- 重点看 false_pass（坏回复被评审员判合格）——这类错误会让问题溜过去，比误杀更危险。

labels：{维度: True=应当合格 / False=应当不合格}，只标注人工有把握的维度，没标的不参与一致率。
这批样本是人工构造的（故意带单一缺陷），难度偏低；一致率高只说明评审员能抓住明显缺陷，
细微质量差异它是否分得出来，需要以后用线上真实回复补充标注。
"""
from __future__ import annotations

from .judge import JudgeItem, Verdict

CALIBRATION_MIN_AGREEMENT = 0.85

_KNOWN_FULL = {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 610, "major_keyword": "计算机"}


def _p(id_, user, known, missing, reply, labels, history=None):
    return JudgeItem(id_, "profile_reply",
                     {"对话历史": history or [], "本轮用户输入": user, "已收集的条件": known, "仍缺的必填项": missing},
                     reply), labels


_FACTS = {
    "考生": "江苏 物理类 位次5000 想学计算机",
    "tiers": {
        "保底": [{"university": "南京航空航天大学", "rank_diff": 2300, "school_tier": "211", "city": "南京"}],
        "稳妥": [{"university": "苏州大学", "rank_diff": 800, "school_tier": "211", "city": "苏州"}],
        "冲刺": [{"university": "东南大学", "rank_diff": -600, "school_tier": "985", "city": "南京"}],
    },
    "discoveries": [{"university": "石河子大学", "city": "石河子", "school_tier": "211", "rank_diff": 900, "category_label": "稳妥"}],
}
_FACTS_NO_DISC = {**_FACTS, "discoveries": []}
_FACTS_NOTE = {**_FACTS_NO_DISC, "tiers": {**_FACTS["tiers"], "稳妥": [
    {"university": "苏州大学", "rank_diff": 800, "school_tier": "211", "city": "苏州",
     "rank_equivalent_note": "没能做跨年换算，按原始位次计算，仅供参考"}]}}


def _a(id_, facts, reply, labels):
    return JudgeItem(id_, "advisor_answer", {"用户请求": "帮我推荐一份志愿", "工具结果": facts}, reply), labels


_GOOD_ANSWER = (
    "按你的位次5000，我分三档给你：\n保底：南京航空航天大学（位次差约+2300，较稳）。\n稳妥：苏州大学（+800）。\n"
    "冲刺：东南大学（-600，需要一点运气）。\n\n你可能没注意到的好学校：石河子大学是211，位于石河子，不少人一看城市就跳过，"
    "但它的位次差约+900，属于稳妥档，对口计算机，值得认真考虑。\n建议把东南大学放第一志愿冲一冲，南航兜底；"
    "如果更在意城市环境，再评估石河子的取舍。"
)

GOLD: list[tuple[JudgeItem, dict]] = [
    _p("p-good-ask", "我是江苏考生", {"province": "江苏"}, ["类别", "选科", "分数或位次", "专业关键词"],
       "好的，江苏考生。请问你是物理类还是历史类，选科组合是什么？",
       {"ask_fit": True, "concise": True, "no_fabrication": True, "tone": True}),
    _p("p-too-many-asks", "我是江苏考生", {"province": "江苏"}, ["类别", "选科", "分数或位次", "专业关键词"],
       "请告诉我你的类别、选科、分数、位次、想学的专业、城市偏好、学费上限，以及是否接受中外合作办学和专项计划。",
       {"ask_fit": False, "no_fabrication": True}),
    _p("p-asks-known", "610分", {k: v for k, v in _KNOWN_FULL.items() if k != "major_keyword"}, ["专业关键词"],
       "好的。请问你是哪个省份的考生，选科是什么？",
       {"ask_fit": False}),
    _p("p-fabricates", "我考了610分，想学计算机", {"score": 610, "major_keyword": "计算机"}, ["省份", "类别", "选科"],
       "610分很稳，南京航空航天大学计算机几乎肯定能上！请告诉我你的省份和选科。",
       {"no_fabrication": False}),
    _p("p-verbose", "我是江苏考生", {"province": "江苏"}, ["类别", "选科", "分数或位次", "专业关键词"],
       "你好呀！非常高兴能够帮助到你。高考志愿填报是人生中非常重要的一步，需要综合考虑很多因素，比如分数、位次、选科、兴趣、城市、"
       "就业前景等等。我会陪你一步一步地梳理清楚。首先，你已经告诉我你是江苏考生了，真的很棒。接下来，为了更好地帮助你，"
       "麻烦你再告诉我你是物理类还是历史类，以及你的具体选科组合。",
       {"concise": False}),
    _p("p-answers-then-asks", "什么是平行志愿？", {}, ["省份", "类别", "选科", "分数或位次", "专业关键词"],
       "平行志愿是指同一批次里可以填多个院校志愿，系统按分数从高到低依次投档，每位考生最终只会被其中一个录取。"
       "方便的话，先告诉我你是哪个省份、物理类还是历史类吧？",
       {"ask_fit": True, "concise": True, "no_fabrication": True}),
    _p("p-ignores-question", "什么是平行志愿？", {}, ["省份", "类别", "选科", "分数或位次", "专业关键词"],
       "请提供您的省份、类别和选科。",
       {"ask_fit": False}),
    _p("p-rude", "我还没想好选科", {"province": "江苏"}, ["类别", "选科", "分数或位次", "专业关键词"],
       "你怎么连选科都没定？这样我没法帮你，赶紧想清楚再来。",
       {"tone": False}),
    _p("p-asks-required-when-full", "我还想去上海读书", {**_KNOWN_FULL, "city_pref": "上海"}, [],
       "好的，记下了偏好上海。请再告诉我你的省份和选科组合。",
       {"ask_fit": False}),
    _p("p-ready-optional-ask", "610分，想学计算机", {**_KNOWN_FULL}, [],
       "条件已经齐了，我来为你检索候选。另外，是否愿意考虑中外合作办学或专项计划？（不考虑可以不回复，默认不会推荐这两类。）",
       {"ask_fit": True, "no_fabrication": True, "concise": True}),

    _a("a-good", _FACTS, _GOOD_ANSWER,
       {"grounded": True, "discovery_value": True, "structure": True, "helpful": True}),
    _a("a-hallucinated-school", _FACTS_NO_DISC,
       "保底：南京航空航天大学（+2300）。稳妥：苏州大学（+800）。冲刺：复旦大学（-300），计算机很强，也可以试试。建议东南大学冲一冲。",
       {"grounded": False}),
    _a("a-ignores-discovery", _FACTS,
       "保底：南京航空航天大学（+2300，较稳）。稳妥：苏州大学（+800）。冲刺：东南大学（-600）。建议东南大学第一志愿冲，南航兜底。",
       {"discovery_value": False, "structure": True}),
    _a("a-discovery-name-only", _FACTS,
       "保底：南京航空航天大学（+2300）。稳妥：苏州大学（+800）。冲刺：东南大学（-600）。另外还有石河子大学。",
       {"discovery_value": False}),
    _a("a-bare-list", _FACTS_NO_DISC,
       "推荐：南京航空航天大学、苏州大学、东南大学。",
       {"structure": False, "helpful": False, "grounded": True}),
    _a("a-note-ignored", _FACTS_NOTE,
       "保底：南京航空航天大学（+2300）。稳妥：苏州大学（位次差+800，比较稳）。冲刺：东南大学（-600）。建议东南大学冲，苏州大学作主力。",
       {"rank_note": False}),
    _a("a-note-conveyed", _FACTS_NOTE,
       "保底：南京航空航天大学（+2300）。稳妥：苏州大学（位次差约+800，不过这条没能做跨年换算、仅供参考，可信度打了折扣）。"
       "冲刺：东南大学（-600）。建议东南大学冲，苏州大学作主力，但填之前最好再核对一下它近几年的录取位次。",
       {"rank_note": True, "grounded": True}),
]


def agreement(verdicts: list[Verdict]) -> dict:
    by_id = {v.item_id: v for v in verdicts}
    total = agree = false_pass = false_fail = errors = 0
    disagreements = []
    for item, labels in GOLD:
        v = by_id.get(item.id)
        if v is None or v.error:
            errors += len(labels)
            continue
        for key, should_pass in labels.items():
            got = v.passed(key)
            total += 1
            if got is None:  # 评审员标不适用，但这个维度人工认为适用
                false_fail += should_pass
                false_pass += (not should_pass)
                disagreements.append({"item": item.id, "key": key, "human": should_pass, "judge": "N/A",
                                      "reason": v.scores.get(key, {}).get("reason")})
                continue
            if got == should_pass:
                agree += 1
            else:
                false_pass += (got and not should_pass)
                false_fail += ((not got) and should_pass)
                disagreements.append({"item": item.id, "key": key, "human": should_pass, "judge": got,
                                      "score": v.scores[key]["score"], "reason": v.scores[key]["reason"]})
    n = total + errors
    return {
        "labels": n, "scored": total, "errors": errors, "agree": agree,
        "agreement": round(agree / n, 3) if n else None,
        "false_pass": false_pass, "false_fail": false_fail,
        "reliable": bool(n) and agree / n >= CALIBRATION_MIN_AGREEMENT and false_pass == 0,
        "disagreements": disagreements,
    }
