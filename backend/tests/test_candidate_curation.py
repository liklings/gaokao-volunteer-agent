"""
candidate_curation.curate() 的测试——用户明确要的效果："只能查看10所学校，
分别是保底的学校，稳的学校，冲刺的学校，其他的都折叠起来"，且"应该只把
好学校放在前面"。这里直接构造 CandidateOut（不经过 evaluate_group/数据库，
curate() 只读 category/diff/school_tier_score/school_overall_rank 这几个
字段），把"分档 + 档内按学校质量排序 + 按比例分配名额 + 其余折叠"这几条
规则分别测清楚。
"""
from app.schemas.candidate import CandidateOut
from app.services.candidate_curation import curate, find_discoveries


def _cand(
    group_id: str, *, category: str | None, diff: int | None = 50,
    tier_score: int = 0, tier: str | None = None, overall_rank: int | None = None,
    major_soft_miss: bool = False, city: str = "测试市", school_id: str | None = None,
) -> CandidateOut:
    return CandidateOut(
        group_id=group_id, school_id=school_id or f"{group_id}-school",
        university=f"{group_id}大学", city=city, province="测试省", tags=[],
        group_name=None, code=None, track="物理类", subjects_required=[], major_keywords=[],
        tuition=None, duration=None, why_recommend="", caution="", source_label="",
        history=[], all_hard_pass=True, rules=[],
        diff=diff, category=category,
        category_label={"baowen": "保底", "wen": "稳妥", "chong": "冲刺", "jichong": "极冲/慎选"}.get(category),
        major_soft_miss=major_soft_miss,
        school_tier=tier, school_tier_score=tier_score, school_overall_rank=overall_rank,
    )


def test_within_category_good_school_ranks_before_high_diff_ordinary_school():
    # 真实撞到的问题场景：一所参考概率更高、但没有质量数据的普通学校（比如
    # 潍坊科技学院这类），跟一所参考概率略低、但是 985 的学校在同一个"稳妥"
    # 档位里——精选结果里应该是好学校排前面，不是继续按 diff 排。
    ordinary = _cand("ordinary", category="wen", diff=74, tier_score=0, tier=None)
    good = _cand("good", category="wen", diff=55, tier_score=5, tier="985", overall_rank=6)
    result = curate([ordinary, good], total=10)
    wen_group = next(g for g in result.groups if g.category == "wen")
    assert [c.group_id for c in wen_group.candidates] == ["good", "ordinary"]


def test_major_matched_candidate_ranks_before_higher_quality_mismatch():
    """真实撞到的问题：档位内部只按学校质量排序时，一个查"计算机"的考生，
    精选出来的名额可能塞满了质量分更高、但专业完全不对口的学校，专业
    真正对口的候选反而被挤进折叠区看不到。专业匹配应该比学校质量更优先。
    """
    mismatched = _cand("nursing", category="wen", diff=60, tier_score=5, tier="985", major_soft_miss=True)
    matched = _cand("cs", category="wen", diff=60, tier_score=0, tier=None, major_soft_miss=False)
    result = curate([mismatched, matched], total=10)
    wen_group = next(g for g in result.groups if g.category == "wen")
    assert [c.group_id for c in wen_group.candidates] == ["cs", "nursing"]


def test_jichong_and_no_diff_candidates_go_straight_to_collapsed():
    baowen = _cand("b1", category="baowen", diff=90)
    jichong = _cand("j1", category="jichong", diff=10)
    no_diff = _cand("n1", category=None, diff=None)
    result = curate([baowen, jichong, no_diff], total=10)
    collapsed_ids = {c.group_id for c in result.collapsed}
    assert collapsed_ids == {"j1", "n1"}
    baowen_group = next(g for g in result.groups if g.category == "baowen")
    assert [c.group_id for c in baowen_group.candidates] == ["b1"]


