FROM python:3.12-slim

# PYTHONUNBUFFERED：日志实时输出到 Railway（否则 print/日志会被缓冲，看起来像“没有输出”）
# MPLCONFIGDIR：非 root 运行时 matplotlib 需要一个可写的配置目录
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/mplconfig

WORKDIR /app

# 图表中文显示：slim 镜像不含任何中文字体，不装会渲染成方块
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fontconfig \
        gosu \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# 让非 root 用户也能读写 matplotlib 缓存
RUN mkdir -p /tmp/mplconfig && chmod 777 /tmp/mplconfig
# 预测落盘目录：挂载持久化卷后重启不丢；未挂载时也能正常读写（仅重启清空）
RUN mkdir -p /data && chmod 777 /data

# 以非 root 用户运行
RUN useradd --create-home --uid 10001 bot
COPY --chown=bot:bot . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 挂载卷 /data 的属主通常是 root，启动脚本会修正后再降权启动
ENTRYPOINT ["docker-entrypoint.sh"]

# 这是一个长轮询的 worker，不监听端口，也不需要 Railway 的 cron 设置
CMD ["python", "main.py"]
