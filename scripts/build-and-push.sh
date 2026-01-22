#!/bin/bash
# Build and push all Project Athena container images
# Usage: ./scripts/build-and-push.sh [service_name]
#   If service_name is provided, only that service is built
#   Otherwise, all services are built
#
# Environment variables:
#   REGISTRY - Container registry URL (default: localhost:5000)
#   TAG      - Image tag (default: latest)
#
# Examples:
#   REGISTRY=myregistry.io:5000 ./scripts/build-and-push.sh
#   REGISTRY=ghcr.io/myorg TAG=v1.0.0 ./scripts/build-and-push.sh gateway

set -e

# Load config if exists
if [ -f "$(dirname "$0")/../config.env" ]; then
    source "$(dirname "$0")/../config.env"
fi

REGISTRY="${REGISTRY:-localhost:5000}"
TAG="${TAG:-latest}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Function to build and push - standard context (admin, jarvis-web)
build_push() {
    local name=$1
    local context=$2
    local dockerfile="${context}/Dockerfile"

    if [ ! -f "$dockerfile" ]; then
        log_warn "Dockerfile not found: $dockerfile - SKIPPING"
        return 1
    fi

    # Copy shared module if needed (for admin-backend)
    if [[ "$name" == "athena-admin-backend" ]]; then
        log_info "Copying shared module to $context..."
        rm -rf "$context/shared"
        cp -r "$PROJECT_ROOT/src/shared" "$context/shared"
    fi

    log_info "Building $name from $context..."
    if docker build --platform linux/amd64 -t "$REGISTRY/$name:$TAG" -f "$dockerfile" "$context"; then
        log_info "Pushing $name..."
        docker push "$REGISTRY/$name:$TAG"
        log_info "$name built and pushed successfully"
        # Clean up shared module copy
        if [[ "$name" == "athena-admin-backend" ]]; then
            rm -rf "$context/shared"
        fi
        return 0
    else
        log_error "Failed to build $name"
        # Clean up shared module copy on failure too
        if [[ "$name" == "athena-admin-backend" ]]; then
            rm -rf "$context/shared"
        fi
        return 1
    fi
}

# Function to build services that need src/ context (gateway, orchestrator, mode_service, RAG)
build_push_src() {
    local name=$1
    local service_dir=$2
    local dockerfile="$PROJECT_ROOT/src/${service_dir}/Dockerfile"

    if [ ! -f "$dockerfile" ]; then
        log_warn "Dockerfile not found: $dockerfile - SKIPPING"
        return 1
    fi

    log_info "Building $name with src/ context..."
    if docker build --platform linux/amd64 \
        -t "$REGISTRY/$name:$TAG" \
        -f "$dockerfile" \
        "$PROJECT_ROOT/src"; then
        log_info "Pushing $name..."
        docker push "$REGISTRY/$name:$TAG"
        log_info "$name built and pushed successfully"
        return 0
    else
        log_error "Failed to build $name"
        return 1
    fi
}

# Admin services (use their own directory as context)
ADMIN_SERVICES=(
    "athena-admin-backend:$PROJECT_ROOT/admin/backend"
    "athena-admin-frontend:$PROJECT_ROOT/admin/frontend"
    "athena-jarvis-web:$PROJECT_ROOT/apps/jarvis-web"
)

# Core services that need src/ context (have shared module dependency)
CORE_SRC_SERVICES=(
    "athena-gateway:gateway"
    "athena-orchestrator:orchestrator"
    "athena-mode-service:mode_service"
)

# RAG services - name:directory_name (built with src/ context)
RAG_SERVICES=(
    "athena-rag-weather:rag/weather"
    "athena-rag-airports:rag/airports"
    "athena-rag-stocks:rag/stocks"
    "athena-rag-flights:rag/flights"
    "athena-rag-events:rag/events"
    "athena-rag-streaming:rag/streaming"
    "athena-rag-news:rag/news"
    "athena-rag-sports:rag/sports"
    "athena-rag-websearch:rag/websearch"
    "athena-rag-dining:rag/dining"
    "athena-rag-recipes:rag/recipes"
    "athena-rag-onecall:rag/onecall"
    "athena-rag-seatgeek:rag/seatgeek_events"
    "athena-rag-transportation:rag/transportation"
    "athena-rag-community:rag/community_events"
    "athena-rag-amtrak:rag/amtrak"
    "athena-rag-tesla:rag/tesla"
    "athena-rag-media:rag/media"
    "athena-rag-directions:rag/directions"
    "athena-rag-sitescraper:rag/site_scraper"
    "athena-rag-serpapi:rag/serpapi_events"
    "athena-rag-pricecompare:rag/price_compare"
    "athena-rag-brightdata:rag/brightdata"
)

