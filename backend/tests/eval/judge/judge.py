"""评审员：构造 prompt、调用 Gemini、校验并解析评分。纯逻辑部分（build_prompt / parse_verdict /
judge_item / aggregate）不依赖网络，可以注入假的 judge_fn 离线测试。

设计要点（避免 LLM 评审常见的坑，写在这里方便以后复查）：
- 评审员只能依据"背景事实"，不许用自己的知识补充事实（否则它会替被评回复圆谎）。
- 每个维度先写理由（引用回复里的具体内容）再给分，温度 0。
- 明确告知"不因回复更长而加分"（长度偏好）。
- 评审员自己是否可信，由 calibration.py 用人工标注样本检验，不达标时这里的分数不能当结论。
- 解析失败/缺维度/分数越界一律记为 error，不会悄悄补默认分。
- 建议评审模型与被评模型不同（环境变量 JUDGE_MODEL），避免自我偏好；默认回落到 gemini_model。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .rubrics import PASS_SCORE, RUBRICS, Criterion

JudgeFn = Callable[[str], Awaitable[str]]  # prompt -> 模型返回的 JSON 文本

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "applicable": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "score": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["key", "applicable", "reason", "score"],
            },
        }
    },
    "required": ["criteria"],
}


@dataclass
class JudgeItem:
    id: str
    kind: str           # rubrics.RUBRICS 的 key
    context: dict       # 背景事实（确定性信息），评审员只能依据这里
    reply: str          # 被评回复
    extra: dict = field(default_factory=dict)  # 不进 prompt 的附加信息（如确定性检查结果）


def _criterion_block(c: Criterion) -> str:
    na = "（本条不适用时可标 applicable=false）" if c.can_be_na else ""
    return (f"### {c.key}｜{c.name}{na}\n问题：{c.question}\n"
            f"1 分：{c.low}\n3 分：{c.mid}\n5 分：{c.high}")


def build_prompt(item: JudgeItem) -> str:
    rubric = RUBRICS[item.kind]
    ctx = json.dumps(item.context, ensure_ascii=False, indent=2)
    keys = "、".join(c.key for c in rubric)
    return f"""你是一名严格、公正的评审员，负责评估「高考志愿规划助手」的一条回复质量。

## 规则
- 只能依据【背景事实】评判，不要用你自己的知识补充或"纠正"事实；背景里没有的信息，回复若断言了就是无据。
- 逐个维度独立打分（1–5 分整数），先写 reason（引用回复里的具体内容，不超过 60 字）再给 score。
- 不要因为回复更长、更热情就加分；也不要因为措辞华丽就加分。
- 不适用的维度标 applicable=false（score 仍填 3，不会被计入）。
- 必须且只能输出这些维度：{keys}。只输出 JSON。

## 背景事实
{ctx}

## 待评回复
{item.reply}

## 评分维度
""" + "\n\n".join(_criterion_block(c) for c in rubric)


@dataclass
class Verdict:
    item_id: str
    kind: str
    scores: dict = field(default_factory=dict)   # key -> {"score", "applicable", "reason"}
    error: str | None = None

    def passed(self, key: str) -> bool | None:
        s = self.scores.get(key)
        if not s or not s["applicable"]:
            return None
        return s["score"] >= PASS_SCORE


def parse_verdict(item: JudgeItem, text: str) -> Verdict:
    v = Verdict(item.id, item.kind)
    try:
        data = json.loads(text)
        rows = data["criteria"]
    except (ValueError, KeyError, TypeError) as exc:
        v.error = f"评审输出不是合法 JSON 或缺 criteria：{exc}"
        return v
    want = [c.key for c in RUBRICS[item.kind]]
    na_ok = {c.key: c.can_be_na for c in RUBRICS[item.kind]}
    got = {}
    for r in rows:
        try:
            k, sc = r["key"], int(r["score"])
        except (KeyError, TypeError, ValueError):
            v.error = f"维度条目格式错误：{r!r}"
            return v
        if not 1 <= sc <= 5:
            v.error = f"分数越界：{k}={sc}"
            return v
        applicable = bool(r.get("applicable", True))
        if not applicable and not na_ok.get(k, False):
            applicable = True  # 该维度不允许 N/A：评审员乱标 N/A 时按适用处理，避免靠 N/A 躲分
        got[k] = {"score": sc, "applicable": applicable, "reason": str(r.get("reason", ""))}
    missing = [k for k in want if k not in got]
    extra = [k for k in got if k not in want]
    if missing or extra:
        v.error = f"维度不匹配：缺 {missing}，多 {extra}"
        return v
    v.scores = got
    return v


async def judge_item(item: JudgeItem, judge_fn: JudgeFn) -> Verdict:
    try:
        text = await judge_fn(build_prompt(item))
    except Exception as exc:  # 评审调用失败要记录，不能悄悄丢掉这一条
        return Verdict(item.id, item.kind, error=f"{type(exc).__name__}: {exc}")
    return parse_verdict(item, text)


async def judge_all(items: list[JudgeItem], judge_fn: JudgeFn, concurrency: int = 2) -> list[Verdict]:
    import asyncio
    sem = asyncio.Semaphore(concurrency)

    async def one(it: JudgeItem) -> Verdict:
        async with sem:
            return await judge_item(it, judge_fn)

    return list(await asyncio.gather(*(one(i) for i in items)))


def aggregate(verdicts: list[Verdict]) -> dict:
    """每个维度：n（适用条数）、mean、pass_rate（≥PASS_SCORE 的比例）；error 数单独列出。"""
    out: dict = {"n_items": len(verdicts), "n_error": sum(1 for v in verdicts if v.error), "by_criterion": {}}
    keys = []
    for v in verdicts:
        for k in v.scores:
            if k not in keys:
                keys.append(k)
    for k in keys:
        rows = [v.scores[k] for v in verdicts if not v.error and k in v.scores and v.scores[k]["applicable"]]
        n = len(rows)
        out["by_criterion"][k] = {
            "n": n,
            "mean": round(sum(r["score"] for r in rows) / n, 3) if n else None,
            "pass_rate": round(sum(r["score"] >= PASS_SCORE for r in rows) / n, 3) if n else None,
        }
    return out


# ---------------- 真实调用 Gemini（需要 Key） ----------------

def judge_model_name() -> str:
    from app.config import get_settings
    return os.environ.get("JUDGE_MODEL") or get_settings().gemini_model


async def call_gemini_judge(prompt: str) -> str:
    import httpx

    from app.config import get_settings
    from app.services.gemini_errors import GeminiCallError, GeminiConfigError

    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiConfigError("未配置 GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{judge_model_name()}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0, "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA, "maxOutputTokens": 4096,
        },
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers={"x-goog-api-key": settings.gemini_api_key}, json=body, timeout=90.0)
    except httpx.HTTPError as exc:
        raise GeminiCallError(f"请求 Gemini 失败（{type(exc).__name__}）：{exc}") from exc
    if resp.status_code != 200:
        raise GeminiCallError(f"Gemini API 返回 {resp.status_code}：{resp.text[:1500]}")
    try:
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiCallError(f"解析 Gemini 返回失败：{exc}；{resp.text[:1500]}") from exc
