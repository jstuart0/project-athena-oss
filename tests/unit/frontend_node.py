"""Helpers for the admin-frontend Node tests: a hard-failing node lookup and
a runner that loads classic browser scripts into one shared context.

Not a test file. A missing node is a failure, never a skip.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Union

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "admin" / "frontend"


def _resolve() -> str | None:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if not candidate:
        return None
    try:
        proc = subprocess.run([candidate, "--version"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return candidate if proc.returncode == 0 else None


NODE_BIN = _resolve()
if NODE_BIN is None:
    # Never a skip: these tests are the evidence the pages work (set ATHENA_NODE_BIN or install node).
    pytest.fail("no working node interpreter found (checked ATHENA_NODE_BIN, then PATH); the admin-frontend tests need one", pytrace=False)

Source = Union[str, Path]

# A permissive browser stub: an unknown global is a callable Proxy, so scripts that
# touch the DOM at load time still load. console.log is silenced (stdout carries the result).
DOM_STUB = """
console.log = console.info = console.warn = console.debug = () => {};
const handler = {
    get: (target, key) => {
        if (key === Symbol.toPrimitive) return () => '';
        if (key === 'then') return undefined;
        if (key === Symbol.iterator) return function* () {};
        return makeStub();
    },
    apply: () => makeStub(),
    construct: () => makeStub(),
    set: () => true,
    has: () => true,
};
function makeStub() { return new Proxy(function () {}, handler); }
global.document = makeStub();
Object.defineProperty(global, 'navigator', { value: { userAgent: 'node', platform: 'node', clipboard: makeStub() }, configurable: true });
global.location = { hash: '', href: 'http://localhost/', origin: 'http://localhost', pathname: '/', search: '', host: 'localhost', protocol: 'http:' };
const store = new Map();
global.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
global.sessionStorage = global.localStorage;
global.fetch = () => new Promise(() => {});
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
global.IntersectionObserver = global.ResizeObserver = global.MutationObserver = class { observe() {} disconnect() {} unobserve() {} };
global.requestAnimationFrame = () => 0;
global.lucide = makeStub();
global.Chart = makeStub();
global.FingerprintJS = makeStub();
global.tailwind = makeStub();
global.HTMLElement = class {};
global.CSS = { escape: (s) => s };
"""


def run_scripts(sources: Iterable[Source], expression: str, *, timezone: str = "UTC", prelude: str = "", timeout: int = 60):
    """Run the scripts (paths are read; strings are used as is) one after the
    other in one vm context with ``window`` and ``escapeHtml`` available, then
    evaluate ``expression`` (a JS expression; a promise is awaited) and return
    its JSON value."""
    parts: List[str] = []
    for source in sources:
        text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        parts.append(text)
    script = f"""
    'use strict';
    const vm = require('vm');
    global.window = global;
    {prelude}
    const parts = {json.dumps(parts)};
    parts.forEach((text, i) => vm.runInThisContext(text, {{ filename: 'part' + i + '.js' }}));
    Promise.resolve(vm.runInThisContext({json.dumps("(function() { return " + expression + "; })()")})).then((result) => {{
        process.stdout.write(JSON.stringify(result === undefined ? "__undefined__" : result));
    }});
    """
    proc = subprocess.run(
        [NODE_BIN, "-e", script], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "TZ": timezone},
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)
