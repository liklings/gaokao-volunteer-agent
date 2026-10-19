from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, health, candidates, chat, plans
from app.config import get_settings
from app.middleware import SecurityHeadersMiddleware
from app.services.logging_config import configure_logging
from app.services.mcp_client import get_mcp_manager

# 在任何业务模块打第一条日志之前配置好——统一格式（带 run_id）、统一
# 输出目标。放在其他 app.* 模块导入之前执行，不然万一某个模块导入期间
# 就打了日志（目前没有，但以后要是加了不至于用默认的无格式配置）。
configure_logging()

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description=(
        "志愿领航 Agent 后端 API。\n\n"
        "**免责声明**：本系统的录取概率、院校数据均为参考性质，"
        "不构成录取保证；数据可能与官方最新发布存在时间差，"
        "正式填报前请以省级教育考试院和目标院校官方发布的信息为准。"
    ),
    version="0.1.0",
)

# 中间件的添加顺序有讲究：Starlette 是后添加的先执行（洋葱模型），
# 这里顺序无所谓先后（两者互不依赖对方的处理结果），但 CORS 官方建议尽量
# 加在外层，确保连预检请求（OPTIONS）和出错响应也能带上 CORS 头。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(candidates.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(plans.router, prefix="/api")


@app.on_event("startup")
def _start_mcp_client() -> None:
    """
    应用启动时就预热 MCP 连接（连接子进程冷启动可能要几百毫秒到几秒，
    见 mcp_client.py 顶部说明），避免第一个真实用户请求恰好第一次调用
    web_search 时才现连、白白多等这次冷启动时间。get_mcp_manager().start()
    本身是幂等的——就算这里没触发（比如测试环境 TestClient 默认不触发
    startup 事件），第一次真正调用工具时也会懒启动，这里只是做预热，
    不是唯一的启动入口。
    """
    get_mcp_manager().start()


@app.on_event("shutdown")
def _shutdown_mcp_client() -> None:
    """应用关闭时优雅关闭所有 MCP server 子进程和后台事件循环线程，
    避免进程退出后留下僵尸子进程。
    """
    get_mcp_manager().shutdown()


@app.get("/")
def root():
    return {"message": "志愿领航 Agent 后端已启动，访问 /docs 查看接口文档，/api/health 查看健康检查。"}
