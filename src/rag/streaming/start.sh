#!/bin/bash
set -e
export PORT=${1:-${PORT:-8014}}
[ -z "$TMDB_API_KEY" ] && echo "Warning: TMDB_API_KEY not set"
if [ ! -d "venv" ]; then python3 -m venv venv; fi
source venv/bin/activate
pip install -q --upgrade pip && pip install -q -r requirements.txt
export PYTHONPATH="${PYTHONPATH}:$(pwd)/../../.."
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --log-config /dev/null
