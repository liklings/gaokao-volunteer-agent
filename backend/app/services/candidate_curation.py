"""
"好学校优先展示"：把已经"通过硬性规则、按参考概率排好序"的候选
（rules_engine.search_candidates 返回的 results）进一步整理成一份精选
名单，供前端候选卡片区展示。

真实需求（用户原话）："当选择查询候选时，所有符合条件的都会出来，反而
不知道选哪个……用户根本拉不到最底下查看那个学校的概率大" ——上一轮已经
按概率排过序、加过数量上限，但纯按概率排序还是不够：排在最前面的经常是
数据库里凑巧 min_rank 差值很大、但学校本身很普通的专科（比如撞到过的
潍坊科技学院），对大多数考生来说这不是真正想看到的第一屏内容。用户明确
要的是"只看 10 所，分保底/稳妥/冲刺三档展示，档内优先展示好学校，
其余的折叠起来"。

设计要点：
1. 极冲/慎选（category="jichong"）以及没有 diff（category=None，算不出
   概率）的候选，不参与三档精选，直接进折叠区——三档精选本来就是想突出
   "大概率能上、且学校不差"的候选，极冲本身就不该占首屏篇幅。
2. 每个档位内部按"学校质量优先、同质量再按概率"重排（不是继续按概率
   排）——这是这次改动的核心，档位已经保证了"冲稳保"这个维度，档位
   内部就该让"好学校优先"这个维度说了算。
3. 名额分配按各档位实际候选数量的比例分（用户在澄清问题里确认过这个
   方案）：候选多的档位分到的名额也多，但只要某档有候选就至少给 1 个
   名额（不能因为按比例算出来是 0 就让"保底/稳妥/冲刺三档都要展示"这个
   明确要求落空）。
"""
from __future__ import annotations

from app.schemas.candidate import CandidateOut, CuratedGroupOut, CuratedOut

TARGET_TOTAL = 10
CATEGORIES = ("baowen", "wen", "chong")  # 顺序固定：保底 → 稳妥 → 冲刺
CATEGORY_LABELS = {"baowen": "保底", "wen": "稳妥", "chong": "冲刺"}
MAX_COLLAPSED_RETURNED = 200

# 信息差挖掘：候选够格 985/211/双一流（school_tier_score >= 3，见
# school_quality.py 的档位打分）、但学校所在城市不在这份"热门"名单里，
# 就有理由怀疑考生可能压根没听说过这所学校——比如"东北大学秦皇岛分校"
# 在秦皇岛、"西北农林科技大学"在杨凌、"哈尔滨工程大学"虽然是211但很多
# 人一看城市是哈尔滨就下意识觉得"肯定不如南京的普通一本"，实际未必如此。
# 这份名单刻意给得宽松（宁可漏判几个真正冷门的省会，也不要把西安、
# 武汉这类本身就是热门高校聚集地的城市误判成"信息差"，那样会显得系统
# 见识短），覆盖直辖市、省会、计划单列市这一档"大部分人一听就有概念"
# 的城市，城市名不含"市"后缀（跟 School.city 字段里的原始写法保持一致，
# 这份项目里 city 存的都是不带"市"字的简称，比如"南京"不是"南京市"）。
_WELL_KNOWN_CITIES = {
    "北京", "上海", "天津", "重庆",
    "石家庄", "太原", "呼和浩特", "沈阳", "长春", "哈尔滨",
    "南京", "杭州", "合肥", "福州", "南昌", "济南", "郑州",
    "武汉", "长沙", "广州", "南宁", "海口", "成都", "贵阳",
    "昆明", "拉萨", "西安", "兰州", "西宁", "银川", "乌鲁木齐",
    "大连", "青岛", "宁波", "厦门", "深圳",
    "苏州", "无锡", "香港", "澳门", "台北",
}
MAX_DISCOVERIES = 5


