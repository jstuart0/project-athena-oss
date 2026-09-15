# Changelog

All notable changes to Project Athena are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

> **Plan:** `.mozart/plans/active/2026-09-14-deliver-athena-twilio-webhook-signature.md`
> **Ticket:** ATHENA-72
> **Commits:** `71d152d` (Phase 1), `253f9a1` (Phase 2 — merge and revert as one unit), `ae0396f` (drop the superseded str-mocked test)

### Twilio SMS webhook signature validation (ATHENA-72)

- **Fixed**: signature validation raised `RuntimeError: Stream consumed` (500) on every signed request when `TWILIO_AUTH_TOKEN` was set, and its re-parse dropped blank and repeated params. It now validates the parsed form.
- **Fixed**: the validation URL was Host-derived `http://…`, which Twilio's validator never matches behind a TLS-terminating proxy.
- **Added**: `TWILIO_WEBHOOK_BASE_URL`.
- **Changed**:
  - Token set with base unset or malformed → 503 plus an error log (per request and once at import).
  - Non-urlencoded → 403.
  - Logs carry `path`, not `url`.
- **Operator note**:
  - Enabling signature validation needs both variables.
  - The Admin UI "Auth Token" field isn't persisted (ATHENA-75).
  - Until a deployment sets both, the webhook still accepts unsigned requests; the post-merge action is ATHENA-76.

---

## [Unreleased]

