"""提示词里几条由测评发现的规则必须一直在（防止以后改提示词时被悄悄删掉）。
这只是"规则文字还在"的保险，不能证明模型一定遵守——遵守程度由 tests/eval 下的 Slot F1 / LLM-as-Judge 测。"""
from app.services.agent_client import build_system_prompt
from app.services.agent_prompts import ADVISOR_SYSTEM_PROMPT, RETRIEVAL_SYSTEM_PROMPT


def test_profile_prompt_forbids_asking_year_and_optional_fields():
    p = build_system_prompt()
    assert "不是必填项" in p and "绝对不要追问" in p and "哪一年高考" in p


def test_profile_prompt_limits_questions_per_turn():
    p = build_system_prompt()
    assert "只挑最关键的 2 项问" in p and "合起来算" in p and "不要再追问另一个" in p


def test_retrieval_prompt_bans_probability_words_and_requires_every_discovery():
    p = RETRIEVAL_SYSTEM_PROMPT
    assert "极大概率" in p and "录取概率" in p
    assert "每一所学校都必须单独介绍" in p and "为什么容易被忽略" in p


def test_advisor_prompt_also_bans_probability_words():
    assert "极大概率" in ADVISOR_SYSTEM_PROMPT and "录取概率" in ADVISOR_SYSTEM_PROMPT