def find_discoveries(results: list[CandidateOut], limit: int = MAX_DISCOVERIES) -> list[CandidateOut]:
    """从已经通过硬性规则的候选里挑出"信息差发现"——够格 985/211/双一流、
    但学校所在城市不在热门名单里的候选，按"专业对口优先、学校档次越高
    越靠前、同档次概率越高越靠前"排序。

    每所学校最多出现一次（同一所学校的好几个专业组都够格时，只保留
    排序最靠前的那一个）——这个模块是"提醒考生这所学校本身值得注意"，
    不是"把这所学校的专业组都列一遍"，列重复了反而稀释了提醒的效果。

    city 缺失（空字符串/None）的候选不参与——没有城市数据的情况下没法
    判断"是不是热门城市"，宁可漏判也不能瞎猜。
    """
    # c.category in ("wen","chong") 这一条是真实撞到的第二次问题修的：
    # 上一版只排除了 jichong/None，把 baowen（保底）也当成"信息差发现"
    # 推荐了出去——真实撞到的案例：一个位次很靠前的考生，被推荐了位次差
    # 8000、11000 这种量级的"保底"学校（中央民族大学、西北农林科技大学
    # 这类，虽然是985/211，但对这个考生来说属于随手就能上、根本不用
    # "发现"）。"你可能没注意到的好学校"这句文案的潜台词是"这所学校
    # 本身值得你认真考虑、只是因为不在热门城市容易被跳过"——保底档位
    # 位次差往往大到离谱（考生的位次远超学校历年最低录取线太多），这种
    # 差距本身就说明这所学校对该考生而言没有"值得认真考虑"的意义，
    # 用"发现"这种措辞包装反而显得系统没识别出考生真实水平。只保留
    # 稳妥/冲刺两档——这两档本来就是"这所学校要不要报，值得认真掂量"
    # 的区间，"信息差"提醒放在这个区间里才有实际意义。
    candidates = [
        c for c in results
        if c.school_tier_score >= 3 and c.city and c.city not in _WELL_KNOWN_CITIES
        and c.category in ("wen", "chong")
    ]
    candidates.sort(key=lambda c: (c.major_soft_miss, -c.school_tier_score, -(c.diff or 0)))

    picked: list[CandidateOut] = []
    seen_schools: set[str] = set()
    for c in candidates:
        if c.school_id in seen_schools:
            continue
        seen_schools.add(c.school_id)
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked


def _quality_sort_key(c: CandidateOut):
    # 专业方向匹配 major_keyword 的排最前面（major_soft_miss=False 优先）——
    # 真实撞到的问题：档位内部只按学校质量/概率排序时，一个查"计算机"的
    # 考生，保底档位精选出来的10个名额里可能塞满了护理学、中医学这类跟
    # 目标专业毫不相关的学校，因为它们质量分/概率恰好更高，真正对口的
    # 计算机专业组反而被挤进折叠区看不到——这跟"精选10所"这个功能本身
    # 的初衷（突出用户真正会考虑的候选）矛盾。专业匹配放在质量之前，
    # 保证精选名额优先分给对口专业。
    # 学校质量从高到低（tier_score 越大越好）；质量相同时综合排名越小越好；
    # 没有综合排名数据的（school_overall_rank is None）排在"同档质量里"
    # 靠后一点，但不能跟质量分混在一起比——不然一个查得到 985 标记、但
    # 查不到具体排名数字的学校，会被排到"没有质量数据的普通候选"后面去，
    # 质量分本身已经是更可靠的信号，排名数据只是同质量档位里的次级依据。
    has_rank = c.school_overall_rank is not None
    return (
        c.major_soft_miss,
        -c.school_tier_score,
        0 if has_rank else 1,
        c.school_overall_rank if has_rank else 0,
        -(c.diff or 0),
    )


def _ensure_city_pref_represented(
    picked: list[CandidateOut], ranked: list[CandidateOut], city_pref: str | None,
) -> list[CandidateOut]:
    """真实撞到的问题：城市偏好（city_pref）之前只在"结果扁平列表"里当过
    同概率档位内的排序 tiebreak（见 rules_engine._results_sort_key），
    候选卡片区实际展示的这份精选名单（curate()）完全是按学校质量重排的，
    city_pref 从来没被传进来过——一个明确说了"想去西安"的考生，只要
    质量更高的候选恰好不在西安，精选出来的保底/稳妥/冲刺三档里可能一所
    西安的学校都看不到，用户完全感受不到这个偏好起过任何作用。

    这里不改变"档内优先按学校质量排序"这个既有设计（质量优先仍然是主要
    排序依据，不整体让位给城市），而是做一个更弱、但足够解决问题的保证：
    只要这一档已经选出的名额里，一个都不匹配 city_pref，但该档候选池里
    其实存在匹配 city_pref 的候选（只是质量排序没让它进精选名额），就把
    当前精选名额里质量最低的那一个换成城市匹配候选里质量最好的那一个——
    不额外占用名额，只是把"垫底"的那个换掉，保证用户明确说过的城市偏好
    至少能看到一点效果，而不是被完全忽略。
    """
    if not city_pref or not picked:
        return picked
    if any(c.city == city_pref for c in picked):
        return picked
    city_match = next((c for c in ranked if c.city == city_pref), None)
    if city_match is None:
        return picked
    return picked[:-1] + [city_match]


# 真实撞到的问题：名额分配最初按各档候选数量的比例分（用户在澄清问题里
# 确认过这个方案），但接入真实数据后发现效果不对——"保底"档天然候选数量
# 远远多于"稳妥"（几乎任何一个位次，全省能选的分数线明显低于考生的学校
# 都算"保底"，基数本来就大；"稳妥"要求分数线跟考生位次比较接近，候选池
# 天然小很多），按候选数量比例分配的结果是精选展示里"保底"占了大头、
# "稳妥"反而没剩几个名额——但对一个正在做志愿规划的考生来说，"稳妥"档
# （大概率能上、又不算浪费分数）才是最值得花篇幅展示的部分，"保底"只要
# 给够安全感、不需要占大头。改成固定目标权重（不再跟候选数量挂钩），
# 稳妥档权重最高；某一档实际候选数不够按权重分到的名额时，多出来的名额
# 会自动挪给还有余量的其他档位（见下面 _allocate_quotas 的实现），
# 不会出现"稳妥权重给多了但稳妥候选不够、名额浪费"的情况。
_CATEGORY_TARGET_WEIGHTS = {"baowen": 3, "wen": 4, "chong": 3}


