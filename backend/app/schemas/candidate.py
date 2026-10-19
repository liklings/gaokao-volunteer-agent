from pydantic import BaseModel, Field


class ProfileIn(BaseModel):
    """考生条件，对应前端 state.profile。全部字段可选——
    缺字段时后端仍然能返回资格校验结果，只是 diff/pct 算不出来（rank 缺失时）。
    """
    province: str | None = None
    track: str | None = None  # 物理类 / 历史类
    subjects: list[str] = Field(default_factory=list)
    score: int | None = None
    rank: int | None = None
    major_keyword: str | None = None
    city_pref: str | None = None
    budget_max: int | None = None
    home_province_only: bool = False
    # 只看"学校实际物理所在省份 == province"的候选——跟上面 province 那条
    # 硬规则的语义不一样：那条比的是 School.province（这个专业组招哪个省的
    # 考生，可能是外省院校面向本省的招生计划），这条比的是 School.city 反查
    # 出来的实际所在地。默认 False（不过滤）是刻意的：城市反查表
    # （app/services/geo.py）覆盖不了所有城市名，查不到时这条规则按"未知"
    # 处理、不算硬性排除，默认关闭避免用户没主动选就被这张不完整的表悄悄
    # 影响结果。

    # "多轮槽位填充表单化"新增的两个开关，语义是三态（None/True/False），
    # 不是简单的 bool 默认值——None 表示"还没问过/用户还没明确表态"，此时
    # rules_engine.resolve_admission_gate() 按"没有明确同意"处理（效果同
    # False，默认排除，不会因为加了这两个字段就意外放宽默认行为）。跟
    # major_keyword/city_pref/budget_max/home_province_only 一样，一旦
    # 用户在对话里明确表态，就会被 Profile Agent 提取出来、持久化进
    # Profile，后续轮次和聊天工具调用都会记得住，不需要重复问。
    accept_sino_foreign: bool | None = None
    # 是否接受中外合作办学项目——这类项目学费通常显著更高（一年几万到
    # 十几万不等），默认不纳入常规推荐，避免像之前真实撞到的问题那样
    # （用户反馈"明明列出了学费上限，结果却盲目推荐学费特别高的中外合办
    # 学校"）系统在没问过的情况下就把这类候选跟普通候选混在一起推荐。
    accept_special_program: bool | None = None
    # 是否接受专项计划/定向培养类型（国家专项/地方专项/高校专项/农村
    # 订单定向医学生/公费师范生定向等）——这类项目通常有户籍、服务期、
    # 毕业去向等额外限制，是很多考生会主动考虑的正规报考渠道，但同样
    # 不该在没问过的情况下悄悄混进常规推荐。


class RuleResult(BaseModel):
    name: str
    passed: bool
    desc: str


class HistoryYearOut(BaseModel):
    year: int
    group_name: str | None
    plan_count: int | None
    min_score: int | None
    min_rank: int | None
    source_url: str | None
    fetched_at: str | None
    rank_diff: int | None  # 该年 min_rank - 考生 rank，前端表格里"位次差"那一列用


