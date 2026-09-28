"""ATHENA-126 (codex review) — tests for scripts/trivy-scoped-scan.sh's
allow-predicate.

The original approach was a blanket `.trivyignore` ID suppression for two
findings that are actually pip's own internally-vendored msgpack/setuptools
copies (no fix available in any released pip). codex r1 flagged that a bare
VulnerabilityID allowlist would silently swallow a FUTURE finding too — if
a real top-level dependency ever lands on the exact same CVE ID (a
plausible coincidence: e.g. an app adds a real `msgpack` dependency that
happens to also be pinned at 1.1.2), the blanket ID ignore hides it
forever with zero diff to review.

codex r2 found the r1 replacement predicate was ITSELF still bypassable:
it kept a `(name, version) -> FilePath` map where a single vendored
(FilePath=None) occurrence unconditionally overwrote any real (FilePath
set) occurrence at the same name+version, so a real top-level msgpack
installed ALONGSIDE pip's vendored copy was still silently allowed. The
predicate now joins each Vulnerability to its EXACT Package instance via
Trivy's own `PkgIdentifier.UID` / `Identifier.UID` (matching real schema:
`trivy image --list-all-pkgs` reports a distinct UID per package
occurrence), falling back to the vulnerability's own `PkgPath` field when
no UID is present, and ADDITIONALLY fails closed if ANY package sharing
that (name, version) anywhere in the scan carries a real FilePath — even
when the UID-resolved instance for a specific finding is itself vendored.

No mocking of the predicate logic itself: each test copies the real
trivy-scoped-scan.sh into a sandbox with a stub `docker` on PATH that
serves canned Trivy JSON on `docker run ... image ...` instead of running
Trivy for real, then runs the script as a real subprocess end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_SH = REPO_ROOT / "scripts" / "trivy-scoped-scan.sh"

STUB_DOCKER = """#!/bin/bash
# Only intercepts `docker run ... <trivy-image> image ... <target>` (what
# trivy-scoped-scan.sh invokes) -- serves the fixture JSON named by
# FAKE_TRIVY_JSON on stdout instead of actually running Trivy.
if [ "$1" = "run" ]; then
    cat "$FAKE_TRIVY_JSON"
    exit 0
