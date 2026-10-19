"""
Layer 2：硬约束遵从度（Zero Tolerance）。

对应测评指标"硬约束违规率（Constraint Violation Rate）——Pass Line 0%，
绝对红线"。这里每条场景（tests/eval/scenarios.py::LAYER2_SCENARIOS）都是
直接调用 rules_engine.search_candidates() 这个纯函数、不经过任何 LLM，
所以"违规"是可以精确计数的：must_fail 里任何一个 group_id 出现在通过
硬性规则的结果里，就是一次violation，不允许"大部分时候没问题"这种
说法——这也是为什么这一层要放在三段流水线最前面先做：它是后面历史
回测、LLM-as-Judge 能不能信的地基。
"""
import pytest

from app.services.rules_engine import search_candidates

from .scenarios import LAYER2_SCENARIOS


@pytest.mark.parametrize("scenario", LAYER2_SCENARIOS, ids=[s["id"] for s in LAYER2_SCENARIOS])
def test_hard_constraint_zero_tolerance(scenario):
    results, _audit = search_candidates(scenario["groups"], scenario["profile"])
    passed_ids = {c.group_id for c in results}

    violations = scenario["must_fail"] & passed_ids
    assert not violations, (
        f"[{scenario['id']}] 硬约束违规（Pass Line 是 0%，不允许出现）："
        f"{sorted(violations)} 本不该出现在通过硬性规则的结果里。\n"
        f"场景说明：{scenario['description']}"
    )

    missed = scenario["must_pass"] - passed_ids
    assert not missed, (
        f"[{scenario['id']}] 误杀：{sorted(missed)} 本应该通过硬性规则，却被排除了"
        f"（这不是「零容忍」红线本身，但同样是硬约束判定错误，会让考生看不到"
        f"明明合规的候选）。\n场景说明：{scenario['description']}"
    )


def test_violation_rate_summary(capsys):
    """跑一遍全部场景，打印一份「硬约束违规率」汇总——对应需求文档里的量化
    指标本身，不只是逐条 pass/fail。CI 里看不看这个输出不影响上面逐条测试
    的断言结果，纯粹是给人看的汇总报告。
    """
    total = len(LAYER2_SCENARIOS)
    violated = 0
    for scenario in LAYER2_SCENARIOS:
        results, _audit = search_candidates(scenario["groups"], scenario["profile"])
        passed_ids = {c.group_id for c in results}
        if scenario["must_fail"] & passed_ids:
            violated += 1
    rate = violated / total if total else 0.0
    with capsys.disabled():
        print(f"\n[Layer 2] 硬约束违规率：{violated}/{total} = {rate:.1%}（Pass Line: 0%）")
    assert rate == 0.0
