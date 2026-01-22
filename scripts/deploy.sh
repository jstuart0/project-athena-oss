#!/bin/bash
# Project Athena Production Deployment Script
#
# Prerequisites:
#   1. config.env file created (copy from config.env.example)
#   2. kubectl configured to your cluster
#   3. Docker configured (optionally with insecure registry)
#   4. (Optional) Ollama server running (local or remote)
#
# Usage:
#   ./scripts/deploy.sh [phase]
#
# Phases:
#   all       - Run all phases (default)
#   secrets   - Create Kubernetes secrets only
#   images    - Build and push container images only
#   deploy    - Deploy manifests only
#   status    - Show deployment status
#
# Environment variables (or set in config.env):
#   REGISTRY  - Container registry URL
#   NAMESPACE - Kubernetes namespace (default: athena-prod)

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load config if exists
if [ -f "$PROJECT_ROOT/config.env" ]; then
    source "$PROJECT_ROOT/config.env"
fi

NAMESPACE="${NAMESPACE:-athena-prod}"
REGISTRY="${REGISTRY:-localhost:5000}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_phase() { echo -e "\n${BLUE}========================================${NC}"; echo -e "${BLUE}$1${NC}"; echo -e "${BLUE}========================================${NC}\n"; }

check_prerequisites() {
    log_phase "Checking Prerequisites"

    # Check kubectl context
    CONTEXT=$(kubectl config current-context 2>/dev/null || echo "none")
    log_info "kubectl context: $CONTEXT"

    # Check cluster connectivity
    if kubectl cluster-info &>/dev/null; then
        log_info "Kubernetes cluster accessible"
    else
        log_error "Cannot connect to Kubernetes cluster"
        exit 1
    fi

    # Check registry accessibility (if not localhost)
    if [[ "$REGISTRY" != "localhost"* ]]; then
        REGISTRY_HOST=$(echo "$REGISTRY" | cut -d/ -f1)
        if curl -s --connect-timeout 5 "http://${REGISTRY_HOST}/v2/" > /dev/null 2>&1; then
            log_info "Container registry accessible: $REGISTRY"
        else
            log_warn "Cannot reach container registry at $REGISTRY"
            log_info "Make sure the registry is running and accessible"
        fi
    else
        log_info "Using local registry: $REGISTRY"
    fi

    # Check Ollama (if configured)
    OLLAMA_URL=$(kubectl -n $NAMESPACE get configmap athena-config -o jsonpath='{.data.OLLAMA_URL}' 2>/dev/null || echo "")
    if [ -n "$OLLAMA_URL" ] && [[ "$OLLAMA_URL" != *"ollama:11434"* ]]; then
        # Only check external Ollama URLs, not in-cluster
        if curl -s --connect-timeout 5 "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
            log_info "Ollama accessible at $OLLAMA_URL"
        else
            log_warn "Cannot reach Ollama at $OLLAMA_URL"
        fi
    fi

    # Check postgres (if configured)
    if [ -n "$ATHENA_DB_HOST" ] && [ "$ATHENA_DB_HOST" != "localhost" ]; then
        if nc -z -w 3 "$ATHENA_DB_HOST" "${ATHENA_DB_PORT:-5432}" 2>/dev/null; then
            log_info "PostgreSQL accessible at $ATHENA_DB_HOST"
        else
            log_warn "Cannot reach PostgreSQL at $ATHENA_DB_HOST:${ATHENA_DB_PORT:-5432}"
        fi
    fi
}

create_secrets() {
    log_phase "Creating Secrets"

    if [ -f "$PROJECT_ROOT/config.env" ] || [ -f "$PROJECT_ROOT/.env.secrets" ]; then
        log_info "Creating Kubernetes secrets..."
        "$PROJECT_ROOT/scripts/create-secrets.sh"
    else
        log_error "No configuration file found!"
        log_info "Copy config.env.example to config.env and fill in values"
        exit 1
    fi
}

build_images() {
    log_phase "Building and Pushing Container Images"

    "$PROJECT_ROOT/scripts/build-and-push.sh"
}

deploy_manifests() {
    log_phase "Deploying to Kubernetes"

    # Apply in order
    log_info "Creating namespace..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/namespace.yaml"

    log_info "Applying config..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/config.yaml"

    log_info "Deploying Ollama (in-cluster)..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/ollama.yaml"

    log_info "Deploying Redis..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/redis.yaml"

    log_info "Waiting for Ollama and Redis..."
    kubectl -n $NAMESPACE wait --for=condition=available --timeout=120s deployment/ollama || true
    kubectl -n $NAMESPACE wait --for=condition=available --timeout=60s deployment/redis

    log_info "Pulling Ollama models..."
    kubectl -n $NAMESPACE delete job ollama-model-pull 2>/dev/null || true
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/ollama.yaml"
    # Wait a bit for models to start pulling
    sleep 10

    log_info "Deploying Admin services..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/admin-backend.yaml"
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/admin-frontend.yaml"

    log_info "Deploying Core services..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/gateway.yaml"
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/orchestrator.yaml"
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/mode-service.yaml"

    log_info "Deploying RAG services..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/rag-services.yaml"

    log_info "Deploying Jarvis Web..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/jarvis-web.yaml"

    log_info "Configuring Ingress..."
    kubectl apply -f "$PROJECT_ROOT/manifests/athena-prod/ingress.yaml"

    log_info "Deployment complete!"
}

show_status() {
    log_phase "Deployment Status"

    echo "Namespace: $NAMESPACE"
    echo ""

    echo "=== Pods ==="
    kubectl -n $NAMESPACE get pods -o wide
    echo ""

    echo "=== Services ==="
    kubectl -n $NAMESPACE get svc
    echo ""

    echo "=== Ingress Routes ==="
    kubectl -n $NAMESPACE get ingressroute
    echo ""

    echo "=== Pod Status Summary ==="
    RUNNING=$(kubectl -n $NAMESPACE get pods --no-headers 2>/dev/null | grep -c Running || echo 0)
    TOTAL=$(kubectl -n $NAMESPACE get pods --no-headers 2>/dev/null | wc -l | tr -d ' ')
    echo "Running: $RUNNING / $TOTAL"
}

# Main
PHASE="${1:-all}"

case $PHASE in
    all)
        check_prerequisites
        create_secrets
        build_images
        deploy_manifests
        show_status
        ;;
    secrets)
        create_secrets
        ;;
    images)
        build_images
        ;;
    deploy)
        deploy_manifests
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 [all|secrets|images|deploy|status]"
        exit 1
        ;;
esac
