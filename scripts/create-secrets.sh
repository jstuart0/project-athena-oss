#!/bin/bash
# Create Kubernetes secrets for Project Athena
#
# Usage:
#   1. Copy config.env.example to config.env and fill in values
#   2. Run: ./scripts/create-secrets.sh
#
# Or set environment variables directly before running
#
# Required variables:
#   ATHENA_DB_PASSWORD - Database password
#   ATHENA_DB_HOST     - Database hostname (default: localhost)
#   ATHENA_DB_USER     - Database username (default: athena)
#   ATHENA_DB_NAME     - Database name (default: athena)

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load config if exists
if [ -f "$PROJECT_ROOT/config.env" ]; then
    echo "Loading from config.env..."
    source "$PROJECT_ROOT/config.env"
fi

# Also load .env.secrets for backwards compatibility
if [ -f "$PROJECT_ROOT/.env.secrets" ]; then
    echo "Loading from .env.secrets..."
    export $(grep -v '^#' "$PROJECT_ROOT/.env.secrets" | xargs)
fi

NAMESPACE="${NAMESPACE:-athena-prod}"

# Check required variables
check_var() {
    if [ -z "${!1}" ]; then
        echo "ERROR: $1 is not set"
        echo "Please set it in config.env or as an environment variable"
        exit 1
    fi
}

echo "Checking required variables..."
check_var "ATHENA_DB_PASSWORD"

# Set defaults for optional variables
ATHENA_DB_HOST="${ATHENA_DB_HOST:-localhost}"
ATHENA_DB_PORT="${ATHENA_DB_PORT:-5432}"
ATHENA_DB_NAME="${ATHENA_DB_NAME:-athena}"
ATHENA_DB_USER="${ATHENA_DB_USER:-athena}"
HA_URL="${HA_URL:-http://homeassistant.local:8123}"

# Generate encryption keys if not provided
ENCRYPTION_KEY="${ENCRYPTION_KEY:-$(openssl rand -base64 32)}"
ENCRYPTION_SALT="${ENCRYPTION_SALT:-$(openssl rand -base64 16)}"
SESSION_SECRET_KEY="${SESSION_SECRET_KEY:-$(openssl rand -base64 32)}"
JWT_SECRET="${JWT_SECRET:-$(openssl rand -base64 32)}"

echo "Creating namespace if not exists..."
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

echo "Creating athena-db-credentials..."
kubectl -n $NAMESPACE create secret generic athena-db-credentials \
    --from-literal=DATABASE_URL="postgresql://${ATHENA_DB_USER}:${ATHENA_DB_PASSWORD}@${ATHENA_DB_HOST}:${ATHENA_DB_PORT}/${ATHENA_DB_NAME}" \
    --from-literal=ATHENA_DB_HOST="${ATHENA_DB_HOST}" \
    --from-literal=ATHENA_DB_PORT="${ATHENA_DB_PORT}" \
    --from-literal=ATHENA_DB_NAME="${ATHENA_DB_NAME}" \
    --from-literal=ATHENA_DB_USER="${ATHENA_DB_USER}" \
    --from-literal=ATHENA_DB_PASSWORD="${ATHENA_DB_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "Creating athena-encryption..."
kubectl -n $NAMESPACE create secret generic athena-encryption \
    --from-literal=ENCRYPTION_KEY="${ENCRYPTION_KEY}" \
    --from-literal=ENCRYPTION_SALT="${ENCRYPTION_SALT}" \
    --from-literal=SESSION_SECRET_KEY="${SESSION_SECRET_KEY}" \
    --from-literal=JWT_SECRET="${JWT_SECRET}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "Creating athena-api-keys..."
kubectl -n $NAMESPACE create secret generic athena-api-keys \
    --from-literal=OPENWEATHER_API_KEY="${OPENWEATHER_API_KEY:-}" \
    --from-literal=BRAVE_API_KEY="${BRAVE_API_KEY:-}" \
    --from-literal=NEWSAPI_KEY="${NEWSAPI_KEY:-}" \
    --from-literal=TMDB_API_KEY="${TMDB_API_KEY:-}" \
    --from-literal=TICKETMASTER_API_KEY="${TICKETMASTER_API_KEY:-}" \
    --from-literal=ALPHA_VANTAGE_API_KEY="${ALPHA_VANTAGE_API_KEY:-}" \
    --from-literal=YELP_API_KEY="${YELP_API_KEY:-}" \
    --from-literal=SPOONACULAR_API_KEY="${SPOONACULAR_API_KEY:-}" \
    --from-literal=THESPORTSDB_API_KEY="${THESPORTSDB_API_KEY:-}" \
    --from-literal=FLIGHTAWARE_API_KEY="${FLIGHTAWARE_API_KEY:-}" \
    --from-literal=SEATGEEK_API_KEY="${SEATGEEK_API_KEY:-}" \
    --from-literal=TESLA_API_KEY="${TESLA_API_KEY:-}" \
    --from-literal=GOOGLE_MAPS_API_KEY="${GOOGLE_MAPS_API_KEY:-}" \
    --from-literal=SERPAPI_KEY="${SERPAPI_KEY:-}" \
    --from-literal=BRIGHTDATA_API_KEY="${BRIGHTDATA_API_KEY:-}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "Creating ha-credentials..."
kubectl -n $NAMESPACE create secret generic ha-credentials \
    --from-literal=url="${HA_URL}" \
    --from-literal=token="${HA_TOKEN:-}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "Secrets created successfully in namespace: $NAMESPACE"
echo ""
kubectl -n $NAMESPACE get secrets
