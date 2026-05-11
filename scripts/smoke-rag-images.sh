#!/usr/bin/env bash
# smoke-rag-images.sh — build each RAG image and verify it can import main.
#
# Execution model: FULL SWEEP, not fail-fast.
# All 23 services run regardless of individual failures; a summary table is printed
# at the end and the script exits 1 if any service failed. Rationale: a single bad-import
# PR typically triggers >1 failure, so surfacing them all in one pass beats N fix-rebuild
# cycles.
#
# Usage:
#   bash scripts/smoke-rag-images.sh                  # run all services
#   bash scripts/smoke-rag-images.sh --service <name> # run one service (image name, e.g. athena-rag-sports)
#   bash scripts/smoke-rag-images.sh --keep-images     # don't remove :smoke tags after run
#
# GHA build cache is activated automatically when GITHUB_ACTIONS=true AND
# ACTIONS_CACHE_URL is set (the latter is only present on runners with cache-write perms).
# Local runs and fork PRs always use plain buildx (no cache flags).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source service definitions (RAG_SERVICES array)
# shellcheck source=service-defs.sh
source "${SCRIPT_DIR}/service-defs.sh"

# ── Argument parsing ──────────────────────────────────────────────────────────
FILTER_SERVICE=""
KEEP_IMAGES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)
            FILTER_SERVICE="$2"
            shift 2
            ;;
        --keep-images)
            KEEP_IMAGES=true
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--service <image-name>] [--keep-images]" >&2
            exit 1
            ;;
    esac
done

# ── GHA BuildKit cache (conditional) ─────────────────────────────────────────
use_gha_cache() {
    # Only when running inside GitHub Actions AND the runner has cache write access.
    [[ -n "${GITHUB_ACTIONS:-}" && -n "${ACTIONS_CACHE_URL:-}" ]]
}

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ── Results tracking ──────────────────────────────────────────────────────────
PASSED=()
FAILED=()
SMOKE_TAGS=()

cleanup_images() {
    if [[ "${KEEP_IMAGES}" == "false" && ${#SMOKE_TAGS[@]} -gt 0 ]]; then
        echo ""
        echo "Removing :smoke tags..."
        for tag in "${SMOKE_TAGS[@]}"; do
            docker rmi "$tag" 2>/dev/null || true
        done
    fi
}
trap cleanup_images EXIT

# ── Main loop ─────────────────────────────────────────────────────────────────
run_smoke() {
    local name="$1"
    local path="$2"      # e.g. rag/site_scraper  (relative to src/)
    local smoke_tag="${name}:smoke"
    local dockerfile="${PROJECT_ROOT}/src/${path}/Dockerfile"

    if [[ ! -f "$dockerfile" ]]; then
        echo -e "${YELLOW}[SKIP]${NC} ${name}: Dockerfile not found at src/${path}/Dockerfile"
        FAILED+=("${name} (no Dockerfile)")
        return
    fi

    echo ""
    echo "── ${name} ──"

    # Build cache flags — active only inside GHA with cache-write permissions
    local build_args=()
    if use_gha_cache; then
        build_args+=(
            "--cache-from" "type=gha,scope=rag-${name}"
            "--cache-to"   "type=gha,mode=max,scope=rag-${name},ignore-error=true"
        )
    fi

    # Build
    if ! docker buildx build \
            "${build_args[@]}" \
            --load \
            --platform linux/amd64 \
            -t "${smoke_tag}" \
            -f "${dockerfile}" \
            "${PROJECT_ROOT}/src" \
            2>&1; then
        echo -e "${RED}[FAIL]${NC} ${name}: docker build failed"
        FAILED+=("${name} (build failed)")
        return
    fi

    SMOKE_TAGS+=("${smoke_tag}")

    # Import smoke test
    if docker run \
            --rm \
            --platform linux/amd64 \
            --entrypoint python \
            "${smoke_tag}" \
            -c "import main; print('import OK')" \
            2>&1; then
        echo -e "${GREEN}[PASS]${NC} ${name}"
        PASSED+=("${name}")
    else
        echo -e "${RED}[FAIL]${NC} ${name}: import main failed"
        FAILED+=("${name} (import failed)")
    fi
}

# ── Filter and iterate ────────────────────────────────────────────────────────
matched=false
for service_def in "${RAG_SERVICES[@]}"; do
    IFS=':' read -r name path <<< "${service_def}"

    if [[ -n "${FILTER_SERVICE}" && "${name}" != "${FILTER_SERVICE}" ]]; then
        continue
    fi
    matched=true

    run_smoke "${name}" "${path}"
done

if [[ -n "${FILTER_SERVICE}" && "${matched}" == "false" ]]; then
    echo "Error: service '${FILTER_SERVICE}' not found in RAG_SERVICES" >&2
    valid=$(printf '%s\n' "${RAG_SERVICES[@]}" | cut -d: -f1 | sort | paste -sd ' ')
    echo "Valid names: ${valid}" >&2
    exit 1
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  Smoke-test summary"
echo "══════════════════════════════════════════════════"
echo -e "  Passed: ${GREEN}${#PASSED[@]}${NC}"
echo -e "  Failed: ${RED}${#FAILED[@]}${NC}"
echo ""

if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "Failed services:"
    for f in "${FAILED[@]}"; do
        echo -e "  ${RED}✗${NC} ${f}"
    done
    echo ""
    exit 1
fi

echo -e "  ${GREEN}All RAG images pass import smoke test.${NC}"
