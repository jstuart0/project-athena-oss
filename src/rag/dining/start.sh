#!/bin/bash
set -e
export PORT=${1:-${PORT:-8019}}
[ -z "$YELP_API_KEY" ] && echo "Warning: YELP_API_KEY not set"
if [ ! -d "venv" ]; then python3 -m venv venv; fi
source venv/bin/activate
pip install -q --upgrade pip && pip install -q -r requirements.txt
export PYTHONPATH="${PYTHONPATH}:$(pwd)/../../.."
# Kill any existing instance on port 8019
echo "Checking for existing service on port 8019..."
lsof -ti:8019 | xargs kill -9 2>/dev/null || true
sleep 1

exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
