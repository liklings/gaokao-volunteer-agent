"""
Layer 3：冲稳保梯度合理性。

口径已确认：以代码实际的固定权重 baowen:wen:chong = 3:4:3 为准（见文件
末尾"名额分配口径"一节），不再使用需求文档最初写的 冲20/稳60/保20。
这里的断言分三类：

1. 结构自洽：curate() 分出来的每个档位（CuratedGroupOut），组内候选的
   category 是不是真的跟档位名一致——如果哪天 curate()/prob_category()
   改坏了，导致"保底"档里混进了 category="chong" 的候选，这类结构性
   错误不需要等 LLM 判断"合不合理"，直接程序化判定就是错的。
2. 极端分段（需求文档里"高分段 Case"）：位次差占比分档在位次基数很小
   时（比如全省前500名）天然更敏感——绝对差 25 和差 24 就能让同一个
   候选从「稳妥」翻到「冲刺」。这条不是要断言这种敏感性本身是"错的"
   （比例分档相对固定阈值分档已经是权衡后的选择，见 rules_engine.py
   模块文档），而是把这个边界行为显式钉在测试里：往后如果有人调整
   BAOWEN_RATIO/WEN_RATIO/CHONG_RATIO 这几个常量，能立刻看到高分段
   考生的分档结果发生了什么变化，而不是悄悄改了都没人注意。

"""
from app.services.candidate_curation import curate
from app.services.rules_engine import prob_category, search_candidates

from .fixtures import make_group, make_profile, make_school


def test_curated_groups_only_contain_matching_category():
    """结构自洽：baowen 档位里只能有 category="baowen" 的候选，其余同理，
    跨档位混入是纯粹的代码 bug，不存在"看起来还算合理"的灰色地带。
    """
    profile = make_profile(rank=5000)
    groups = [
        # diff=1200 (ratio=0.24>=0.20) -> baowen
        make_group("g-bao", make_school("s-bao", city="南京"), min_rank=6200),
        # diff=300 (ratio=0.06, 5%<=ratio<20%) -> wen
        make_group("g-wen", make_school("s-wen", city="南京"), min_rank=5300),
        # diff=0 (ratio=0, -10%<=ratio<5%) -> chong
        make_group("g-chong", make_school("s-chong", city="南京"), min_rank=5000),
        # diff=-800 (ratio=-0.16 < -10%) -> jichong，不该出现在三档精选里
        make_group("g-jichong", make_school("s-jichong", city="南京"), min_rank=4200),
    ]
    results, _audit = search_candidates(groups, profile)
    curated = curate(results, city_pref=None)

    by_group_category = {c.group_id: c.category for c in results}
    assert by_group_category["g-bao"] == "baowen"
    assert by_group_category["g-wen"] == "wen"
    assert by_group_category["g-chong"] == "chong"
    assert by_group_category["g-jichong"] == "jichong"

    for group_out in curated.groups:
        for cand in group_out.candidates:
            assert cand.category == group_out.category, (
                f"结构性错误：{cand.group_id} 的 category 是 {cand.category!r}，"
                f"却被放进了 {group_out.category!r} 档位里"
            )

    collapsed_ids = {c.group_id for c in curated.collapsed}
    assert "g-jichong" in collapsed_ids, "极冲/慎选候选不该进三档精选，必须落进折叠区"


def test_high_rank_segment_ratio_sensitivity_is_pinned():
    """需求文档「极端分段 Case·高分段（全省前500名）」：位次基数很小时，
    绝对位次差的一点点变化就能跨档位——这里把具体边界钉住，作为回归
    保护（谁改了三个 RATIO 常量，这条测试会直接告诉你高分段考生的分档
    结果变没变，而不是要等真人报 bug）。
    """
    rank = 500
    # ratio = diff / rank，边界是 0.20 / 0.05 / -0.10
    assert prob_category(diff=100, rank=rank) == ("baowen", "保底")  # ratio = 0.20，边界值算保底
    assert prob_category(diff=99, rank=rank) == ("wen", "稳妥")      # 只差1名，跌出保底档
    assert prob_category(diff=25, rank=rank) == ("wen", "稳妥")      # ratio = 0.05，边界值算稳妥
    assert prob_category(diff=24, rank=rank) == ("chong", "冲刺")    # 只差1名，跌出稳妥档
    assert prob_category(diff=-50, rank=rank) == ("chong", "冲刺")   # ratio = -0.10，边界值仍算冲刺
    assert prob_category(diff=-51, rank=rank) == ("jichong", "极冲/慎选")


