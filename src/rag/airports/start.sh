#!/bin/bash
# Airports RAG Service Startup Script

set -e

# Load Homebrew environment
eval "$(/opt/homebrew/bin/brew shellenv)"

# Activate virtual environment
source ~/dev/project-athena/.venv/bin/activate

# Load environment variables
set -a
source ~/dev/project-athena/config/env/.env
set +a

# Set service port
export AIRPORTS_SERVICE_PORT=8011

# Navigate to project directory
cd ~/dev/project-athena

# Start service
exec python -m src.rag.airports.main
