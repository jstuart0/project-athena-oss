"""ATHENA-126 — every python:3.11-slim Dockerfile must upgrade Debian
packages at build time (with a cache-busting ARG so a stale layer cache
can't silently skip it) and pin setuptools at/above the fixed version for
CVE-2025-47273 (path traversal in setuptools' PackageIndex).

Trivy against the running fleet (2026-09-28) found the same Debian Critical
+ High set (perl-base, gzip, libpcre2-8-0, ...) on every image because none
of the Dockerfiles ran an OS-level upgrade after FROM -- the base image's
package versions shipped as-is. This is a drift test, not a functional one:
it greps the actual Dockerfile text for the invariants rather than building
images, so it stays fast and catches a future Dockerfile edit that silently
drops one.

Population discovery (codex review): originally a hardcoded list of the
six non-RAG paths plus a RAG glob, asserted to exactly 29 files -- a new
non-RAG `FROM python:...` Dockerfile added anywhere else in the repo would
silently never be checked. Discovery is now a repo-wide glob over
`Dockerfile*` filtered to files that actually declare a `FROM python:`
stage, with a floor (`>= 29`) rather than an exact count, so the
population only grows as new Python images are added and never has to be
edited by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]

MIN_SETUPTOOLS = Version("78.1.1")

APT_UPGRADE_MARKER = "apt-get -y --no-install-recommends upgrade"
CACHE_BUST_ARG = "ARG APT_CACHE_BUST"
CACHE_BUST_CONSUME = 'echo "$APT_CACHE_BUST" >/dev/null'

_EXCLUDED_DIR_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
_FROM_PYTHON_RE = re.compile(r"^\s*FROM\s+python:", re.MULTILINE)


def _discover_python_dockerfiles() -> list[Path]:
    found = []
    for path in REPO_ROOT.rglob("Dockerfile*"):
        if not path.is_file():
            continue
        if _EXCLUDED_DIR_PARTS & set(path.relative_to(REPO_ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _FROM_PYTHON_RE.search(text):
            found.append(path)
    return sorted(found)


PYTHON_DOCKERFILES = _discover_python_dockerfiles()

MIN_KNOWN_PYTHON_IMAGES = 29


def test_population_is_not_accidentally_empty():
    """Sanity: prove the glob-derived population actually found the fleet
    (a floor, not an exact count -- new Python images are expected to grow
    this over time), so a refactor that silently breaks path resolution
    fails loudly here instead of the parametrized tests below just not
    running."""
    assert len(PYTHON_DOCKERFILES) >= MIN_KNOWN_PYTHON_IMAGES, sorted(
        str(p.relative_to(REPO_ROOT)) for p in PYTHON_DOCKERFILES
    )
    for path in PYTHON_DOCKERFILES:
        assert path.is_file(), f"expected Dockerfile at {path}"


def test_discovery_finds_the_known_fleet_by_relative_path():
    """Belt-and-suspenders on top of the floor count above: the specific
    known images must all be present by path, not just a matching count
    (a count-only floor could pass with 29 DIFFERENT files if discovery
    silently missed one image and picked up an unrelated one)."""
    discovered = {str(p.relative_to(REPO_ROOT)) for p in PYTHON_DOCKERFILES}
    expected_non_rag = {
        "admin/backend/Dockerfile",
        "apps/jarvis-web/Dockerfile",
        "apps/chat-embed/Dockerfile",
        "src/gateway/Dockerfile",
        "src/orchestrator/Dockerfile",
        "src/mode_service/Dockerfile",
    }
    missing = expected_non_rag - discovered
    assert not missing, f"discovery missed known non-RAG Dockerfiles: {missing}"
    rag_count = sum(1 for d in discovered if d.startswith("src/rag/"))
    assert rag_count >= 23, f"expected at least 23 RAG Dockerfiles, discovered {rag_count}"


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
    """Every `FROM python:...` stage must run an apt-get upgrade before
    anything else installs on top of it. Multi-stage files (only
    apps/jarvis-web today) must apply it in EVERY stage, not just the final
    one that ships."""
    text = dockerfile.read_text(encoding="utf-8")
    from_count = len(_FROM_PYTHON_RE.findall(text))
    upgrade_count = text.count(APT_UPGRADE_MARKER)
    assert upgrade_count >= from_count, (
        f"{dockerfile} has {from_count} python stage(s) but only "
        f"{upgrade_count} occurrence(s) of the apt-get upgrade step "
        f"('{APT_UPGRADE_MARKER}') -- every stage must patch its own base layer"
    )
    assert "rm -rf /var/lib/apt/lists/*" in text, (
        f"{dockerfile} runs apt-get but never cleans /var/lib/apt/lists -- "
        "leaves stale package index bloat in the image layer"
    )


@pytest.mark.parametrize("dockerfile", PYTHON_DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_dockerfile_busts_the_apt_upgrade_layer_cache(dockerfile: Path):
    """--pull refreshes the base image tag, but Docker's build cache for
    the apt-get-upgrade RUN layer is keyed on (parent layer + instruction
    text) -- if the base digest hasn't moved since the last build, that
    layer cache-hits even though Debian's package repos kept publishing
    fixes independently of the base image. Every stage needs its own `ARG
    APT_CACHE_BUST` (build args don't cross stage boundaries) consumed
    inside the upgrade RUN line, matched build-and-push.sh passing a fresh
    value on every invocation."""
    text = dockerfile.read_text(encoding="utf-8")
    from_count = len(_FROM_PYTHON_RE.findall(text))
    arg_count = text.count(CACHE_BUST_ARG)
    consume_count = text.count(CACHE_BUST_CONSUME)
    assert arg_count >= from_count, (
        f"{dockerfile} has {from_count} python stage(s) but only {arg_count} "
        f"occurrence(s) of '{CACHE_BUST_ARG}' -- every stage needs its own "
        "(ARG does not cross FROM boundaries)"
    )
    assert consume_count >= from_count, (
        f"{dockerfile} has {from_count} python stage(s) but only {consume_count} "
        f"occurrence(s) of '{CACHE_BUST_CONSUME}' in the upgrade RUN line -- "
        "declaring the ARG without consuming it inside the RUN doesn't bust the cache"
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
    assert text.count(APT_UPGRADE_MARKER) < len(_FROM_PYTHON_RE.findall(text))


def test_positive_control_upgrade_without_cache_bust_is_caught(tmp_path):
    """An apt-get upgrade RUN with no ARG APT_CACHE_BUST ahead of it is
    exactly what this repo shipped before the codex review -- correct on
    day one, silently stale after the first cached rebuild."""
    bad_dockerfile = tmp_path / "Dockerfile"
    bad_dockerfile.write_text(
        "FROM python:3.11-slim\nWORKDIR /app\n"
        "RUN apt-get update && apt-get -y --no-install-recommends upgrade "
        "&& rm -rf /var/lib/apt/lists/*\n",
        encoding="utf-8",
    )
    text = bad_dockerfile.read_text(encoding="utf-8")
    from_count = len(_FROM_PYTHON_RE.findall(text))
    assert text.count(CACHE_BUST_ARG) < from_count
    assert text.count(CACHE_BUST_CONSUME) < from_count


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
