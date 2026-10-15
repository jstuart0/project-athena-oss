"""Every test this campaign adds really runs in CI.

A test file that no workflow runs is a green check that checks nothing. This
parses every workflow with YAML and, for each test file the campaign added or
touched (derived from globs over the working tree, plus the campaign diff on a
local checkout), requires:

(a) a pytest path token in a ``run:`` step, spelled as the path relative to that
    step's ``working-directory`` (a comment or a ``paths:`` entry doesn't count);
(b) for a workflow with ``paths:`` filters, the file in both the
    ``pull_request`` and the ``push`` lists;
(c) its own step when the file assigns ``os.environ[...]``, ``sys.modules[...]``
    or the service key at module scope (it would change every other test in the
    process);
(d) a step that isn't ``continue-on-error`` and has no ``if:`` that is false.

The check runs from the first phase, so an unwired file fails in the phase that
adds it. The rules are pure functions over parsed YAML, and the negative controls
below feed them workflow text that breaks each rule.
"""
from __future__ import annotations

import ast
import posixpath
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
BASE = "9e80241"

GLOBS = (
    "tests/unit/test_ha_*.py",
    "tests/unit/test_gateway_ha_*.py",
    "tests/unit/test_url_safety_*.py",
    "tests/unit/test_room_directory_refresh.py",
    "tests/unit/test_motion_control_rooms.py",
    "tests/unit/test_media_rag_seerr.py",
    "tests/unit/test_admin_frontend_*.py",
    "tests/unit/test_campaign_ci_wiring.py",
    "tests/unit/test_stuck_sensor_detection.py",
    "tests/unit/test_exclusion_vocabulary.py",
    "tests/unit/test_smart_home_intent_prompt.py",
    "tests/unit/test_access_target.py",
    "tests/unit/test_access_intent_policy.py",
    "tests/unit/test_sequence_step_params.py",
    "tests/unit/test_sequence_step_clauses.py",
    "tests/unit/test_write_cue.py",
    "tests/unit/test_mode_permission.py",
    "tests/unit/test_public_refusal_wording.py",
    "tests/unit/test_llm_context_plan.py",
    "tests/unit/test_llm_router_context_wiring.py",
    "tests/unit/test_llm_strict_callers.py",
    "tests/unit/test_llm_direct_ollama_drift.py",
    "tests/unit/test_model_json_strip.py",
    "tests/unit/test_access_target_drift.py",
    "tests/unit/test_lint_gate.py",
    "tests/unit/test_validate.py",
    "tests/unit/test_retrieve.py",
    "tests/unit/test_rag_url_reachability.py",
    "tests/unit/test_sports_status.py",
    "tests/unit/test_favorite_teams_*.py",
    "tests/unit/test_admin_config_favorite_teams.py",
    "admin/backend/tests/test_favorite_teams_settings.py",
    "tests/unit/test_household_*.py",
    "tests/unit/test_smart_home_bed_warmer.py",
    "tests/unit/test_oss_readiness_orchestrator.py",
    "tests/unit/test_jarvis_web_household_*.py",
    "admin/backend/tests/test_household_*.py",
    "admin/backend/tests/integration/test_household_*.py",
    "tests/unit/test_context_detector_dining.py",
    "tests/unit/test_semantic_cache_audience.py",
    "tests/unit/test_bench_observability.py",
    "tests/unit/test_gateway_nonstream_continuity.py",
    "tests/unit/test_identity_answer.py",
    "tests/unit/test_identity_routes.py",
    "tests/unit/test_synthesize.py",
    "tests/unit/test_gateway_spoken_failure.py",
    "tests/unit/test_model_literal_drift.py",
    "tests/unit/test_clause_rule.py",
    "tests/unit/test_command_text.py",
    "tests/unit/test_extract_command_text.py",
    "tests/unit/test_imperative_never_read.py",
    "tests/unit/test_media_scene_cue.py",
    "tests/unit/test_grounding.py",
    "tests/unit/test_tool_call_grounding.py",
    "tests/unit/test_answer_route.py",
    "tests/unit/test_followup.py",
    "tests/unit/test_followup_wiring.py",
    "tests/unit/test_agent_gate.py",
    "tests/unit/test_entity_repair.py",
    "tests/unit/test_intent_arbitration.py",
    "tests/unit/test_conversation_eval_replay.py",
    "tests/unit/test_latency_budget.py",
    "tests/integration/test_semantic_cache_real_redis.py",
    "tests/unit/test_access_lock.py",
    "tests/unit/test_entity_freshness.py",
    "tests/unit/test_occupancy_answer.py",
    "tests/unit/test_occupancy_whole_house_words.py",
    "tests/integration/test_ha_auth_*.py",
    "admin/backend/tests/test_ha_*.py",
    "admin/backend/tests/test_room_tv_audio_*.py",
    "admin/backend/tests/test_dashboard_not_checked.py",
    "admin/backend/tests/test_service_urls_accessor.py",
    "admin/backend/tests/test_core_probe.py",
    "admin/backend/tests/test_stuck_sensor_alerts.py",
    "admin/backend/tests/test_oss_profiles_*.py",
)

