"""Every service caller of an admin-backend route the guest-data hardening
gated sends ``X-Service-Key`` on that call (stdlib only).

The scan is function-scoped: for each named caller, every HTTP call whose
URL (the literal, or a same-function variable it was assigned from) names a
gated path must carry a ``headers=`` whose source mentions
``X-Service-Key``, or go through a client constructed with it in the same
function. A helper called in the ``headers=`` expression counts when it is
defined in the same module (its source is searched too).

The six scoped voice-automation methods must also send
``X-Athena-Caller-Mode``: admin-backend returns 400 without it.

``AdminConfigClient``'s own default header is ``X-API-Key``, which
admin-backend reads as a *user* API key and rejects for the service key, so
a call that relies on the client defaults does not count.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GATED_PATH = re.compile(
    r"/api/(?:room-groups|user-sessions/device|voice-automations"
    r"|internal/emerging-intents|internal/intent-metrics"
    r"|settings/house-layout|settings/directions-origin-placeholders)"
)

TARGETS = {
    "src/shared/admin_config.py": {
        "resolve_room_group", "get_room_groups", "get_user_session_by_device",
        "create_voice_automation", "get_voice_automations", "archive_voice_automation",
        "restore_voice_automation", "delete_voice_automation",
        "archive_guest_automations", "restore_guest_automations",
    },
    "src/orchestrator/intent_discovery.py": {
        "find_similar_emerging_intent", "create_emerging_intent",
        "increment_intent_count", "record_intent_metric",
    },
    "src/orchestrator/smart_home_controller.py": {"_get_house_layout"},
    "src/orchestrator/main.py": {"get_origin_placeholder_patterns"},
}

SCOPED = {
    "create_voice_automation", "get_voice_automations", "archive_voice_automation",
    "restore_voice_automation", "archive_guest_automations", "restore_guest_automations",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request", "stream"}


def _functions(tree):
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, node)
    return found


def _assigned_sources(func, source):
    """name -> [source of every value assigned to it in the function]."""
    out = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(ast.get_source_segment(source, node.value) or "")
    return out


def _assigned_values(func, name):
    return [
        node.value
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == name
    ]


def _url_text(call, assigned, source):
    arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "url"), None)
    if arg is None:
        return ""
    if isinstance(arg, ast.Name):
        return " ".join(assigned.get(arg.id, []))
    return ast.get_source_segment(source, arg) or ""


def _expand_helpers(expr, functions, source):
    """The expression's source plus the source of any same-module function
    it calls (by bare name or self.<name>)."""
    text = ast.get_source_segment(source, expr) or ""
    for node in ast.walk(expr):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else (
                callee.attr if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
                and callee.value.id == "self" else None)
            if name and name in functions:
                text += "\n" + (ast.get_source_segment(source, functions[name]) or "")
    return text


def _client_headers_text(func, receiver, functions, source):
    """Header text of an httpx client bound to `receiver` in the function
    (`x = httpx.AsyncClient(headers=...)` or `async with ... as x`)."""
    texts = []
    for node in ast.walk(func):
        pairs = []
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            pairs = [(item.optional_vars, item.context_expr) for item in node.items]
        for target, value in pairs:
            if isinstance(target, ast.Name) and target.id == receiver and isinstance(value, ast.Call):
                for kw in value.keywords:
                    if kw.arg == "headers":
                        texts.append(_expand_helpers(kw.value, functions, source))
    return "\n".join(texts)


def scan(source: str, names: set):
    """{function: [(lineno, header_text or None)]} for every gated HTTP call
    in the named functions."""
    tree = ast.parse(source)
    functions = _functions(tree)
    results = {}
    for name in names:
        func = functions.get(name)
        if func is None:
            continue
        assigned = _assigned_sources(func, source)
        calls = []
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _HTTP_METHODS):
                continue
            if not GATED_PATH.search(_url_text(node, assigned, source)):
                continue
            header = next((k.value for k in node.keywords if k.arg == "headers"), None)
            text = _expand_helpers(header, functions, source) if header is not None else ""
            if isinstance(header, ast.Name):
                text += "\n" + "\n".join(
                    _expand_helpers(value, functions, source)
                    for value in _assigned_values(func, header.id)
                )
            receiver = node.func.value
            if isinstance(receiver, ast.Name):
                text += "\n" + _client_headers_text(func, receiver.id, functions, source)
            calls.append((node.lineno, text))
        results[name] = calls
    return results


def _all_results():
    out = {}
    for rel, names in TARGETS.items():
        source = (REPO_ROOT / rel).read_text()
        for name, calls in scan(source, names).items():
            out[(rel, name)] = calls
    return out


def test_population_is_the_sixteen_callers():
    results = _all_results()
    assert len(results) == 16, sorted(set((r, n) for r, ns in TARGETS.items() for n in ns) - set(results))
    empty = sorted(k for k, calls in results.items() if not calls)
    assert not empty, f"named callers with no gated call found: {empty}"
    names = {n for _r, n in results}
    for named in ("get_user_session_by_device", "create_voice_automation", "record_intent_metric"):
        assert named in names


def test_every_gated_call_sends_the_service_key():
    missing = sorted(
        f"{rel}:{lineno} {name}"
        for (rel, name), calls in _all_results().items()
        for lineno, text in calls
        if "X-Service-Key" not in text
    )
    assert not missing, f"{len(missing)} gated call(s) without X-Service-Key: {missing}"


def test_scoped_voice_calls_send_the_caller_mode():
    missing = sorted(
        f"{rel}:{lineno} {name}"
        for (rel, name), calls in _all_results().items()
        if name in SCOPED
        for lineno, text in calls
        if "X-Athena-Caller-Mode" not in text
    )
    assert not missing, f"{len(missing)} scoped call(s) without X-Athena-Caller-Mode: {missing}"


PLANTED = '''
async def unheadered(client, u):
    return await client.get(f"{u}/api/room-groups")

async def headered(client, u):
    return await client.get(f"{u}/api/room-groups", headers={"X-Service-Key": "k"})

async def via_client(u):
    async with httpx.AsyncClient(headers={"X-Service-Key": "k"}) as client:
        return await client.get(f"{u}/api/settings/house-layout")

async def via_variable(self, u):
    url = f"{u}/api/voice-automations"
    headers = self._voice_headers("owner", None)
    return await self.client.get(url, headers=headers)

def _voice_headers(self, mode, name):
    return {"X-Service-Key": self.api_key, "X-Athena-Caller-Mode": mode}

async def ungated(client, u):
    return await client.get(f"{u}/api/features/public")
'''


def test_planted_self_test():
    results = scan(PLANTED, {"unheadered", "headered", "via_client", "via_variable", "ungated"})
    assert [("X-Service-Key" in t) for _l, t in results["unheadered"]] == [False]
    assert [("X-Service-Key" in t) for _l, t in results["headered"]] == [True]
    assert [("X-Service-Key" in t) for _l, t in results["via_client"]] == [True]
    assert [("X-Athena-Caller-Mode" in t) for _l, t in results["via_variable"]] == [True]
    assert results["ungated"] == []
