#!/bin/bash
set -e
echo "Starting Recipes RAG Service..."
export PORT=${1:-${PORT:-8020}}
export REDIS_URL=${REDIS_URL:-redis://localhost:6379/0}

if [ -z "$SPOONACULAR_API_KEY" ]; then
    echo "Warning: SPOONACULAR_API_KEY not set"
    echo "Get a free API key at: https://spoonacular.com/food-api"
fi

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

export PYTHONPATH="${PYTHONPATH}:$(pwd)/../../.."

python3 -c "from shared.cache import cached" 2>/dev/null || {
    echo "Error: Cannot import shared package"
    exit 1
}

echo "Starting Recipes RAG service on port $PORT..."

# Kill any existing instance on port 8020
echo "Checking for existing service on port 8020..."
lsof -ti:8020 | xargs kill -9 2>/dev/null || true
sleep 1

exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