# Build specific service if provided
if [ -n "$1" ]; then
    found=false

    # Check admin services
    for service_def in "${ADMIN_SERVICES[@]}"; do
        IFS=':' read -r name context <<< "$service_def"
        if [ "$1" == "$name" ] || [ "$1" == "${name#athena-}" ]; then
            build_push "$name" "$context"
            found=true
            break
        fi
    done

    # Check core src services
    if [ "$found" = false ]; then
        for service_def in "${CORE_SRC_SERVICES[@]}"; do
            IFS=':' read -r name dir_name <<< "$service_def"
            if [ "$1" == "$name" ] || [ "$1" == "${name#athena-}" ]; then
                build_push_src "$name" "$dir_name"
                found=true
                break
            fi
        done
    fi

    # Check RAG services
    if [ "$found" = false ]; then
        for service_def in "${RAG_SERVICES[@]}"; do
            IFS=':' read -r name dir_name <<< "$service_def"
            short_name="${name#athena-rag-}"
            if [ "$1" == "$name" ] || [ "$1" == "${name#athena-}" ] || [ "$1" == "$short_name" ]; then
                build_push_src "$name" "$dir_name"
                found=true
                break
            fi
        done
    fi

    if [ "$found" = false ]; then
        log_error "Unknown service: $1"
        echo ""
        echo "Available services:"
        echo "  Admin services:"
        for service_def in "${ADMIN_SERVICES[@]}"; do
            IFS=':' read -r name _ <<< "$service_def"
            echo "    - $name"
        done
        echo "  Core services:"
        for service_def in "${CORE_SRC_SERVICES[@]}"; do
            IFS=':' read -r name _ <<< "$service_def"
            echo "    - $name"
        done
        echo "  RAG services:"
        for service_def in "${RAG_SERVICES[@]}"; do
            IFS=':' read -r name _ <<< "$service_def"
            echo "    - $name"
        done
        exit 1
    fi
    exit 0
fi

# Build all services
log_info "Building all Project Athena services..."
log_info "Registry: $REGISTRY"
log_info "Tag: $TAG"
echo ""

SUCCESSFUL=0
FAILED=0
SKIPPED=0

# Build admin services
log_info "=== Building Admin Services ==="
for service_def in "${ADMIN_SERVICES[@]}"; do
    IFS=':' read -r name context <<< "$service_def"
    if build_push "$name" "$context"; then
        ((SUCCESSFUL++))
    else
        if [ -f "$context/Dockerfile" ]; then
            ((FAILED++))
        else
            ((SKIPPED++))
        fi
    fi
    echo ""
done

# Build core src services
log_info "=== Building Core Services ==="
for service_def in "${CORE_SRC_SERVICES[@]}"; do
    IFS=':' read -r name dir_name <<< "$service_def"
    if build_push_src "$name" "$dir_name"; then
        ((SUCCESSFUL++))
    else
        dockerfile="$PROJECT_ROOT/src/${dir_name}/Dockerfile"
        if [ -f "$dockerfile" ]; then
            ((FAILED++))
        else
            ((SKIPPED++))
        fi
    fi
    echo ""
done

# Build RAG services
log_info "=== Building RAG Services ==="
for service_def in "${RAG_SERVICES[@]}"; do
    IFS=':' read -r name dir_name <<< "$service_def"
    if build_push_src "$name" "$dir_name"; then
        ((SUCCESSFUL++))
    else
        dockerfile="$PROJECT_ROOT/src/${dir_name}/Dockerfile"
        if [ -f "$dockerfile" ]; then
            ((FAILED++))
        else
            ((SKIPPED++))
        fi
    fi
    echo ""
done

# Summary
echo ""
log_info "=========================================="
log_info "Build Summary"
log_info "=========================================="
log_info "Successful: $SUCCESSFUL"
log_warn "Skipped (no Dockerfile): $SKIPPED"
if [ $FAILED -gt 0 ]; then
    log_error "Failed: $FAILED"
else
    echo -e "${GREEN}Failed: 0${NC}"
fi

if [ $FAILED -gt 0 ]; then
    exit 1
fi

log_info "All images built and pushed successfully!"
