#!/usr/bin/env node
/**
 * ATHENA-67 load-order regression guard.
 *
 * admin/frontend/*.js are classic (non-module) scripts loaded in document
 * order from index.html. Nine files declare a bare top-level `function
 * escapeHtml`; each later declaration overwrites `window.escapeHtml`. This
 * script proves that after loading every local script in the exact order
 * index.html declares them, `window.escapeHtml` and `window.escapeJsAttr`
 * are bound to the implementations from escape-html.js — not to whichever
 * of the nine legacy declarations happens to run last.
 *
 * The check is reference-identity based, not behavioral: escape-html.js's
 * escapeHtml is a byte-for-byte copy of conversations.js's implementation
 * (both are "correct" by the '&#39;' output test), so a behavioral check
 * would false-pass even if escape-html.js's <script> tag were accidentally
 * moved before conversations.js's. Instead this harness tracks, for each
 * global rebind of window.escapeHtml / window.escapeJsAttr, which file's
 * execution caused it, and asserts the LAST rebind of each came from
 * escape-html.js.
 *
 * Exits 0 on success, non-zero (with a diagnostic) on failure.
 *
 * `--check hardening` (Phase 7, D13) additionally asserts that, after the
 * real load order runs, `window.escapeHtml` / `window.escapeJsAttr` are
 * non-writable and non-configurable, and that a runtime reassignment after
 * load cannot replace the canonical function. This is threat 2 of D13's
 * three-threat table (runtime overwrite) — the ONE threat `defineProperty`
 * actually covers. It does not (and cannot) prove anything about threat 1
 * (declaration drift, guarded by check-escape-html-uniqueness.py) or
 * threat 3 (tag-order drift, guarded by the identity check above).
 *
 * `--index <path>` / `--frontend-dir <path>` override the real
 * admin/frontend/index.html and admin/frontend/ directory — used by
 * tests/unit/test_frontend_guard_scripts.py to build isolated temp trees
 * for the tag-order-drift and declaration-drift CAN-FAIL fixtures without
 * touching the real tree.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const FRONTEND_DIR = path.resolve(__dirname, '..', 'admin', 'frontend');
const INDEX_HTML = path.join(FRONTEND_DIR, 'index.html');

function parseArgs(argv) {
    const args = { check: null, index: null, frontendDir: null };
    for (let i = 0; i < argv.length; i++) {
        if (argv[i] === '--check') args.check = argv[++i];
        else if (argv[i] === '--index') args.index = argv[++i];
        else if (argv[i] === '--frontend-dir') args.frontendDir = argv[++i];
    }
    return args;
}

function extractLocalScriptFiles(indexHtmlPath) {
    const html = fs.readFileSync(indexHtmlPath, 'utf8');
    const tagRe = /<script\s+src="\/([^"]+)"\s*><\/script>/g;
    const files = [];
    let match;
    while ((match = tagRe.exec(html)) !== null) {
        const rawSrc = match[1]; // e.g. "escape-html.js?v=20260913"
        const filename = rawSrc.split('?')[0];
        files.push(filename);
    }
    if (files.length === 0) {
        throw new Error(`No local <script src="/..."> tags found in ${indexHtmlPath}`);
    }
    return files;
}

function makeStubContext() {
    // Minimal, permissive DOM-ish stub. Real scripts will throw partway
    // through on missing APIs — that's fine and expected: top-level function
    // declarations are hoisted and bound to the global object before any
    // statement in the script executes, so a runtime error later in a
    // script does not undo earlier hoisting. We catch and log per-file.
    function makeAutoStubNode() {
        const handler = {
            get(target, prop) {
                if (prop === 'then' || prop === Symbol.toPrimitive || prop === Symbol.iterator) {
                    return undefined;
                }
                if (!(prop in target)) {
                    target[prop] = function stub() {
                        return makeAutoStubNode();
                    };
                }
                return target[prop];
            },
            set(target, prop, value) {
                target[prop] = value;
                return true;
            },
        };
        return new Proxy({}, handler);
    }

    const sandbox = {};
    sandbox.window = sandbox; // classic-script global === window
    sandbox.globalThis = sandbox;
    sandbox.self = sandbox;
    sandbox.document = makeAutoStubNode();
    sandbox.navigator = { userAgent: 'node-harness' };
    sandbox.location = { href: 'http://localhost/', hostname: 'localhost', pathname: '/' };
    sandbox.localStorage = (function () {
        const store = new Map();
        return {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
            clear: () => store.clear(),
        };
    })();
    sandbox.sessionStorage = sandbox.localStorage;
    sandbox.console = console;
    sandbox.fetch = function stubFetch() {
        return Promise.resolve(makeAutoStubNode());
    };
    sandbox.WebSocket = function StubWebSocket() {};
    sandbox.setTimeout = setTimeout;
    sandbox.clearTimeout = clearTimeout;
    sandbox.setInterval = setInterval;
    sandbox.clearInterval = clearInterval;
    sandbox.alert = function () {};
    sandbox.confirm = function () {
        return false;
    };
    sandbox.CustomEvent = function StubCustomEvent() {};
    sandbox.URLSearchParams = URLSearchParams;
    sandbox.URL = URL;

    return vm.createContext(sandbox);
}

function main() {
    const args = parseArgs(process.argv.slice(2));
    const indexHtmlPath = args.index || INDEX_HTML;
    const frontendDir = args.frontendDir || FRONTEND_DIR;

    const scriptFiles = extractLocalScriptFiles(indexHtmlPath);
    console.log(`Found ${scriptFiles.length} local <script> tags in index.html, in order:`);
    scriptFiles.forEach((f, i) => console.log(`  ${i + 1}. ${f}`));

    const context = makeStubContext();

    let escapeHtmlOwner = null;
    let escapeJsAttrOwner = null;
    const executionErrors = [];
    // D13 threat 3: once escape-html.js's IIFE has run (and frozen the two
    // globals), any script tagged AFTER it that throws during load is not a
    // benign DOM-stub artifact — under load-last, nothing should ever load
    // after the freeze, and a colliding `function escapeHtml` declaration in
    // such a file throws a SyntaxError at GlobalDeclarationInstantiation
    // time (CanDeclareGlobalFunction returns false for a non-configurable,
    // non-writable property). Tracked separately so it fails the gate
    // instead of being logged as "not fatal".
    let freezeApplied = false;
    const postFreezeErrors = [];

    for (const filename of scriptFiles) {
        const filePath = path.join(frontendDir, filename);
        if (!fs.existsSync(filePath)) {
            console.log(`  [skip] ${filename} (not found in admin/frontend/ — likely a non-local or generated asset)`);
            continue;
        }
        const source = fs.readFileSync(filePath, 'utf8');

        const escapeHtmlBefore = context.escapeHtml;
        const escapeJsAttrBefore = context.escapeJsAttr;

        let threw = false;
        try {
            const script = new vm.Script(source, { filename });
            script.runInContext(context, { timeout: 5000 });
        } catch (err) {
            threw = true;
            if (freezeApplied) {
                postFreezeErrors.push({ filename, message: err.message });
            } else {
                executionErrors.push({ filename, message: err.message });
            }
        }

        if (context.escapeHtml !== escapeHtmlBefore) {
            escapeHtmlOwner = filename;
        }
        if (context.escapeJsAttr !== escapeJsAttrBefore) {
            escapeJsAttrOwner = filename;
        }

        if (filename === 'escape-html.js' && !threw) {
            freezeApplied = true;
        }
    }

    console.log('');
    console.log(`Final window.escapeHtml was last (re)bound by: ${escapeHtmlOwner}`);
    console.log(`Final window.escapeJsAttr was last (re)bound by: ${escapeJsAttrOwner}`);
    if (executionErrors.length > 0) {
        console.log('');
        console.log(`(${executionErrors.length} script(s) threw a runtime error after their own top-level`);
        console.log(' function declarations were hoisted — expected, given the bare DOM stub. Not fatal.)');
    }

    const failures = [];

    for (const e of postFreezeErrors) {
        failures.push(
            `"${e.filename}" is tagged AFTER escape-html.js and threw while loading: ${e.message}. ` +
                'A script positioned after the canonical escaping file must never throw during load — this is ' +
                'D13 threat 3 (tag-order drift): the frozen escapeHtml/escapeJsAttr reject a colliding global ' +
                'declaration in this file. Move its <script> tag before escape-html.js\'s.'
        );
    }

    if (escapeHtmlOwner !== 'escape-html.js') {
        failures.push(
            `window.escapeHtml was last bound by "${escapeHtmlOwner}", not "escape-html.js". ` +
                `escape-html.js's <script> tag must be the LAST local script tag in index.html.`
        );
    }

    if (escapeJsAttrOwner !== 'escape-html.js') {
        failures.push(
            `window.escapeJsAttr was last bound by "${escapeJsAttrOwner}", expected "escape-html.js". ` +
                `escapeJsAttr is only defined in escape-html.js — this likely means escape-html.js never ran.`
        );
    }

    if (typeof context.escapeHtml !== 'function') {
        failures.push('window.escapeHtml is not a function after loading all scripts.');
    } else if (context.escapeHtml.toString().includes('createElement')) {
        failures.push(
            'window.escapeHtml resolves to a DOM round-trip implementation (contains "createElement"). ' +
                'This is the vulnerable innerHTML-based pattern this hotfix removes.'
        );
    }

    if (typeof context.escapeJsAttr !== 'function') {
        failures.push('window.escapeJsAttr is not a function after loading all scripts.');
    }

    // Behavioral sanity check (in addition to, never instead of, the identity checks above).
    if (typeof context.escapeHtml === 'function') {
        const out = context.escapeHtml('<x>&"\'');
        if (out !== '&lt;x&gt;&amp;&quot;&#39;') {
            failures.push(`window.escapeHtml produced unexpected output: ${JSON.stringify(out)}`);
        }
    }
    if (typeof context.escapeJsAttr === 'function') {
        const out = context.escapeJsAttr("x');alert(1)//");
        if (out !== "x\\&#39;);alert(1)//") {
            failures.push(`window.escapeJsAttr produced unexpected output: ${JSON.stringify(out)}`);
        }
    }

    if (args.check === 'hardening' && failures.length === 0) {
        const descHtml = Object.getOwnPropertyDescriptor(context, 'escapeHtml');
        const descAttr = Object.getOwnPropertyDescriptor(context, 'escapeJsAttr');
        if (!descHtml || descHtml.writable !== false || descHtml.configurable !== false) {
            failures.push(
                'window.escapeHtml is not frozen (expected writable:false, configurable:false) after load — ' +
                    'the two Object.defineProperty calls in escape-html.js are missing or were bypassed.'
            );
        }
        if (!descAttr || descAttr.writable !== false || descAttr.configurable !== false) {
            failures.push(
                'window.escapeJsAttr is not frozen (expected writable:false, configurable:false) after load.'
            );
        }
        if (failures.length === 0) {
            const canonicalHtml = context.escapeHtml;
            const canonicalAttr = context.escapeJsAttr;
            try {
                context.escapeHtml = function pwned() {
                    return 'PWNED';
                };
            } catch (e) {
                // Strict-mode assignment to a non-writable property throws.
                // Expected and fine — the assertion below is what matters.
            }
            try {
                context.escapeJsAttr = function pwned() {
                    return 'PWNED';
                };
            } catch (e) {
                // Same as above.
            }
            if (context.escapeHtml !== canonicalHtml) {
                failures.push(
                    'a runtime reassignment of window.escapeHtml AFTER load was not rejected — the canonical ' +
                        'function was replaced. D13 threat 2 (runtime overwrite) is unguarded.'
                );
            }
            if (context.escapeJsAttr !== canonicalAttr) {
                failures.push(
                    'a runtime reassignment of window.escapeJsAttr AFTER load was not rejected.'
                );
            }
            if (failures.length === 0) {
                console.log('PASS [hardening]: escapeHtml/escapeJsAttr are frozen and survive a runtime reassignment attempt.');
            }
        }
    }

    console.log('');
    if (failures.length > 0) {
        console.error('FAIL: load-order guard failed:');
        for (const f of failures) {
            console.error(`  - ${f}`);
        }
        process.exit(1);
    }

    console.log('PASS: window.escapeHtml and window.escapeJsAttr both resolve to escape-html.js as the last loaded script.');
    process.exit(0);
}

main();
