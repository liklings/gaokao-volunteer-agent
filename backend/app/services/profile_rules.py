"""
考生条件的必填项判断。这是原型 JS 里 requiredMissingList() 的移植——
刻意做成服务器端的确定性判断，而不是信任模型自己汇报"是否收集齐了"：
模型负责理解语义、提取字段，"齐不齐"这种可以用代码精确判断的事，
交给代码判断更可靠。
"""
from app.models import Profile

REQUIRED_LABELS = {
    "province": "省份",
    "track": "类别（物理/历史）",
    "subjects": "选科组合",
    "score_or_rank": "分数或位次",
    "major_keyword": "专业关键词",
}


def compute_missing(profile: Profile) -> list[str]:
    missing = []
    if not profile.province:
        missing.append(REQUIRED_LABELS["province"])
    if not profile.track:
        missing.append(REQUIRED_LABELS["track"])
    if not profile.subjects:
        missing.append(REQUIRED_LABELS["subjects"])
    if not profile.score and not profile.rank:
        missing.append(REQUIRED_LABELS["score_or_rank"])
    if not profile.major_keyword:
        missing.append(REQUIRED_LABELS["major_keyword"])
    return missing
