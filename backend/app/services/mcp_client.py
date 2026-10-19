"""
MCP 客户端管理器：让 Agent 作为 MCP client 接入外部 MCP server，替换掉
tools.py 里 web_search 之前"尚未接入真实数据源"的占位实现。

三个设计取舍，都是从这次会话里实际验证过的约束推出来的，不是凭空设计：

1. 【为什么长期复用连接，不是每次调用工具都现连】
   MCP 标准的 stdio 传输方式是"启动一个子进程，通过 stdin/stdout 说协议"，
   现实中的 MCP server（尤其是真实联网搜索类的，很多是 Node/npx 起步）
   冷启动一次要几百毫秒到几秒。如果每次工具调用都重新 spawn 一次子进程，
   一次对话里模型连续调两次 web_search，用户就要多等两次冷启动时间。
   所以设计成：进程级别只连接一次，长期复用，直到应用关闭。

2. 【为什么要专门开一个后台线程跑常驻事件循环，而不是直接 async/await】
   MCP SDK 的 ClientSession 是纯 async 接口，但 react_loop.py 里
   _execute_tool_with_timeout() 是通过 loop.run_in_executor(None,
   execute_tool, ...) 在一个线程池线程（没有运行中的事件循环）里同步调用
   TOOL_REGISTRY 里的工具函数——这是 ReAct 循环那边已经跑通、被其他三个
   工具依赖的既有设计，不为了新加一个工具去改。于是这里反过来：用一个
   专门的后台线程常驻一个 asyncio 事件循环，对外暴露的 call_tool_sync()
   内部用 run_coroutine_threadsafe 把请求丢进那个循环、阻塞等结果返回——
   跟 run_in_executor（"给同步代码找一个线程去跑"）刚好是反过来的同一种
   "同步/异步边界桥接"技巧。

3. 【为什么连接哪些 server 是配置文件而不是写死在代码里】
   backend/mcp_servers.json 决定连哪些 server、怎么启动，是因为这些信息
   （命令、参数、尤其是真实联网 server 可能需要的 API Key）应该能不改代码
   就替换——本仓库默认只配了一个自带的离线参考实现（见
   app/mcp_servers/policy_search_server.py 顶部注释：为什么默认离线），
   以后要换真实联网搜索 server，只改这一份 JSON 配置文件。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "mcp_servers.json"

# 单次 MCP 工具调用最多等待多少秒——跟 limits.py 里 TOOL_CALL_TIMEOUT_SECONDS
# 是同一类"防止一步卡死拖垮整个 ReAct 循环"的保护，这里单独定义（不直接
# import limits.py 里那个值）是因为两者概念上独立：limits.py 那个是
# react_loop.py 对"任意工具"的通用超时保护，这个是 mcp_client 内部
# "等 MCP server 响应"这一步单独的超时，数值目前保持一致，但允许以后
# 分开调整（比如真实联网搜索 server 明显比本地工具慢，可能需要更长的
# MCP 层超时，同时 react_loop 层的整体超时保持不变直接让这一步失败）。
MCP_CALL_TIMEOUT_SECONDS = 15

# 连接/初始化单个 MCP server（含子进程启动+协议握手）最多等待多少秒。
MCP_CONNECT_TIMEOUT_SECONDS = 20

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: str) -> str:
    """把字符串里的 ${VAR_NAME} 替换成当前进程环境变量的值。
    找不到对应环境变量时保留原样（不静默替换成空字符串）——这样配置错了
    （比如忘记设 SEARCH_API_KEY）能在连接失败时看到明显不对的值，
    而不是悄悄传一个空字符串给 server 导致更难排查的"认证失败"。
    """
    def _sub(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return _ENV_VAR_PATTERN.sub(_sub, value)


@dataclass(frozen=True)
class MCPToolInfo:
    """一个已连接 MCP server 暴露出来的工具，记录它来自哪个 server——
    调用时需要知道该把请求发去哪个 ClientSession。
    """
    server_name: str
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class _ServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]


def _load_server_configs(config_path: Path) -> list[_ServerConfig]:
    if not config_path.exists():
        logger.warning("MCP 配置文件不存在：%s，本次不连接任何 MCP server", config_path)
        return []

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    configs = []
    for entry in raw.get("servers", []):
        if not entry.get("enabled", True):
            continue
        env = {k: _interpolate_env(v) for k, v in (entry.get("env") or {}).items()}
        configs.append(_ServerConfig(
            name=entry["name"],
            command=entry["command"],
            args=list(entry.get("args") or []),
            env=env,
        ))
    return configs


class MCPClientManager:
    """
    进程内单例（由模块级 get_mcp_manager() 提供），管理所有配置里启用的
    MCP server 连接，对外暴露同步接口，供 tools.py 的工具函数直接调用。

    生命周期：start() 幂等（重复调用直接返回，不会重复连接），可以在
    FastAPI 启动事件里显式调用做"预热"，也可以完全不显式调用——
    第一次真正需要调用工具时（call_tool_sync/has_tool）会自动触发懒启动，
    这是为了兼容测试场景：pytest 用的 TestClient(app) 不是用 `with` 包裹
    使用的（见 tests/conftest.py），不会触发 FastAPI 的 startup 事件，
    如果只依赖 startup 事件来启动，测试环境下 MCP 连接永远不会建立。
    shutdown() 同样幂等，进程退出/应用关闭时应该调用，负责优雅关闭所有
    子进程和后台线程，避免残留僵尸进程。
    """

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path(
            os.environ.get("MCP_SERVERS_CONFIG", str(_DEFAULT_CONFIG_PATH))
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, object] = {}  # server_name -> ClientSession
        self._tools: dict[str, MCPToolInfo] = {}  # tool_name -> info（跨 server 全局唯一）
        self._exit_stack: AsyncExitStack | None = None
        self._start_lock = threading.Lock()
        self._started = False
        self._start_error: str | None = None
        self._loop_ready_event = threading.Event()

    # ---------------------------------------------------------------- 启动

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._thread = threading.Thread(
                target=self._run_loop, name="mcp-client-loop", daemon=True,
            )
            self._thread.start()
            # 等后台线程真正把事件循环建好、并且完成了所有 server 的连接
            # （或者连接失败但已经记录了错误）之后再返回——start() 之后
            # 立即调用 has_tool()/call_tool_sync() 应该能看到确定的状态，
            # 不需要调用方自己再去猜"是不是还没连完"。事件对象在 __init__
            # 里就创建好（而不是这里才创建），避免线程跑得比这里赋值还快、
            # _run_loop 的 finally 里访问 self._loop_ready_event 时属性
            # 还不存在的竞态。
            if not self._loop_ready_event.wait(timeout=MCP_CONNECT_TIMEOUT_SECONDS * 5):
                logger.error("MCP 客户端后台线程启动超时，工具将不可用")
                self._start_error = "MCP 客户端启动超时"
            self._started = True

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_all())
        except Exception as exc:  # noqa: BLE001 - 连接失败要记录，但不能让后台线程崩溃导致进程退出
            logger.exception("MCP server 连接过程出错：%s", exc)
            self._start_error = str(exc)
        finally:
            self._loop_ready_event.set()
        loop.run_forever()

    async def _connect_all(self) -> None:
        import mcp
        from mcp.client.stdio import stdio_client

        self._exit_stack = AsyncExitStack()
        configs = _load_server_configs(self._config_path)
        if not configs:
            return

        for cfg in configs:
            try:
                params = mcp.StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None)
                read, write = await asyncio.wait_for(
                    self._exit_stack.enter_async_context(stdio_client(params)),
                    timeout=MCP_CONNECT_TIMEOUT_SECONDS,
                )
                session = await self._exit_stack.enter_async_context(mcp.ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=MCP_CONNECT_TIMEOUT_SECONDS)
                self._sessions[cfg.name] = session

                listed = await session.list_tools()
                for tool in listed.tools:
                    if tool.name in self._tools:
                        logger.warning(
                            "MCP 工具名冲突：%s 在 server=%s 和 server=%s 都有，"
                            "保留先连接的那个，跳过后面的",
                            tool.name, self._tools[tool.name].server_name, cfg.name,
                        )
                        continue
                    self._tools[tool.name] = MCPToolInfo(
                        server_name=cfg.name, name=tool.name,
                        description=tool.description or "", input_schema=tool.inputSchema or {},
                    )
                logger.info(
                    "MCP server 已连接：name=%s tools=%s",
                    cfg.name, [t.name for t in listed.tools],
                )
            except Exception as exc:  # noqa: BLE001 - 单个 server 连接失败不能拖垮其他 server，记录后继续下一个
                logger.exception("连接 MCP server 失败：name=%s，跳过", cfg.name)
                self._start_error = f"连接 MCP server '{cfg.name}' 失败：{exc}"

    # ---------------------------------------------------------------- 查询

    def has_tool(self, tool_name: str) -> bool:
        self.start()
        return tool_name in self._tools

    def list_tool_schemas(self) -> list[MCPToolInfo]:
        """当前已连接、可用的全部 MCP 工具信息，主要用于调试/文档，
        不直接暴露给 Gemini function calling（tools.py 里 web_search 的
        Gemini schema 仍然是静态声明的，保证系统提示词/工具描述的稳定性，
        不随 MCP server 是否连上而变化）。
        """
        self.start()
        return list(self._tools.values())

    # ---------------------------------------------------------------- 调用

    def call_tool_sync(self, tool_name: str, arguments: dict, timeout: float = MCP_CALL_TIMEOUT_SECONDS) -> dict:
        """
        同步接口，专门给 tools.py 里的工具函数用——那些函数签名要求同步、
        且是在没有运行中事件循环的线程池线程里被调用的（见模块顶部说明）。

        返回值统一是一个 dict：
        - 成功：{"ok": True, "data": <工具返回的结构化内容>}
        - 失败：{"ok": False, "error": "<原因>"}
        调用方（tools.py 的 _tool_web_search）直接拿这个 dict 组装 ToolResult，
        不需要关心这里内部到底是"MCP server 没连上"还是"调用本身超时"还是
        "工具执行报错"这几种失败原因的区别，都统一成同一种失败观测结果。
        """
        self.start()
        if self._start_error and not self._tools:
            return {"ok": False, "error": f"MCP 客户端未能建立任何连接：{self._start_error}"}
        if tool_name not in self._tools:
            return {"ok": False, "error": f"MCP 工具不可用：{tool_name}（当前已连接的工具：{list(self._tools)}）"}
        if self._loop is None:
            return {"ok": False, "error": "MCP 客户端事件循环未就绪"}

        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(tool_name, arguments), self._loop,
        )
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            return {"ok": False, "error": f"MCP 工具调用超时（超过{timeout}秒）：{tool_name}"}
        except Exception as exc:  # noqa: BLE001 - 任何调用期异常都转成失败观测结果，不抛给上层
            return {"ok": False, "error": f"MCP 工具调用出错（{type(exc).__name__}）：{exc}"}

    async def _call_tool_async(self, tool_name: str, arguments: dict) -> dict:
        info = self._tools[tool_name]
        session = self._sessions[info.server_name]
        result = await session.call_tool(tool_name, arguments)

        if result.isError:
            text = "".join(getattr(block, "text", "") for block in result.content)
            return {"ok": False, "error": text or "MCP 工具返回了错误，但没有附带说明文字"}

        # MCP 的 CallToolResult.content 是一个内容块列表（文本/图片/……），
        # 这几个参考 server 目前只会返回 TextContent、且文本内容是 JSON——
        # 优先按 JSON 解析成结构化数据喂给模型（跟其他本地工具的返回格式
        # 一致），解析失败就退化成原始文本，不假设所有 MCP server 都严格
        # 返回 JSON（真实第三方 server 可能只返回纯文本）。
        texts = [getattr(block, "text", None) for block in result.content]
        texts = [t for t in texts if t is not None]
        combined = "\n".join(texts)
        try:
            data = json.loads(combined)
        except (json.JSONDecodeError, ValueError):
            data = {"text": combined}
        return {"ok": True, "data": data}

    # ---------------------------------------------------------------- 关闭

    def shutdown(self) -> None:
        if not self._started or self._loop is None:
            return

        async def _close():
            if self._exit_stack is not None:
                await self._exit_stack.aclose()

        try:
            future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
            future.result(timeout=MCP_CONNECT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - 关闭过程中的异常不应该阻止进程退出
            logger.exception("关闭 MCP 客户端连接时出错")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._started = False
            self._sessions.clear()
            self._tools.clear()


_manager: MCPClientManager | None = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> MCPClientManager:
    """进程内单例——tools.py 每次调用 web_search 工具都要用同一个 manager，
    才能复用已经建立的连接，不是每次都新建一个（新建等于每次都要重新走
    一遍冷启动，违背了整个设计的初衷）。
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = MCPClientManager()
    return _manager