# The test files this campaign adds, pinned so a CI checkout at depth 1 (no
# campaign base to diff against) still checks them. Each phase extends it.
CAMPAIGN_TESTS = (
    "tests/unit/test_url_safety_service_host.py",
    "tests/unit/test_ha_credentials.py",
    "tests/unit/test_ha_auth_latch.py",
    "tests/unit/test_ha_http.py",
    "tests/unit/test_ha_ws.py",
    "tests/unit/test_ha_auth_bound_end_to_end.py",
    "tests/unit/test_ha_link_wire_compat.py",
    "tests/unit/test_ha_secret_hygiene.py",
    "tests/unit/test_campaign_ci_wiring.py",
    "tests/integration/test_ha_auth_real_redis.py",
    "tests/unit/test_ha_consolidation_drift.py",
    "tests/unit/test_room_directory_refresh.py",
    "tests/unit/test_gateway_ha_channel.py",
    "tests/unit/test_ha_client_over_the_channel.py",
    "tests/unit/test_ha_docs_match_constants.py",
    "tests/unit/test_ha_admin_config_tool_keys.py",
    "admin/backend/tests/test_ha_key_not_injectable.py",
    "tests/unit/test_admin_frontend_status_vocab.py",
    "tests/unit/test_admin_frontend_script_order.py",
    "tests/unit/test_admin_frontend_voice_config_ha.py",
    "tests/unit/test_admin_frontend_mission_control_ha.py",
    "tests/unit/test_admin_frontend_ha_reason_v1_fallback.py",
    "admin/backend/tests/test_ha_access.py",
    "admin/backend/tests/test_ha_key_write_boundary.py",
    "admin/backend/tests/test_ha_pipelines_routes.py",
    "admin/backend/tests/test_room_tv_audio_ha_channel.py",
    "admin/backend/tests/test_ha_status_v1_reader_compat.py",
    "admin/backend/tests/test_dashboard_not_checked.py",
    "admin/backend/tests/test_service_urls_accessor.py",
    "admin/backend/tests/test_core_probe.py",
    "tests/unit/test_stuck_sensor_detection.py",
    "tests/unit/test_exclusion_vocabulary.py",
    "tests/unit/test_access_target.py",
    "tests/unit/test_access_intent_policy.py",
    "tests/unit/test_sequence_step_params.py",
    "tests/unit/test_sequence_step_clauses.py",
    "tests/unit/test_write_cue.py",
    "tests/unit/test_mode_permission.py",
    "tests/unit/test_public_refusal_wording.py",
    "tests/unit/test_ha_refusal_wiring.py",
    "tests/unit/test_ha_door_read_timeout.py",
    "tests/unit/test_llm_context_plan.py",
    "tests/unit/test_llm_router_context_wiring.py",
    "tests/unit/test_llm_strict_callers.py",
    "tests/unit/test_llm_direct_ollama_drift.py",
    "tests/unit/test_model_json_strip.py",
    "tests/unit/test_access_target_drift.py",
    "admin/backend/tests/test_oss_profiles_context.py",
    "tests/unit/test_motion_control_rooms.py",
    "tests/unit/test_media_rag_seerr.py",
    "tests/unit/test_lint_gate.py",
    "tests/unit/test_validate.py",
    "tests/unit/test_retrieve.py",
    "tests/unit/test_rag_url_reachability.py",
    "tests/unit/test_sports_status.py",
    "tests/unit/test_context_detector_dining.py",
    "tests/unit/test_semantic_cache_audience.py",
    "tests/unit/test_bench_observability.py",
    "tests/unit/test_gateway_nonstream_continuity.py",
    "tests/unit/test_identity_answer.py",
    "tests/unit/test_identity_routes.py",
    "tests/unit/test_synthesize.py",
    "tests/unit/test_gateway_spoken_failure.py",
    "tests/unit/test_model_literal_drift.py",
    "tests/unit/test_clause_rule.py",
    "tests/unit/test_command_text.py",
    "tests/unit/test_extract_command_text.py",
    "tests/unit/test_imperative_never_read.py",
    "tests/unit/test_media_scene_cue.py",
    "tests/unit/test_grounding.py",
    "tests/unit/test_tool_call_grounding.py",
    "tests/unit/test_answer_route.py",
    "tests/unit/test_followup.py",
    "tests/unit/test_followup_wiring.py",
    "tests/unit/test_agent_gate.py",
    "tests/unit/test_entity_repair.py",
    "tests/unit/test_intent_arbitration.py",
    "tests/unit/test_conversation_eval_replay.py",
    "tests/unit/test_latency_budget.py",
    "tests/integration/test_semantic_cache_real_redis.py",
    "tests/unit/test_access_lock.py",
    "tests/unit/test_entity_freshness.py",
    "tests/unit/test_occupancy_answer.py",
    "tests/unit/test_occupancy_whole_house_words.py",
    "tests/unit/test_favorite_teams_model.py",
    "tests/unit/test_favorite_teams_choice.py",
    "tests/unit/test_admin_config_favorite_teams.py",
    "tests/unit/test_admin_frontend_favorite_teams.py",
    "admin/backend/tests/test_favorite_teams_settings.py",
    "tests/unit/test_household_settings_model.py",
    "tests/unit/test_household_reader.py",
    "tests/unit/test_household_orchestrator_readers.py",
    "tests/unit/test_household_orchestrator_lifespan.py",
    "tests/unit/test_smart_home_bed_warmer.py",
    "tests/unit/test_oss_readiness_orchestrator.py",
    "admin/backend/tests/test_household_settings_routes.py",
    "admin/backend/tests/test_household_settings_real_seam.py",
    "admin/backend/tests/integration/test_household_settings_postgres.py",
    "tests/unit/test_jarvis_web_household_settings.py",
    "tests/unit/test_jarvis_web_household_copy.py",
    "admin/backend/tests/test_household_sms_country_code.py",
)

