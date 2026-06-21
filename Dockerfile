FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install basic tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install cloudflared (detects architecture: amd64 / arm64)
RUN arch=$(uname -m) && \
    if [ "$arch" = "x86_64" ]; then \
        wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -O cloudflared.deb; \
    elif [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then \
        wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -O cloudflared.deb; \
    else \
        echo "Unsupported architecture: $arch" && exit 1; \
    fi && \
    dpkg -i cloudflared.deb && \
    rm cloudflared.deb

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium and its system dependencies
RUN playwright install chromium && \
    playwright install-deps chromium && \
    rm -rf /var/lib/apt/lists/*

# Copy app files
COPY . .

# Ensure start.sh is executable
RUN chmod +x start.sh

# Expose port 8000
EXPOSE 8000

# Set entrypoint
ENTRYPOINT ["./start.sh"]
