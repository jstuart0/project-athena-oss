"""ATHENA-126 — tests for scripts/build-and-push.sh's TAG precedence and
tag-collision guard.

Before this fix, config.env's unconditional `TAG=...` silently overwrote a
caller's explicit `TAG=v2.0.0 ./build-and-push.sh` (or `--tag v2.0.0`)
invocation, because `source config.env` ran before the script ever looked
at what the caller had exported. A push could land on — and overwrite — a
stale tag the caller never asked for.

The tag-collision guard originally probed the registry's `/v2/<repo>/tags/
list` API directly via curl. codex r1 review flagged two real bugs: (1) the
URL construction breaks for path-style registries (`REGISTRY=ghcr.io/org`
needs `https://ghcr.io/v2/org/name/tags/list`, not
`https://ghcr.io/org/v2/name/tags/list`), and (2) an unreachable/auth-gated
registry warned and proceeded — silently bypassing the guard for exactly
the private registries it exists to protect. The guard now shells out to
`docker manifest inspect` (Docker's own credential store and endpoint
resolution) and fails CLOSED (refuses) on any error other than a genuine
"not found", overridable only by --force-tag.

codex r2 review flagged that the not-found classifier was still too broad:
a bare "not found" substring matched auth/credential/TLS/routing failures
too (e.g. an UNAUTHORIZED response's "repository not found or you do not
have access"), fail-opening the guard for exactly those cases. Matching is
now restricted (case-insensitively) to the specific manifest/name-unknown
markers the Docker distribution spec uses for genuine absence:
MANIFEST_UNKNOWN, NAME_UNKNOWN, "manifest unknown", "no such manifest".
Also added REGISTRY_INSECURE=1, which appends --insecure to the
`docker manifest inspect` call only, for a private plain-HTTP registry.

No mocking of the resolution logic itself: each test copies the real
build-and-push.sh + service-defs.sh into a throwaway PROJECT_ROOT with a
fixture config.env and a stub docker on PATH, then runs the script as a
real subprocess end to end.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SH = REPO_ROOT / "scripts" / "build-and-push.sh"
SERVICE_DEFS_SH = REPO_ROOT / "scripts" / "service-defs.sh"

# Intercepts both plain `docker build|push ...` (logged, always "succeeds")
# and `docker manifest inspect [--insecure] <image>` (the tag-collision
# guard) -- the image is always the LAST argument regardless of whether
# --insecure was inserted, matching the real docker CLI's own positional
# convention:
#   - FAKE_MANIFEST_EXISTS: space-separated "registry/name:tag" strings that
#     "exist" (manifest inspect exits 0) — everything else "doesn't exist"
#     (a fixed-string comparison against the invoked image, matching the
#     real script's non-regex tag handling).
#   - FAKE_MANIFEST_ERROR_MODE:
#       unset / "notfound" (default): docker's real "manifest unknown"
#         not-found message.
#       "authfail": a non-not-found auth failure (no "not found"-shaped
#         text at all).
#       "authfail_with_not_found_text": an auth-style failure whose
#         message HAPPENS to contain the bare substring "not found" (e.g.
#         "repository not found or you do not have access") without any
#         of the four specific absence markers — must still fail closed.
STUB_DOCKER = """#!/bin/bash
echo "docker $*" >> "$DOCKER_LOG"
if [ "$1" = "manifest" ] && [ "$2" = "inspect" ]; then
    image="${@: -1}"
    if [ -n "${FAKE_MANIFEST_EXISTS:-}" ]; then
        for existing in $FAKE_MANIFEST_EXISTS; do
            if [ "$existing" = "$image" ]; then
                echo '{"schemaVersion":2}'
                exit 0
            fi
        done
    fi
    case "${FAKE_MANIFEST_ERROR_MODE:-notfound}" in
        authfail)
            echo 'Error response from daemon: Get "https://example/v2/": unauthorized' >&2
            exit 1
            ;;
        authfail_with_not_found_text)
            echo 'Error response from daemon: unauthorized: repository not found or you do not have access' >&2
            exit 1
            ;;
        *)
            echo "manifest unknown" >&2
            exit 1
            ;;
    esac
