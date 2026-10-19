"""
对话 Agent（Profile Agent）的 Gemini 客户端。密钥只在这里、只在服务器端使用——
浏览器不会看到 GEMINI_API_KEY。

请求格式（responseSchema 字段大小写等）已经用真实 API Key 联调验证过，
是对的，不再是未验证的假设。
"""
import sys
sys.set_int_max_str_digits(0)  # 0 表示不限制
import json

import httpx

from app.config import get_settings
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.gemini_retry import call_with_retry

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "profile": {
            "type": "object",
            "properties": {
                "province": {"type": "string", "nullable": True},
                "year": {"type": "integer", "nullable": True, "minimum": 2020, "maximum": 2030},
                "track": {"type": "string", "nullable": True},
                "subjects": {"type": "array", "items": {"type": "string"}, "nullable": True},
                "score": {"type": "integer", "nullable": True},
                "rank": {"type": "integer", "nullable": True},
                "major_keyword": {"type": "string", "nullable": True},
                "city_pref": {"type": "string", "nullable": True},
                "budget_max": {"type": "integer", "nullable": True},
                "home_province_only": {"type": "boolean", "nullable": True},
                "accept_sino_foreign": {"type": "boolean", "nullable": True},
                "accept_special_program": {"type": "boolean", "nullable": True},
            },
            "required": [
                "province", "year", "track", "subjects", "score",
                "rank", "major_keyword", "city_pref", "budget_max", "home_province_only",
                "accept_sino_foreign", "accept_special_program",
            ],
        },
        "conflicts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reply", "profile", "conflicts"],
}


def build_system_prompt() -> str:
    return """你是"志愿领航 Agent"，一个高考志愿规划助手。你的任务是通过多轮对话，从用户的自然语言描述中提取结构化的报考条件，并在信息不全时用简短、具体的问题追问缺失或冲突的条件。

需要收集的字段（profile 对象的 key）：
- province：省份（字符串，如"江苏"）
- year：年份/届（数字，如 2026）
- track：类别，"物理类" 或 "历史类"
- subjects：选科数组，如 ["物理","化学"]
- score：高考分数（数字，可为空，与 rank 至少填一个）
- rank：全省位次（数字，可为空，与 score 至少填一个）
- major_keyword：专业关键词（字符串，如"计算机"）
- city_pref：城市偏好（字符串，可空）
- budget_max：学费上限，元/年（数字，可空）
- home_province_only：是否只看学校实际所在省份跟考生目标省份一致的候选（布尔值，可空）。
  只有用户明确表达出"只想看省内的学校""不考虑外省的学校""不要给我推荐外省院校"这类意思时才填 true；
  用户明确说"外省也可以""不限制省份"时填 false；用户完全没提这件事时，这一轮不要填这个字段（保持 null）。
  这个字段跟 province 不是一回事：province 是考生的高考所在省份（用来匹配招生计划面向哪个省），
  home_province_only 是"要不要额外排除掉那些'虽然招这个省的考生、但学校自己physically不在这个省'的学校"
  （比如面向江苏招生的外省专科院校）——不要把这两个字段搞混，也不要在用户只说了自己是哪个省考生、
  没有额外表达"只要省内"这层意思时，就自作主张把这个字段设成 true。
- accept_sino_foreign：是否接受中外合作办学项目（布尔值，可空）。这类项目学费通常显著更高
  （一年几万到十几万），系统默认不把它们纳入推荐。用户明确表示"可以考虑中外合作""中外合办也行"
  时填 true；明确表示"不考虑中外合作""学费太贵的不要"这类排斥意思时填 false；用户没提过这件事时
  保持 null，不要替用户假设。
- accept_special_program：是否接受专项计划/定向培养类型（国家专项/地方专项/高校专项/农村订单
  定向医学生/公费师范生定向等，布尔值，可空）。这类项目通常有户籍、服务期、毕业去向等额外限制。
  用户明确表示"专项计划也可以看看""愿意定向培养"时填 true；明确表示"不考虑定向的""不想被户籍/
  服务期限制"时填 false；没提过时保持 null。

规则：
1. 1. 每轮都必须返回完整的 profile 对象，包含全部字段的 key（province/year/track/subjects/score/rank/major_keyword/city_pref/budget_max/home_province_only/accept_sino_foreign/accept_special_program 一个都不能少）。本轮用户这句话没有涉及、或者你无法从中提取出新信息的字段，值填 null；已经提取到的字段，不管是不是本轮新说的，都要填上实际值。服务器会跳过值为 null 的字段、不会因此覆盖已收集到的信息。
2. reply 要简洁（1-3句话），像一个专业、克制、友好的规划顾问。如果必填字段（province、track、subjects、score或rank、major_keyword）还缺，用一句自然的话追问，一次最多追问2项，不要罗列所有字段；不要在 reply 里自己判断"是否可以开始检索"，这由服务器根据字段是否齐全来决定。
3. 如果发现明显冲突（例如选择的专业方向与选科不兼容、城市偏好与省份明显矛盾），在 conflicts 数组中用一句话描述冲突，并在 reply 中说明；没有冲突就返回空数组。
4. 只输出符合给定 JSON Schema 的对象，不要输出 JSON 之外的任何文字。
5. 必填字段刚好在这一轮收集齐、accept_sino_foreign 和 accept_special_program 这两个字段
   在当前已知条件里都还是 null（说明还没问过）时，在这一轮的 reply 末尾用一句话顺带问一下：
   "另外，是否愿意考虑中外合作办学或专项计划/定向培养类型？（不考虑的话可以不用回复，默认不会
   推荐这两类）"——只在这个时机问一次，不要在必填字段还没收集齐时问，也不要在已经问过一次
   （不管用户是否回答、看对话历史里是否已经有过类似的话）之后又重复问，用户没有回应就默认
   保持现状（等价于不接受），不用揪着追问。
6. 只追问必填字段（province、track、subjects、score 或 rank、major_keyword）。year（年份/届次）不是必填项，
   用户没有主动提到就保持 null，绝对不要追问"哪一年高考""哪一届"；city_pref、budget_max、
   home_province_only 这类可选条件也不要主动追问，用户自己说了才记录。唯一的例外是规则5里那一句关于
   中外合作/专项计划的顺带询问。必填字段都齐了、又没有要顺带问的事时，reply 只需简短确认已记录的信息，
   不要再提出新的问题。
7. 追问要克制：缺的必填项超过 2 项时，这一轮只挑最关键的 2 项问，其余留到下一轮，不要把缺失项一次全列出来
   （哪怕用户只是打招呼、提问，或只说了一个可选偏好，也一样）。计数口径：「类别（物理/历史）+选科」合起来算
   1 项，「分数或位次」算 1 项，省份、专业各算 1 项。建议顺序：先问省份和类别/选科，再问分数或位次和专业。
   分数和位次有一个就算齐了，不要再追问另一个；必填项已齐时，不要问"还需要补充其他要求吗"这类开放式问题。
"""


