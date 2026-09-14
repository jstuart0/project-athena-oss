#!/usr/bin/env bash
# smoke-images.sh — build every Python image in the repo and verify it
# imports cleanly and has a consistent dependency set (pip check).
#
# Generalizes scripts/smoke-rag-images.sh (23 RAG images) to all 29 Python
# images: the 23 RAG images plus admin-backend, chat-embed, jarvis-web,
# gateway, mode-service and orchestrator. admin/frontend (FROM nginx:alpine)
# is the repo's only non-Python Dockerfile and is declared as an exclusion
# rather than silently absent — see --list-excluded.
#
# Execution model: FULL SWEEP, not fail-fast — same rationale as
# smoke-rag-images.sh: a single bad-import PR typically triggers more than
# one failure, so surfacing them all in one pass beats N fix-rebuild cycles.
#
# Usage:
#   bash scripts/smoke-images.sh                  # build+smoke all 29 images
#   bash scripts/smoke-images.sh --service <name> # one image (e.g. athena-chat-embed)
#   bash scripts/smoke-images.sh --keep-images    # don't remove :smoke tags after run
#   bash scripts/smoke-images.sh --list           # one repo-root-relative Dockerfile path per line
#   bash scripts/smoke-images.sh --list-excluded  # declared non-Python exclusions
#   bash scripts/smoke-images.sh --dry-run        # "PLAN <image> <context> <dockerfile>" per image, nothing else on stdout

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=service-defs.sh
source "${SCRIPT_DIR}/service-defs.sh"

# ── Build the combined 29-image population ────────────────────────────────
# Each row: name|dockerfile_relpath|context_relpath|needs_shared_copy
IMAGES=()
for def in "${SPECIAL_PYTHON_IMAGES[@]}"; do
    IMAGES+=("${def}")
done
for service_def in "${CORE_SRC_SERVICES[@]}"; do
    IFS=':' read -r name path <<< "${service_def}"
    IMAGES+=("${name}|src/${path}/Dockerfile|src|0")
done
for service_def in "${RAG_SERVICES[@]}"; do
    IFS=':' read -r name path <<< "${service_def}"
    IMAGES+=("${name}|src/${path}/Dockerfile|src|0")
done

# ── Argument parsing ──────────────────────────────────────────────────────
FILTER_SERVICE=""
KEEP_IMAGES=false
MODE="run"

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
        --list)
            MODE="list"
            shift
            ;;
        --list-excluded)
            MODE="list-excluded"
            shift
            ;;
        --dry-run)
            MODE="dry-run"
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--service <image-name>] [--keep-images] [--list] [--list-excluded] [--dry-run]" >&2
            exit 1
            ;;
    esac
done

# ── Non-executing modes ────────────────────────────────────────────────────
if [[ "${MODE}" == "list" ]]; then
    for def in "${IMAGES[@]}"; do
        IFS='|' read -r _ dockerfile_rel _ _ <<< "${def}"
        echo "${dockerfile_rel}"
    done | sort
    exit 0
fi

if [[ "${MODE}" == "list-excluded" ]]; then
    for f in "${NON_PYTHON_DOCKERFILES[@]}"; do
        echo "${f}"
    done
    exit 0
fi

if [[ "${MODE}" == "dry-run" ]]; then
    for def in "${IMAGES[@]}"; do
        IFS='|' read -r name dockerfile_rel context_rel _ <<< "${def}"
        if [[ -n "${FILTER_SERVICE}" && "${name}" != "${FILTER_SERVICE}" ]]; then
            continue
        fi
        echo "PLAN ${name} ${context_rel} ${dockerfile_rel}"
    done
    exit 0
fi

# ── Colours ───────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ── Results tracking ────────────────────────────────────────────────────────
PASSED=()
FAILED=()
SMOKE_TAGS=()
SHARED_COPY_DIRS=()

