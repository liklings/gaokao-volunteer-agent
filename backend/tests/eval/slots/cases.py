"""Slot F1 种子用例集。

每条用例 = 一段（单轮或多轮）用户原话 + 最终应当抽到的槽位。
expected 里只写对话里**明确表达**的槽位，没写的槽位应当保持 null——
Agent 凭空填值会被记为误报（见 metrics.py 顶部口径）。

followup_turns：这些轮（从 0 数）结束时必填项仍不齐，Agent 的回复里应该
包含追问（含问号）。这是需求文档里"多轮纠偏与补全"的最小可自动化代理指标。

= 手写种子（seed/hard/hand）+ gen_cases.py 参数化生成。要作为正式指标（>98%）
需要再扩到 300–500 条并人工复核至少 20%（见 README、export_review.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Case:
    id: str
    tag: str
    turns: list[str]
    expected: dict
    followup_turns: tuple = field(default_factory=tuple)


CASES: list[Case] = [
    # —— 单轮、信息齐全 ——
    Case("full-01", "full", ["我是江苏考生，物理类，选了物理化学生物，高考610分，想学计算机"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 610, "major_keyword": "计算机"}),
    Case("full-02", "full", ["江苏历史类，选科历史+政治+地理，位次12000，想读法学"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "rank": 12000, "major_keyword": "法学"}),
    Case("full-03", "full", ["我是江苏的物理类考生，选科物理、化学、地理，全省排名3500名，想学临床医学"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "地理"], "rank": 3500, "major_keyword": "临床医学"}),
    Case("full-04", "full", ["江苏考生，历史类，历史地理生物，分数585，想读师范类"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "地理", "生物"], "score": 585, "major_keyword": "师范"}),
    Case("full-05", "full", ["江苏，物化生，物理类，620分，想学人工智能"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 620, "major_keyword": "人工智能"}),
    Case("full-06", "full", ["江苏历史类 史政地 位次8000 想学金融"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "rank": 8000, "major_keyword": "金融"}),
    Case("full-07", "full", ["我家娃是江苏的，物理类，选科是物理、化学、生物，考了598，想让他学自动化"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 598, "major_keyword": "自动化"}),
    Case("full-08", "full", ["本人江苏历史类，政史地，位次大概15000左右，对心理学感兴趣"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "rank": 15000, "major_keyword": "心理学"}),
    Case("full-09", "full", ["我选的是物理、政治、地理，江苏物理类，630分，想学计算机"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "政治", "地理"], "score": 630, "major_keyword": "计算机"}),
    Case("full-10", "full", ["江苏历史类，选科历史、化学、生物，位次20000，想学农学"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "化学", "生物"], "rank": 20000, "major_keyword": "农学"}),
    Case("full-11", "full", ["我是山东的物理类考生，选科物理化学生物，分数640，想读口腔医学"],
         {"province": "山东", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 640, "major_keyword": "口腔医学"}),
    Case("full-12", "full", ["江苏物理类物化生考生，高考成绩672分，目标是集成电路"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 672, "major_keyword": "集成电路"}),

    # —— 分数 / 位次的表达 ——
    Case("num-01", "numbers", ["我考了610分，位次大概在8000名"], {"score": 610, "rank": 8000}, followup_turns=(0,)),
    Case("num-02", "numbers", ["我的全省排名是两万三千名"], {"rank": 23000}, followup_turns=(0,)),
    Case("num-03", "numbers", ["分数是五百八十二分"], {"score": 582}, followup_turns=(0,)),

    # —— 信息不全：该追问、不该幻觉 ——
    Case("part-01", "partial", ["我是江苏的考生"], {"province": "江苏"}, followup_turns=(0,)),
    Case("part-02", "partial", ["我想学计算机"], {"major_keyword": "计算机"}, followup_turns=(0,)),
    Case("part-03", "partial", ["我考了610分，能去哪"], {"score": 610}, followup_turns=(0,)),
    Case("part-04", "partial", ["你好，我想了解一下志愿填报"], {}),
    Case("part-05", "partial", ["什么是平行志愿？"], {}),
    Case("part-06", "partial", ["谢谢你，再见"], {}),

    # —— 可选偏好与开关 ——
    Case("opt-01", "optional", ["我想学计算机，学费每年不要超过8000元"], {"major_keyword": "计算机", "budget_max": 8000}),
    Case("opt-02", "optional", ["学费一年一万五以内，江苏物理类物化生，600分，想学化学"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 600, "major_keyword": "化学", "budget_max": 15000}),
    Case("opt-03", "optional", ["我想去上海读书"], {"city_pref": "上海"}),
    Case("opt-04", "optional", ["只想留在省内，不考虑外省的学校，我是江苏考生"], {"province": "江苏", "home_province_only": True}),
    Case("opt-05", "optional", ["外省也可以，不限制省份。我是江苏考生"], {"province": "江苏", "home_province_only": False}),
    Case("opt-06", "optional", ["中外合作办学我可以考虑"], {"accept_sino_foreign": True}),
    Case("opt-07", "optional", ["中外合办太贵了，不考虑"], {"accept_sino_foreign": False}),
    Case("opt-08", "optional", ["专项计划我愿意看看"], {"accept_special_program": True}),
    Case("opt-09", "optional", ["我不想被户籍和服务期限制，定向培养的不要"], {"accept_special_program": False}),
    Case("opt-10", "optional", ["中外合作可以，专项计划不要"], {"accept_sino_foreign": True, "accept_special_program": False}),
    Case("opt-11", "optional", ["我不限制城市，学费也没要求，江苏物理类物化生，600分，想学数学"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 600, "major_keyword": "数学"}),

    # —— 多轮补全 ——
    Case("multi-01", "multi_turn", ["我是江苏考生", "物理类，选物理化学生物", "610分", "想学计算机"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 610, "major_keyword": "计算机"},
         followup_turns=(0, 1, 2)),
    Case("multi-02", "multi_turn", ["江苏历史类考生，历史政治地理", "位次9000", "想读新闻传播"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "rank": 9000, "major_keyword": "新闻传播"},
         followup_turns=(0, 1)),
    Case("multi-03", "multi_turn", ["我江苏的，想学医学", "物理类，物化生", "位次4000"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "rank": 4000, "major_keyword": "医学"},
         followup_turns=(0, 1)),

    # —— 多轮纠偏（后说的覆盖先说的）——
    Case("fix-01", "correction", ["我是江苏考生，物理类，物化生，600分，想学法学", "抱歉说错了，分数是605"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 605, "major_keyword": "法学"}),
    Case("fix-02", "correction", ["江苏物理类物化生，620分，想学机械，学费不超过一万", "学费上限改成两万吧"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 620, "major_keyword": "机械", "budget_max": 20000}),
    Case("fix-03", "correction", ["江苏物理类物化生 600分 想学电子", "想去北京", "还是改成去上海吧"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 600, "major_keyword": "电子", "city_pref": "上海"}),
    Case("fix-04", "correction", ["江苏历史类 史政地 590分 想学汉语言文学", "可以考虑中外合作，专项计划不考虑"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "score": 590, "major_keyword": "汉语言文学",
          "accept_sino_foreign": True, "accept_special_program": False}),

    # —— 较难：否定、户籍/考区、混合表达、推断 ——
    Case("hard-01", "hard", ["我不想学计算机，想学法学"], {"major_keyword": "法学"}),
    Case("hard-02", "hard", ["我在江苏高考，不过户籍是山东"], {"province": "江苏"}),
    Case("hard-03", "hard", ["我选的是物理，再选化学和政治，江苏物理类，610分，想学电气"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "政治"], "score": 610, "major_keyword": "电气"}),
    Case("hard-04", "hard", ["江苏的，史政地，位次一万二，想读新闻"],  # 类别由 史政地 推断为历史类，属合理推断
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "rank": 12000, "major_keyword": "新闻"}),
    Case("hard-05", "hard", ["学费别太贵，一年最多一万二"], {"budget_max": 12000}),

    # —— 手写补充：口语数字、否定、局部信息、组合偏好 ——
    Case("hand-01", "hard", ["我不想学计算机，想学电子"], {"major_keyword": "电子"}),
    Case("hand-02", "hard", ["不要医学类的，想学法学"], {"major_keyword": "法学"}),
    Case("hand-03", "hard", ["我650分，位次大概3000名上下"], {"score": 650, "rank": 3000}, followup_turns=(0,)),
    Case("hand-04", "hard", ["估分600左右"], {"score": 600}, followup_turns=(0,)),
    Case("hand-05", "hard", ["我是广东考生"], {"province": "广东"}, followup_turns=(0,)),
    Case("hand-06", "hard", ["选了历史和政治，还有地理，江苏的"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"]}, followup_turns=(0,)),
    Case("hand-07", "hard", ["我考了六百一十分"], {"score": 610}, followup_turns=(0,)),
    Case("hand-08", "hard", ["位次是一万五"], {"rank": 15000}, followup_turns=(0,)),
    Case("hand-09", "hard", ["我是2026届江苏考生，物理类，物化生，615分，想学计算机"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 615, "major_keyword": "计算机"}),
    Case("hand-10", "hard", ["江苏物理类物化生，位次5000，想学计算机，但是学费别超过一万，也不想去外省"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "rank": 5000, "major_keyword": "计算机",
          "budget_max": 10000, "home_province_only": True}),
    Case("hand-11", "hard", ["总分598，江苏历史类史政地，想学法学，上海优先"],
         {"province": "江苏", "track": "历史类", "subjects": ["历史", "政治", "地理"], "score": 598, "major_keyword": "法学", "city_pref": "上海"}),
    Case("hand-12", "hard", ["专业还没想好"], {}, followup_turns=(0,)),
    Case("hand-13", "hard", ["专业无所谓，看分数能上什么"], {}, followup_turns=(0,)),
    Case("hand-14", "hard", ["我是河北考生，位次20000"], {"province": "河北", "rank": 20000}, followup_turns=(0,)),
    Case("hand-15", "hard", ["我是辽宁的，物理类，选的物化生"],
         {"province": "辽宁", "track": "物理类", "subjects": ["物理", "化学", "生物"]}, followup_turns=(0,)),
    Case("hand-16", "hard", ["我选物理化学生物"], {"subjects": ["物理", "化学", "生物"], "track": "物理类"},  # 类别由首选物理推断，与 hard-04 口径一致
         followup_turns=(0,)),
    Case("hand-17", "hard", ["我是历史类考生"], {"track": "历史类"}, followup_turns=(0,)),
    Case("hand-18", "hard", ["我是物理类的"], {"track": "物理类"}, followup_turns=(0,)),
    Case("hand-19", "hard", ["分数580，江苏，历史类"], {"score": 580, "province": "江苏", "track": "历史类"}, followup_turns=(0,)),
    Case("hand-20", "hard", ["中外合作和专项计划我都不考虑"], {"accept_sino_foreign": False, "accept_special_program": False}),
    Case("hand-21", "hard", ["专项计划可以，中外合办不行"], {"accept_sino_foreign": False, "accept_special_program": True}),
    Case("hand-22", "hard", ["钱不是问题，学费不限"], {}),
    Case("hand-23", "hard", ["我对南京和杭州都有兴趣，优先南京"], {"city_pref": "南京"}),
    Case("hand-24", "hard", ["江苏物理类，选物化生，610分，想学计算机，如果不行也可以接受电子信息"],
         {"province": "江苏", "track": "物理类", "subjects": ["物理", "化学", "生物"], "score": 610, "major_keyword": "计算机"}),
]

from .gen_cases import generate  # noqa: E402  放在最后：生成器需要上面的 Case 类

CASES += generate(Case)

assert len({c.id for c in CASES}) == len(CASES), "用例 id 必须唯一"
