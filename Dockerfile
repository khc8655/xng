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

# 从 builder 阶段复制已编译的 Python 包
COPY --from=builder /install /usr/local

# 安装 Playwright Chromium 及其系统依赖（--with-deps 会自动装 libX11 等运行时库）
RUN playwright install chromium --with-deps \
    && rm -rf /var/lib/apt/lists/*

# Create user with UID 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Copy the application files and set ownership
COPY --chown=user . $HOME/app

# Switch to the non-root user
USER user

# Ensure files are executable
RUN chmod +x start.sh

EXPOSE 7860

ENTRYPOINT ["./start.sh"]
