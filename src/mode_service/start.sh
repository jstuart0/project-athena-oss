#!/bin/bash
set -e

# Mode Service Startup Script

export PORT=${1:-${MODE_SERVICE_PORT:-8021}}

# Warning if calendar URL not set
[ -z "$CALENDAR_URL" ] && echo "Warning: CALENDAR_URL not set - guest mode will not function"

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install/upgrade dependencies
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Set PYTHONPATH to include parent directories
export PYTHONPATH="${PYTHONPATH}:$(pwd)/../../.."

# Start service
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
