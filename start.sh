#!/bin/bash

# Start FastAPI MCP Server directly
echo "Starting FastAPI MCP Server on port 8000..."
exec uvicorn app:app --host 0.0.0.0 --port 8000
