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
#   signing API. GUARD (mechanical, AST-based — not text matching: it
#   resolves every python-jose import form to a canonical name, so it also
#   catches an aliased import, a `getattr`-dispatched call, a call that omits
#   `algorithms=` entirely — which python-jose's jwt.decode() then accepts
#   any `alg` the token header claims — and a `JWT_ALGORITHM` reassignment,
#   none of which a grep for "algorithms=[" can see): runs
#   scripts/check-jwt-algorithm-guard.py before this allowlist is ever
#   applied, scanning admin/backend (excluding admin/backend/tests/, where
#   fixtures legitimately construct dangerous jose calls to prove other code
#   rejects them) plus src/shared (installed into the admin-backend image).
#   If the guard fails, the --ignore-vuln flag is withheld for the whole
#   run — PYSEC-2026-1325 then surfaces as an unallowlisted finding rather
#   than being silently suppressed.

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

# Mechanical precondition for the PYSEC-2026-1325 allowlist (see header
# comment). Runs once, against source on disk — not per image, since it's a
# property of the app code, not the built artifact. GUARD_RC:
#   0 — holds; audit_one's ignore_vuln_flag carries the allowlist.
#   1 — a real finding (the HS256-only assumption doesn't hold, or isn't
#       statically verifiable); the allowlist is withheld for this run.
#   2 — the check itself couldn't run; this script cannot safely proceed.
GUARD_RC=0
GUARD_OUTPUT="$(python3 "${SCRIPT_DIR}/check-jwt-algorithm-guard.py" --root "${PROJECT_ROOT}" 2>&1)" || GUARD_RC=$?
echo "${GUARD_OUTPUT}"
if [[ "${GUARD_RC}" -eq 2 ]]; then
    echo "TOOL ERROR: JWT algorithm guard could not run — refusing to audit" >&2
    exit 2
fi
if [[ "${GUARD_RC}" -ne 0 ]]; then
    echo "WARNING: JWT algorithm guard failed — PYSEC-2026-1325 allowlist withheld for this run" >&2
