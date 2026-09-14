# admin/frontend escaping

`admin/frontend/*.js` are classic (non-module) scripts loaded in document order
from `index.html`. There is no bundler, no module system, and no framework —
every file shares one global `window`. This document is the contract for how
untrusted data gets into the DOM without becoming script.

## What this does NOT cover — read this before trusting the guards

The guards in this directory enforce **four populations at absolute zero**: wrong-primitive handler
sites, unescaped handler interpolations, non-canonical `escapeHtml` definitions, and unescaped callee
sinks. That is not the same as "this frontend is safe from XSS," and anyone who reads it that way will
make a bad decision.

Measured at the time of writing, still live and deliberately **not** fixed:

| Population | Count | Status |
|---|---:|---|
| bare-expression handler interpolations (`${entry.id}`, `${index}`) | **169** | ratcheted at 169, not fixed |
| unescaped data-bearing plain attributes | **344** (44 files) | ratcheted at 344, not fixed |
| `.innerHTML =` assignments | **442** (52 files) | ratcheted at 442, not audited |
| `insertAdjacentHTML` sites | **21** (9 files) | only `emerging-intents.js`'s 6 were fixed |
| `script-src 'unsafe-inline'` in `nginx.conf` | present | **no CSP backstop** |

**Escaping in this frontend is the exception, not the rule.** Roughly 12% of data interpolations into
HTML-building files carry an escaping call. A ratchet stops a population growing; it does not clean it.

Two consequences worth stating plainly:

1. **Because there is no CSP backstop, the correctness of this consolidation is load-bearing with no
   second line of defence.** A single missed call site is a full bypass. That is why every guard here
   has a fixture proving it can fail, and why `unresolved` is a violation rather than a pass.
2. **`apps/jarvis-web/` is not covered by any of these guards** — the CI `paths:` filter is scoped to
   `admin/frontend/**`. That tree has its own quote-unsafe `escapeHtml`, two handler sites using it,
   unescaped OpenStreetMap place names, and a `music-player.js` with no escaping function at all. It
   is the *end-user-facing* surface, not an authenticated admin one. Tracked as **ATHENA-68**.

Related, and the same pathology this document exists to prevent: `showError` has **8 definitions with
two incompatible signatures**, and the global winner has an unescaped `innerHTML` sink that is inert
today only because callers pass the wrong number of arguments. Tracked as **ATHENA-70**.

## The two primitives, and which context each is for

Both live in `admin/frontend/escape-html.js`, defined inside an IIFE and
exported as `window.escapeHtml` / `window.escapeJsAttr`.

| Primitive | Context | Example |
|---|---|---|
| `escapeHtml(value)` | A text node, or a plain HTML attribute (`title=`, `value=`, `alt=`, `data-*`) | `` `<div title="${escapeHtml(name)}">${escapeHtml(name)}</div>` `` |
| `escapeJsAttr(value)` | Inside an `on*=` handler, in a quoted **JS string literal** position | `` `<button onclick="doThing('${escapeJsAttr(name)}')">` `` |
| *(neither)* | Inside an `on*=` handler, in **bare-expression** position — a number, boolean, or object/array literal | `` `<button onclick="doThing(${id})">` `` — validate/coerce the type instead |

`escapeJsAttr` does two passes, in order: backslash-first JS-string escaping
(quotes, backslashes, line terminators, ` `/` `), then a single
HTML-entity pass over the result. The browser's HTML attribute parser
decodes the entities back to the JS-escaped characters before the handler
body is compiled as JS — that decode step is what makes the two-pass order
load-bearing. `escapeHtml` alone is not sufficient inside `on*=`: it does
not backslash-escape, so a raw `'` inside a JS string literal still
terminates the string early.

### The three-branch rule

For any interpolation into HTML, decide which of three positions it sits in
and apply the matching rule. Getting this wrong in either direction is a bug
— wrong-primitive is an XSS hole, and reversing the wrap is broken output
(the display bug D11 was written to catch: `escapeJsAttr` wrapping an
already-hand-escaped value renders `O'Brien` as `O\'Brien`).

1. **Inside a quoted JS string literal within an `on*=` attribute** →
   `escapeJsAttr`. This is the *only* context it is correct for.
2. **Text node or plain HTML attribute** → `escapeHtml`.
3. **Bare-expression position inside an `on*=` handler** → **neither**. If
   the value must be untrusted, validate/coerce its *type* (`parseInt`,
   an allowlist, `JSON.stringify` if the callee expects a JS literal and the
   whole result is then run through `escapeHtml` — see `app.js`'s
   `showEditMultiIntentConfigModal` call site for the worked example). Type
   coercion is a different problem from string escaping and is intentionally
   out of scope for either primitive.

## D15 — the six contexts where `escapeJsAttr` is wrong

`escapeJsAttr`'s two-pass design is sound for exactly one context (rule 1
above). Applying it anywhere else either does nothing useful or actively
reopens the hole. Two of these six are *actively* wrong, not merely
inapplicable — HTML-escaping a value that lands in a context which never
runs an HTML-decode pass leaves a literal entity reference sitting in
running code instead of being interpreted as data.