def test_total_curated_candidates_capped_at_target_total():
    # 保底/稳妥/冲刺三档各构造 20 个候选（远超 target_total=10），精选出来
    # 的候选总数不能超过 10——这是用户明确要的"只能查看10所学校"。
    groups_input = []
    for cat in ("baowen", "wen", "chong"):
        for i in range(20):
            groups_input.append(_cand(f"{cat}_{i}", category=cat, diff=60, tier_score=i % 5))
    result = curate(groups_input, total=10)
    total_curated = sum(len(g.candidates) for g in result.groups)
    assert total_curated == 10
    assert result.collapsed_count == 60 - 10


def test_every_category_with_candidates_gets_at_least_one_slot():
    # 三档候选数量差距悬殊（保底只有 1 个，稳妥/冲刺各 50 个）——按比例算
    # 保底本来分不到名额，但用户要求"分别展示"，保底这一档不能因为候选少
    # 就在精选结果里完全消失。
    groups_input = [_cand("only_baowen", category="baowen", diff=90, tier_score=3)]
    for i in range(50):
        groups_input.append(_cand(f"wen_{i}", category="wen", diff=60, tier_score=i % 5))
    for i in range(50):
        groups_input.append(_cand(f"chong_{i}", category="chong", diff=30, tier_score=i % 5))
    result = curate(groups_input, total=10)
    baowen_group = next(g for g in result.groups if g.category == "baowen")
    assert len(baowen_group.candidates) == 1
    total_curated = sum(len(g.candidates) for g in result.groups)
    assert total_curated == 10


def test_category_proportional_allocation_roughly_matches_availability():
    # wen 候选数量是 chong 的 4 倍、固定权重（_CATEGORY_TARGET_WEIGHTS）
    # wen 本身也比 chong 高——两个因素都指向 wen 应该分到更多名额（不是平摊）。
    # 注意：现在的分配不是纯按候选数量比例（那样"保底"会系统性挤占"稳妥"的
    # 名额，见 _CATEGORY_TARGET_WEIGHTS 的说明），这条测试只断言"数量多、
    # 权重也高的一档名额应该更多"这个方向性结论，不断言具体数字。
    groups_input = []
    for i in range(40):
        groups_input.append(_cand(f"wen_{i}", category="wen", diff=60, tier_score=1))
    for i in range(10):
        groups_input.append(_cand(f"chong_{i}", category="chong", diff=30, tier_score=1))
    result = curate(groups_input, total=10)
    wen_n = len(next(g for g in result.groups if g.category == "wen").candidates)
    chong_n = len(next(g for g in result.groups if g.category == "chong").candidates)
    assert wen_n > chong_n
    assert wen_n + chong_n == 10


def test_empty_input_returns_empty_groups_and_no_collapsed():
    result = curate([], total=10)
    assert all(len(g.candidates) == 0 for g in result.groups)
    assert result.collapsed == []
    assert result.collapsed_count == 0


def test_fewer_candidates_than_target_total_not_padded():
    # 三档加起来才 3 个候选，精选结果就应该是 3 个，不该硬凑到 10 个
    # （凑不出来的名额就是没有，不能伪造候选）。
    groups_input = [
        _cand("b1", category="baowen", diff=90),
        _cand("w1", category="wen", diff=60),
        _cand("c1", category="chong", diff=30),
    ]
    result = curate(groups_input, total=10)
    total_curated = sum(len(g.candidates) for g in result.groups)
    assert total_curated == 3
    assert result.collapsed == []


# ---- 信息差挖掘（find_discoveries） ----
#
# 真实痛点：够格985/211/双一流的学校，只因为不在热门省会/一线城市，
# 考生可能压根没听说过、直接跳过了。这里只测 find_discoveries() 本身的
# 筛选/去重/排序规则，不测"热门城市名单具体收录了哪些城市"这种数据本身
# （名单在 candidate_curation._WELL_KNOWN_CITIES，用一个明显不在名单里的
# 虚构城市名做测试，不依赖名单的真实内容）。

