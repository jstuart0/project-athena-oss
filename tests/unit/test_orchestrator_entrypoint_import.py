"""ATHENA-89 Phase 7 (DC14 item 0, blocking) -- hank's build gate caught a
circular-import ImportError at real container startup
(`ImportError: cannot import name 'maybe_post_synthesis_fallback' from
'orchestrator.helpers'`) that every existing unit test missed, because
every existing test imports `orchestrator.nodes` (directly or via
`orchestrator.nodes._runtime`) before it imports anything else
orchestrator-related -- exactly the import order helpers.py's own
docstring assumes production code always takes. It doesn't: main.py
imports `orchestrator.automation_agent` (main.py:77) and
`orchestrator.semantic_cache` (main.py:98) BEFORE it imports
`orchestrator.nodes` (main.py:144), and P3 added module-level
`from orchestrator.helpers import ...` lines to both of those modules --
each one an independent trigger for the same partial-init cycle.

This test doesn't import through the test-harness's own sys.modules
stubbing (which papers over exactly this class of bug by controlling
import order itself). It spawns a FRESH subprocess with the container's
real cwd/PYTHONPATH/entry-module, for all three services with their own
container image: orchestrator, gateway, jarvis-web backend. A regression
here means the container that image actually failed to start; nothing
short of a real subprocess boundary catches it.

Fixture note: `langgraph` and `prometheus_client` are real runtime
dependencies (requirements.txt) but are not installed in the venv that
runs `pytest tests/unit` (every existing orchestrator/gateway unit test
already stubs them via sys.modules -- that trick doesn't cross a
subprocess boundary, so this test instead prepends
tests/fixtures/entrypoint_import_stubs/ to PYTHONPATH, which holds minimal
stand-ins for the handful of names main.py imports from each at module
level).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB_DIR = REPO_ROOT / "tests" / "fixtures" / "entrypoint_import_stubs"


def _run_import(cwd: Path, pythonpath_dirs: list[Path], extra_env: dict[str, str]):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in pythonpath_dirs)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_clean_import(result: subprocess.CompletedProcess, label: str):
    assert result.returncode == 0, (
        f"{label}: `import main` exited {result.returncode} in a fresh "
        f"subprocess (the container's real import order) -- this is what "
        f"the built image actually does at startup.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_orchestrator_entrypoint_imports_cleanly():
    """Mirrors src/orchestrator/Dockerfile: WORKDIR /app, ENV PYTHONPATH=
    /app/orchestrator:/app (i.e. src/ is /app, both orchestrator/ and src/
    on the path), CMD python -m uvicorn main:app -- main resolves to
    src/orchestrator/main.py."""
    cwd = REPO_ROOT / "src" / "orchestrator"
    result = _run_import(
        cwd,
        [STUB_DIR, REPO_ROOT / "src" / "orchestrator", REPO_ROOT / "src"],
        {
            "SERVICE_API_KEY": "test-key-entrypoint-import",
            "DEV_MODE": "true",
            "ADMIN_API_URL": "http://localhost:8080",
        },
    )
    _assert_clean_import(result, "orchestrator (src/orchestrator/Dockerfile)")


def test_gateway_entrypoint_imports_cleanly():
    """Mirrors src/gateway/Dockerfile: `shared` is pip-installed editable
    from /app/shared and gateway/main.py is copied to /app/main.py, WORKDIR
    /app. PYTHONPATH substitutes for the editable install in this
    non-container reproduction."""
    cwd = REPO_ROOT / "src" / "gateway"
    result = _run_import(
        cwd,
        [STUB_DIR, REPO_ROOT / "src"],
        {
            "SERVICE_API_KEY": "test-key-entrypoint-import",
            "ADMIN_API_URL": "http://localhost:8080",
        },
    )
    _assert_clean_import(result, "gateway (src/gateway/Dockerfile)")


def test_jarvis_web_backend_entrypoint_imports_cleanly():
    """Mirrors apps/jarvis-web/Dockerfile: WORKDIR /app/backend, main.py
    and a copied sibling admin_url.py live there together -- no `shared`
    package dependency, so no repo src/ needs to be on PYTHONPATH."""
    cwd = REPO_ROOT / "apps" / "jarvis-web" / "backend"
    result = _run_import(
        cwd,
        [STUB_DIR],
        {},
    )
    _assert_clean_import(result, "jarvis-web backend (apps/jarvis-web/Dockerfile)")
