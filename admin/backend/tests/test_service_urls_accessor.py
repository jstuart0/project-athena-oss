"""One reader of GATEWAY_URL and ORCHESTRATOR_URL, and the probe that uses them.

The accessor test is an AST scan of admin-backend's shipped code: the two names
appear as string literals only in ``app/services/service_urls.py``. That also
catches a variable-indirected read such as ``_base("GATEWAY_URL", ...)``.
"""
import ast
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
NAMES = {"GATEWAY_URL", "ORCHESTRATOR_URL"}
OWNER = "app/services/service_urls.py"


def shipped_files():
    files = [BACKEND / "main.py"] + sorted((BACKEND / "app").rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def literal_sites(source):
    return sorted(
        node.lineno for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in NAMES
    )


def test_the_names_are_literals_in_the_accessor_module_only():
    found = {}
    for path in shipped_files():
        sites = literal_sites(path.read_text())
        if sites:
            found[path.relative_to(BACKEND).as_posix()] = sites
    assert set(found) == {OWNER}, found
    assert len(found[OWNER]) == 2, "the owner reads each variable once"


def test_the_scan_sees_the_files_it_claims_to():
    files = {f.relative_to(BACKEND).as_posix() for f in shipped_files()}
    assert {"main.py", "app/routes/dashboard.py", "app/services/ha_status.py", OWNER} <= files
    assert len(files) > 100


@pytest.mark.parametrize("source", [
    'import os\nx = os.getenv("GATEWAY_URL", "d")\n',
    'def _base(name, default): ...\ny = _base("ORCHESTRATOR_URL", "d")\n',
    'z = os.environ["GATEWAY_URL"]\n',
])
def test_the_detector_finds_a_planted_read(source):
    assert literal_sites(source)


def test_defaults_when_unset_or_empty(monkeypatch):
    from app.services import service_urls

    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    assert service_urls.gateway_url() == "http://localhost:8000"
    assert service_urls.orchestrator_url() == "http://localhost:8001"
    monkeypatch.setenv("GATEWAY_URL", "")
    assert service_urls.gateway_url() == "http://localhost:8000"


def test_the_value_is_read_at_call_time_without_a_trailing_slash(monkeypatch):
    from app.services import service_urls

    monkeypatch.setenv("GATEWAY_URL", "http://gw.example.invalid:9000/")
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.example.invalid//")
    assert service_urls.gateway_url() == "http://gw.example.invalid:9000"
    assert service_urls.orchestrator_url() == "http://orch.example.invalid"
    monkeypatch.setenv("GATEWAY_URL", "http://other.example.invalid")
    assert service_urls.gateway_url() == "http://other.example.invalid"
