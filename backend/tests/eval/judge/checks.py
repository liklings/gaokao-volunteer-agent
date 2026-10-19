"""确定性检查：能用代码判断的，不交给 LLM。针对检索/建议类回复，对照工具返回的真实结果。

检查项：
- no_probability：回复里不得出现把位次差包装成概率/把握的说法（agent_prompts 规则 14，红线）。
- discoveries_mentioned：discoveries 非空时，每所信息差学校的校名都应在回复里出现。
- grounded_schools：回复里出现的「XX大学/学院」必须能在工具结果的学校名里找到（防编造学校）。
局限：学校名用正则抽取，简称（如"南航"）抽不到也对不上，这类要靠 LLM 的 grounded 维度补；
只对照 search_candidates 的结果，回复里若来自 get_candidate_detail 等其它工具的学校会被误报，看到误报先核对一下。
"""
from __future__ import annotations

import re

# 红线：概率/成功率字样，或数字化表达（百分号、几成把握）。「把握较高」这类纯定性措辞不算——
# 提示词禁的是把位次差包装成概率，不是禁止一切对把握程度的描述。
_PROB = re.compile(r"\d{1,3}(?:\.\d+)?\s*[%％]|概率|成功率|[一二三四五六七八九十\d]\s*成\s*(?:的)?把握")
_SCHOOL = re.compile(r"[一-龥]{2,14}?(?:大学|学院)")


def tier_candidates(tier) -> list[dict]:
    """search_candidates 的 tiers 里每档的真实结构是 {"total_in_category": n, "candidates": [...]}；
    同时兼容直接给列表的写法（早期测试/校准样本用的简化形态）。"""
    if isinstance(tier, dict):
        return list(tier.get("candidates") or [])
    return list(tier or [])


def tool_universities(tool_data: dict) -> set[str]:
    names: set[str] = set()
    for tier in (tool_data.get("tiers") or {}).values():
        for c in tier_candidates(tier):
            if c.get("university"):
                names.add(c["university"])
    for c in tool_data.get("candidates") or []:
        if c.get("university"):
            names.add(c["university"])
    for c in tool_data.get("discoveries") or []:
        if c.get("university"):
            names.add(c["university"])
    return names


_LEAD = ("还有", "另外", "以及", "比如", "例如", "推荐", "冲刺", "稳妥", "保底", "考虑", "如", "和", "与", "及", "是", "有", "的", "：", "、")


def _trim(name: str) -> str:
    """正则抽出来的片段前面可能粘着"还有/另外"之类的连接词，去掉后再比对/展示。"""
    changed = True
    while changed:
        changed = False
        for lead in _LEAD:
            if name.startswith(lead) and len(name) > len(lead) + 2:
                name, changed = name[len(lead):], True
    return name


_GENERIC = ("高水平大学", "工程大学", "综合性大学", "重点大学", "研究型大学", "一流大学", "双一流大学")


def _is_generic(name: str) -> bool:
    """「985工程大学」「双一流高水平大学」这类是概括性说法，不是学校名，正则会误抽出来。"""
    return any(name.endswith(g) for g in _GENERIC)


def _strip_paren(name: str) -> str:
    """校名带括号后缀（如「华北电力大学(保定)」）时，回复里常写成全角括号或省略，比对前统一去掉。"""
    return re.sub(r"[（(][^）)]*[）)]", "", name).strip()


def _strip_paren_all(text: str) -> str:
    return re.sub(r"[（(][^）)]*[）)]", "", text)


def check_answer(reply: str, tool_data: dict) -> dict:
    known = tool_universities(tool_data)
    mentioned = {_trim(m) for m in _SCHOOL.findall(reply)}
    known_plain = {_strip_paren(k) for k in known} | known
    ungrounded = sorted(m for m in mentioned if not _is_generic(m) and not any(m in k or k in m for k in known_plain))
    disc = [d["university"] for d in (tool_data.get("discoveries") or []) if d.get("university")]
    plain_reply = _strip_paren_all(reply)
    missing_disc = [d for d in disc if d not in reply and _strip_paren(d) not in plain_reply]
    prob = _PROB.findall(reply)
    return {
        "no_probability": not prob,
        "probability_hits": prob,
        "discoveries_mentioned": not missing_disc,
        "discoveries_missing": missing_disc,
        "grounded_schools": not ungrounded,
        "ungrounded": ungrounded,
    }
