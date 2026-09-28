"""ATHENA-126 — tests for scripts/build-and-push.sh's TAG precedence and
tag-collision guard.

Before this fix, config.env's unconditional `TAG=...` silently overwrote a
caller's explicit `TAG=v2.0.0 ./build-and-push.sh` (or `--tag v2.0.0`)
invocation, because `source config.env` ran before the script ever looked
at what the caller had exported. A push could land on — and overwrite — a
stale tag the caller never asked for.

No mocking of the resolution logic itself: each test copies the real
build-and-push.sh + service-defs.sh into a throwaway PROJECT_ROOT with a
fixture config.env and stub docker/curl on PATH, then runs the script as a
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

STUB_DOCKER = """#!/bin/bash
echo "docker $*" >> "$DOCKER_LOG"
exit 0
"""

# Stub curl for the tag-collision guard's /v2/<repo>/tags/list probe.
# FAKE_TAGS_RESPONSE unset => simulate an unreachable/auth-gated registry
# (both the https and http attempts fail, like the real curl would against
# a registry with no anonymous /v2/ access). FAKE_TAGS_RESPONSE set => both
# attempts "succeed" and return that body, exactly like a real registry
# would for either scheme it happens to serve on.
STUB_CURL = """#!/bin/bash
if [ -z "${FAKE_TAGS_RESPONSE:-}" ]; then
    exit 7
fi
echo "$FAKE_TAGS_RESPONSE"
exit 0
"""


def _make_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Build PROJECT_ROOT/scripts/{build-and-push.sh,service-defs.sh} plus a
    minimal src/mode_service/Dockerfile, and a stub docker+curl on their own
    PATH dir. Returns (project_root, bin_dir)."""
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
    for tool, body in (("docker", STUB_DOCKER), ("curl", STUB_CURL)):
        tool_path = bin_dir / tool
        tool_path.write_text(body, encoding="utf-8")
        tool_path.chmod(tool_path.stat().st_mode | stat.S_IEXEC)

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


def test_refuses_to_overwrite_existing_tag_and_never_calls_docker(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"FAKE_TAGS_RESPONSE": '{"name":"athena-mode-service","tags":["v1","v0"]}'},
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "already exists" in combined
    assert "--force-tag" in combined
    assert not docker_log.exists(), (
        f"guard must fire before any docker build/push, but docker was invoked: "
        f"{docker_log.read_text() if docker_log.exists() else ''}"
    )


def test_force_tag_overrides_the_collision_guard(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "--force-tag", "mode-service",
        extra_env={"FAKE_TAGS_RESPONSE": '{"name":"athena-mode-service","tags":["v1","v0"]}'},
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "already exists" not in combined
    assert docker_log.exists(), "docker build must run once --force-tag bypasses the guard"
    assert "--pull" in docker_log.read_text()


def test_non_colliding_tag_proceeds_without_force(tmp_path):
    """Positive control: a tag that isn't in the registry's list needs no
    --force-tag at all."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v2")
    docker_log = tmp_path / "docker.log"

    result = _run(
        project_root, bin_dir, docker_log, "mode-service",
        extra_env={"FAKE_TAGS_RESPONSE": '{"name":"athena-mode-service","tags":["v1","v0"]}'},
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "already exists" not in combined
    assert docker_log.exists()


def test_unreachable_registry_warns_but_proceeds(tmp_path):
    """The tags/list probe against an auth-gated or down registry can't
    determine collision either way — must warn, not block."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_config_env(project_root, tag="v1")
    docker_log = tmp_path / "docker.log"

    result = _run(project_root, bin_dir, docker_log, "mode-service")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Could not verify" in combined
    assert docker_log.exists()


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
