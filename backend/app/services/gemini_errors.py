"""
Gemini 调用相关的异常类型单独放这一个文件，不放在 agent_client.py 或
gemini_tool_caller.py 里——因为接下来要加的重试逻辑（gemini_retry.py）
需要同时判断这两种异常，如果异常类定义在 agent_client.py 里，
gemini_retry.py 导入它、agent_client.py 又要导入 gemini_retry.py 里的
重试函数，会形成循环依赖。单独拆出来，三个文件都只依赖这一个，不互相依赖。
"""


class GeminiConfigError(RuntimeError):
    """
    GEMINI_API_KEY 未配置时抛出。这是配置问题，不是"这次运气不好"，
    重试不会让它变好，gemini_retry.py 里明确不对这个异常重试。
    """


class GeminiCallError(RuntimeError):
    """
    请求本身失败（网络错误、非200、解析畸形响应）时抛出。这类问题已经
    真实撞到过两次偶发案例——Gemini 免费层503限流、模型偶尔生成畸形超长
    数字导致解析失败——都是"这次运气不好，重试大概率就好"的情况，
    gemini_retry.py 会对这个异常自动重试。
    """
