"""
MCP 客户端管理器测试——真实端到端连接（子进程 + stdio 协议握手），
不做协议层的假模拟，跟 app/mcp_servers/policy_search_server.py 这个真实的
离线参考 server 通信，因为这两者本身就是"跑在同一台机器上的两个进程互相
说 stdio 协议"，不依赖外部网络，能在任何环境（包括这个开发沙箱）稳定跑通。

用独立的 MCPClientManager 实例（自己的 config_path），不用
get_mcp_manager() 返回的进程级单例——这样各用例互不干扰，也能测"配置文件
不存在""配置的 server 启动失败"这些边界情况，不会影响单例状态、也不会
影响其他测试用例里通过 tools.py -> get_mcp_manager() 拿到的那个共享连接。

每个用例结束后都要 shutdown()，避免残留的后台线程和子进程累积（尤其是
这个测试文件里特意构造的"连不上"的 server 配置，对应的子进程如果启动
成功了但协议握手失败，也要确保被清理掉）。
"""
import json

import pytest

from app.services.mcp_client import MCPClientManager

_REAL_CONFIG = {
    "servers": [
        {
            "name": "policy_search",
            "enabled": True,
            "command": "python",
            "args": ["-m", "app.mcp_servers.policy_search_server"],
            "env": {},
        }
    ]
}


@pytest.fixture
def real_manager(tmp_path):
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps(_REAL_CONFIG), encoding="utf-8")
    manager = MCPClientManager(config_path=config_path)
    yield manager
    manager.shutdown()


def test_connects_and_lists_web_search_tool(real_manager):
    assert real_manager.has_tool("web_search")
    schemas = real_manager.list_tool_schemas()
    names = {t.name for t in schemas}
    assert "web_search" in names


def test_call_tool_sync_returns_structured_result(real_manager):
    result = real_manager.call_tool_sync("web_search", {"query": "招生计划", "recency": "any"})
    assert result["ok"], f"应该成功: {result.get('error')}"
    assert "results" in result["data"]


def test_call_tool_sync_unknown_tool_fails_cleanly(real_manager):
    result = real_manager.call_tool_sync("this_tool_does_not_exist", {})
    assert not result["ok"]
    assert "不可用" in result["error"]


def test_missing_config_file_yields_no_tools(tmp_path):
    """配置文件路径指向一个不存在的文件——应该优雅降级成"没有可用工具"，
    不能抛异常把上层（tools.py 的 _tool_web_search）拖崩。
    """
    manager = MCPClientManager(config_path=tmp_path / "does_not_exist.json")
    try:
        assert not manager.has_tool("web_search")
        result = manager.call_tool_sync("web_search", {"query": "test"})
        assert not result["ok"]
    finally:
        manager.shutdown()


def test_disabled_server_is_not_connected(tmp_path):
    config = {
        "servers": [
            {
                "name": "policy_search",
                "enabled": False,
                "command": "python",
                "args": ["-m", "app.mcp_servers.policy_search_server"],
                "env": {},
            }
        ]
    }
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    manager = MCPClientManager(config_path=config_path)
    try:
        assert not manager.has_tool("web_search"), "enabled=false 的 server 不应该被连接"
    finally:
        manager.shutdown()


def test_start_is_idempotent(real_manager):
    """重复调用 start() 不应该重复连接/重复启动线程——has_tool()/call_tool_sync()
    内部都会调用 start()，多次调用应该安全、结果一致。
    """
    real_manager.start()
    real_manager.start()
    assert real_manager.has_tool("web_search")
