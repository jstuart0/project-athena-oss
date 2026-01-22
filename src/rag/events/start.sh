#!/bin/bash
#
# Events RAG Service Startup Script
#
# This script starts the Events RAG service with proper configuration.
# It handles environment setup, dependency verification, and service startup.
#
# Usage:
#   ./start.sh [port]
#
# Environment Variables:
#   PORT - Port to listen on (default: 8013)
#   TICKETMASTER_API_KEY - Ticketmaster API key (required)
#   REDIS_URL - Redis cache URL (default: redis://localhost:6379/0)
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting Events RAG Service...${NC}"

# Check if running from correct directory
if [ ! -f "main.py" ]; then
    echo -e "${RED}Error: Must run from events service directory${NC}"
    echo "Current directory: $(pwd)"
    echo "Expected: src/rag/events/"
    exit 1
fi

# Default configuration
export PORT=${1:-${PORT:-8013}}
export REDIS_URL=${REDIS_URL:-redis://localhost:6379/0}

# Verify Ticketmaster API key is set
if [ -z "$TICKETMASTER_API_KEY" ]; then
    echo -e "${YELLOW}Warning: TICKETMASTER_API_KEY not set${NC}"
    echo "Service will start but API calls will fail"
    echo "Get a free API key at: https://developer.ticketmaster.com/"
    echo ""
    echo "Set it with:"
    echo "  export TICKETMASTER_API_KEY=your_api_key_here"
    echo ""
fi

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
echo "  Ticketmaster API Key: ${TICKETMASTER_API_KEY:0:10}... (${#TICKETMASTER_API_KEY} chars)"
echo ""

# Start the service
echo -e "${GREEN}Starting Events RAG service on port $PORT...${NC}"
echo "Health check: http://localhost:$PORT/health"
echo "API docs: http://localhost:$PORT/docs"
echo ""
echo "Press Ctrl+C to stop"
echo ""

exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-config /dev/null