def test_quota_always_covers_three_categories_when_candidates_exist():
    """只要三档各自都有候选，精选结果就该三档都展示至少1个名额——用户
    明确要求过"保底/稳妥/冲刺三档都要展示"，不能因为按比例算出来是0
    就让某一档整个消失。这里刻意让 baowen 候选数量远多于 wen/chong，
    验证按比例分配名额时不会把候选少的档位挤没。
    """
    profile = make_profile(rank=5000)
    groups = (
        [make_group(f"bao-{i}", make_school(f"s-bao-{i}", city="南京"), min_rank=6200 + i) for i in range(8)]
        + [make_group("only-wen", make_school("s-wen", city="南京"), min_rank=5300)]
        + [make_group("only-chong", make_school("s-chong", city="南京"), min_rank=5000)]
    )
    results, _audit = search_candidates(groups, profile)
    curated = curate(results, city_pref=None)

    quota_by_cat = {g.category: len(g.candidates) for g in curated.groups}
    assert quota_by_cat.get("wen", 0) >= 1, "稳妥档只有1个候选也不该被挤没"
    assert quota_by_cat.get("chong", 0) >= 1, "冲刺档只有1个候选也不该被挤没"
    assert quota_by_cat.get("baowen", 0) >= 1


# ---- 名额分配口径：固定权重 3:4:3（保底:稳妥:冲刺）----
#
# 已确认的口径（见 README"已确认口径"）：Layer 3 的 Pass Line 以代码里实际的
# 固定权重 candidate_curation._CATEGORY_TARGET_WEIGHTS = 3:4:3 为准，衡量的是
# "精选 10 所里三档各占几个名额"，不再使用需求文档最初写的 冲20/稳60/保20。
# 位次差占比 diff/rank 的分档阈值（BAOWEN_RATIO 等）决定候选"属于哪一档"，
# 3:4:3 决定"每档展示几个"，两者互不冲突。

import pytest

from app.services.candidate_curation import TARGET_TOTAL, _CATEGORY_TARGET_WEIGHTS

# rank=5000 时各档的 min_rank：baowen >= 6000, wen 5250~5999, chong 4500~5249
_MIN_RANK_BY_CAT = {"baowen": 6500, "wen": 5400, "chong": 5000}


def _pool(n_bao: int, n_wen: int, n_chong: int):
    groups = []
    for cat, n in (("baowen", n_bao), ("wen", n_wen), ("chong", n_chong)):
        for i in range(n):
            groups.append(make_group(
                f"{cat}-{i}", make_school(f"s-{cat}-{i}", city="南京"),
                min_rank=_MIN_RANK_BY_CAT[cat] + i,
            ))
    return groups


def _quota_of(groups) -> dict[str, int]:
    results, _ = search_candidates(groups, make_profile(rank=5000))
    curated = curate(results)
    return {g.category: len(g.candidates) for g in curated.groups}


def test_fixed_weights_are_3_4_3():
    """口径本身钉住：谁改了权重常量，这条会立刻报警，提醒同步更新 README 口径。"""
    assert _CATEGORY_TARGET_WEIGHTS == {"baowen": 3, "wen": 4, "chong": 3}
    assert TARGET_TOTAL == 10


def test_quota_is_exactly_3_4_3_when_every_pool_is_ample():
    """三档候选池都充足（>=4）时，精选名额必须正好是 3:4:3。"""
    assert _quota_of(_pool(12, 12, 12)) == {"baowen": 3, "wen": 4, "chong": 3}


def test_quota_not_affected_by_pool_size_imbalance():
    """候选池规模悬殊（保底档候选是其余的 10 倍）也不影响 3:4:3——
    这正是固定权重相对按候选数量比例分配的意义（防止保底档挤占稳妥档）。
    """
    assert _quota_of(_pool(40, 5, 5)) == {"baowen": 3, "wen": 4, "chong": 3}


@pytest.mark.parametrize(
    "pool_shape, expected",
    [
        # 冲刺档只有 1 所：它用完自己的 1 个名额，剩余名额按权重流向另外两档，总数仍为 10
        ((12, 12, 1), {"baowen": 4, "wen": 5, "chong": 1}),
        # 稳妥档只有 2 所
        ((12, 2, 12), {"baowen": 4, "wen": 2, "chong": 4}),
    ],
    ids=["short-chong", "short-wen"],
)
def test_quota_redistributes_when_a_pool_is_short(pool_shape, expected):
    quota = _quota_of(_pool(*pool_shape))
    assert quota == expected, f"池子 {pool_shape} 下名额应为 {expected}，实际 {quota}"
    assert sum(quota.values()) == TARGET_TOTAL, "候选总量充足时精选总数必须正好是 10"
