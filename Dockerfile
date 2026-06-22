FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/home/user/.cache/ms-playwright

# Install basic tools and compilation dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create user with UID 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install dependencies (if any) and set ownership
RUN chown -R user:user /home/user && \
    rm -rf /var/lib/apt/lists/*

# Copy the rest of the application files and set ownership
COPY --chown=user . $HOME/app

# Switch to the non-root user
USER user

# Ensure files are executable
RUN chmod +x start.sh

# Expose the default port for HF Spaces
EXPOSE 7860

ENTRYPOINT ["./start.sh"]
