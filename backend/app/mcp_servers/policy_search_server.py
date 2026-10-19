"""
参考 MCP server：一个自带的、离线的 web_search 实现，作为 web_search 工具
默认接入的数据源，替换掉 tools.py 里之前"尚未接入真实数据源"的占位实现。

为什么默认是离线的、不是直接接一个真实的联网搜索 API：
1. 这是这次会话里已经验证过的、真实存在的约束——当前开发/测试沙箱的网络
   只放行包管理源（pypi.org 等），一般搜索引擎/API（DuckDuckGo、必应
   Search API 等）全部不通，就算写了真实联网搜索的接入代码，在这个环境
   里也没法测试、没法验证是否正确，等于是没验证过的代码。
2. 项目里此前已经踩过一次"真实 Gemini google_search grounding 免费层
   额度是0"的坑（见 orchestrator.py 里的历史说明），联网搜索类功能本身
   就有"额度/网络是否可用"的不确定性，不适合直接作为唯一实现、没有兜底。
3. 更重要的是架构上的意义：MCP 的价值就在于"client 端代码不用关心 server
   到底是本地进程还是远程服务、离线数据还是真实联网"——只要新 server 遵守
   同样的 MCP 协议、暴露同名的 web_search 工具，替换只需要改
   backend/mcp_servers.json 这一份配置文件（换 command/args/env，比如换成
   一个调用真实搜索 API 的 npx 包），app/services/mcp_client.py 和
   app/services/tools.py 完全不用改一行代码。这个参考实现同时也是那份
   "协议契约"的具体示例：真实 server 要接进来，工具名叫 web_search、
   入参是 {"query": str, "recency": str}、返回结构是下面这样，照着实现
   即可。

数据集本身覆盖几类考生常问、但数据库种子数据（历年录取分数线等结构化数据）
不包含的政策性/趋势性问题——真实部署换上联网搜索 server 后，这份数据集
不再被使用，可以整个删除或保留作为联网搜索失败时的兜底（当前实现没有做
兜底降级，这是明确的已知范围之外，见 README 加固清单）。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("policy-search")

# 每条：id、标题、正文、标签（用于关键词匹配）、来源说明。
# 内容是概括性的政策常识，不是某个具体年份/省份的权威数据——工具返回结果
# 里明确带上这条免责说明，避免模型把这当成跟 search_candidates 一样权威
# 的结构化数据库结果来引用。
_DATASET: list[dict] = [
    {
        "id": "enrollment_plan_change",
        "title": "招生计划为什么会逐年变化",
        "content": (
            "高校每年的招生计划（分省分专业名额）由学校根据办学条件、"
            "学科建设、就业情况等因素逐年调整并报教育主管部门审核，"
            "不是固定不变的。同一所学校同一个专业组，今年和去年的招生"
            "名额可能有明显差异，进而影响当年的实际录取分数线和位次——"
            "这也是为什么参考往年录取数据做预测本身存在不确定性，"
            "名额增加通常会拉低当年的录取分数线，名额减少则相反。"
        ),
        "tags": ["招生计划", "名额", "计划变化", "扩招", "缩招"],
    },
    {
        "id": "new_major_first_year",
        "title": "新增专业/专业组第一年招生要注意什么",
        "content": (
            "高校新设立的专业或者当年新组建的专业组，因为没有历年录取"
            "数据可以参考，报考热度和最终分数线的不确定性比成熟专业组"
            "更高：可能因为'新'而被追捧导致分数偏高，也可能因为考生"
            "和家长不了解、报考谨慎导致分数偏低甚至没招满。填报时如果"
            "候选列表里出现这类新专业组，应该额外提醒考生这一点不确定性，"
            "不能简单套用同校其他老牌专业的分数线做参考。"
        ),
        "tags": ["新专业", "新专业组", "第一年招生", "没有历年数据"],
    },
    {
        "id": "major_vs_school_tradeoff",
        "title": "'冲学校'还是'冲专业'的常见权衡思路",
        "content": (
            "'冲名校、稳专业'（志愿梯度里靠前的位置选层次更高但可能被"
            "调剂到冷门专业的学校）和'冲专业、稳学校'（优先保证专业"
            "对口，学校层次适当放宽）是两种常见但没有统一答案的策略，"
            "选哪种取决于考生对专业的执念程度、未来是否考虑跨专业"
            "考研/深造、目标行业对第一学历院校层次的看重程度等因素，"
            "不存在'哪种策略更好'的普遍结论，回答时应该呈现权衡本身，"
            "结合考生已经表达过的偏好（比如 major_keyword 是否明确、"
            "是否强调不接受调剂）辅助判断，而不是替考生下定论。"
        ),
        "tags": ["冲学校", "冲专业", "选学校还是选专业", "策略"],
    },
    {
        "id": "employment_trend_caveat",
        "title": "参考'就业前景'类信息时的注意事项",
        "content": (
            "专业的中长期就业前景受行业周期、技术变化、区域经济等多种"
            "因素影响，四年后（本科）甚至更长时间后的就业市场情况，"
            "很难在填报当下准确预测；市面上流传的'热门专业''天坑专业'"
            "之类的笼统说法往往滞后于真实变化，或者以偏概全。回答涉及"
            "就业前景的问题时应该谨慎给出确定性很强的断言，建议考生"
            "结合自身兴趣、该专业的核心课程设置、学校在该专业方向的"
            "真实资源投入（师资、实验室、校企合作）综合判断，而不是"
            "单纯依据某个专业标签是'热门'还是'冷门'。"
        ),
        "tags": ["就业前景", "热门专业", "天坑专业", "行业趋势"],
    },
]


def _score(entry: dict, query: str) -> int:
    q = query.strip().lower()
    if not q:
        return 0
    score = 0
    if q in entry["title"].lower():
        score += 3
    if q in entry["content"].lower():
        score += 1
    for tag in entry["tags"]:
        if q in tag.lower() or tag.lower() in q:
            score += 2
    return score


@mcp.tool()
def web_search(query: str, recency: str = "any") -> dict:
    """联网搜索本地数据库没有覆盖的信息：近期招生政策变化、报考趋势、就业去向。

    这是参考实现（离线、内置数据集），不是真实联网搜索——返回结果里
    source 字段会明确标注这一点，调用方（Agent）应该把结果当作"背景常识"
    而不是权威的最新数据来使用。recency 参数当前的离线数据集不区分时间，
    保留这个参数只是为了跟真实联网搜索 server 的接口保持一致，方便以后
    替换成真实 server 时 client 端调用代码不用改。
    """
    scored = [(e, _score(e, query)) for e in _DATASET]
    matched = sorted((item for item in scored if item[1] > 0), key=lambda item: -item[1])

    if not matched:
        return {
            "query": query,
            "recency": recency,
            "results": [],
            "note": "离线参考数据集里没有匹配到相关内容，不代表现实中没有相关信息，"
                    "只是这个内置数据集覆盖范围有限。",
            "source": "policy_search_server（离线参考实现，非真实联网搜索）",
        }

    results = [
        {"title": e["title"], "content": e["content"]}
        for e, _score_value in matched[:3]
    ]
    return {
        "query": query,
        "recency": recency,
        "results": results,
        "source": "policy_search_server（离线参考实现，非真实联网搜索，"
                   "内容为概括性政策常识，不是某个具体年份/省份的权威数据，"
                   "涉及具体政策请以官方发布为准）",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
