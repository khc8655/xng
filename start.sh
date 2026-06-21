#!/bin/bash

# Start FastAPI MCP Server on port 7860 directly (disabling access logs to prevent stats polling spam)
echo "Starting FastAPI MCP Server on port 7860..."
exec uvicorn app:app --host 0.0.0.0 --port 7860 --no-access-log