def _allocate_quotas(counts: dict[str, int], total: int) -> dict[str, int]:
    """按 _CATEGORY_TARGET_WEIGHTS 固定权重分配名额（不是按候选数量比例），
    最大余数法保证分配总数精确等于 min(total, 三档候选总数)。每一档只要
    有候选就先给 1 个名额垫底——候选数量占比很小的档位，按权重算下来可能
    分到 0，但用户明确要求"保底/稳妥/冲刺分别展示"，某一档因为候选少就在
    精选结果里完全消失是不能接受的。
    """
    available = {k: v for k, v in counts.items() if v > 0}
    if not available:
        return {k: 0 for k in counts}
    total = min(total, sum(available.values()))

    quotas = {k: min(1, v) for k, v in available.items()}
    remaining = total - sum(quotas.values())
    if remaining < 0:
        # 只有 total 比"有候选的档位数"还小才会发生（TARGET_TOTAL=10 时不会），
        # 兜底：候选数量从多到少依次保留档位，凑够 total 个档位。
        keep = sorted(available, key=lambda k: -available[k])[:total]
        return {k: (1 if k in keep else 0) for k in counts}

    capacity = {k: available[k] - quotas[k] for k in available}  # 每档还能再分多少
    total_capacity = sum(capacity.values())
    if total_capacity > 0 and remaining > 0:
        # 按固定权重分——而不是按候选数量比例分（那样会被候选基数本来就大
        # 的"保底"档系统性挤占名额，见上面 _CATEGORY_TARGET_WEIGHTS 的说明）。
        # 只在还有余量（capacity>0）的档位之间按权重分配，某档 capacity 已经
        # 到顶（候选池本身就小，1个垫底名额已经用完全部候选）不再分给它，
        # 多出来的名额自然会在最大余数法这一步流向其他还有余量的档位。
        weight_total = sum(_CATEGORY_TARGET_WEIGHTS.get(k, 1) for k in available if capacity[k] > 0)
        raw = {
            k: (remaining * _CATEGORY_TARGET_WEIGHTS.get(k, 1) / weight_total if capacity[k] > 0 else 0)
            for k in available
        }
        floor = {k: min(int(raw[k]), capacity[k]) for k in available}
        left = remaining - sum(floor.values())
        # 剩下的名额按小数部分从大到小依次发放（最大余数法），
        # 同时不能超过该档位自己的 capacity 上限。
        order = sorted(available, key=lambda k: -(raw[k] - floor[k]))
        i = 0
        while left > 0 and order:
            k = order[i % len(order)]
            if floor[k] < capacity[k]:
                floor[k] += 1
                left -= 1
            i += 1
            if i > 10 * len(order):
                break  # 理论上不会走到这里，纯防御性兜底避免死循环
        for k in available:
            quotas[k] += floor[k]

    return {k: quotas.get(k, 0) for k in counts}


def curate(results: list[CandidateOut], total: int = TARGET_TOTAL, city_pref: str | None = None) -> CuratedOut:
    """city_pref：考生的城市偏好（Profile 里已经采集的字段，之前只在扁平
    的 results 列表里当过同概率档位内的排序 tiebreak，从没被传进精选逻辑，
    见 _ensure_city_pref_represented 的说明）——传了才会在每个档位里尽量
    保证至少有一所匹配城市的学校，不传（None）时行为跟以前完全一样。
    """
    by_category: dict[str, list[CandidateOut]] = {cat: [] for cat in CATEGORIES}
    others: list[CandidateOut] = []
    for c in results:
        if c.category in by_category:
            by_category[c.category].append(c)
        else:
            others.append(c)  # jichong，或者 category=None（没有 diff，算不出档位）

    for cat in CATEGORIES:
        by_category[cat].sort(key=_quality_sort_key)

    counts = {cat: len(by_category[cat]) for cat in CATEGORIES}
    quotas = _allocate_quotas(counts, total)

    groups: list[CuratedGroupOut] = []
    collapsed: list[CandidateOut] = list(others)
    for cat in CATEGORIES:
        quota = quotas.get(cat, 0)
        ranked = by_category[cat]
        picked = _ensure_city_pref_represented(ranked[:quota], ranked, city_pref)
        picked_ids = {c.group_id for c in picked}
        collapsed.extend(c for c in ranked if c.group_id not in picked_ids)
        groups.append(CuratedGroupOut(
            category=cat, category_label=CATEGORY_LABELS[cat],
            candidates=picked, total_in_category=counts[cat],
        ))

    return CuratedOut(
        groups=groups,
        collapsed=collapsed[:MAX_COLLAPSED_RETURNED],
        collapsed_count=len(collapsed),
        discoveries=find_discoveries(results),
    )
