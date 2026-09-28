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
# InstalledVersion) match one of the two rows below AND the SPECIFIC
# package instance that vulnerability was reported against (joined by
# Trivy's own PkgIdentifier.UID / Identifier.UID, falling back to PkgPath
# when no UID is present) has no on-disk FilePath — i.e. Trivy found it
# only via pip's bundled CycloneDX SBOM, not a real installed distribution.
# Joining by (name, version) alone is NOT enough (codex r2): if pip's
# vendored copy AND a real top-level package share the exact same
# name+version, a same-ID/name/version finding for the REAL copy must
# still fail even though a same-ID/name/version finding for the vendored
# copy is allowed — so this script also fails closed if ANY package
# sharing that (name, version) anywhere in the scan carries a FilePath,
# not just the UID-resolved instance for this specific finding.
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

# (VulnerabilityID, PkgName, InstalledVersion) -> ALSO requires the
# UID-resolved package instance to have no on-disk FilePath, AND no
# sibling package at the same (name, version) anywhere in the scan to
# have one either. Keep this set to exactly the two rows documented in
# this script's header — do not widen it to a bare VulnerabilityID or
# PkgName match.
ALLOWED = {
    ("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2"),
    ("CVE-2025-47273", "setuptools", "70.3.0"),
}

try:
    with open(path) as f:
        data = json.load(f)

    # `data.get("Results", [])` cannot distinguish "genuinely zero results"
    # from "no Results key at all" -- both produce an empty list, so a
    # malformed report missing the key entirely (e.g. `{}`) would silently
    # print "Failing: 0" and exit 0 (codex r3). "Results" must be present
    # AND a list before this scan is accepted as a real Trivy report.
    if "Results" not in data:
        raise KeyError("'Results' key is missing from the trivy report")
    results = data["Results"]
    if not isinstance(results, list):
        raise TypeError(f"'Results' is {type(results).__name__}, expected a list")

    # Packages keyed by Identifier.UID — the exact instance a Vulnerability
    # entry's PkgIdentifier.UID resolves to. Also track, per (name,
    # version), whether ANY package instance anywhere in the scan carries
    # a real on-disk FilePath — a sibling real copy at the same
    # name+version must block the vendored copy's own finding too, even
    # though the UID-resolved instance for that specific finding is
    # vendored-only.
    packages_by_uid: dict[str, dict] = {}
    has_real_path_for_name_version: set[tuple[str, str]] = set()

    for result in results:
        for pkg in result.get("Packages", []) or []:
            uid = (pkg.get("Identifier") or {}).get("UID")
            name, version = pkg.get("Name"), pkg.get("Version")
            fp = pkg.get("FilePath") or None
            if uid:
                packages_by_uid[uid] = pkg
            if fp:
                has_real_path_for_name_version.add((name, version))

    allowed_findings = []
    failing_findings = []

    for result in results:
        for v in result.get("Vulnerabilities", []) or []:
            vuln_id = v.get("VulnerabilityID")
            pkg_name = v.get("PkgName")
            installed = v.get("InstalledVersion")
            key3 = (vuln_id, pkg_name, installed)
            label = f"{pkg_name}=={installed} {vuln_id} (severity={v.get('Severity')})"

            # Resolve THIS finding's specific package instance: exact join
            # by PkgIdentifier.UID first, falling back to the
            # vulnerability's own PkgPath field when no UID is present. If
            # neither resolves, we cannot prove this is the vendored-only
            # instance — treat as unresolved, never as "allowed".
            pkg_uid = (v.get("PkgIdentifier") or {}).get("UID")
            resolved_pkg = packages_by_uid.get(pkg_uid) if pkg_uid else None
            if resolved_pkg is not None:
                unresolved = False
                instance_filepath = resolved_pkg.get("FilePath") or None
            elif v.get("PkgPath"):
                unresolved = False
                instance_filepath = v.get("PkgPath")
            else:
                unresolved = True
                instance_filepath = None

            is_vendored_instance = (not unresolved) and instance_filepath is None
            has_sibling_real_copy = (pkg_name, installed) in has_real_path_for_name_version

            if key3 in ALLOWED and is_vendored_instance and not has_sibling_real_copy:
                allowed_findings.append(label)
            else:
                if key3 in ALLOWED:
                    reasons = []
                    if unresolved:
                        reasons.append(
                            "could not resolve the specific package instance "
                            "(no PkgIdentifier.UID or PkgPath match) -- cannot confirm vendored-only"
                        )
                    elif not is_vendored_instance:
                        reasons.append(f"resolved package instance has a real FilePath ({instance_filepath})")
                    if has_sibling_real_copy:
                        reasons.append(
                            "a real (non-vendored) copy of this name+version is present "
                            "elsewhere in the image"
                        )
                    label += f" [DOES NOT MATCH the allowed vendored-copy predicate: {'; '.join(reasons)}]"
                failing_findings.append(label)

    print(f"=== {image} ===")
    print(f"Allowed (documented pip-vendored copies): {len(allowed_findings)}")
    for a in allowed_findings:
        print(f"   {a}")
    print(f"Failing: {len(failing_findings)}")
    for finding in failing_findings:
        print(f"   x {finding}")

    sys.exit(1 if failing_findings else 0)

except (AttributeError, TypeError, KeyError) as e:
    # The JSON parsed (bash already checked that), but its shape doesn't
    # match what this script expects -- a tool/schema problem, not a
    # security finding. Must exit 2 (scan error), never 1 (would read as
    # "gate correctly failed on real findings") or 0 (would read as clean).
    print(f"FAIL: unexpected JSON schema from trivy for {image}: {e}", file=sys.stderr)
    sys.exit(2)
PY
