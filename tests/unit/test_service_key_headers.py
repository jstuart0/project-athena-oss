"""``shared.service_key``: the per-call ``X-Service-Key`` header and the
caller-side refusal log, plus the packaging that gets the module into
jarvis-web (which has no ``shared`` package).

The clock is patched, never slept on. The rate-limit state is reset around
every test.
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shared import service_key  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402

MODULE_PATH = _SRC / "shared" / "service_key.py"
JARVIS_BACKEND = REPO_ROOT / "apps" / "jarvis-web" / "backend"
JARVIS_DOCKERFILE = REPO_ROOT / "apps" / "jarvis-web" / "Dockerfile"
ROUTE = "/api/features/public"
BACKEND_COPY = "COPY apps/jarvis-web/backend/ /app/backend/"
HELPER_COPY = "COPY src/shared/service_key.py /app/backend/service_key.py"


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh_state():
    service_key._reset_for_tests()
    _clear_cache_for_tests()
    yield
    service_key._reset_for_tests()
    _clear_cache_for_tests()


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(service_key, "_clock", fake)
    return fake


def _set_key(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SERVICE_API_KEY", value)
    _clear_cache_for_tests()


def _refusals(logs):
    return [r for r in logs if r.get("event") == "admin_backend_refused"]


# E1 -----------------------------------------------------------------------

def test_set_key_gives_the_header(monkeypatch):
    _set_key(monkeypatch, "abc")
    assert service_key.service_key_headers() == {"X-Service-Key": "abc"}


# E2 -----------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_unset_or_empty_key_gives_no_header(monkeypatch, value):
    _set_key(monkeypatch, value)
    assert service_key.service_key_headers() == {}


# E3 -----------------------------------------------------------------------

def test_key_is_read_through_config_on_each_call(monkeypatch):
    _set_key(monkeypatch, "a")
    first = service_key.service_key_headers()
    _set_key(monkeypatch, "b")
    second = service_key.service_key_headers()
    assert (first, second) == ({"X-Service-Key": "a"}, {"X-Service-Key": "b"})


# E4 -----------------------------------------------------------------------

def test_each_call_returns_a_fresh_dict(monkeypatch):
    _set_key(monkeypatch, "abc")
    first = service_key.service_key_headers()
    first["X-Other"] = "1"
    first["X-Service-Key"] = "changed"
    second = service_key.service_key_headers()
    assert second is not first
    assert second == {"X-Service-Key": "abc"}


# E5 -----------------------------------------------------------------------

def test_headers_helper_logs_nothing(monkeypatch, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    for value in ("sentinel-key-value", None, ""):
        _set_key(monkeypatch, value)
        service_key.service_key_headers()
    assert captured_logs == []
    # shared.config announces its own (re)load through stdlib logging; the
    # helper adds nothing, and the key reaches no record at all.
    assert [r.name for r in caplog.records if r.name != "shared.config"] == []
    assert "sentinel-key-value" not in caplog.text


# E6 -----------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 503])
def test_refusal_statuses_are_noted(status, captured_logs, clock):
    assert service_key.note_admin_refusal(status, ROUTE) is True
    assert captured_logs == [
        {"event": "admin_backend_refused", "log_level": "error", "status": status, "route": ROUTE},
    ]


# E7 -----------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 204, 404, 422, 429, 500, 502])
def test_other_statuses_are_not_refusals(status, captured_logs, clock):
    assert service_key.note_admin_refusal(status, ROUTE) is False
    assert captured_logs == []


# E8 -----------------------------------------------------------------------

def test_refusals_are_rate_limited_per_route_and_status(captured_logs, clock):
    other = "/api/llm-backends/public"
    calls = [(0, ROUTE, 401), (1, ROUTE, 503), (2, other, 401), (59, ROUTE, 401), (60, ROUTE, 401)]
    for at, route, status in calls:
        clock.now = float(at)
        assert service_key.note_admin_refusal(status, route) is True
    assert [(r["route"], r["status"]) for r in _refusals(captured_logs)] == [
        (ROUTE, 401), (ROUTE, 503), (other, 401), (ROUTE, 401),
    ]
    assert len(captured_logs) == 4


# E9 -----------------------------------------------------------------------

def test_noting_never_raises_or_leaks_the_key(monkeypatch, captured_logs, caplog, clock):
    caplog.set_level(logging.DEBUG)
    _set_key(monkeypatch, "sentinel-key-value")
    assert service_key.service_key_headers() == {"X-Service-Key": "sentinel-key-value"}
    assert service_key.note_admin_refusal(401, ROUTE) is True
    assert len(_refusals(captured_logs)) == 1
    assert "sentinel-key-value" not in repr(captured_logs)
    assert "sentinel-key-value" not in caplog.text

    attempts = []

    class _Raising:
        def error(self, *args, **kwargs):
            attempts.append(args)
            raise RuntimeError("logger is broken")

    monkeypatch.setattr(service_key, "logger", _Raising())
    # A fresh window, so this call reaches the logger instead of being
    # rate-limited before it.
    service_key._reset_for_tests()
    assert service_key.note_admin_refusal(401, ROUTE) is True
    assert len(attempts) == 1, "the raising logger was really called"


# E10 ----------------------------------------------------------------------

def _module_level_imports(tree):
    """Every Import/ImportFrom that runs at import time: the module body,
    and the bodies of top-level if/try/with blocks, but nothing inside a
    function or class."""
    found = []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        else:
            pending.extend(n for n in ast.iter_child_nodes(node) if isinstance(n, ast.stmt))
            for handler in getattr(node, "handlers", []):
                pending.extend(handler.body)
    return found


def _non_stdlib_imports(source):
    problems, examined = [], 0
    for node in _module_level_imports(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append(f"relative import at line {node.lineno}")
                continue
            names = [node.module or ""]
        else:
            names = [alias.name for alias in node.names]
        for name in names:
            examined += 1
            top = name.split(".")[0]
            if top != "structlog" and top not in sys.stdlib_module_names:
                problems.append(f"{name} at line {node.lineno}")
    return problems, examined


def test_module_level_imports_are_stdlib_and_structlog():
    problems, examined = _non_stdlib_imports(MODULE_PATH.read_text())
    assert examined >= 1, "floor: the module imports something"
    assert problems == []


def test_import_rule_planted_self_test():
    planted = (
        "import time\n"
        "import structlog\n"
        "try:\n"
        "    from shared.config import get_config\n"
        "except ImportError:\n"
        "    from . import config\n"
        "if True:\n"
        "    import pydantic_settings\n"
        "def lazy():\n"
        "    from shared.config import get_config\n"
    )
    problems, examined = _non_stdlib_imports(planted)
    assert examined == 4
    assert sorted(problems) == [
        "pydantic_settings at line 8", "relative import at line 6", "shared.config at line 4",
    ]


# E11 ----------------------------------------------------------------------

_COPY_SHARED = re.compile(r"^COPY\s+(src/shared/[\w.]+\.py)\s+/app/backend/([\w.]+\.py)\s*$")

_IMAGE_CHECK = (
    "import sys\n"
    "sys.path.insert(0, {layout!r})\n"
    "import service_key\n"
    "assert service_key.__file__.startswith({layout!r}), service_key.__file__\n"
    "assert service_key.note_admin_refusal(401, '/api/x') is True\n"
    "assert 'shared' not in sys.modules, 'the helper pulled in the shared package'\n"
)


def _build_image_layout(dockerfile_text, target):
    """The jarvis-web image's /app/backend rebuilt without docker: the
    backend directory, then every single-file COPY from src/shared applied
    in Dockerfile order, but only the ones that come after the backend COPY
    (an earlier one is overwritten by it, exactly as in the image)."""
    lines = [line.strip() for line in dockerfile_text.splitlines()]
    assert BACKEND_COPY in lines, "the backend COPY line this test anchors on is gone"
    shutil.copytree(JARVIS_BACKEND, target, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    applied = []
    for line in lines[lines.index(BACKEND_COPY) + 1:]:
        match = _COPY_SHARED.match(line)
        if match:
            shutil.copyfile(REPO_ROOT / match.group(1), target / match.group(2))
            applied.append(match.group(2))
    return applied


def _run_image_check(layout):
    # -I: no PYTHONPATH, no user site, no cwd on sys.path. What's left is the
    # interpreter's own site-packages (structlog), like the image.
    return subprocess.run(
        [sys.executable, "-I", "-c", _IMAGE_CHECK.format(layout=str(layout))],
        cwd=layout, capture_output=True, text=True, timeout=60,
    )


def test_helper_loads_in_the_jarvis_web_image_layout(tmp_path):
    layout = tmp_path / "backend"
    applied = _build_image_layout(JARVIS_DOCKERFILE.read_text(), layout)
    assert "admin_url.py" in applied, "floor: the COPY parser sees the existing single-file copies"
    proc = _run_image_check(layout)
    assert proc.returncode == 0, (
        f"`import service_key` fails in the jarvis-web image layout "
        f"(copied from src/shared: {applied}): {proc.stderr[-600:]}"
    )


def test_image_layout_check_can_fail_and_can_pass(tmp_path):
    """Positive and negative control for the check above, independent of the
    Dockerfile: the real module passes; the local-dev shim shape (it imports
    ``shared``) fails even where ``shared`` happens to be installed."""
    good = tmp_path / "good"
    good.mkdir()
    shutil.copyfile(MODULE_PATH, good / "service_key.py")
    assert _run_image_check(good).returncode == 0

    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "service_key.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(_SRC)!r})\n"
        "from shared.service_key import note_admin_refusal, service_key_headers\n"
    )
    proc = _run_image_check(shim)
    assert proc.returncode != 0
    assert "shared" in proc.stderr

    missing = tmp_path / "missing"
    missing.mkdir()
    assert _run_image_check(missing).returncode != 0


# J1 -----------------------------------------------------------------------

def _helper_copy_problems(dockerfile_text):
    lines = [line.strip() for line in dockerfile_text.splitlines()]
    copies = [i for i, line in enumerate(lines) if line == HELPER_COPY]
    if len(copies) != 1:
        return f"{len(copies)} helper COPY line(s), expected exactly 1"
    if BACKEND_COPY not in lines:
        return "backend COPY line not found"
    if copies[0] < lines.index(BACKEND_COPY):
        return "helper COPY comes before the backend COPY (the shim would overwrite it)"
    return None


def test_jarvis_web_dockerfile_copies_the_helper():
    assert _helper_copy_problems(JARVIS_DOCKERFILE.read_text()) is None


def test_dockerfile_rule_planted_self_test():
    assert _helper_copy_problems(f"{BACKEND_COPY}\n{HELPER_COPY}\n") is None
    assert "0 helper COPY" in _helper_copy_problems(f"{BACKEND_COPY}\n")
    assert "2 helper COPY" in _helper_copy_problems(f"{BACKEND_COPY}\n{HELPER_COPY}\n{HELPER_COPY}\n")
    assert "before the backend COPY" in _helper_copy_problems(f"{HELPER_COPY}\n{BACKEND_COPY}\n")


# J2 -----------------------------------------------------------------------

def test_jarvis_web_local_dev_shim_reexports_the_helper():
    path = JARVIS_BACKEND / "service_key.py"
    assert path.exists(), "apps/jarvis-web/backend/service_key.py (the local-dev shim) is missing"
    source = path.read_text()
    spec = importlib.util.spec_from_file_location("_shim_jarvis_web_service_key", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.note_admin_refusal is service_key.note_admin_refusal
    assert module.service_key_headers is service_key.service_key_headers
    defined = [
        node.name for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == [], "the shim re-exports; it defines nothing of its own"


# J3 -----------------------------------------------------------------------

def _imports_helper_in_module_body(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import) and any(alias.name == "service_key" for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "service_key":
            return True
    return False


def test_jarvis_web_imports_the_helper_at_module_level():
    assert _imports_helper_in_module_body((JARVIS_BACKEND / "main.py").read_text()), (
        "apps/jarvis-web/backend/main.py doesn't import service_key in its module body"
    )


def test_module_level_import_rule_planted_self_test():
    assert _imports_helper_in_module_body("import os\nimport service_key\n")
    assert _imports_helper_in_module_body("from service_key import note_admin_refusal\n")
    assert not _imports_helper_in_module_body("def f():\n    import service_key\n")
    assert not _imports_helper_in_module_body("try:\n    import service_key\nexcept ImportError:\n    pass\n")
    assert not _imports_helper_in_module_body("if True:\n    import service_key\n")
    assert not _imports_helper_in_module_body("from shared.service_key import note_admin_refusal\n")
