"""ATHENA-126 (codex review) — tests for scripts/trivy-scoped-scan.sh's
allow-predicate.

The original approach was a blanket `.trivyignore` ID suppression for two
findings that are actually pip's own internally-vendored msgpack/setuptools
copies (no fix available in any released pip). codex flagged that a bare
VulnerabilityID allowlist would silently swallow a FUTURE finding too — if
a real top-level dependency ever lands on the exact same CVE ID (a
plausible coincidence: e.g. an app adds a real `msgpack` dependency that
happens to also be pinned at 1.1.2), the blanket ID ignore hides it
forever with zero diff to review.

The replacement predicate requires an exact match on (VulnerabilityID,
PkgName, InstalledVersion) AND "no on-disk FilePath" (i.e. Trivy found it
only via pip's bundled SBOM, not a real installed distribution). The most
important test below is the regression case: a same-ID/name/version
finding that DOES have a real FilePath (simulating exactly that future
real-dependency scenario) must NOT be allowed.

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
    to cover must both be classified as allowed, not failing."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            {
                "VulnerabilityID": "GHSA-6v7p-g79w-8964",
                "PkgName": "msgpack",
                "InstalledVersion": "1.1.2",
                "Severity": "HIGH",
            },
            {
                "VulnerabilityID": "CVE-2025-47273",
                "PkgName": "setuptools",
                "InstalledVersion": "70.3.0",
                "Severity": "HIGH",
            },
        ],
        packages=[
            {"Name": "msgpack", "Version": "1.1.2", "FilePath": None},
            {"Name": "setuptools", "Version": "70.3.0", "FilePath": None},
            {
                "Name": "setuptools",
                "Version": "84.0.0",
                "FilePath": "usr/local/lib/python3.11/site-packages/setuptools-84.0.0.dist-info/METADATA",
            },
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Allowed (documented pip-vendored copies): 2" in result.stdout
    assert "Failing: 0" in result.stdout


def test_same_id_name_version_WITH_a_real_filepath_is_NOT_allowed(tmp_path):
    """The regression case codex flagged: a real top-level dependency that
    happens to land on the exact same VulnerabilityID/PkgName/Version as
    the documented vendored copy must still fail the gate, because it has
    a real on-disk FilePath (a genuine installed distribution), not
    FilePath=None (SBOM-only)."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            {
                "VulnerabilityID": "GHSA-6v7p-g79w-8964",
                "PkgName": "msgpack",
                "InstalledVersion": "1.1.2",
                "Severity": "HIGH",
            },
        ],
        packages=[
            {
                "Name": "msgpack",
                "Version": "1.1.2",
                "FilePath": "usr/local/lib/python3.11/site-packages/msgpack-1.1.2.dist-info/METADATA",
            },
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failing: 1" in result.stdout
    assert "DOES NOT MATCH" in result.stdout


def test_different_version_of_an_allowed_package_is_not_allowed(tmp_path):
    """A real msgpack at a DIFFERENT version than the documented vendored
    1.1.2 (e.g. a real app dependency pinned elsewhere) must not be
    silently allowed just because the package name and ID match."""
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            {
                "VulnerabilityID": "GHSA-6v7p-g79w-8964",
                "PkgName": "msgpack",
                "InstalledVersion": "1.1.0",
                "Severity": "HIGH",
            },
        ],
        packages=[
            {"Name": "msgpack", "Version": "1.1.0", "FilePath": None},
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failing: 1" in result.stdout


def test_unrelated_critical_finding_fails(tmp_path):
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            {
                "VulnerabilityID": "CVE-2099-00001",
                "PkgName": "some-other-package",
                "InstalledVersion": "1.0.0",
                "Severity": "CRITICAL",
            },
        ],
        packages=[
            {
                "Name": "some-other-package",
                "Version": "1.0.0",
                "FilePath": "usr/local/lib/python3.11/site-packages/some_other_package-1.0.0.dist-info/METADATA",
            },
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "some-other-package" in result.stdout


def test_mixed_allowed_and_failing_reports_both_correctly(tmp_path):
    bin_dir = _make_sandbox(tmp_path)
    fixture = _trivy_json(
        vulnerabilities=[
            {
                "VulnerabilityID": "GHSA-6v7p-g79w-8964",
                "PkgName": "msgpack",
                "InstalledVersion": "1.1.2",
                "Severity": "HIGH",
            },
            {
                "VulnerabilityID": "CVE-2099-00002",
                "PkgName": "unrelated",
                "InstalledVersion": "2.0.0",
                "Severity": "HIGH",
            },
        ],
        packages=[
            {"Name": "msgpack", "Version": "1.1.2", "FilePath": None},
            {
                "Name": "unrelated",
                "Version": "2.0.0",
                "FilePath": "usr/local/lib/python3.11/site-packages/unrelated-2.0.0.dist-info/METADATA",
            },
        ],
    )

    result = _run(bin_dir, fixture)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Allowed (documented pip-vendored copies): 1" in result.stdout
    assert "Failing: 1" in result.stdout
    assert "unrelated" in result.stdout


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
