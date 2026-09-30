"""Every admin UI ``fetch(`` to an admin-backend route that requires a user
sends ``Authorization`` (stdlib only).

For each ``fetch(`` in ``admin/frontend/*.js`` the first argument is
resolved by substituting same-file ``const|let|var NAME = '<literal>'``
values into ``${NAME}`` and bare ``NAME +`` prefixes, then matched against
the gated path prefixes. The options argument (possibly multi-line, up to
the matching ``)``) must contain ``getAuthHeaders(`` or ``Authorization``.
Calls through ``apiRequest(``/``Athena.api(`` aren't ``fetch(`` calls and
already authenticate.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "admin" / "frontend"

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
)

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
        authed = "getAuthHeaders(" in options or "Authorization" in options
        found.append((text.count("\n", 0, m.start()) + 1, url.strip(), authed))
    return found


def _all_sites():
    sites = []
    for path in sorted(FRONTEND.glob("*.js")):
        for line, url, authed in scan_text(path.read_text()):
            sites.append((path.name, line, url, authed))
    return sites


def test_gated_fetches_send_authorization():
    sites = _all_sites()
    assert len(sites) >= 11, sites
    named = {(f, u) for f, _l, u, _a in sites}
    assert any(f == "room-groups.js" and "/api/room-groups/available-rooms" in u for f, u in named)
    assert any(f == "admin-jarvis.js" and "/api/pipeline-events" in u for f, u in named)
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
    const r = await fetch('/api/features/public');
}
"""


def test_planted_self_test():
    found = scan_text(PLANTED)
    assert [(u.startswith("/api/room-groups/available-rooms") or "available-rooms" in u, a) for _l, u, a in found][0] == (True, False)
    assert [a for _l, _u, a in found] == [False, False, True]
    assert all("/api/features/public" not in u for _l, u, _a in found)
