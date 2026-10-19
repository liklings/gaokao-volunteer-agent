"""
school_quality.get_school_quality() 的测试——用真实的
app/data/school_quality_source.json（不 mock，这份数据是静态的、随包一起
分发，跟测试 policy_search.py 用真实 pgvector 不 mock 数据库是同一个考虑：
真正要验证的是"名字能不能查到、查到的档位对不对"，mock 掉数据源就测不出
真实数据里的匹配问题）。
"""
from app.services.school_quality import get_school_quality, get_school_quality_for_school


def test_exact_match_985_school():
    q = get_school_quality("清华大学")
    assert q is not None
    assert q.tier_label == "985"
    assert q.tier_score == 5
    assert q.overall_rank == 1


def test_exact_match_211_but_not_985():
    q = get_school_quality("南京航空航天大学")
    assert q is not None
    assert q.tier_label == "211"
    assert q.tier_score == 4


def test_unknown_school_returns_none():
    # 参考表里不存在的学校名（数据表覆盖不到独立学院/新设院校很正常）——
    # 必须返回 None，不能悄悄当成"最差"处理，调用方要能区分
    # "没有质量数据"和"真的是专科"。
    assert get_school_quality("这是一个完全编造的学校名字ABCXYZ") is None


def test_empty_or_none_name_returns_none():
    assert get_school_quality(None) is None
    assert get_school_quality("") is None


def test_fuzzy_match_strips_bracket_suffix():
    # 参考表里存的是不带括号的"南京大学"，DB 里如果存的是
    # "南京大学(仙林校区)"这种带校区说明的名字，精确匹配会失败，
    # 应该退化成"去掉括号"的规整匹配、查到跟"南京大学"一样的质量数据。
    exact = get_school_quality("南京大学")
    fuzzy = get_school_quality("南京大学(仙林校区)")
    assert exact is not None
    assert fuzzy is not None
    assert fuzzy.tier_label == exact.tier_label
    assert fuzzy.overall_rank == exact.overall_rank


def test_get_school_quality_for_school_prefers_tags_over_name_matching():
    """tags 有数据时（"江苏专家版数据"导入时从"院校标签"/"本科专科"两列
    带出来的，见 admission_master_loader.py），应该直接从 tags 推断档次，
    不走名字匹配那条兜底路径——即使学校名跟静态参考表里的名字完全对不上
    （独立学院、新设院校这类），也应该有数据。
    """
    q = get_school_quality_for_school("这是一个完全编造的学校名字ABCXYZ", ["985", "国重点", "本科"], 3)
    assert q is not None
    assert q.tier_label == "985"
    assert q.tier_score == 5
    assert q.overall_rank == 3


def test_get_school_quality_for_school_falls_back_to_name_matching_when_tags_empty():
    """tags 为空（老数据源没有这几列，比如种子数据）时退回名字匹配兜底，
    不能因为换了新字段就让种子数据的候选全部失去质量参考。"""
    q = get_school_quality_for_school("清华大学", [], None)
    assert q is not None
    assert q.tier_label == "985"


def test_get_school_quality_for_school_vocational_tag_maps_to_专科():
    q = get_school_quality_for_school("某职业学院", ["专科"], None)
    assert q is not None
    assert q.tier_label == "专科"
    assert q.tier_score == 1
