"""
安全响应头中间件（方向⑥：生产环境加固）。

这几个头都是"几乎不可能误伤正常使用、但能挡掉一整类常见攻击"的低成本防护，
所以默认对所有响应都加，不需要按接口开关：
- X-Content-Type-Options: nosniff —— 防止浏览器"猜"错内容类型去执行本不该执行的东西
  （比如把一个 JSON 响应当成 HTML/JS 解析），是 MIME 混淆攻击的基本防护。
- X-Frame-Options: DENY —— 禁止这个后端的任何响应被套进 <iframe> 里，防点击劫持。
  这个后端只提供 JSON API，没有理由被嵌进别的页面。
- Referrer-Policy: strict-origin-when-cross-origin —— 跳转到别的域名时不把完整 URL
  （可能带着 query string 里的敏感参数，比如 /export?token=... 那个链接）当 referrer 发出去。

Strict-Transport-Security（HSTS）单独处理：只在 environment=="production" 时才加。
本地开发/docker 环境很多时候还是走 http，HSTS 一旦被浏览器记住会强制这个域名之后
全部走 https，本地调试环境加了反而添乱，所以严格限定只在生产环境下生效。
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        if get_settings().environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

        return response
