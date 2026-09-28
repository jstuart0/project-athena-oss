#!/usr/bin/env bash
# list-python-images.sh — emit every Python image's {name, dockerfile,
# context, needs_shared_copy} as a JSON array on stdout, sourced from
# service-defs.sh — the same enumeration build-and-push.sh and
# audit-images.sh already share. Single source of truth for "what are the
# 29 Python images", consumed by .github/workflows/image-scan.yml to build
# its scan matrix.
#
# Usage: bash scripts/list-python-images.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # consumed by service-defs.sh's ADMIN_SERVICES
# array expansion after sourcing, not referenced directly in this file.
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=service-defs.sh
source "${SCRIPT_DIR}/service-defs.sh"

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

python3 - "${IMAGES[@]}" <<'PY'
import json
import sys

rows = []
for entry in sys.argv[1:]:
    name, dockerfile, context, needs_shared_copy = entry.split("|")
    rows.append(
        {
            "name": name,
            "dockerfile": dockerfile,
            "context": context,
            "needs_shared_copy": needs_shared_copy == "1",
        }
    )
print(json.dumps(rows))
PY