fi
exit 1
"""


def _make_sandbox(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_path = bin_dir / "docker"
    docker_path.write_text(STUB_DOCKER, encoding="utf-8")
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _pkg(name: str, version: str, uid: str, file_path: str | None = None) -> dict:
    """A Packages[] entry shaped like real `trivy --list-all-pkgs` output."""
    entry = {"Name": name, "Version": version, "Identifier": {"UID": uid}}
    if file_path is not None:
        entry["FilePath"] = file_path
    return entry


def _vuln(vuln_id: str, pkg_name: str, version: str, uid: str, severity: str = "HIGH") -> dict:
    """A Vulnerabilities[] entry shaped like real Trivy output, with the
    PkgIdentifier.UID that joins it to a specific `_pkg()` instance above."""
    return {
        "VulnerabilityID": vuln_id,
        "PkgName": pkg_name,
        "InstalledVersion": version,
        "Severity": severity,
        "PkgIdentifier": {"UID": uid},
    }


def _trivy_json(vulnerabilities: list[dict], packages: list[dict]) -> str:
    return json.dumps(
        {
            "Results": [
                {
                    "Target": "Python",
                    "Type": "python-pkg",
                    "Vulnerabilities": vulnerabilities,
                    "Packages": packages,
                }
            ]
        }
    )


def _run(bin_dir: Path, fixture_json: str, image: str = "test-image:latest") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    fixture_path = bin_dir.parent / "fixture.json"
    fixture_path.write_text(fixture_json, encoding="utf-8")
    env["FAKE_TRIVY_JSON"] = str(fixture_path)
    return subprocess.run(
        ["bash", str(SCAN_SH), image],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_no_findings_passes(tmp_path):
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(vulnerabilities=[], packages=[])

    result = _run(bin_dir, fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Failing: 0" in result.stdout


def test_documented_vendored_msgpack_and_setuptools_are_allowed(tmp_path):
    """Positive control: the two real, current findings this script exists
    to cover must both be classified as allowed, not failing, when joined
    by UID to their (vendored, no-path) package instance."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            _vuln("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2", uid="msgpack-vendored-uid"),
            _vuln("CVE-2025-47273", "setuptools", "70.3.0", uid="setuptools-vendored-uid"),
        ],
        packages=[
            _pkg("msgpack", "1.1.2", uid="msgpack-vendored-uid", file_path=None),
            _pkg("setuptools", "70.3.0", uid="setuptools-vendored-uid", file_path=None),
            _pkg(
                "setuptools", "84.0.0", uid="setuptools-clean-uid",
                file_path="usr/local/lib/python3.11/site-packages/setuptools-84.0.0.dist-info/METADATA",
            ),
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Allowed (documented pip-vendored copies): 2" in result.stdout
    assert "Failing: 0" in result.stdout


def test_same_id_name_version_WITH_a_real_filepath_is_NOT_allowed(tmp_path):
    """A real top-level dependency that happens to land on the exact same
    VulnerabilityID/PkgName/Version as the documented vendored copy, with
    NO vendored sibling present, must fail the gate: its UID-resolved
    package instance has a real on-disk FilePath, not None."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            _vuln("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2", uid="msgpack-real-uid"),
        ],
        packages=[
            _pkg(
                "msgpack", "1.1.2", uid="msgpack-real-uid",
                file_path="usr/local/lib/python3.11/site-packages/msgpack-1.1.2.dist-info/METADATA",
            ),
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failing: 1" in result.stdout
    assert "DOES NOT MATCH" in result.stdout


def test_simultaneous_vendored_and_real_copy_BOTH_fail(tmp_path):
    """The codex r2 regression case: pip's vendored msgpack==1.1.2
    (FilePath=None) AND a real top-level msgpack==1.1.2 (a real FilePath)
    exist in the SAME image at the SAME name+version, each reported as its
    own Vulnerability entry with its own distinct UID. The r1 predicate's
    (name, version)-keyed map collapsed to FilePath=None here (a vendored
    occurrence anywhere silently cleared the real one), allowing the real
    finding through. Both findings must now fail: the real one because its
    own UID-resolved instance has a FilePath, and the vendored one because
    a sibling real copy at the same name+version exists anywhere in the
    scan — presence of a real installed package makes the CVE exploitable
    regardless of which specific instance a given finding happens to cite."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            _vuln("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2", uid="msgpack-vendored-uid"),
            _vuln("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2", uid="msgpack-real-uid"),
        ],
        packages=[
            _pkg("msgpack", "1.1.2", uid="msgpack-vendored-uid", file_path=None),
            _pkg(
                "msgpack", "1.1.2", uid="msgpack-real-uid",
                file_path="usr/local/lib/python3.11/site-packages/msgpack-1.1.2.dist-info/METADATA",
            ),
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Allowed (documented pip-vendored copies): 0" in result.stdout
    assert "Failing: 2" in result.stdout
    assert result.stdout.count("DOES NOT MATCH") == 2, result.stdout
    assert "a real (non-vendored) copy" in result.stdout


def test_different_version_of_an_allowed_package_is_not_allowed(tmp_path):
    """A real msgpack at a DIFFERENT version than the documented vendored
    1.1.2 (e.g. a real app dependency pinned elsewhere) must not be
    silently allowed just because the package name and ID match."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            _vuln("GHSA-6v7p-g79w-8964", "msgpack", "1.1.0", uid="msgpack-1-1-0-uid"),
        ],
        packages=[
            _pkg("msgpack", "1.1.0", uid="msgpack-1-1-0-uid", file_path=None),
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failing: 1" in result.stdout


def test_unrelated_critical_finding_fails(tmp_path):
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            _vuln("CVE-2099-00001", "some-other-package", "1.0.0", uid="other-uid", severity="CRITICAL"),
        ],
        packages=[
            _pkg(
                "some-other-package", "1.0.0", uid="other-uid",
                file_path="usr/local/lib/python3.11/site-packages/some_other_package-1.0.0.dist-info/METADATA",
            ),
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "some-other-package" in result.stdout


def test_mixed_allowed_and_failing_reports_both_correctly(tmp_path):
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            _vuln("GHSA-6v7p-g79w-8964", "msgpack", "1.1.2", uid="msgpack-vendored-uid"),
            _vuln("CVE-2099-00002", "unrelated", "2.0.0", uid="unrelated-uid"),
        ],
        packages=[
            _pkg("msgpack", "1.1.2", uid="msgpack-vendored-uid", file_path=None),
            _pkg(
                "unrelated", "2.0.0", uid="unrelated-uid",
                file_path="usr/local/lib/python3.11/site-packages/unrelated-2.0.0.dist-info/METADATA",
            ),
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Allowed (documented pip-vendored copies): 1" in result.stdout
    assert "Failing: 1" in result.stdout
    assert "unrelated" in result.stdout


def test_unresolvable_package_instance_is_not_allowed(tmp_path):
    """A Vulnerability entry with no PkgIdentifier.UID and no PkgPath
    fallback can't be proven vendored-only — must fail closed, not be
    silently allowed just because no contradicting FilePath was found
    either."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            {
                "VulnerabilityID": "GHSA-6v7p-g79w-8964",
                "PkgName": "msgpack",
                "InstalledVersion": "1.1.2",
                "Severity": "HIGH",
                # Deliberately no PkgIdentifier and no PkgPath.
            },
        ],
        packages=[],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failing: 1" in result.stdout
    assert "could not resolve" in result.stdout


def test_malformed_json_fails_closed_not_open(tmp_path):
    bin_dir = _make_sandbox(tmp_path)
    fixture_path = bin_dir.parent / "fixture.json"
    fixture_path.write_text("not valid json {{{", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_TRIVY_JSON"] = str(fixture_path)

    result = subprocess.run(
        ["bash", str(SCAN_SH), "test-image:latest"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2, result.stdout + result.stderr


@pytest.mark.parametrize(
    "bad_payload",
    [
        pytest.param("[]", id="top-level-array-not-object"),
        pytest.param("{}", id="missing-results-key-entirely"),
        pytest.param('{"Results": "not-a-list"}', id="results-is-a-string"),
        pytest.param('{"Results": [{"Vulnerabilities": "not-a-list"}]}', id="vulnerabilities-is-a-string"),
        pytest.param('{"Results": [{"Packages": [{"Identifier": "not-a-dict"}]}]}', id="identifier-is-a-string"),
    ],
)
def test_schema_failure_after_json_validity_check_exits_2_not_1(tmp_path, bad_payload):
    """codex r2/r3: valid JSON that doesn't match the shape this script
    expects is a TOOL/schema problem, not a security finding, and must
    exit 2 (scan error) -- never 1 (reads as 'gate correctly failed on
    real findings') or 0 (reads as clean)."""
    bin_dir = _make_sandbox(tmp_path)

    result = _run(bin_dir, bad_payload)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "unexpected JSON schema" in result.stderr
