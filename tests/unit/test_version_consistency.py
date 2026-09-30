"""Every version site agrees with the CHANGELOG's latest release heading.

Telemetry reports ``shared.__version__``, so a missed bump shows up as the
wrong version on every install. admin-backend's FastAPI ``app.version``
reads the same value, so the stale literal can't come back unnoticed.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

INIT_SITES = (
    "src/shared/__init__.py",
    "src/gateway/__init__.py",
    "src/jetson/__init__.py",
    "src/control_agent/__init__.py",
)
PYPROJECT = "src/shared/pyproject.toml"


def _init_version(rel: str) -> str | None:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def _changelog_release() -> str:
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## \[(\d+\.\d+\.\d+[^\]]*)\]", line)
        if m:
            return m.group(1)
    raise AssertionError("CHANGELOG.md has no released `## [x.y.z]` heading")


def _all_sites() -> dict[str, str | None]:
    sites = {rel: _init_version(rel) for rel in INIT_SITES}
    sites[PYPROJECT] = tomllib.loads((ROOT / PYPROJECT).read_text(encoding="utf-8"))["project"]["version"]
    return sites


def test_every_version_site_matches_the_changelog_release():
    sites = _all_sites()
    assert len(sites) >= 5
    assert "src/shared/__init__.py" in sites
    release = _changelog_release()
    mismatched = {rel: v for rel, v in sites.items() if v != release}
    assert not mismatched, f"version sites differ from CHANGELOG release {release}: {mismatched}"


def test_admin_backend_app_version_is_shared_version():
    # A subprocess keeps admin-backend's `main` out of this process, where
    # other services' `main` modules are imported too.
    code = (
        "import os, sys\n"
        "os.environ['DEV_MODE']='true'\n"
        "os.environ['DATABASE_URL']='sqlite:///:memory:'\n"
        "os.environ.setdefault('SERVICE_API_KEY','version-consistency-test-key')\n"
        "os.environ['QDRANT_URL']='http://127.0.0.1:1'\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "import shared\n"
        "from main import app\n"
        "print(app.version, shared.__version__)\n"
        "sys.exit(0 if app.version == shared.__version__ else 3)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT / "admin" / "backend",
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
