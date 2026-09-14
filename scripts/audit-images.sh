#!/usr/bin/env bash
# audit-images.sh — build a Python image and run pip-audit INSIDE it, so the
# advisory surface reported is the one the pinned build tooling (Decision 6)
# and the committed lock actually produce, not a throwaway venv's guess (X5).
#
# Exit codes (three-valued, never collapse "no findings" and "couldn't run"):
#   0 — every audited image is clean (besides the one allowlisted residual).
#   1 — at least one image has an unallowlisted advisory. Findings, not a bug.
#   2 — the audit itself could not complete (docker build failed, pip-audit
#       produced no parseable JSON, etc.). NEVER treated as a pass.
#
# Usage:
#   bash scripts/audit-images.sh                  # all 29 Python images
#   bash scripts/audit-images.sh --scope remediated  # only the two images
#                                                     # this campaign fixed
#   bash scripts/audit-images.sh --service <name>  # one image by name
#
# One allowlisted residual, exactly one --ignore-vuln, reason inline:
#   PYSEC-2026-1325 (ecdsa, pulled in transitively by python-jose[cryptography]).
#   Unreachable: this codebase's only python-jose usage is HS256 (symmetric
#   HMAC) — see admin/backend/app/auth/oidc.py — which never calls ecdsa's
#   signing API. GUARD: if `algorithms=[...]` is ever widened to an EC
#   algorithm (ES256/384/512) this exception no longer holds and python-jose
#   must be replaced. grep -rn "algorithms=\[" admin/backend/app to check.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=service-defs.sh
source "${SCRIPT_DIR}/service-defs.sh"

IGNORE_VULN="PYSEC-2026-1325"

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

REMEDIATED_SCOPE=("athena-admin-backend" "athena-jarvis-web")

SCOPE="all"
FILTER_SERVICE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scope)
            SCOPE="$2"
            shift 2
            ;;
        --service)
            FILTER_SERVICE="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--scope remediated] [--service <name>]" >&2
            exit 2
            ;;
    esac
done

if [[ "${SCOPE}" != "all" && "${SCOPE}" != "remediated" ]]; then
    echo "FAIL: unknown --scope '${SCOPE}' (expected 'all' or 'remediated')" >&2
    exit 2
fi

is_remediated() {
    local n="$1"
    for r in "${REMEDIATED_SCOPE[@]}"; do
        [[ "${n}" == "${r}" ]] && return 0
    done
    return 1
}

CLEAN=()
FINDINGS=()
TOOL_ERRORS=()
SHARED_COPY_DIRS=()

cleanup() {
    for d in "${SHARED_COPY_DIRS[@]}"; do
        rm -rf "$d"
    done
}
trap cleanup EXIT

