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


# E12: the refusal table can't grow without bound ---------------------------

def test_refusal_table_is_capped(captured_logs, clock):
    cap = service_key.MAX_TRACKED_REFUSALS
    assert 16 <= cap <= 4096
    for index in range(cap + 40):
        assert service_key.note_admin_refusal(401, f"/api/zz-route-{index}") is True
    assert len(service_key._last_logged) <= cap
    # An evicted key logs again instead of being silenced: every distinct
    # route above got its line.
    assert len(_refusals(captured_logs)) == cap + 40
    # The most recently logged keys are the ones kept: still rate-limited.
    for index in (cap + 39, cap + 38, 40):
        assert service_key.note_admin_refusal(401, f"/api/zz-route-{index}") is True
    assert len(_refusals(captured_logs)) == cap + 40
    # The oldest were the ones dropped: they log again.
    assert service_key.note_admin_refusal(401, "/api/zz-route-0") is True
    assert service_key.note_admin_refusal(401, "/api/zz-route-39") is True
    assert len(_refusals(captured_logs)) == cap + 42
    assert len(service_key._last_logged) <= cap


# E13: a route that isn't a template never reaches the log -------------------

BAD_ROUTES = {
    "query": "/api/features/public?token=zz-secret-token",
    "overlong": "/api/" + "zz-secret-segment/" * 20,
    "not_a_string": 12345,
    "none": None,
}


@pytest.mark.parametrize("kind", sorted(BAD_ROUTES))
def test_a_route_that_is_not_a_template_is_replaced(kind, captured_logs, caplog, clock):
    caplog.set_level(logging.DEBUG)
    assert service_key.note_admin_refusal(401, BAD_ROUTES[kind]) is True
    assert captured_logs == [
        {"event": "admin_backend_refused", "log_level": "error", "status": 401,
         "route": service_key.INVALID_ROUTE},
    ]
    assert "zz-secret" not in repr(captured_logs) and "zz-secret" not in caplog.text
    assert "?" not in service_key.INVALID_ROUTE and len(service_key.INVALID_ROUTE) < 40


def test_replaced_routes_share_one_rate_limit_key(captured_logs, clock):
    for index in range(30):
        assert service_key.note_admin_refusal(401, f"/api/x?attempt={index}") is True
    assert len(_refusals(captured_logs)) == 1
    assert list(service_key._last_logged) == [(service_key.INVALID_ROUTE, 401)]


def test_a_route_at_the_length_limit_is_kept(captured_logs, clock):
    route = "/api/" + "a" * (service_key.MAX_ROUTE_LENGTH - 5)
    assert len(route) == service_key.MAX_ROUTE_LENGTH
    assert service_key.note_admin_refusal(401, route) is True
    assert service_key.note_admin_refusal(401, route + "a") is True
    assert [r["route"] for r in _refusals(captured_logs)] == [route, service_key.INVALID_ROUTE]


# E14: "never raises" holds for any status -----------------------------------

@pytest.mark.parametrize("status", [[401], {"status": 401}, None, "401", 401.5], ids=repr)
def test_an_odd_status_is_not_a_refusal_and_does_not_raise(status, captured_logs, clock):
    assert service_key.note_admin_refusal(status, ROUTE) is False
    assert captured_logs == []


# E15: a key that can't be a header value is never sent ----------------------
# httpx/h11 refuse such a value with "Illegal header value b'<the key>'", and
# callers log the exception text.

UNUSABLE_KEYS = {
    "trailing_newline": "zz-sentinel-key\n",
    "carriage_return": "zz-sentinel-key\r",
    "leading_space": " zz-sentinel-key",
    "trailing_space": "zz-sentinel-key ",
    "inner_space": "zz-sentinel key",
    "tab": "zz-sentinel\tkey",
    "delete": "zz-sentinel-key\x7f",
    "non_ascii": "zz-sentinel-key\u00ff",
}


