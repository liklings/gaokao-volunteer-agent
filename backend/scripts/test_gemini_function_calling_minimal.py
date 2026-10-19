"""
独立测试1/2：Gemini function calling 最基本的能力——给它一个工具描述，
看它能不能正确识别"这句话该调用这个工具"。完全不经过项目其他代码，
纯粹验证这个新的 API 能力本身能不能用、格式对不对。

用法：先 export GEMINI_API_KEY=你的key，再直接跑：
    python scripts/test_gemini_function_calling_minimal.py

【预期成功的样子】
"模型是否正确识别出要调用工具" 打印 True，且调用的工具名是 search_candidates，
参数里能看到类似 {'major_keyword': '计算机'} 这样的内容。

【如果失败】
把完整的 status 和响应原文发给我，格式错误通常是 400，
网络问题是连不上/超时，都要看具体报错内容才能判断怎么改。
"""
import os

import httpx

SYSTEM_INSTRUCTION = "你是志愿领航系统的检索助手。只要用户问到具体的候选院校/专业组、" \
    "冲稳保概率、或者任何需要真实数据支撑的问题，必须调用 search_candidates 工具获取真实数据，" \
    "禁止凭你自己的知识编列学校名单或数字。"

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    raise SystemExit("请先设置环境变量 GEMINI_API_KEY（例如 export GEMINI_API_KEY=你的key）")
MODEL = "gemini-3.1-flash-lite"

TOOL_SCHEMA = [{
    "name": "search_candidates",
    "description": "根据考生条件检索符合资格的候选院校专业组，返回冲稳保参考概率。",
    "parameters": {
        "type": "object",
        "properties": {
            "major_keyword": {"type": "string"},
        },
    },
}]

body = {
    "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
    "contents": [{"role": "user", "parts": [{"text": "帮我查一下有哪些计算机相关的候选院校，重点推荐一个"}]}],
    "tools": [{"functionDeclarations": TOOL_SCHEMA}],
}

url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
resp = httpx.post(
    url,
    headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
    json=body,
    timeout=60.0,
)
print("status:", resp.status_code)
print(resp.text)

if resp.status_code == 200:
    data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    has_function_call = any("functionCall" in p for p in parts)
    print("\n模型是否正确识别出要调用工具：", has_function_call)
    if has_function_call:
        for p in parts:
            if "functionCall" in p:
                print("调用的工具:", p["functionCall"])
    else:
        print("模型直接给了文字回复，没有调用工具，内容：", parts)
