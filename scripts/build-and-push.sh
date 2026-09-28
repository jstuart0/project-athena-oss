#!/bin/bash
# Build and push all Project Athena container images
# Usage: ./scripts/build-and-push.sh [--tag TAG] [--force-tag] [service_name]
#   If service_name is provided, only that service is built
#   Otherwise, all services are built
#
# Environment variables:
#   REGISTRY - Container registry URL (default: localhost:5000)
#   TAG      - Image tag (default: latest)
#
# Flags:
#   --tag TAG    - Image tag; wins over both an exported TAG and config.env's
#   --force-tag  - Skip the tag-collision guard and push even if REGISTRY
#                  already has this image:tag (default: refuse)
#
# Precedence for the effective tag (highest wins): --tag flag > exported
# TAG env var > config.env's TAG > "latest". config.env is a deployment
# default for unattended runs — it never overrides an explicit invocation.
#
# Every build passes --pull, so it always starts from the current upstream
# base image rather than a stale local layer cache.
#
# Examples:
#   REGISTRY=myregistry.io:5000 ./scripts/build-and-push.sh
#   REGISTRY=ghcr.io/myorg TAG=v1.0.0 ./scripts/build-and-push.sh gateway
#   ./scripts/build-and-push.sh --tag v2.0.0 --force-tag gateway

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Capture any TAG the caller exported *before* config.env gets a chance to
# clobber it — config.env supplies deployment defaults for unattended runs,
# it must never silently override an explicit `TAG=v2.0.0 ./build-and-push.sh`
# or `--tag v2.0.0` invocation (ATHENA-126: a stale config.env TAG overwrote
# an intentional push to a fresh tag).
CALLER_TAG="${TAG-}"

# --- CLI flag parsing (before config.env, so --tag/--force-tag participate
# in the same precedence resolution as an exported TAG) ---
FORCE_TAG=0
CLI_TAG=""
SERVICE_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --tag)
            if [ $# -lt 2 ] || [[ "$2" == --* ]]; then
                log_error "--tag requires a value"
                exit 1
            fi
            CLI_TAG="$2"
            shift 2
            ;;
        --tag=*)
            CLI_TAG="${1#--tag=}"
            shift
            ;;
        --force-tag)
            FORCE_TAG=1
            shift
            ;;
        --)
            shift
            if [ $# -gt 0 ]; then
                SERVICE_ARG="$1"
            fi
            break
            ;;
        -*)
            log_error "Unknown flag: $1"
            exit 1
            ;;
        *)
            SERVICE_ARG="$1"
            shift
            ;;
    esac
done

# Load config if exists
if [ -f "$PROJECT_ROOT/config.env" ]; then
    source "$PROJECT_ROOT/config.env"
fi

REGISTRY="${REGISTRY:-localhost:5000}"

# Precedence (highest wins): --tag flag > caller-exported TAG > config.env's
# TAG > "latest" default.
if [ -n "$CLI_TAG" ]; then
    TAG="$CLI_TAG"
elif [ -n "$CALLER_TAG" ]; then
    TAG="$CALLER_TAG"
else
    TAG="${TAG:-latest}"
fi

log_info "Effective registry: $REGISTRY"
log_info "Effective tag: $TAG"

# Returns 0 if $REGISTRY/$1:$2 is listed by the registry's v2 tags/list API,
# 1 if the registry answered but the tag wasn't listed, 2 if the endpoint
# could not be reached at all (auth-gated registry, e.g. ghcr.io, or the
# registry is down) — callers must treat 2 as "unknown", not "safe".
tag_exists_in_registry() {
    local name="$1" tag="$2" proto url resp
    for proto in https http; do
        url="${proto}://${REGISTRY}/v2/${name}/tags/list"
        if resp=$(curl -fsS --max-time 5 "$url" 2>/dev/null); then
            if printf '%s' "$resp" | grep -q "\"${tag}\""; then
                return 0
            fi
            return 1
        fi
    done
    return 2
}

# Refuses (returns 1) a push to $REGISTRY/$name:$TAG that would silently
# overwrite an existing tag, unless --force-tag was passed. A registry that
# can't be queried (return 2 above) warns and proceeds — we must not block a
# build for a check we structurally can't perform against an auth-gated
# registry.
guard_tag_collision() {
    local name="$1"
    if [ "$FORCE_TAG" = "1" ]; then
        return 0
    fi
    tag_exists_in_registry "$name" "$TAG"
    case $? in
        0)
            log_error "$REGISTRY/$name:$TAG already exists — refusing to overwrite. Re-run with --force-tag to push anyway."
            return 1
            ;;
        2)
            log_warn "Could not verify whether $REGISTRY/$name:$TAG already exists (registry unreachable or requires auth) — proceeding."
            ;;
    esac
    return 0
}

