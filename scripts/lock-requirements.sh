#!/usr/bin/env bash
# lock-requirements.sh — compile every requirements.in in the repo into its
# locked requirements.txt with one wrapper, so no two locks are ever produced
# by a slightly different `uv pip compile` invocation.
#
# Usage:
#   bash scripts/lock-requirements.sh
#       Discover every requirements.in in the repo and (re)compile its lock.
#       Tolerates a partially-converted tree: directories with no
#       requirements.in yet are skipped with a printed notice, never an
#       error — this is expected between Phase 2 (one .in exists) and
#       Phase 8 (all 29 do).
#
#   bash scripts/lock-requirements.sh --upgrade
#       Same, but allows existing pins to move (otherwise uv preserves an
#       existing pin across a recompile — pip-tools semantics).
#
#   bash scripts/lock-requirements.sh --input FILE [--input FILE ...] \
#       --output FILE [--constraint FILE ...]
#       Compile exactly one pair from exactly the given inputs/constraints —
#       no shared-package auto-detection. Used by CI gates against scratch
#       fixtures, and for locking a single image directly.
#
#   bash scripts/lock-requirements.sh --check
#       Non-mutating: recompiles every discovered lock in the working tree,
#       diffs the result against what's committed, then restores the tree
#       regardless of outcome. Exits 1 if any requirements*.txt would change
#       (an .in was edited without recompiling) or if the tree was already
#       dirty when invoked (refuses to run against uncommitted state, and
#       won't overwrite the pre-existing .gitignore edit intake left behind).
#
# Must run from the repo root — uv's "# via" annotations only stay portable
# (relative paths, not this machine's scratch path) when compiled from here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

UV_MIN_MAJOR=0
UV_MIN_MINOR=10

require_uv() {
    if ! command -v uv >/dev/null 2>&1; then
        echo "FAIL: uv is not on PATH (need >= ${UV_MIN_MAJOR}.${UV_MIN_MINOR})" >&2
        exit 1
    fi
    local ver major minor
    ver="$(uv --version | awk '{print $2}')"
    major="${ver%%.*}"
    minor="${ver#*.}"
    minor="${minor%%.*}"
    if [[ "${major}" -lt "${UV_MIN_MAJOR}" ]] || { [[ "${major}" -eq "${UV_MIN_MAJOR}" ]] && [[ "${minor}" -lt "${UV_MIN_MINOR}" ]]; }; then
        echo "FAIL: uv ${ver} found, need >= ${UV_MIN_MAJOR}.${UV_MIN_MINOR}" >&2
        exit 1
    fi
}
require_uv

# Directories whose image does NOT install src/shared (Decision 7b): their
# compile has no shared co-input. Everyone else gets src/shared/pyproject.toml
# compiled alongside their own .in (Context: "uv pip compile
# src/shared/pyproject.toml admin/backend/requirements.txt").
NO_SHARED_DIRS=("apps/chat-embed" "apps/jarvis-web/backend")

is_no_shared_dir() {
    local d="$1"
    for n in "${NO_SHARED_DIRS[@]}"; do
        [[ "${d}" == "${n}" ]] && return 0
    done
    return 1
}

UPGRADE=""

compile_pair() {
    # $1 = output path; remaining args = uv pip compile inputs/constraints
    local output="$1"
    shift
    local upgrade_args=()
    if [[ -n "${UPGRADE}" ]]; then
        upgrade_args=(--upgrade)
    fi
    uv pip compile "$@" \
        --output-file "${output}" \
        --python-version 3.11 \
        --python-platform x86_64-unknown-linux-gnu \
        --generate-hashes \
        --custom-compile-command "make lock" \
        "${upgrade_args[@]}" \
        --quiet
}

discover_ins() {
    find . -name "requirements.in" \
        -not -path "./node_modules/*" -not -path "./.git/*" -not -path "./.mozart/*" \
        | sed 's|^\./||' | sort
}

compile_all() {
    local found=0
    local in_path
    while IFS= read -r in_path; do
        [[ -z "${in_path}" ]] && continue
        found=1
        local dir base out
        dir="$(dirname "${in_path}")"
        base="$(basename "${in_path}")"
        out="${dir}/${base%.in}.txt"
        if [[ "${base}" == "requirements-test.in" ]]; then
            # Constrained by the sibling production lock — never an
            # independent resolve that could silently diverge from it.
            compile_pair "${out}" "${in_path}" -c "${dir}/requirements.txt"
        elif is_no_shared_dir "${dir}"; then
            compile_pair "${out}" "${in_path}"
        else
            compile_pair "${out}" "src/shared/pyproject.toml" "${in_path}"
        fi
        echo "compiled: ${out}"
    done < <(discover_ins)
    if [[ "${found}" -eq 0 ]]; then
        echo "NOTE: no requirements.in found yet — nothing to compile (expected on a partially-converted tree)"
    fi
}

MODE="discover"
INPUTS=()
CONSTRAINTS=()
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)
            INPUTS+=("$2")
            MODE="pair"
            shift 2
            ;;
        --output)
            OUTPUT="$2"
            shift 2
            ;;
        --constraint)
            CONSTRAINTS+=("-c" "$2")
            shift 2
            ;;
        --upgrade)
            UPGRADE=1
            shift
            ;;
        --check)
            MODE="check"
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

case "${MODE}" in
    pair)
        if [[ -z "${OUTPUT}" || ${#INPUTS[@]} -eq 0 ]]; then
            echo "FAIL: --input requires --output" >&2
            exit 1
        fi
        compile_pair "${OUTPUT}" "${INPUTS[@]}" "${CONSTRAINTS[@]}"
        ;;
    discover)
        compile_all
        ;;
    check)
        # Excludes .gitignore from the dirty-tree guard: this worktree
        # carries one pre-existing uncommitted line there from campaign
        # intake (the .mozart/ ignore entry), which is deliberately left for
        # the phase commit rather than this script's problem to fix or hide.
        if ! git diff --quiet -- . ':!.gitignore'; then
            echo "FAIL: dirty tree (excluding .gitignore), refusing to run --check" >&2
            exit 1
        fi
        # compile_all can abort partway (set -e) if any single compile_pair
        # fails — a trap, not fall-through, is what guarantees the restore
        # still runs and no partially-regenerated locks are left in the tree
        # for a later `git add -A` to pick up.
        restore_locks() {
            if [[ -n "$(git ls-files -- '*/requirements*.txt' 2>/dev/null)" ]]; then
                git checkout -- '*/requirements*.txt' 2>/dev/null \
                    || echo "WARNING: could not restore requirements*.txt after --check — the tree may be left dirty" >&2
            fi
        }
        trap restore_locks EXIT
        compile_all
        RC=0
        git diff --exit-code -- '*/requirements*.txt' || RC=1
        if [[ "${RC}" -ne 0 ]]; then
            echo "FAIL: a requirements.in was edited without recompiling its lock"
            exit 1
        fi
        echo "LOCK MATCHES SPEC OK"
        exit 0
        ;;
esac