@pytest.mark.parametrize("kind", sorted(UNUSABLE_KEYS))
def test_a_key_outside_visible_ascii_sends_no_header(kind, monkeypatch, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    _set_key(monkeypatch, UNUSABLE_KEYS[kind])
    from shared.config import get_config

    assert get_config().service_api_key == UNUSABLE_KEYS[kind], "the configured value really carries it"
    assert service_key.service_key_headers() == {}
    assert service_key.service_key_headers() == {}
    assert captured_logs == [
        {"event": "service_api_key_unusable", "log_level": "error", "variable": "SERVICE_API_KEY"},
    ], "one line, naming the variable"
    assert "zz-sentinel" not in repr(captured_logs) and "zz-sentinel" not in caplog.text


def test_reporting_an_unusable_key_never_raises(monkeypatch):
    attempts = []

    class _Raising:
        def error(self, *args, **kwargs):
            attempts.append(args)
            raise RuntimeError("logger is broken")

    monkeypatch.setattr(service_key, "logger", _Raising())
    _set_key(monkeypatch, UNUSABLE_KEYS["trailing_newline"])
    assert service_key.service_key_headers() == {}
    assert len(attempts) == 1, "the raising logger was really called"


def test_every_visible_ascii_character_is_a_usable_key(monkeypatch, captured_logs):
    key = "".join(chr(code) for code in range(0x21, 0x7F))
    _set_key(monkeypatch, key)
    assert service_key.service_key_headers() == {"X-Service-Key": key}
    assert captured_logs == []


def test_an_unusable_key_really_is_refused_by_httpx():
    """Why the rule exists: the client library refuses the value and puts
    all of it in the exception text. A loopback listener, so the request
    gets as far as writing its headers."""
    import asyncio
    import socket

    import httpx

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    async def send():
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(f"http://127.0.0.1:{port}/", headers={"X-Service-Key": "zz-sentinel-key\n"})

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(httpx.LocalProtocolError) as refused:
            loop.run_until_complete(send())
    finally:
        loop.close()
        listener.close()
    assert "zz-sentinel-key" in str(refused.value)


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


# The Control Agent's inline form of the unusable-key rule ------------------
#
# Its host gets no ``shared`` module, so ``send_progress_callback`` reads the
# key from the environment itself and applies the same rule as
# ``is_header_safe``.

CALLBACK_BASE = "http://admin:8080/api/model-downloads"


def _drive_progress_callback(monkeypatch, key, calls=2, *, url=CALLBACK_BASE, allowed="admin", status=200):
    """Requests recorded at the socket for `calls` progress callbacks to
    `url`, sent with SERVICE_API_KEY set to `key` and ALLOWED_CALLBACK_HOSTS
    set to `allowed`; admin-backend answers `status`."""
    import asyncio
    import importlib

    import httpx

    agent = importlib.import_module("control_agent.huggingface")
    agent._reset_callback_log_state_for_tests()
    if key is None:
        monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SERVICE_API_KEY", key)
    monkeypatch.setenv("ALLOWED_CALLBACK_HOSTS", allowed)
    recorded = []

    def handler(request):
        recorded.append(request)
        return httpx.Response(status, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(handler), **kwargs))
    loop = asyncio.new_event_loop()
    try:
        for _ in range(calls):
            loop.run_until_complete(agent.send_progress_callback(url, 7, "downloading"))
    finally:
        loop.close()
        agent._reset_callback_log_state_for_tests()
    return recorded