| Context | Why `escapeJsAttr` fails here |
|---|---|
| `href="javascript:..."` | The URL undergoes a separate decode stage; use a URL/scheme allowlist. |
| `style="..."` | CSS has its own escaping rules; entity-encoding a CSS value does not neutralize `expression()`/`url()` payloads. |
| **`<script>` body** | **Actively wrong.** A `<script>` element's text content is never HTML-decoded by the browser — `&#39;` appears in the running script literally, as five characters, not as `'`. |
| `srcdoc` | Treated as a nested HTML document, not an attribute value — a second decode stage, not a first one. |
| **`el.setAttribute('on...', value)`** | **Actively wrong.** No HTML parse occurs on a value set via `setAttribute` — the handler body is taken as literal JS source with no decode step, so an entity reference never resolves back to its character. |
| `eval(...)` / `new Function(...)` / `setTimeout(string)` | No HTML parse stage at all; these consume raw JS text directly. |

These six are measured and pinned (`scripts/check-handler-escaping.py --check
mis-context`), not characterised as clean — `href="javascript:` is 2 static
literals, `style=` is a set-pinned population of 10 sites (named member:
`icons.js:169`, a real, low-severity, ticketed attribute-injection surface —
see `## Out of scope` in the plan), the other four are at zero. The pin
exists because this population has been characterised wrong twice by two
different reviewers, in two different directions; the counts are
reproducible, the characterisations are not.

## The callee-sink rule: escaping the call site does not escape the callee

`escapeJsAttr` guarantees **byte-identical delivery** to whatever function
the handler calls. That is correct at the attribute layer — and it means a
callee that re-interpolates its parameter into an unescaped HTML sink
(`.innerHTML =`, `insertAdjacentHTML(`) becomes *more* reliably exploitable
after a call site is fixed, not less: the accidental mangling a bad hand-
rolled escape used to introduce is gone, and the attacker's string now
arrives exactly as sent.

Before converting or adding a handler call, check what the callee does with
its parameter. `scripts/check-callee-sinks.py` mechanizes this: for every
handler argument, it resolves the callee, the parameter index, and whether
that parameter reaches `.innerHTML =` / `insertAdjacentHTML(` unescaped
inside the callee body. Three buckets — `sink-escaped` (fine),
`sink-unescaped` (violation — fix the callee, not just the call site),
`unresolved` (violation until adjudicated in
`.callee-sink-adjudications.json`, which requires a human-written reason).
`unresolved` is fail-closed on purpose: a classifier that silently drops
what it cannot resolve reproduces the exact hole it exists to find.

**This is a documented one-hop limit, not an oversight.** The classifier
traces the handler argument's own textual identity through exactly one
callee body. It does not see a **second-order sink**: a callee that uses
its parameter to *fetch a different record* (an async call, a lookup by
key) and then renders **fields of that fetched record** — not the
parameter itself — into a sink. `app.js`'s `showCreateExternalApiKeyModal`
is the live example: `serviceName` (byte-preserving, correctly delivered)
is used only as a lookup key for `apiRequest(...)`; the four fields that
actually reach `.innerHTML =` (`service_name`, `api_name`, `endpoint_url`,
`description`) come from the fetched `payload`, which has no textual
derivation from `serviceName` the classifier could follow.

Extending the classifier to cover this would not be "two hops" of the same
mechanism — it would require tracing an async network response's *shape*
back through a backend API contract the classifier has no visibility into,
with no bounded termination condition (a third hop, a fourth, a value
round-tripped through `localStorage`). That is a materially different and
open-ended problem, not a deeper version of the one D9 solves. **Fix the
sink directly** (`escapeHtml()` on each rendered field of the fetched
record) and audit callees that fetch-then-render by hand; do not expect
`check-callee-sinks.py` to find this class.

## The builder rule (D9b)

`oss-profiles.js`'s `actionButton(label, className, onClick, tooltip)` is a
**handler-text builder**: its `onClick` parameter is interpolated raw into
`onclick="${onClick}"`. That parameter's contract is *executable JS source*,
not string data — `actionButton`'s own `onClick` argument is never escaped,
by design, and must never be. What changes is the **call sites**: any data
interpolated *into* the JS source string passed as `onClick` must be
escaped at the point it enters that string, exactly as if it were a direct
handler span (`escapeJsAttr` for quoted position, nothing for bare
position). `check-handler-escaping.py`'s builder pass finds `actionButton`'s
call sites and applies the identical classification a direct span would
get. The builder set itself is pinned by set-equality — a second builder
appearing anywhere in `admin/frontend/*.js` fails the gate on arrival rather
than silently escaping detection the way `actionButton` did for two plan
rounds before it was found.

If you write a new function whose job is to *build* a handler-attribute
string from parameters, treat the parameter that becomes the raw handler
body the same way: document at the definition that it is JS source, never
call `escapeHtml`/`escapeJsAttr` on it, and escape data into it at each call
site instead.

## Which of the three guards covers which threat (D13)

Consolidating every definition behind `escape-html.js` and freezing it
against overwrite closes three distinct threats, and each is covered by
exactly one mechanism — conflating them was round 2's mistake.