# Function to build and push - standard context (admin, jarvis-web)
build_push() {
    local name=$1
    local context=$2
    local dockerfile="${context}/Dockerfile"

    if [ ! -f "$dockerfile" ]; then
        log_warn "Dockerfile not found: $dockerfile - SKIPPING"
        return 1
    fi

    guard_tag_collision "$name" || return 1

    # Copy shared module if needed (for admin-backend)
    if [[ "$name" == "athena-admin-backend" ]]; then
        log_info "Copying shared module to $context..."
        rm -rf "$context/shared"
        cp -r "$PROJECT_ROOT/src/shared" "$context/shared"
    fi

    log_info "Building $name from $context..."
    if docker build --pull --platform linux/amd64 -t "$REGISTRY/$name:$TAG" -f "$dockerfile" "$context"; then
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

# Function to build and push with an explicit Dockerfile path.
# Used for services whose build context differs from the Dockerfile location —
# e.g. jarvis-web needs repo-root context to access src/shared/admin_url.py while
# its Dockerfile remains at apps/jarvis-web/Dockerfile.
#
# Args: $1 = image_name, $2 = build_context, $3 = dockerfile path relative to context
build_push_with_dockerfile() {
    local name=$1
    local context=$2
    local dockerfile_relpath=$3
    local dockerfile_abspath="${context}/${dockerfile_relpath}"

    if [ ! -f "$dockerfile_abspath" ]; then
        log_warn "Dockerfile not found: $dockerfile_abspath - SKIPPING"
        return 1
    fi

    guard_tag_collision "$name" || return 1

    log_info "Building $name from $context (Dockerfile: $dockerfile_relpath)..."
    if docker build --pull --platform linux/amd64 \
        -t "$REGISTRY/$name:$TAG" \
        -f "$dockerfile_abspath" \
        "$context"; then
        log_info "Pushing $name..."
        docker push "$REGISTRY/$name:$TAG"
        log_info "$name built and pushed successfully"
        return 0
    else
        log_error "Failed to build $name"
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

    guard_tag_collision "$name" || return 1

    log_info "Building $name with src/ context..."
    if docker build --pull --platform linux/amd64 \
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

# Service definitions (arrays) are maintained in service-defs.sh.
# $PROJECT_ROOT must be set before sourcing so ADMIN_SERVICES paths expand correctly.
# shellcheck source=service-defs.sh
source "$(dirname "$0")/service-defs.sh"

# Build specific service if provided
if [ -n "$SERVICE_ARG" ]; then
    found=false

    # Check admin services
    for service_def in "${ADMIN_SERVICES[@]}"; do
        IFS=':' read -r name context dockerfile_relpath <<< "$service_def"
        if [ "$SERVICE_ARG" == "$name" ] || [ "$SERVICE_ARG" == "${name#athena-}" ]; then
            if [ -n "$dockerfile_relpath" ]; then
                build_push_with_dockerfile "$name" "$context" "$dockerfile_relpath"
            else
                build_push "$name" "$context"
            fi
            found=true
            break
        fi
    done

    # Check core src services
    if [ "$found" = false ]; then
        for service_def in "${CORE_SRC_SERVICES[@]}"; do
            IFS=':' read -r name dir_name <<< "$service_def"
            if [ "$SERVICE_ARG" == "$name" ] || [ "$SERVICE_ARG" == "${name#athena-}" ]; then
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
            if [ "$SERVICE_ARG" == "$name" ] || [ "$SERVICE_ARG" == "${name#athena-}" ] || [ "$SERVICE_ARG" == "$short_name" ]; then
                build_push_src "$name" "$dir_name"
                found=true
                break
            fi
        done
    fi

    if [ "$found" = false ]; then
        log_error "Unknown service: $SERVICE_ARG"
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
    IFS=':' read -r name context dockerfile_relpath <<< "$service_def"
    if [ -n "$dockerfile_relpath" ]; then
        dockerfile_abspath="${context}/${dockerfile_relpath}"
        if build_push_with_dockerfile "$name" "$context" "$dockerfile_relpath"; then
            ((SUCCESSFUL++))
        else
            if [ -f "$dockerfile_abspath" ]; then
                ((FAILED++))
            else
                ((SKIPPED++))
            fi
        fi
    else
        if build_push "$name" "$context"; then
            ((SUCCESSFUL++))
        else
            if [ -f "$context/Dockerfile" ]; then
                ((FAILED++))
            else
                ((SKIPPED++))
            fi
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
