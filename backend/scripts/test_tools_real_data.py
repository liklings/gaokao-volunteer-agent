"""
工具适配层的真实数据测试——不用脚本化模拟，直接对真实数据库跑一遍，
确认 search_candidates / get_candidate_detail / compare_candidates 这几个工具
包装后返回的数据是对的，跟之前 /api/candidates/search 接口本身的行为一致。
web_search 只测占位返回是否符合预期（因为还没接真实数据源）。

用法（backend/ 目录下，激活 venv）：
    python scripts/test_tools_real_data.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.schemas.candidate import ProfileIn
from app.services.tools import ToolContext, execute_tool


def main() -> int:
    db = SessionLocal()
    profile = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    ctx = ToolContext(db=db, profile=profile)

    print("=== search_candidates（无覆盖参数，用当前profile）===")
    r = execute_tool("search_candidates", {}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["count"] == 10, f"参考考生条件下应有10个候选，实际 {r.data['count']}"
    print(f"count={r.data['count']}，第一条: {r.data['candidates'][0]}")

    print("\n=== search_candidates（覆盖参数：换成不存在的省份）===")
    r = execute_tool("search_candidates", {"province": "上海"}, ctx)
    assert r.ok, f"应该成功（0结果也是成功）: {r.error}"
    assert r.data["count"] == 0, "换成上海应该查不到江苏的候选"
    assert "note" in r.data and r.data["note"], "0结果时应该有提示文案"
    print(f"count={r.data['count']}, note={r.data['note']}")

    print("\n=== get_candidate_detail（真实group_id）===")
    r = execute_tool("get_candidate_detail", {"group_id": "seu-cs-04"}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["university"] == "东南大学"
    print(f"university={r.data['university']}, pct={r.data['pct']}, history条数={len(r.data['history'])}")

    print("\n=== get_candidate_detail（不存在的group_id）===")
    r = execute_tool("get_candidate_detail", {"group_id": "nonexistent-xyz"}, ctx)
    assert not r.ok, "不存在的group_id应该返回失败"
    print(f"预期内失败: {r.error}")

    print("\n=== compare_candidates（两个真实候选）===")
    r = execute_tool("compare_candidates", {"group_ids": ["seu-cs-04", "nuist-cs-03"]}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert len(r.data["comparison"]) == 2
    for row in r.data["comparison"]:
        print(f"  {row['university']}: pct={row['pct']} tuition={row['tuition']}")

    print("\n=== compare_candidates（只传一个，应该失败）===")
    r = execute_tool("compare_candidates", {"group_ids": ["seu-cs-04"]}, ctx)
    assert not r.ok, "只传一个group_id应该报错"
    print(f"预期内失败: {r.error}")

    print("\n=== web_search（占位实现，预期明确的未接入提示）===")
    r = execute_tool("web_search", {"query": "测试"}, ctx)
    assert not r.ok
    assert "尚未接入" in r.error
    print(f"预期内的占位返回: {r.error}")

    print("\n=== 未知工具名 ===")
    r = execute_tool("delete_everything", {}, ctx)
    assert not r.ok
    print(f"预期内失败: {r.error}")

    print("\n=== 工具内部真实抛出异常时，execute_tool 也要兜住不能让调用方崩溃 ===")
    r = execute_tool("compare_candidates", {"group_ids": 12345}, ctx)  # 传错类型，len()会在内部真实抛TypeError
    assert not r.ok
    assert "TypeError" in r.error or "工具执行出错" in r.error
    print(f"预期内失败（未崩溃，异常被正确捕获）: {r.error}")

    db.close()
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