fi

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
    # Under `set -u` on bash 3.2 (macOS's stock /bin/bash), expanding
    # "${arr[@]}" on a still-empty array is an unbound-variable error, not an
    # empty expansion -- the classic bash-3-vs-4 array gap. An error here
    # inside an EXIT trap clobbers the script's real exit code with the
    # trap's, silently turning a real failure (or a real pass) into whatever
    # this cleanup happened to return. The `+` parameter-expansion form
    # short-circuits before the array ever gets indexed when it's empty.
    for d in "${SHARED_COPY_DIRS[@]+"${SHARED_COPY_DIRS[@]}"}"; do
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
    rm -f "${out_dir}/a.json" "${out_dir}/frozen.txt" "${out_dir}/pip-audit.rc"

    # Fidelity (ian's finding): pip-audit must never be installed INTO the
    # audited environment -- that mutates it (can upgrade a shared dep to
    # satisfy pip-audit's own requirements) and adds pip-audit's own
    # transitive deps to the very site-packages being measured. Instead:
    # freeze the target environment's installed packages from its own
    # python (--exclude-editable drops the local `-e /app/shared` entry,
    # which isn't a pip-audit-resolvable spec and carries no PyPI CVEs of
    # its own), then audit that frozen list from a throwaway venv that
    # never touches the target env. pip-audit's exit code is captured to a
    # sidecar file rather than swallowed with `|| true`, so a tool crash
    # can't be misread as an empty-but-valid JSON result.
    local container_name="athena63-p6-audit-${name}"
    docker rm -f "${container_name}" >/dev/null 2>&1 || true
    # Passed as a container env var, not interpolated into the script text,
    # so the single-quoted heredoc-style script below needs no nested
    # quoting and $? etc. stay literal for the container's own shell.
    local ignore_vuln_flag=""
    if [[ "${GUARD_RC}" -eq 0 ]]; then
        ignore_vuln_flag="--ignore-vuln ${IGNORE_VULN}"
    fi
    local docker_rc=0
    docker run --rm -i --platform linux/amd64 --name "${container_name}" \
        -e IGNORE_VULN_FLAG="${ignore_vuln_flag}" \
        -v "${out_dir}:/audit-out" --entrypoint sh "${audit_tag}" -c '
        set -e
        python3 -m pip freeze --all --exclude-editable > /audit-out/frozen.txt
        python3 -m venv /audit-venv
        /audit-venv/bin/pip install --no-cache-dir -q --upgrade pip
        /audit-venv/bin/pip install --no-cache-dir -q pip-audit
        set +e
        /audit-venv/bin/pip-audit -r /audit-out/frozen.txt --format json $IGNORE_VULN_FLAG --output /audit-out/a.json
        echo $? > /audit-out/pip-audit.rc
        exit 0
    ' || docker_rc=$?
    if [[ "${docker_rc}" -ne 0 ]]; then
        echo "TOOL ERROR: ${name}: audit container exited ${docker_rc} (venv/pip-audit-install step failed, before pip-audit itself ran)"
        TOOL_ERRORS+=("${name} (container error, rc=${docker_rc})")
        rm -rf "${out_dir}"
        return
    fi

    if [[ ! -s "${out_dir}/pip-audit.rc" ]]; then
        echo "TOOL ERROR: ${name}: no pip-audit exit-code sidecar written — INCONCLUSIVE, not clean"
        TOOL_ERRORS+=("${name} (no pip-audit.rc sidecar)")
        rm -rf "${out_dir}"
        return
    fi
    local pip_audit_rc
    pip_audit_rc="$(cat "${out_dir}/pip-audit.rc")"

    local verdict
    verdict="$(python3 - "${out_dir}/a.json" "${name}" "${pip_audit_rc}" <<'PY'
import json, sys
path, name, rc_str = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    rc = int(rc_str)
except ValueError:
    print(f"TOOL_ERROR {name}: pip-audit exit code sidecar was not an integer: {rc_str!r}")
    sys.exit(0)
# pip-audit's own contract: 0 = clean, 1 = vulnerabilities found. Any other
# code (dependency-resolution crash, network failure, bad args, ...) means
# the JSON on disk -- if any -- cannot be trusted as a complete result.
if rc not in (0, 1):
    print(f"TOOL_ERROR {name}: pip-audit exited {rc} (not 0=clean or 1=findings) -- result not trustworthy")
    sys.exit(0)
try:
    with open(path) as f:
        d = json.load(f)
except Exception as e:
    print(f"TOOL_ERROR {name}: pip-audit exited {rc} but produced no parseable JSON: {e}")
    sys.exit(0)
# De-duplicated on (package, version, advisory id): pip-audit's JSON can
# list the same advisory more than once for one dependency (e.g. the same
# ID reachable through more than one alias/source in its own vuln record),
# and/or the same dependency can appear more than once in `dependencies`
# for a resolver-internal reason. Neither is a second distinct finding —
# counting them as one each would report N discovered advisories as some
# multiple of N and make "5 distinct PYSECs" read as 10.
seen = set()
found = []
for dep in d.get("dependencies", []):
    for v in dep.get("vulns", []):
        key = (dep.get("name"), dep.get("version"), v.get("id"))
        if key in seen:
            continue
        seen.add(key)
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

ALLOWLIST_STATUS="${IGNORE_VULN} (guard held)"
if [[ "${GUARD_RC}" -ne 0 ]]; then
    ALLOWLIST_STATUS="none — guard failed, ${IGNORE_VULN} was NOT allowlisted this run"
fi

echo ""
echo "══════════════════════════════════════════════════"
echo "  Image audit summary (scope: ${SCOPE}, allowlist: ${ALLOWLIST_STATUS})"
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