def test_discoveries_only_include_985_211_shuangyiliu_tier():
    # tier_score < 3（普通本科/专科）不算"信息差"，哪怕城市再冷门也不该
    # 被当成"你可能没听过的好学校"推荐给用户——它可能真的就是普通学校。
    ordinary = _cand("ord", category="wen", tier_score=2, tier="普通本科", city="冷门市")
    gem = _cand("gem", category="wen", tier_score=4, tier="211", city="冷门市")
    discoveries = find_discoveries([ordinary, gem])
    assert [c.group_id for c in discoveries] == ["gem"]


def test_discoveries_exclude_well_known_cities():
    # 南京是热门省会，哪怕是985也不算"信息差"——考生大概率知道南京有
    # 好大学，不需要系统额外提醒。
    nanjing_985 = _cand("nj", category="wen", tier_score=5, tier="985", city="南京")
    obscure_985 = _cand("qhd", category="wen", tier_score=5, tier="985", city="秦皇岛")
    discoveries = find_discoveries([nanjing_985, obscure_985])
    assert [c.group_id for c in discoveries] == ["qhd"]


def test_discoveries_excludes_candidates_with_missing_city():
    # city 缺失时没法判断是不是热门城市，宁可不推荐也不能瞎猜。
    no_city = _cand("nc", category="wen", tier_score=5, tier="985", city="")
    discoveries = find_discoveries([no_city])
    assert discoveries == []


def test_discoveries_dedup_by_school_keeps_best_ranked_group():
    # 同一所学校两个专业组都够格时，只保留排序更靠前的那一个（这里用
    # diff 更大的那个模拟"更靠前"），不能把同一所学校列两遍占用提醒名额。
    g1 = _cand("s-cs", category="wen", diff=40, tier_score=4, tier="211", city="秦皇岛", school_id="same-school")
    g2 = _cand("s-ee", category="wen", diff=70, tier_score=4, tier="211", city="秦皇岛", school_id="same-school")
    discoveries = find_discoveries([g1, g2])
    assert len(discoveries) == 1
    assert discoveries[0].group_id == "s-ee", "同分档同档次时，diff更大的那个专业组应该被保留"


def test_discoveries_respects_limit():
    candidates = [
        _cand(f"g{i}", category="wen", diff=50, tier_score=4, tier="211", city=f"冷门市{i}", school_id=f"school{i}")
        for i in range(8)
    ]
    discoveries = find_discoveries(candidates, limit=3)
    assert len(discoveries) == 3


def test_curate_includes_discoveries_field():
    gem = _cand("gem", category="wen", diff=60, tier_score=5, tier="985", city="秦皇岛")
    ordinary = _cand("ord", category="wen", diff=80, tier_score=0, city="测试市")
    result = curate([gem, ordinary])
    assert [c.group_id for c in result.discoveries] == ["gem"]


def test_discoveries_excludes_jichong_and_no_chance_candidates():
    """真实撞到的问题：一所 985/211/双一流但录取位次明显够不着（极冲/慎选，
    甚至算不出概率）的学校，只因为城市不热门，一样会被当成"信息差发现"
    推荐给完全没希望的考生——"这几所好学校容易被忽略"这句提示文案暗示的是
    "你其实够得着"，跟"极冲/慎选"的真实含义完全矛盾。"""
    reachable = _cand("reachable", category="chong", diff=30, tier_score=4, tier="211", city="秦皇岛")
    jichong = _cand("jichong", category="jichong", diff=10, tier_score=4, tier="211", city="秦皇岛")
    no_diff = _cand("no_diff", category=None, diff=None, tier_score=4, tier="211", city="秦皇岛")
    discoveries = find_discoveries([reachable, jichong, no_diff])
    assert [c.group_id for c in discoveries] == ["reachable"]