class CandidateOut(BaseModel):
    group_id: str
    school_id: str
    university: str
    city: str
    province: str
    tags: list[str]
    group_name: str | None  # 最新一年的官方专业组名称，用于卡片标题展示
    code: str | None
    track: str
    subjects_required: list[str]
    major_keywords: list[str]
    tuition: int | None
    duration: str | None
    why_recommend: str
    caution: str
    source_label: str
    history: list[HistoryYearOut]

    all_hard_pass: bool
    rules: list[RuleResult]
    diff: int | None  # 最新一年 min_rank - 考生 rank，null 表示缺 rank 算不出来
    # 之前这里还有一个 pct（3-97 的"参考概率"百分比）字段，用户反馈这个
    # 数字看着精确、实际是拍脑袋校准出来的两个参考点硬套一条线性公式算出来
    # 的，容易被误当成有统计学依据的东西——现在冲稳保梯度直接用 diff 占
    # 考生自己 rank 的比例分档（见 rules_engine.prob_category()），不再
    # 经过这个百分比数字这一步，字段整个去掉了，不是只在前端隐藏不显示。
    category: str | None  # baowen / wen / chong / jichong / None
    category_label: str | None  # 保底 / 稳妥 / 冲刺 / 极冲/慎选
    major_soft_miss: bool  # 专业关键词软匹配未命中（不作为硬性排除条件）

    # 学校质量参考数据（来自 app/services/school_quality.py，静态表整理自
    # 《全国高等院校信息汇总》+ U.S.News），用于"好学校优先展示"这个功能——
    # 光按位次差排序，排在最前面的经常是一些数据凑巧、但学校本身很普通的
    # 候选，这几个字段让前端能在同一个梯度档位内再按学校质量排一次、以及
    # 判断要不要折叠。查不到参考数据时 school_tier 是 None、school_tier_score
    # 是 0（当成"最低档"参与排序，但前端要用 school_tier is None 判断展示
    # "暂无质量数据"，不能直接说这是专科）。
    school_tier: str | None  # "985" / "211" / "双一流" / "普通本科" / "专科" / None
    school_tier_score: int  # 0~5，只用于排序，数字越大越"好"
    school_overall_rank: int | None  # 国内综合排名，越小越靠前，查不到时 None

    # "同位次跨年映射"（app/services/rules_engine.py 的 RankScaleContext）
    # 的说明文案：diff 是用考生今年的位次跟历年 min_rank 比较算出来的，
    # 两个年份的报考总人数可能不一样（自然波动+扩招缩招），这个字段如实
    # 说明这次计算有没有用一分一段表数据做跨年换算、换算细节是什么——
    # None 表示考生没填位次、或者该候选压根算不出位次差，不适用这个说明。
    rank_equivalent_note: str | None = None


class CuratedGroupOut(BaseModel):
    """一个冲稳保档位（保底/稳妥/冲刺）里精选出来展示的候选。"""
    category: str  # baowen / wen / chong
    category_label: str  # 保底 / 稳妥 / 冲刺
    candidates: list[CandidateOut]  # 组内已经按"学校质量优先、同质量再按概率"排好序
    total_in_category: int  # 该档位实际通过硬性规则的候选总数（精选截断前）


class CuratedOut(BaseModel):
    """"好学校优先展示"精选结果：只挑一小撮、按冲稳保三档分组展示，
    其余候选（含极冲/慎选档、以及每档里没入选精选名额的）都归进 collapsed，
    前端默认折叠、用户点开才看到，保持首屏简洁。
    """
    groups: list[CuratedGroupOut]  # 固定 3 组，顺序 baowen/wen/chong
    collapsed: list[CandidateOut]  # 折叠区候选（已按 MAX_COLLAPSED_RETURNED 截断）
    collapsed_count: int  # 折叠区真实候选总数（截断前）

    # "信息差挖掘"：候选里够格 985/211/双一流、但学校所在城市不是热门
    # 一线/省会城市的，单独挑出来提示一下——真实痛点是"考生可能压根没
    # 听说过这所学校，因为不在济南只在秦皇岛，直接按冲稳保分组浏览、
    # 或者按参考概率排序，很容易被埋没在一堆更"耳熟"但档次未必更高的
    # 候选里面"。见 candidate_curation.find_discoveries() 的说明。
    # 每所学校最多出现一次（同一所学校可能有好几个专业组都够格，不需要
    # 全部列出来，占用户注意力）。
    discoveries: list[CandidateOut] = Field(default_factory=list)


class SearchResponse(BaseModel):
    results: list[CandidateOut]  # 通过全部硬性规则的候选——已经按参考概率排序、且按
    # MAX_RESULTS_RETURNED 截断，不是数据库里全部匹配的候选（数量见 total_results）
    audit: list[CandidateOut]  # 全部候选（含未通过的），资格校验模块要展示这个——同样
    # 已排序（通过的排前面）并按 MAX_AUDIT_RETURNED 截断（数量见 total_audit）
    total_results: int  # results 截断前，真实通过硬性规则的候选总数
    total_audit: int  # audit 截断前，真实参与过校验的候选总数
    curated: CuratedOut  # "好学校优先展示"精选结果，前端候选卡片区改用这个渲染
