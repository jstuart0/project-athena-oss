"""The lint gate stays a gate.

ruff.toml and .github/workflows/lint.yml are the whole mechanism; this pins
their shape so a loosened rule, a widened exclude, a dropped tree, an unpinned
action or a new `noqa: F811` fails here instead of passing silently.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RUFF_TOML = ROOT / "ruff.toml"
LINT_WORKFLOW = ROOT / ".github" / "workflows" / "lint.yml"

LINT_TREES = {"src", "admin/backend/app", "apps"}
REQUIRED_RULES = {"F821", "F822", "F823", "F811", "E9"}
EXCLUDED_FILES = {"src/jetson/athena_lite_llm.py"}
F811_SITES = {
    ("admin/backend/app/routes/audit.py", "get_recent_audit_logs"),
    ("admin/backend/app/routes/cloud_llm_usage.py", "get_cost_alerts"),
}
FULL_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}(\s|$)")
RUFF_COMMAND = re.compile(r"\bruff check --no-cache ((?:[\w./-]+\s*)+)")


def _config() -> dict:
    return tomllib.loads(RUFF_TOML.read_text())


def _workflow() -> dict:
    return yaml.safe_load(LINT_WORKFLOW.read_text())


def _steps(workflow: dict) -> list:
    return [s for job in workflow["jobs"].values() for s in job["steps"]]


def test_ruff_selects_at_least_the_correctness_rules():
    assert REQUIRED_RULES <= set(_config()["lint"]["select"])


def test_ruff_excludes_exactly_the_unparseable_jetson_file():
    assert set(_config()["extend-exclude"]) == EXCLUDED_FILES
    assert "exclude" not in _config(), "replacing `exclude` would drop ruff's defaults"
    assert "per-file-ignores" not in _config().get("lint", {})


def test_the_excluded_file_is_still_broken_so_the_exclude_is_still_needed():
    import ast

    source = (ROOT / "src/jetson/athena_lite_llm.py").read_text()
    with pytest.raises(SyntaxError):
        ast.parse(source)


def test_workflow_runs_ruff_over_exactly_the_three_trees():
    runs = [s["run"] for s in _steps(_workflow()) if "run" in s]
    targets = [
        set(m.group(1).split()) for r in runs for m in [RUFF_COMMAND.search(r)] if m
    ]
    assert targets == [LINT_TREES]


def test_workflow_runs_this_test_file():
    runs = "\n".join(s["run"] for s in _steps(_workflow()) if "run" in s)
    assert "tests/unit/test_lint_gate.py" in runs


def test_every_lint_tree_exists():
    for tree in LINT_TREES:
        assert (ROOT / tree).is_dir(), tree


def test_workflow_actions_are_pinned_by_full_sha():
    uses = [s["uses"] for s in _steps(_workflow()) if "uses" in s]
    assert uses, "the planted-negative below needs a non-empty population"
    assert all(FULL_SHA.match(u) for u in uses), uses
    assert not FULL_SHA.match("actions/checkout@v4")


def test_workflow_permissions_are_read_only_contents():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert "permissions" not in job or job["permissions"] == {"contents": "read"}


def test_workflow_has_no_path_filter_and_no_pull_request_target():
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = _workflow().get("on", _workflow().get(True))
    assert set(triggers) == {"pull_request", "push"}
    for name, body in triggers.items():
        assert "paths" not in (body or {}), name
        assert "paths-ignore" not in (body or {}), name


def test_checkout_does_not_persist_credentials():
    checkouts = [
        s for s in _steps(_workflow()) if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_workflow_uses_python_311():
    setups = [
        s for s in _steps(_workflow()) if s.get("uses", "").startswith("actions/setup-python@")
    ]
    assert [str(s["with"]["python-version"]) for s in setups] == ["3.11"]


def _f811_sites() -> set:
    import ast

    found = set()
    for tree in LINT_TREES:
        for path in (ROOT / tree).rglob("*.py"):
            text = path.read_text(errors="replace")
            if "noqa: F811" not in text:
                continue
            rel = path.relative_to(ROOT).as_posix()
            def_lines = {
                n.lineno: n.name
                for n in ast.walk(ast.parse(text))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for lineno, line in enumerate(text.splitlines(), 1):
                if "noqa: F811" in line:
                    found.add((rel, def_lines.get(lineno, f"line:{lineno}")))
    return found


def test_noqa_f811_sites_are_exactly_the_two_duplicate_routes():
    assert _f811_sites() == F811_SITES


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is installed only in the lint job")
def test_ruff_fails_on_a_planted_undefined_name(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("def f():\n    return undefined_x\n")
    result = subprocess.run(
        ["ruff", "check", "--no-cache", "--isolated", "--select", "F821", str(planted)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "F821" in result.stdout


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is installed only in the lint job")
def test_repo_config_flags_a_planted_undefined_name_in_a_lint_tree():
    planted = ROOT / "src" / "_planted_lint_probe.py"
    planted.write_text("def f():\n    return undefined_x\n")
    try:
        result = subprocess.run(
            ["ruff", "check", "--no-cache", "src"], capture_output=True, text=True, cwd=ROOT
        )
    finally:
        planted.unlink()
    assert result.returncode == 1
    assert "F821" in result.stdout
