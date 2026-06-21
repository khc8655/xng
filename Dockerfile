FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Install curl/wget if not present
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install cloudflared static binary directly (detects architecture: amd64 / arm64)
RUN arch=$(uname -m) && \
    if [ "$arch" = "x86_64" ]; then \
        wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared; \
    elif [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then \
        wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O /usr/local/bin/cloudflared; \
    else \
        echo "Unsupported architecture: $arch" && exit 1; \
    fi && \
    chmod +x /usr/local/bin/cloudflared

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Ensure start.sh is executable
RUN chmod +x start.sh

# Expose port 8000
EXPOSE 8000

# Set entrypoint
ENTRYPOINT ["./start.sh"]