cleanup() {
    if [[ "${KEEP_IMAGES}" == "false" && ${#SMOKE_TAGS[@]} -gt 0 ]]; then
        echo ""
        echo "Removing :smoke tags..."
        for tag in "${SMOKE_TAGS[@]}"; do
            docker rmi "$tag" 2>/dev/null || true
        done
    fi
    # Same bash-3.2 `set -u` empty-array gap fixed in audit-images.sh's
    # cleanup() this round: "${arr[@]}" on a still-empty array is an
    # unbound-variable error on macOS's stock /bin/bash, not an empty
    # expansion, which would clobber this script's real exit code with the
    # trap's. Every image with needs_shared_copy=0 (all RAG services,
    # gateway, mode_service, orchestrator, chat-embed, jarvis-web) leaves
    # this array empty, so a single-service run hit this on every one of
    # them.
    for d in "${SHARED_COPY_DIRS[@]+"${SHARED_COPY_DIRS[@]}"}"; do
        rm -rf "$d"
    done
}
trap cleanup EXIT

# ── Main loop ─────────────────────────────────────────────────────────────
run_smoke() {
    local name="$1" dockerfile_rel="$2" context_rel="$3" needs_shared_copy="$4"
    local smoke_tag="${name}:smoke"
    local dockerfile="${PROJECT_ROOT}/${dockerfile_rel}"
    local context="${PROJECT_ROOT}"
    if [[ "${context_rel}" != "." ]]; then
        context="${PROJECT_ROOT}/${context_rel}"
    fi

    if [[ ! -f "$dockerfile" ]]; then
        echo -e "${YELLOW}[SKIP]${NC} ${name}: Dockerfile not found at ${dockerfile_rel}"
        FAILED+=("${name} (no Dockerfile)")
        return
    fi

    echo ""
    echo "── ${name} ──"

    local shared_copy_dir=""
    if [[ "${needs_shared_copy}" == "1" ]]; then
        # Replicates build-and-push.sh's admin-backend special case: the
        # image's build context doesn't naturally reach src/shared, so it is
        # copied in before the build and removed unconditionally after.
        shared_copy_dir="${context}/shared"
        rm -rf "${shared_copy_dir}"
        cp -r "${PROJECT_ROOT}/src/shared" "${shared_copy_dir}"
        SHARED_COPY_DIRS+=("${shared_copy_dir}")
    fi

    if ! docker buildx build \
            --load \
            --platform linux/amd64 \
            -t "${smoke_tag}" \
            -f "${dockerfile}" \
            "${context}" \
            2>&1; then
        echo -e "${RED}[FAIL]${NC} ${name}: docker build failed"
        FAILED+=("${name} (build failed)")
        return
    fi

    SMOKE_TAGS+=("${smoke_tag}")

    local import_env=()
    if [[ "${name}" == "athena-admin-backend" ]]; then
        # DEV_MODE bypasses admin-backend's OIDC/DB startup gates, which
        # otherwise raise SystemExit at import time (admin/backend/main.py) —
        # see CLAUDE.md "Admin-backend startup gates". Without it, every
        # import smoke of this image fails for a reason unrelated to what
        # this harness checks (dependency consistency).
        import_env=(-e DEV_MODE=true)
    elif [[ "${name}" == "athena-chat-embed" ]]; then
        # apps/chat-embed/main.py reads os.environ["ATHENA_CHAT_URL"] with no
        # default at import time (its docstring documents it as required).
        # The value is never dialed during a plain import — only when a
        # request handler runs — so any non-empty placeholder is fine here.
        import_env=(-e ATHENA_CHAT_URL=http://smoke-test.invalid)
    fi

    # Import smoke test
    if ! docker run \
            --rm \
            --platform linux/amd64 \
            "${import_env[@]}" \
            --entrypoint python \
            "${smoke_tag}" \
            -c "import main; print('import OK')" \
            2>&1; then
        echo -e "${RED}[FAIL]${NC} ${name}: import main failed"
        FAILED+=("${name} (import failed)")
        return
    fi

    # pip check — the check this campaign exists to make CI enforce.
    if docker run \
            --rm \
            --platform linux/amd64 \
            "${import_env[@]}" \
            --entrypoint python \
            "${smoke_tag}" \
            -m pip check \
            2>&1; then
        echo -e "${GREEN}[PASS]${NC} ${name}"
        PASSED+=("${name}")
    else
        echo -e "${RED}[FAIL]${NC} ${name}: pip check failed"
        FAILED+=("${name} (pip check failed)")
    fi
}

# ── Filter and iterate ────────────────────────────────────────────────────
matched=false
for def in "${IMAGES[@]}"; do
    IFS='|' read -r name dockerfile_rel context_rel needs_shared_copy <<< "${def}"

    if [[ -n "${FILTER_SERVICE}" && "${name}" != "${FILTER_SERVICE}" ]]; then
        continue
    fi
    matched=true

    run_smoke "${name}" "${dockerfile_rel}" "${context_rel}" "${needs_shared_copy}"
done

if [[ -n "${FILTER_SERVICE}" && "${matched}" == "false" ]]; then
    echo "Error: service '${FILTER_SERVICE}' not found" >&2
    valid=$(for def in "${IMAGES[@]}"; do IFS='|' read -r n _ <<< "${def}"; echo "$n"; done | sort | paste -sd ' ')
    echo "Valid names: ${valid}" >&2
    exit 1
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  Smoke-test summary"
echo "══════════════════════════════════════════════════"
echo -e "  Passed: ${GREEN}${#PASSED[@]}${NC}"
echo -e "  Failed: ${RED}${#FAILED[@]}${NC}"
echo ""

if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "Failed images:"
    for f in "${FAILED[@]}"; do
        echo -e "  ${RED}✗${NC} ${f}"
    done
    echo ""
    exit 1
fi

echo -e "  ${GREEN}All Python images pass import + pip check.${NC}"
exit 0
