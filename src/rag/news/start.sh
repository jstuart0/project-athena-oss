#!/bin/bash
#
# News RAG Service Startup Script
#
# This script starts the News RAG service with proper configuration.
# It handles environment setup, dependency verification, and service startup.
#
# Usage:
#   ./start.sh [port]
#
# Environment Variables:
#   PORT - Port to listen on (default: 8015)
#   REDIS_URL - Redis cache URL (default: redis://localhost:6379/0)
#
# News reads its API key from the admin key store only (store keys
# api-newsapiai / api-webz) -- there is no NEWSAPI_KEY (or any other)
# env var to set here. Configure it via the admin UI's External API Keys
# page.
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting News RAG Service...${NC}"

# Check if running from correct directory
if [ ! -f "main.py" ]; then
    echo -e "${RED}Error: Must run from news service directory${NC}"
    echo "Current directory: $(pwd)"
    echo "Expected: src/rag/news/"
    exit 1
fi

# Default configuration
export PORT=${1:-${PORT:-8015}}
export REDIS_URL=${REDIS_URL:-redis://localhost:6379/0}

# News's key comes from the admin key store only -- there is no env var
# to verify here (see the header comment above).

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $PYTHON_VERSION"

# Install dependencies if needed
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install/upgrade dependencies
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Add shared package to Python path
export PYTHONPATH="${PYTHONPATH}:$(pwd)/../../.."

# Verify shared package is accessible
python3 -c "from shared.cache import cached" 2>/dev/null || {
    echo -e "${RED}Error: Cannot import shared package${NC}"
    echo "Make sure shared package is installed:"
    echo "  cd ../../shared && pip install -e ."
    exit 1
}

echo -e "${GREEN}Configuration:${NC}"
echo "  Port: $PORT"
echo "  Redis: $REDIS_URL"
echo ""

# Start the service
echo -e "${GREEN}Starting News RAG service on port $PORT...${NC}"
echo "Health check: http://localhost:$PORT/health"
echo "API docs: http://localhost:$PORT/docs"
echo ""
echo "Press Ctrl+C to stop"
echo ""

exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-config /dev/null
