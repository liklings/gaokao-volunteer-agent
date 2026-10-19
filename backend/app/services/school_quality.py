"""
学校质量参考数据的运行时查询接口。

数据来源：app/data/school_quality_source.json，整理自《全国高等院校信息
汇总》（985/211/双一流标记、学科层次、国内综合排名）+ U.S.News 世界大学
排名，具体怎么整理出来的、覆盖率怎么样，见 scripts/match_school_quality.py
的说明——那个脚本用同一份数据、同一套名字匹配逻辑，跑在真实数据库上
校验过匹配率。这里是把同样的匹配逻辑搬进正式接口，供"好学校优先展示"
这个功能（app/services/candidate_curation.py）在查询时实时使用。

只在模块导入时加载一次、建好索引（做法跟 app/services/geo.py 一致）——
这份参考数据在进程运行期间不会变，没必要每次查询都重新读文件。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "school_quality_source.json"
_BRACKET_RE = re.compile(r"[（(].*?[）)]")


@dataclass(frozen=True)
class SchoolQuality:
    tier_label: str  # "985" / "211" / "双一流" / "普通本科" / "专科"
    tier_score: int  # 5/4/3/2/1，只用于排序，数字越大越"好"
    overall_rank: int | None  # 国内综合排名，越小越靠前，查不到时 None
    usnews_world_rank: int | None


def _tier(rec: dict) -> tuple[str, int]:
    # 985/211/双一流不是互斥的（985 院校几乎必然也是211和双一流），
    # 按"最高档位"取一个标签，不是简单叠加——用户关心的是"这所学校算
    # 哪个档次"，不是把三个标签都堆给他看。
    if rec["is_985"]:
        return "985", 5
    if rec["is_211"]:
        return "211", 4
    if rec["is_shuangyiliu"]:
        return "双一流", 3
    if rec.get("level") == "专科":
        return "专科", 1
    return "普通本科", 2  # level 缺失/其他情况按本科兜底，不能默认成最低档


def _normalize(name: str) -> str:
    """去掉括号里的内容（校区/学院分支说明），只用于精确匹配失败后的
    兜底匹配——跟 scripts/match_school_quality.py 里的规整匹配是同一套逻辑，
    保持两边行为一致（脚本校验过匹配率，这里的运行时查询不能跟脚本不一样）。
    """
    return _BRACKET_RE.sub("", name).strip()


def _build_index() -> tuple[dict[str, SchoolQuality], dict[str, list[SchoolQuality]]]:
    with open(_DATA_FILE, encoding="utf-8") as f:
        records: list[dict] = json.load(f)
    exact: dict[str, SchoolQuality] = {}
    fuzzy: dict[str, list[SchoolQuality]] = {}
    for rec in records:
        label, score = _tier(rec)
        q = SchoolQuality(
            tier_label=label, tier_score=score,
            overall_rank=rec.get("overall_rank"), usnews_world_rank=rec.get("usnews_world_rank"),
        )
        name = rec["name"].strip()
        exact[name] = q
        fuzzy.setdefault(_normalize(name), []).append(q)
    return exact, fuzzy


_EXACT_INDEX, _FUZZY_INDEX = _build_index()


def get_school_quality(school_name: str | None) -> SchoolQuality | None:
    """按学校名查质量参考数据。查不到返回 None——调用方要把 None 当成
    "没有参考数据"，不能悄悄当成"最差"处理（真实数据库里，独立学院、
    分校区这类名字对不上参考表的情况不会太少，见 match_school_quality.py
    实际跑出来的未匹配名单）。

    先精确匹配；查不到再退化成"去掉括号后缀"的规整匹配，且只在规整后
    唯一命中时采用——规整后撞上参考表里不止一条记录（比如"某大学"和
    "某大学(分校)"规整后是同一个 key）就不猜，宁可返回 None 也不要
    张冠李戴把错的质量数据安到某所学校头上。
    """
    if not school_name:
        return None
    name = school_name.strip()
    hit = _EXACT_INDEX.get(name)
    if hit is not None:
        return hit
    candidates = _FUZZY_INDEX.get(_normalize(name))
    if candidates and len(candidates) == 1:
        return candidates[0]
    return None


def _tier_from_tags(tags: list[str]) -> tuple[str, int] | None:
    """从 School.tags（"江苏专家版数据"导入时从"院校标签"/"本科/专科"两列
    拆出来的，见 admission_master_loader.py 的 _parse_school_tags）推出
    档次。tags 为空返回 None（不代表"最低档"，只是没有这个信息源，调用方
    应该退回名字匹配那条兜底路径，不能当成"查到了、只是最低档"）。
    """
    if not tags:
        return None
    if "985" in tags:
        return "985", 5
    if "211" in tags:
        return "211", 4
    if "双一流" in tags:
        return "双一流", 3
    if "专科" in tags:
        return "专科", 1
    if "本科" in tags or "职业本科" in tags:
        return "普通本科", 2
    return None


def get_school_quality_for_school(
    school_name: str | None, tags: list[str] | None, overall_rank: int | None,
) -> SchoolQuality | None:
    """"好学校优先展示"功能（candidate_curation.py）实际调用的入口——优先用
    School.tags / School.overall_rank（"江苏专家版数据"导入时从"院校标签"/
    "本科专科"/"院校排名"这几列直接带出来的，是数据源自带、跟这所学校
    本身绑定的权威信息，不是靠猜的），这两个字段是这次新加的。

    tags 为空（老数据源没有这几列，比如种子数据、admission_data_loader.py
    导入的通用招生计划表）时退回 get_school_quality() 那套"拿学校名去
    匹配《全国高等院校信息汇总》静态表"的兜底路径——那套路径的问题是
    独立学院/分校区这类名字对不上参考表的学校查不到数据，tags 是导入时
    跟这所学校本身一起存的，不存在"匹配不上"这个问题，只要数据源本身
    有这几列就一定有数据，可靠得多，所以优先用它。
    """
    tier = _tier_from_tags(tags or [])
    if tier is not None:
        label, score = tier
        return SchoolQuality(tier_label=label, tier_score=score, overall_rank=overall_rank, usnews_world_rank=None)
    return get_school_quality(school_name)
