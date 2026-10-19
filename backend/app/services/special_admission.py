"""
军事/警务/司法/专项计划/定向培养等"特殊类型招生"识别。

真实撞到的问题：一个江苏考生只想查普通计算机专业，检索结果前几条却全是
郑州"网络空间部队信息工程大学"、西安"空军工程大学"这类军事院校。原因是
这些学校面向普通高考生的名额少、竞争小，历年最低位次反而比普通高校宽松，
纯按参考概率（冲稳保）排序时天然会被顶到最前面，但这类招生除了分数/位次
门槛外，通常还要求政治审查、体检、面试、户籍/性别/身高/视力限制等额外
资格条件，跟"分数够了、平行志愿正常投档"完全不是一回事——用户没有明确
表示要考虑这类院校时，把它们跟普通专业组混在一起、按同样的概率标签推荐，
是严重误导。

两层识别，都会用到，不是"二选一"：

1. admission_category（权威，优先判定异常）：app/services/admission_master_loader.py
   解析"江苏专家版数据"这份文件时，"计划类别"这一列本身就是官方现成的招生
   类型分类——"本科院校"/"专科院校"是正常批次，其余的（"提前录取本科院校
   —军队院校""—公安政法院校(专业)""专科院校-定向培养军士""提前录取本科
   院校—高校专项计划/地方专项计划""—农村订单定向医学生免费培养计划"等）
   全部需要额外资格，默认都算特殊类型。这个字段是数据源里现成的，不是猜的，
   比关键词匹配可靠得多。只要它的值不属于"本科院校"/"专科院校"这两个正常
   批次，直接采信，不用再看关键词。
2. 关键词匹配：**不是**只在 admission_category 为 None 时才用的兜底——真实
   数据跑起来才发现，"计划类别"这一列只区分大批次（本科/专科/提前录取/
   专项计划等），"中外合作办学""马来西亚分校"这类信息很多时候压根不会
   单独占一个"计划类别"，照样标成"本科院校"，实际信息只出现在专业组
   名称的括注里（比如"05专业组(中外合作办学)"）。真实撞到的问题：一个
   admission_category="本科院校"、专业组名称是"(中外合作办学)"的候选，
   之前的逻辑一看 admission_category 有值就直接采信"本科院校"、判定成
   正常批次，根本不会再看专业组名称，学费不菲的中外合办项目就这样混进了
   "保底"档，跟普通专业组一起被推荐给了完全没表态愿意考虑的考生。所以
   现在改成：admission_category 判定为异常批次时优先采信（第1层）；但
   只要它判定为"正常"（或者压根没有这一列），都会**再做一次**关键词
   匹配，两层都要过，不是有了 admission_category 就不看关键词了。覆盖
   不全，但能拦住最常见、最容易造成误导的一类，是跟 geo.py 城市反查表
   同一类"不完整但总比没有强"的静态表。
"""
# admission_category 严格等于这两个值之一才算"正常批次"，其余任何值
# （包括未来数据源里出现的新分类名）一律按特殊类型处理——宁可默认更保守，
# 也不能让考生没主动要求就被塞进一份需要额外资格的名单。
_NORMAL_ADMISSION_CATEGORIES = {"本科院校", "专科院校"}

_SPECIAL_KEYWORDS = (
    "军事", "军官", "陆军", "海军", "空军", "武警", "国防生", "定向培养军士",
    "武装警察", "消防救援", "警察学院", "公安", "司法警官", "监狱", "戒毒",
    "边防", "军队", "国防科技大学",
)

# ---- "多轮槽位填充表单化"：把"特殊类型招生"进一步分成两档 ----
#
# 军事/警务/司法/航海类——始终默认硬性排除，不提供开关（政治审查/体检/
# 面试等门槛跟"分数够了就能报"完全不是一回事，绝大多数考生也确实不会
# 考虑）。中外合作办学/境外分校（真实产品痛点：用户反馈过"系统盲目推荐
# 学费特别高的中外合作学校"，说明很多考生确实会正常考虑这类项目，只是
# 希望自己决定要不要看，而不是被塞或者被完全排除，境外分校跟中外合作
# 办学是同一类考虑——学费更高、就读地点也不是常规校本部，用同一个开关
# accept_sino_foreign 管）和专项计划/定向培养（国家专项/地方专项/高校
# 专项/农村订单定向医学生等，面向特定生源、有户籍/服务期限制，但同样是
# 很多考生会主动考虑的正规报考渠道）——默认也排除，但考生可以通过
# accept_sino_foreign / accept_special_program 这两个"多轮槽位填充"收集
# 到的开关明确表示"愿意考虑"后放行，不是一刀切。
_ALWAYS_EXCLUDED_MARKERS = _SPECIAL_KEYWORDS + ("航海",)
_SINO_FOREIGN_MARKERS = ("中外合作", "分校")
# "分校"匹配得比较宽——像"厦门大学马来西亚分校"这类境外分校，跟中外合作
# 办学是同一类考虑（学费更高、就读环境也跟常规校本部不同），刻意归进同一个
# accept_sino_foreign 开关而不是新开一个开关；代价是可能误伤个别国内的
# 正常"分校"（比如同城的第二校区叫法里偶尔带"分校"二字），两害相权，
# 宁可默认保守排除、让愿意考虑的考生自己在对话里开一句口。
_SPECIAL_PROGRAM_MARKERS = ("专项", "定向")
# 判断顺序很重要："定向培养军士"这个关键词本身就在 _ALWAYS_EXCLUDED_MARKERS
# 里（继承自 _SPECIAL_KEYWORDS），所以下面 resolve_admission_gate() 里
# 先查 _ALWAYS_EXCLUDED_MARKERS 再查 _SPECIAL_PROGRAM_MARKERS，能保证
# "定向培养军士"不会被"定向"这个更宽泛的词误判成"专项计划"这种可以随便
# 开关放行的档次——它本质上是军事性质的定向培养，不是普通专项计划。

