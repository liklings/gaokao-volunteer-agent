"""
Skill 机制：把分领域的高考志愿政策知识（专项计划、平行志愿、征集志愿、
调剂……）从系统提示词里搬出来，改成"目录常驻、内容按需加载"。

为什么不直接把这些知识写进 agent_prompts.py 的系统提示词里：
1. 这几类政策知识本身内容不短（每个 skill 文档几百字到一千字），如果
   全部塞进系统提示词，不管这一轮用户问不问得到，每次调用 Gemini 都要
   重复付费传这些 token——大部分对话根本不会问到"征集志愿是什么"这种
   问题，绝大多数场次都是纯浪费。
2. 以后要加新的政策领域知识（比如"艺术类校考""军校提前批"），只需要
   新增一个 markdown 文档 + manifest.json 里加一行，不用去改
   agent_prompts.py 里的大段提示词字符串、不用担心改动引入格式问题。

这跟这次会话里 ToolSearch（工具名字始终便宜地列在列表里，完整 schema
按需加载）、以及 MCP 工具 schema 的处理方式，是同一种"目录常驻、内容
按需加载"的设计模式在不同场景下的应用。

catalog（技能目录：id + title + summary）常驻在系统提示词里，模型看到
用户问题涉及某个领域时，自己判断要不要调用 load_skill(skill_id) 工具
把完整内容加载进来，而不是每次都不管用不用得到就整份塞进去。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
_MANIFEST_PATH = _SKILLS_DIR / "manifest.json"


@dataclass(frozen=True)
class SkillInfo:
    id: str
    title: str
    summary: str
    file: str


class SkillNotFoundError(KeyError):
    """请求的 skill_id 在 manifest.json 里不存在。"""


@lru_cache
def list_skills() -> list[SkillInfo]:
    """
    读取 manifest.json 里登记的全部 skill 元信息（不含正文内容）。
    用 lru_cache 缓存——manifest.json 在进程运行期间不会变化（改了要重启
    进程才生效，跟代码改动同一个部署节奏），没必要每次调用都重新读文件、
    重新解析 JSON。

    manifest.json 缺失或格式不对时不吞掉异常、原样抛出——这是部署配置
    错误，应该在启动/首次调用时就暴露出来，而不是悄悄返回一个空目录、
    让"skill 功能好像启用了但其实目录是空的"这种情况被掩盖。
    """
    raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return [SkillInfo(**entry) for entry in raw["skills"]]


def skill_catalog_text() -> str:
    """
    格式化成可以直接拼进系统提示词里的目录文本，常驻在 prompt 里、
    体积很小（只有标题+一句话摘要），不含正文。
    """
    lines = [f"- {s.id}：{s.title}——{s.summary}" for s in list_skills()]
    return "\n".join(lines)


def _skill_by_id(skill_id: str) -> SkillInfo:
    for s in list_skills():
        if s.id == skill_id:
            return s
    raise SkillNotFoundError(skill_id)


def load_skill_content(skill_id: str) -> str:
    """
    按需加载某个 skill 的完整正文内容——只有模型真的调用 load_skill 工具、
    传入某个具体 skill_id 时才读这个文件、把内容喂给模型，不在目录阶段
    就提前读取。

    skill_id 不存在时抛 SkillNotFoundError，调用方（tools.py 里的
    _tool_load_skill）负责转换成 ToolResult(ok=False, ...) 喂回模型，
    模型可以看到"这个 skill_id 不对"，自己决定换一个可用的 id 重试还是
    放弃调用。
    """
    skill = _skill_by_id(skill_id)
    path = _SKILLS_DIR / skill.file
    return path.read_text(encoding="utf-8")
