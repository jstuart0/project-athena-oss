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
#
# Idempotency: re-running this script preserves existing secret values.
# If you have upgraded Athena and a new required key was added to an existing
# secret, the script will report the missing key and exit with instructions
# rather than silently regenerating or overwriting existing values.

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load config if exists
if [ -f "$PROJECT_ROOT/config.env" ]; then
    echo "Loading from config.env..."
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/config.env"
fi

# Also load .env.secrets for backwards compatibility
if [ -f "$PROJECT_ROOT/.env.secrets" ]; then
    echo "Loading from .env.secrets..."
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env.secrets"
    set +a
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

echo "Creating namespace if not exists..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# -----------------------------------------------------------------------
# athena-db-credentials — idempotent via --dry-run=client | kubectl apply
# (kubectl apply is already idempotent for this block)
# -----------------------------------------------------------------------
echo "Creating athena-db-credentials..."
kubectl -n "$NAMESPACE" create secret generic athena-db-credentials \
    --from-literal=DATABASE_URL="postgresql://${ATHENA_DB_USER}:${ATHENA_DB_PASSWORD}@${ATHENA_DB_HOST}:${ATHENA_DB_PORT}/${ATHENA_DB_NAME}" \
    --from-literal=ATHENA_DB_HOST="${ATHENA_DB_HOST}" \
    --from-literal=ATHENA_DB_PORT="${ATHENA_DB_PORT}" \
    --from-literal=ATHENA_DB_NAME="${ATHENA_DB_NAME}" \
    --from-literal=ATHENA_DB_USER="${ATHENA_DB_USER}" \
    --from-literal=ATHENA_DB_PASSWORD="${ATHENA_DB_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -

