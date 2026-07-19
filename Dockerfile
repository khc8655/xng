# ---- Stage 1: Build (编译 C 扩展如 lxml) ----
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-hf.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-hf.txt

# ---- Stage 2: Runtime (仅运行时依赖) ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/home/user/.cache/ms-playwright

# 先创建用户以分配正确的家目录权限
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# 从 builder 阶段复制已编译的 Python 包
COPY --from=builder /install /usr/local

# 安装 Playwright Chromium 及其系统依赖，并修复 /home/user 的所有权为 user
RUN playwright install chromium --with-deps \
    && chown -R user:user /home/user \
    && rm -rf /var/lib/apt/lists/*

# 复制应用程序文件并分配所有权
COPY --chown=user . $HOME/app

# 切换为非 root 用户
USER user

# 确保启动脚本可执行
RUN chmod +x start.sh

EXPOSE 7860

ENTRYPOINT ["./start.sh"]
