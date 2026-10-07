FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
 PYTHONDONTWRITEBYTECODE=1 \
 PIP_NO_CACHE_DIR=1 \
 PIP_DISABLE_PIP_VERSION_CHECK=1 \
 MPLCONFIGDIR=/tmp/mplconfig
WORKDIR /app
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
RUN pip install -r requirements.txt && pip install -r requirements-web.txt
RUN mkdir -p /tmp/mplconfig && chmod 777 /tmp/mplconfig
RUN mkdir -p /data && chmod 777 /data
RUN useradd --create-home --uid 10001 bot
COPY --chown=bot:bot . .
COPY docker-entrypoint.sh start.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/start.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["start.sh"]

