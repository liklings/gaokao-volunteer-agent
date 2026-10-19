"""用例生成器：按槽位参数化模板批量造用例，标准答案由生成参数直接得出（不靠人工抄写，所以不会抄错）。

固定随机种子，每次运行生成的用例完全一致（用例 id 和内容都稳定，缓存才能命中）。
局限（如实记录）：模板化用例的句式多样性有限，只能保证"槽位组合 × 表达方式"的覆盖广度；
真实用户的口语、错别字、跑题、冲突输入要靠手写用例（cases.py 里的 hand-* / hard-*）和以后
线上真实对话回流来补。
"""
from __future__ import annotations

import random

PROVINCES = ["江苏", "河北", "辽宁", "福建", "湖北", "湖南", "广东", "重庆"]  # 均为 3+1+2 新高考省份
PHYS_COMBOS = [["物理", "化学", "生物"], ["物理", "化学", "地理"], ["物理", "生物", "地理"],
               ["物理", "化学", "政治"], ["物理", "政治", "地理"]]
HIST_COMBOS = [["历史", "政治", "地理"], ["历史", "政治", "生物"], ["历史", "化学", "生物"],
               ["历史", "地理", "生物"], ["历史", "化学", "地理"]]
_SHORT = {"物理": "物", "化学": "化", "生物": "生", "地理": "地", "政治": "政", "历史": "史"}
MAJORS = ["计算机", "软件工程", "人工智能", "电子信息", "临床医学", "口腔医学", "法学", "金融", "会计",
          "汉语言文学", "新闻传播", "师范", "机械", "自动化", "土木", "建筑学", "化学", "数学", "心理学",
          "农学", "英语", "经济学", "护理学", "药学", "电气工程"]
CITIES = ["上海", "北京", "杭州", "南京", "广州", "成都", "武汉", "西安"]

CHITCHAT = ["你好", "你能帮我做什么？", "平行志愿是什么意思？", "什么是专业组？", "一分一段表是什么？",
            "怎么查自己的位次？", "填报志愿一般在什么时候？", "谢谢", "你是谁？", "我有点紧张，不知道怎么填",
            "志愿能填几个？", "冲稳保是什么意思？", "什么是调剂？", "服从调剂有什么风险？", "好的，我明白了",
            "再说一遍刚才的意思", "你们这个系统靠谱吗？", "能不能帮我看看志愿填报的流程", "先不填了，我再想想",
            "先聊聊选专业该看什么吧"]


def _subj(rng: random.Random, subs: list[str]) -> str:
    style = rng.choice(["full", "short", "dun", "plus"])
    if style == "short":
        return "".join(_SHORT[s] for s in subs)
    return {"full": "".join(subs), "dun": "、".join(subs), "plus": "+".join(subs)}[style]


def _number(rng: random.Random, use_rank: bool) -> tuple[str, dict]:
    if use_rank:
        r = rng.choice([800, 1500, 3000, 5000, 8000, 12000, 20000, 35000, 60000, 3500, 9500, 15500])
        opts = [f"位次{r}", f"全省排名{r}名", f"排名大概{r}名", f"位次约{r}"]
        if r % 1000 == 0 and r >= 10000:
            opts.append(f"位次{r / 10000:g}万")
        return rng.choice(opts), {"rank": r}
    s = rng.randint(480, 690)
    return rng.choice([f"{s}分", f"分数{s}", f"考了{s}", f"高考{s}分", f"裸分{s}"]), {"score": s}


def _addons(rng: random.Random) -> tuple[list[str], dict]:
    """随机 0–2 条可选偏好，返回（短句列表，对应的期望槽位）。"""
    pool = []
    x = rng.choice([6000, 8000, 10000, 20000])
    pool.append((f"学费每年不超过{x}元", {"budget_max": x}))
    w = rng.choice([1, 2, 3])
    pool.append((f"学费一年最多{w}万", {"budget_max": w * 10000}))
    c = rng.choice(CITIES)
    pool.append((f"想去{c}读书", {"city_pref": c}))
    pool.append((rng.choice(["中外合作办学可以考虑", "中外合办也行"]), {"accept_sino_foreign": True}))
    pool.append((rng.choice(["不考虑中外合作", "中外合办的不要"]), {"accept_sino_foreign": False}))
    pool.append((rng.choice(["专项计划可以看看", "定向培养的也可以"]), {"accept_special_program": True}))
    pool.append((rng.choice(["不要专项计划", "定向培养的不考虑"]), {"accept_special_program": False}))
    pool.append((rng.choice(["只想留在省内", "外省的学校不考虑"]), {"home_province_only": True}))
    texts, exp = [], {}
    for text, e in rng.sample(pool, rng.choice([0, 1, 1, 2])):
        if any(k in exp for k in e):  # 同一槽位不叠加
            continue
        texts.append(text)
        exp.update(e)
    return texts, exp


def _profile(rng: random.Random, with_track: bool = True) -> dict:
    track = rng.choice(["物理类", "历史类"])
    subs = rng.choice(PHYS_COMBOS if track == "物理类" else HIST_COMBOS)
    num_text, num_exp = _number(rng, rng.random() < 0.4)
    return {"province": rng.choice(PROVINCES), "track": track, "subs": subs,
            "subj_text": _subj(rng, subs), "num_text": num_text, "num_exp": num_exp,
            "major": rng.choice(MAJORS), "with_track": with_track}