# Files that match a glob and were not run by any workflow at the base commit.
# Compared exactly; it may only shrink. Each has the reason it isn't run.
PREEXISTING_UNWIRED: Dict[str, str] = {
    "tests/unit/test_admin_frontend_cache_buster_pins.py":
        "needs the merge base with origin/main; no workflow ran it at the base commit (the frontend phase edits it and decides)",
    "tests/unit/test_admin_frontend_calendar_sources.py": "a Node test no workflow ran at the base commit",
    "tests/unit/test_admin_frontend_default_tab.py": "a static test no workflow ran at the base commit",
    "tests/unit/test_admin_frontend_guest_mode_status.py": "a Node test no workflow ran at the base commit",
    "tests/unit/test_ha_permission_wiring_pass_d.py": "split out of test_ha_permission_wiring.py; no workflow ran it at the base commit",
}

# Files that stub an absent module at import (``if name not in sys.modules``) and
# already share an orchestrator-behaviour step at the base commit without harm.
PREEXISTING_SHARED_STATE: Dict[str, str] = {
    "tests/unit/test_extract_intent_room_mentions.py": "stubs langgraph and prometheus_client only when absent; shared the orchestrator-behaviour step at the base",
    "tests/unit/test_ha_permission_guard.py": "stubs langgraph and prometheus_client only when absent",
    "tests/unit/test_ha_permission_wiring.py": "stubs langgraph and prometheus_client only when absent",
    "tests/unit/test_gateway_speaker_room.py": "stubs prometheus_client only when absent",
    "tests/unit/test_gateway_output_channel.py": "setdefault for SERVICE_API_KEY and ADMIN_API_URL, and a prometheus_client stub only when absent; shared the orchestrator-behaviour step at the base",
    "tests/unit/test_gateway_simple_commands_routing.py": "setdefault for SERVICE_API_KEY and ADMIN_API_URL, and a prometheus_client stub only when absent; shared a step at the base",
    "tests/unit/test_room_light_area_tier.py": "stubs langgraph and prometheus_client only when absent",
    "tests/unit/test_room_light_dispatch.py": "stubs langgraph and prometheus_client only when absent",
    "tests/unit/test_smart_home_intent_prompt.py": "stubs langgraph and prometheus_client only when absent; unwired at the base",
    "tests/unit/test_smart_home_controller_permissions.py": "stubs langgraph and prometheus_client only when absent; shared the orchestrator-behaviour step at the base",
    "tests/unit/test_write_fanout_confirmation.py": "stubs langgraph and prometheus_client only when absent; shared the orchestrator-behaviour step at the base",
    "tests/unit/test_reviewed_route_callers_wire.py": "setdefault for ADMIN_API_URL only; shared the orchestrator-behaviour step at the base",
    "tests/unit/test_state_question_routing.py": "stubs langgraph and prometheus_client only when absent; shared the orchestrator-behaviour step at the base",
    "tests/unit/test_mode_permission.py": "stubs langgraph and prometheus_client only when absent, the same as test_ha_permission_guard.py in this step; unwired at the base, wired by the guard phase",
}

