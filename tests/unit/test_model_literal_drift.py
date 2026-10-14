"""No code path names a model tag the shipped deployment doesn't install.

`llama3.1:8b` is not pulled by any shipped job, so a literal of it in a code
path sends a request to a model Ollama doesn't have (ATHENA-258: the gateway's
`gpt-4` mapping did exactly that). Models come from `AthenaConfig`
(`athena_default_model`, `gateway_fallback_model`) instead.

This scans the gateway and the orchestrator. Docstrings and comments are not
code: a docstring is skipped by position, a comment never reaches the AST.
`phi3:mini` is not forbidden because
`manifests/athena-prod/ollama-model-pull-job.yaml` pulls it.

`ATHENA_DEFAULT_MODEL` has one reader, `AthenaConfig.athena_default_model`; the
orchestrator and admin-backend read the field, not the variable.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
SCANNED_TREES = ("src/gateway", "src/orchestrator")
ENV_READ_TREES = ("src/orchestrator", "admin/backend/app")
DEFAULT_MODEL_ENV = "ATHENA_DEFAULT_MODEL"
FORBIDDEN_LITERALS = frozenset({"llama3.1:8b"})
# (path, literal) -> why this literal is allowed. Empty: neither tree has one.
ALLOWLIST: dict = {}
MIN_FILES_SCANNED = 10


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    skipped: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                skipped.add(id(body[0].value))
    return skipped


def forbidden_literals(source: str) -> List[Tuple[int, str]]:
    """(line, literal) for each forbidden string constant outside a docstring."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for literal in FORBIDDEN_LITERALS:
                if literal in node.value:
                    found.append((node.lineno, literal))
    return sorted(found)


def scanned_files() -> List[Path]:
    return sorted(p for tree in SCANNED_TREES for p in (ROOT / tree).rglob("*.py"))


def violations(files: Iterable[Path]) -> List[str]:
    problems = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        for line, literal in forbidden_literals(path.read_text(encoding="utf-8")):
            if (rel, literal) not in ALLOWLIST:
                problems.append(f"{rel}:{line} names {literal!r}; read the model from AthenaConfig")
    return problems


def test_the_gateway_names_no_uninstalled_model_tag():
    assert violations(scanned_files()) == []


def test_the_scan_covers_the_gateway_and_its_named_members():
    files = {p.name for p in scanned_files()}
    assert len(files) >= MIN_FILES_SCANNED
    assert {"main.py", "intent_prerouter.py", "circuit_breaker.py"} <= files


def test_the_scan_covers_the_orchestrator_and_its_named_members():
    files = {p.relative_to(ROOT).as_posix() for p in scanned_files()}
    assert {
        "src/orchestrator/main.py",
        "src/orchestrator/automation_agent.py",
        "src/orchestrator/nodes/route_control.py",
        "src/orchestrator/self_building_tools.py",
        "src/orchestrator/smart_home_controller.py",
    } <= files


def _env_reads(source: str) -> List[int]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == DEFAULT_MODEL_ENV and id(node) not in docstrings
    )


def test_nothing_reads_the_default_model_variable_directly():
    problems = []
    for tree in ENV_READ_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            for line in _env_reads(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(ROOT).as_posix()}:{line} reads {DEFAULT_MODEL_ENV}; use get_config().athena_default_model")
    assert problems == []


def test_a_planted_env_read_is_found_and_the_readers_are_in_scope():
    assert _env_reads('import os\nx = os.getenv("ATHENA_DEFAULT_MODEL", "m")\n') == [2]
    scanned = {p.relative_to(ROOT).as_posix() for t in ENV_READ_TREES for p in (ROOT / t).rglob("*.py")}
    assert {
        "src/orchestrator/main.py", "src/orchestrator/state.py", "src/orchestrator/helpers.py",
        "src/orchestrator/utils/constants.py", "admin/backend/app/database.py",
        "admin/backend/app/services/oss_profiles.py",
    } <= scanned


def test_a_planted_literal_is_found():
    planted = 'MODEL = "llama3.1:8b"\nOTHER = {"gpt-4": "llama3.1:8b"}\n'
    assert [literal for _, literal in forbidden_literals(planted)] == ["llama3.1:8b", "llama3.1:8b"]


def test_a_docstring_or_comment_is_not_code():
    source = (
        '"""Module doc: llama3.1:8b."""\n'
        "# llama3.1:8b in a comment\n"
        "def f():\n"
        '    """Say llama3.1:8b."""\n'
        "    return 1\n"
    )
    assert forbidden_literals(source) == []


def test_every_allowlist_entry_names_a_file_that_still_holds_the_literal():
    for (path, literal), reason in ALLOWLIST.items():
        assert reason
        assert any(lit == literal for _, lit in forbidden_literals((ROOT / path).read_text(encoding="utf-8")))