@pytest.mark.parametrize("kind", sorted(UNUSABLE_KEYS))
def test_control_agent_never_sends_a_key_outside_visible_ascii(kind, monkeypatch, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    recorded = _drive_progress_callback(monkeypatch, UNUSABLE_KEYS[kind])
    assert len(recorded) == 2, "the callback is still attempted"
    assert all("X-Service-Key" not in request.headers for request in recorded)
    assert captured_logs == [
        {"event": "service_api_key_unusable", "log_level": "error", "variable": "SERVICE_API_KEY"},
    ], "one line across both calls, naming the variable"
    assert "zz-sentinel" not in repr(captured_logs) and "zz-sentinel" not in caplog.text


def test_control_agent_sends_every_visible_ascii_character(monkeypatch, captured_logs):
    """Positive control for the test above: the same drive with a usable key
    sends it, so the absent header there is the rule and not the harness."""
    key = "".join(chr(code) for code in range(0x21, 0x7F))
    recorded = _drive_progress_callback(monkeypatch, key)
    assert [request.headers.get("X-Service-Key") for request in recorded] == [key, key]
    assert captured_logs == []


def test_control_agent_rule_is_the_helper_s_rule():
    """The inline check and ``is_header_safe`` agree on every single
    character and on the named unusable keys."""
    import importlib

    agent = importlib.import_module("control_agent.huggingface")
    samples = [chr(code) for code in range(0x00, 0x100)] + list(UNUSABLE_KEYS.values()) + ["zz-sentinel-key"]
    assert len(samples) >= 256
    disagreements = [
        repr(sample) for sample in samples
        if agent._is_header_safe(sample) != service_key.is_header_safe(sample)
    ]
    assert disagreements == []
    assert agent._is_header_safe("zz-sentinel-key") is True
    assert agent._is_header_safe("zz-sentinel-key\n") is False


# The edge senders read the key when they call, and never send a bad one ----
#
# ``gateway.main`` keeps a module constant holding the key as it was when the
# module was imported. A caller that sent a constant like that would keep
# sending a rotated-out or not-yet-set key, and a test that never changes the
# key after the import can't tell. Each sender below goes through
# ``service_key_headers()``, so it also sends no header at all for an unset
# key or one that can't be a header value.

ROTATED_KEY = "zz-rotated-after-import"
EDGE_ADMIN_URL = "http://admin-backend:8080"
EDGE_ORCHESTRATOR_URL = "http://orchestrator:8001"


def _edge_senders(monkeypatch):
    """{name: (module, async call, the path it requests)}"""
    import importlib
    import types

    gateway = importlib.import_module("gateway.main")
    livekit = importlib.import_module("gateway.livekit_service")
    scraper = importlib.import_module("rag.site_scraper.main")

    async def feature_flag():
        gateway._feature_flag_cache.clear()
        return await gateway.get_feature_flag("zz_flag")

    async def metric():
        return await gateway._log_metric_to_db(
            timestamp=1.0, model="m1", backend="ollama", latency_seconds=0.5, tokens=10, tokens_per_second=20.0)

    async def follow_up_flags():
        service = object.__new__(livekit.LiveKitService)
        service._follow_ups_enabled, service._last_feature_flag_check = False, 0.0
        service._feature_flag_check_interval = 60.0
        return await service._refresh_feature_flags()

    async def warmup():
        class _Sessions:
            async def get_session_for_device(self, device_id):
                return "zz-session"

        monkeypatch.setattr(gateway, "device_session_mgr", _Sessions())
        monkeypatch.setattr(gateway, "ORCHESTRATOR_URL", EDGE_ORCHESTRATOR_URL)
        return await gateway._warmup_session("zz-device")

    async def music_config():
        request = types.SimpleNamespace(url=types.SimpleNamespace(scheme="http"))
        return await gateway.get_music_config(request)

    async def music_token():
        monkeypatch.setattr(gateway, "_ma_auth_token_cache", None)
        monkeypatch.delenv("MA_AUTH_TOKEN", raising=False)
        return await gateway._fetch_ma_auth_token()

    music = "/api/external-api-keys/public/music-assistant/credentials"
    return {
        "gateway.main.get_feature_flag": (gateway, feature_flag, "/api/features/public"),
        "gateway.main._log_metric_to_db": (gateway, metric, "/api/llm-backends/metrics"),
        "gateway.main.list_models": (gateway, gateway.list_models, "/api/llm-backends/public"),
        "gateway.main._warmup_session": (gateway, warmup, "/session/zz-session/warmup"),
        "gateway.main.get_music_config": (gateway, music_config, music),
        "gateway.main._fetch_ma_auth_token": (gateway, music_token, music),
        "gateway.livekit_service._refresh_feature_flags": (livekit, follow_up_flags, "/api/features/public"),
        "gateway.livekit_service.fetch_livekit_credentials": (
            livekit, livekit.fetch_livekit_credentials, "/api/external-api-keys/public/livekit/credentials"),
        "rag.site_scraper.main.load_config": (scraper, scraper.load_config, "/api/site-scraper/config/public"),
    }


EDGE_SENDER_NAMES = (
    "gateway.main.get_feature_flag", "gateway.main._log_metric_to_db", "gateway.main.list_models",
    "gateway.main._warmup_session", "gateway.main.get_music_config", "gateway.main._fetch_ma_auth_token",
    "gateway.livekit_service._refresh_feature_flags", "gateway.livekit_service.fetch_livekit_credentials",
    "rag.site_scraper.main.load_config",
)
STALE_KEY = "zz-import-time-key"


def _edge_requests(monkeypatch, name, key, header_for=None):
    """The requests `name` makes with SERVICE_API_KEY set to `key` after the
    module was imported."""
    import asyncio

    import httpx

    module, call, path = _edge_senders(monkeypatch)[name]
    monkeypatch.setenv("ADMIN_API_URL", EDGE_ADMIN_URL)
    if hasattr(module, "ADMIN_API_URL"):
        monkeypatch.setattr(module, "ADMIN_API_URL", EDGE_ADMIN_URL)
    if hasattr(module, "metric_client"):
        monkeypatch.setattr(module, "metric_client", None)
    if header_for is not None:
        monkeypatch.setattr(module, "service_key_headers", header_for)
    _set_key(monkeypatch, key)
    from shared.admin_url import _clear_cache_for_tests as clear_admin_url

    clear_admin_url()
    recorded = []

    def handler(request):
        recorded.append(request)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(handler), **kwargs))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(call())
    finally:
        loop.close()
        clear_admin_url()
    assert [request.url.path for request in recorded] == [path]
    return recorded