NAMED_MEMBERS = (
    "tests/unit/test_ha_auth_bound_end_to_end.py",
    "tests/integration/test_ha_auth_real_redis.py",
)

# Source files a path-filtered workflow's tests read: a change to one must
# trigger the workflow. Each must be in both lists once the file exists.
SOURCES_READ_BY_TESTS = {
    "frontend-escaping.yml": (
        "admin/backend/app/services/ha_status.py",
        "admin/backend/app/services/health_poller.py",
        "admin/backend/app/utils/service_state.py",
        "src/shared/ha_link.py",
        "tests/unit/frontend_node.py",
        "tests/fixtures/admin_frontend/app_ha_block_9e80241.js",
    ),
    "telemetry.yml": (
        "admin/backend/app/services/ha_access.py",
        "src/shared/ha_credentials.py",
        "src/shared/url_safety.py",
    ),
    "ha-auth-redis.yml": (
        "src/shared/ha_auth.py",
        "src/shared/ha_http.py",
        "src/shared/ha_ws.py",
        "src/shared/ha_registry.py",
    ),
}

ENV_SETTERS = ("SERVICE_API_KEY", "DEV_MODE")


# -- parsing ------------------------------------------------------------------------------------


Step = Tuple[str, Dict]  # (working directory relative to the repo root, the step mapping)


def load(text: str) -> Dict:
    document = yaml.safe_load(text)
    if True in document and "on" not in document:  # YAML 1.1 reads the key ``on`` as a boolean
        document["on"] = document.pop(True)
    return document


def run_steps(workflow: Dict) -> List[Step]:
    found: List[Step] = []
    for job in (workflow.get("jobs") or {}).values():
        default = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", "")
        for step in job.get("steps") or []:
            if isinstance(step.get("run"), str):
                found.append((step.get("working-directory", default) or "", step))
    return found


def _tokens(script: str) -> List[str]:
    out: List[str] = []
    for line in script.splitlines():
        try:
            out.extend(shlex.split(line, comments=True))
        except ValueError:
            out.extend(t for t in line.split() if not t.startswith("#"))
    return out


def pytest_paths_in(script: str) -> Set[str]:
    """Pytest path tokens in a script (``::node`` stripped), as written."""
    found = set()
    for token in _tokens(script):
        path = token.split("::", 1)[0]
        if path.endswith(".py") and not re.search(r"\s", path):
            found.add(path)
    return found


def pytest_dirs_in(script: str) -> Set[str]:
    """Directory arguments of a pytest invocation (``pytest ... tests/integration``), as written."""
    found = set()
    for line in script.splitlines():
        tokens = _tokens(line)
        if "pytest" not in tokens:
            continue
        for token in tokens[tokens.index("pytest") + 1:]:
            if "/" in token and not token.startswith("-") and not token.endswith(".py") and "::" not in token:
                found.add(token.rstrip("/"))
    return found


def _repo_path(working_directory: str, token: str) -> str:
    return posixpath.normpath(posixpath.join(working_directory or ".", token))


def runs_of(workflow: Dict, path: str) -> List[Step]:
    """The steps that name ``path``, or a directory above it, as a pytest path relative to their working directory."""
    return [(wd, step) for wd, step in run_steps(workflow)
            if any(_repo_path(wd, token) == path for token in pytest_paths_in(step["run"]))
            or any(path.startswith(_repo_path(wd, token) + "/") for token in pytest_dirs_in(step["run"]))]


