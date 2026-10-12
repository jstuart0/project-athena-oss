"""The admin panel's classic scripts, in the order index.html loads them.

Every listed file shares one global scope, so a name declared at the top level
of two of them silently shadows (a function) or throws (a const). Two checks:

- static: no top-level function/const/let/class name is declared in two of the
  files index.html lists;
- dynamic: every listed local file is loaded in order into one Node vm context
  with a permissive DOM stub, and the status files must load without an
  exception and expose the shared vocabulary to the pages that use it.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from .frontend_node import DOM_STUB, FRONTEND, NODE_BIN, requires_node, run_scripts

pytestmark = requires_node

DOCKERFILE = FRONTEND / "Dockerfile"
STATUS_FILES = ["utils.js", "status-vocab.js", "mission-control.js", "voice-pipelines.js", "app.js", "service-control.js",
                "system-config.js", "voice-config.js"]
ALWAYS_LOADS = STATUS_FILES + ["escape-html.js"]

# Scripts index.html lists that are not in the repository (the browser gets a 404
# and carries on). Present at the base commit; compared exactly, so adding the
# file, or listing another missing one, is a change to look at.
ABSENT_AT_BASE = {
    "mode-audit.js": "listed by index.html, never added to the repo or the Dockerfile",
    "notifications.js": "listed by index.html, never added to the repo or the Dockerfile",
}

# Files that cannot load under the stub, with the reason. Compared exactly: a
# file that starts loading (or stops) is a change to look at.
LOAD_SKIP: dict[str, str] = {}

FUNCTION_DECLARATION = re.compile(r"^(?:async\s+)?function\s*\*?\s+([A-Za-z_$][\w$]*)", re.M)
LEXICAL_DECLARATION = re.compile(r"^(?:const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)

# Function declarations that two listed files both make at the top level. The
# later file wins silently; it was already so at the base commit. Compared
# exactly, so a new one fails and fixing one makes this list shrink.
PREEXISTING_FUNCTION_SHADOWS = {
    "apiRequest": ["app.js", "utils.js"],
    "closeApiKeyModal": ["cloud-providers.js", "user-api-keys.js"],
    "closeModal": ["app.js", "routing.js"],
    "formatBytes": ["model-downloads.js", "utils.js"],
    "formatDate": ["calendar-sources.js", "guest-mode.js", "memory-management.js", "model-downloads.js", "room-audio.js", "utils.js"],
    "formatDuration": ["admin-jarvis.js", "conversations.js", "system-config.js", "utils.js"],
    "formatNumber": ["cloud-providers.js", "model-downloads.js"],
    "formatTimeAgo": ["alerts.js", "service-control.js"],
    "getAuthHeaders": ["conversations.js", "utils.js"],
    "getStatusBadge": ["calendar-sources.js", "sms.js"],
    "getToken": ["app.js", "calendar-sources.js", "voice-config.js"],
    "loadMLXApplicability": ["features.js", "llm-components.js", "presets.js"],
    "loadPresets": ["model-config.js", "presets.js"],
    "loadServices": ["app.js", "service-control.js"],
    "renderIntentsTable": ["analytics.js", "emerging-intents.js"],
    "safeShowToast": ["escalation.js", "utils.js"],
    "showAddGuestModal": ["guest-context.js", "guest-mode.js"],
    "showCreateHallucinationCheckModal": ["app.js", "crud-extensions.js"],
    "showCreateValidationModelModal": ["app.js", "crud-extensions.js"],
    "showError": ["app.js", "base-knowledge.js", "calendar-sources.js", "follow-me.js", "room-audio.js", "room-groups.js", "tool-calling.js", "utils.js"],
    "showLoadingState": ["calendar-sources.js", "follow-me.js", "room-audio.js"],
    "showNotification": ["admin-jarvis.js", "cloud-providers.js", "utils.js"],
    "showSuccess": ["app.js", "utils.js"],
}


def served_name_to_file() -> dict[str, str]:
    """URL path -> source file, from the Dockerfile's COPY lines (some are renamed)."""
    mapping = {}
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"COPY\s+(\S+\.js)\s+/usr/share/nginx/html/(\S*)", line)
        if m:
            mapping[(m.group(2) or m.group(1)).rstrip("/") or m.group(1)] = m.group(1)
    return mapping


def listed_files() -> list[str]:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    served = served_name_to_file()
    files = []
    for src in re.findall(r'<script\s+src="(/[^"?]+\.js)(?:\?[^"]*)?"', html):
        name = src.lstrip("/")
        files.append(served.get(name, name))
    return files


def present_files() -> list[str]:
    return [name for name in listed_files() if name not in ABSENT_AT_BASE]


def declarations(text: str) -> set[str]:
    return set(FUNCTION_DECLARATION.findall(text)) | set(LEXICAL_DECLARATION.findall(text))


def _owners(files: dict[str, str], pattern) -> dict[str, list[str]]:
    owners = defaultdict(list)
    for name, text in files.items():
        for declared in set(pattern.findall(text)):
            owners[declared].append(name)
    return {n: sorted(f) for n, f in owners.items() if len(f) > 1}


