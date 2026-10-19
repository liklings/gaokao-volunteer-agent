"""
规则引擎：资格校验（硬性规则通过/不通过）+ 冲稳保梯度分档。

真实撞到的问题：之前冲稳保是靠一个"参考概率"百分比数字算出来的（拿位次差
diff 代入一条线性公式换算成 3%~97% 的百分比，再按固定百分比区间分档）。
用户反馈这个百分比本身没有意义——诊断报告平台/公开数据都算不出真正的
录取概率，这个数字看着精确，实际是拍脑袋校准出来的两个参考点硬套出的
线性公式，容易被考生误当成有统计学依据的东西，反而造成误导。改成直接
按"位次差占考生自己位次的比例"分档（不再有中间的百分比数字这一步），
不是把百分比隐藏起来不显示，是从计算逻辑里彻底去掉。

分档标准见 prob_category()：用位次差（某年该专业组最低录取位次 − 考生
位次，正数说明考生比那年最后一名靠前）除以考生自己的位次，得到一个
"位次差占比"——之所以不直接用位次差的绝对值分档，是因为同样是"位次差
1000"，对位次500的考生（差不多是2倍）和位次5万的考生（几乎可以忽略）
代表的把握程度完全不同，按绝对值定死档位对位次靠前的考生会明显偏乐观、
对位次靠后的考生会明显偏悲观，按比例分档才能在不同位次段之间保持
一致的松紧尺度。
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import AdmissionGroup
from app.schemas.candidate import CandidateOut, HistoryYearOut, ProfileIn, RuleResult
from app.services.geo import resolve_city_province
from app.services.rank_lookup import get_total_candidates_by_year
from app.services.school_quality import get_school_quality_for_school
from app.services.special_admission import resolve_admission_gate

# 位次差占考生自己位次的比例分档阈值——比如 BAOWEN_RATIO=0.20 表示"该专业组
# 最低录取位次比考生位次靠后至少20%"才算保底。数值是产品侧给的初始经验值，
# 不是从统计模型反推出来的，后续可以根据实际使用反馈再调整，但含义是清楚的
# "比例阈值"，不再是一个假装精确的百分比概率。
BAOWEN_RATIO = 0.20   # 位次差占比 >= 20% → 保底
WEN_RATIO = 0.05      # 5% <= 位次差占比 < 20% → 稳妥
CHONG_RATIO = -0.10   # -10% <= 位次差占比 < 5% → 冲刺；< -10% → 极冲/慎选


@dataclass(frozen=True)
class RankScaleContext:
    """"同位次跨年映射"要用的数据：直接拿考生今年的位次去跟往年专业组的
    min_rank 比大小，隐含假设"每年全省考生总数差不多"——真实情况是各省
    每年报考人数会有几千到几万人的自然波动，扩招/缩招也会让"同样是第
    5000名"在不同年份代表的相对水平不一样。这里用一分一段表里每年的
    总人数（近似值，见 get_total_candidates_by_year）算一个缩放比例，
    把"去年的第 N 名"折算成"如果放到今年的报考规模下，大概相当于第几名"，
    再拿这个折算后的位次去跟考生自己的位次比，比直接比原始位次准确。

    current_year 是"考生自己的位次对应哪一年"——取这个省份+科类下，
    一分一段表里最新的那一年（默认就是考生刚考完、系统里最新导入的
    这一年的一分一段表，考生自己的位次本来就是从这张表算出来的）。
    数据没导入齐时，缺哪年就用不了哪年的换算——这里刻意不做"缺了就
    随便估一个"，宁可某一年退化回"不做换算，直接比原始位次"（更保守，
    不会比真实情况更乐观），也不能凭空编一个总人数。
    """
    current_year: int | None
    totals_by_year: dict[int, int]

    def equivalent_rank(self, rank: int, target_year: int) -> tuple[int, bool]:
        """把 rank（考生今年的位次）折算成"如果发生在 target_year 那年的
        报考规模下，大概相当于第几名"，跟 target_year 那一年专业组的
        min_rank 是同一个"尺度"，才能直接相减比大小。返回
        (折算后的位次, 是否真的做了折算)——第二个值是 False 时，
        说明换算所需的一分一段表数据不全，用的还是原始 rank，调用方
        应该据此如实告知用户这次没能做跨年换算，而不是假装做了。
        """
        if self.current_year is None or target_year == self.current_year:
            return rank, False
        total_target = self.totals_by_year.get(target_year)
        total_current = self.totals_by_year.get(self.current_year)
        if not total_target or not total_current:
            return rank, False
        return round(rank * total_target / total_current), True


_NO_RANK_SCALE = RankScaleContext(current_year=None, totals_by_year={})


def build_rank_scale_context(db: Session, province: str | None, track: str | None) -> RankScaleContext:
    """一次查询把这个省份+科类下所有年份的一分一段表总人数都取出来，
    调用方（search_candidates）应该只在一次检索里调用一次、缓存下来
    给每个候选复用，不要每个候选各自查一遍——见
    get_total_candidates_by_year 的说明。province/track 缺失（考生
    档案还没填全）时直接返回"不做换算"的空上下文，不查库。
    """
    if not province or not track:
        return _NO_RANK_SCALE
    totals = get_total_candidates_by_year(db, province, track)
    current_year = max(totals) if totals else None
    return RankScaleContext(current_year=current_year, totals_by_year=totals)


def prob_category(diff: int | None, rank: int | None) -> tuple[str | None, str | None]:
    """返回 (category_key, category_label)。diff 或 rank 为 None 时两者都
    返回 None（对应"尚未填写位次，或者这条候选没有可比的历年数据，算不出
    把握"这种情况，前端应展示"待计算"而不是伪造一个档位）。rank<=0 同样
    返回 None——真实数据里不该出现，但除以 0/负数会算出没有意义的比例，
    宁可保守地"算不出"，也不能算出一个荒谬的档位。

    按"位次差占考生自己位次的比例"分档，不是按位次差的绝对值——同样是
    位次差 1000，对位次 500 的考生和位次 5 万的考生代表的把握程度完全
    不同，按比例分档才能在不同位次段之间保持一致的松紧尺度（阈值见模块
    顶部 BAOWEN_RATIO / WEN_RATIO / CHONG_RATIO 的说明）。
    """
    if diff is None or rank is None or rank <= 0:
        return None, None
    ratio = diff / rank
    if ratio >= BAOWEN_RATIO:
        return "baowen", "保底"
    if ratio >= WEN_RATIO:
        return "wen", "稳妥"
    if ratio >= CHONG_RATIO:
        return "chong", "冲刺"
    return "jichong", "极冲/慎选"


def evaluate_group(
    group: AdmissionGroup, profile: ProfileIn, rank_scale: RankScaleContext | None = None,
) -> CandidateOut:
    rank = profile.rank
    if rank_scale is None:
        rank_scale = _NO_RANK_SCALE
    rules: list[RuleResult] = []

    prov_match = group.school.province == profile.province
    rules.append(RuleResult(
        name="省份匹配", passed=prov_match,
        desc=(f"该校在 {group.school.province} 招生，符合目标省份。" if prov_match
              else f"该校在 {group.school.province} 招生，与目标省份 {profile.province or '—'} 不一致。"),
    ))

    track_match = group.track == profile.track
    rules.append(RuleResult(
        name="科类匹配", passed=track_match,
        desc=(f"科类为 {group.track}，与考生一致。" if track_match
              else f"该专业组要求 {group.track}，与考生科类不一致。"),
    ))

    subj_set = set(profile.subjects or [])
    subj_ok = all(s in subj_set for s in (group.subjects_required or []))
    req_str = "+".join(group.subjects_required or [])
    rules.append(RuleResult(
        name="选科要求", passed=subj_ok,
        desc=(f"要求选考 {req_str}，考生选科满足。" if subj_ok
              else f"要求选考 {req_str}，考生选科（{'+'.join(profile.subjects) or '未填写'}）不满足。"),
    ))

    budget_ok = True
    if profile.budget_max is not None:
        if group.tuition is None:
            # 学费数据本身缺失时不作为硬性排除依据，但明确告知“未知”而不是悄悄放行
            budget_ok = True
            budget_desc = "该专业组学费数据缺失，暂无法核对预算，请人工确认。"
        else:
            budget_ok = group.tuition <= profile.budget_max
            budget_desc = (
                f"学费 {group.tuition} 元/年，未超过预算 {profile.budget_max} 元/年。" if budget_ok
                else f"学费 {group.tuition} 元/年，超出预算 {profile.budget_max} 元/年。"
            )
    else:
        budget_desc = "未设置学费上限，默认不限制。"
    rules.append(RuleResult(name="学费预算", passed=budget_ok, desc=budget_desc))

    major_ok = True
    if profile.major_keyword:
        kw = profile.major_keyword
        major_ok = any(kw in mk or mk in kw for mk in (group.major_keywords or []))
    rules.append(RuleResult(
        name="专业方向", passed=major_ok,
        desc=(f"专业方向包含“{profile.major_keyword}”相关内容。" if (major_ok or not profile.major_keyword)
              else f"专业方向与关键词“{profile.major_keyword}”未直接匹配（已放宽显示）。"),
    ))

    home_prov_ok = True
    # 用 getattr 而不是直接 profile.home_province_only：evaluate_group() 的
    # 调用方不止一处传 ProfileIn（这次新加了这个字段），还有 api/plans.py、
    # recommendation_guard.py 等传的是数据库里的 Profile ORM 模型——这个模型
    # 目前没有这个字段（也不需要持久化它，见下面的说明），直接访问会在这些
    # 调用路径上炸出 AttributeError。真实测试时就是这么发现的：加完这个字段
    # 后 test_auth.py/test_plans_api.py 一堆之前完全无关的测试突然报错。
    if getattr(profile, "home_province_only", False):
        # School.province 存的是"招生目标省份"，不是学校实际所在地——比如
        # 潍坊科技学院物理上在山东，但面向江苏招生时 School.province 会是
        # "江苏"，prov_match 那条硬规则不会拦住它。这里额外用学校实际所在
        # 城市（school.city）反查出它真正属于哪个省，只有用户主动勾选
        # "只看省内院校"时才启用。反查表覆盖不了所有城市名，查不到时按
        # "未知"处理、不作为硬性排除依据（宁可漏过滤，也不能因为一张不完整
        # 的表误伤真实候选）。
        actual_province = resolve_city_province(group.school.city)
        if actual_province is None:
            home_prov_ok = True
            home_prov_desc = f"该校实际所在城市（{group.school.city}）暂无法核实所属省份，未作为过滤依据。"
        else:
            home_prov_ok = actual_province == profile.province
            home_prov_desc = (
                f"该校实际位于{actual_province}（{group.school.city}），符合\"只看省内院校\"。" if home_prov_ok
                else f"该校实际位于{actual_province}（{group.school.city}），不属于目标省份 {profile.province or '—'}，"
                     f"已按\"只看省内院校\"排除。"
            )
        rules.append(RuleResult(name="实际所在地（省内院校）", passed=home_prov_ok, desc=home_prov_desc))

    # 军事/警务/司法/航海类——始终默认硬性排除，不提供开关（政治审查/体检/
    # 面试等门槛跟"分数够了、平行志愿正常投档"完全不是一回事，绝大多数
    # 考生也确实不会考虑）。中外合作办学/专项计划-定向培养——默认也排除，
    # 但考生可以通过 accept_sino_foreign / accept_special_program 这两个
    # "多轮槽位填充"收集到的开关明确表示"愿意考虑"后放行，不是一刀切
    # （真实产品反馈：不少考生确实会正常考虑中外合作项目，只是要自己
    # 决定要不要看，被系统盲目推荐或者被完全排除都不对）。两个开关跟
    # home_province_only 一样用 getattr 读取，兼容调用方传的是 Profile
    # ORM 模型还是 ProfileIn（见上面 home_province_only 那段注释的说明）。
    # 判断逻辑本身在 special_admission.resolve_admission_gate()。
    special_ok, special_reason = resolve_admission_gate(
        group.school.name,
        next((r.group_name for r in group.records if r.group_name), None),
        group.admission_category,
        getattr(profile, "accept_sino_foreign", None),
        getattr(profile, "accept_special_program", None),
    )
    rules.append(RuleResult(
        name="招生类型",
        passed=special_ok,
        desc=(
            "常规批次招生，非军事/警务/司法/中外合作/专项计划等特殊类型。" if special_ok
            else f"疑似军事/警务/司法/航海/中外合作办学/专项计划-定向培养等特殊类型招生"
                 f"（命中“{special_reason}”），通常还需要额外资格或涉及更高学费/户籍限制，"
                 f"默认不纳入常规推荐——如果是中外合作办学或专项计划类型，在对话里明确表示"
                 f"愿意考虑后可以放行；军事/警务/司法/航海类不提供这个开关，已排除。"
        ),
    ))

    all_hard_pass = prov_match and track_match and subj_ok and budget_ok and home_prov_ok and special_ok

    history_sorted = sorted(group.records, key=lambda r: r.year, reverse=True)
    latest = history_sorted[0] if history_sorted else None

    # 算概率用的"最近一年数据"，不能直接取 history_sorted[0]（最新年份的那一条）：
    # 真实数据里最新年份（比如 2026）往往只是当年的招生计划（plan_count 有值，
    # min_rank 是 None，因为高考还没考、没有实际录取结果），如果只看这一条，
    # 哪怕更早的 2025/2024 明明有真实的最低位次数据，也会被判定成"算不出概率"，
    # 页面上就显示"待计算"——这正是真实撞到的 bug：潍坊科技学院这类学校
    # 2026 是纯计划行（— / —），但 2025、2024 都有真实数据，之前的逻辑却因为
    # 只认最新一条而把这些历史数据白白浪费掉了。这里改成找"最近一个真的有
    # min_rank 的年份"来算概率，latest（用于展示 group_name 等字段）仍然
    # 保持"最新年份"不变，两者用途不同不能合并。
    pct_source = next((h for h in history_sorted if h.min_rank is not None), None)

    diff: int | None = None
    rank_equivalent_note: str | None = None
    if rank is not None and pct_source is not None:
        # "同位次跨年映射"：pct_source 通常是去年/前年的数据，直接拿考生
        # 今年的位次跟往年的 min_rank 相减，隐含假设了"每年报考人数差不多"，
        # 但实际每年都会有自然波动、扩招缩招——用一分一段表的总人数比例把
        # 考生今年的位次折算成"放到 pct_source 那一年的报考规模下大概相当于
        # 第几名"，再拿这个折算后的位次去跟当年的 min_rank 比，比直接比
        # 原始位次更准确。数据不全（比如这个省份/科类还没导入一分一段表）
        # 时 equivalent_rank 会原样退回 rank 本身、equivalent_applied=False，
        # 这里如实告知用户没能做这层换算，而不是假装做了。
        effective_rank, equivalent_applied = rank_scale.equivalent_rank(rank, pct_source.year)
        diff = pct_source.min_rank - effective_rank
        if equivalent_applied:
            rank_equivalent_note = (
                f"已用一分一段表数据把您今年的位次 {rank} 折算成 {pct_source.year} 年报考规模下约等于"
                f"第 {effective_rank} 名，再跟该年的实际录取位次比较（不是直接用原始位次比大小）。"
            )
        elif rank_scale.current_year is not None and pct_source.year != rank_scale.current_year:
            rank_equivalent_note = (
                f"未找到 {pct_source.year} 年或 {rank_scale.current_year} 年完整的一分一段表数据，"
                "本条位次差暂按原始位次直接比较计算，未做跨年报考规模换算，仅供参考。"
            )
    category, category_label = prob_category(diff, rank)

    history_out = []
    for h in history_sorted:
        rd = (h.min_rank - rank) if (rank is not None and h.min_rank is not None) else None
        history_out.append(HistoryYearOut(
            year=h.year, group_name=h.group_name, plan_count=h.plan_count,
            min_score=h.min_score, min_rank=h.min_rank,
            source_url=h.source_url,
            fetched_at=h.fetched_at.isoformat() if h.fetched_at else None,
            rank_diff=rd,
        ))

    # "好学校优先展示"功能要用的学校质量参考数据（见 school_quality.py）。
    # 查不到时 tier 是 None、score 按 0 处理——0 天然比任何真实档位
    # （专科=1 到 985=5）都低，quality 排序时会自然沉到"有数据的候选"
    # 后面，但前端要用 school_tier is None 区分"暂无质量数据"和"真的是专科"。
    quality = get_school_quality_for_school(group.school.name, group.school.tags, group.school.overall_rank)
    school_tier = quality.tier_label if quality else None
    school_tier_score = quality.tier_score if quality else 0
    school_overall_rank = quality.overall_rank if quality else None

    return CandidateOut(
        group_id=group.id, school_id=group.school_id,
        university=group.school.name, city=group.school.city, province=group.school.province,
        tags=group.school.tags or [],
        group_name=latest.group_name if latest else None,
        code=group.code, track=group.track,
        subjects_required=group.subjects_required or [],
        major_keywords=group.major_keywords or [],
        tuition=group.tuition, duration=group.duration,
        why_recommend=group.why_recommend, caution=group.caution, source_label=group.source_label,
        history=history_out,
        all_hard_pass=all_hard_pass, rules=rules,
        diff=diff, category=category, category_label=category_label,
        major_soft_miss=not major_ok,
        school_tier=school_tier, school_tier_score=school_tier_score,
        school_overall_rank=school_overall_rank,
        rank_equivalent_note=rank_equivalent_note,
    )


def _results_sort_key(c: CandidateOut, city_pref: str | None):
    """results（通过硬性规则的候选）排序键：
    0. 专业方向是否匹配 major_keyword（major_soft_miss=False 排前面）——
       真实撞到的可用性问题：major_keyword 是软匹配、不参与硬性排除（见
       evaluate_group 里的说明，关键词覆盖不全，不能直接把没匹配上的
       候选排除掉），但完全不参与排序的话，一个查"计算机"的考生，排在
       最前面的候选完全可能是护理学、中医学这类跟"计算机"毫不相关的
       专业组——只是因为它们位次差恰好更大。用户真正想要的体验是
       "专业对口的优先看到，专业不对口的靠后但仍然可见（万一是关键词
       没覆盖到的相关专业）"，所以专业匹配放在位次差之前，是比位次差
       更优先的排序维度，不是唯一维度。
    1. 位次差从大到小（diff 越大说明该专业组历年最低录取位次比考生位次
       靠后越多，把握越大，越接近"保底"）——这是真实撞到过的可用性问题：
       接入真实数据后单次查询能匹配上千条候选，之前完全没有排序，前端
       拿到的是数据库返回的任意顺序，候选一多用户根本没法找到自己关心的
       那个学校在哪儿。diff 为 None（没填位次，或者这条候选没有可比的
       历年数据）的候选排在最后，不跟有位次差的候选混在一起比较。
    2. 同一梯度（保底/稳妥/冲刺）内，城市匹配 city_pref 的排前面（city_pref
       这个字段从 Profile Agent 收集阶段就采集了，之前从来没有被真正用上
       过，这里补上）。用 city != city_pref 当排序 key 是刻意写法：
       Python 里 False < True，匹配的候选 key 是 False，天然排到前面。
    """
    has_diff = c.diff is not None
    return (
        c.major_soft_miss,
        0 if has_diff else 1,
        -c.diff if has_diff else 0,
        c.city != city_pref if city_pref else False,
    )


def _audit_sort_key(c: CandidateOut):
    """audit（全部候选，含未通过的）排序键：通过硬性规则的排在前面（这是
    "资格核验"面板真正有信息量的部分——用户翻这个列表主要是想看"我符合
    条件的候选都有哪些、状态如何"，把几千条里大量"未通过"的原始顺序堆在
    最前面会把真正通过的候选埋掉），组内同样按位次差从大到小排序。
    """
    has_diff = c.diff is not None
    return (
        0 if c.all_hard_pass else 1,
        0 if has_diff else 1,
        -c.diff if has_diff else 0,
    )


def search_candidates(
    groups: list[AdmissionGroup], profile: ProfileIn, db: Session | None = None,
    rank_scale: RankScaleContext | None = None,
) -> tuple[list[CandidateOut], list[CandidateOut]]:
    """返回 (results, audit)，均已排序（见 _results_sort_key / _audit_sort_key）：
    results = 通过全部硬性规则的候选（用于候选卡片展示）
    audit = 全部候选，含未通过的（用于资格校验模块，逐条说明原因）

    注意：这个函数本身不做数量截断——它是 tools.py（聊天工具）和
    api/candidates.py（前端直接查询接口）共用的纯业务逻辑，两个调用方
    对"该截断到多少条、要不要截断"的需求不一样（前者已经有自己的
    MAX_CANDIDATES_RETURNED，后者见 api/candidates.py 的 MAX_RESULTS_RETURNED/
    MAX_AUDIT_RETURNED），截断逻辑留给调用方各自处理，这里只保证排序正确。

    db 是可选的——传了才会做"同位次跨年映射"（build_rank_scale_context，
    见该函数说明），不传就退化成原来的行为（直接拿考生位次跟历年 min_rank
    比）。这里只查一次一分一段表（不是每个候选各查一次），同一个 profile
    下的所有候选共用同一份 RankScaleContext。

    rank_scale 是给历史回测用的显式注入口（评测需要"假装现在是 T 年"，不能去查
    数据库里最新年份的一分一段表）；传了就直接用，优先于 db。线上调用方不传。
    """
    if rank_scale is None and db is not None:
        rank_scale = build_rank_scale_context(db, profile.province, profile.track)
    audit = [evaluate_group(g, profile, rank_scale) for g in groups]
    audit.sort(key=_audit_sort_key)
    results = [c for c in audit if c.all_hard_pass]
    results.sort(key=lambda c: _results_sort_key(c, profile.city_pref))
    return results, audit
