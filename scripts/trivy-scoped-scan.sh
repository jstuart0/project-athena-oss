#!/usr/bin/env bash
# trivy-scoped-scan.sh — run Trivy against a built image and gate on
# CRITICAL/HIGH findings, with a narrow, mechanically-checked exception for
# packages pip vendors internally (pip/_vendor/) that no released pip has
# fixed yet (ATHENA-126).
#
# A blanket .trivyignore ID suppression (the original approach here, now
# removed per codex review) would silently hide a FUTURE real top-level
# dependency that happens to land on the same CVE ID/package/version — the
# gate would never fire again even though the risk profile changed
# completely. Instead this script parses Trivy's own JSON output and only
# treats a finding as pre-approved when ALL of (VulnerabilityID, PkgName,
# InstalledVersion) match one of the two rows below AND the package has no
# on-disk FilePath (i.e. Trivy found it only via pip's bundled CycloneDX
# SBOM, not a real installed distribution — `--list-all-pkgs` surfaces
# this). A real top-level package at the same name+version would carry a
# real FilePath and would NOT match — it fails the gate like anything else.
#
# Known vendored-pip exceptions today (re-check: `pip index versions pip`;
# remove a row once a released pip vendors the fix):
#   GHSA-6v7p-g79w-8964  msgpack      1.1.2   pip/_vendor/msgpack/
#   CVE-2025-47273       setuptools   70.3.0  pip/_vendor/pkg_resources/
#     (the vendored copy is pkg_resources — setuptools' companion package
#     and the module pip actually imports — even though pip's own
#     vendor.txt and Trivy's SBOM both track it under the "setuptools"
#     package name/version)
#
# Usage:
#   bash scripts/trivy-scoped-scan.sh <image> [--severity LIST] [--trivy-image REF]
#
# Exit codes:
#   0 — clean (besides the two documented vendored-pip rows above)
#   1 — at least one unallowlisted CRITICAL/HIGH finding
#   2 — the scan itself could not complete (docker/trivy invocation failed,
#       no parseable JSON, etc.) — never treated as a pass

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <image> [--severity LIST] [--trivy-image REF]" >&2
    exit 2
fi

IMAGE="$1"
shift

SEVERITY="CRITICAL,HIGH"
# Pinned to a fixed release, never :latest — a moving tag means the gate's
# behavior (and its vulnerability DB snapshot policy) can change out from
# under CI with no diff to review.
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy:0.74.0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --severity)
            SEVERITY="$2"
            shift 2
            ;;
        --trivy-image)
            TRIVY_IMAGE="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

TMP_JSON="$(mktemp)"
cleanup() { rm -f "${TMP_JSON}"; }
trap cleanup EXIT

if ! docker run --rm \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v trivy-scoped-scan-cache:/root/.cache/ \
        "${TRIVY_IMAGE}" image \
        --format json \
        --severity "${SEVERITY}" \
        --ignore-unfixed \
        --list-all-pkgs \
        "${IMAGE}" > "${TMP_JSON}"; then
    echo "FAIL: trivy scan did not complete for ${IMAGE}" >&2
    exit 2
fi

if ! python3 -c "import json; json.load(open('${TMP_JSON}'))" 2>/dev/null; then
    echo "FAIL: trivy produced no parseable JSON for ${IMAGE}" >&2
    exit 2
fi

python3 - "${TMP_JSON}" "${IMAGE}" <<'PY'
import json
import sys

path, image = sys.argv[1], sys.argv[2]

# (VulnerabilityID, PkgName, InstalledVersion) -> ALSO requires no on-disk
# FilePath (vendored-only) to be allowed. Keep this set to exactly the two
# rows documented in this script's header — do not widen it to a bare
# VulnerabilityID or PkgName match.
ALLOWED = {
    ("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2"),
    ("CVE-2025-47273", "setuptools", "70.3.0"),
}

with open(path) as f:
    data = json.load(f)

# FilePath lookup from --list-all-pkgs, keyed by (name, version). None
# means at least one occurrence of that (name, version) has no on-disk
# FilePath — i.e. it's the vendored-only instance a Vulnerability entry
# with no path of its own could be describing.
filepaths: dict[tuple[str, str], str | None] = {}
for result in data.get("Results", []):
    for pkg in result.get("Packages", []) or []:
        key = (pkg.get("Name"), pkg.get("Version"))
        fp = pkg.get("FilePath") or None
        if key not in filepaths or fp is None:
            filepaths[key] = fp

allowed_findings = []
failing_findings = []

for result in data.get("Results", []):
    for v in result.get("Vulnerabilities", []) or []:
        vuln_id = v.get("VulnerabilityID")
        pkg_name = v.get("PkgName")
        installed = v.get("InstalledVersion")
        key3 = (vuln_id, pkg_name, installed)
        fp = filepaths.get((pkg_name, installed))
        label = f"{pkg_name}=={installed} {vuln_id} (severity={v.get('Severity')})"
        if key3 in ALLOWED and fp is None:
            allowed_findings.append(label)
        else:
            if key3[:2] in {(a, b) for a, b, _ in ALLOWED}:
                label += " [DOES NOT MATCH the allowed vendored-copy predicate — real FilePath or version mismatch]"
            failing_findings.append(label)

print(f"=== {image} ===")
print(f"Allowed (documented pip-vendored copies): {len(allowed_findings)}")
for a in allowed_findings:
    print(f"   {a}")
print(f"Failing: {len(failing_findings)}")
for finding in failing_findings:
    print(f"   x {finding}")

sys.exit(1 if failing_findings else 0)
PY
