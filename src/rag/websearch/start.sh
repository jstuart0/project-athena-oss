#!/bin/bash
set -e
export PORT=${1:-${PORT:-8018}}
[ -z "$BRAVE_API_KEY" ] && echo "Warning: BRAVE_API_KEY not set"
if [ ! -d "venv" ]; then python3 -m venv venv; fi
source venv/bin/activate
pip install -q --upgrade pip && pip install -q -r requirements.txt
export PYTHONPATH="${PYTHONPATH}:$(pwd)/../../.."
# Kill any existing instance on port 8018
echo "Checking for existing service on port 8018..."
lsof -ti:8018 | xargs kill -9 2>/dev/null || true
sleep 1

exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