@pytest.mark.parametrize("name", EDGE_SENDER_NAMES)
def test_edge_sender_sends_the_key_as_it_is_at_call_time(name, monkeypatch):
    (request,) = _edge_requests(monkeypatch, name, ROTATED_KEY)
    assert request.headers.get("X-Service-Key") == ROTATED_KEY


@pytest.mark.parametrize("name", EDGE_SENDER_NAMES)
def test_the_call_time_check_sees_an_import_time_key(name, monkeypatch):
    """Positive control: a caller that sends a value fixed earlier is seen
    sending something other than the key now configured."""
    (request,) = _edge_requests(monkeypatch, name, ROTATED_KEY, header_for=lambda: {"X-Service-Key": STALE_KEY})
    assert request.headers.get("X-Service-Key") == STALE_KEY


@pytest.mark.parametrize("kind", ["trailing_newline", "non_ascii", "inner_space"])
@pytest.mark.parametrize("name", EDGE_SENDER_NAMES)
def test_edge_sender_never_sends_an_unusable_key(name, kind, monkeypatch, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    (request,) = _edge_requests(monkeypatch, name, UNUSABLE_KEYS[kind])
    assert "X-Service-Key" not in request.headers
    unusable = [r for r in captured_logs if r.get("event") == "service_api_key_unusable"]
    assert unusable == [{"event": "service_api_key_unusable", "log_level": "error", "variable": "SERVICE_API_KEY"}]
    assert "zz-sentinel" not in repr(captured_logs) and "zz-sentinel" not in caplog.text


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
@pytest.mark.parametrize("name", EDGE_SENDER_NAMES)
def test_edge_sender_sends_no_header_for_an_unset_key(name, value, monkeypatch):
    (request,) = _edge_requests(monkeypatch, name, value)
    assert "X-Service-Key" not in request.headers, "not even an empty one"


def test_edge_sender_population(monkeypatch):
    assert set(_edge_senders(monkeypatch)) == set(EDGE_SENDER_NAMES) and len(EDGE_SENDER_NAMES) == 9


# Closed world: no hand-built key header in the edge sender files -----------
#
# The senders above are the ones a unit test can drive. The rest (the
# gateway's orchestrator client default, the Wyoming handler's query, the
# directions base-knowledge fetch, jarvis-web's chat routes) are held to the
# same rule by source: the only way to put the key on a request is the helper.

# file -> the fewest helper calls it holds
EDGE_SENDER_FILES = {
    "src/gateway/main.py": 8,
    "src/gateway/livekit_service.py": 2,
    "src/gateway/wyoming_bridge.py": 2,
    "src/rag/directions/main.py": 2,
    "src/rag/site_scraper/main.py": 1,
    "apps/jarvis-web/backend/main.py": 5,
}
# jarvis-web has no shared.config; its own helper is the one place the header
# is built there.
LOCAL_HELPERS = {"apps/jarvis-web/backend/main.py": "_service_key_headers"}
# Still hand-built, and not changed by the route-auth review: each sends
# get_config().service_api_key to the orchestrator or the mode service.
KNOWN_RAW_SENDERS = {"src/gateway/livekit_integration.py": 1, "src/gateway/mode_gate.py": 1}


def _key_header_sites(source):
    """([(lineno, enclosing function)] of dict literals with an
    "X-Service-Key" key, number of key-header helper calls)."""
    tree = ast.parse(source)
    owner = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                owner.setdefault(id(node), func.name)
    raw = sorted(
        (node.lineno, owner.get(id(node), "<module>"))
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and any(isinstance(key, ast.Constant) and key.value == "X-Service-Key" for key in node.keys)
    )
    helper_calls = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None))
        in ("service_key_headers", "_service_key_headers")
    )
    return raw, helper_calls


