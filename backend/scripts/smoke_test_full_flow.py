"""
端到端冒烟测试：用真实 Gemini Key 跑一遍完整链路
    对话收集条件 → 条件收集齐(ready=True) → 拿这份条件去查候选

用法（backend/ 目录下，激活 venv，.env 里已经填好真实 GEMINI_API_KEY，
uvicorn 已经在跑）：
    python scripts/smoke_test_full_flow.py

如果想测多轮追问（比如只说一半信息，看 Agent 是否会正确追问缺失项），
把 MESSAGES 列表拆成多条依次发送即可，脚本本身按顺序发送、复用同一个 profile_id。
"""
import json
import sys

import httpx

BASE_URL = "http://localhost:8000"

# 单条消息就把必填信息说全，用来验证"一轮就能收集齐条件"这条路径。
# 想测试多轮追问，把这条改成拆成多条依次发的列表即可。
MESSAGES = [
    "我是江苏物理类考生，2026届，637分，位次5200，选科物理+化学，想找计算机相关专业，优先考虑省内城市。",
]


def main() -> int:
    profile_id = None
    last_result = None

    print("=== 第一步：健康检查 ===")
    r = httpx.get(f"{BASE_URL}/api/health", timeout=10)
    print(r.status_code, r.json())
    if r.json().get("database") != "connected":
        print("数据库没连上，先检查 postgres 是否在跑、迁移是否跑过。")
        return 1

    print("\n=== 第二步：对话收集条件（真实调用 Gemini） ===")
    for msg in MESSAGES:
        print(f"\n用户: {msg}")
        try:
            r = httpx.post(
                f"{BASE_URL}/api/chat",
                json={"profile_id": profile_id, "message": msg},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            print(f"请求失败（网络层）：{exc}")
            return 1

        if r.status_code != 200:
            print(f"HTTP {r.status_code}")
            print(r.text)
            print("\n如果是 400/格式错误，把上面这段报错发给我，我调整 agent_client.py 里的请求格式。")
            return 1

        last_result = r.json()
        profile_id = last_result["profile_id"]
        print(f"Agent: {last_result['reply']}")
        print(f"profile_id={profile_id} missing={last_result['missing']} ready={last_result['ready']}")

    if not last_result or not last_result["ready"]:
        print("\n条件还没收集齐，正常情况——多发几轮消息补充缺失项即可。管线本身工作正常。")
        return 0

    print("\n=== 第三步：条件已收集齐，用这份 profile 查真实候选 ===")
    profile = last_result["profile"]
    search_payload = {
        "province": profile["province"],
        "track": profile["track"],
        "subjects": profile["subjects"],
        "score": profile["score"],
        "rank": profile["rank"],
        "major_keyword": profile["major_keyword"],
        "city_pref": profile["city_pref"],
        "budget_max": profile["budget_max"],
    }
    r = httpx.post(f"{BASE_URL}/api/candidates/search", json=search_payload, timeout=10)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text}")
        return 1

    data = r.json()
    print(f"通过硬性规则的候选数：{len(data['results'])}（资格校验总数：{len(data['audit'])}）")
    for c in data["results"][:5]:
        print(f"  {c['university']:12s} pct={c['pct']:>3} {c['category_label']}")

    print("\n=== 全链路跑通 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