fi
exit 0
"""


def _make_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Build PROJECT_ROOT/scripts/{build-and-push.sh,service-defs.sh} plus a
    minimal src/mode_service/Dockerfile, and a stub docker on its own PATH
    dir. Returns (project_root, bin_dir)."""
    project_root = tmp_path / "project"
    (project_root / "scripts").mkdir(parents=True)
    (project_root / "src" / "mode_service").mkdir(parents=True)
    (project_root / "admin" / "backend").mkdir(parents=True)
    (project_root / "admin" / "frontend").mkdir(parents=True)
    (project_root / "apps" / "jarvis-web").mkdir(parents=True)
    (project_root / "apps" / "chat-embed").mkdir(parents=True)

    shutil.copy(BUILD_SH, project_root / "scripts" / "build-and-push.sh")
    shutil.copy(SERVICE_DEFS_SH, project_root / "scripts" / "service-defs.sh")
    os.chmod(project_root / "scripts" / "build-and-push.sh", 0o755)

    (project_root / "src" / "mode_service" / "Dockerfile").write_text(
        "FROM python:3.11-slim\n", encoding="utf-8"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_path = bin_dir / "docker"
    docker_path.write_text(STUB_DOCKER, encoding="utf-8")
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IEXEC)

    return project_root, bin_dir


def _write_config_env(project_root: Path, tag: str, registry: str = "localhost:5000") -> None:
    (project_root / "config.env").write_text(
        f"REGISTRY={registry}\nTAG={tag}\n", encoding="utf-8"
    )


def _run(
    project_root: Path,
    bin_dir: Path,
    docker_log: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DOCKER_LOG"] = str(docker_log)
    # Never let the real caller's shell TAG/REGISTRY leak into the sandbox —
    # each test sets exactly what it wants to assert on.
    env.pop("TAG", None)
    env.pop("REGISTRY", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(project_root / "scripts" / "build-and-push.sh"), *args],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_config_env_tag_applies_when_caller_sets_nothing(tmp_path):
    """Positive control: with no caller override at all, config.env's TAG is
    exactly what should be used."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="stale-config-tag")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "no-such-service")

    combined = result.stdout + result.stderr
    assert "Effective tag: stale-config-tag" in combined, combined


def test_caller_exported_tag_wins_over_config_env(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="stale-config-tag")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "no-such-service",
        extra_env={"TAG": "caller-tag"},
    )

    combined = result.stdout + result.stderr
    assert "Effective tag: caller-tag" in combined, combined
    assert "stale-config-tag" not in combined


def test_tag_flag_wins_over_caller_exported_tag_and_config_env(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="stale-config-tag")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "--tag", "flag-tag", "no-such-service",
        extra_env={"TAG": "caller-tag"},
    )

    combined = result.stdout + result.stderr
    assert "Effective tag: flag-tag" in combined, combined


def test_tag_equals_flag_form_accepted(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="stale-config-tag")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "--tag=eqform-tag", "no-such-service")

    combined = result.stdout + result.stderr
    assert "Effective tag: eqform-tag" in combined, combined


def test_tag_flag_without_value_rejected(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="stale-config-tag")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "--tag")

    assert result.returncode != 0
    assert "--tag requires a value" in result.stdout + result.stderr


def test_unknown_flag_rejected(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="stale-config-tag")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "--bogus-flag")

    assert result.returncode != 0
    assert "Unknown flag" in result.stdout + result.stderr


def test_refuses_to_overwrite_existing_tag_and_never_calls_docker_build(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"FAKE_MANIFEST_EXISTS": "localhost:5000/athena-mode-service:v1"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "already exists" in combined
    assert "--force-tag" in combined
    log_text = docker_log.read_text() if docker_log.exists() else ""
    assert "manifest inspect" in log_text, "guard must have checked the registry"
    assert "docker build" not in log_text, (
        f"guard must fire before any docker build/push, but docker build was invoked: {log_text}"
    )


def test_force_tag_overrides_the_collision_guard(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "--force-tag", "mode-service",
        extra_env={"FAKE_MANIFEST_EXISTS": "localhost:5000/athena-mode-service:v1"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "already exists" not in combined
    log_text = docker_log.read_text()
    assert "manifest inspect" not in log_text, "--force-tag must skip the registry check entirely"
    assert "docker build" in log_text, "docker build must run once --force-tag bypasses the guard"
    assert "--pull" in log_text


def test_non_colliding_tag_proceeds_without_force(tmp_path):
    """Positive control: a tag the registry genuinely doesn't have (docker
    manifest inspect reports "not found") needs no --force-tag at all."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v2")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"FAKE_MANIFEST_EXISTS": "localhost:5000/athena-mode-service:v1"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "already exists" not in combined
    log_text = docker_log.read_text()
    assert "manifest inspect" in log_text
    assert "docker build" in log_text


