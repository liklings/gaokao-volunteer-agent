"""
Skill 机制测试：manifest 目录读取、目录文本格式化、按需加载正文、
以及包装成工具后 (_tool_load_skill) 的行为——特别是"缺参数""未知 skill_id"
这两种失败路径要如实喂回明确的错误信息，而不是笼统报错，方便模型自己
判断要不要换个 skill_id 重试。
"""
from app.services.skills import (
    SkillNotFoundError,
    list_skills,
    load_skill_content,
    skill_catalog_text,
)
from app.services.tools import ToolContext, execute_tool

from .conftest import make_profile_in


def make_ctx(db) -> ToolContext:
    return ToolContext(db=db, profile=make_profile_in())


def test_list_skills_returns_all_manifest_entries():
    skills = list_skills()
    ids = {s.id for s in skills}
    assert ids == {"special_program", "parallel_choice", "collected_volunteer", "adjustment"}
    for s in skills:
        assert s.title, f"{s.id} 缺少 title"
        assert s.summary, f"{s.id} 缺少 summary"


def test_skill_catalog_text_contains_every_skill_id_and_summary():
    catalog = skill_catalog_text()
    for s in list_skills():
        assert s.id in catalog
        assert s.summary in catalog


def test_load_skill_content_returns_real_markdown_for_every_skill():
    """确认 manifest.json 里登记的每个 skill，对应的 markdown 文件都真实
    存在、能被读出来，而且不是空文件——避免"目录里写了但正文文件没建"
    这种配置和实际内容脱节的情况。
    """
    for s in list_skills():
        content = load_skill_content(s.id)
        assert len(content) > 100, f"{s.id} 的正文内容看起来太短，可能是空文件或占位内容"
        assert s.title.split("（")[0] in content or s.title in content, (
            f"{s.id} 的正文里应该能找到跟标题相关的内容"
        )


def test_load_skill_content_unknown_id_raises():
    try:
        load_skill_content("nonexistent_skill_id")
        assert False, "应该抛出 SkillNotFoundError"
    except SkillNotFoundError:
        pass


def test_tool_load_skill_success(db):
    ctx = make_ctx(db)
    r = execute_tool("load_skill", {"skill_id": "parallel_choice"}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["skill_id"] == "parallel_choice"
    assert "平行志愿" in r.data["content"]


def test_tool_load_skill_missing_skill_id(db):
    ctx = make_ctx(db)
    r = execute_tool("load_skill", {}, ctx)
    assert not r.ok
    assert "skill_id" in r.error


def test_tool_load_skill_unknown_id_lists_available(db):
    ctx = make_ctx(db)
    r = execute_tool("load_skill", {"skill_id": "does_not_exist"}, ctx)
    assert not r.ok
    assert "does_not_exist" in r.error
    # 失败信息里应该带上可选值，方便模型自己换一个重试，而不是干等着用户告诉它
    assert "special_program" in r.error
