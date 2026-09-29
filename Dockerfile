FROM python:3.12-slim

# PYTHONUNBUFFERED：日志实时输出到 Railway（否则 print/日志会被缓冲，看起来像“没有输出”）
# MPLCONFIGDIR：非 root 运行时 matplotlib 需要一个可写的配置目录
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/mplconfig

WORKDIR /app

# 图表中文显示：slim 镜像不含任何中文字体，不装会渲染成方块。
# build-essential / libfreetype6-dev / libpng-dev：ARM（aarch64）机器上 matplotlib
# 可能拿不到预编译 wheel 而需要本地编译，缺这几个包会直接构建失败。
# x86 上它们用不到，但保留可让同一份 Dockerfile 在两种架构下都能构建。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fontconfig \
        gosu \
        build-essential \
        pkg-config \
        libfreetype6-dev \
        libpng-dev \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-web.txt ./
# 两个都装：web 依赖只是 fastapi + uvicorn，体积很小。
# 分开装本来是为了让机器人镜像保持精简，但在 Railway 上拆成两个服务
# 会导致看板读不到数据（Volume 不能跨服务共享），所以只能合到一个镜像里。
RUN pip install -r requirements-web.txt

# 让非 root 用户也能读写 matplotlib 缓存
RUN mkdir -p /tmp/mplconfig && chmod 777 /tmp/mplconfig
# 预测落盘目录：挂载持久化卷后重启不丢；未挂载时也能正常读写（仅重启清空）
RUN mkdir -p /data && chmod 777 /data

# 以非 root 用户运行
RUN useradd --create-home --uid 10001 bot
COPY --chown=bot:bot . .
COPY docker-entrypoint.sh start.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/start.sh

# 挂载卷 /data 的属主通常是 root，启动脚本会修正后再降权启动
ENTRYPOINT ["docker-entrypoint.sh"]

# 由 start.sh 同时拉起看板与机器人：
#   - uvicorn api:app 监听 $PORT（Railway 生成域名后即可访问看板）
#   - main.py 长轮询跑在前台，它退出则容器退出并被自动重启
# 机器人本身不需要 Cron Schedule，定时推送由程序内部调度。
CMD ["start.sh"]
