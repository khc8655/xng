#!/bin/bash

PORT=${PORT:-7860}
echo "Starting FastAPI MCP Server on port $PORT..."
exec uvicorn app:app --host 0.0.0.0 --port "$PORT"
