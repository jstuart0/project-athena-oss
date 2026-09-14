/**
 * Canonical HTML/JS escaping helpers for the admin frontend.
 *
 * This file MUST be loaded as the LAST local <script> tag in index.html.
 * These are classic (non-module) scripts: a top-level `function` declaration
 * in a classic script writes a property on `window` at evaluation time, and
 * later scripts silently overwrite earlier ones of the same name. Several
 * other files in this directory still declare their own local `escapeHtml`;
 * loading this file last guarantees `window.escapeHtml` / `window.escapeJsAttr`
 * resolve to the implementations below, without having to touch those files.
 *
 * This file is an IIFE (not a bare classic script) so that `escapeJsAttr`'s
 * internal call to `escapeHtml` is bound lexically to the implementation
 * defined in this file, not resolved through the global at call time. If it
 * were a plain top-level declaration, `escapeJsAttr` would call whichever
 * `escapeHtml` most recently won the global-clobbering race described above
 * — which may not escape `"`, silently reopening the exact class of
 * attribute-breakout XSS this file exists to close.
 *
 * ---------------------------------------------------------------------------
 * CONTRACT — there are three output contexts, not two. Picking the wrong one
 * is how the next XSS defect lands.
 *
 * 1. Inside an `on*=` handler, in a quoted JS STRING LITERAL position, e.g.
 *      onclick="foo('${VALUE}')"
 *    Use escapeJsAttr(value). The value must first survive as a JS string
 *    literal (quotes/backslashes/line terminators escaped), and the RESULT
 *    of that escaping must then survive as an HTML attribute value (so the
 *    browser's HTML parser doesn't decode a character reference back into a
 *    quote before the handler body is compiled as JS). escapeJsAttr does
 *    both steps, in that order.
 *
 * 2. Anywhere else in HTML — a text node, a plain attribute (`title=`,
 *    `value=`, `alt=`, etc.).
 *    Use escapeHtml(value). A single entity-encoding pass is correct and
 *    sufficient.
 *
 * 3. Inside an `on*=` handler in BARE-EXPRESSION position — a number, a
 *    boolean, a JSON object/array literal, e.g. onclick="foo(${id})".
 *    Use NEITHER. escapeJsAttr would wrap the value in escaped quotes and
 *    corrupt the expression (a number becomes a broken string literal).
 *    If the value must be untrusted, validate/coerce its TYPE (parseInt,
 *    JSON.stringify with attribute-safe encoding, etc.) — that is a
 *    different problem from string escaping and is out of scope here.
 *
 * escapeJsAttr is NOT safe for, and must NOT be used in:
 *   - href="javascript:..." — the URL undergoes a separate decode stage;
 *     use a URL/scheme allowlist instead.
 *   - style="..." — CSS has its own escaping rules; entity-encoding a CSS
 *     value does not neutralize `expression()`/`url()` payloads.
 *   - <script> bodies, or any value later assigned to a live function
 *     (Function constructor, eval, setTimeout(string), addEventListener
 *     string handlers) — these contexts never go through an HTML decode
 *     step, so an entity reference like `&#39;` is never decoded back to
 *     `'` and leaks into the running script literally instead of being
 *     interpreted as data.
 *   - srcdoc — treated as a nested HTML document, not an attribute value.
 * ---------------------------------------------------------------------------
 */
(function (global) {
    'use strict';

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function escapeJsAttr(str) {
        if (str === null || str === undefined) return '';
        // Order matters: backslashes must be escaped FIRST. Escaping quotes
        // before backslashes lets an attacker-supplied backslash re-pair
        // with the escaping backslash inserted for a later quote and break
        // out of the JS string literal.
        const js = String(str)
            .replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '\\"')
            .replace(/\n/g, '\\n')
            .replace(/\r/g, '\\r')
            .replace(/\u2028/g, '\\u2028')
            .replace(/\u2029/g, '\\u2029');
        return escapeHtml(js);
    }

    global.escapeHtml = escapeHtml;
    global.escapeJsAttr = escapeJsAttr;

    // D13: freeze against RUNTIME overwrite only (a console paste, a
    // lazily-injected script, a later feature's dynamic <script> tag). This
    // does NOT guard declaration drift -- under load-last, no script
    // executes after this IIFE runs, so an earlier `function escapeHtml`
    // in a file tagged BEFORE this one is a silent no-op at evaluation
    // time, not a throw; scripts/check-escape-html-uniqueness.py is the
    // guard for that. It does not guard tag-order drift either -- a
    // <script> appended AFTER this one is the one case that DOES execute
    // post-freeze, so it throws here AND trips
    // scripts/check-frontend-escape-load-order.js. Three threats, three
    // mechanisms; see admin/frontend/README.md.
    //
    // Placed INSIDE the IIFE, using the lexically bound `escapeHtml` /
    // `escapeJsAttr` locals just assigned above, not appended after the
    // closing `})(window);` -- an unqualified reference out there would
    // resolve through the global object record, the exact late-binding
    // hazard this IIFE exists to eliminate, and a future global lexical
    // `const escapeHtml` in an earlier script would shadow the property
    // and freeze the wrong function.
    Object.defineProperty(global, 'escapeHtml', { value: escapeHtml, writable: false, configurable: false });
    Object.defineProperty(global, 'escapeJsAttr', { value: escapeJsAttr, writable: false, configurable: false });
})(window);