def _glob_match(pattern: str, path: str) -> bool:
    regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


def path_filters(workflow: Dict) -> Optional[Tuple[List[str], List[str]]]:
    on = workflow.get("on") or {}
    if not isinstance(on, dict):
        return None
    pull, push = (on.get("pull_request") or {}), (on.get("push") or {})
    if "paths" not in pull and "paths" not in push:
        return None
    return list(pull.get("paths") or []), list(push.get("paths") or [])


def sets_shared_state_at_module_scope(source: str) -> bool:
    """True when the file's module level (not a function body) assigns
    ``os.environ[...]`` or ``sys.modules[...]``, or calls ``os.environ.setdefault``,
    ``os.environ.update`` or ``os.putenv``."""
    tree = ast.parse(source)

    def subscript_of(node: ast.AST, owner: str, attr: str) -> bool:
        return (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == attr and isinstance(node.value.value, ast.Name) and node.value.value.id == owner)

    def visit(node: ast.AST) -> bool:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        if isinstance(node, ast.Assign) and any(subscript_of(t, "os", "environ") or subscript_of(t, "sys", "modules") for t in node.targets):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if node.func.attr in ("setdefault", "update") and isinstance(owner, ast.Attribute) and owner.attr == "environ":
                return True
            if node.func.attr == "putenv":
                return True
        return any(visit(child) for child in ast.iter_child_nodes(node))

    return any(visit(statement) for statement in tree.body)


def violations(workflows: Dict[str, Dict], files: Dict[str, str], shared_state_exempt: Iterable[str] = ()) -> List[str]:
    """Every way ``files`` (repo path -> source) fail rules (a)-(d) in ``workflows``."""
    problems: List[str] = []
    for path, source in sorted(files.items()):
        runners = [(name, wd, step, wf) for name, wf in workflows.items() for wd, step in runs_of(wf, path)]
        if not runners:
            problems.append(f"{path}: no workflow runs it (a)")
            continue
        isolated = path not in set(shared_state_exempt) and sets_shared_state_at_module_scope(source)
        for name, wd, step, wf in runners:
            filters = path_filters(wf)
            if filters is not None:
                for label, patterns in zip(("pull_request", "push"), filters):
                    if not any(_glob_match(p, path) for p in patterns):
                        problems.append(f"{path}: {name} runs it but {label}.paths doesn't list it (b)")
            others = {_repo_path(wd, t) for t in pytest_paths_in(step["run"])} - {path}
            if isolated and others:
                problems.append(f"{path}: sets process-wide state at import but shares a step with {sorted(others)} (c)")
            if step.get("continue-on-error") in (True, "true"):
                problems.append(f"{path}: its step in {name} is continue-on-error (d)")
            if str(step.get("if", "")).strip().lower() in ("false", "${{ false }}"):
                problems.append(f"{path}: its step in {name} never runs (d)")
    return problems


def source_problems(workflows: Dict[str, Dict], exists) -> List[str]:
    problems: List[str] = []
    for name, sources in SOURCES_READ_BY_TESTS.items():
        filters = path_filters(workflows[name]) if name in workflows else None
        if filters is None:
            continue
        for source in sources:
            if exists(source):
                for label, patterns in zip(("pull_request", "push"), filters):
                    if not any(_glob_match(p, source) for p in patterns):
                        problems.append(f"{name}: {label}.paths misses {source}")
    return problems


# -- the tree ---------------------------------------------------------------------------------------------


def all_workflows() -> Dict[str, Dict]:
    return {p.name: load(p.read_text(encoding="utf-8")) for p in sorted(WORKFLOWS.glob("*.yml"))}


def globbed_files() -> Set[str]:
    return {p.relative_to(ROOT).as_posix() for pattern in GLOBS for p in ROOT.glob(pattern)}


