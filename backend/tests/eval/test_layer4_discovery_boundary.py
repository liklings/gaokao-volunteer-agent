"""
Layer 4：信息差发现能力。

需求文档给的指标是"信息差召回率（Discovery Recall）≥ 2所/推荐方案"——
这个具体下限数字目前没有跑真实种子数据摸过底（find_discoveries() 上
一轮刚从"保底也算"收紧到只保留稳妥/冲刺两档，候选池变小了，不同位次段
能召回几所是未知数），所以这里先不断言"一定 >= 2"，而是先钉住两条更
根本、已经在真实用户反馈里出现过的边界行为：

1. 够格 985/211/双一流但档位是"保底"（对考生而言随手就能上）的候选，
   不该出现在"信息差发现"里——这是上一轮真实修的 bug（中央民族大学、
   西北农林科技大学这类，位次差 8000+，被当成"你可能没注意到的好学校"
   推荐给了位次很靠前的考生）。
2. 需求文档"信息差对抗 Case"描述的场景——位次卡在 211 边缘、想学计算机
   又怕滑档——应该真正被"发现"的是稳妥/冲刺档位、地处冷门城市的对口
   985/211/双一流学校（哈工程、石河子大学这类），这里用虚构学校复现
   同样的结构（不依赖真实种子数据里具体有没有这些学校名）。

"≥2所"这条数量下限，等 tests/eval/README.md 里"待确认事项"跑完真实
数据分布摸底后再补上，避免在数据分布还不知道的情况下先编一个可能一开始
就大批量不达标的数字进测试。
"""
from app.services.candidate_curation import find_discoveries
from app.services.rules_engine import search_candidates

from .fixtures import make_group, make_profile, make_school


def test_baowen_gem_excluded_from_discoveries():
    """真实撞到过的问题：位次差大到离谱（对考生而言是"随手就能上"）的
    985/211，即使城市冷门，也不该被推荐成"信息差发现"。
    """
    profile = make_profile(rank=5000)
    groups = [
        # diff=9000（ratio=1.8，远超 baowen 阈值），211，城市冷门
        make_group(
            "obscure-baowen", make_school("s-ob-bao", city="石河子", tags=["211"]),
            min_rank=14000,
        ),
    ]
    results, _audit = search_candidates(groups, profile)
    discoveries = find_discoveries(results)
    assert discoveries == [], "位次差大到「随手就能上」的保底候选不该出现在信息差发现里"


def test_wen_and_chong_gems_in_obscure_city_are_discovered():
    """需求文档"信息差对抗 Case"：位次卡在211边缘、想学计算机又怕滑档时，
    真正该被"发现"的是地处冷门城市、但录取难度落在稳妥/冲刺区间（值得
    认真掂量、不是随手就能上）的对口985/211/双一流学校。
    """
    profile = make_profile(rank=5000, major_keyword="计算机")
    groups = [
        # diff=300（ratio=0.06，稳妥档），211，城市冷门，专业对口
        make_group(
            "obscure-wen", make_school("s-ob-wen", city="秦皇岛", tags=["211"]),
            major_keywords=["计算机"], min_rank=5300,
        ),
        # diff=0（ratio=0，冲刺档），双一流，城市冷门，专业对口
        make_group(
            "obscure-chong", make_school("s-ob-chong", city="咸阳", tags=["双一流"]),
            major_keywords=["计算机"], min_rank=5000,
        ),
        # 对照组：南京（热门城市）同样是211+对口，不该出现在发现里
        make_group(
            "well-known-city", make_school("s-known", city="南京", tags=["211"]),
            major_keywords=["计算机"], min_rank=5300,
        ),
    ]
    results, _audit = search_candidates(groups, profile)
    discoveries = find_discoveries(results)

    discovered_ids = {c.group_id for c in discoveries}
    assert {"obscure-wen", "obscure-chong"} <= discovered_ids, (
        "位次落在稳妥/冲刺区间、地处冷门城市的对口985/211/双一流学校应该被识别成信息差发现"
    )
    assert "well-known-city" not in discovered_ids, "热门城市的学校不算信息差，考生大概率已经知道"