# classify_special_admission() 的关键词回退层（第2层）要扫的完整关键词表——
# 注意这个跟下面 detect_special_admission_type()/_detect_by_keyword() 用的
# _SPECIAL_KEYWORDS 不是同一份：后者是保留给旧调用方的、范围更窄的兼容别名
# （只认军事/警务类），前者是 classify_special_admission() 真正在用的，
# 还要覆盖中外合作/境外分校/专项计划/定向培养这些能通过开关放行的类型——
# 两者分开定义，是为了不让 detect_special_admission_type() 这个保留给旧
# 调用方的窄范围函数因为这次改动意外扩大了匹配范围。
_ALL_SPECIAL_MARKERS = _ALWAYS_EXCLUDED_MARKERS + _SINO_FOREIGN_MARKERS + _SPECIAL_PROGRAM_MARKERS


def _detect_by_keyword(school_name: str, group_name: str | None) -> str | None:
    """命中任意关键词就返回该关键词（用于展示原因），没命中返回 None。
    只在学校名称 + 专业组名称里找，不看专业方向关键词——避免"网络空间安全"
    这类正常计算机相关专业方向被"安全"两个字误伤（关键词表里也刻意没放
    "安全"这种过于宽泛、容易大量误伤正常专业组的词）。

    只扫 _SPECIAL_KEYWORDS（军事/警务/司法类）——是保留给
    detect_special_admission_type 这个旧名字兼容别名用的窄范围版本，
    classify_special_admission() 内部走的是 _detect_by_all_markers()，
    覆盖范围更全，不要在这里加中外合作/专项/定向这些新关键词。
    """
    text = f"{school_name} {group_name or ''}"
    for kw in _SPECIAL_KEYWORDS:
        if kw in text:
            return kw
    return None


def _detect_by_all_markers(school_name: str, group_name: str | None) -> str | None:
    """classify_special_admission() 关键词回退层真正用的版本——覆盖
    军事/警务/司法/航海 + 中外合作/境外分校 + 专项计划/定向培养全部标记，
    不止 _SPECIAL_KEYWORDS 那一小份。"""
    text = f"{school_name} {group_name or ''}"
    for kw in _ALL_SPECIAL_MARKERS:
        if kw in text:
            return kw
    return None


def classify_special_admission(
    school_name: str, group_name: str | None, admission_category: str | None = None,
) -> str | None:
    """返回命中的分类/关键词（用于在核验面板展示原因），是正常批次则返回 None。

    admission_category 是异常分类名时直接采信（模块文档第1层，权威、优先）；
    是正常分类名（本科院校/专科院校）或者压根没有这一列时，都会再做一次
    关键词匹配（第2层，覆盖军事/警务/司法/航海/中外合作/境外分校/专项计划/
    定向培养全部标记）——不是"admission_category 有值就不看关键词了"，
    真实数据里"计划类别"经常不会把中外合作/境外分校这类信息单独标出来，
    这类信息更多出现在专业组名称的括注里，必须两层都过一遍才不会漏判。
    """
    if admission_category is not None and admission_category not in _NORMAL_ADMISSION_CATEGORIES:
        return admission_category
    return _detect_by_all_markers(school_name, group_name)


# 保留旧名字兼容之前直接 import detect_special_admission_type 的调用方
# （rules_engine.py 迁移到 classify_special_admission 之后这个别名可以删掉，
# 先保留一轮避免漏改）——范围刻意保持原来的窄范围（只认 _SPECIAL_KEYWORDS），
# 不要因为 classify_special_admission() 这次扩大了关键词覆盖面就跟着改这个。
detect_special_admission_type = _detect_by_keyword


def resolve_admission_gate(
    school_name: str,
    group_name: str | None,
    admission_category: str | None,
    accept_sino_foreign: bool | None,
    accept_special_program: bool | None,
) -> tuple[bool, str | None]:
    """rules_engine.py "招生类型"这条硬规则真正要调用的入口——在
    classify_special_admission() 的基础上，结合 profile 里两个开关，
    判断这条候选到底要不要被排除。

    返回 (passed, reason)：passed=True 时 reason 是 None；passed=False 时
    reason 是命中的分类名/关键词（用于在核验面板/候选详情里说明原因）。

    accept_sino_foreign / accept_special_program 传 None（用户还没被问过、
    或者问了但还没回答）时按"没有明确同意"处理，效果跟传 False 一样——
    默认保守排除，跟这两个字段被加进来之前的行为完全一致，不会因为加了
    开关就意外放宽了默认结果。
    """
    matched = classify_special_admission(school_name, group_name, admission_category)
    if matched is None:
        return True, None
    if any(marker in matched for marker in _ALWAYS_EXCLUDED_MARKERS):
        return False, matched
    if any(marker in matched for marker in _SINO_FOREIGN_MARKERS):
        return (True, None) if accept_sino_foreign else (False, matched)
    if any(marker in matched for marker in _SPECIAL_PROGRAM_MARKERS):
        return (True, None) if accept_special_program else (False, matched)
    # 命中了"特殊类型"，但既不属于始终排除关键词表，也不含"中外合作"/
    # "分校"/"专项"/"定向"字样——数据源里以后可能出现没见过的新分类名，
    # 保守起见按"始终排除"处理，不能因为不认识就默认当成这两个开关能放开
    # 的范围。
    return False, matched
