FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1

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