# -----------------------------------------------------------------------
# athena-encryption — upgrade-safe idempotency
# Required keys: ENCRYPTION_KEY, ENCRYPTION_SALT, SESSION_SECRET_KEY,
#                JWT_SECRET, SERVICE_API_KEY
# -----------------------------------------------------------------------
ENCRYPTION_REQUIRED_KEYS=("ENCRYPTION_KEY" "ENCRYPTION_SALT" "SESSION_SECRET_KEY" "JWT_SECRET" "SERVICE_API_KEY")
if kubectl -n "$NAMESPACE" get secret athena-encryption &>/dev/null; then
    MISSING=()
    for key in "${ENCRYPTION_REQUIRED_KEYS[@]}"; do
        if ! kubectl -n "$NAMESPACE" get secret athena-encryption \
                -o "jsonpath={.data.${key}}" 2>/dev/null | grep -q '.'; then
            MISSING+=("$key")
        fi
    done
    if [ ${#MISSING[@]} -gt 0 ]; then
        echo "ERROR: existing secret 'athena-encryption' is missing required key(s): ${MISSING[*]}"
        echo "  This usually means you upgraded Athena and a new secret key was added."
        echo "  Two options:"
        echo "    (a) Manually patch each missing key:"
        for key in "${MISSING[@]}"; do
            echo "          kubectl -n $NAMESPACE patch secret athena-encryption --type=json \\"
            echo "            -p='[{\"op\":\"add\",\"path\":\"/data/${key}\",\"value\":\"<base64-encoded-value>\"}]'"
        done
        echo "    (b) Regenerate (DESTRUCTIVE — existing values lost):"
        echo "          kubectl -n $NAMESPACE delete secret athena-encryption"
        echo "          ./scripts/create-secrets.sh"
        exit 1
    fi
    echo "Secret athena-encryption already exists with all required keys; skipping."
else
    # Generate encryption keys if not provided
    ENCRYPTION_KEY="${ENCRYPTION_KEY:-$(openssl rand -base64 32)}"
    ENCRYPTION_SALT="${ENCRYPTION_SALT:-$(openssl rand -base64 16)}"
    SESSION_SECRET_KEY="${SESSION_SECRET_KEY:-$(openssl rand -base64 32)}"
    JWT_SECRET="${JWT_SECRET:-$(openssl rand -base64 32)}"
    SERVICE_API_KEY="${SERVICE_API_KEY:-$(openssl rand -hex 32)}"

    echo "Creating athena-encryption..."
    kubectl -n "$NAMESPACE" create secret generic athena-encryption \
        --from-literal=ENCRYPTION_KEY="${ENCRYPTION_KEY}" \
        --from-literal=ENCRYPTION_SALT="${ENCRYPTION_SALT}" \
        --from-literal=SESSION_SECRET_KEY="${SESSION_SECRET_KEY}" \
        --from-literal=JWT_SECRET="${JWT_SECRET}" \
        --from-literal=SERVICE_API_KEY="${SERVICE_API_KEY}"
fi

# -----------------------------------------------------------------------
# athena-oidc — upgrade-safe idempotency
# Required keys: OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER, OIDC_REDIRECT_URI
#
# WARNING: If OIDC variables are not set, placeholder values are written.
# The admin-backend will reject these placeholders in production.
# Set OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER, OIDC_REDIRECT_URI in
# config.env (or as environment variables), then delete this secret and re-run.
# -----------------------------------------------------------------------
OIDC_REQUIRED_KEYS=("OIDC_CLIENT_ID" "OIDC_CLIENT_SECRET" "OIDC_ISSUER" "OIDC_REDIRECT_URI")
if kubectl -n "$NAMESPACE" get secret athena-oidc &>/dev/null; then
    MISSING=()
    for key in "${OIDC_REQUIRED_KEYS[@]}"; do
        if ! kubectl -n "$NAMESPACE" get secret athena-oidc \
                -o "jsonpath={.data.${key}}" 2>/dev/null | grep -q '.'; then
            MISSING+=("$key")
        fi
    done
    if [ ${#MISSING[@]} -gt 0 ]; then
        echo "ERROR: existing secret 'athena-oidc' is missing required key(s): ${MISSING[*]}"
        echo "  Two options:"
        echo "    (a) Manually patch each missing key:"
        for key in "${MISSING[@]}"; do
            echo "          kubectl -n $NAMESPACE patch secret athena-oidc --type=json \\"
            echo "            -p='[{\"op\":\"add\",\"path\":\"/data/${key}\",\"value\":\"<base64-encoded-value>\"}]'"
        done
        echo "    (b) Regenerate:"
        echo "          kubectl -n $NAMESPACE delete secret athena-oidc"
        echo "          ./scripts/create-secrets.sh"
        exit 1
    fi
    echo "Secret athena-oidc already exists with all required keys; skipping."
else
    if [ -z "$OIDC_CLIENT_ID" ] || [ -z "$OIDC_CLIENT_SECRET" ] || \
       [ -z "$OIDC_ISSUER" ] || [ -z "$OIDC_REDIRECT_URI" ]; then
        echo ""
        echo "WARNING: OIDC variables not fully set. Creating athena-oidc with CONFIGURE_ME placeholders."
        echo "  The admin-backend will REJECT these placeholders in production."
        echo "  Set OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER, OIDC_REDIRECT_URI in config.env,"
        echo "  then run: kubectl -n $NAMESPACE delete secret athena-oidc && ./scripts/create-secrets.sh"
        echo ""
    fi
    echo "Creating athena-oidc..."
    kubectl -n "$NAMESPACE" create secret generic athena-oidc \
        --from-literal=OIDC_CLIENT_ID="${OIDC_CLIENT_ID:-CONFIGURE_ME_OIDC_CLIENT_ID}" \
        --from-literal=OIDC_CLIENT_SECRET="${OIDC_CLIENT_SECRET:-CONFIGURE_ME_OIDC_CLIENT_SECRET}" \
        --from-literal=OIDC_ISSUER="${OIDC_ISSUER:-CONFIGURE_ME_OIDC_ISSUER}" \
        --from-literal=OIDC_REDIRECT_URI="${OIDC_REDIRECT_URI:-CONFIGURE_ME_OIDC_REDIRECT_URI}"
fi

# -----------------------------------------------------------------------
# athena-api-keys — idempotent via --dry-run=client | kubectl apply
# -----------------------------------------------------------------------
echo "Creating athena-api-keys..."
kubectl -n "$NAMESPACE" create secret generic athena-api-keys \
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

# -----------------------------------------------------------------------
# ha-credentials — idempotent via --dry-run=client | kubectl apply
# -----------------------------------------------------------------------
echo "Creating ha-credentials..."
kubectl -n "$NAMESPACE" create secret generic ha-credentials \
    --from-literal=url="${HA_URL}" \
    --from-literal=token="${HA_TOKEN:-}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "Secrets created successfully in namespace: $NAMESPACE"
echo ""
kubectl -n "$NAMESPACE" get secrets