def test_discoveries_excludes_baowen_even_if_985_211():
    """真实撞到的问题（第二次）：位次很靠前的考生，被推荐了位次差 8000+
    这种量级的"保底"学校当"信息差发现"——虽然是985/211，但对这个考生
    而言属于随手就能上、根本不需要"发现"，"你可能没注意到的好学校"这句
    文案暗示的是"这所学校值得你认真考虑"，保底档位次差大到离谱，说明
    这所学校对该考生没有"值得认真考虑"的意义。只有稳妥/冲刺两档的候选
    才应该出现在信息差发现里。"""
    baowen = _cand("baowen", category="baowen", diff=9000, tier_score=4, tier="211", city="秦皇岛")
    wen = _cand("wen", category="wen", diff=40, tier_score=4, tier="211", city="秦皇岛", school_id="other-school")
    discoveries = find_discoveries([baowen, wen])
    assert [c.group_id for c in discoveries] == ["wen"], "保底档的候选不该出现在信息差发现里，哪怕是985/211"


# ---- 名额分配权重（issue：保底档挤占了稳妥档的展示名额） ----


def test_baowen_does_not_crowd_out_wen_when_baowen_has_far_more_candidates():
    """真实撞到的问题：保底档候选数量天然远多于稳妥档（几乎任何位次，全省
    分数线明显更低的学校都算保底，基数本来就大；稳妥要求分数线接近考生
    位次，候选池小得多），之前按候选数量比例分配名额，结果精选展示里
    保底占了大头、稳妥反而没剩几个——但稳妥（大概率能上、又不浪费分数）
    才是最该重点展示的部分。改成固定权重后，即使保底候选数量是稳妥的
    10 倍，稳妥分到的名额也不该被挤到只剩垫底的 1 个。
    """
    groups_input = []
    for i in range(100):
        groups_input.append(_cand(f"b_{i}", category="baowen", diff=90, tier_score=1))
    for i in range(10):
        groups_input.append(_cand(f"w_{i}", category="wen", diff=60, tier_score=1))
    result = curate(groups_input, total=10)
    baowen_n = len(next(g for g in result.groups if g.category == "baowen").candidates)
    wen_n = len(next(g for g in result.groups if g.category == "wen").candidates)
    assert wen_n >= 3, f"稳妥档只分到 {wen_n} 个名额，被候选数量占绝对多数的保底档挤占了"
    assert baowen_n + wen_n == 10


# ---- 城市偏好在精选结果里至少有一所代表（issue：city_pref 完全没生效） ----


def test_curate_ensures_city_pref_represented_when_available():
    """真实撞到的问题：一个明确说了"想去西安"的考生，候选卡片区展示的
    精选名单（curate()）完全按学校质量重排，city_pref 从没被传进来过——
    即使候选池里其实有西安的学校，只要质量分不是最高的几个，精选结果里
    可能一所西安的学校都看不到，用户完全感受不到这个偏好起过任何作用。
    """
    other_city_good = _cand("good_elsewhere", category="wen", diff=60, tier_score=5, tier="985", city="南京")
    xian_ordinary = _cand("xian", category="wen", diff=55, tier_score=1, tier=None, city="西安")
    result = curate([other_city_good, xian_ordinary], total=10, city_pref="西安")
    wen_group = next(g for g in result.groups if g.category == "wen")
    assert any(c.city == "西安" for c in wen_group.candidates), "精选结果里应该至少有一所西安的学校"


def test_curate_without_city_pref_matching_candidate_unaffected():
    # 候选池里压根没有西安的学校时，不该强行伪造一个——精选结果应该
    # 跟不设置 city_pref 时完全一样。
    other_city_good = _cand("good_elsewhere", category="wen", diff=60, tier_score=5, tier="985", city="南京")
    result = curate([other_city_good], total=10, city_pref="西安")
    wen_group = next(g for g in result.groups if g.category == "wen")
    assert [c.group_id for c in wen_group.candidates] == ["good_elsewhere"]


def test_curate_city_pref_already_represented_not_disturbed():
    # 精选结果里已经有匹配城市的候选时，不需要再做替换。
    xian_candidate = _cand("xian_top", category="wen", diff=70, tier_score=5, tier="985", city="西安")
    other = _cand("other", category="wen", diff=60, tier_score=3, city="南京")
    result = curate([xian_candidate, other], total=10, city_pref="西安")
    wen_group = next(g for g in result.groups if g.category == "wen")
    assert [c.group_id for c in wen_group.candidates] == ["xian_top", "other"]
