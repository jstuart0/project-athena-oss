"""The statuses that are "not checked" have one owner: ``service_state.NOT_CHECKED_STATUSES``.

The dashboard imports it; no other module in the app declares a set of status
words of its own.
"""
import ast
import pathlib

from app.routes import dashboard
from app.utils import service_state

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
STATUS_WORDS = {"disabled", "pending", "stale", "unconfigured"}


def test_the_set_is_the_four_not_checked_words():
    assert service_state.NOT_CHECKED_STATUSES == frozenset(STATUS_WORDS)


def test_the_dashboard_uses_the_one_set():
    assert dashboard.NOT_CHECKED_STATUSES is service_state.NOT_CHECKED_STATUSES


def _status_word_sets(path):
    """Every set or frozenset literal in ``path`` that holds two or more of the
    not-checked words."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        elements = []
        if isinstance(node, ast.Set):
            elements = node.elts
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("frozenset", "set") and node.args:
            argument = node.args[0]
            if isinstance(argument, (ast.List, ast.Tuple)):  # a Set argument is found as a Set
                elements = argument.elts
        words = {e.value for e in elements if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        if len(words & STATUS_WORDS) >= 2:
            found.append((path.name, node.lineno, sorted(words)))
    return found


def test_dashboard_declares_no_status_word_set_of_its_own():
    assert _status_word_sets(APP / "routes" / "dashboard.py") == []


def test_the_only_declaration_in_the_app_is_service_states():
    declared = []
    for path in sorted(APP.rglob("*.py")):
        declared += _status_word_sets(path)
    assert [name for name, _, _ in declared] == ["service_state.py"], declared


def test_the_detector_finds_a_planted_copy(tmp_path):
    planted = tmp_path / "copy.py"
    planted.write_text('NOT_CHECKED = frozenset({"disabled", "pending", "stale", "unconfigured"})\nOTHER = {"stale", "pending"}\n')
    assert len(_status_word_sets(planted)) == 2
