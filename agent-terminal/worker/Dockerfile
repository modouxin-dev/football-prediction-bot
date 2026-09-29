# 一次性沙盒镜像：只装执行固定任务所需的最小工具集。
FROM python:3.12-slim

# 只装必要工具：git（克隆/打包）、curl（建 PR）、file（判断文本/二进制）
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl file ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行
RUN useradd -m -u 10001 sandbox
WORKDIR /workspace

# 固定任务脚本（只读挂载即可，无需写权限）
COPY ci/ /opt/ci/
COPY run_task.sh /opt/run_task.sh

RUN chmod +x /opt/run_task.sh /opt/ci/*.sh \
    && chown -R sandbox:sandbox /workspace

USER sandbox

# 禁用 git 交互式凭据提示，避免卡住
ENV GIT_TERMINAL_PROMPT=0

ENTRYPOINT ["/opt/run_task.sh"]