@pytest.mark.parametrize("rel", sorted(EDGE_SENDER_FILES))
def test_edge_sender_files_build_the_key_header_only_through_the_helper(rel):
    raw, helper_calls = _key_header_sites((REPO_ROOT / rel).read_text())
    allowed = LOCAL_HELPERS.get(rel)
    outside = [(lineno, func) for lineno, func in raw if func != allowed]
    assert outside == [], f"hand-built X-Service-Key header(s) in {rel}: {outside}"
    assert len(raw) == (1 if allowed else 0)
    assert helper_calls >= EDGE_SENDER_FILES[rel], (rel, helper_calls)


def test_known_raw_senders_are_exactly_the_two():
    found = {
        rel: len(_key_header_sites((REPO_ROOT / rel).read_text())[0])
        for rel in sorted(str(p.relative_to(REPO_ROOT)) for p in (_SRC / "gateway").glob("*.py"))
    }
    assert {rel: n for rel, n in found.items() if n} == KNOWN_RAW_SENDERS


def test_key_header_site_rule_planted_self_test():
    planted = (
        "async def raw(client):\n"
        "    await client.get('/x', headers={'X-Service-Key': KEY})\n"
        "async def through_the_helper(client):\n"
        "    await client.get('/x', headers=service_key_headers())\n"
        "def _service_key_headers():\n"
        "    return {'X-Service-Key': KEY}\n"
        "async def merged(client):\n"
        "    await client.get('/x', headers={'Accept': 'a', **_service_key_headers()})\n"
        "CLIENT = httpx.AsyncClient(headers={'X-Service-Key': KEY})\n"
    )
    raw, helper_calls = _key_header_sites(planted)
    assert raw == [(2, "raw"), (6, "_service_key_headers"), (9, "<module>")]
    assert helper_calls == 2


def test_gateway_orchestrator_client_default_comes_from_the_helper():
    """The default header is fixed when the client is built. Built from the
    helper, an unset or unusable key installs no default at all."""
    tree = ast.parse((_SRC / "gateway" / "main.py").read_text())
    built = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "orchestrator_client" for t in node.targets)
        and isinstance(node.value, ast.Call)
    ]
    assert len(built) == 1, "one construction of the gateway's orchestrator client"
    headers = [ast.unparse(kw.value) for kw in built[0].keywords if kw.arg == "headers"]
    assert headers == ["service_key_headers()"]


# The Control Agent sends the key only to a host it was told to trust --------
#
# The callback URL arrives in a request body. ALLOWED_CALLBACK_HOSTS also lets
# through any name that resolves to a public address, and loopback on a
# separate agent host is that host, not admin-backend. So the key goes only to
# a host that is itself an entry of the list.

AGENT_KEY = "zz-agent-key"

UNTRUSTED_CALLBACKS = {
    # id: (callback base URL, ALLOWED_CALLBACK_HOSTS)
    "another_host": ("http://other.example:8080/api/model-downloads", "admin"),
    "public_address_passes_the_ssrf_rule": ("http://93.184.216.34/api/model-downloads", "admin"),
    "public_name_passes_the_ssrf_rule": ("https://downloads.example.org/api/model-downloads", "admin"),
    "loopback_name_not_listed": ("http://localhost:8080/api/model-downloads", "admin"),
    "loopback_address_not_listed": ("http://127.0.0.1:8080/api/model-downloads", "admin,localhost"),
    "empty_list": ("http://admin:8080/api/model-downloads", ""),
    "entry_is_a_prefix": ("http://admin.evil.example/api/model-downloads", "admin"),
    "entry_is_a_suffix": ("http://evil-admin/api/model-downloads", "admin"),
    "entry_in_the_userinfo": ("http://admin@evil.example/api/model-downloads", "admin"),
    "entry_in_the_path": ("http://evil.example/admin/api/model-downloads", "admin"),
    "no_host": ("http:///api/model-downloads", "admin"),
}

