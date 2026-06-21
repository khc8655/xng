FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

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
