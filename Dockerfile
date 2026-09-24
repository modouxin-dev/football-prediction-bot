FROM python:3.12-slim

# PYTHONUNBUFFERED：日志实时输出到 Railway（否则 print/日志会被缓冲，看起来像“没有输出”）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# 以非 root 用户运行
RUN useradd --create-home --uid 10001 bot
COPY --chown=bot:bot . .
USER bot

# 这是一个长轮询的 worker，不监听端口，也不需要 Railway 的 cron 设置
CMD ["python", "main.py"]
