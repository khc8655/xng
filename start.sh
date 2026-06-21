#!/bin/bash

# Terminate all background jobs if this script is killed/exits
trap "kill 0" EXIT

# 1. Start FastAPI MCP Server
echo "Starting FastAPI MCP Server on port 8000..."
uvicorn app:app --host 0.0.0.0 --port 8000 &
MCP_PID=$!

# 2. Start Cloudflare Tunnel if TUNNEL_TOKEN is set
CF_PID=""
if [ -n "$TUNNEL_TOKEN" ]; then
    echo "TUNNEL_TOKEN is set. Starting cloudflared tunnel..."
    cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" &
    CF_PID=$!
else
    echo "TUNNEL_TOKEN is not set. Cloudflare tunnel disabled (direct access mode)."
fi

# 3. Wait and monitor the processes
# If either uvicorn or cloudflared exits, the script exits.
if [ -n "$CF_PID" ]; then
    # Wait for either process to exit
    wait -n $MCP_PID $CF_PID
else
    # Only wait for FastAPI
    wait $MCP_PID
fi

# Exit with the status of the process that ended
EXIT_STATUS=$?
echo "One of the supervised processes exited with status $EXIT_STATUS. Terminating all..."
exit $EXIT_STATUS