| Threat | Mechanism | Why |
|---|---|---|
| **Declaration drift** — a file tagged *before* `escape-html.js` adds its own `function escapeHtml` | `scripts/check-escape-html-uniqueness.py` (static, PR-time) | Under load-last, a classic script's global function declaration is silently overwritten by whatever loads later — there is no runtime event for a runtime check to catch. Static analysis is the only thing that ever sees the declaration. |
| **Runtime overwrite** — a console paste, a lazily-injected `<script>`, an extension, or any code executing *after* load that does `window.escapeHtml = something` | `Object.defineProperty(global, 'escapeHtml', { writable: false, configurable: false })` inside `escape-html.js`'s own IIFE | This is the one threat that is a genuine runtime mutation, not a load-order artifact — freezing converts a silent swap into a rejected assignment. |
| **Tag-order drift** — a new `<script>` tag is appended *after* `escape-html.js`'s | `scripts/check-frontend-escape-load-order.js` (primary — loads the real tag order and asserts `escape-html.js` is terminal), with the `defineProperty` freeze as a runtime backstop (a colliding declaration in that later file now throws instead of silently winning) | The one case that actually executes *after* the freeze, so both the static check and the runtime freeze fire on it. |

The `defineProperty` calls live **inside** the IIFE, using the lexically
bound `escapeHtml`/`escapeJsAttr` locals — never appended after the closing
`})(window);`. An unqualified reference out there resolves through the
global object record at call time, which is the exact late-binding hazard
the IIFE exists to eliminate in the first place.

## The delivery-path rule (D5)

**Disk content is not served bytes.** A file, a Dockerfile `COPY` line, an
`index.html` `<script>` tag, that tag's `?v=` cache-buster, and load order
are five separate links in the path from "the fix is correct in the repo"
to "a browser actually runs it." ATHENA-67 is the proof: the fix was
correct on disk, every disk-level gate passed, and it reached zero
warm-cache browsers for an hour, because `nginx.conf` sent
`Cache-Control: public, immutable` and the changed file's `?v=` had not
moved — a browser holding an `immutable` response does not revalidate even
on a user-initiated reload (RFC 8246).

Every one of the five links now has its own gate, and none of them
substitutes for another:

1. **File exists and is correct** — reviewed / tested directly.
2. **Dockerfile `COPY`s it** — `scripts/check-frontend-wiring.py --check three-way` (three-way `.js` ↔ `COPY` ↔ `<script>` tag parity; dangling tags are pinned to an exact set so a real gap can't hide inside a growing allowlist).
3. **`index.html` tags it** — same check.
4. **The tag's `?v=` moved** for any file this PR changes — `scripts/check-frontend-cache-busters.py --base <merge-base>`, three-valued exit (`0` clean, `1` stale buster, `2` could not run — a broken check is never read as a pass).
5. **Load order is correct** — `scripts/check-frontend-escape-load-order.js`, and `nginx.conf` no longer sends `immutable`, so even a missed bump is recoverable with a manual reload rather than unrecoverable for the cache's full TTL.

A green result on (1) proves nothing about (2)–(5). Gate all five.

## Reverse-order rollback constraint

Once Phase 3 landed, `escapeJsAttr` exists **only** in `escape-html.js`, and
the old `escapeHtmlAttr` name is gone. **Reverting ATHENA-67 in isolation,
after this campaign has landed, is a full admin-UI outage** — every call
site converted to `escapeJsAttr` throws `ReferenceError` inside template-
literal evaluation the moment `escape-html.js` disappears, the render
function aborts mid-expression, and the page section never gets an
`innerHTML` assignment at all. That is strictly worse than the XSS this
campaign closes.

**Rollback must be reverse-order**: revert Phase 6, then 5, then 4, then 3,
then ATHENA-67 — never ATHENA-67 alone. If you only need to roll back one
piece, roll back the *last* one that landed, not the first.

## Adding a new frontend file — checklist

1. Create `admin/frontend/<name>.js`.
2. Add `COPY <name>.js /usr/share/nginx/html/<name>.js` to the Dockerfile (or the equivalent renamed line if the deployed name differs — see `auth.js` → `admin-auth.js` for the existing precedent).
3. Add `<script src="/<name>.js?v=YYYYMMDD"></script>` to `index.html`.
4. **The tag must be placed *before* `escape-html.js`'s tag.** `escape-html.js` must remain the last local `<script>` — that is what makes it the terminal (and, after Phase 7, frozen) definition of `escapeHtml`/`escapeJsAttr`. A tag placed after it will throw at load if the new file happens to declare either name, and will otherwise just never get to observe the freeze.
5. If the file defines any escaping-shaped function (an entity-map, a DOM round-trip, anything under `escapeHtml`/`escapeJsAttr`-adjacent names), don't — import the canonical global instead. `scripts/check-escape-html-uniqueness.py` will catch a body-shape match under any name, but the point is not to need the catch.
6. If the file interpolates any value into an `on*=` handler, apply the three-branch rule above. `scripts/check-handler-escaping.py` runs on every PR touching `admin/frontend/**`.
