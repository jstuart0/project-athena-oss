"""ATHENA-85 — tests for scripts/deploy.sh's manifest placeholder guard.

manifests/athena-prod/*.yaml ship with unconfigured YOUR_REGISTRY image
placeholders. Applying them as-is against an already-running namespace
overwrites live deployments with those placeholders and takes the namespace
down. `check_manifest_placeholders()` (called first thing inside
`deploy_manifests()`) must refuse before any `kubectl` invocation unless the
operator explicitly passes --allow-placeholders.

No mocking of the guard itself: each test copies the real deploy.sh into a
throwaway PROJECT_ROOT with fixture manifests and a stub `kubectl` on PATH,
then runs the script as a real subprocess end to end.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"

STUB_KUBECTL = """#!/bin/bash
echo "kubectl $*" >> "$KUBECTL_LOG"
exit 0
"""


def _make_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Build PROJECT_ROOT/scripts/deploy.sh + PROJECT_ROOT/manifests/athena-prod/
    and a stub kubectl on its own PATH dir. Returns (project_root, bin_dir)."""
    project_root = tmp_path / "project"
    (project_root / "scripts").mkdir(parents=True)
    (project_root / "manifests" / "athena-prod").mkdir(parents=True)
    shutil.copy(DEPLOY_SH, project_root / "scripts" / "deploy.sh")
    os.chmod(project_root / "scripts" / "deploy.sh", 0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl_path = bin_dir / "kubectl"
    kubectl_path.write_text(STUB_KUBECTL, encoding="utf-8")
    kubectl_path.chmod(kubectl_path.stat().st_mode | stat.S_IEXEC)

    return project_root, bin_dir


def _write_manifest(project_root: Path, name: str, body: str) -> None:
    (project_root / "manifests" / "athena-prod" / name).write_text(body, encoding="utf-8")


def _run_deploy(project_root: Path, bin_dir: Path, kubectl_log: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["KUBECTL_LOG"] = str(kubectl_log)
    # Never let a real config.env or REGISTRY leak in from the caller's shell.
    env.pop("REGISTRY", None)
    return subprocess.run(
        ["bash", str(project_root / "scripts" / "deploy.sh"), *args, "deploy"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


PLACEHOLDER_ADMIN_BACKEND = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: athena-admin-backend
spec:
  template:
    spec:
      containers:
      - name: admin-backend
        image: YOUR_REGISTRY/athena-admin-backend:latest
"""

CONFIGURED_ADMIN_BACKEND = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: athena-admin-backend
spec:
  template:
    spec:
      containers:
      - name: admin-backend
        image: registry.example.com/athena-admin-backend:v1.2.3
"""

CONFIGURE_ME_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: athena-config
data:
  OIDC_CLIENT_ID: CONFIGURE_ME_OIDC_CLIENT_ID
"""

COMMENTED_PLACEHOLDER_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: athena-config
data:
  # Example only, not a live value: image: YOUR_REGISTRY/athena-example:latest
  DEFAULT_CITY: "some-city"
"""


def test_refuses_and_makes_zero_kubectl_calls_on_your_registry_placeholder(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_manifest(project_root, "admin-backend.yaml", PLACEHOLDER_ADMIN_BACKEND)
    kubectl_log = tmp_path / "kubectl.log"

    result = _run_deploy(project_root, bin_dir, kubectl_log)

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "Refusing to deploy" in combined
    assert "YOUR_REGISTRY" in combined
    assert "admin-backend.yaml" in combined
    assert not kubectl_log.exists(), (
        f"guard must fire before any kubectl call, but kubectl was invoked: {kubectl_log.read_text() if kubectl_log.exists() else ''}"
    )


def test_refuses_on_configure_me_placeholder(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_manifest(project_root, "config.yaml", CONFIGURE_ME_CONFIGMAP)
    kubectl_log = tmp_path / "kubectl.log"

    result = _run_deploy(project_root, bin_dir, kubectl_log)

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "Refusing to deploy" in combined
    assert "CONFIGURE_ME_OIDC_CLIENT_ID" in combined
    assert not kubectl_log.exists()


def test_commented_placeholder_does_not_trigger_refusal(tmp_path):
    """Positive control: a placeholder string that only appears inside a
    comment line must not false-positive the guard. Proves the guard strips
    comments rather than doing a naive whole-file grep."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_manifest(project_root, "config.yaml", COMMENTED_PLACEHOLDER_CONFIGMAP)
    kubectl_log = tmp_path / "kubectl.log"

    result = _run_deploy(project_root, bin_dir, kubectl_log)

    combined = result.stdout + result.stderr
    assert "Refusing to deploy" not in combined
    assert result.returncode == 0, combined
    assert "Deployment complete!" in combined
    assert kubectl_log.exists()


def test_allow_placeholders_flag_warns_but_proceeds(tmp_path):
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_manifest(project_root, "admin-backend.yaml", PLACEHOLDER_ADMIN_BACKEND)
    kubectl_log = tmp_path / "kubectl.log"

    result = _run_deploy(project_root, bin_dir, kubectl_log, "--allow-placeholders")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Proceeding because --allow-placeholders was passed" in combined
    assert "YOUR_REGISTRY" in combined
    assert "Deployment complete!" in combined
    assert kubectl_log.exists(), "guard must let deploy proceed to kubectl when --allow-placeholders is passed"


def test_no_placeholders_deploys_cleanly_without_flag(tmp_path):
    """Positive control: a fully configured manifest set needs no flag at all."""
    project_root, bin_dir = _make_sandbox(tmp_path)
    _write_manifest(project_root, "admin-backend.yaml", CONFIGURED_ADMIN_BACKEND)
    kubectl_log = tmp_path / "kubectl.log"

    result = _run_deploy(project_root, bin_dir, kubectl_log)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Refusing to deploy" not in combined
    assert "Deployment complete!" in combined
    assert kubectl_log.exists()


# ---------------------------------------------------------------------------
# codex diff-review High (2026-09-28): the fixture tests above prove the
# guard's LOGIC works, but the guard is only as good as the pattern it
# scans for -- manifests/athena-prod/ollama.yaml also ships
# `storageClassName: YOUR_STORAGE_CLASS`, which the original
# YOUR_REGISTRY|CONFIGURE_ME pattern never matched. This test runs the
# REAL deploy.sh against the REAL, shipped manifests/athena-prod/
# directory (no fixture substitution) and asserts it refuses -- proving
# the guard actually covers the placeholder set this repo ships, not just
# a fixture built to match whatever the guard happens to check today.
# ---------------------------------------------------------------------------

def test_guard_refuses_against_the_real_shipped_manifests():
    assert (REPO_ROOT / "manifests" / "athena-prod" / "ollama.yaml").exists(), (
        "sanity: this test must run against the real repo tree"
    )

    env = dict(os.environ)
    kubectl_log = REPO_ROOT / "tests" / "unit" / ".tmp_kubectl_log_for_real_manifest_test"
    if kubectl_log.exists():
        kubectl_log.unlink()

    bin_dir = kubectl_log.parent / ".tmp_bin_for_real_manifest_test"
    bin_dir.mkdir(exist_ok=True)
    kubectl_stub = bin_dir / "kubectl"
    kubectl_stub.write_text(STUB_KUBECTL, encoding="utf-8")
    kubectl_stub.chmod(kubectl_stub.stat().st_mode | stat.S_IEXEC)

    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["KUBECTL_LOG"] = str(kubectl_log)
    env.pop("REGISTRY", None)

    try:
        result = subprocess.run(
            ["bash", str(DEPLOY_SH), "deploy"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "Refusing to deploy" in combined
        assert "YOUR_REGISTRY" in combined
        assert "YOUR_STORAGE_CLASS" in combined, (
            "the broadened YOUR_[A-Z_]+ pattern must catch ollama.yaml's "
            "storageClassName placeholder, not just YOUR_REGISTRY"
        )
        assert not kubectl_log.exists(), "guard must fire before any kubectl call"
    finally:
        if kubectl_log.exists():
            kubectl_log.unlink()
        shutil.rmtree(bin_dir, ignore_errors=True)
