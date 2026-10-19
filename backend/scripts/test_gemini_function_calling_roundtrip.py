"""
独立测试2/2：把第一轮模型请求调用的工具，构造一个假的返回结果喂回去，
看模型能不能正确读懂、给出基于这个结果的最终文字回复。这一步专门用来
验证 functionResponse 那部分的角色(role)字段和格式对不对——这是文档
不同版本容易写法不一致的地方。

用法：
    python scripts/test_gemini_function_calling_roundtrip.py

【预期成功的样子】
第一轮能看到模型请求调用 search_candidates；第二轮喂回假数据后，
模型的最终文字回复里应该提到"东南大学"或"南京邮电大学"——
这说明它真的读懂了我们喂回去的工具结果，不是自说自话。

【如果第二轮报错或者模型没有正确引用喂回去的数据】
大概率是 functionResponse 那部分 contents 里的 role 字段需要从
"user" 改成 "function"，把完整报错原文发我，我照着改
app/services/gemini_tool_caller.py 和这个脚本里的写法。
"""
import os

import httpx

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    raise SystemExit("请先设置环境变量 GEMINI_API_KEY（例如 export GEMINI_API_KEY=你的key）")
MODEL = "gemini-3.1-flash-lite"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

TOOL_SCHEMA = [{
    "name": "search_candidates",
    "description": "根据考生条件检索符合资格的候选院校专业组。",
    "parameters": {
        "type": "object",
        "properties": {"major_keyword": {"type": "string"}},
    },
}]


def post(body):
    return httpx.post(
        URL,
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=60.0,
    )


# 第一轮：让模型请求调用工具
contents = [{"role": "user", "parts": [{"text": "帮我查一下计算机相关的专业，有哪些候选"}]}]
SYSTEM_INSTRUCTION = "你是志愿领航系统的检索助手。只要用户问到具体的候选院校/专业组、" \
    "冲稳保概率、或者任何需要真实数据支撑的问题，必须调用 search_candidates 工具获取真实数据，" \
    "禁止凭你自己的知识编列学校名单或数字。"

body = {
    "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
    "contents": contents,
    "tools": [{"functionDeclarations": TOOL_SCHEMA}],
}
resp1 = post(body)
print("=== 第一轮 ===")
print("status:", resp1.status_code)
if resp1.status_code != 200:
    print(resp1.text)
    raise SystemExit(1)

data1 = resp1.json()
parts1 = data1["candidates"][0]["content"]["parts"]
function_call_part = next((p for p in parts1 if "functionCall" in p), None)
if function_call_part is None:
    print("模型没有请求调用工具，拿到的是文字回复:", parts1)
    raise SystemExit(1)
print("模型请求调用:", function_call_part["functionCall"])

# 构造一个假的工具执行结果（不查真实数据库，纯粹测试格式）
fake_tool_result = {
    "ok": True,
    "data": {
        "count": 2,
        "candidates": [
            {"university": "东南大学", "pct": 42, "category_label": "冲刺"},
            {"university": "南京邮电大学", "pct": 71, "category_label": "稳妥"},
        ],
    },
}

# 第二轮：把模型的 functionCall 和我们的工具结果都加进对话历史，再问一次
contents.append({"role": "model", "parts": [function_call_part]})
contents.append({"role": "user", "parts": [{"functionResponse": {
    "name": function_call_part["functionCall"]["name"],
    "response": fake_tool_result,
}}]})

body2 = {"contents": contents, "tools": [{"functionDeclarations": TOOL_SCHEMA}]}
resp2 = post(body2)
print("\n=== 第二轮（喂回工具结果）===")
print("status:", resp2.status_code)
print(resp2.text)

if resp2.status_code == 200:
    data2 = resp2.json()
    parts2 = data2["candidates"][0]["content"]["parts"]
    text_parts = [p["text"] for p in parts2 if "text" in p]
    final_text = "".join(text_parts)
    print("\n模型基于工具结果给出的最终回复:", final_text)
    mentioned = ("东南大学" in final_text) or ("南京邮电大学" in final_text)
    print("是否正确引用了喂回去的数据:", mentioned)
