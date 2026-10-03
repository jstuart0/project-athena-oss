"""Every admin UI ``fetch(`` to an admin-backend route that requires a user
sends ``Authorization`` (stdlib only).

For each ``fetch(`` in ``admin/frontend/*.js`` the first argument is
resolved by substituting same-file ``const|let|var NAME = '<literal>'``
values into ``${NAME}`` and bare ``NAME +`` prefixes, then matched against
the gated path prefixes. The options argument (possibly multi-line, up to
the matching ``)``) must contain ``getAuthHeaders(`` or ``Authorization``.
When the options are passed as a bare identifier, the nearest
``const|let|var NAME = ...`` before the call in the same top-level function
is read instead. Calls through ``apiRequest(``/``Athena.api(`` aren't
``fetch(`` calls and already authenticate.

``GATED_PREFIXES`` must cover every route only a signed-in user may call
(read from the route test's literal table): a fetch to one of those in a
family this scan doesn't look at would go unnoticed.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "admin" / "frontend"
POPULATION_TEST = REPO_ROOT / "admin" / "backend" / "tests" / "test_route_auth_population.py"

GATED_PREFIXES = (
    "/api/guests",
    "/api/user-sessions",
    "/api/room-groups",
    "/api/settings/llm-memory",
    "/api/settings/tool-proposals",
    "/api/settings/ollama-url",
    "/api/settings/house-layout",
    "/api/settings/directions-origin-placeholders",
    "/api/llm-backends/model/",
    "/api/sms/internal/",
    "/api/debug-logs",
    "/api/ha-pipelines/mode/set",
    "/api/voice-automations",
    "/api/pipeline-events",
    "/api/internal/emerging-intents",
    "/api/internal/intent-metrics",
    # The routes that answered anonymously before the route-auth review.
    "/api/alerts/public",
    "/api/cloud-llm-usage",
    "/api/cloud-providers",
    "/api/features/public",
    "/api/ha-pipelines",
    "/api/llm-backends/public",
    "/api/model-configs",
    "/api/modules",
    "/api/rag-service-bypass",
    "/api/room-tv",
    "/api/tool-calling",
    "/api/tool-proposals",
    "/api/voice-config",
    "/api/music-config/browser-playback",
    "/api/service-registry/services/",
    "/api/escalation/metrics",
)

AUTH_TOKENS = ("getAuthHeaders(", "Authorization")

_CONST = re.compile(r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(['"`])([^'"`$]*)\2""")


def _constants(text):
    return {m.group(1): m.group(3) for m in _CONST.finditer(text)}


def _matching_paren(text, open_index):
    """Index of the ')' closing the '(' at open_index; skips strings,
    template literals (with ${} nesting) and comments."""
    depth = 0
    i = open_index
    stack = []  # string delimiters / template states
    while i < len(text):
        ch = text[i]
        if stack and stack[-1] in "'\"":
            if ch == "\\":
                i += 2
                continue
            if ch == stack[-1]:
                stack.pop()
            i += 1
            continue
        if stack and stack[-1] == "`":
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                stack.pop()
            elif text.startswith("${", i):
                stack.append("{")
                i += 2
                continue
            i += 1
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if ch in "'\"`":
            stack.append(ch)
        elif ch == "{" and stack:
            stack.append("{")
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level(args_text):
    """Split on commas at depth 0 (outside (), [], {}, strings)."""
    parts, depth, start, i, quote = [], 0, 0, 0, None
    while i < len(args_text):
        ch = args_text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(args_text[start:i])
            start = i + 1
        i += 1
    parts.append(args_text[start:])
    return parts


def _resolve(url_text, consts):
    for name, value in consts.items():
        url_text = url_text.replace("${" + name + "}", value)
        url_text = re.sub(r"(?<![\w$.])" + re.escape(name) + r"\s*\+", value + " +", url_text)
        url_text = re.sub(r"^\s*" + re.escape(name) + r"\s*$", value, url_text)
    return url_text


_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_TOP_LEVEL_FUNCTION = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\b|^(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:function\b|\()",
    re.MULTILINE,
)