async def _call_agent_once(profile_json: str, history: list[dict], user_message: str) -> dict:
    """真正发一次请求。call_agent()（下面）在这外面包了一层自动重试。"""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiConfigError(
            "未配置 GEMINI_API_KEY。请在 backend/.env 里设置真实的 Gemini API Key"
            "（去 https://aistudio.google.com/apikey 申请），然后重启服务。"
        )

    contents = []
    for m in history:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    contents.append({
        "role": "user",
        "parts": [{"text": f"当前已知条件（JSON）：{profile_json}\n\n用户最新输入：{user_message}"}],
    })

    body = {
        "systemInstruction": {"parts": [{"text": build_system_prompt()}]},
        "contents": contents,
                "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget":512},
        },
    }

    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
                json=body,
                timeout=60.0,
            )
    except httpx.HTTPError as exc:
        raise GeminiCallError(
            f"请求 Gemini API 失败（网络层，{type(exc).__name__}）：{exc}。"
            f"如果本机配置了代理（HTTP_PROXY/HTTPS_PROXY环境变量），这很可能是代理问题——"
            f"可以尝试临时关闭代理，或检查代理是否稳定。"
        ) from exc

    if resp.status_code != 200:
        raise GeminiCallError(f"Gemini API 返回 {resp.status_code}：{resp.text[:2000]}")

    try:
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiCallError(f"解析 Gemini 返回内容失败：{exc}；原始响应：{resp.text[:2000]}") from exc

    if "reply" not in parsed or "profile" not in parsed:
        raise GeminiCallError(f"Gemini 返回内容缺少必要字段：{parsed}")
    parsed.setdefault("conflicts", [])
    return parsed


async def call_agent(profile_json: str, history: list[dict], user_message: str) -> dict:
    """
    对外入口，带自动重试。GeminiCallError（网络失败/畸形响应/503限流等）
    会自动重试最多2次，GeminiConfigError（没配Key）不重试——这两种情况
    这个项目调试过程中都真实撞到过，之前只能靠人工手动重跑，现在自动化。

    history: [{"role": "user"|"assistant", "content": "..."}]（数据库里存的历史，不含本轮）
    返回: {"reply": str, "profile": dict, "conflicts": list[str]}
    """
    return await call_with_retry(lambda: _call_agent_once(profile_json, history, user_message))