def _expected(p: dict) -> dict:
    return {"province": p["province"], "track": p["track"], "subjects": p["subs"],
            "major_keyword": p["major"], **p["num_exp"]}


def _single_text(rng: random.Random, p: dict, extras: list[str]) -> str:
    P, T, S, N, M = p["province"], p["track"] if p["with_track"] else None, p["subj_text"], p["num_text"], p["major"]
    parts = {
        0: [f"我是{P}考生", T, f"选科{S}", N, f"想学{M}"],
        1: [f"{P}{T or ''}", S, N, f"意向专业{M}"],
        2: [N, f"{P}的{T + '考生' if T else '考生'}", f"选了{S}", f"对{M}感兴趣"],
        3: ["帮我看看志愿", P, T, S, N, f"想读{M}相关专业"],
        4: [f"我家孩子{P}{T or ''}", f"选科是{S}", N, f"打算报{M}"],
        5: [f"想读{M}", f"{P}考生", S, T, N],
    }[rng.randrange(6)]
    return "，".join([x for x in parts if x] + extras)


def generate(Case) -> list:
    rng = random.Random(20261006)
    out = []

    # 1) 单轮齐全 × 随机可选偏好（其中约 1/4 省略类别，要求由选科推断）
    for i in range(40):
        p = _profile(rng, with_track=rng.random() > 0.25)
        extras, extra_exp = _addons(rng)
        out.append(Case(f"gen-full-{i:02d}", "gen_full" if p["with_track"] else "gen_infer_track",
                        [_single_text(rng, p, extras)], {**_expected(p), **extra_exp}))

    # 2) 多轮补全：把一份完整条件拆成 2–4 轮
    for i in range(30):
        p = _profile(rng)
        P, T, S, M = p["province"], p["track"], p["subj_text"], p["major"]
        chunks = [rng.choice([f"我是{P}的{T}考生", f"{P}考生，{T}"]), rng.choice([f"选科是{S}", f"选了{S}"]),
                  p["num_text"], rng.choice([f"想学{M}", f"专业想读{M}"])]
        n_turns = rng.choice([2, 3, 4])
        cuts = sorted(rng.sample(range(1, 4), n_turns - 1))
        turns, prev = [], 0
        for c in cuts + [4]:
            turns.append("，".join(chunks[prev:c]))
            prev = c
        out.append(Case(f"gen-multi-{i:02d}", "gen_multi", turns, _expected(p), followup_turns=tuple(range(len(turns) - 1))))

    # 3) 纠偏：第二轮改掉一个已说过的值
    for i in range(20):
        p = _profile(rng)
        kind = i % 4
        base_extra, exp = [], _expected(p)
        if kind == 2:
            base_extra, exp_extra = ["学费每年不超过8000元"], {"budget_max": 8000}
            exp.update(exp_extra)
        if kind == 3:
            base_extra, exp_extra = ["想去北京读书"], {"city_pref": "北京"}
            exp.update(exp_extra)
        turns = [_single_text(rng, p, base_extra)]
        if kind == 0:
            if "score" in exp:
                s2 = exp["score"] + rng.choice([-12, -5, 7, 15])
                turns.append(rng.choice([f"不好意思，分数记错了，是{s2}分", f"分数更正一下，应该是{s2}"]))
                exp["score"] = s2
            else:
                r2 = exp["rank"] + 500
                turns.append(f"位次说错了，是{r2}名")
                exp["rank"] = r2
        elif kind == 1:
            m2 = rng.choice([m for m in MAJORS if m != p["major"]])
            turns.append(rng.choice([f"专业改成{m2}吧", f"我想了想，还是学{m2}"]))
            exp["major_keyword"] = m2
        elif kind == 2:
            turns.append("学费上限改成两万吧")
            exp["budget_max"] = 20000
        else:
            turns.append("还是改成去上海吧")
            exp["city_pref"] = "上海"
        out.append(Case(f"gen-fix-{i:02d}", "gen_correction", turns, exp))

    # 4) 只表达一个可选偏好的短句（多种说法）
    single = [
        (["中外合作办学我可以考虑", "中外合办也行", "我对中外合作的项目不排斥"], {"accept_sino_foreign": True}),
        (["不考虑中外合作办学", "中外合办的就不要了", "中外合作的太贵，不要"], {"accept_sino_foreign": False}),
        (["专项计划我愿意看看", "国家专项和地方专项我可以考虑", "定向培养的也可以"], {"accept_special_program": True}),
        (["不想要专项计划", "定向培养的不考虑", "公费师范这类定向的不要"], {"accept_special_program": False}),
        (["只想在省内读", "外省学校不考虑", "不要给我推荐外省的学校"], {"home_province_only": True}),
    ]
    k = 0
    for texts, exp in single:
        for t in texts:
            out.append(Case(f"gen-opt-{k:02d}", "gen_optional", [t], exp))
            k += 1
    for c in CITIES[:4]:
        out.append(Case(f"gen-opt-{k:02d}", "gen_optional", [rng.choice([f"我想去{c}读书", f"想在{c}上学", f"优先{c}的学校"])], {"city_pref": c}))
        k += 1
    for x in (6000, 12000, 30000):
        out.append(Case(f"gen-opt-{k:02d}", "gen_optional", [f"学费不要超过{x}元一年"], {"budget_max": x}))
        k += 1

    # 5) 闲聊/提问：不该填任何槽位，也不该因此被误判
    for i, t in enumerate(CHITCHAT):
        out.append(Case(f"gen-neg-{i:02d}", "gen_negative", [t], {}))
    return out