> **Plan:** n/a — TINY tier; ATHENA-71's ticket body is the spec
> **Ticket:** [ATHENA-71](https://plane.xmojo.net)
> **Commits:** `4f0e265` (guard semantics), `12bd012` (fixture fixup), `459d6de` (lexical-detection docstring, exit 2 on git read failure)

### skip-guard fix: subset + tree-presence in place of set-equality (ATHENA-71)

- **Fixed**: `scripts/check-no-new-test-skips.py` (added by ATHENA-66) asserted set *equality* between skips added in `base..HEAD` and a pinned allowlist — true only while the originating campaign branch was unmerged. Once merged, every subsequent `base..HEAD` diff added no skips, and an empty added-set can never equal a non-empty pinned set, so the gate went permanently red (CI run 34845479251 on `8302271`). Now enforces two obligations independently: (a) skips added in `base..HEAD` must be a **subset** of the pinned allowlist (diff-scoped); (b) every pinned skip must still be **present in HEAD's tree** via `git ls-tree`/`git show`, checked regardless of `--base` (tree-scoped).
- **Fixed**: `.github/workflows/frontend-escaping.yml` — on `push`, the guard now diffs against `github.event.before` (falling back to `--base HEAD` for a branch's first push, whose `before` is the all-zeros SHA) instead of `origin/main`, so it verifies what the push actually introduced instead of a vacuous base-equals-HEAD comparison.
- **Fixed**: `check_tree_presence`'s `git show` failure on a path `git ls-tree` already confirmed exists at HEAD now exits 2 ("could not run"), distinct from exit 1 for a pinned skip genuinely missing from HEAD's tree.
- **Documented**: the script's docstring now states detection is lexical (substring match, no AST) — a ratchet against carelessness, not a control against a motivated bypass — and spells out how to retire a pinned skip: remove it from `SANCTIONED` in the same change that removes the skip.

---

## [Unreleased]

> **Plan:** `.mozart/plans/active/2026-09-13-deliver-athena-frontend-escaping.md` (round 3)
> **Ticket:** [ATHENA-66](https://plane.xmojo.net)
> **Commits:** `0468035`..`9d12d97`..`7411f6e` (Phase 1 — guards + baseline), `03af3df`..`15a09cd` (Phase 2 — callee/param/sink table), `087a47b`..`0df3ff6`..`2a48323` (Phase 3 — 52 wrong-primitive sites + callee-sink closures), `dd1b30d` (Phase 4 — `emerging-intents.js`), `045b6eb`..`e842149`..`9b328f6` (Phase 5 — 18 definitions deleted), `76384cd`, `1e5e284`, `bd23f40` (Phase 6 — 77 unescaped-quoted sites), `89b03ba`, `d8f4021`, `088ac1e`, `d06e33a`, `8c9245a` (Phase 7 — hardening, `immutable` removal, CI)

### admin-frontend escaping consolidation (ATHENA-66)

- **Added**: `admin/frontend/escape-html.js` is now the **only** file in `admin/frontend/` defining `escapeHtml`/`escapeJsAttr` — 20 definitions (7 closure-local, 2 entity-map, 11 DOM-round-trip) consolidated to 1, enforced by `scripts/check-escape-html-uniqueness.py` at absolute zero (name-matched forms, a body-shape scan for a hand-rolled entity map under any name, getter/`defineProperty`/computed-name forms — all path-scoped-excluding the canonical file itself).
- **Added**: `Object.defineProperty` freezes `window.escapeHtml`/`window.escapeJsAttr` against runtime overwrite (a console paste, a lazily-injected script) — the one threat static analysis can't see. Declaration drift and tag-order drift remain the uniqueness script's and the load-order harness's jobs respectively; three threats, three mechanisms, documented in `admin/frontend/README.md`.
- **Fixed**: 52 `on*=` handler sites (46 direct + 6 routed through `oss-profiles.js`'s `actionButton` builder) that called `escapeHtml` — the wrong primitive for a JS-string-literal position inside a handler attribute — now call `escapeJsAttr`, which escapes for the JS-string layer *and* the HTML-attribute layer in the correct order.
- **Fixed**: 77 `on*=` handler sites (63 direct + 14 builder-routed) with no escaping call at all now call `escapeJsAttr`. Includes **6** hand-rolled `.replace(/'/g, "\'")` escapes deleted (`app.js` ×3, `escalation.js` ×3, one of which landed in Phase 3 with its call site) in favor of `escapeJsAttr` (replace, never wrap — wrapping renders `O'Brien` as `O\'Brien`), and `app.js:3309`'s special case (`escapeHtml(JSON.stringify(config))`, delimiter switched from `'` to `"`) — a bare JS object-literal argument in the frontend's only single-quoted handler attribute, where `escapeJsAttr` would have produced a `SyntaxError`.
- **Fixed**: the 2 verified callee-sink defects (`app.js`'s `revealSecret`, `escalation.js`'s `showCloneEscalationPresetModal`) where a handler argument was delivered to a callee that re-rendered it into an unescaped `.innerHTML`. Mechanized via `scripts/check-callee-sinks.py` — a fail-closed classifier over every converted handler argument, with `unresolved` treated as a violation until adjudicated in `admin/frontend/.callee-sink-adjudications.json`.
- **Fixed**: 6 raw LLM-fed interpolations in `emerging-intents.js` (`display_name`, `canonical_name`, `description`) now `escapeHtml`-wrapped. Provenance: LLM output shaped by user input via prompt injection against the intent classifier, with no CSP backstop (`script-src 'unsafe-inline'`).
- **Fixed**: `app.js`'s `infoIcon` — a 21st in-tree hand-rolled entity-map implementation whose escaping was undone by a `getAttribute` read-back before reaching `innerHTML`. Escaping moved to the sink.
- **Changed**: `admin/frontend/nginx.conf` no longer sends `immutable` in `Cache-Control` (kept `public`, `expires 1h`) — `immutable` means a browser does not revalidate even on a user-initiated reload (RFC 8246), which is exactly what made ATHENA-67's hotfix unrecoverable for an hour on warm-cache clients.
- **Added**: `.github/workflows/frontend-escaping.yml` now runs the full absolute-zero gate set (handler escaping, callee sinks, uniqueness, wiring parity, hardening) plus the D10 wide-population ratchets (`data-attribute` ≤344, `innerhtml-sink` ≤442, `bare-expression` ≤169, and `emerging-intents.js` text-node ≤19) on every PR and on push to `main`.
- **Note**: the wide out-of-scope populations — 344 unescaped data-bearing plain-attribute interpolations (44 files), 442 `.innerHTML =` assignments (52 files) and 169 bare-expression handler interpolations — are ratcheted at their measured values, not fixed. Escaping in this frontend remains the exception, not the rule; tracked as a follow-up.
- **Note**: `apps/jarvis-web/` carries the same defect class (including a 22nd definition, unescaped OSM place names, and the same Lodgify `guest_name` reaching an end-user-facing chat surface) and is **not** covered by this campaign's guards. Tracked as ATHENA-68.

---

## [Unreleased]

> **Plan:** `.mozart/plans/active/2026-09-13-deliver-athena-frontend-escaping.md` (carved out of round 1)
> **Ticket:** [ATHENA-67](https://plane.xmojo.net)
> **Commits:** `a482635`, `ed8714f`

### admin-frontend guest-name XSS hotfix (ATHENA-67)

- **Fixed**: live reflected-XSS in `guest-context.js` and `memory-context.js` — a Lodgify-sourced guest name reached an `on*=` handler attribute unescaped. Introduced `admin/frontend/escape-html.js` (an IIFE, loaded as the **last** local `<script>` tag) so `window.escapeHtml`/`window.escapeJsAttr` resolve to its implementation regardless of the other files still declaring their own local copies — classic scripts, later tag wins.
- **Added**: `scripts/check-frontend-escape-load-order.js` — a Node `vm` harness that loads the real `index.html` tag order and asserts `escape-html.js` is the terminal definition.
- **Added**: `scripts/check-frontend-cache-busters.py` — a changed `.js` file whose `?v=` did not move never reaches a warm-cache browser; three-valued exit (`0` clean, `1` findings, `2` could-not-run). Bumped `guest-context.js` and `memory-context.js`'s busters so the fix in this release actually reaches clients that had already cached the vulnerable version.

> **Plan:** `.mozart/plans/active/2026-09-12-deliver-athena-dependency-remediation.md`
> **Ticket:** ATHENA-63
> **Commits:** `1657900` (Phase 1), `8bd26fd` (Phase 2), `8fb9b7e` (Phase 3), `689fd11`, `657ea56` (Phase 4), `93b2643` (Phase 5), `8c33829` (Phase 5 follow-up), `6814b6f`, `d9ff720`, `c84672c`, `e11e244`, `3e359ee`, `4df47b8` (Phase 6), `39daf6f`, `449bbfe`, `7b07a31` (Phase 7), `ccccd47` (Phase 8), `21a4b45`, `b3b9a9e`, `32df611` (Phase 9), `9f0dacd` (merge into the campaign branch), `5b765f5`, `95b4423` (documentation and changelog corrections)

### dependency remediation (ATHENA-63)

**Phase 1 — repair the verification harness:**

- **Fixed**: `scripts/smoke-rag-images.sh` — the RAG image smoke test CI already runs on every PR touching `src/rag/**`/`src/shared/**` (`.github/workflows/rag-smoke.yml`) now runs `pip check` after the import smoke, so an unmet or conflicting dependency in a shipped image fails the build, not just a missing/broken import.
- **Fixed**: `.github/workflows/rag-generator-drift.yml` — the Dockerfile-generator drift check ran under GitHub's default shell (`bash -e {0}`, no `pipefail`), so `--check | tee drift-summary.txt` always reported success regardless of the generator's own exit code. Added `shell: bash` so the step's exit status is the check's, not `tee`'s.
- **Fixed**: `scripts/generate-rag-dockerfiles.py` — strict `--check` now treats a missing RAG service directory or Dockerfile as drift; previously a deleted service Dockerfile silently passed as "no drift detected."
- **Added**: `scripts/smoke-images.sh` — generalizes the RAG-only image smoke harness to all 29 Python images in the repo (the 23 RAG images plus admin-backend, chat-embed, jarvis-web, gateway, mode-service, orchestrator). Supports `--list`, `--list-excluded`, `--service <name>`, `--dry-run`.
- **Added**: `scripts/lock-requirements.sh` — a single `uv pip compile` wrapper for every image's dependency lock, so no two locks can be produced by a slightly different invocation. Discovers and compiles every `requirements.in` (including a `requirements-test.in`) in dependency order. `--check` detects a `requirements.in` that was edited without recompiling its lock; `--upgrade` recompiles every lock against current upstream versions; `--upgrade-package NAME` (repeatable, added in Phase 7) scopes an upgrade to named packages only, since a plain recompile preserves an already-satisfied pin even after its ceiling lifts.
- **Added**: `scripts/check-build-tooling.py` — asserts the pinned build-tooling triplet (`pip==26.2.1 setuptools==84.0.0 wheel==0.48.0`) precedes every dependency install in a Dockerfile stage. `make check-build-tooling` was added in Phase 6 once the triplet had a repo-wide population to check; CI wiring is tracked in ATHENA-60.
- **Added**: `Makefile` targets `smoke-images`, `lock`, `lock-upgrade`, `lock-check` (developer conveniences — CI and other automation should call the underlying `scripts/*.sh` directly, since `make` collapses every non-zero recipe exit code to `2`).

**Phase 2 — close the unauthenticated upload exposure at jarvis-web's `POST /api/voice/transcribe`** (`apps/jarvis-web/backend/main.py`, no auth dependency, fully attacker-controlled request body). Three changes were needed to close it:

- **Security fix (version-independent)**: the endpoint now rejects a request whose declared `Content-Length` exceeds **25 MB** (`MAX_AUDIO_UPLOAD_BYTES`, env-overridable) before the multipart parser runs, and separately bounds both the form parse and the file read with a **30 s** timeout (`AUDIO_UPLOAD_READ_TIMEOUT_SECONDS`, env-overridable) that catches a request with no, or a dishonest, `Content-Length`. This closes the DoS class at this endpoint regardless of which multipart/HTTP library version is resolved here. Also fixed in the same change: `HTTPException`s raised inside `transcribe_audio` (the new `408`/`413`, and the pre-existing `400`/`502`) were being silently rewritten to a generic `503` by the function's own catch-all `except Exception`, since `HTTPException` is itself an `Exception` subclass.
- **Security fix**: `apps/jarvis-web/backend/requirements.{in,txt}` — `python-multipart` `0.0.20` → `>=0.0.32` (locked at `0.0.32`), clearing seven advisories affecting `0.0.20`: CVE-2026-24486 (arbitrary file write, fixed 0.0.22), CVE-2026-40347 (0.0.26), CVE-2026-42561 (0.0.27), CVE-2026-53537 (0.0.30), CVE-2026-53538 (0.0.30), CVE-2026-53539 (0.0.30), CVE-2026-53540 (0.0.31). CVE-2024-53981 (CVSS 8.7) was already patched at `0.0.20` (fixed in `0.0.18`) and is not one of the seven cleared here.
- **Security fix**: `apps/jarvis-web/backend/requirements.{in,txt}` — `fastapi` `0.115.6` → `>=0.141,<0.142` (locked at `0.141.1`), moving the transitively-resolved `starlette` from `0.41.3` to `1.6.0`. The `python-multipart` bump alone left the same unauthenticated endpoint DoS-able through Starlette's own multipart parser: seven distinct advisories affect `starlette==0.41.3` (CVE-2025-54121 fixed 0.47.2, CVE-2025-62727 fixed 0.49.1, CVE-2026-48710 fixed 1.0.1, CVE-2026-48817 fixed 1.1.0, CVE-2026-48818 fixed 1.1.0, CVE-2026-54282 fixed 1.3.0, CVE-2026-54283 fixed 1.3.1) — all seven clear at `starlette==1.6.0`. `fastapi==0.115.6` pinned `starlette<0.42.0,>=0.40.0`, so re-pinning starlette alone was not possible; `0.141.1` is the same target Phase 4 uses for admin-backend.
- **Added**: `apps/jarvis-web/backend/requirements.in` — `requirements.txt` converted from a hand-pinned file to a generated, hashed, `x86_64-unknown-linux-gnu`-platform lock. Only `python-multipart` and `fastapi` (and its transitive `starlette`) moved; `httpx==0.28.1`, `pydantic==2.10.3`, `uvicorn==0.34.0`, `websockets==12.0` unchanged.
- **Fixed**: `apps/jarvis-web/Dockerfile` — pinned build tooling (`pip==26.2.1 setuptools==84.0.0 wheel==0.48.0`) added to the `AS builder` stage, above the line that installs the hashed lock.
- **Fixed**: `apps/jarvis-web/Dockerfile`'s production stage now removes its own factory-installed `pip`/`setuptools`/`wheel` before `COPY --from=builder` copies the pinned versions over — Docker's `COPY` onto an existing directory merges rather than replaces, so without this the base image's `pip==24.0`/`setuptools==79.0.1` (with their own CVEs, including CVE-2026-59890) survived on disk alongside the pinned 26.2.1/84.0.0, and `importlib.metadata`-based tooling resolved the stale, vulnerable one. Verified after the fix: exactly one dist-info per tool, `pip-audit` reports zero vulnerabilities in the finished image. `--no-build-isolation` intentionally **not** added here — this Dockerfile installs no editable package.
- **Changed**: `README.md` — the two jarvis-web dev-install commands (`:235`, `:347`) repointed from `requirements.txt` to `requirements.in`. Not a fix for a failure: installing the generated `requirements.txt` directly on Apple Silicon succeeds, by silently resolving macOS wheels instead of the x86_64 binaries the image ships — a reproducibility gap, not a break.
- **Documented, not fixed**: `apps/jarvis-web/backend/main.py:115-122` — `allow_origins=["*"]` + `allow_credentials=True` makes Starlette reflect the request `Origin` header verbatim, defeating same-origin credential protection; most of this app's routes (`/api/welcome`, `/api/climate`, `/api/sensors/*`, `/api/chat`) need no auth at all, so any origin's JavaScript can already read guest PII, HVAC state, and occupancy data regardless of credentials. Tracked in **[ATHENA-64](https://plane.xmojo.net)**; fixing it requires enumerating every legitimate origin that embeds this app, a separate consumer audit.
- **Ticketed, not fixed**: `apps/jarvis-web/Dockerfile:41-42` copies the builder stage's entire `site-packages` and `/usr/local/bin` into the production image, so the runtime image ships a working `pip` and its entrypoints — a post-RCE hardening gap. Tracked in **[ATHENA-65](https://plane.xmojo.net)**.

**Phase 3 — canonicalize the shared dependency declaration:**

- **Fixed**: deleted `src/shared/requirements.txt` — a duplicate of `src/shared/pyproject.toml`'s `dependencies` array with zero consumers anywhere in the tree outside the ATHENA-63 planning documents. `pyproject.toml` is now the sole source of shared's dependency spec.
- **Changed**: relocated the `httpx~=0.28` upgrade warning (the private `_pool` API dependency in `url_safety.py`'s `_build_pinned_transport`) from the deleted flat file to a comment directly above the pin in `pyproject.toml`, and recorded the same contract in `CONTRIBUTING.md`'s SSRF guard section.

**Phase 4 — resolve the admin-backend dependency graph, and commit the lock that was tested:**

- **Fixed**: `admin/backend/requirements.in` — four pin changes take the environment from unsatisfiable (`ollama>=0.4.9` needs `pydantic>=2.9`, the spec pinned `pydantic==2.5.0`) to satisfiable, and the admin-backend test suite from 203 errors to 0: `fastapi==0.104.1` → `>=0.141,<0.142` (resolves `0.141.1`), `pydantic==2.5.0` → `>=2.13,<3` (resolves `2.13.5`, forced transitively by `ollama`), `authlib==1.3.0` → `>=1.8,<2` (resolves `1.8.0`; `parse_id_token` gained `leeway=` in 1.8, closing 10 `TypeError` failures in `test_oidc_validation.py`), `python-multipart==0.0.6` → `>=0.0.32` (resolves `0.0.32`, closes the `TWILIO_AUTH_TOKEN`-gated admin-backend multipart exposure). `httpx~=0.28` deliberately untouched.
- **Added**: `admin/backend/requirements.in`/`requirements.txt` — `requirements.txt` renamed to `.in` and compiled to a generated, hashed, `x86_64-unknown-linux-gnu` lock. Verification installs from this committed lock, not a scratch file, so a later resolve can't land an untested FastAPI/starlette pair.
- **Added**: `admin/backend/requirements-test.in`/`requirements-test.txt` — a locked test environment (`pytest==9.1.1`, `pytest-asyncio==1.4.0`) compiled with `-c requirements.txt`, so no test dependency can resolve a different version of anything the production lock already pins.
- **Changed**: FastAPI 0.141.1 is stricter about request bodies than 0.104.1: a non-empty request body sent with no `Content-Type` header now returns `422` (a request with no body, or any `Content-Type` value, is unaffected); `Bearer` credentials are also whitespace-stripped before comparison. All internal outbound POST callers of admin-backend (`src/shared/admin_config.py`, `src/orchestrator/main.py`) already use `json=` or target endpoints with no JSON body parameter, so none is affected. An external caller using the `X-API-Key` path that posts with `data=json.dumps(...)` and no explicit header must switch to `json=` or set the header itself.
- **Disclosed**: the resolved lock carries `starlette==0.52.1`, which still has 5 distinct CVEs — CVE-2026-48710, CVE-2026-48817, CVE-2026-48818 (Windows-only, not applicable to this deployment), CVE-2026-54282, CVE-2026-54283 — with fix floors `1.0.1`–`1.3.1` that require starlette's `1.x` major, delivered in Phase 7. This is a strict reduction from `main`'s pre-remediation `starlette==0.27.0` (7 distinct CVEs: the above 5 plus CVE-2024-47874 and CVE-2025-54121, both already fixed at 0.52.1) — Phase 4 introduced no new advisory. `admin/backend/app/routes/sms_webhook.py:48,56` reconstructs `str(request.url)` for Twilio HMAC verification; `main.py` registers no `TrustedHostMiddleware`.
- **Known limitation**: `test_success_path_not_artificially_delayed` (`admin/backend/tests/test_security_hardening.py`) is a wall-clock assertion (`elapsed_ms < 1500`) that fails only when the test suite runs under amd64 emulation on non-x86 hardware (measured about 4.2–6.4 s under amd64 emulation, ~67× slower than native); it passes natively on arm64. Pre-existing; the admin-backend suite is otherwise clean across Phases 4, 6, and 7 (0 errors, 0 unexplained failures).

**Phase 5 — fix 4 order-dependent test failures (test files only, no skips, xfails, or deletions):**

- **Fixed**: `admin/backend/tests/test_security_hardening.py::TestTwilioSignatureValidation` — three tests used `asyncio.get_event_loop().run_until_complete(...)` instead of `asyncio.run(...)`, the only async calls in the suite doing so; `asyncio.run` closes the loop after each call, stranding these three once every other async test had moved to it.
- **Fixed**: `admin/backend/tests/test_phase2_consolidation.py::TestPhase2ReconcileRegressions::test_integrations_healthy_path_uses_rag_service_host` — a dotted-string `monkeypatch.setattr` target could fail depending on test collection order, because a different test file's module-eviction fixture doesn't always leave `app.routes` re-attached to a freshly re-imported `app`. Fixed by importing `app.routes.integrations` inside the test function, immediately before the `monkeypatch.setattr` call, so the binding is always current relative to whatever module tree is live at that point.

**Phase 6 — clear the non-major CVE surface, pin build tooling across the remaining images, and harden the CVE-allowlist guard:**

- **Fixed**: `admin/backend/requirements.in` — three bumps clear the two remaining auth-path advisories: `aiohttp==3.9.1` → `>=3.14.3,<4` (resolves `3.14.3`), `python-jose[cryptography]==3.3.0` → `>=3.5,<4` (resolves `3.5.0`), `python-dotenv==1.0.0` → `>=1.2.3,<2` (resolves `1.2.3`). `starsessions`, `starlette`, and `httpx` deliberately untouched — the starlette major is Phase 7's alone.
- **Fixed**: build tooling (`pip==26.2.1 setuptools==84.0.0 wheel==0.48.0`) pinned ahead of every dependency install across the remaining 28 Python images — the 5 hand-written Dockerfiles that lacked the full triplet (`admin/backend`, `apps/chat-embed`, `src/gateway`, `src/mode_service`, `src/orchestrator`) and the RAG Dockerfile generator template, regenerated across all 23 RAG service Dockerfiles. `apps/jarvis-web/Dockerfile` already carried this pin from Phase 2. `setuptools==84.0.0` ships zero `pkg_resources/` entries (a real removal, fixing CVE-2026-59890 whose floor is `83.0.0`; no downgrade below that floor is possible without reintroducing it). A direct `import pkg_resources` was checked on seven images: `athena-gateway`, `athena-mode-service`, `athena-orchestrator`, `athena-chat-embed`, `athena-admin-backend`, `athena-rag-sitescraper`, `athena-rag-weather`. It's absent in all seven, and none of them needs it. `athena-orchestrator` and `athena-rag-sitescraper` were additionally checked with unguarded imports of the optional content-fetcher packages (`trafilatura`, `extruct`, `pandas`, `playwright` in `src/shared/content_fetcher.py`) they actually ship.
- **Added**: `scripts/audit-images.sh` + `make audit-images` (`SCOPE=remediated` scopes to `athena-admin-backend` and `athena-jarvis-web`). Builds each image (`docker buildx build --platform linux/amd64`), freezes its installed package list from its own Python (`pip freeze --all --exclude-editable`), then audits that frozen list from a throwaway venv that never touches the built image, so the reported advisory surface reflects what the pinned build tooling and committed lock actually produce. `pip-audit`'s exit code is captured to a sidecar file rather than swallowed; only rc 0 (clean) or rc 1 (findings) with parseable JSON counts as a result, anything else is `TOOL_ERROR` (exit 2). Carries exactly one `--ignore-vuln`: `PYSEC-2026-1325` (`ecdsa`, transitive via `python-jose[cryptography]`), unreachable under this codebase's HS256-only JWT usage — `admin/backend/app/auth/oidc.py` calls `jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])` with `JWT_ALGORITHM = "HS256"` at both call sites — gated on `scripts/check-jwt-algorithm-guard.py` passing first; a guard failure withholds the allowlist for that run.
- **Added**: `scripts/check-jwt-algorithm-guard.py` — an AST-based check (immune to a match inside a comment or string) that resolves every python-jose import form (`import jose`, `import jose.jwt`, `from jose import jwt`/`jws`, `from jose.jwt import decode/encode`, `from jose.jws import verify/sign`, and attribute chains built on any of them) to a canonical name, then verifies `JWT_ALGORITHM == "HS256"` (the single top-level literal in `oidc.py`, with no other rebinding permitted), that every `jwt.decode`/`jws.verify` call site under `admin/backend/app` (excluding tests) and `src/shared` passes `algorithms=` naming only `HS256`/`JWT_ALGORITHM`, and that every `jwt.encode`/`jws.sign` call passes `algorithm="HS256"` or `JWT_ALGORITHM`. A non-UTF-8 file or other tool failure exits 2 rather than passing silently. Covered by `tests/unit/test_jwt_algorithm_guard.py` (16 cases). **Known limitation**: dynamic-dispatch forms — an intermediate-variable alias (`x = jwt; x.decode(...)`), a star import, `importlib.import_module`, or `setattr` on `JWT_ALGORITHM` — are not detected; the current tree is clean of all of these, but the guard cannot prove it stays that way. Tracked as **[ATHENA-79](https://plane.xmojo.net)**.
- **Fixed**: `scripts/audit-images.sh`, `scripts/smoke-images.sh`, `scripts/smoke-rag-images.sh`, and `scripts/lock-requirements.sh` each had at least one `set -u` empty-array expansion (e.g. any image with no shared-copy step) that aborted or silently swallowed the script's real exit status under macOS's stock bash 3.2. Fixed to the `"${arr[@]+"${arr[@]}"}"` form; verified directly under bash 3.2.57.
- **Documented (operator note)**: python-jose 3.5 rejects a `JWT_SECRET`/`SESSION_SECRET_KEY` that contains a PEM header or SSH key-type substring, mistaking it for asymmetric key material — `jwt.encode` raises `JWSError`, `jwt.decode` raises `JWKError`, and neither subclasses `JWTError`, so both escape `oidc.py`'s `except JWTError` handling: login and every authenticated request return `500`, not `401`. Generate these secrets as plain random strings (e.g. `openssl rand -base64 32`, per `docs/CONFIGURATION.md`), never as a copy-pasted key file.
- **Audit result**: `bash scripts/audit-images.sh --scope remediated` exits 1 (findings), not 0, at the end of Phase 6. `athena-jarvis-web` is clean. `athena-admin-backend` carries 5 distinct unallowlisted advisories on `starlette==0.52.1` (`PYSEC-2026-161`, `PYSEC-2026-248`, `PYSEC-2026-249`, `PYSEC-2026-2280`, `PYSEC-2026-2281`) — the same 5 CVEs disclosed above under Phase 4. Only Phase 7's starlette major clears them; no second `--ignore-vuln` was added for them.

**Phase 7 — the starlette major:**

- **Fixed**: `admin/backend/requirements.in` — `starsessions[redis]==2.1.3` (which requires `starlette>=0,<1`, and was the sole gate blocking the starlette major) → `>=2.2.1,<3`. Recompiled the lock scoped to exactly the two packages this lifts: `starlette` `0.52.1` → `1.6.0`, `starsessions` `2.1.3` → `2.2.1`. Nothing else in the lock moved. Clears all 5 residual starlette advisories from Phase 4/6: `bash scripts/audit-images.sh --scope remediated` now reports both remediated images clean besides the one allowlisted `PYSEC-2026-1325` residual.
- **Verified**: `starsessions==2.2.1`'s `SessionMiddleware.__init__` signature, `RedisStore(connection=, prefix=)`, and `InMemoryStore()` are unchanged from `2.1.3`; `request.session` behaves identically under `starlette==1.6.0`. No changes needed to `admin/backend/main.py`'s or `app/routes/local_auth.py`'s session-middleware usage. The admin-backend test suite is unchanged from Phase 6 (same one pre-existing wall-clock flake under emulation, see Phase 4; every session/middleware/cookie/OIDC/WebSocket test passes).
- **Pending, blocking for merge (not for this commit)**: manual confirmation in a browser that the session cookie set by `/api/auth/login` carries `Secure` and `SameSite=Lax`, that `/api/auth/logout` clears it, and that a second, independent browser session is unaffected by the first one's logout.
- **Fixed**: `scripts/lock-requirements.sh`'s `--upgrade-package`, `--input`, `--output`, and `--constraint` each crashed with an "unbound variable" error when their value was missing, or silently consumed the next flag as their own value. Each now fails cleanly (`FAIL: <flag> requires a value`, non-zero exit); a following token starting with `--` is treated as a missing value too. `--check` combined with `--upgrade`/`--upgrade-package`, in either order, now fails (`FAIL: --check cannot be combined with --upgrade/--upgrade-package`) instead of letting a verification run mutate pins.
- **Ticketed, not fixed**: `regenerate_session_id()` is never called on login (`admin/backend/app/routes/local_auth.py:167-169`, `main.py:817-818,888-889`) — a pre-existing session-fixation gap, unrelated to and unaffected by this phase's library bump. Tracked as **[ATHENA-80](https://plane.xmojo.net)**.
- **Revert note**: Phase 7 ships as three commits — `39daf6f`, `449bbfe`, `7b07a31`. To undo it as a group: revert `7b07a31`, then `449bbfe`, then `39daf6f`, in that order. Only `CHANGELOG.md` conflicts (resolvable by restoring the pre-Phase-7 text); reverting `39daf6f` before `7b07a31` instead conflicts in `scripts/lock-requirements.sh`. The result restores `admin/backend/requirements.in` to `starsessions[redis]==2.1.3`, both admin-backend locks (`requirements.txt`, `requirements-test.txt`) to `starlette==0.52.1`/`starsessions==2.1.3`, and `scripts/lock-requirements.sh` to its pre-Phase-7 form. It changes no other image's lock or Dockerfile.

**Phase 8 — lock the remaining 27 images:**

- **Added**: the remaining 27 image-mapped `requirements.txt` files (23 RAG services, `apps/chat-embed`, `src/gateway`, `src/mode_service`, `src/orchestrator`) converted to `requirements.in`, then compiled into generated, hashed, `x86_64-unknown-linux-gnu`-platform locks. Every image-mapped dependency spec in the repo is now a compiled lock (29 total, alongside `admin/backend` and `apps/jarvis-web/backend` from earlier phases). Resolved versions match a fresh install today across all 27: `fastapi==0.141.1`, `pydantic==2.13.5`, `httpx==0.28.1`, `starlette==1.6.0`, `uvicorn==0.53.0` — locking freezes current behavior, since every non-admin spec floors with `>=` rather than pinning.
- **Added**: a "deliberately unlocked" header on the three requirements files that intentionally stay unlocked, each naming the target platform and why an `x86_64-Linux` lock would be wrong for it: `./requirements.txt` (a developer's own machine), `src/control_agent/requirements.txt` (an arm64 macOS host), `src/jetson/requirements.txt` (an aarch64 Jetson edge device — `torch` ships Jetson-specific wheels from NVIDIA's own index, not PyPI's x86_64 build).
- **Added**: `.gitattributes`, marking all 29 image-mapped locks plus `admin/backend/requirements-test.txt` as `linguist-generated` (kept out of human review diffs).
- **Fixed**: `CONTRIBUTING.md`'s RAG-service checklist named `requirements.txt` (now a generated lock) as the file to hand-edit — corrected to `requirements.in` plus `make lock`. A new test-only dependency belongs in `admin/backend/requirements-test.in`, with `pytest-httpserver` documented as the one grandfathered exception still living in the production spec.
- **Disclosed**: an image built from Phase 8 alone still installs `src/shared`'s dependencies through an unhashed editable install (`pip install -e /app/shared`) that runs before the hashed lock — unconstrained, and able to resolve any version satisfying `src/shared/pyproject.toml`'s own floors independent of what the lock pins. Closed in Phase 9.

**Phase 9 — the image consumes exactly the lock:**

- **Fixed**: every shared install across all 27 shared-installing images (23 RAG services, `admin/backend`, `src/gateway`, `src/mode_service`, `src/orchestrator`) changed from `pip install --no-cache-dir -e /app/shared` to `pip install --no-cache-dir --no-deps --no-build-isolation -e /app/shared`. `--no-deps` makes the hashed lock — installed in the same stage immediately after — the single source of every installed package version. `--no-build-isolation` closes a second gap: `src/shared/pyproject.toml`'s own build-backend floor (`setuptools>=61.0`, unpinned `wheel`) would otherwise resolve fresh over the network inside an isolated build environment, bypassing the pinned triplet entirely. All 9 of `src/shared/pyproject.toml`'s dependencies are pinned with `==` in all 27 shared-installing locks. The 23 RAG Dockerfiles were regenerated from the generator template (never hand-edited); the 4 hand-written Dockerfiles (`admin/backend`, `src/gateway`, `src/mode_service`, `src/orchestrator`) were edited directly. `apps/chat-embed` and `apps/jarvis-web` install no shared module and are unchanged.
- **Known limitation**: the four hand-written Dockerfiles' `--no-deps`/`--no-build-isolation` line and the 29 `.in`-to-`.txt` lock syncs have no CI gate — enforcement today is local (`make lock-check`, `scripts/check-build-tooling.py`, `scripts/generate-rag-dockerfiles.py --check`, and `scripts/smoke-images.sh`). The lock content itself is still consumed on every image build regardless, and the 23 generated RAG Dockerfiles keep their drift gate (the generator `--check`). Tracked as **[ATHENA-81](https://plane.xmojo.net)**.
- **Known limitation**: the `pip`/`setuptools`/`wheel` bootstrap triplet is pinned by version across all 29 images, not by hash — a supply-chain gap one level below the hashed dependency locks. Tracked as **[ATHENA-82](https://plane.xmojo.net)**.
- **Documented**: the orchestrator lock's `httpx2`, `httpcore2`, and `langchain-protocol` entries are legitimate upstream dependencies, not typosquats — `httpx2`/`httpcore2` are pulled in by `langsmith` and by starlette 1.6's own `TestClient`; `langchain-protocol` is pulled in by `langchain-core` and `langgraph-sdk`.
- **Fixed**: `scripts/audit-images.sh`'s isolated pip-audit venv was created at `/audit-venv`, a path requiring write access to the container filesystem root — 26 of 29 images set a non-root `USER athena`, so the audit could not complete on any of them. Moved the venv into a `mktemp`-created directory under `/tmp`, which every base image in this repo makes world-writable.
- **Corrected**: `CONTRIBUTING.md`'s httpx version contract now states the current mechanism: httpx is pinned exactly (`httpx==0.28.1`) in every one of the 29 generated locks; for the 27 images compiled against `src/shared/pyproject.toml`, that pin is fixed at lock time by `scripts/lock-requirements.sh`, not at image-install time (the shared install now installs zero dependencies); changing it means editing the `src/shared/pyproject.toml` constraint and running `make lock`.

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-15-deliver-auth-deferred-hardening.md` (r2)
> **Ticket:** [ATHENA-55](https://plane.xmojo.net)
> **Commits:** `179fd8c` (Phase 1), `77f50d6` (Phase 2), `f956be2` (Phase 3), `9b2926e` (Phase 4)

### auth-deferred-hardening (ATHENA-55)

- **Added**: `OIDC_VALIDATE_ISS` env flag (default `true`, opt-out). When set to `false`, the issuer-**mismatch** startup gate (branch (d) in `_enforce_oidc_runtime_gates`) is softened from `SystemExit` to `logger.warning`, and authlib's own `iss` check is relaxed via `claims_options={"iss": {"essential": False}}` on the OIDC callback at `main.py:815`. **Scope is deliberately narrow**: the flag relaxes issuer *mismatch* only — it does **not** let the service boot with an empty/placeholder issuer, an unreachable IdP, or a discovery doc missing the `issuer` field (those branches remain fatal `SystemExit` regardless of the flag). Startup emits `oidc_iss_validation_disabled` warning when the flag is false; flag state is exposed on `GET /api/auth/methods` as `"oidc_iss_validation"` for runtime observability. **Audience validation note**: authlib (which this codebase uses for the OIDC callback) validates the `aud` claim via `IDToken.validate_azp` against the registered `OIDC_CLIENT_ID`; `OIDC_VALIDATE_ISS=false` has no effect on audience validation.

- **Added**: `POST /api/auth/ws-ticket` — authenticated, rate-limited endpoint that mints a short-lived (45 s) single-use WS ticket. Ticket claims: `{user_id, ws_ticket: true, aud: "ws", jti: <uuid4>, exp: now+45s}`. Authentication via `Depends(get_current_user)` (not a session read). Rate-limited via `Depends(login_rate_limit_dep)`. Response shape: `{"ticket": "<jwt>", "ws_ticket_supported": true}`. The `ws_ticket_supported` marker is the capability signal the frontend uses; an old backend without this endpoint returns 404.

- **Changed**: `admin/backend/app/routes/websocket.py` — WS handler now accepts **both** a `ws_ticket` JWT (audience-scoped, single-use via Redis) and a legacy session JWT (`?token=`) during the one-release deprecation window. Ticket path: `oidc.decode_ws_ticket(token)` (validates `aud="ws"` positively via PyJWT `audience="ws"`) → requires `payload.get("ws_ticket") is True` (identity, not truthy) → single-use claim via `athena:ws_ticket:<jti>` Redis key (DEV_MODE: in-memory set). Legacy path: falls through to `decode_access_token` on `InvalidAudienceError`; logs `websocket_legacy_token_auth_deprecated`. Origin checked against `CORS_ORIGINS` at upgrade time; mismatch → close 4003. `JWT_SECRET`/`JWT_ALGORITHM` consolidated to the `oidc` module (no second `os.getenv` read in `websocket.py`). Token fragment logging removed.

- **Changed**: `admin/backend/app/utils/oidc.py` — `decode_access_token` / `get_current_user` now reject any token carrying `aud == "ws"` or `ws_ticket is True` with HTTP 401 (WS ticket is not a valid REST credential — lateral-path closure). New `decode_ws_ticket(token)` helper validates `aud="ws"` positively, keeping one source of truth for the JWT secret.

- **Changed**: `admin/frontend/admin-jarvis.js` — `connectWebSocket()` now:
  - POSTs `/api/auth/ws-ticket` (Bearer session token) and uses the returned ticket in the WS URL; fresh mint on every (re)connect — no cached URL.
  - **Mint-time fallback** (404 only): old backend with no ticket endpoint → legacy session JWT in `?token=`. Status 401/403/5xx → fail loudly, no fallback.
  - **Upgrade-time capability fallback**: WS close 4001 + `ticketMintStatus === 200` + `!legacyRetried` → one retry with legacy session JWT (mixed-rollout gap where a new pod mints the ticket but an old pod handles the WS upgrade and rejects `aud="ws"`). 4003, second 4001, or mint status ≠ 200 → fail loudly. `legacyRetried` resets per `connectWebSocket()` invocation.
  - `console.log` logs host+path only (never `?token=` query string) — xander C-2.

- **Operational note — deploy ordering (ATHENA-55)**: the admin **backend** (`athena-admin-backend`, `replicas: 2`, RollingUpdate) must be fully rolled out to the ticket-aware image **before** the new admin-frontend image is promoted. Both deployments roll independently with no `sessionAffinity`; the capability fallback above covers the residual window (e.g. a backend pod restart mid-frontend-rollout). Gate: `kubectl rollout status deployment/athena-admin-backend -n athena-prod`.

- **Security review follow-ups (xander mid-build, same campaign):**
  - **(M-1) Origin policy documented**: absent `Origin` header (non-browser clients — curl, scripts, monitors) is now explicitly **allowed** on WS upgrade. Non-browser clients have no CSRF surface; the Origin check defends against browser-based cross-origin upgrades only. Only a PRESENT-but-mismatched Origin closes 4003. A startup log line (`websocket_origin_absent_allowed`) confirms the policy at runtime. Tests: `TestWsOriginPolicy`.
  - **(M-2) Dead variable removed**: `aud_mismatch` local variable in the WS handler was assigned but never read; removed. Added an explanatory comment on the legacy fallthrough condition clarifying it is entered on ANY `JWTClaimsError` / decode failure, not only `aud` mismatch, and why the fallthrough is safe (legacy `decode_access_token` independently rejects `aud="ws"` tokens).
  - **(L-1) Replay guard test**: static ordering assertion confirms `_claim_jti` replay-rejection (close 4001 + return) appears before the legacy fallthrough path in source, proving a replayed ticket can never be laundered through `decode_access_token`. Unit test also verifies `_claim_jti` returns `False` on second call. Tests: `TestWsReplayGuardL1`.
  - **(L-2) Audience list-form regression pin**: token with `aud=["ws","api"]` (JSON array) as REST Bearer → 401. python-jose may deserialize list-form `aud` back to a list; the `ws_ticket=True` discriminator check is the primary guard in that case. Pinned explicitly. Tests: `TestWsAudienceListFormL2`.

- **Follow-up (Phase 6, next release — out of scope)**: remove legacy `?token=` session-JWT acceptance from `websocket.py` and the frontend 404-fallback. Tracked as a follow-up; not built here.

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/active/2026-06-12-deliver-rbac-ssrf-partial.md` (r4)
> **Ticket:** ATHENA-59 (Phase 0 — shared SSRF guard)

### SSRF guard: shared safe_request helper wired into user/admin URL fetch chokepoints (ATHENA-59 Phase 0)

- **Added** (`ATHENA-59`): `src/shared/url_safety.py` — canonical SSRF guard module. Exports `validate_url_not_private` (sync, never-raises, never-reads-env), `safe_request`/`safe_get`/`safe_post` (async, IP-pinned transport, per-hop revalidation, ~10 MB cap), `SsrfBlockedError`, `UrlSafetyResult` (frozen dataclass). DNS-rebinding mitigated via `_PinnedNetworkBackend` that dials the validated IP while httpcore's TLS layer preserves SNI hostname. POST 301/302/303 redirects downgrade to GET and strip body/credential headers on cross-origin hops; POST 307/308 redirects are refused.
- **Changed** (`ATHENA-59`): `src/rag/site_scraper/main.py` — `is_url_allowed` now performs literal-IP detection and domain allow/block matching (`_domain_matches`); it does NOT call `validate_url_not_private` (DNS-resolving guard lives in the async fetch path via `safe_get` to avoid blocking the event loop). `blocked_domains` and `allowed_domains` substring matching replaced with exact-host/suffix matching (`_domain_matches`) to close attacker bypass via `evil.com.attacker.net` (xander H-1).
- **Changed** (`ATHENA-59`): `admin/backend/app/routes/calendar_sources.py` — `fetch_ical_data` now uses `safe_get(allowed_schemes=frozenset({"https"}))` — enforces HTTPS on every redirect hop.
- **Changed** (`ATHENA-59`): `src/shared/content_fetcher.py` — default client changed to `follow_redirects=False`; all HTTP fetches on user-supplied URLs route through `safe_get`; Playwright paths gated behind `CONTENT_FETCHER_ALLOW_BROWSER_FETCH` (default `false`).
- **Changed** (`ATHENA-59`): `admin/backend/app/routes/tool_calling.py` — MCP discovery POST for Class-1 sources (request body / feature-flag DB rows) replaced with `safe_post`; Class-3 (N8N_MCP_URL env) kept exempt.
- **Changed** (`ATHENA-59`): `src/shared/tool_registry.py` — MCP POST for Class-1 feature-flag rows replaced with `safe_post`; Class-3 (N8N_MCP_URL env) kept exempt.
- **Changed** (`ATHENA-59`): `admin/backend/app/services/health_poller.py` — `_validate_service_url` promoted to `async def`; local `_PRIVATE_NETS` duplicate retired; IPv6 bare addresses bracketed before URL construction; delegates to shared `validate_url_not_private`.
- **Changed** (`ATHENA-59`): `admin/backend/app/routes/services.py` — all 5 `aiohttp.ClientSession.get()` calls in connector health checks set `allow_redirects=False`; missing `await` on Redis SSRF guard call fixed.
- **Changed** (`ATHENA-59`): `admin/backend/app/routes/rag_connectors.py` — all 6 `session.get()` calls set `allow_redirects=False`.
- **Changed** (`ATHENA-59`): `admin/backend/app/routes/music_config.py` — `HA_URL` hardcoded fallback `http://192.168.10.168:8123` removed; empty default with startup log warning (OSS-First rule).
- **Added** (`ATHENA-59`): `src/shared/config.py` — `sitescraper_allowed_private_hosts` (str, default `""`), `content_fetcher_allow_browser_fetch` (bool, default `false`).
- **Added** (`ATHENA-59`): `tests/unit/test_url_safety.py` — 69 tests covering all SSRF guard contracts, IP-pinning PoC, POST redirect semantics, safe_request/safe_get/safe_post wrappers.
- **Fixed** (`ATHENA-59`): Health poller `TestSSRFGuard` tests updated to `asyncio.run()` for async `_validate_service_url`; `test_phase4_reconcile.py` and `test_codex_r2_reconcile.py` updated similarly.
- **Fixed** (`ATHENA-59`): Streaming size cap in `safe_request` now iterates `aiter_bytes()` and aborts before buffering the full body (H-1 gate fix — previous `aread()`-then-check defeated the cap).
- **Fixed** (`ATHENA-59`): `discover_mcp_tools` reads `N8N_MCP_URL` env exactly once into a local before URL resolution and Class-1/Class-3 classification to prevent mismatch (H-3 gate fix).
- **Fixed** (`ATHENA-59`): `_domain_matches` in `site_scraper/main.py` strips trailing dots on both domain and pattern (M-3 gate fix — trailing dot previously bypassed exact-match).
- **Fixed** (`ATHENA-59`): `is_url_allowed` in `site_scraper/main.py` no longer calls blocking `socket.getaddrinfo` synchronously from async handlers; DNS-resolving guard lives only in the async `safe_get` fetch path (M-1 gate fix).
- **Fixed** (`ATHENA-59`): `/health` endpoint `browser_rendering` field now reflects `HAS_PLAYWRIGHT AND CONTENT_FETCHER_ALLOW_BROWSER_FETCH` gate (IAN-9).
- **Added** (`ATHENA-59`): TLS SNI PoC tests prove `_PinnedNetworkBackend` dials the pinned IP and the httpcore pool uses the original hostname for SNI (H-2 gate fix).
- **Added** (`ATHENA-59`): IPv6 ULA URL-form parametrized tests (`http://[fc00::1]/`, `http://[fd00::1]/`) confirm ULA addresses are blocked (M-4 gate fix).
- **Added** (`ATHENA-59`): Tool registry localhost:5678 default fail-closed test — asserts `SsrfBlockedError` is caught, `_mcp_tools` stays empty, and `mcp_tool_registry_ssrf_blocked` log fires (IAN item 10).

#### Deployer migration guide (ATHENA-59 Phase 0)

**Behavior changes from this release — action required before upgrading:**

**(a) Implicit `localhost:5678` MCP default is now fail-closed.**
`tool_registry._load_mcp_tools` falls back to `http://localhost:5678/mcp` when
`N8N_MCP_URL` is unset and no feature-flag row is configured.  Loopback is in the
blocked CIDR, so this default now silently returns an empty MCP tool list.
To restore previous behavior: set `N8N_MCP_URL=http://localhost:5678/mcp` (Class-3
env — unguarded) **or** add `localhost` to `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`
(Class-1 allowlist, kept guarded).

**(b) `http://` and private-IP iCal sources are now rejected.**
`fetch_ical_data` requires HTTPS on every redirect hop
(`allowed_schemes=frozenset({"https"})`).  iCal sources served over plain HTTP or
pointing at private-IP hosts will return HTTP **400** (not 422).
Migration: migrate sources to HTTPS; for private-network iCal servers add their
hostname/CIDR to `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` AND change the URL to HTTPS.

**(c) Playwright browser fallback is now default-off.**
Content fetcher's Playwright path is disabled by default
(`CONTENT_FETCHER_ALLOW_BROWSER_FETCH` defaults to `false`).  JS-rendered pages
that previously relied on the headless browser fallback will now return plain-HTTP
content only.  To restore: set `CONTENT_FETCHER_ALLOW_BROWSER_FETCH=true`
(accepts the R3 residual — no per-hop SSRF guard inside Playwright).

**(d) `music_config` `HA_URL` hardcoded default removed.**
The `http://192.168.10.168:8123` fallback in `music_config.py` is gone.  Deployments
that relied on the default must set `HA_URL` explicitly in their env/ConfigMap.
An unset `HA_URL` now logs a startup warning and returns HTTP 503 on music requests.

**(e) SSRF IP-pinned transport requires `httpx~=0.28`.**
`src/shared/url_safety._build_pinned_transport` replaces `httpx.AsyncHTTPTransport._pool`
(a private httpx 0.28 API) to close the DNS-rebinding TOCTOU window.
`src/shared/pyproject.toml` and `admin/backend/requirements.txt` are both pinned to
`httpx~=0.28`.  If a dependency conflict forces an older httpx version at image-build
time, the helper detects the missing `_pool` attribute at runtime and falls back
gracefully: per-hop SSRF validation remains active, but the IP-pinned transport is
disabled and a `url_safety_pinned_transport_unavailable` structured-log warning fires
so the operator can see the degraded state.  Check logs on first startup after upgrade.

---

## [Unreleased]

> **Ticket:** ATHENA-57
> **Commits:** `67075c1` (Phases 1/1b — observability), `a780bfa` (Phase 2 — harness), `d6213a8` (codex r2 + valerie reconciliation)

### Orchestrator benchmark observability + tool-calling harness (ATHENA-57)

- **Added** (`ATHENA-57`): `skip_semantic_cache` field on `POST /query` request body (`OrchestratorState`). When `true`, the semantic-cache lookup and write are both skipped for that request. This prevents cached responses from contaminating repeated benchmark runs. The field is also included in `QueryResponse.metadata` so callers can confirm it was honoured.
- **Added** (`ATHENA-57`): `metadata.model_component_used` — the actual model tag used by the tool-calling node for the turn (e.g. `"qwen3:4b"`). Populated from the component model assignment resolved at query time; falls back to `null` when the tool-call node was not reached.
- **Added** (`ATHENA-57`): `metadata.model_component_name` — the component-model config name resolved by the router (e.g. `"tool_calling_simple"`). Separate from `model_component_used` to distinguish the router's component decision from the actual model tag.
- **Fixed** (`ATHENA-57`): helper-cache invalidation in the tool-calling node — a stale cache entry could return the prior turn's component assignment after a model config change. Cache is now keyed on the component name so config changes are picked up on the next turn.
- **Added** (`ATHENA-57`): `scripts/bench_tool_calling.py` — benchmark harness for A/B tool-calling trials. Sends each query from `bench/query_set.yaml` N times against a live orchestrator (`--n` runs per query, default 20), records per-turn JSONL rows, and writes results to `bench/results/`. Key bindings per run: `temperature=0.1`, `skip_semantic_cache=true`. Pass `--self-test` to validate query-set, scoring logic, and fallback attribution without a live host.
- **Added** (`ATHENA-57`): `scripts/bench_report.py` — aggregates one or two JSONL result files into a human-readable per-component and per-cell summary (correct-tool rate, false-positive rate, p50/p90 latency). Pass one file for a single-cell summary; pass two for an A/B diff.
- **Added** (`ATHENA-57`): `bench/query_set.yaml` — 40-query synthetic benchmark set covering all tool-calling components (simple, complex, super-complex) plus none-tagged turns for false-positive measurement. No real user data.
- **Added** (`ATHENA-57`): `bench/README.md` — JSONL schema, decision gates (Gate 1: correct-tool rate +5pp; Gate 2: FP rate ≤ 15% absolute and ≤ qwen3+5pp relative; Gate 3: p90 latency ≤ incumbent × 1.10), environment contract, attribution-fallback rule, and committed-results policy.

**Status:** Benchmark executed 2026-06-12. Decision: **NO-SWAP** — gemma4 QAT challengers failed all three gates; qwen3:4b-instruct-2507-q4_K_M remains on all tool-calling components; config unchanged. See [`bench/results/ATHENA-57-DECISION.md`](bench/results/ATHENA-57-DECISION.md).

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-15-deliver-audit-deferred-cleanup-batch.md`
> **Tickets:** ATHENA-11 (C1 cleanup-batch — Phase 2 + Phase 5 reconciliation)

### Audit-deferred cleanup-batch reconciliation (ATHENA-11 C1)

- **Changed**: `admin-backend DEV_MODE startup gate now allows local-host Postgres with WARNING instead of FATAL (xander:6 carve-out). Production K8s pods still fail fatally via KUBERNETES_SERVICE_HOST guard. See `admin/backend/app/utils/url_validators.py:151` and `admin/backend/main.py:381-426`.`
- **Fixed**: `Migration 058 clears legacy `oidc_redirect_uri`/`oidc_provider_url` rows in the `secrets` table that were seeded with maintainer-specific xmojo.net values prior to commit `5403a8a`. Requires `ENCRYPTION_KEY` to be set. Supports `DRY_RUN_058=true` for rehearsal. No-op on fresh deployments. (bob:1 follow-up)`

---

## [Unreleased]

> **Plans:** `thoughts/shared/plans/active-2026-05-11-deliver-rag-services-table-rename.md`, `thoughts/shared/plans/active-2026-05-11-deliver-health-poller-leader-election.md`
> **Tickets:** [ATHENA-17](https://plane.xmojo.net), [ATHENA-18](https://plane.xmojo.net)

### Rename `rag_services` table to `athena_service_registry` (ATHENA-17)

- **Changed** (`ATHENA-17`): Alembic migration `057_rename_rag_services_to_athena_service_registry.py` renames the `rag_services` table to `athena_service_registry`. No data loss; downgrade restores the original name. SQLAlchemy model `RagService` updated to `__tablename__ = "athena_service_registry"`. All ORM queries, route docstrings, and YAML comments updated to reference the new table name.

### Health-poller leader election (ATHENA-18)

- **Added** (`ATHENA-18`): Redis SETNX-based leader election in `admin/backend/app/services/health_poller.py`. With `replicas: 2`, only one replica polls and writes health columns per cycle; the non-leader yields the iteration. A strict-abort per-cycle heartbeat task (every `HEARTBEAT_INTERVAL_SECONDS=20`) renews the lease atomically via Lua check-and-set and cancels the in-flight `_poll_all_services` task if the lease cannot be renewed, preventing any replica from writing after lease loss. New env var: `ATHENA_NAMESPACE` (default `athena-prod`, introduced by ATHENA-59 Phase 1 via downward API injection) — the Redis lease key is namespaced using this value so concurrent deployments in different namespaces do not share a lease. Leader election otherwise uses the existing `REDIS_URL`. `HEALTH_POLL_INTERVAL_SECONDS` must remain < 40s (`LEASE_TTL_SECONDS`); startup raises `SystemExit FATAL` if this invariant is violated.

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-09-deliver-validator-fix-general-info.md`
> **Ticket:** [ATHENA-39](https://plane.xmojo.net)

### Validator training-knowledge bypass (ATHENA-39)

- **Fixed** (`ATHENA-39`): `src/orchestrator/nodes/validate_node` — validator no longer rejects GENERAL_INFO and conversation-context responses synthesized from LLM training knowledge when no retrieved data and no base knowledge are available. Previously, `validate.py:189`'s fact-check prompt ("ANY specific factual claims are likely hallucinations" when no Retrieved Data is present) caused false-positive Layer 4 rejections for responses that `synthesize_node` itself explicitly allowed using training knowledge (`synthesize.py:129-144` for `GENERAL_INFO`, `synthesize.py:152-166` for any intent with conversation history). First-turn current-domain queries (WEATHER, SPORTS, STOCKS, NEWS, etc.) with no RAG data continue to run the LLM fact-check because their synthesize branch (`synthesize.py:167-179`) tells the model not to invent specifics — Layer 4 protection is correctly aligned there. WEBSEARCH is carved out even with conversation context (freshness implied by intent).
- **Added** (`ATHENA-39`): new Prometheus metric label `validation_counter{passed="true", reason="training_knowledge_fallback"}` for observability of the bypass path. No env var or API change.

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-09-deliver-rag-oss-dep-cleanup.md`
> **Ticket:** [ATHENA-33..38](https://plane.xmojo.net)

### RAG OSS dependency cleanup (ATHENA-33..38)

- **Fixed** (`ATHENA-33`): `src/rag/sports/requirements.txt` — added `feedparser>=6.0.10`. Service had `import feedparser` but the package was missing, causing CrashLoopBackOff at startup.
- **Fixed** (`ATHENA-34`): `src/rag/community_events/main.py` — replaced `REDIS_HOST`/`REDIS_PORT`/`REDIS_DB` env-var reads with `COMMUNITY_EVENTS_REDIS_URL` (default `redis://redis:6379/1`). The kubelet auto-injects `REDIS_PORT=tcp://<svc-ip>:6379` for any K8s Service named `redis`, which broke the `int(os.getenv("REDIS_PORT"))` cast at import time. The new URL-based approach is unambiguous and immune to the injection.
- **Fixed** (`ATHENA-35`): `src/rag/price_compare/Dockerfile` — `providers/` subpackage is now COPYd to `/app/providers` (the WORKDIR, matching `from providers.base import ...`). Previously it was copied to `/app/rag_service/providers/` (unreachable on `sys.path`). Fix is encoded in `scripts/generate-rag-dockerfiles.py` via the new `SERVICE_EXTRA_COPIES` dict so future `--force` regeneration doesn't clobber it.
- **Fixed** (`ATHENA-36`): `src/rag/site_scraper/main.py` — `ContentFetcher` is now imported from `shared.content_fetcher` (not `orchestrator.search_providers.content_fetcher`, which was never in the site_scraper image). `ContentFetcher` moved to `src/shared/content_fetcher.py`; backward-compat shim at the old path re-exports all public symbols. See ATHENA-36-followup for shim removal.
- **Fixed** (`ATHENA-37`): `src/rag/tesla/requirements.txt` — added `asyncpg>=0.29.0`. Service startup no longer crashes on import. Pool creation is now gated behind `TESLAMATE_ENABLED` (default `false`); when disabled the service starts cleanly and query endpoints return HTTP 503 with a clear remediation message. `/health` returns 200 regardless of DB state.
- **Fixed** (`ATHENA-38`): `src/rag/transportation/requirements.txt` — added `beautifulsoup4>=4.12.0`. Service had `from bs4 import BeautifulSoup` but the package was missing.
- **Added** (`ATHENA-35`): `scripts/generate-rag-dockerfiles.py` — `SERVICE_EXTRA_COPIES` dict for service-specific subpackage COPY entries; `--service <name>` flag to regenerate a single service; `--check` and `--check-advisory` flags for drift detection.
- **Added**: `scripts/service-defs.sh` — single source of truth for `RAG_SERVICES`, `CORE_SRC_SERVICES`, `ADMIN_SERVICES` arrays; sourced by both `build-and-push.sh` and `smoke-rag-images.sh`.
- **Added**: `scripts/smoke-rag-images.sh` — builds all 23 RAG images and verifies `python -c "import main"` for each. Full-sweep (not fail-fast); `--service <name>` for single-service runs.
- **Added**: `Makefile` with `smoke-rags` target (`make smoke-rags SERVICE=<name>`).
- **Added**: `.github/workflows/rag-smoke.yml` — PR-gated CI check on `src/rag/**` and `src/shared/**` changes.
- **Added**: `.github/workflows/rag-generator-drift.yml` — advisory-only CI check for generator/Dockerfile drift (always exits 0 until ATHENA-36b).
- **Added**: `CONTRIBUTING.md` — "Adding or modifying a RAG service" checklist (6 items).

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-08-deliver-service-auth-hardening.md`
> **Ticket:** [ATHENA-21](https://plane.xmojo.net)
> **Commits:** `3ed22e6` (phase 1), `eb8b305` (phase 2)

### service-auth hardening (ATHENA-21)

- **Fixed**: `verify_service_or_oidc` no longer silently falls through to OIDC when `SERVICE_API_KEY` is unset and a caller sends a non-empty `X-Service-Key` header. The helper now returns HTTP 503 with body `{"detail": "Service authentication not configured"}`. The `WWW-Authenticate` header is intentionally absent — this is a server-side misconfiguration signal, not an authentication challenge; retrying with credentials will not help. (`admin/backend/app/utils/service_auth.py`, `3ed22e6`)
- **Behavioral change for callers**: any `verify_service_or_oidc`-protected endpoint (service-registry write endpoints: POST, toggle, refresh, delete, poll-now, check) will now return 503 instead of the previous silent OIDC fallthrough when a non-empty `X-Service-Key` is sent to a deployment where `SERVICE_API_KEY` is unset. Callers that send no `X-Service-Key` header are unaffected.
- **Startup gate — behavioral tests added**: the existing production gate (`_INSECURE_DEFAULTS` loop in `admin/backend/main.py`) that already raises `SystemExit` when `SERVICE_API_KEY` is empty **or** set to the placeholder `dev-service-key-change-in-production` now has dedicated behavioral regression tests pinned to ATHENA-21 (`TestAthena21StartupGate` in `test_security_hardening.py`). The prior static-source-scan test that asserted the gate's existence by reading `main.py` source text has been demoted via comment as superseded. (`eb8b305`)

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-07-deliver-consolidate-service-registry.md`
> **Ticket:** [ATHENA-1](https://plane.xmojo.net)
> **Commits:** `058d489` → `086e4e1` (phases 1–5)

### service-registry consolidation (ATHENA-1)

- 5-phase architectural refactor consolidating 3 service-definition tables across 2 databases into a single source-of-truth `rag_services` table in the admin DB.
- New `RagService` SQLAlchemy model (`admin/backend/app/models.py`) replaces `ServiceRegistry` + `AthenaService` + `ServerConfig` as the canonical service definition. The 3 deprecated tables are renamed `*_deprecated` in migration 055 and scheduled for hard drop in migration 056 after a 7-day maintenance window.
- Background async health poller (`admin/backend/app/services/health_poller.py`) replaces inline-blocking pings on `GET /api/service-registry/services`. Health state (`health_status`, `last_health_check`, `last_error`, `last_response_time_ms`) is written back to `rag_services` by the poller; the admin UI reads the cache. Eliminates the previous up-to-44s block on the listing endpoint (22 services × 2s timeout).
- Control Agent gains `sync_registry_loop` — on startup it POSTs each entry in `PROCESS_SERVICES` to admin-backend's `POST /api/service-registry/services` using `X-Service-Key`. Host is derived from `urlparse(CONTROL_AGENT_URL).hostname` — no `localhost` fallback (which would silently poison the registry from inside the K8s pod). Missing `CONTROL_AGENT_URL` logs critical and skips the upsert; all other CA endpoints continue normally.
- 5 new env vars: `SERVICE_REGISTRY_WRITE_PER_MINUTE` (default 60), `HEALTH_POLL_INTERVAL_SECONDS` (default 30), `HEALTH_POLL_TIMEOUT_SECONDS` (default 5), `HEALTH_POLL_CONCURRENCY` (default 8), `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` (comma-separated CIDRs/hostnames overriding the SSRF block; default empty — K8s operators with services on private subnets must set this).
- SSRF guard in `health_poller._validate_service_url` mirrors `src/control_agent/url_validator.py::_PRIVATE_NETS` — blocks RFC1918 (10/8, 172.16/12, 192.168/16), loopback, link-local, IPv6 ULA (fc00::/7), IPv6 link-local (fe80::/10), and `.cluster.local` / `kubernetes.default.svc` suffix targets. Path-injection (CRLF, `..`, NUL) also rejected. Overridden per-host via `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`.
- `last_error` values are categorically sanitized — stored as one of `connection_refused`, `timeout`, `http_5xx`, `http_4xx`, `ssrf_blocked`, `unknown`. No raw exception text is written to the DB or rendered in the admin UI.
- `GET /api/service-registry/services` now requires authentication (Bearer JWT or `X-Service-Key`). Pre-Campaign-4 the endpoint was unauthenticated.
- Dual-auth helper `verify_service_or_oidc` (`admin/backend/app/utils/service_auth.py`) accepts `X-Service-Key` OR OIDC Bearer JWT. Wired on POST, toggle, refresh, delete, and poll-now endpoints. Write endpoints are also covered by the `SERVICE_REGISTRY_WRITE_PER_MINUTE` rate-limit bucket (separate from the login rate-limit).
- **Removed**: `admin/backend/app/routes/servers.py` route module, `ServerConfig` model, and the "Servers" tab in the admin UI. The "server" concept was a pre-consolidation artifact with no remaining callers after Phase 5.
- **Behavioral change**: `health_status` column values normalized to `'healthy'`/`'unhealthy'`/`'unknown'`/`'pending'`. Previous mixed values (`'online'`/`'degraded'`/`'offline'`) from legacy code paths are no longer emitted.

Closes ATHENA-1.

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/active-2026-05-06-deliver-auth-rate-limit-bypass.md`
> **Ticket:** [ATHENA-14](https://plane.xmojo.net)
> **Commits:** `f94c589` → `6e2b8db` (phases 1–4)

### auth-hardening (ATHENA-14)

- **Per-IP rate limit** on `POST /api/auth/local-login` via fastapi-limiter (Redis-backed). Default: 5 requests/minute/IP. Custom identifier uses `request.client.host` only — `X-Forwarded-For` is ignored to defeat IP-rotation bypass. Returns 429 on breach. Configurable via `LOGIN_RATE_LIMIT_PER_MINUTE`; set to 0 to disable (lockout + timing floor still apply).
- **Per-username DB lockout**: `users.failed_login_count` is incremented atomically on each wrong-password attempt. Account locks (`users.locked_until` set) once `LOGIN_LOCKOUT_THRESHOLD` cumulative failures are reached (default 10). Lockout window is `LOGIN_LOCKOUT_MINUTES` (default 30 min). Lockout is idempotent — past-threshold attempts do not extend the window. Successful login resets the counter. Manual unlock: `UPDATE users SET failed_login_count=0, locked_until=NULL WHERE username='<name>';`
- **400 ms wall-time floor** (`LOGIN_MINIMUM_DELAY_MS`, default 400) on every failure path. All four failure branches (not-found, inactive, locked, wrong-password) pay full PBKDF2-600k cost via dummy-hash AND sleep until elapsed >= floor, closing the timing side-channel.
- **Enumeration oracle closed**: all four failure branches now return an identical 401 `"Invalid username or password"`. `403 Account inactive` is no longer emitted — **behavioral change for callers that distinguished inactive-user 403 from wrong-password 401**.
- **4 new env vars**: `LOGIN_RATE_LIMIT_PER_MINUTE` (default 5), `LOGIN_LOCKOUT_THRESHOLD` (default 10), `LOGIN_LOCKOUT_MINUTES` (default 30), `LOGIN_MINIMUM_DELAY_MS` (default 400). All modelled on `AthenaConfig`; see `.env.example` and `manifests/athena-prod/config.yaml` for commented stubs.
- **Follow-up tickets** (out of scope for this campaign): [ATHENA-15](https://plane.xmojo.net) — public-route service-key gating for alert-write + tool_calling api-key endpoints; [ATHENA-16](https://plane.xmojo.net) — lockout-DoS mitigation (admin-unlock CLI + email notification on lockout).

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/active-2026-05-06-deliver-security-hardening.md`
> **Ticket:** [ATHENA-12](https://plane.xmojo.net)
> **Commits:** `762f263` → `41f0b57` (8 commits, phases 1–4)

### Security

- **Phase 1** (xander:6): admin-backend now exits at startup if `DEV_MODE=true` AND `DATABASE_URL` is a non-SQLite URL. `DEV_MODE` auto-creates an unauthenticated `dev-admin` user with `role=owner` on every unauthenticated request — running this against a real database is a misconfiguration that would silently provision a privileged account. The startup gate fires before `init_db()` so no partial state is produced. Error message names both env vars and the resolution. (`762f263`, `ea623d0`)
- **Phase 2** (xander:13 + codex-M2): `_INSECURE_DEFAULTS` rejection dict now covers `OIDC_CLIENT_ID`. Two placeholder values are rejected: `"demo-mode"` (previously handled by an ad-hoc `if` block, now folded into the canonical dict) and `"CONFIGURE_ME_OIDC_CLIENT_ID"` (the placeholder emitted by `scripts/create-secrets.sh:127-132`, which documented backend rejection that was never enforced). Whitespace-bypass closed: `OIDC_CLIENT_ID` is read via `get_config().oidc_client_id` (pydantic-stripped), so `" demo-mode"` no longer evades the gate. (`0117a25`)
- **Phase 2 reconcile** (xander:16 + xander:17): `DEMO_MODE=true` with `DEV_MODE=false` now raises `SystemExit` at startup — closes a separate privilege-escalation path through `auth_login`'s demo-bypass branch. Empty `OIDC_CLIENT_ID` is now rejected before `oauth.register()` is called, preventing authlib registration with a blank client ID. (`6115ecb`)
- **Phase 3** (xander:3 + MED-A + MED-E): OIDC ID-token `iss`, `aud`, and `exp` validation re-enabled. The `claims_options={"essential": False, ...}` override that disabled authlib's built-in claim validation was removed; authlib now enforces `iss`/`aud`/`exp` by default. Two new fail-closed startup gates added: (1) a runtime-issuer assertion that fires after `configure_oauth_client()` loads the DB-stored OIDC config, catching a tampered or empty issuer that would slip past the env-var gate; (2) a discovery-doc gate that fetches and validates the IdP's `.well-known/openid-configuration` at startup — if the document is unreachable or omits `issuer`, the backend exits rather than registering with a client whose `iss` validation would be silently skipped by authlib. **Operational note for deployers upgrading from a prior release:** the admin-backend now requires the IdP to be reachable at startup. An unreachable or non-conformant IdP causes `SystemExit("FATAL: OIDC discovery metadata fetch failed")`. Sequence pod startup behind an init container or readiness gate that verifies IdP connectivity. If your IdP's `iss` claim does not match `OIDC_ISSUER` exactly, align them before upgrading — tokens with a mismatched issuer will now be rejected. (`85f35f7`, `701fbe9`)
- **Phase 4** (xander:4): JWT is no longer passed as `?token=<jwt>` in the OIDC callback redirect URL or the `DEMO_MODE` redirect URL. The backend now writes the JWT to the server session and redirects to `<FRONTEND_URL>?logged_in=1`. The admin frontend detects `?logged_in=1`, clears any stale `localStorage.auth_token` (preventing cross-user contamination on shared devices), and fetches the JWT from the existing `/api/auth/session-token` endpoint. The `?token=` URL query parameter is no longer emitted; the `?logged_in=1` hint is idempotent and carries no credentials. Closes the JWT-leak-via-URL chain: 8-hour bearer tokens are no longer written to reverse-proxy access logs, browser history, or `Referer` headers on every admin login. Note: the `admin-jarvis.js` WebSocket URL (`?token=` in upgrade request) was a related but distinct exposure requiring a backend protocol change; it was deferred to a separate Plane ticket at the time of this release — **closed by ATHENA-55** (see above, cross-ref Notes). (`33db179`, `41f0b57`)

### Notes

- This is **Campaign 2 of 6** in the audit-deferred security-hardening sequence. Findings closed: xander:3, xander:4, xander:6, xander:13 (audit-named scope) plus xander:16, xander:17 (pre-existing findings pulled into Campaign 2 by user direction). The admin-jarvis.js WebSocket query-token (codex-H2) was explicitly out of scope in this campaign — **closed by ATHENA-55** (see ATHENA-55 entry above).
- `pytest-httpserver>=1.0.8` was added to `admin/backend/requirements.txt` (annotated `# test-only`) to support OIDC validation tests that drive authlib against a real fixture issuer. Splitting dev and production requirements is deferred to a future campaign (HIGH-E).

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/active-2026-05-06-deliver-audit-deferred-quick-wins.md`
> **Ticket:** [ATHENA-11](https://plane.xmojo.net)
> **Commits:** phases 1–6

### Added

- `admin/backend/alembic/versions/053_clear_legacy_gateway_config_ips.py` — data migration that clears legacy maintainer-IP defaults (`http://192.168.10.167:*`) from `gateway_config.orchestrator_url` and `gateway_config.ollama_fallback_url` rows; handles exact and trailing-slash variants. (audit bob:1 follow-up, ATHENA-11 Phase 5)
- `manifests/athena-prod/ollama-model-pull-job.yaml` — Ollama model-pull Job extracted into its own manifest file. `scripts/deploy.sh` now accepts a `--first-run` flag; the Job apply/wait is gated behind `FIRST_RUN=true` so normal re-deploys skip it. (audit otto:11/12, ATHENA-11 Phase 3)

### Changed

- **control-agent**: Control Agent is now opt-in via `CONTROL_AGENT_ENABLED` (default `false`). OSS deployers no longer see Control-Agent connection errors out of the box. Existing Mac-Studio-equipped deployments must set `CONTROL_AGENT_ENABLED=true` (and `CONTROL_AGENT_URL=<host>:8099`) in their env or kubeconfig overlay. Disabled-path responses are per-endpoint: 503 for download mutations, structured logs for orchestrator keepalive, neutral typed responses for service-control queries. (audit bob:4, ATHENA-11 Phase 6)
- `docs/INSTALLATION.md`: `imagePullPolicy: Always` documented as dev-default; first-run vs. normal deploy flow clarified. (audit otto:11/12, ATHENA-11 Phase 3)

### Fixed

- **a11y**: added `for=`/`id=` associations to ~426 admin-frontend `<label>` elements across 28 files (WCAG 1.3.1, 4.1.2). Display-only label misuse converted to `<p>`/`<span>`. Wrapping labels (~41 residual) left as-is per containment rule. (audit ruby:1, ATHENA-11 Phase 4)

### Removed

- Deleted dead stub directories under `apps/`: `gateway/`, `orchestrator/`, `rag/`, `share-service/`, `shared/`, `validators/`. These were README-only placeholders with zero importers (verified by librarian agent at HEAD `03736ee`). The live `apps/jarvis-web/` (Jarvis voice/chat web UI) and `apps/chat-embed/` (CORS-relay proxy) are unchanged. (audit bob:6 / librarian:8, ATHENA-11 Phase 2)

### Notes

- This is **Campaign 1 of 6** in the audit-deferred remediation sequence. Subsequent campaigns cover: security-hardening (OIDC `iss` validation, JWT URL removal, SQLite `DEV_MODE` — xander); rate-limiting (`fastapi-limiter`); network policies + RBAC (otto:10); `BaseRAGService` migration (librarian:2/4); and remaining UI scope (ruby:2–10). Changes here are self-contained; none of those campaigns depend on this one shipping first.
- Per-endpoint disabled-path route tests for Phase 6 (`debug_logs` status, `model_downloads` helper + create/retry/delete gates, `service_control` containers/ollama-health shape) are deferred — `admin/backend` lacks route-level test scaffolding at HEAD. (#ATHENA-11)

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md`
> **Ticket:** [ATHENA-10](https://plane.xmojo.net)
> **Commits:** `615d7d0` → `14fcb73` (19 commits)

Pure refactor — no behavior change. Decomposed `src/orchestrator/main.py` from 12,409 lines into an 8,758-line core plus 12 sibling modules. Zero new failures introduced; 209 new unit tests added (31 failed / 207 passed → 31 failed / 416 passed).

### Added

- `src/orchestrator/nodes/_runtime.py` — runtime singleton accessor (`_runtime.get_X()` / `_runtime.set_X()` / `_runtime.is_ready()` / `_runtime.missing_required()` / `_runtime.required_singletons()`). Singletons are set by lifespan, read at call time. Tests install fakes via setters directly. (#ATHENA-10)
- `src/orchestrator/urls.py` — 15 module-level service URL constants previously scattered in `main.py`'s constant block. (#ATHENA-10)
- `src/orchestrator/metrics.py` — 7 Prometheus metric declarations (`request_counter`, `request_duration`, `node_duration`, `tool_call_breakdown`, `validation_counter`, `hallucination_counter`, `validation_layer_duration`) moved verbatim from `main.py`. (#ATHENA-10)
- `src/orchestrator/helpers.py` — 17 stateless helper functions extracted from `main.py`. Helpers that need runtime singletons call `_runtime.get_X()` at call time (Pattern 1). (#ATHENA-10)
- `src/orchestrator/mode_permission.py` — 6 mode/permission helpers (`get_current_mode`, `detect_owner_mode_command`, `extract_pin_from_query`, `activate_owner_override`, `check_intent_permission`, `check_entity_permission`) plus `OWNER_MODE_PATTERNS` constant. (#ATHENA-10)
- `src/orchestrator/nodes/route_info.py` — `route_info_node` (33 LOC, zero runtime dependencies). (#ATHENA-10)
- `src/orchestrator/nodes/send_sms.py` — `send_sms_node`. (#ATHENA-10)
- `src/orchestrator/nodes/notification_pref.py` — `notification_pref_node`. (#ATHENA-10)
- `src/orchestrator/nodes/synthesize.py` — `synthesize_node`. (#ATHENA-10)
- `src/orchestrator/nodes/validate.py` — `validate_node`. (#ATHENA-10)
- `src/orchestrator/nodes/finalize.py` — `finalize_node`. (#ATHENA-10)
- `src/orchestrator/nodes/route_control.py` — `route_control_node`. (#ATHENA-10)
- `src/orchestrator/nodes/route_music.py` — `route_music_node`. (#ATHENA-10)
- `src/orchestrator/nodes/route_tv.py` — `route_tv_node`. (#ATHENA-10)
- `src/orchestrator/nodes/retrieve.py` — `retrieve_node` (largest single extraction; 538 LOC body). (#ATHENA-10)
- 209 new unit tests across `tests/unit/test_helpers.py`, `test_mode_permission.py`, `test_route_info.py`, `test_send_sms_node.py`, `test_notification_pref.py`, `test_synthesize.py`, `test_validate.py`, `test_finalize.py`, `test_route_control.py`, `test_route_music.py`, `test_route_tv.py`, `test_retrieve.py`, `test_health_probes.py`. (#ATHENA-10)

### Changed

- `src/orchestrator/main.py` reduced from 12,409 → 8,758 lines (−3,651, ~29%). The 10 extracted node functions and 17 helpers are imported back into `main.py`'s graph builder; runtime behavior is byte-identical. (#ATHENA-10)
- `src/orchestrator/main.py` runtime singletons: 97 bare module-level reads migrated to `_runtime.get_X()` call-time accessors. 16 bare `Optional[X] = None` module-level declarations removed. `global` keyword removed from lifespan. Lifespan dual-write removed (Phase 1.2 scaffolding); `_runtime.set_X()` is now the sole write path. (#ATHENA-10)
- 6 bare-except blocks in `health_check` and `readiness_probe` replaced with `except Exception as e` + structured log with `exc_info=e`. (#ATHENA-10)

### Notes

- `src/orchestrator/state.py` is now the canonical source for `OrchestratorState`, `IntentCategory`, `ModelTier`, and `ConversationContext`. Duplicate definitions that had accumulated in `main.py` were removed in commit `8034360` (Phase 1.1). (#ATHENA-10)
- `classify_node` (2,473 lines), `tool_call_node`, route handlers, and streaming functions remain in `main.py`. Extraction is deferred: `classify_node` to Campaign 2; `tool_call_node` to Campaign 1.3; route handlers to Campaign 1.5. (#ATHENA-10)
- 14 proxy-class instances across `nodes/` share a common `__getattr__`-defers-to-`_runtime.get_X()` shape. Promotion to a shared `orchestrator.nodes._proxy.runtime_proxy()` factory is deferred to Campaign 2. (#ATHENA-10)
- xander (security review) on Phase 3.1 surfaced 2 HIGH + 3 MEDIUM + 3 LOW pre-existing findings on the permission/PIN surface. All pre-existing; none introduced by this refactor. Tracked for a follow-up security-hardening campaign. (#ATHENA-10)

---

## [Unreleased]

> **Plan:** `thoughts/shared/plans/2026-05-06-deliver-config-py-rebuild.md`
> **Ticket:** [ATHENA-7](https://plane.xmojo.net)
> **Commits:** `aaa989d`

### Added

- `AthenaConfig` (`src/shared/config.py`) — canonical pydantic-settings `BaseSettings` object centralizing 11 env vars: `OLLAMA_URL`, `LLM_SERVICE_URL`, `REDIS_URL`, `DATABASE_URL`, `SERVICE_API_KEY`, `DEFAULT_TIMEZONE`, `DEFAULT_CITY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `DEMO_MODE`, `DEV_MODE`. Read via `get_config()`. `admin_url` is a computed field that delegates to Campaign 3's `get_admin_url()` and is not env-loadable via `ADMIN_URL`. (#ATHENA-7)
- `pydantic-settings>=2.1.0,<3.0` dependency (required by `AthenaConfig`).
- `CONTRIBUTING.md` Configuration Guidelines section updated to recommend the `AthenaConfig` extension pattern for new env vars.

### Removed

- **`admin/frontend/router.js`**: deleted (~200 lines of dead navigation code that paralleled the live `showTab()` system; no callers besides one in `command-palette.js`, now updated). (audit follow-up: dexter:7, ATHENA-9)

### Changed

- **LLM endpoint precedence** (`admin-backend`): when both `OLLAMA_URL` and
  `LLM_SERVICE_URL` are set, `LLM_SERVICE_URL` now wins at the database
  seeder (matches the dominant precedence used by orchestrator + gateway).
  Previously `OLLAMA_URL` won at `admin/backend/app/database.py:268` (the seed
  path) while the rest of the codebase used `LLM_SERVICE_URL`-first. Operators
  using both env vars should verify the seeded `system_settings.ollama_url` row
  after deploy. Run:
  `kubectl -n athena-prod exec -it deploy/athena-admin-backend -- psql $DATABASE_URL -c "SELECT key, value FROM system_settings WHERE key='ollama_url';"`
  See Campaign 4 plan, Phase 2a-ii.

- **`REDIS_URL` default** changed from `redis://localhost:6379` (mixed across call sites) to `redis://redis:6379/0` (in-cluster DNS shortname, consistent with `manifests/athena-prod/config.yaml`). Production deployments are unaffected — the manifest sets `REDIS_URL` explicitly. Local-dev users should add `REDIS_URL=redis://localhost:6379` to their `.env` file — see `.env.example`. (#ATHENA-7)

- **Qdrant**: PersistentVolumeClaim is now the default storage backend (was `emptyDir`, which silently lost all conversation memory on every pod restart). Deployers must replace `YOUR_STORAGE_CLASS` in `manifests/athena-prod/qdrant.yaml` with their cluster's StorageClass before applying. Existing `emptyDir`-based deployments will lose their current Qdrant data on the next apply — see `docs/INSTALLATION.md` for migration notes. (audit follow-up: otto:3, ATHENA-8)

---

## [0.3.0] - 2026-05-06 — Admin URL Consolidation

> **Plan:** `thoughts/shared/plans/2026-05-06-deliver-admin-url-consolidation.md`
> **Ticket:** [ATHENA-3](https://plane.xmojo.net)
> **Commits:** `105f782` → `979812f` (8 commits)

Replaces 32 independent admin-URL resolution sites across 20 files with a single canonical helper. One resolution order, one fallback chain, one startup log line per service.

### Added

- `src/shared/admin_url.py` — canonical `get_admin_url()` helper. Resolution order: `ADMIN_API_URL` → `ADMIN_BACKEND_URL` → `ADMIN_INTERNAL_URL` (deprecated alias) → `LOCAL_DEV=true` → K8s in-cluster auto-discovery (`KUBERNETES_SERVICE_HOST`) → empty string + warning log. Caches the resolved URL at module import time; cache is invalidable via `_clear_cache_for_tests()` in test code.

### Changed

- **32 admin-URL resolution sites consolidated** — all callers in `src/shared/`, `src/orchestrator/`, `src/gateway/`, `src/mode_service/`, `src/rag/` (4 services), `apps/jarvis-web/backend/`, and `src/sms/` now delegate to `get_admin_url()` instead of each performing their own `os.getenv` chain.
- **jarvis-web Dockerfile build context changed to repo root** — required so `src/shared/admin_url.py` is reachable during the image build. `apps/jarvis-web/build-and-deploy.sh` updated accordingly.
- **`docs/CONFIGURATION.md`** — `ADMIN_API_URL` promoted to Required Settings table; full resolution order documented with reference to `src/shared/admin_url.py`.
- **`.env.example`** — resolution order documented inline; `ADMIN_BACKEND_URL` and `ADMIN_INTERNAL_URL` moved to commented-out alias block with deprecation note; `LOCAL_DEV` escape-hatch entry added.
- **`docs/INSTALLATION.md`** — admin URL configuration section updated to reference the new helper and `ADMIN_API_URL` as the canonical variable.
- **`README.md`** — env-var table updated; `ADMIN_API_URL` entry now references the resolver with `LOCAL_DEV=true` note.

### Fixed

- **`src/mode_service/main.py` port typo** — fallback was `http://localhost:5000` (the mode service's own port); corrected to delegate to `get_admin_url()` which resolves to the admin backend.
- **3 hardcoded literals in `src/orchestrator/smart_home_controller.py`** (lines 2885, 2925, 3176) — plain `admin_url = "http://localhost:8080"` string literals inside `_create_stuck_sensor_alert`, `_resolve_stuck_sensor_alert`, and `_get_house_layout` that pointed to the pod's own localhost in K8s. Now call `get_admin_url()`.
- **1 hardcoded literal in `src/sms/service.py`** (line 252, `SMSService.from_admin_config()`) — same localhost-literal pattern, also broken in K8s. Now calls `get_admin_url()`.
- **`src/orchestrator/memory_manager.py` IN_CLUSTER namespace defect** — previous fallback used the fully-qualified namespace `athena-admin.svc.cluster.local` which is only valid for cross-namespace calls; the helper now uses `athena-admin-backend:8080` (same-namespace short form, consistent with the rest of the fleet).
- **`src/shared/cache.py`** — was the only site that checked `ADMIN_BACKEND_URL` before `ADMIN_API_URL`, silently ignoring `ADMIN_API_URL` if `ADMIN_BACKEND_URL` was set. Now follows the canonical order via the helper.

### Deprecated

- **`ADMIN_INTERNAL_URL`** — accepted as a backward-compatible alias at resolution priority 3, but documented as deprecated in `.env.example` and `docs/CONFIGURATION.md`. Will be removed in a future release. Deployments using this variable should migrate to `ADMIN_API_URL`.

### Removed

- **`src/shared/config_loader.py`** — dead file; no in-tree callers. Deleted in `979812f`.

---

## [0.2.0] - 2026-05-06 — Comprehensive OSS Audit Remediation

> **Audit document:** `thoughts/shared/audits/2026-05-05-audit-athena-oss-comprehensive.md`
> **Plan:** `thoughts/shared/plans/2026-05-05-audit-athena-oss-comprehensive.md`
> **Ticket:** ATHENA-2
> **Commits:** `9f4c40e` → `5830a71` (13 commits)

This release bundles all changes from the comprehensive OSS audit conducted 2026-05-05.
All changes are additive or hardening — no features were removed.

### Added

- `CHANGELOG.md` — this file, tracking changes from the OSS baseline forward ([ATHENA-2](https://plane.xmojo.net))
- `apps/chat-embed/` — CORS-relay proxy for embedding Athena-backed chat on external websites; documented in README and build scripts
- GitHub issue and pull request templates (`.github/`)
- `pytest.ini` — `integration` marker registered; default run (`pytest`) skips live-service tests; `pytest -m integration` selects them
- `scripts/check-env-example.py` — audits `.env.example` for drift against env vars referenced in source code

### Changed

- **Admin backend startup validation hardened** — in production (non-dev) mode the process now hard-fails at startup if `OIDC_ISSUER` is empty, missing, or matches the `CONFIGURE_ME` placeholder; if `OIDC_CLIENT_ID` is the literal string `demo-mode`; or if `SERVICE_API_KEY` is unset. Previously these conditions were silently ignored.
- **Service-to-service auth enforced end-to-end** — `SERVICE_API_KEY` is now required and wired through all service boundaries (admin backend, orchestrator, gateway, RAG services). Previously some paths accepted unauthenticated internal calls.
- **Alembic migrations parameterized** — 6 migrations that previously embedded deployment-specific values via Python f-strings now read those values from environment variables at migration time. `alembic upgrade head` is safe to run against any deployment without code edits.
- **Control Agent input hardening** — path-traversal and SSRF guards added; callback URLs are rejected unless the hostname matches `ALLOWED_CALLBACK_HOSTS` (fail-closed by default when the variable is empty).
- **`scripts/create-secrets.sh` is now idempotent** — re-running the script on a cluster where secrets already exist skips rotation rather than overwriting keys.
- **`scripts/deploy.sh` pre-flight check** — the deploy script now verifies the target namespace and required secrets (`athena-db-credentials`, `athena-encryption`, `athena-oidc`) exist before running `kubectl apply`. Missing secrets abort with an actionable error.
- **Orchestrator Kubernetes manifest** — memory limit raised from 512Mi to 2Gi; CPU limit raised from 250m to 2000m; `startupProbe` added with 420-second grace period for slow LLM initialization.
- **nginx security headers** — admin frontend nginx config now emits `Content-Security-Policy`, `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`, and `Referrer-Policy`. CSP allows existing CDN dependencies (Bootstrap, cdnjs).
- **README** — Chat Embed interface documented; build scripts updated.
- **`.env.example` curated** — stale keys removed; drift between documented and actual environment variables corrected.

### Fixed

- `docs/INSTALLATION.md` — broken cross-references repaired
- Hardcoded `xmojo.net` domain references removed from admin OIDC configuration panel — all OIDC fields now derive from environment variables
- Hardcoded location defaults (`Baltimore`, MD timezone) removed from RAG services — `DEFAULT_CITY`, `DEFAULT_STATE`, and `DEFAULT_TIMEZONE` are now required from the environment or left blank
- Hardcoded HA JWT removed from `src/jetson/` — **token revocation in Home Assistant is a required manual step** (the token appears in git history at commit `794096b`; see audit doc for details)
- Alembic JSONB cast error in Phase 4 migrations corrected (codex r2, `5830a71`)
- RAG `SERVICE_API_KEY` wiring fixed — keys were read but not forwarded in some service paths
- `CONTROL_AGENT_URL` handling corrected — fallback behavior on missing var now logs a warning instead of raising

### Security

- Deployment-specific secrets and domains removed from source code across 18+ files
- Admin backend endpoints that previously accepted requests without authentication now require a valid session or service key
- nginx CSP, HSTS, `X-Frame-Options`, `X-Content-Type-Options`, and `Referrer-Policy` headers added to admin frontend
- Control Agent hardened against path traversal and SSRF via callback URL allowlist (`ALLOWED_CALLBACK_HOSTS`)
- **Action required on upgrade:** revoke the Home Assistant long-lived access token that was hardcoded in `src/jetson/` — it is present in git history at commit `794096b` even though the code reference was removed in `be251ef`

### New environment variables

The following variables were added to `.env.example` and are required or recommended for production deployments:

| Variable | Required | Description |
|---|---|---|
| `SERVICE_API_KEY` | Yes (production) | Shared secret for service-to-service auth |
| `OIDC_ISSUER` | Yes (production) | OIDC provider issuer URL; startup fails if unset |
| `OIDC_REDIRECT_URI` | Yes (with OIDC) | Callback URL registered with your OIDC provider |
| `OIDC_CLIENT_ID` | Yes (with OIDC) | Must not be the literal string `demo-mode` in production |
| `ALLOWED_CALLBACK_HOSTS` | Yes (with Control Agent) | Allowlist of hostnames for HuggingFace download-progress callbacks |
| `DEFAULT_CITY` | No | Default city for location-aware RAG queries (blank = no default) |
| `DEFAULT_STATE` | No | Default state/region for location-aware RAG queries |
| `DEFAULT_TIMEZONE` | No | Timezone for time-aware queries (e.g., `America/New_York`); defaults to `UTC` |
| `OIDC_USERINFO_URL` | No | Manual override for OIDC userinfo endpoint; auto-derived from discovery if unset |

---

## [0.1.0] - 2026-05-05

> **First public OSS baseline, anchored to commit [`7f5387b`](https://github.com/jstuart0/project-athena-oss/commit/7f5387b).**
> Pre-existing commits represent initial development history leading to this point.
> Entries below describe the state of the project at this baseline, not changes since a prior release.

### Added

- **Jarvis Web** (`apps/jarvis-web/`) — full-featured browser chat interface with streaming text, push-to-talk voice, LiveKit WebRTC streaming, smart home widgets, owner/guest mode, and music playback
- **Chat Embed** (`apps/chat-embed/`) — lightweight CORS-relay proxy so external websites can embed an Athena chatbot; fetches assistant profile from admin backend at startup; includes per-IP rate limiting and analytics source tagging
- **MLX streaming** — real token-level streaming for MLX-format models; `answer_chunk` SSE events relay tokens to the browser as they are generated
- **Analytics Mode** — optional conversation capture and review pipeline; tracks source, mode, latency, and session data; gated behind `MODULE_ANALYTICS=true`
- **Persistent chat sessions** — anonymous browser-cookie session IDs retain conversation context across page reloads
- **Safety guardrails** — jailbreak pre-screen layer in the orchestrator preprocessing stack
- **Semantic cache** — intent-aware response caching to avoid redundant LLM calls for equivalent queries
- **Privacy filter** — PII scrubbing (`src/shared/privacy_filter.py`) for queries routed to cloud LLM backends
- **Complexity-aware model routing** — regex-only complexity detector selects fast 4B vs. capable 14B/32B model tier without a routing LLM call
- **Multi-intent decomposition** — orchestrator decomposes compound queries ("turn on the lights and check the weather") into parallel sub-queries
- **23 RAG microservices** — weather, sports, dining, flights, airports, Amtrak, directions, transportation, streaming, events, SeatGeek, SerpAPI, community events, news, stocks, price comparison, Tesla Fleet API, web search, site scraper, BrightData, media, recipes, one-call weather
- **OpenAI-compatible gateway** — `/v1/chat/completions` endpoint for drop-in compatibility with Home Assistant and any OpenAI client library
- **4-layer anti-hallucination pipeline** — dedicated validation model fact-checks LLM responses against retrieved source data before delivery
- **Admin UI** — web interface for runtime model assignment, feature flags, encrypted API key management, service registry, device management, guest mode, analytics, audit log, and memory management; 62 route modules, 50+ DB migrations
- **Module system** — `MODULE_*` environment variable toggles for Home Assistant integration, guest mode, analytics, and Jarvis Web
- **Read-only viewer role** — restricted admin access tier for non-operator users
- **OSS tuning controls** — diagnostics panel and control-plane UI for observable pipeline tuning

### Changed

- README restructured to present chat interface as a first-class deployment path alongside voice hardware
- Dashboard health checks use environment variables rather than hardcoded addresses
- Orchestrator synthesis token limit raised for chat interface to prevent response cutoff on long list answers
- Chat interface routed to complex model tier for richer responses
- `qwen3` and `llama.cpp` thinking/reasoning tokens suppressed via `/no_think` and equivalent flags to reduce latency

### Fixed

- Conversation context not persisting across multi-turn streamed sessions
- Empty responses from streaming endpoint under certain model backends
- `NameError` / `UnboundLocalError` in analytics and search-log paths when modules were partially enabled
- Debug-logs endpoint returning 503 instead of degrading gracefully
- Jarvis Web streaming not wired to `/api/chat/stream` (frontend connected to non-streaming path)
- `interface_type` not forwarded to streaming endpoint initial state
- LLM hallucinated role-continuation in streaming `finalize_node`
- Base knowledge URL validation and progressive streaming correctness
- ASCII art formatting in README

### Security

- Admin backend hardened: service-to-service auth, CORS policy, cookie flags, and startup validation added
- API permission enforcement scoped by viewer vs. operator role

---

[Unreleased]: https://github.com/jstuart0/project-athena-oss/compare/979812f...HEAD
[0.3.0]: https://github.com/jstuart0/project-athena-oss/compare/5830a71...979812f
[0.2.0]: https://github.com/jstuart0/project-athena-oss/compare/7f5387b...5830a71
[0.1.0]: https://github.com/jstuart0/project-athena-oss/commit/7f5387b