def lexical_collisions(files: dict[str, str]) -> dict[str, list[str]]:
    """const/let/class declared in two files: a SyntaxError at load. Never allowed."""
    return _owners(files, LEXICAL_DECLARATION)


def function_collisions(files: dict[str, str]) -> dict[str, list[str]]:
    return _owners(files, FUNCTION_DECLARATION)


def test_the_listed_files_exist_and_are_in_the_order_the_pages_need():
    files = listed_files()
    assert len(files) >= 50 and len(set(files)) == len(files)
    missing = {name for name in files if not (FRONTEND / name).is_file()}
    assert missing == set(ABSENT_AT_BASE)
    order = {name: i for i, name in enumerate(files)}
    assert order["utils.js"] < order["status-vocab.js"] < order["mission-control.js"] < order["voice-pipelines.js"] < order["app.js"]
    for page in ("service-control.js", "system-config.js", "voice-config.js"):
        assert order["status-vocab.js"] < order[page]
    assert files[-1] == "escape-html.js", "escape-html.js stays the last local script"


def test_no_top_level_name_is_declared_in_two_listed_files_beyond_the_known_shadows():
    files = {name: (FRONTEND / name).read_text(encoding="utf-8") for name in present_files()}
    assert lexical_collisions(files) == {}
    assert function_collisions(files) == PREEXISTING_FUNCTION_SHADOWS


def test_the_collision_checks_have_a_floor_and_catch_a_planted_duplicate():
    files = {name: (FRONTEND / name).read_text(encoding="utf-8") for name in present_files()}
    assert "statusInfo" in declarations(files["status-vocab.js"])
    assert len(declarations(files["app.js"])) > 50
    planted = dict(files)
    planted["app.js"] = files["app.js"] + "\nfunction statusInfo() { return 1; }\n"
    assert function_collisions(planted) == {**PREEXISTING_FUNCTION_SHADOWS, "statusInfo": ["app.js", "status-vocab.js"]}
    planted_const = dict(files)
    planted_const["system-config.js"] = files["system-config.js"] + "\nconst TONE_DOT = {};\n"
    assert lexical_collisions(planted_const) == {"TONE_DOT": ["status-vocab.js", "system-config.js"]}


STUB = DOM_STUB


def load_all() -> dict:
    files = present_files()
    sources = [FRONTEND / name for name in files]
    # Load each file separately so one failure is reported by name instead of hiding the rest.
    expression = "__results"
    prelude = STUB + """
    const fs = require('fs');
    const __results = [];
    global.__results = __results;
    const __files = %s;
    """ % json.dumps([str(p) for p in sources])
    loader = """
    (function () {
        const vm = require('vm');
        const fs = require('fs');
        const out = [];
        for (const file of __files) {
            try {
                vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
                out.push([file.split('/').pop(), null]);
            } catch (error) {
                out.push([file.split('/').pop(), String(error && error.message || error).slice(0, 200)]);
            }
        }
        return out;
    })()
    """
    results = run_scripts([], loader.strip(), prelude=prelude, timeout=120)
    return {name: error for name, error in results}


@pytest.fixture(scope="module")
def loaded():
    return load_all()


def test_every_listed_file_loads_except_the_reasoned_skips(loaded):
    failures = {name: error for name, error in loaded.items() if error}
    assert set(failures) == set(LOAD_SKIP), failures
    assert len(loaded) == len(present_files())


def test_the_status_files_and_escape_html_always_load(loaded):
    for name in ALWAYS_LOADS:
        assert loaded[name] is None, (name, loaded[name])


def test_the_pages_see_the_shared_vocabulary_after_loading_in_order():
    probe = """
    (function () {
        const vm = require('vm');
        const fs = require('fs');
        for (const file of __files) {
            try { vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file }); } catch (e) {}
        }
        return {
            statusInfo: typeof statusInfo,
            tile: typeof TONE_TILE,
            service: serviceStatus({ health_status: 'x' }),
            serviceFallback: serviceStatus(null),
            tone: statusInfo('stale').tone,
        };
    })()
    """
    prelude = STUB + "const __files = %s;" % json.dumps([str(FRONTEND / n) for n in present_files()])
    out = run_scripts([], probe.strip(), prelude=prelude, timeout=120)
    assert out == {"statusInfo": "function", "tile": "object", "service": "x", "serviceFallback": "unknown", "tone": "neutral"}


def test_a_planted_duplicate_const_in_a_page_fails_the_dynamic_load():
    """The static scan and the load agree: a const that clashes with the vocabulary throws."""
    text = (FRONTEND / "status-vocab.js").read_text(encoding="utf-8")
    clash = "const TONE_DOT = {};\n"
    prelude = STUB
    probe = """
    (function () {
        const vm = require('vm');
        try { vm.runInThisContext(%s); vm.runInThisContext(%s); return 'loaded'; } catch (e) { return e.name; }
    })()
    """ % (json.dumps(text), json.dumps(clash))
    assert run_scripts([], probe.strip(), prelude=prelude) == "SyntaxError"
