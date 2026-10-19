FROM python:3.12-slim

WORKDIR /app

# 先只拷贝依赖描述文件，这样源码变化时不用重新装依赖，构建更快
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

# 不用 root 跑应用进程——容器逃逸/依赖漏洞这类问题里，"进程本身是不是 root"
# 直接决定攻击者拿到的权限上限。三行做完：建一个专用用户/组，把 /app 的属主
# 换过去，再用 USER 切过去，之后所有 RUN/CMD 都以这个用户身份执行。
RUN groupadd -r app && useradd -r -g app app && chown -R app:app /app
USER app

EXPOSE 8000

# 健康检查用 Python 标准库自己发一个请求，不额外装 curl（slim 镜像里没有，
# 装了还得多一层 apt-get update 的体积和维护成本）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