def test_unreachable_registry_fails_closed_without_force_tag(tmp_path):
    """The registry check failing for a reason OTHER than "not found" (auth,
    network, TLS, ...) must refuse the push, not silently proceed — this is
    the exact bypass codex flagged in the old curl-based guard."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"FAKE_MANIFEST_ERROR_MODE": "authfail"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "Could not verify" in combined
    assert "--force-tag" in combined
    log_text = docker_log.read_text() if docker_log.exists() else ""
    assert "manifest inspect" in log_text
    assert "docker build" not in log_text, (
        f"a registry-check failure must block the build, but docker build was invoked: {log_text}"
    )


def test_force_tag_overrides_fail_closed_on_unreachable_registry(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "--force-tag", "mode-service",
        extra_env={"FAKE_MANIFEST_ERROR_MODE": "authfail"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    log_text = docker_log.read_text()
    assert "manifest inspect" not in log_text, "--force-tag must skip the registry check entirely"
    assert "docker build" in log_text


def test_auth_error_containing_bare_not_found_text_still_fails_closed(tmp_path):
    """codex r2 regression: an auth/credential failure whose message
    happens to contain the substring "not found" (e.g. an UNAUTHORIZED
    response reading "repository not found or you do not have access")
    must NOT be classified as genuine tag absence — only the four specific
    manifest/name-unknown markers count as absence."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"FAKE_MANIFEST_ERROR_MODE": "authfail_with_not_found_text"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "Could not verify" in combined
    log_text = docker_log.read_text() if docker_log.exists() else ""
    assert "docker build" not in log_text, (
        f"a bare 'not found' substring in an auth failure must still fail closed, "
        f"but docker build was invoked: {log_text}"
    )


def test_registry_insecure_flag_passed_to_manifest_inspect_only(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"REGISTRY_INSECURE": "1"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    log_text = docker_log.read_text()
    inspect_lines = [line for line in log_text.splitlines() if "manifest inspect" in line]
    assert inspect_lines, log_text
    assert all("--insecure" in line for line in inspect_lines), log_text
    build_lines = [line for line in log_text.splitlines() if line.startswith("docker build")]
    assert build_lines, log_text
    assert not any("--insecure" in line for line in build_lines), (
        f"--insecure must apply only to the manifest-inspect check, not the actual build: {log_text}"
    )


def test_every_build_passes_pull(tmp_path):
    """--pull refreshes the base image tag on every build, not just some
    code paths — the whole point of ATHENA-126 item 3."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "mode-service")

    assert result.returncode == 0, result.stdout + result.stderr
    log_text = docker_log.read_text()
    build_lines = [line for line in log_text.splitlines() if line.startswith("docker build")]
    assert build_lines, log_text
    assert all("--pull" in line for line in build_lines), log_text


def test_every_build_passes_apt_cache_bust(tmp_path):
    """APT_CACHE_BUST is what forces a stale cached apt-get-upgrade layer
    to actually re-run — every build must pass it."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "mode-service")

    assert result.returncode == 0, result.stdout + result.stderr
    log_text = docker_log.read_text()
    build_lines = [line for line in log_text.splitlines() if line.startswith("docker build")]
    assert build_lines, log_text
    assert all("--build-arg APT_CACHE_BUST=" in line for line in build_lines), log_text
