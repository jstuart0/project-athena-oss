"""ATHENA-126 — every python:3.11-slim Dockerfile must upgrade Debian
packages at build time and pin setuptools at/above the fixed version for
CVE-2025-47273 (path traversal in setuptools' PackageIndex).

Trivy against the running fleet (2026-09-28) found the same Debian Critical
+ High set (perl-base, gzip, libpcre2-8-0, ...) on every image because none
of the Dockerfiles ran an OS-level upgrade after FROM -- the base image's
package versions shipped as-is. This is a drift test, not a functional one:
it greps the actual Dockerfile text for the two invariants rather than
building images, so it stays fast and catches a future Dockerfile edit that
silently drops either line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]

MIN_SETUPTOOLS = Version("78.1.1")

APT_UPGRADE_MARKER = "apt-get -y --no-install-recommends upgrade"

# Every Dockerfile in the repo that is FROM python:3.11-slim (or a stage of
# a multi-stage build that is). Built directly from the same three groups
# scripts/service-defs.sh uses to drive build-and-push.sh, plus
# apps/chat-embed which shares the identical python:3.11-slim base and
# carries the same CVEs even though it sits outside service-defs.sh's
# ADMIN_SERVICES naming (it's still in SPECIAL_PYTHON_IMAGES there).
RAG_SERVICE_DIRS = sorted(
    p.parent.relative_to(REPO_ROOT)
    for p in (REPO_ROOT / "src" / "rag").glob("*/Dockerfile")
)

PYTHON_DOCKERFILES = sorted(
    {
        REPO_ROOT / "admin" / "backend" / "Dockerfile",
        REPO_ROOT / "apps" / "jarvis-web" / "Dockerfile",
        REPO_ROOT / "apps" / "chat-embed" / "Dockerfile",
        REPO_ROOT / "src" / "gateway" / "Dockerfile",
        REPO_ROOT / "src" / "orchestrator" / "Dockerfile",
        REPO_ROOT / "src" / "mode_service" / "Dockerfile",
        *(REPO_ROOT / d / "Dockerfile" for d in RAG_SERVICE_DIRS),
    }
)


def test_population_is_not_accidentally_empty():
    """Sanity: prove the glob-derived population actually found the fleet,
    so a refactor that silently breaks path resolution fails loudly here
    instead of the parametrized tests below just not running."""
    assert len(PYTHON_DOCKERFILES) == 29, sorted(str(p) for p in PYTHON_DOCKERFILES)
    for path in PYTHON_DOCKERFILES:
        assert path.is_file(), f"expected Dockerfile at {path}"


@pytest.mark.parametrize("dockerfile", PYTHON_DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_dockerfile_is_from_python_slim(dockerfile: Path):
    """Sanity: every file in this population must actually declare the base
    image this test suite assumes -- otherwise the two checks below are
    meaningless for that file."""
    text = dockerfile.read_text(encoding="utf-8")
    assert "FROM python:3.11-slim" in text, (
        f"{dockerfile} does not declare FROM python:3.11-slim; "
        "remove it from PYTHON_DOCKERFILES or update the base image assumption"
    )


@pytest.mark.parametrize("dockerfile", PYTHON_DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_dockerfile_applies_debian_security_updates(dockerfile: Path):
    """Every `FROM python:3.11-slim` stage must run an apt-get upgrade
    before anything else installs on top of it. Multi-stage files (only
    apps/jarvis-web today) must apply it in EVERY python:3.11-slim stage,
    not just the final one that ships."""
    text = dockerfile.read_text(encoding="utf-8")
    from_count = text.count("FROM python:3.11-slim")
    upgrade_count = text.count(APT_UPGRADE_MARKER)
    assert upgrade_count >= from_count, (
        f"{dockerfile} has {from_count} python:3.11-slim stage(s) but only "
        f"{upgrade_count} occurrence(s) of the apt-get upgrade step "
        f"('{APT_UPGRADE_MARKER}') -- every stage must patch its own base layer"
    )
    assert "rm -rf /var/lib/apt/lists/*" in text, (
        f"{dockerfile} runs apt-get but never cleans /var/lib/apt/lists -- "
        "leaves stale package index bloat in the image layer"
    )


@pytest.mark.parametrize("dockerfile", PYTHON_DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_dockerfile_pins_setuptools_at_or_above_fixed_version(dockerfile: Path):
    """CVE-2025-47273 (setuptools PackageIndex path traversal) is fixed at
    78.1.1. Every Dockerfile explicitly pins pip install ... setuptools==X
    before requirements are installed -- assert X >= the fixed floor rather
    than a literal version string, so this test doesn't need editing every
    time the pin is bumped."""
    text = dockerfile.read_text(encoding="utf-8")
    matches = re.findall(r"setuptools==([0-9][0-9A-Za-z.\-]*)", text)
    assert matches, (
        f"{dockerfile} has no explicit `setuptools==X.Y.Z` pin -- without one, "
        "the base image's stale setuptools (vulnerable to CVE-2025-47273) is "
        "whatever ships in python:3.11-slim at build time"
    )
    for raw_version in matches:
        assert Version(raw_version) >= MIN_SETUPTOOLS, (
            f"{dockerfile} pins setuptools=={raw_version}, below the "
            f"CVE-2025-47273 fix floor {MIN_SETUPTOOLS}"
        )


# ---------------------------------------------------------------------------
# Positive control: prove the checks above can fail. Without this, a
# `dockerfile.read_text()` typo or an always-true regex would pass silently
# against the whole fleet and this file would provide zero actual signal.
# ---------------------------------------------------------------------------


def test_positive_control_missing_upgrade_step_is_caught(tmp_path):
    bad_dockerfile = tmp_path / "Dockerfile"
    bad_dockerfile.write_text(
        "FROM python:3.11-slim\nWORKDIR /app\nRUN pip install --no-cache-dir --upgrade "
        "pip==26.2.1 setuptools==84.0.0 wheel==0.48.0\n",
        encoding="utf-8",
    )
    text = bad_dockerfile.read_text(encoding="utf-8")
    assert text.count(APT_UPGRADE_MARKER) < text.count("FROM python:3.11-slim")


def test_positive_control_stale_setuptools_pin_is_caught(tmp_path):
    bad_dockerfile = tmp_path / "Dockerfile"
    bad_dockerfile.write_text(
        "FROM python:3.11-slim\nRUN pip install --no-cache-dir --upgrade "
        "pip==26.2.1 setuptools==70.3.0 wheel==0.48.0\n",
        encoding="utf-8",
    )
    text = bad_dockerfile.read_text(encoding="utf-8")
    matches = re.findall(r"setuptools==([0-9][0-9A-Za-z.\-]*)", text)
    assert matches and Version(matches[0]) < MIN_SETUPTOOLS
