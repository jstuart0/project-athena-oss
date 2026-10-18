"""Source guards for the room-policy boundary: who may call what, and that the
motion handler's direct helper writes sit only on the branch for rooms the
policy engine doesn't run."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

SRC = Path(__file__).resolve().parents[2] / "src"


def parsed() -> Iterator[Tuple[Path, ast.AST]]:
    for path in sorted(SRC.rglob("*.py")):
        try:
            yield path, ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue


def call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def calls_of(name: str) -> List[str]:
    return [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path, tree in parsed() for node in ast.walk(tree)
        if isinstance(node, ast.Call) and call_name(node) == name
    ]


def test_run_policy_script_is_called_only_by_the_router():
    assert {site.split(":")[0] for site in calls_of("run_policy_script")} == {"orchestrator/room_policy.py"}


def test_the_grant_is_made_only_by_the_router():
    assert [site.split(":")[0] for site in calls_of("PolicyGrant")] == ["orchestrator/room_policy.py"]


def test_the_router_has_no_direct_service_call():
    """Everything above the self-test section (which drives the guard on
    purpose, against a recording fake)."""
    tree = ast.parse((SRC / "orchestrator" / "room_policy.py").read_text(encoding="utf-8"))
    boundary = next(
        n.lineno for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SELFTEST_LINE" for t in n.targets)
    )
    router = [n for n in tree.body if n.lineno < boundary]
    assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == "apply" for n in router)
    names = {call_name(c) for n in router for c in ast.walk(n) if isinstance(c, ast.Call)}
    assert "call_service" not in names and "create_automation" not in names


def test_the_response_call_is_made_only_by_the_guards_policy_method():
    sites = calls_of("call_service_response")
    assert [s.split(":")[0] for s in sites] == ["orchestrator/mode_permission.py"], sites


def test_the_motion_handlers_helper_writes_are_on_the_non_policy_branch():
    controller = ast.parse((SRC / "orchestrator" / "smart_home_controller.py").read_text(encoding="utf-8"))
    handler = next(
        n for n in ast.walk(controller)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_motion_control_intent"
    )
    branch = next(
        n for n in handler.body
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "policy_slug"
    )
    assert isinstance(branch.body[-1], ast.Return), "the policy branch must end the handler's work"
    writes = [
        n for n in ast.walk(handler)
        if isinstance(n, ast.Call) and call_name(n) == "call_service" and n.args
        and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).startswith("input_")
    ]
    assert len(writes) >= 10, "the legacy writes are still there for plain rooms"
    assert all(w.lineno > branch.end_lineno for w in writes), [w.lineno for w in writes]


def test_the_motion_handler_never_writes_through_the_policy_helpers_module():
    """The policy branch hands over to room_policy.apply and does nothing else."""
    controller = ast.parse((SRC / "orchestrator" / "smart_home_controller.py").read_text(encoding="utf-8"))
    method = next(
        n for n in ast.walk(controller)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_motion_via_room_policy"
    )
    names = {call_name(n) for n in ast.walk(method) if isinstance(n, ast.Call)}
    assert "call_service" not in names and "apply" in names


def test_the_guard_gets_its_policy_rooms_right_after_it_is_built():
    main = (SRC / "orchestrator" / "main.py").read_text(encoding="utf-8")
    assert main.count("set_policy_room_source(") == 1
    built = main.index("ha_client = ensure_permission_enforcing(ha_client_raw)")
    registered = main.index("set_policy_room_source(_policy_slugs_from_cache(entity_manager))")
    assert built < registered < main.index("_runtime.set_ha_client(ha_client)") + 200


def test_only_the_orchestrator_builds_or_imports_the_ha_guard():
    """Every process that wraps an HA client in the guard registers the policy
    source in orchestrator.main; none of the other services (gateway, jarvis-web,
    admin, mode service, jetson) imports or constructs it."""
    apps = SRC.parents[0] / "apps"
    admin = SRC.parents[0] / "admin" / "backend" / "app"
    outside = []
    for root in (SRC, apps, admin):
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts or (root == SRC and path.relative_to(SRC).parts[0] == "orchestrator"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] + [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else [])
                if any("mode_permission" in m for m in modules):
                    outside.append(f"{path}:{node.lineno}")
                if isinstance(node, ast.Call) and call_name(node) in ("PermissionEnforcingHAClient", "ensure_permission_enforcing"):
                    outside.append(f"{path}:{node.lineno}")
    assert outside == []
    assert len(calls_of("ensure_permission_enforcing")) >= 5, "the orchestrator wraps its holders"
