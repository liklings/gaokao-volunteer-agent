"""
独立测试：Gemini 原生 Google Search grounding 联网搜索能力本身，完全不涉及
我们自己的 function calling 工具，纯粹先看清楚真实的响应结构长什么样——
特别是来源链接（groundingMetadata）到底是什么字段名、什么结构，
拿到真实样本之后再写解析代码，不凭空猜。

用法：先 export GEMINI_API_KEY=你的key，再直接跑：
    python scripts/test_gemini_search_grounding_minimal.py

【预期成功的样子】
status 200，响应里能看到一段基于最新信息的文字回复，以及某种带 URL 的
来源信息（大概率在 groundingMetadata 字段里，但具体结构要看真实输出才知道）。

【如果失败】
- 如果报字段不认识（400，提示 "google_search" 或类似字段无效），
  说明这个字段名写法不对，尝试把 "google_search" 换成 "googleSearch" 再跑一次，
  把两次的完整报错都发我。
- 如果报权限/额度相关的错误，可能是这个功能需要额外开通或者免费额度不包含，
  把报错原文发我。
"""
import os

import httpx

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    raise SystemExit("请先设置环境变量 GEMINI_API_KEY（例如 export GEMINI_API_KEY=你的key）")
MODEL = "gemini-3.1-flash-lite"

url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

# 问一个必须联网才能答对的问题（模型训练数据里不可能有这个信息），
# 这样如果它答对了，就能确认联网搜索确实生效了，不是靠记忆蒙对的。
body = {
    "contents": [{"role": "user", "parts": [{"text": "2026年高考江苏省的一分一段表是什么时候公布的？请说明信息来源。"}]}],
    "tools": [{"google_search": {}}],
}

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
    candidate = data["candidates"][0]
    parts = candidate["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    print("\n=== 模型的文字回复 ===")
    print(text)

    grounding = candidate.get("groundingMetadata")
    print("\n=== groundingMetadata 是否存在 ===")
    print(grounding is not None)
    if grounding:
        print("\n=== groundingMetadata 完整内容（用来看真实结构）===")
        import json
        print(json.dumps(grounding, ensure_ascii=False, indent=2))