def _options_variable(text, name, before):
    """Source of the value last assigned to `name` by a declaration between
    the start of the enclosing top-level function and `before`; "" if none."""
    start = 0
    for m in _TOP_LEVEL_FUNCTION.finditer(text, 0, before):
        start = m.start()
    declared = None
    for m in re.finditer(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*", text[start:before]):
        declared = start + m.end()
    if declared is None:
        return ""
    if text[declared] == "{":
        depth = 0
        for i in range(declared, before):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[declared:i + 1]
        return text[declared:before]
    end = text.find(";", declared, before)
    return text[declared:before if end < 0 else end]


def scan_text(text):
    """[(line, resolved_url, authenticated)] for every gated fetch( in text."""
    consts = _constants(text)
    found = []
    for m in re.finditer(r"(?<![\w$.])fetch\s*\(", text):
        open_index = m.end() - 1
        close = _matching_paren(text, open_index)
        if close < 0:
            continue
        args = _split_top_level(text[open_index + 1:close])
        url = _resolve(args[0], consts)
        if not any(p in url for p in GATED_PREFIXES):
            continue
        options = ",".join(args[1:])
        if _IDENTIFIER.fullmatch(options.strip()):
            options = _options_variable(text, options.strip(), m.start())
        authed = any(token in options for token in AUTH_TOKENS)
        found.append((text.count("\n", 0, m.start()) + 1, url.strip(), authed))
    return found


@functools.lru_cache(maxsize=None)
def _all_sites():
    sites = []
    for path in sorted(FRONTEND.glob("*.js")):
        for line, url, authed in scan_text(path.read_text()):
            sites.append((path.name, line, url, authed))
    return tuple(sites)


def test_gated_fetches_send_authorization():
    sites = _all_sites()
    assert len(sites) >= 97, len(sites)
    named = {(f, u) for f, _l, u, _a in sites}
    assert any(f == "room-groups.js" and "/api/room-groups/available-rooms" in u for f, u in named)
    assert any(f == "admin-jarvis.js" and "/api/pipeline-events" in u for f, u in named)
    assert any(f == "voice-config.js" and "/api/voice-config/running-config" in u for f, u in named)
    assert any(f == "alerts.js" and "/api/alerts/public/active-by-type" in u for f, u in named)
    assert any(f == "cloud-providers.js" and "/api/cloud-providers" in u for f, u in named)
    assert any(f == "app.js" and "/api/service-registry/services/" in u for f, u in named)
    bare = [f"{f}:{l} {u}" for f, l, u, a in sites if not a]
    assert not bare, f"{len(bare)} gated fetch(es) without Authorization: {bare}"


PLANTED = """
const API_X = '/api/room-groups';
async function a() {
    const r = await fetch(`${API_X}/available-rooms`);
}
async function b() {
    const r = await fetch('/api/guests/current', {
        method: 'GET',
    });
}
async function c() {
    const r = await fetch(API_X + '/resolve/x', {
        method: 'GET',
        headers: getAuthHeaders()
    });
}
async function d() {
    const r = await fetch('/api/auth/methods');
}
async function e() {
    const fetchOptions = {
        headers: getAuthHeaders()
    };
    const r = await fetch('/api/cloud-providers', fetchOptions);
}
async function f() {
    const fetchOptions = { method: 'GET' };
    const r = await fetch('/api/cloud-providers', fetchOptions);
}
async function g() {
    const r = await fetch('/api/voice-config/health', { headers: { 'Content-Type': 'application/json' } });
}
async function h() {
    const r = await fetch('/api/cloud-providers', fetchOptions);
}
"""


def test_planted_self_test():
    found = scan_text(PLANTED)
    assert [(u.startswith("/api/room-groups/available-rooms") or "available-rooms" in u, a) for _l, u, a in found][0] == (True, False)
    # a, b, c, e, f, g, then h: the same options name as e, declared only in
    # another function, authenticates nothing here.
    assert [a for _l, _u, a in found] == [False, False, True, True, False, False, False]
    assert all("/api/auth/methods" not in u for _l, u, _a in found)


@functools.lru_cache(maxsize=None)
def _user_only_routes():
    tree = ast.parse(POPULATION_TEST.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REVIEWED_BY_FILE" for t in node.targets
        ):
            by_file = ast.literal_eval(node.value)
            return tuple(sorted(op for ops in by_file.values() for op, (kind, _p) in ops.items() if kind == "user"))
    raise AssertionError("REVIEWED_BY_FILE not found as a literal")


def _uncovered(routes, prefixes):
    return [op for op in routes if not any(op[1].startswith(prefix) for prefix in prefixes)]


def test_prefix_list_covers_every_user_only_route():
    routes = _user_only_routes()
    assert len(routes) >= 42, len(routes)
    assert ("GET", "/api/music-config/browser-playback") in routes
    uncovered = _uncovered(routes, GATED_PREFIXES)
    assert not uncovered, f"{len(uncovered)} user-only route(s) no prefix covers: {uncovered}"


def test_prefix_coverage_self_test():
    routes = _user_only_routes()
    without = tuple(p for p in GATED_PREFIXES if p not in (
        "/api/music-config/browser-playback", "/api/service-registry/services/", "/api/escalation/metrics"))
    assert _uncovered(routes, without) == [
        ("GET", "/api/escalation/metrics/prometheus"),
        ("GET", "/api/music-config/browser-playback"),
        ("GET", "/api/service-registry/services/{service_name}"),
    ]