def campaign_diff() -> Optional[Set[str]]:
    """Test files changed since the campaign's base, or None when the base isn't
    in this checkout (CI fetches depth 1)."""
    if subprocess.run(["git", "cat-file", "-e", BASE], cwd=ROOT, capture_output=True).returncode != 0:
        return None
    out = subprocess.run(
        ["git", "diff", "--name-only", BASE, "--", "tests/**/test_*.py", "admin/backend/tests/**/test_*.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return {line for line in out.splitlines() if (ROOT / line).exists()}


def campaign_added_tests() -> Optional[Set[str]]:
    """Test files the campaign added since its base; None when the base isn't in this checkout."""
    if subprocess.run(["git", "cat-file", "-e", BASE], cwd=ROOT, capture_output=True).returncode != 0:
        return None
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", BASE, "--", "tests/**/test_*.py", "admin/backend/tests/**/test_*.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return {line for line in out.splitlines() if (ROOT / line).exists()}


def source_of(paths: Iterable[str]) -> Dict[str, str]:
    return {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}


def test_the_campaign_tests_are_inside_the_globs_so_a_shallow_checkout_still_finds_them():
    existing = {p for p in CAMPAIGN_TESTS if (ROOT / p).exists()}
    assert existing <= globbed_files(), sorted(existing - globbed_files())
    assert len(existing) >= 8, "the floor: Phase 1 adds at least eight wired files"
    assert set(NAMED_MEMBERS) <= existing


def test_campaign_tests_lists_every_test_file_the_campaign_added():
    added = campaign_added_tests()
    if added is not None:
        assert added <= set(CAMPAIGN_TESTS), sorted(added - set(CAMPAIGN_TESTS))
        assert len(added) >= 20, "the floor: the campaign adds at least twenty test files"
    assert all((ROOT / p).exists() for p in CAMPAIGN_TESTS if p.startswith("admin/backend/tests/"))


def test_every_test_the_campaign_adds_or_touches_is_run_by_a_workflow():
    workflows = all_workflows()
    candidates = globbed_files()
    diff = campaign_diff()
    if diff is not None:
        candidates |= diff
    unwired_at_base = set(PREEXISTING_UNWIRED)
    expected = candidates - unwired_at_base
    problems = violations(workflows, source_of(sorted(expected)), PREEXISTING_SHARED_STATE)
    assert problems == [], "\n".join(problems)
    still_unwired = {p for p in candidates & unwired_at_base if not any(runs_of(wf, p) for wf in workflows.values())}
    assert still_unwired == unwired_at_base, "a pre-existing unwired file got wired: remove it from PREEXISTING_UNWIRED"
    floor = {p for p in CAMPAIGN_TESTS if (ROOT / p).exists()}
    assert floor <= candidates


def test_the_real_redis_tier_runs_under_the_integration_marker_in_its_own_workflow():
    workflow = all_workflows()["ha-auth-redis.yml"]
    path = "tests/integration/test_ha_auth_real_redis.py"
    steps = runs_of(workflow, path)
    assert len(steps) == 1
    script = steps[0][1]["run"]
    assert "-m integration" in script and "HA_AUTH_TEST_REDIS_URL" in str(steps[0][1].get("env", "")) + script
    assert "services" in str(workflow["jobs"]["real-redis"]) and "redis" in workflow["jobs"]["real-redis"]["services"]


def test_the_sources_the_filtered_workflows_read_trigger_them():
    problems = source_problems(all_workflows(), lambda p: (ROOT / p).exists())
    assert problems == [], "\n".join(problems)


# -- negative controls: each rule must fail on text that breaks it ---------------------------------------


GOOD = """
on:
  pull_request:
    paths: ["tests/unit/test_x.py"]
  push:
    paths: ["tests/unit/test_x.py"]
jobs:
  j:
    steps:
      - name: run it
        run: |
          python3 -m pytest -q tests/unit/test_x.py
"""
PLAIN = "def test_a():\n    pass\n"
ENV_SETTING = "import os\nos.environ.setdefault('SERVICE_API_KEY', 'k')\n\ndef test_a():\n    pass\n"


def check(text: str, source: str = PLAIN, path: str = "tests/unit/test_x.py") -> List[str]:
    return violations({"w.yml": load(text)}, {path: source})


def test_the_rules_accept_a_wired_file():
    assert check(GOOD) == []


def test_a_mention_in_a_comment_is_not_wiring():
    commented = GOOD.replace("python3 -m pytest -q tests/unit/test_x.py", "# python3 -m pytest -q tests/unit/test_x.py\n          echo done")
    assert any("(a)" in p for p in check(commented))


def test_a_paths_entry_alone_is_not_wiring():
    only_paths = GOOD.replace("python3 -m pytest -q tests/unit/test_x.py", "echo nothing")
    assert any("(a)" in p for p in check(only_paths))


def test_a_file_missing_from_one_paths_list_fails():
    push_only = GOOD.replace('pull_request:\n    paths: ["tests/unit/test_x.py"]', 'pull_request:\n    paths: ["src/other.py"]')
    problems = check(push_only)
    assert any("pull_request.paths" in p and "(b)" in p for p in problems) and len(problems) == 1


def test_a_glob_in_a_paths_list_counts():
    globbed = GOOD.replace('paths: ["tests/unit/test_x.py"]', 'paths: ["tests/unit/**"]')
    assert check(globbed) == []


def test_an_env_setting_file_cannot_share_a_run():
    shared = GOOD.replace("python3 -m pytest -q tests/unit/test_x.py", "python3 -m pytest -q tests/unit/test_x.py tests/unit/test_y.py")
    assert check(shared, ENV_SETTING) and "(c)" in check(shared, ENV_SETTING)[0]
    assert check(shared, PLAIN) == []


def test_module_scope_state_is_detected_in_each_form_and_not_inside_functions():
    for line in ("os.environ['X'] = '1'", "os.environ.setdefault('DEV_MODE', 'true')", "os.environ.update({'A': 'b'})",
                 "sys.modules['m'] = object()", "os.putenv('A', 'b')"):
        assert sets_shared_state_at_module_scope(f"import os, sys\n{line}\n"), line
    assert not sets_shared_state_at_module_scope("import os\ndef f():\n    os.environ['X'] = '1'\n")
    assert not sets_shared_state_at_module_scope("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('X', '1')\n")


def test_a_continue_on_error_step_does_not_count():
    soft = GOOD.replace("- name: run it", "- name: run it\n        continue-on-error: true")
    assert any("(d)" in p for p in check(soft))


def test_a_step_that_is_literally_false_does_not_count():
    for condition in ("false", "${{ false }}"):
        gated = GOOD.replace("- name: run it", f"- name: run it\n        if: {condition}")
        assert any("(d)" in p for p in check(gated)), condition


def test_a_basename_under_the_wrong_working_directory_does_not_count():
    wrong = GOOD.replace("      - name: run it", "      - name: run it\n        working-directory: admin/backend")
    assert any("(a)" in p for p in check(wrong))
    right = GOOD.replace("      - name: run it", "      - name: run it\n        working-directory: admin/backend").replace(
        "tests/unit/test_x.py\n", "../../tests/unit/test_x.py\n", 1).replace("-q tests", "-q ../../tests")
    assert check(right) == [], check(right)


def test_a_job_default_working_directory_is_honoured():
    defaulted = GOOD.replace("jobs:\n  j:\n", "jobs:\n  j:\n    defaults:\n      run:\n        working-directory: admin/backend\n").replace(
        "-q tests/unit/test_x.py", "-q ../../tests/unit/test_x.py")
    assert check(defaulted) == []


def test_a_node_id_still_counts_as_the_file():
    node = GOOD.replace("tests/unit/test_x.py\n", "tests/unit/test_x.py::test_a\n", 1).replace("-q tests/unit/test_x.py\n", "-q tests/unit/test_x.py::test_a\n")
    assert check(node) == []


def test_a_source_a_filtered_workflow_reads_must_be_in_both_lists():
    workflows = {"telemetry.yml": load(GOOD)}
    assert source_problems(workflows, lambda p: p == "src/shared/ha_credentials.py") != []


def test_a_pytest_directory_argument_covers_the_files_beneath_it_and_only_those():
    step = "      - name: run it\n        working-directory: admin/backend\n        run: python -m pytest -m integration tests/integration -q\n"
    workflow = load("on: push\njobs:\n  j:\n    steps:\n" + step)
    for covered in ("admin/backend/tests/integration/test_conversation_capture_postgres.py",
                    "admin/backend/tests/integration/test_capture_sink_postgres.py"):
        assert runs_of(workflow, covered)
    assert not runs_of(workflow, "admin/backend/tests/unit_like/test_x.py")
    assert not runs_of(workflow, "admin/backend/tests/integration_other/test_x.py")
    assert not runs_of(workflow, "tests/integration/test_x.py")  # wrong working directory
    real = all_workflows()["admin-memory.yml"]
    assert runs_of(real, "admin/backend/tests/integration/test_conversation_capture_postgres.py")