TRUSTED_CALLBACKS = {
    "exact_entry": ("http://admin:8080/api/model-downloads", "admin"),
    "exact_entry_among_several": ("https://admin.example.org/api/model-downloads", "localhost, admin.example.org"),
    "loopback_name_listed": ("http://localhost:8080/api/model-downloads", "localhost"),
    "loopback_address_listed": ("http://127.0.0.1:8080/api/model-downloads", "127.0.0.1"),
}


@pytest.mark.parametrize("kind", sorted(UNTRUSTED_CALLBACKS))
def test_control_agent_withholds_the_key_from_a_host_not_on_its_list(kind, monkeypatch, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    url, allowed = UNTRUSTED_CALLBACKS[kind]
    recorded = _drive_progress_callback(monkeypatch, AGENT_KEY, url=url, allowed=allowed)
    if kind != "no_host":
        assert len(recorded) == 2, "the callback is still sent, without the key"
    assert all("X-Service-Key" not in request.headers for request in recorded)
    withheld = [r for r in captured_logs if r.get("event") == "progress_callback_service_key_withheld"]
    assert len(withheld) == 1, captured_logs
    assert withheld[0]["log_level"] == "warning" and withheld[0]["variable"] == "ALLOWED_CALLBACK_HOSTS"
    assert AGENT_KEY not in repr(captured_logs) and AGENT_KEY not in caplog.text


@pytest.mark.parametrize("kind", sorted(TRUSTED_CALLBACKS))
def test_control_agent_sends_the_key_to_an_exact_list_entry(kind, monkeypatch, captured_logs):
    url, allowed = TRUSTED_CALLBACKS[kind]
    recorded = _drive_progress_callback(monkeypatch, AGENT_KEY, url=url, allowed=allowed)
    assert [request.headers.get("X-Service-Key") for request in recorded] == [AGENT_KEY, AGENT_KEY]
    assert captured_logs == []


def test_callback_trust_populations():
    assert len(UNTRUSTED_CALLBACKS) >= 11 and len(TRUSTED_CALLBACKS) >= 4
    assert "loopback_name_not_listed" in UNTRUSTED_CALLBACKS and "exact_entry" in TRUSTED_CALLBACKS


# A callback that went out without a key and came back 422 was refused -------
#
# The progress route takes the service key only. Without the header it
# answers 422 (a missing required header), which is a refusal of the
# credential. With the header, a 422 is about the body.

def _agent_refusals(logs):
    return [r for r in logs if r.get("event") == "admin_backend_refused"]


@pytest.mark.parametrize("why", ["unset", "unusable", "host_not_listed"])
def test_control_agent_reports_a_keyless_422_as_a_refusal(why, monkeypatch, captured_logs):
    key, allowed = {
        "unset": (None, "admin"), "unusable": ("zz-sentinel-key\n", "admin"), "host_not_listed": (AGENT_KEY, "elsewhere"),
    }[why]
    recorded = _drive_progress_callback(monkeypatch, key, calls=3, allowed=allowed, status=422)
    assert len(recorded) == 3 and all("X-Service-Key" not in request.headers for request in recorded)
    assert _agent_refusals(captured_logs) == [{
        "event": "admin_backend_refused", "log_level": "error", "status": 422,
        "route": "/api/model-downloads/internal/{download_id}/progress",
    }], "one line for the three callbacks in the window"
    assert [r for r in captured_logs if r.get("event") == "callback_failed"] == []


def test_control_agent_refusal_line_returns_after_a_minute(monkeypatch, captured_logs):
    """The agent's inline once-a-minute rule (the helper's, without the
    helper): refused at 0, 59 and 60 seconds, logged at 0 and 60."""
    import asyncio
    import importlib

    import httpx

    agent = importlib.import_module("control_agent.huggingface")
    agent._reset_callback_log_state_for_tests()
    monkeypatch.setenv("SERVICE_API_KEY", AGENT_KEY)
    monkeypatch.setenv("ALLOWED_CALLBACK_HOSTS", "admin")
    now = [0.0]
    monkeypatch.setattr(agent, "_clock", lambda: now[0])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *args, **kwargs: real_client(
            *args, transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})), **kwargs))
    lines_after = []
    loop = asyncio.new_event_loop()
    try:
        for second in (0.0, 59.0, 60.0):
            now[0] = second
            loop.run_until_complete(agent.send_progress_callback(CALLBACK_BASE, 7, "downloading"))
            lines_after.append(len(_agent_refusals(captured_logs)))
    finally:
        loop.close()
        agent._reset_callback_log_state_for_tests()
    assert lines_after == [1, 1, 2]