audit_one() {
    local name="$1" dockerfile_rel="$2" context_rel="$3" needs_shared_copy="$4"
    local audit_tag="${name}:audit"
    local dockerfile="${PROJECT_ROOT}/${dockerfile_rel}"
    local context="${PROJECT_ROOT}"
    if [[ "${context_rel}" != "." ]]; then
        context="${PROJECT_ROOT}/${context_rel}"
    fi

    if [[ ! -f "${dockerfile}" ]]; then
        echo "TOOL ERROR: ${name}: Dockerfile not found at ${dockerfile_rel}"
        TOOL_ERRORS+=("${name} (no Dockerfile)")
        return
    fi

    echo ""
    echo "── ${name} ──"

    if [[ "${needs_shared_copy}" == "1" ]]; then
        local shared_copy_dir="${context}/shared"
        rm -rf "${shared_copy_dir}"
        cp -r "${PROJECT_ROOT}/src/shared" "${shared_copy_dir}"
        SHARED_COPY_DIRS+=("${shared_copy_dir}")
    fi

    if ! docker buildx build --load --platform linux/amd64 \
            -t "${audit_tag}" -f "${dockerfile}" "${context}" 2>&1; then
        echo "TOOL ERROR: ${name}: docker build failed"
        TOOL_ERRORS+=("${name} (build failed)")
        return
    fi

    # colima shares only $HOME with the VM; a /tmp (or macOS TMPDIR) mktemp
    # dir bind-mounts as silently empty inside the container. Scratch space
    # for this bind mount must live under $HOME.
    local out_dir
    mkdir -p "${HOME}/.athena-audit-images"
    out_dir="$(mktemp -d "${HOME}/.athena-audit-images/audit.XXXXXX")"
    RC=0
    docker run --rm --platform linux/amd64 \
        -v "${out_dir}:/audit-out" --entrypoint sh "${audit_tag}" -c "
        set -e
        pip install --no-cache-dir -q pip-audit 2>/dev/null || true
        pip-audit --format json --ignore-vuln ${IGNORE_VULN} --output /audit-out/a.json || true
    " || RC=$?
    if [[ "${RC}" -ne 0 ]]; then
        echo "TOOL ERROR: ${name}: audit container exited ${RC}"
        TOOL_ERRORS+=("${name} (container error)")
        rm -rf "${out_dir}"
        return
    fi

    local verdict
    verdict="$(python3 - "${out_dir}/a.json" "${name}" <<'PY'
import json, sys
path, name = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        d = json.load(f)
except Exception as e:
    print(f"TOOL_ERROR {name}: pip-audit produced no parseable JSON: {e}")
    sys.exit(0)
found = []
for dep in d.get("dependencies", []):
    for v in dep.get("vulns", []):
        found.append(f"{dep.get('name')}=={dep.get('version')}:{v.get('id')}")
if found:
    print(f"FINDINGS {name}: " + ", ".join(found))
else:
    print(f"CLEAN {name}")
PY
)"
    echo "${verdict}"
    rm -rf "${out_dir}"

    case "${verdict}" in
        CLEAN*) CLEAN+=("${name}") ;;
        FINDINGS*) FINDINGS+=("${verdict}") ;;
        TOOL_ERROR*) TOOL_ERRORS+=("${verdict}") ;;
        *) TOOL_ERRORS+=("${name} (unrecognised verdict: ${verdict})") ;;
    esac
}

matched=false
for def in "${IMAGES[@]}"; do
    IFS='|' read -r name dockerfile_rel context_rel needs_shared_copy <<< "${def}"

    if [[ -n "${FILTER_SERVICE}" && "${name}" != "${FILTER_SERVICE}" ]]; then
        continue
    fi
    if [[ "${SCOPE}" == "remediated" ]] && ! is_remediated "${name}"; then
        continue
    fi
    matched=true

    audit_one "${name}" "${dockerfile_rel}" "${context_rel}" "${needs_shared_copy}"
done

if [[ "${matched}" == "false" ]]; then
    echo "FAIL: no images matched (scope=${SCOPE}, service=${FILTER_SERVICE})" >&2
    exit 2
fi

echo ""
echo "══════════════════════════════════════════════════"
echo "  Image audit summary (scope: ${SCOPE}, allowlist: ${IGNORE_VULN})"
echo "══════════════════════════════════════════════════"
echo "  Clean: ${#CLEAN[@]}"
echo "  Findings: ${#FINDINGS[@]}"
echo "  Tool errors: ${#TOOL_ERRORS[@]}"
echo ""

if [[ ${#TOOL_ERRORS[@]} -gt 0 ]]; then
    echo "Tool errors (audit did not complete — NOT a clean result):"
    for e in "${TOOL_ERRORS[@]}"; do
        echo "  ! ${e}"
    done
    exit 2
fi

if [[ ${#FINDINGS[@]} -gt 0 ]]; then
    echo "Unallowlisted findings:"
    for f in "${FINDINGS[@]}"; do
        echo "  x ${f}"
    done
    exit 1
fi

echo "ALL AUDITED IMAGES CLEAN (besides ${IGNORE_VULN}, allowlisted above)"
exit 0
