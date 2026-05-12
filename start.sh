#!/bin/bash
# Nifty AI Agent — unified start script
set -e
cd "$(dirname "$0")"

mkdir -p logs

echo "[START] Installing dependencies..."
pip install -r requirements.txt --quiet

echo "[START] Launching agent engine in background..."
nohup python3 agent/engine.py > logs/engine.log 2>&1 &
echo $! > logs/engine.pid
echo "[START] Engine PID: $(cat logs/engine.pid)"

# Render injects $PORT — fall back to 8080 locally
PORT="${PORT:-8080}"
echo "[START] Launching web server on port $PORT..."
exec python3 server.py