def test_control_agent_keeps_a_keyed_422_as_a_failed_callback(monkeypatch, captured_logs):
    """The body was refused, not the credential: no refusal line."""
    recorded = _drive_progress_callback(monkeypatch, AGENT_KEY, status=422)
    assert [request.headers.get("X-Service-Key") for request in recorded] == [AGENT_KEY, AGENT_KEY]
    assert _agent_refusals(captured_logs) == []
    failed = [r for r in captured_logs if r.get("event") == "callback_failed"]
    assert len(failed) == 2 and all(r["status_code"] == 422 and r["log_level"] == "warning" for r in failed)


# A transport failure of a keyed admin call is visible ------------------------
#
# With certificate verification back on, a private CA that isn't in the trust
# bundle fails these calls before any answer comes back: nothing is refused,
# so there is no admin_backend_refused line. The two follow-up flag refreshes
# logged that at DEBUG only.

_WYOMING_STAND_INS = (
    "wyoming", "wyoming.server", "wyoming.event", "wyoming.audio", "wyoming.asr", "wyoming.tts",
    "wyoming.info", "wyoming.handle",
)


def _wyoming_bridge_module():
    """gateway.wyoming_bridge with its handler class defined; where the
    `wyoming` package isn't installed, loaded once with empty stand-ins for
    its names (the method under test touches none of them)."""
    import importlib
    import types

    real = importlib.import_module("gateway.wyoming_bridge")
    if getattr(real, "WYOMING_AVAILABLE", False):
        return real

    class _AnyName(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return type(name, (), {})

    saved = {name: sys.modules.get(name) for name in _WYOMING_STAND_INS}
    sys.modules.update({name: _AnyName(name) for name in _WYOMING_STAND_INS})
    try:
        spec = importlib.util.spec_from_file_location(
            "_helper_test_wyoming_bridge", _SRC / "gateway" / "wyoming_bridge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


def _flag_refreshers():
    import importlib

    livekit = importlib.import_module("gateway.livekit_service")
    wyoming = _wyoming_bridge_module()
    return {
        "livekit": (livekit, livekit.LiveKitService, "feature_flag_check_error"),
        "wyoming": (wyoming, wyoming.AthenaWyomingHandler, "wyoming_feature_flag_check_error"),
    }


@pytest.fixture
def flag_refreshers():
    """Requested before `captured_logs`: a gateway module configures logging
    when it is first imported, which would detach a capture already open."""
    return _flag_refreshers()


@pytest.mark.parametrize("which", ["livekit", "wyoming"])
def test_a_failed_flag_refresh_is_a_warning_with_the_error_type_only(
        which, monkeypatch, flag_refreshers, captured_logs, caplog):
    import asyncio

    import httpx

    caplog.set_level(logging.DEBUG)
    module, cls, event = flag_refreshers[which]
    monkeypatch.setattr(module, "ADMIN_API_URL", "https://zz-admin.example:8443")
    _set_key(monkeypatch, "zz-flag-key")

    def handler(request):
        raise httpx.ConnectError(f"certificate verify failed for {request.url}", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(handler), **kwargs))
    instance = object.__new__(cls)
    instance._follow_ups_enabled, instance._last_feature_flag_check = True, 0.0
    instance._feature_flag_check_interval = 60.0
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(instance._refresh_feature_flags())
    finally:
        loop.close()
    assert instance._follow_ups_enabled is True, "the flag keeps its value"
    # (Importing the module can log too; only the refresh's own line counts.)
    assert [r for r in captured_logs if "check" in str(r.get("event"))] == [
        {"event": event, "log_level": "warning", "error_type": "ConnectError"}]
    for text in (repr(captured_logs), caplog.text):
        assert "zz-admin.example" not in text and "zz-flag-key" not in text and "certificate" not in text
