"""
方案版本对比 + 导出文本生成。

设计上，PlanCandidate.snapshot_json 存了确认那一刻的完整候选信息
（大学名、城市、专业组名称等），版本对比和导出都直接读这份快照，
不用再回头联表查 AdmissionGroup/School——即使以后种子数据更新了，
历史版本看到的还是"当时"的信息，不会跟着最新数据变。
"""
from app.models import Plan, PlanCandidate, Profile


def compute_diff(prev_candidates: list[PlanCandidate], new_candidates: list[PlanCandidate]) -> list[dict]:
    prev_map = {c.group_id: c for c in prev_candidates}
    new_map = {c.group_id: c for c in new_candidates}

    diff: list[dict] = []

    for gid, c in new_map.items():
        name = (c.snapshot_json or {}).get("university", gid)
        if gid not in prev_map:
            label = (c.snapshot_json or {}).get("category_label") or "—"
            diff.append({
                "kind": "add", "group_id": gid, "university": name,
                "detail": f"新增 {name}（{label}）",
            })

    for gid, c in prev_map.items():
        name = (c.snapshot_json or {}).get("university", gid)
        if gid not in new_map:
            diff.append({
                "kind": "remove", "group_id": gid, "university": name,
                "detail": f"剔除 {name}",
            })

    for gid, c in new_map.items():
        # 之前这里比较的是 prob_pct（参考概率百分比），跟这个字段一起去掉了
        # （见 PlanCandidate 模型的说明）——改成比较梯度标签（category_label，
        # 存在 snapshot_json 里）：跟考生位次相关的候选，是否从"稳妥"变成
        # "冲刺"这类档位变化，比一个百分比数字的细微波动更有信息量，也更
        # 贴近用户真正关心的问题（"这个学校现在算稳还是算冲？"）。
        prev_c = prev_map.get(gid)
        if prev_c is None:
            continue
        prev_label = (prev_c.snapshot_json or {}).get("category_label")
        new_label = (c.snapshot_json or {}).get("category_label")
        if prev_label != new_label:
            name = (c.snapshot_json or {}).get("university", gid)
            diff.append({
                "kind": "change", "group_id": gid, "university": name,
                "detail": f"{name} {prev_label or '待计算'} → {new_label or '待计算'}",
            })

    return diff


def build_export_text(profile: Profile, plan: Plan, candidates: list[PlanCandidate]) -> str:
    lines = []
    lines.append("志愿领航 Agent · 志愿方案导出")
    lines.append(f"生成时间：{plan.created_at.strftime('%Y-%m-%d %H:%M')}　版本：v{plan.version_n}")
    lines.append("")
    lines.append("【考生条件】")
    lines.append(f"省份：{profile.province or '—'}　年份：{profile.year or '—'}　类别：{profile.track or '—'}")
    lines.append(f"选科：{'+'.join(profile.subjects) if profile.subjects else '—'}　"
                  f"分数：{profile.score or '—'}　位次：{profile.rank or '—'}")
    lines.append(f"专业关键词：{profile.major_keyword or '—'}　"
                  f"学费上限：{(str(profile.budget_max) + '元/年') if profile.budget_max else '不限'}")
    lines.append("")
    lines.append("【方案候选】")
    for i, c in enumerate(candidates, start=1):
        snap = c.snapshot_json or {}
        name = snap.get("university", c.group_id)
        city = snap.get("city", "")
        code = snap.get("code") or "—"
        tuition = snap.get("tuition")
        label = snap.get("category_label") or "—"
        source_label = snap.get("source_label") or "（来源说明缺失，需补充）"
        rank_diff_txt = f"位次差 {c.rank_diff}" if c.rank_diff is not None else "位次差 —"
        lines.append(f"{i}. {name}（{city}） —— {label}（{rank_diff_txt}）")
        lines.append(f"   招生代码：{code}　学费：{tuition if tuition is not None else '—'}元/年　来源：{source_label}")
    lines.append("")
    lines.append("【免责说明】以上数据基于当前种子数据集生成，正式填报前请以各省教育考试院公布的官方数据为准。")
    return "\n".join(lines)
