# Deliver — Auth Rate Limit + Public-Endpoint Hardening (Campaign 3 of 6)

**Slug**: `2026-05-06-deliver-auth-rate-limit-bypass`
**Plane ticket**: ATHENA-14 (`51cbdf54-685f-4854-85ff-e0e7adbded8b`)
**Base commit**: `5ff3428` on `tooling/env-example-curation-12` (ATHENA-12 close-out, May 6 2026 — supersedes round-0/round-1's `f23b73a`)
**Pre-campaign tag**: `pre-auth-rate-limit-bypass-2026-05-06`
**Tier**: HEAVY
**Mode**: AUTONOMOUS (codex iteration cap = 3)
**Source audit**: `thoughts/shared/audits/2026-05-05-audit-athena-oss-comprehensive.md` (xander:10 lines 308–312, xander:11 lines 314–319)
**Sibling campaigns** (same branch): Campaign 1 (ATHENA-11, `f23b73a`); Campaign 2 (ATHENA-12, **closed at `5ff3428`**); Campaigns 4, 7, 10, 11, 13 follow.

## Iteration round 2 — what changed and why

Stage 6 codex r1 review (`active-2026-05-06-deliver-auth-rate-limit-bypass.codex-r1-plan.md`) verdict ITERATE, plus two user-locked decisions. Tagged findings cited inline.

**User-locked decisions:**
- **(B) Public-route surface triage** — explicit table classifying all 39 unauthenticated `/public/*`-pattern routes in `admin/backend/app/routes/`. New scope ceiling: only the 4 already-named endpoints (`cloud_providers.py:629/643`, `site_scraper.py:58`, `tool_calling.py:241`) get gated this campaign — every other public route is classified `intentionally-public` (service-to-service config fetches consumed by orchestrator/gateway/RAG without service keys today) or `defer-to-follow-up` (with a Plane ticket). Closes codex-r1 HIGH "public-route scope is not actually closed." See **Public-route surface triage** section below.
- **(D) Inactive-user 403 → 401 generic** — `local_auth.py:38-39` returns 403 today for inactive accounts; that's a status-code oracle that contradicts the campaign's enumeration-protection claim. Pulled into Phase 4 scope. Failure now returns same generic 401 + `"Invalid username or password"` as wrong-password and locked branches. Closes codex-r1 MEDIUM "inactive-user 403 remains a status-code oracle."

**Codex-r1 mechanical fixes:**
- **codex-r1 HIGH "DEV_MODE limiter break"** — `RATE_LIMIT_DEGRADED` defaults False; in DEV_MODE `_init_rate_limiter()` never runs, so the dep called `RateLimiter.__call__` against an uninitialized `FastAPILimiter` and raised. Fix: rename to `LIMITER_ACTIVE` (positive semantics — set True only on successful init in `_init_rate_limiter()`), default False; the dep now no-ops when `not LIMITER_ACTIVE` (covers both DEV_MODE-never-initialized AND prod-Redis-failure-at-init). One flag with a single positive truth condition is cheaper to reason about than two flags ANDed/ORed; degraded-flag observability still works (test reads `LIMITER_ACTIVE is False`). Phase 3 + Phase 4 updated.
- **codex-r1 MEDIUM "400ms floor didn't propagate"** — D6 says 400ms but `AthenaConfig` default in Phase 3 was still 200, verification asserted 200, timing tests asserted ≥180. All three updated: `login_minimum_delay_ms: int = Field(default=400)`, Phase 3 verification asserts 400, D4 cases 6/7 assert ≥360 ms (40 ms test-runner slack against the 400 ms floor).
- **codex-r1 MEDIUM "fakeredis[lua] extra"** — `fastapi-limiter==0.1.6` `depends.py` calls `redis.script_load(...)` and `redis.evalsha(...)`; plain `fakeredis-aioredis` doesn't ship Lua. Phase 3 test-dep updated to `fakeredis[lua]>=2.21,<3` (Lua support is built into fakeredis ≥ 2.0 but `[lua]` extra installs `lupa` for full script execution; pinning the extra is defensive).
- **codex-r1 MEDIUM "Redis-failure flag observability"** — subprocess test couldn't read parent-process state. Negative case rewritten as in-process: monkeypatch `FastAPILimiter.init` to raise, drive `_init_rate_limiter(redis_conn)` directly, assert `LIMITER_ACTIVE is False` and `"rate_limiter_init_failed"` log line emitted. The subprocess pattern is retained only for the `REDIS_URL=redis://127.0.0.1:1` end-to-end case where `LIMITER_ACTIVE` is read by the subprocess and printed to stdout for the parent to grep.
- **codex-r1 push-back "UPDATE...RETURNING SQLite version"** — adopted runtime-guard option. **Codex-r1b polish (Medium)**: guard moved out of `_init_rate_limiter()` (production-branch-only) into the TOP of `startup_event` BEFORE the `if DEV_MODE:` branch — fires for both DEV_MODE local SQLite and production SQLite-backed deploys, since Phase 4's `UPDATE ... RETURNING` runs in either case. Project's `python:3.11-slim` Docker base ships SQLite 3.40+ (verified at `Dockerfile:5`); guard is defense-in-depth for self-host deployers on older Debian/Ubuntu base images. Phase 3 verification documents.
- **codex-r1 LOW "frontend 429 rationale"** — codex confirmed `data.detail || data.error || 'Login failed'` works either way (fastapi-limiter 0.1.6 raises `HTTPException(429, "Too Many Requests")`, FastAPI returns `{"detail":"Too Many Requests"}`). Keeping the OR fallback as defensive future-proofing. No change beyond comment update in Phase 4.
- **codex-r1 metadata** — base SHA + Plane ticket + Campaign 2 status updated above.

**Phase-count change**: 5 → 5 (unchanged). Phase 1 grows by ~25 lines (triage table). Phase 4 grows by 1 test case (inactive → 401). Phase 3 grows by SQLite-version assertion + `LIMITER_ACTIVE` flag rename.

**Open questions**: 0 new. All 4 round-0 OQs remain closed.

## Iteration round 1 — what changed and why

Stage 4 internal review (bob / xander / librarian / tessa / ian; all ITERATE) plus user-locked scope decisions. Tagged findings cited inline.

**Critical (1):**
- **bob:1 / xander:32 / ian-#1** — the `login_rate_limiter()` factory called inside `dependencies=[...]` resolves at module-import time, so D2's graceful-degrade flag is dead code (Redis-down at boot → every login 500s). **Fix**: drop the factory; introduce a plain async dep `login_rate_limit_dep` that reads `RATE_LIMIT_DEGRADED` and `get_config()` at request time. Phases 3 + 4 updated.

**High (5):**
- **librarian:2 / tessa:5 / ian-#3** — Phase 1 was about to duplicate `TestRagBypassPublicEndpointsProtected` at `test_security_hardening.py:422-453`. The existing class already covers no-key (422, FastAPI's missing-required-`Header(...)` shape) and wrong-key (401) for both routes. Phase 1 re-scoped: append the missing positive `correct_key → 200` cases to the existing class. Pseudocode corrected (422 vs 401).
- **xander:34/35/36 (USER-LOCKED SCOPE EXPANSION)** — three additional unauthenticated `/public/*` endpoints found during plan review must be auth-gated this campaign: `cloud_providers.py:629` (`/public/enabled`), `cloud_providers.py:643` (`/public/{provider}/config`), `site_scraper.py:58` (`/public`), `tool_calling.py:241` (`/tools/public`). Phase 1 graduates from "warm-up regression test" to a small implementation phase: add `Depends(verify_service_api_key)` to all four (rag_service_bypass already done), with regression tests for each. For `tool_calling.py` choosing auth gate (consistent with the others) over field-omit.
- **tessa:1 (BLOCKING)** — Phase 3's `TestRateLimiterStartup` fixture pattern unspecified. Plan now specifies the same subprocess / in-process split Campaign 2 uses at `test_security_hardening.py:1031-1072`: subprocess for the Redis-down negative case (assert non-zero exit + log-grep), in-process with `fakeredis-aioredis` for the positive case (so `RATE_LIMIT_DEGRADED` can be read back).
- **tessa:3** — Phase 2's migration test needs an explicit 053-state DB fixture, otherwise `Base.metadata.create_all()` in conftest already creates the new columns and the backfill assertion is meaningless. Phase 2 now spells out: fresh SQLite engine → `alembic.command.upgrade(config, "053")` → insert User row → `alembic.command.upgrade(config, "054")` → assert backfill.
- **xander:33** — `_init_rate_limiter()` referenced module-level `redis_client` defined only in production `else` branch; risks `NameError` from test contexts. Phase 3 now passes `redis_client` as parameter (or uses `globals().get("redis_client")` defensively).

**Medium (addressed):**
- **xander:37** — lockout-DoS gap (attacker locks known-username accounts) acknowledged in D3 but lacked an actionable mitigation path. Now: file ATHENA follow-up for admin-unlock CLI + lockout email notification; reference in Out-of-scope. Not shipped this campaign.
- **xander:38** — 200ms floor too low for 600k PBKDF2. Two combined fixes: (1) raise `LOGIN_FAILURE_DELAY_MS` default to **400ms** in `AthenaConfig` (D6 + D8 updated); (2) compute a constant pre-baked dummy-hash via `verify_password` on the user-not-found path so the cost is paid even when no user exists ("constant-time user lookup" pattern). Phase 4 updated.
- **xander:39 / tessa:2** — TOCTOU on `failed_login_count` increment: ORM read-modify-write is racy under concurrent failed logins. Phase 4 switches to atomic `UPDATE users SET failed_login_count = failed_login_count + 1 WHERE id = ?` followed by `db.refresh(user)`. New test `test_concurrent_failures_do_not_double_increment` using `asyncio.gather`.
- **xander:40** — `service_auth.SERVICE_API_KEY` captured at module import. Phase 1 tests using `monkeypatch.setenv` won't take effect. Refactor to read at call-time inside `verify_service_api_key` (also enables runtime key rotation; minor).
- **tessa:4** — wall-time tests will flake against real PBKDF2. Phase 4 fixtures override iterations: `password_hash = hash_password(password, iterations=1000)`.
- **tessa:7 / xander:41** — `DateTime(timezone=True)` on SQLite stores text without TZ awareness; round-trip can yield naive datetimes. Phase 4 fixtures must `db.commit(); db.refresh(user)` after setting `locked_until` and use `datetime.now(timezone.utc)` consistently on both sides of comparisons.
- **bob:3 / bob:4** — `_init_rate_limiter()` placement clarified: inside the production `else` branch in `startup_event`, after `_enforce_oidc_runtime_gates()`, BEFORE the `OSS_AUTO_PULL_MODELS` block. 401-vs-403 asymmetry (locked → 401, inactive → 403) flagged in plan; inactive untouched (out of scope).
- **ian-#2 (OQ4 resolution)** — frontend 429 UX: `admin/frontend/auth.js:215` shows generic "Login failed" because fastapi-limiter returns `{"error": "Too Many Requests"}` (no `detail`). One-line fix `data.detail || data.error || 'Login failed'` shipped in Phase 4 (with the response shape change).
- **ian-#4** — `database.py:243` `seed_dev_data()` still uses naive `datetime.utcnow()`. Latent comparison hazard with new tz-aware `locked_until`. Tracked in Out-of-scope (deferred utcnow deprecation campaign).
- **ian-#6** — `manifests/athena-prod/config.yaml` lacks the new `LOGIN_*` env-var stubs. Phase 5 adds commented entries.
- **ian-#7** — Phase 4 test fixtures need `db.refresh(user)` after `db.add()` or assertions on `failed_login_count` will see Python-side `None`.

**Low / nit (folded inline):**
- **tessa:6** — added a Phase-4 case asserting 429-from-limiter does NOT increment `failed_login_count`.
- **bob:5 / bob:6** — Postgres/SQLite `server_default` parity (doc note). `fastapi-limiter` version pin verified at impl time (OQ1 unchanged).

**Open questions resolved:**
- **OQ1 (`fastapi-limiter` version)** — close; pin verified at impl time; behavior unchanged.
- **OQ2 (DEV_MODE test path)** — close; ship `fakeredis-aioredis` as test-only dep (mirrors Campaign 2 HIGH-E precedent).
- **OQ3 (locked response code)** — **close → 401 generic** (`"Invalid username or password"`). Unanimous from bob + librarian + xander + tessa. D4 cases 4/5 updated to assert 401.
- **OQ4 (frontend 429 UX)** — close → ship the one-line frontend fix in Phase 4 alongside the response-shape change.

**Phase-count change**: 5 → 5 (unchanged). Phase 1 is heavier (4 endpoints + tests, vs. 1 endpoint regression-only). No phases added/removed.

## Goal

Stop online password-guessing and account enumeration against the local-login endpoint, and make the per-account brute-force window cost the attacker time and DB state instead of being free. After this campaign: a single IP gets at most 5 `POST /api/auth/local-login` attempts per minute (Redis-backed token bucket via `fastapi-limiter`), every failed attempt across all callers spends a fixed **400 ms** minimum (timing equalization to mask user-existence, inactive-user, and hash-cost differences — round-2 raised 200 → 400 per xander:38 / codex-r1), and any single user account locks for ~30 minutes after 10 cumulative failures (DB-tracked `failed_login_count` / `locked_until` columns). All four failure branches (user-not-found, inactive, wrong-password, locked) return identical generic 401 + `"Invalid username or password"` (round-2 Decision D — inactive was 403, now 401). The xander:11 finding the user prompt names is **already remediated at `f23b73a`** (commit `18c78e5`); this campaign documents that closure with a static-regression guard rather than re-implementing it.

## Critical scope correction (read before reviewing)

**xander:11 is already shipped at the campaign base.** The user's prompt instructs us to "add `X-Service-Key` verification on `GET /public/{name}/config` and `GET /public/enabled`." Reading the file at `f23b73a`:

- `admin/backend/app/routes/rag_service_bypass.py:180` — `GET /public/{service_name}/config` already has `_: bool = Depends(verify_service_api_key)`.
- `admin/backend/app/routes/rag_service_bypass.py:209` — `GET /public/enabled` already has `_: bool = Depends(verify_service_api_key)`.
- `admin/backend/app/utils/service_auth.py:25-50` — `verify_service_api_key` is the constant-time, fail-closed dependency; identical pattern to `internal.py:38`.
- `git show 18c78e5` (May 5, 2026, "fix(audit): phase 5 — auth hardening on admin endpoints") added both dependencies in the same commit that ships ATHENA-7 phase 5.

The audit (dated `2026-05-05`) was written against an earlier tree; ATHENA-7's phase 5 closed it the same day. Re-implementing the dependency is a no-op. This plan instead:

1. **Adds a regression test** to `admin/backend/tests/test_security_hardening.py` that asserts `GET /api/rag-service-bypass/public/{name}/config` and `GET /api/rag-service-bypass/public/enabled` return 401 without `X-Service-Key` and 200 with the correct key — so a future refactor cannot accidentally drop the dependency.
2. **Refocuses xander:11's slice as Phase 1** (regression guard only — short, no implementation).
3. **Spends the campaign budget on xander:10**, which is the actual outstanding work and the source of the HEAVY tier (new dep + Redis runtime coupling + alembic migration).

This is consistent with the user-facts rule (CLAUDE.md Rule 3): the prompt's claim about xander:11 was based on the audit text, not the current tree. Verification overrides the prompt's pre-baked treatment.

If reviewers (bob / xander / codex) want a stricter response — e.g., add a separate `Depends(verify_service_api_key)` at the **router** level so any future `/public/*` route is auth-required by default — that's defensible and cheap. See **D7**.

## Public-route surface triage (round-2 user-locked Decision B)

Codex-r1 correctly noted: 39 unauthenticated `/public/*`-pattern routes exist in `admin/backend/app/routes/`. The campaign's "enumeration-protection / public-surface closure" claim is not honest unless every one is explicitly accounted for. This table classifies every route from `grep -rn -E '@router\.(get|post|put|delete|patch).*\bpublic\b' admin/backend/app/routes/`. **Scope ceiling unchanged**: only rows tagged `gated-this-campaign` are touched in Phase 1.

| Route (`file:line`) | Method + path | Class | Justification |
|---|---|---|---|
| `rag_service_bypass.py:176` | GET `/public/{service_name}/config` | already-gated | `Depends(verify_service_api_key)` shipped at `f23b73a` (commit `18c78e5`); Phase 1 adds positive regression test only. |
| `rag_service_bypass.py:206` | GET `/public/enabled` | already-gated | Same — gated at `f23b73a`; Phase 1 adds regression. |
| `cloud_providers.py:629` | GET `/public/enabled` | gated-this-campaign | Reveals which cloud LLM providers (OpenAI/Anthropic/etc.) are enabled — feeds attacker reconnaissance. xander:34. |
| `cloud_providers.py:643` | GET `/public/{provider}/config` | gated-this-campaign | Returns provider config (model lists, base URLs); orchestrator-only consumer. xander:35. |
| `site_scraper.py:58` | GET `/config/public` | gated-this-campaign | Returns site-scraper allowlist + scraping config; only consumed by RAG-news. xander:36. |
| `tool_calling.py:241` | GET `/tools/public` | gated-this-campaign | Returns full tool registry incl. internal tool names + parameter schemas; orchestrator-only consumer. xander (named in iteration round 1). |
| `tool_calling.py:765` | POST `/metrics/record` | gated-this-campaign | Write endpoint accepting arbitrary JSON to `ToolUsageMetric` table; no prior auth. Attack surface: metric-table flood, dashboard misinformation, potential stored-XSS via `error_message`/`intent` fields. Missed in initial triage. xander:41 (phase-1 reconcile). |
| `alerts.py:87` | POST `/public/create` | defer-to-follow-up | **Write endpoint** — accepts `AlertCreate` from internal services. Auth-gating requires identifying every alert-emitter caller (orchestrator + RAG services + Control Agent) and threading `X-Service-Key`. Higher implementation cost than read-side gates; needs its own design. **Plane ticket: ATHENA-15 (file during Phase 5).** |
| `alerts.py:139` | POST `/public/resolve-by-entity` | defer-to-follow-up | **Write endpoint** — same caller-audit cost as `:87`. Track under same ATHENA-15. |
| `alerts.py:178` | GET `/public/active-by-type` | defer-to-follow-up | Read-only but coupled to alert-write callers above; gate the cluster together under ATHENA-15. |
| `base_knowledge.py:115` | GET `/public` | intentionally-public | Read-only base knowledge (assistant-profile facts) consumed by orchestrator each query. Contents are non-sensitive (assistant persona text). Adding a service key would force every RAG service + orchestrator to thread it; not worth the change for non-sensitive data. |
| `intent_routing.py:694` | GET `/routing/public` | intentionally-public | Orchestrator polls intent routing config on startup; data is config-shape, not credentials. Same cost-of-gating argument as base_knowledge. |
| `intent_routing.py:725` | GET `/providers/public` | intentionally-public | Orchestrator-side provider routing; same as `:694`. |
| `intent_routing.py:806` | GET `/strategy/configs/public` | intentionally-public | Same as `:694`. |
| `mcp_security.py:122` | GET `/public` | intentionally-public | MCP allowlist consumed by services for outbound tool calls. **Note**: this surface is read-only and contents are domain allowlists — leak risk is low, gating risk is high (caller audit across MCP-using services). Track upgrade in deferred queue if a future audit raises sensitivity. |
| `model_config.py:196` | GET `/public` | intentionally-public | LLM model list consumed by gateway/orchestrator/llm-router each request. Hot path. Contents non-sensitive (model name + token-window). codex-r1 named. |
| `model_config.py:224` | GET `/public/{model_name:path}` | intentionally-public | Per-model config; same as `:196`. codex-r1 named. |
| `performance_presets.py:133` | GET `/public/active` | intentionally-public | Active perf preset (CPU/memory hints); consumed by orchestrator. Contents non-sensitive. |
| `escalation.py:152` | GET `/presets/public` | intentionally-public | Escalation preset metadata for orchestrator. |
| `escalation.py:164` | GET `/presets/active/public` | intentionally-public | Active escalation preset + rules; orchestrator hot-path. |
| `escalation.py:188` | GET `/state/{session_id}/public` | intentionally-public | Per-session escalation state; orchestrator-only consumer; session_id is opaque + already a capability token. |
| `gateway_config.py:136` | GET `/public` | intentionally-public | Gateway service self-config fetch on startup. |
| `features.py:134` | GET `/public` | intentionally-public | Feature-flag list for orchestrator/gateway. **Note**: `features.py:273` already imports `verify_service_api_key` for a sibling endpoint; re-evaluate as a fast-follow if `:134` warrants. |
| `llm_backends.py:129` | GET `/public` | intentionally-public | LLM backend (Ollama/MLX/etc.) inventory; consumed by every LLM call. Hot path. codex-r1 named. |
| `llm_backends.py:162` | GET `/public/mlx-applicability` | intentionally-public | MLX backend matchmaking data; hot path. codex-r1 named. |
| `external_api_keys.py:306` | GET `/public/{service_name}/key` | already-gated | Already declares `Depends(verify_service_api_key)` at `:310`; `include_in_schema=False` (out-of-OpenAPI). No change. |
| `external_api_keys.py:340` | GET `/public/{service_name}/credentials` | already-gated | Already declares dep at `:344`; `include_in_schema=False`. No change. |
| `component_models.py:110` | GET `/public` | intentionally-public | Per-component model assignments for orchestrator hot-path. |
| `settings.py:128` | GET `/assistant-profile/public` | intentionally-public | Assistant persona + voice config; runtime services hot-path. |
| `settings.py:1086` | GET `/privacy/public` | intentionally-public | Single boolean (analytics_mode_enabled); orchestrator polls. |
| `voice_interfaces.py:344` | GET `/public` | intentionally-public | Voice interface inventory; gateway hot-path. |
| `voice_interfaces.py:364` | GET `/public/{interface_name}` | intentionally-public | Per-interface config; gateway hot-path. |
| `voice_interfaces.py:433` | GET `/engines/public/stt` | intentionally-public | STT engine list; gateway hot-path. |
| `voice_interfaces.py:447` | GET `/engines/public/tts` | intentionally-public | TTS engine list; gateway hot-path. |
| `directions_settings.py:35` | GET `/public` | intentionally-public | Directions / RAG settings consumed by RAG-directions service. |
| `tool_calling.py:518` | GET `/tools/stats/public` | intentionally-public | Aggregate counts only (no per-tool detail); orchestrator status surface. codex-r1 named. |
| `tool_calling.py:552` | GET `/settings/public` | intentionally-public | Tool-calling global settings; orchestrator hot-path. codex-r1 named. |
| `tool_calling.py:648` | GET `/triggers/public` | intentionally-public | Tool-calling triggers (intent → tool mapping); orchestrator hot-path. codex-r1 named. |
| `tool_calling.py:971` | GET `/tools/{tool_id}/api-keys/public` | defer-to-follow-up | **Returns metadata about which API keys a tool needs** (key name + whether-required). Listing what keys exist is a recon signal; current callers thread `X-Service-Key` already to siblings. Track under ATHENA-15. codex-r1 named. |
| `tool_calling.py:994` | GET `/tools/by-name/{tool_name}/api-keys/public` | defer-to-follow-up | Same as `:971` (by-name variant). Track under ATHENA-15. codex-r1 named. |

**Counts**: 4 already-gated (rag_service_bypass x2 + external_api_keys x2) · 5 gated-this-campaign (xander:34/35/36 + tool_calling/tools/public + tool_calling/metrics/record[xander:41 reconcile]) · 5 defer-to-follow-up (alerts x3 + tool_calling api-keys x2 → ATHENA-15) · 26 intentionally-public.

**ATHENA-15 (deferred)** — file during Phase 5. Body: "Audit alert-write endpoints (`alerts.py:87`, `:139`, `:178`) and tool-calling api-key-listing endpoints (`tool_calling.py:971`, `:994`) for service-key gating. Each requires identifying every internal caller and threading `X-Service-Key` through them. Out of scope for ATHENA-14 (Campaign 3) which gates only the cheap-cost read-side cluster."

**Closure claim** (revised after this triage): the campaign closes the **xander:10/11/34/35/36 named** public-route gaps and the cheap-cost read-side cluster on tool_calling. It does NOT claim "every public route is gated" — the `intentionally-public` rows are accepted as service-to-service config-fetch surface with non-sensitive contents, and `defer-to-follow-up` rows are tracked under ATHENA-15.

## Risk during change

| Finding | Risk during this change | Likelihood | Blast radius | Mitigation in this plan |
|---|---|---|---|---|
| **xander:10 — new `fastapi-limiter` dep** | `fastapi-limiter` requires `await FastAPILimiter.init(redis)` in startup; if init fails (Redis unreachable at boot) and the route declares `Depends(RateLimiter(...))`, every request to that route raises 500 instead of 429 because the limiter isn't initialized. | Medium for fresh OSS deployers whose Redis isn't up before admin-backend starts. Low in Jay's homelab (Redis is a sibling pod with `restartPolicy: Always`). | A failed-init means local-login is broken (500) until admin-backend restarts. Worst case: the admin user is locked out of the admin UI on a fresh deploy. | (1) D1 picks `init` placement: inside the existing `@app.on_event("startup")` AFTER the OIDC gates run, BEFORE `_enforce_oidc_runtime_gates`. (2) Init is wrapped in a try/except: on Redis-init failure, log CRITICAL and skip limiter wiring (the route still works, but rate limiting is degraded — the lockout-counter half is independent and still defends). (3) DEV_MODE skips the limiter entirely (the in-memory-session branch already skips Redis at `:103-107`); local dev does not require Redis. (4) Phase 3 verification asserts both the success path AND the Redis-down degraded path. |
| **xander:10 — new DB columns on `users`** | Alembic migration adds `failed_login_count INT NOT NULL DEFAULT 0` and `locked_until TIMESTAMP NULL`. On large `users` tables this is a brief table-lock under PostgreSQL; the OSS deployer's table is small (<1000 rows typical), so latency is negligible. The risk is migration-ordering: the down-revision must chain off `053`, not the legacy `004a` branch. | Low. | Migration fails to apply, admin-backend crashes on startup with `alembic.util.exc.CommandError`. Operator runs `alembic upgrade head` manually. | (1) Phase 2 numbers the migration `054_add_user_lockout_columns.py` and explicitly chains `down_revision = "053"` (mirroring `053`'s convention at line 23). (2) Both new columns are NOT-NULL with safe DB-side defaults (`server_default="0"` for `failed_login_count`, `nullable=True` for `locked_until`). Existing rows get the default at migration time without manual backfill. (3) Migration `downgrade()` drops both columns (reversible). (4) Phase 2 verification runs the migration on a fresh SQLite test DB AND asserts existing rows get the default value. |
| **xander:10 — login-handler complexity** | The login handler currently has 4 short branches (user-not-found / inactive / wrong-password / success). Adding lockout state, counter increment/reset, fixed-delay enforcement, and rate-limit interaction grows it to ~9 branches. Risk: a logic error in the new branches lets a locked user authenticate, or a successful-login path forgets to reset the counter, leaving a real user locked out after 10 wrong attempts spread across weeks. | Medium without tests. Low with the test contract in Phase 4. | Real user perma-locked OR locked user authenticated. | (1) Phase 4 ships ~8 behavioral pytest cases (Decision D4 names them) covering each branch, including the edge cases (lockout-expiry-during-login-attempt, counter-reset-on-success, locked-user-correct-password rejection). (2) D5 picks "reset on success AND reset on lockout-expiry" — single rule, easy to test. (3) Phase 4's commit explicitly does not change the 200ms-delay constant; constant lives in `AthenaConfig`. |
| **xander:10 — timing-attack mitigation** | Fixed 200 ms minimum applies to **failed** attempts only. The risk: applying it to **successful** attempts too would slow down every login by 200 ms (annoying); applying it to none means a username-existence oracle remains (user-not-found returns ~10 ms, user-found-wrong-password returns ~600 ms PBKDF2 cost; ~590 ms gap). | Medium for the oracle in absence of fixed delay. | Attacker enumerates which usernames exist via timing, then targets only-real-users with the per-IP rate limit (5/min) and per-account lockout (10 cumulative). | D6 picks "apply 200 ms minimum to ALL failure paths (user-not-found, inactive, wrong-password, locked) so all four take the same wall time." Successful login is unaffected (no delay). |
| **Cross-finding** | Phase 1 (xander:11 regression test) ships before Phase 2 (migration) before Phase 3 (limiter wiring) before Phase 4 (lockout logic). A partial campaign that ships only Phase 1+2 leaves users with new DB columns but no code that reads them — harmless, the columns hold their defaults. A partial campaign that ships Phase 1+2+3 has rate-limiting but no lockout — partial defense, still net-positive. Phase 4 is the largest single-commit code change (handler rewrite). | Low — campaign is shipped as a unit; partial deploys would only happen on hand-cherry-pick. | None on the campaign branch. | Phase ordering is bisectable; each phase compiles, has tests, runs cleanly with the others reverted. |

## Decisions

### D1 — Where does `await FastAPILimiter.init(redis)` go?

- **Decision**: Inside the existing `@app.on_event("startup")` (`admin/backend/main.py:293-518`), in a new helper `_init_rate_limiter()`, called AFTER `_enforce_oidc_runtime_gates()` (line 504) and BEFORE `ensure_default_model()` (line 509). DEV_MODE skips it. Wrapped in try/except — on failure, log CRITICAL and continue (route-level `Depends(RateLimiter)` will then raise — see D2).
- **Options**:
  - **(A)** Inside `startup_event`, in the `else` (production) branch, after `_enforce_oidc_runtime_gates()`. **Recommended.**
  - **(B)** Migrate the entire startup hook to the modern `lifespan` context manager. Cleaner long-term but a larger refactor that changes dozens of line numbers and breaks the existing test pattern (`with TestClient(app):` already calls lifespan and `on_event("startup")` is invoked by lifespan automatically — both work with `TestClient`).
  - **(C)** Lazy-init on first request via FastAPI `Depends`. Works but introduces a per-request Redis-readiness check; the audit recommends startup init.
- **Recommendation**: **(A)**. Mirrors the existing pattern; doesn't fight the codebase. (B) is a separate refactor for a later campaign.
- **Tradeoff if we pick differently**: (B) is invasive and unrelated. (C) makes the failure mode "first attacker request hits Redis init race" rather than "boot-time failure surfaces in logs" — worse observability.

### D2 — Behavior when `FastAPILimiter.init` fails (Redis unreachable at boot)

- **Decision (revised after bob:1 / xander:32 / ian-#1, then again after codex-r1)**: On failure, log `logger.critical("rate_limiter_init_failed", error=str(e))` and **leave `LIMITER_ACTIVE` False** (it defaults False; success path sets it True). The login route declares `dependencies=[Depends(login_rate_limit_dep)]` where `login_rate_limit_dep` is a **plain async dep function** that reads `LIMITER_ACTIVE` at request time and either no-ops or invokes `RateLimiter(times=..., seconds=60)(request, response)` directly. **Round-1 picked a negative-semantics flag (`RATE_LIMIT_DEGRADED`)**; codex-r1 correctly observed this broke DEV_MODE (init never ran, flag stayed False, dep tried to use uninitialized `FastAPILimiter`). **Round-2 collapses to a single positive-semantics flag (`LIMITER_ACTIVE`)** that is True only when init succeeded — every other state (DEV_MODE skipped init, prod init failed) leaves it False and the dep no-ops. The factory pattern in the round-0 draft was also broken (Python evaluated `dependencies=[login_rate_limiter(...)]` at module import, baking in whichever flag value existed then); the dep-function form resolves the flag per-request, making D2's graceful degrade actually work.
- **Options**:
  - **(A)** Log + degrade (above). Login still works; per-IP rate limiting silently disabled until Redis recovers + admin-backend restarts.
  - **(B)** Log + raise SystemExit. Admin-backend won't boot without Redis. Coherent with the "fail closed" stance, but Redis is already required for sessions in production, so admin-backend already wouldn't accept logins without it — the SessionMiddleware lookup raises before login ever runs. SystemExit is redundant.
  - **(C)** Log + retry loop. New code path with its own bugs; no current Athena pattern for boot-time retries.
- **Recommendation**: **(A)** because of the redundancy in (B). Production admin-backend with no Redis is already non-functional (sessions fail), so the rate-limit degrade is a no-op in practice — no admin user can reach the login endpoint to test the degrade path. The flag exists for clarity in logs, not for a real ops scenario. The lockout-counter remains effective regardless.
- **Tradeoff if we pick differently**: (B) duplicates SessionMiddleware's failure mode with a less-clear error message. (C) adds new failure modes (retry timeout, retry storm).

### D3 — Per-IP, per-username, or both?

- **Decision**: **Per-IP only at the `fastapi-limiter` layer** (5 attempts/minute/IP). **Per-username at the lockout layer** (10 cumulative failures → 30-minute lock). The two compose: a single attacker IP gets 5 tries/minute against any account; a single account locks after 10 total failures from any IP combination. Names hosted in `AthenaConfig`.
- **Options**:
  - **(A)** Per-IP only at limiter, per-username at lockout (recommended).
  - **(B)** Per-IP AND per-username at the limiter (`fastapi-limiter` supports keying via custom callable). Pros: directly mitigates IP-rotation attacks against one account. Cons: `fastapi-limiter`'s key callable runs synchronously in the dependency; reading `payload.username` from the request body requires re-buffering the request body, which is fragile under FastAPI's body-stream model. Cleaner solution would be an explicit pre-handler middleware, doubling the surface area.
  - **(C)** Per-username only. Throws away the cheap IP-based defense for no gain.
- **Recommendation**: **(A)**. The audit text says "5 attempts/min/IP" — IP is the audit's recommendation. The IP-rotation gap (attacker rotates IPs, hits one account) is closed by the lockout layer (10 cumulative failures = lock), so the composition delivers the required defense without (B)'s body-buffering risk. The lockout's blast radius (real-user lockout) is acceptable because the lockout is time-bounded (30 min default), the threshold is well above legitimate finger-fumbles (10 attempts), and an admin can manually clear `failed_login_count` via `psql`.
- **Tradeoff if we pick differently**: (B) needs body-buffering middleware OR moving username extraction into a parameter the route extracts pre-limiter — either way more code. (C) leaves the cheap IP-defense unused.

### D4 — Test contract for the lockout state machine (named cases for jackson)

- **Decision**: Phase 4 ships these 11 behavioral pytest cases against `TestClient(app)` with `fakeredis[lua]` and a real SQLite DB:
  1. `test_local_login_success_resets_failed_count` — user with `failed_login_count=3` logs in correctly → `failed_login_count=0`, `locked_until=NULL`.
  2. `test_local_login_wrong_password_increments_count` — user with `failed_login_count=2` and wrong password → `failed_login_count=3`, `locked_until=NULL`, response 401.
  3. `test_local_login_tenth_failure_locks_account` — user with `failed_login_count=9` and wrong password → `failed_login_count=10`, `locked_until ≈ now + 30 min`, response 401.
  4. `test_locked_user_correct_password_rejected` — user with `locked_until > now()` and correct password → response **401** (`detail="Invalid username or password"` — generic, per OQ3 resolution), `failed_login_count` unchanged.
  5. `test_locked_user_after_expiry_correct_password_succeeds` — user with `locked_until < now()` and correct password → response 200, `failed_login_count=0`, `locked_until=NULL`.
  6. `test_unknown_username_minimum_delay_enforced` — POST with `username="nonexistent"` → response 401, response wall-time ≥ 360 ms (400 ms floor minus 40 ms test-runner slack).
  7. `test_failed_login_minimum_delay_enforced` — POST with valid username, wrong password → response 401, wall-time ≥ 360 ms.
  8. `test_successful_login_no_delay` — POST with valid creds → response 200, wall-time < 100 ms (PBKDF2 with `iterations=1000` test override per tessa:4).
  9. `test_concurrent_failures_do_not_double_increment` (xander:39) — 5 concurrent wrong-password POSTs via `asyncio.gather` against `failed_login_count=0` → final count is exactly 5, not 1 or 2 (atomic UPDATE behavior).
  10. `test_429_does_not_increment_failed_count` (tessa:6) — exhaust the per-IP limiter with bad-username calls; once 429 is returned, assert the user's `failed_login_count` is unchanged (the dep-injected limiter rejects the request before the handler runs).
  11. `test_inactive_user_returns_generic_401` (round-2 user-locked Decision D / codex-r1 MEDIUM) — user with `active=False` and correct password → response **401** with `detail="Invalid username or password"` (NOT 403, NOT `"User account is inactive"`), `failed_login_count` unchanged. Closes the inactive-user enumeration oracle. Pairs with case 6 (unknown username) and case 4 (locked user) — all three indistinguishable to an attacker.
- **Options**:
  - **(A)** All 11 cases (recommended).
  - **(B)** Skip cases 6/7/8 (timing assertions are flaky on shared CI). Drop the timing test, document the contract.
  - **(C)** Skip cases 1/5 (counter-reset is "trivially correct" — false; this is exactly where bugs live).
- **Recommendation**: **(A)**. Timing assertions are loose-bound; 360 ms floor on a 400 ms target gives 40 ms of test-runner slack. If CI proves flaky, raise the floor to 320 ms before deleting the test. Counter-reset is the bug-prone path (lockout that never clears = perma-lock).
- **Tradeoff if we pick differently**: (B) kills the timing-equalization test, leaving xander:10's fixed-delay claim unverified. (C) removes the counter-reset test that protects real users.

### D5 — When does `failed_login_count` reset?

- **Decision**: Reset on (a) successful login; (b) lockout expiry (lazy: detected at the next login attempt — when the handler sees `locked_until < now()`, it clears both `failed_login_count` and `locked_until` before evaluating the password); (c) admin manual reset (already supported by direct `UPDATE users SET failed_login_count=0` in `psql`; no new endpoint needed in this campaign).
- **Options**:
  - **(A)** Reset on success + reset on lazy-expiry (recommended).
  - **(B)** Reset on success only; lockout never expires automatically. Forever-lock unless admin clears manually. Forces operator intervention; bad UX for the homelab deployer.
  - **(C)** Reset on success + scheduled background task that scans `users` periodically and clears expired locks. Adds a new background job for negligible benefit over (A).
- **Recommendation**: **(A)**. Lazy-expiry is the standard pattern (read-time check); no new background machinery; testable.
- **Tradeoff if we pick differently**: (B) requires admin knowledge of `psql`. (C) adds operational surface area.

### D6 — Fixed delay applies to which paths?

- **Decision (revised after xander:38)**: **400 ms** minimum (raised from 200 ms — production PBKDF2-600k timings are 150–400 ms; 200 ms floor undershoots the natural cost so the floor never fires). Applies to ALL failure paths: user-not-found, user-inactive, password-wrong, account-locked. NOT applied to success. Implemented as `time.monotonic()`-anchored sleep at the END of each failure branch. Combined with a **dummy-hash compute on user-not-found**: a module-level constant `_DUMMY_PBKDF2_HASH` is verified against the submitted password so even unknown usernames pay the PBKDF2 cost. This is the standard "constant-time user lookup" pattern; the 400 ms floor is the safety net for environments where PBKDF2 runs faster (test mode, future bcrypt switch).
- **Options**:
  - **(A)** Equalize all failure paths to ≥ 200 ms (recommended).
  - **(B)** Equalize only "user not found" + "inactive" (the cheap branches). Leaves the wrong-password branch at its natural ~50–100 ms, still distinguishable from the cheap branches' new ~200 ms floor — defeats the purpose.
  - **(C)** No fixed delay; rely on rate limit + lockout. Audit explicitly names the 200 ms minimum.
- **Recommendation**: **(A)**. Equalization is only useful if every failure spends the same wall time.
- **Implementation note**: anchor at handler start (`start = time.monotonic()`), at every failure branch compute `elapsed = time.monotonic() - start; floor = cfg.login_minimum_delay_ms / 1000.0; if elapsed < floor: await asyncio.sleep(floor - elapsed)` before raising. Floor is **400 ms** per round-2 D8 / xander:38 / codex-r1. This avoids ALWAYS adding 400 ms to the natural cost — the natural cost already counts.
- **Tradeoff if we pick differently**: (B) keeps the timing oracle. (C) ignores the audit's explicit recommendation.

### D7 — Should xander:11 add a router-level `Depends(verify_service_api_key)` for defense-in-depth?

- **Decision**: **No, but add a regression test.** The two `/public/*` routes already declare the dependency at the route level. A router-level addition would be defense-in-depth but would also (a) require checking that no future route on this router needs to be unauthenticated — refactoring the router shape; and (b) duplicate the dependency check on every request (negligible perf cost, cosmetic concern). A test that explicitly asserts the 401-without-key behavior at HEAD is a stronger forward guarantee than a router-level dep that someone could later remove without noticing.
- **Options**:
  - **(A)** Regression test only (recommended).
  - **(B)** Regression test + router-level `dependencies=[Depends(verify_service_api_key)]` — but then the non-`/public/*` routes (`GET /`, `GET /{service_name}`, `PUT /{service_name}`, etc.) currently use `get_current_user` for user auth, NOT `verify_service_api_key`. Adding the service-key dep at the router level would require BOTH dependencies on every route, which doesn't match the design (user routes vs. service routes).
  - **(C)** Split the public routes into a sub-router with its own `dependencies=[]` declaration. Cleaner long-term but a router refactor unrelated to this campaign's scope.
- **Recommendation**: **(A)**. The audit's recommendation is satisfied at HEAD; the gap is verification, not implementation.
- **Tradeoff if we pick differently**: (B) is broken because the user-auth and service-auth dependencies don't compose cleanly at the router level. (C) is a router refactor for a follow-up.

### D8 — Where do new env vars live?

- **Decision**: `AthenaConfig` in `src/shared/config.py`. Three new fields:
  - `login_rate_limit_per_minute: int = Field(default=5)` — `fastapi-limiter` arg.
  - `login_lockout_threshold: int = Field(default=10)` — failures before lock.
  - `login_lockout_minutes: int = Field(default=30)` — lockout duration.
  - `login_minimum_delay_ms: int = Field(default=400)` — fixed-delay floor on failure (raised from 200 ms after xander:38: PBKDF2-600k natural cost is 150–400 ms).
- **Options**:
  - **(A)** All four fields in `AthenaConfig` (recommended; CLAUDE.md mandates this).
  - **(B)** Only the per-minute rate in config; hardcode the rest. Inconsistent with OSS-First (deployer can't tune lockout window without code change).
- **Recommendation**: **(A)**. CLAUDE.md "OSS-First Development" is non-negotiable.
- **Tradeoff if we pick differently**: (B) violates OSS-First and forces deployers to fork the code to relax lockout for high-traffic environments.

## Phase plan

### Phase 1 — Public-endpoint auth coverage: xander:11 regression + xander:34/35/36 implementation

- **What**: (1) Refactor `service_auth.py` to read `SERVICE_API_KEY` at call time instead of module import (xander:40). (2) Add `Depends(verify_service_api_key)` to the **4 endpoints classified `gated-this-campaign`** in the round-2 Public-route surface triage table — `cloud_providers.py:629`, `cloud_providers.py:643`, `site_scraper.py:58`, `tool_calling.py:241` (xander:34/35/36 + tool_calling). (3) Append positive-path regression cases to the existing `TestRagBypassPublicEndpointsProtected` class. (4) Add behavioral tests for each newly-protected endpoint following the same 422/401/200 pattern.
- **Findings addressed**: xander:11 (regression guard), xander:34, xander:35, xander:36 (new auth coverage), xander:40 (call-time env read), librarian:2 / tessa:5 / ian-#3 (no test-class duplication), **codex-r1 HIGH "public-route scope is not actually closed"** (closed via the round-2 triage table — every `/public/*` route accounted for; only 4 gated, 5 deferred to ATHENA-15, 26 classified intentionally-public with justification).
- **Files**:
  - `admin/backend/app/utils/service_auth.py:24` — remove module-level `SERVICE_API_KEY = get_config().service_api_key`; read inside `verify_service_api_key` per call:
    ```python
    def verify_service_api_key(x_service_key: str = Header(..., alias="X-Service-Key")) -> bool:
        service_key = get_config().service_api_key
        if not service_key:
            raise HTTPException(503, "Service authentication not configured")
        if not hmac.compare_digest(x_service_key, service_key):
            logger.warning("service_api_key_invalid", key_prefix=x_service_key[:8] if x_service_key else "empty")
            raise HTTPException(401, "Invalid service key")
        return True
    ```
  - `admin/backend/app/routes/cloud_providers.py:629` — add `, _: bool = Depends(verify_service_api_key)` to `get_enabled_providers` signature.
  - `admin/backend/app/routes/cloud_providers.py:643` — add same dep to `get_public_provider_config`.
  - `admin/backend/app/routes/site_scraper.py:58` — add same dep to the public config route.
  - `admin/backend/app/routes/tool_calling.py:241` — add same dep to `/tools/public`. (Decision: auth gate over field-omit, consistent with the others. xander noted both options; auth gate is uniform.)
  - `admin/backend/tests/test_security_hardening.py` — extend the existing `TestRagBypassPublicEndpointsProtected` class with two positive-path cases; append three new sibling classes for the new endpoints.
- **Implementation notes**:
  - **Existing class extension** (do NOT create `TestRagServiceBypassPublicAuth` — duplicate). Append to `TestRagBypassPublicEndpointsProtected` at `test_security_hardening.py:422`:
    ```python
    def test_bypass_config_correct_key_accepted(self, app_client, monkeypatch):
        # SERVICE_API_KEY is set by the app_client fixture; service_auth reads
        # at call time per xander:40 refactor, so monkeypatch.setenv works here.
        r = app_client.get(
            "/api/rag-service-bypass/public/weather/config",
            headers={"X-Service-Key": app_client.app.state.service_api_key_for_tests},
        )
        # 200 + bypass disabled body (no rag_service_bypass_config row in test DB).
        assert r.status_code == 200

    def test_bypass_enabled_correct_key_accepted(self, app_client):
        r = app_client.get(
            "/api/rag-service-bypass/public/enabled",
            headers={"X-Service-Key": app_client.app.state.service_api_key_for_tests},
        )
        assert r.status_code == 200
    ```
    (`app_client.app.state.service_api_key_for_tests` is added in conftest if not already present; otherwise read directly from `get_config().service_api_key` after the fixture sets the env var.)
  - **New classes** (one per newly-protected endpoint), each with three cases mirroring the existing pattern:
    - `class TestCloudProvidersPublicEnabledProtected` → no key → 422 (FastAPI's missing-required-`Header(...)` shape, NOT 401), wrong key → 401, correct key → 200.
    - `class TestCloudProvidersPublicConfigProtected` → same three cases against `/api/cloud-providers/public/openai/config`.
    - `class TestSiteScraperPublicConfigProtected` → same three cases against `/api/site-scraper/config/public`.
    - `class TestToolCallingPublicProtected` → same three cases against `/api/tool-calling/tools/public`.
  - **Pseudocode correction**: the original draft asserted no-key → 401. FastAPI's `Header(...)` (required, no default) returns **422 with `{"detail": [{"loc": ["header", "x-service-key"], "msg": "Field required", ...}]}`** when the header is missing; only a present-but-wrong header reaches `verify_service_api_key` and gets 401. Existing class at `:422-453` is correct on this; new tests must match.
- **Reversible**: yes — revert four route signatures, the `service_auth.py` refactor, and the test additions.
- **Verification (commands jackson runs)**:
  ```bash
  cd admin/backend
  pytest tests/test_security_hardening.py::TestRagBypassPublicEndpointsProtected -v
  pytest tests/test_security_hardening.py::TestCloudProvidersPublicEnabledProtected -v
  pytest tests/test_security_hardening.py::TestCloudProvidersPublicConfigProtected -v
  pytest tests/test_security_hardening.py::TestSiteScraperPublicConfigProtected -v
  pytest tests/test_security_hardening.py::TestToolCallingPublicProtected -v
  pytest tests/test_security_hardening.py -v   # full file passes
  pytest -v                                     # full backend regression
  ```
  All cases pass; no other test class breaks.
- **Out of scope**:
  - Router-level dependency injection (D7 option B).
  - Sub-router refactor for public routes (D7 option C).
  - Audit of remaining `/api/internal/*` for X-Service-Key coverage (separate ticket).
  - Field-level redaction on `/tools/public` (chose auth-gate; field-omit deferred unless a downstream caller needs an unauthenticated subset).
  - **Public-route rows tagged `defer-to-follow-up` in the round-2 triage table** — `alerts.py:87/139/178`, `tool_calling.py:971/994`. Tracked under **ATHENA-15**, filed during Phase 5.
  - **Public-route rows tagged `intentionally-public`** — 26 rows. Service-to-service config-fetch surface with non-sensitive contents; per-row justifications in the triage table.
- **Commit**: `feat(auth-rate-limit-bypass): phase 1 — auth-gate /public/* endpoints + regression tests (xander:11/34/35/36/40, ATHENA-14)`

### Phase 2 — xander:10a — Alembic migration: add `failed_login_count` and `locked_until` to `users`

- **What**: Add two columns to the `users` table via a new alembic migration `054_add_user_lockout_columns.py`. Update the `User` SQLAlchemy model in `admin/backend/app/models.py`. No code reads these columns yet (Phase 4 wires them in).
- **Findings addressed**: xander:10 (DB-state half).
- **Files**:
  - `admin/backend/alembic/versions/054_add_user_lockout_columns.py` — new migration. `down_revision = "053"`, `revision = "054"`.
  - `admin/backend/app/models.py:74` — add `failed_login_count = Column(Integer, nullable=False, default=0, server_default='0')` and `locked_until = Column(DateTime(timezone=True), nullable=True)` immediately after `last_login`.
- **Implementation notes**:
  ```python
  # 054_add_user_lockout_columns.py
  """Add failed_login_count and locked_until to users for xander:10 lockout.

  Revision ID: 054
  Revises: 053
  Create Date: 2026-05-06
  """
  from alembic import op
  import sqlalchemy as sa

  revision = "054"
  down_revision = "053"
  branch_labels = None
  depends_on = None


  def upgrade() -> None:
      op.add_column(
          "users",
          sa.Column(
              "failed_login_count",
              sa.Integer(),
              nullable=False,
              server_default="0",
          ),
      )
      op.add_column(
          "users",
          sa.Column(
              "locked_until",
              sa.DateTime(timezone=True),
              nullable=True,
          ),
      )


  def downgrade() -> None:
      op.drop_column("users", "locked_until")
      op.drop_column("users", "failed_login_count")
  ```
  - `server_default='0'` ensures existing rows backfill at migration time without manual UPDATE (PostgreSQL applies the default to all existing rows on `ALTER TABLE`).
  - `nullable=True` for `locked_until` is correct (most users never get locked).
  - The model declaration uses `default=0` (Python-side) AND `server_default='0'` (DB-side) to keep ORM-creation behavior consistent with migration.
  - Mirror `053`'s convention: docstring at top, explicit `down_revision = "053"` (NOT chaining off `004a` legacy branch).
- **Reversible**: yes (alembic `downgrade` drops both columns; no data is at risk because Phase 2 doesn't write to either).
- **Verification (commands jackson runs)**:
  ```bash
  # 1. Migration applies cleanly on a fresh SQLite DB (the test DB).
  cd admin/backend
  rm -f /tmp/test_054.db
  DATABASE_URL=sqlite:////tmp/test_054.db alembic upgrade head
  # Confirm: alembic current shows 054.

  # 2. Schema check — the new columns exist with the right shape.
  python3 -c "
  import sqlite3
  c = sqlite3.connect('/tmp/test_054.db')
  rows = c.execute('PRAGMA table_info(users)').fetchall()
  cols = {r[1]: r for r in rows}
  assert 'failed_login_count' in cols, f'failed_login_count missing: {list(cols)}'
  assert 'locked_until' in cols, f'locked_until missing: {list(cols)}'
  # PRAGMA: (cid, name, type, notnull, dflt_value, pk)
  assert cols['failed_login_count'][3] == 1, 'failed_login_count must be NOT NULL'
  assert cols['locked_until'][3] == 0, 'locked_until must be NULLABLE'
  print('schema OK')
  "

  # 3. Existing-row backfill works.  Insert a row pre-migration would require
  #    a different test DB; use a behavioral pytest case instead:
  pytest tests/test_security_hardening.py::TestUserLockoutMigration -v
  ```
  **Tessa:3 — explicit 053-state fixture required.** The conftest's default `Base.metadata.create_all()` builds the HEAD schema (which, after Phase 2's models.py change, already includes the new columns). To verify backfill the test must roll forward through alembic, NOT use `create_all`:
  ```python
  # tests/test_security_hardening.py::TestUserLockoutMigration

  import os
  import tempfile
  from alembic import command
  from alembic.config import Config
  from sqlalchemy import create_engine, text

  def test_existing_user_gets_default_failed_count(tmp_path):
      """Pre-existing User row gets failed_login_count=0 / locked_until=NULL on 054 upgrade."""
      db_path = tmp_path / "migration_test.db"
      db_url = f"sqlite:///{db_path}"

      cfg = Config("alembic.ini")
      cfg.set_main_option("sqlalchemy.url", db_url)

      # 1. Roll forward to 053 (the pre-Phase-2 head).
      command.upgrade(cfg, "053")

      # 2. Insert a User row using the 053 schema (no failed_login_count column yet).
      engine = create_engine(db_url)
      with engine.begin() as conn:
          conn.execute(text("""
              INSERT INTO users (username, email, full_name, auth_provider, password_hash, role, active, created_at)
              VALUES ('alice', 'a@example.com', 'Alice', 'local', 'x', 'user', 1, CURRENT_TIMESTAMP)
          """))

      # 3. Apply 054.
      command.upgrade(cfg, "054")

      # 4. Verify backfill: existing row has the server_default applied.
      with engine.connect() as conn:
          row = conn.execute(text(
              "SELECT failed_login_count, locked_until FROM users WHERE username='alice'"
          )).one()
      assert row.failed_login_count == 0
      assert row.locked_until is None
  ```
  This pattern (alembic command API + `tmp_path`) avoids the `create_all()` shortcut and is the only way to verify the `server_default` actually fires on existing rows.

  ```bash
  # 4. Rollback works.
  DATABASE_URL=sqlite:////tmp/test_054.db alembic downgrade 053
  python3 -c "
  import sqlite3
  c = sqlite3.connect('/tmp/test_054.db')
  rows = c.execute('PRAGMA table_info(users)').fetchall()
  cols = {r[1] for r in rows}
  assert 'failed_login_count' not in cols, 'downgrade did not drop failed_login_count'
  assert 'locked_until' not in cols, 'downgrade did not drop locked_until'
  print('downgrade OK')
  "
  ```
- **Out of scope**:
  - Reading the new columns from any code (Phase 4).
  - Adding indexes on `locked_until` (no query reads it as a filter — handler reads per-user-by-username, then checks the field; no scan).
  - User-facing UI to view/clear lockout state (separate ticket; admin can `psql` for now per D5).
- **Commit**: `feat(auth-rate-limit-bypass): phase 2 — alembic 054 add failed_login_count/locked_until to users (xander:10, ATHENA-14)`

### Phase 3 — xander:10b — Add `fastapi-limiter` dep, wire startup init, add `AthenaConfig` fields

- **What**: Add `fastapi-limiter==0.1.6` to `admin/backend/requirements.txt`. Add the four new `login_*` fields to `AthenaConfig`. Add `_init_rate_limiter()` helper and a `_login_rate_limiter` route-level dependency. Wire `init` into `startup_event` after OIDC gates. No login-handler changes yet — Phase 4 attaches the dep to the route.
- **Findings addressed**: xander:10 (rate-limit-init half).
- **Files**:
  - `admin/backend/requirements.txt` — add `fastapi-limiter==0.1.6` (verified latest at the time of writing; confirm at implementation time with `pip index versions fastapi-limiter` or check PyPI).
  - `src/shared/config.py:170` (just before the "Deferred fields" comment) — add the four `login_*` fields.
  - `admin/backend/main.py:293-518` (inside `startup_event`) — add `await _init_rate_limiter()` call after `_enforce_oidc_runtime_gates()` (currently line 504), before `ensure_default_model()` (currently line 509). DEV_MODE branch: do NOT call (in-memory session, no Redis).
  - `admin/backend/main.py` — add `_init_rate_limiter()` helper (somewhere near `_enforce_oidc_runtime_gates`, ~line 290).
  - `admin/backend/app/utils/rate_limit.py` (NEW FILE) — define `LIMITER_ACTIVE: bool = False` module flag and `login_rate_limit_dep(request, response)` plain-async dep that no-ops when `not LIMITER_ACTIVE` and otherwise instantiates `RateLimiter(times=..., seconds=60)` per-request from `get_config()`. Round-2 codex-r1: positive-semantics flag (single truth condition for "limiter runs"), not the negative `RATE_LIMIT_DEGRADED` from round-1.
- **Implementation notes**:
  ```python
  # AthenaConfig additions (src/shared/config.py, after control_agent_enabled at :180):

      # ------------------------------------------------------------------
      # Login rate limit + lockout (Campaign 3 / ATHENA-14)
      # ------------------------------------------------------------------
      # Per-IP rate limit on POST /api/auth/local-login.  Backed by Redis
      # via fastapi-limiter; init runs at startup (admin-backend main.py).
      # Defaults match the audit recommendation (xander:10).
      login_rate_limit_per_minute: int = Field(default=5)
      # Cumulative-failure threshold before account locks (per-username, DB-tracked).
      login_lockout_threshold: int = Field(default=10)
      # Lockout duration in minutes.  Lazy-expiry: a locked user with locked_until < now()
      # is unlocked at the next login attempt (handler-side, no scheduled task).
      login_lockout_minutes: int = Field(default=30)
      # Fixed wall-time floor for failed login responses (timing-attack mitigation).
      # Applied to ALL failure paths (user-not-found, inactive, wrong-password, locked).
      # Successful login is NOT delayed.
      # Default raised 200 → 400 after xander:38 / codex-r1 (PBKDF2-600k natural cost
      # is 150–400 ms; 200 ms floor never fired in practice).
      login_minimum_delay_ms: int = Field(default=400)
  ```
  ```python
  # admin/backend/app/utils/rate_limit.py — NEW FILE
  """Login rate-limit helpers (xander:10).

  fastapi-limiter requires await FastAPILimiter.init(redis) at startup.
  This module exposes a request-time dependency that resolves the
  LIMITER_ACTIVE flag per-request (NOT at route-import time, per
  bob:1 / xander:32 / ian-#1 / codex-r1 HIGH-DEV_MODE).
  """
  from fastapi import Request, Response
  from fastapi_limiter.depends import RateLimiter
  import structlog
  from shared.config import get_config

  logger = structlog.get_logger()

  # Positive-semantics flag (round-2 codex-r1 fix):
  # - Default False so DEV_MODE (which never calls _init_rate_limiter) no-ops.
  # - Set True only on successful FastAPILimiter.init() in _init_rate_limiter.
  # - Stays False on Redis-init failure → degrade path also no-ops.
  # One flag covers both "never initialized" and "init failed" with a single
  # truth condition, easier to reason about than RATE_LIMIT_DEGRADED + a
  # separate _LIMITER_INITIALIZED ANDed at request time.
  LIMITER_ACTIVE: bool = False


  async def login_rate_limit_dep(request: Request, response: Response) -> None:
      """Per-request dep: rate-limit POST /api/auth/local-login.

      Resolves LIMITER_ACTIVE and login_rate_limit_per_minute at REQUEST time,
      so config + active state are live (not import-time-frozen).
      """
      if not LIMITER_ACTIVE:
          return  # No-op — DEV_MODE skipped init OR prod init failed; lockout layer still defends.
      cfg = get_config()
      limiter = RateLimiter(times=cfg.login_rate_limit_per_minute, seconds=60)
      await limiter(request, response)
  ```
  **Why a plain dep function, not a factory.** FastAPI evaluates `dependencies=[Depends(x)]` arguments at module import time. A factory `login_rate_limiter(times=...)` called there would freeze `LIMITER_ACTIVE` to whatever it was at import (always `False`, before `_init_rate_limiter` runs) and bake in a `RateLimiter` instance that subsequently raises when `FastAPILimiter` is uninitialized. The plain async dep resolves at request time and is the only correct pattern for runtime-flag-controlled dependencies.

  **Why `LIMITER_ACTIVE` (positive) and not `RATE_LIMIT_DEGRADED` (negative) — codex-r1 fix.** The round-1 design had `RATE_LIMIT_DEGRADED: bool = False` defaulting False, set True on init failure. Codex-r1 correctly noted: in DEV_MODE `_init_rate_limiter()` never runs, so the flag stays False and the dep tries to invoke `RateLimiter(...)` against an uninitialized `FastAPILimiter` — every DEV_MODE local-login 500s. Round-1 also considered a separate `_LIMITER_INITIALIZED` flag ANDed with `RATE_LIMIT_DEGRADED`, but two flags with three valid combinations (initialized+ok / initialized+degraded / uninitialized) is more state than needed. `LIMITER_ACTIVE` collapses this: the only truth condition that admits the limiter to run is "init ran successfully" — every other state (DEV_MODE never-ran, prod-Redis-failure init-ran-but-raised) leaves it False, dep no-ops, login still works.
  ```python
  # admin/backend/main.py — new helper after _enforce_oidc_runtime_gates:

  async def _init_rate_limiter(redis_conn) -> None:
      """Initialize fastapi-limiter against the production Redis.

      DEV_MODE skips this entirely (caller in startup_event guards with `if DEV_MODE`).
      On failure: log CRITICAL, leave LIMITER_ACTIVE=False, continue startup (D2).

      Takes redis_conn explicitly (xander:33) so the helper does not depend on
      module-level `redis_client` — that name is only bound in the production
      `else` branch, and a test that imports the helper directly would hit
      NameError otherwise.

      """
      from fastapi_limiter import FastAPILimiter
      from app.utils import rate_limit as rate_limit_mod

      try:
          await FastAPILimiter.init(redis_conn)
          rate_limit_mod.LIMITER_ACTIVE = True   # round-2: positive flag
          logger.info("rate_limiter_initialized")
      except Exception as e:
          logger.critical("rate_limiter_init_failed", error=str(e))
          # LIMITER_ACTIVE stays False — dep no-ops, login still works (D2 degrade).
  ```
  Wire-up in `startup_event` — **two distinct calls** (codex-r1b polish):

  **(1) SQLite capability check** at the TOP of `startup_event`, BEFORE the `if DEV_MODE:` branch — fires for both DEV_MODE and production paths because Phase 4's `UPDATE ... RETURNING` runs in either mode whenever `DATABASE_URL` is SQLite. Inline (no helper):
  ```python
      # Campaign 3 / ATHENA-14 — Phase 4 atomic increment uses UPDATE ... RETURNING (SQLite >= 3.35).
      # Fires for DEV_MODE local SQLite AND production SQLite-backed deploys.
      import sqlite3
      from urllib.parse import urlparse
      _db_url_scheme = urlparse(get_config().database_url or "").scheme.lower()
      if _db_url_scheme.startswith("sqlite") and sqlite3.sqlite_version_info < (3, 35, 0):
          raise RuntimeError(
              f"SQLite >= 3.35 required for UPDATE...RETURNING used by local-login lockout "
              f"(Campaign 3 / ATHENA-14); found {sqlite3.sqlite_version}. "
              f"Upgrade SQLite or run on PostgreSQL."
          )
  ```

  **(2) Rate-limiter init** in the production `else` branch only, AFTER `_enforce_oidc_runtime_gates()` at line 504, BEFORE the unconditional `OSS_AUTO_PULL_MODELS` block (bob:3):
  ```python
      await _enforce_oidc_runtime_gates()
      await _init_rate_limiter(redis_client)   # NEW — Campaign 3 / ATHENA-14
      # ... existing else-branch tail (ensure_default_model etc.)
  ```
  Notes:
  - `redis_client` is already module-scoped at `main.py:111`. Phase 3 reuses it; do NOT create a second Redis connection.
  - DEV_MODE branch already returns out of the `if DEV_MODE:` block at line 333 — the `_init_rate_limiter` call is in the `else` branch only.
  - The `from fastapi_limiter import FastAPILimiter` import is local to the helper to avoid module-load coupling at admin-backend import time (so DEV_MODE installs without `fastapi-limiter` continue to work — though we add it to `requirements.txt`, this defends against test environments that subset deps).
- **Reversible**: yes. Revert the requirements.txt line, AthenaConfig fields, the new module, the helper, and the one-line init call.
- **Verification (commands jackson runs)**:
  ```bash
  # 1. Dep installs cleanly.
  cd admin/backend
  pip install -r requirements.txt
  python3 -c "from fastapi_limiter import FastAPILimiter; print('ok')"

  # 2. AthenaConfig fields exist with correct defaults.
  python3 -c "
  import sys; sys.path.insert(0, '../../src')
  from shared.config import get_config, _clear_cache_for_tests
  _clear_cache_for_tests()
  c = get_config()
  assert c.login_rate_limit_per_minute == 5
  assert c.login_lockout_threshold == 10
  assert c.login_lockout_minutes == 30
  assert c.login_minimum_delay_ms == 400  # round-2 codex-r1: floor raised 200 → 400
  print('config OK')
  "

  # 3. Behavioral test: TestClient drives startup, asserts limiter init succeeds
  #    against fakeredis OR a real local redis on localhost:6379.
  pytest tests/test_security_hardening.py::TestRateLimiterStartup -v
  ```
  **Tessa:1 + codex-r1 — fixture pattern for `TestRateLimiterStartup`.** Conftest sets `DEV_MODE=true` globally at `conftest.py:25`; the rate-limiter init is in the production `else` branch, so a normal in-process TestClient never exercises it. Round-2 fixture pattern (collapsed from round-1's subprocess-only negative case):

  - **In-process pattern** (`_run_startup_in_process` in conftest): monkeypatches `DEV_MODE=false`, installs `fakeredis[lua]` via `redis_client = fakeredis.aioredis.FakeRedis(...)`, drives `with TestClient(app):` OR calls `_init_rate_limiter(redis_conn)` directly, then reads back `from app.utils.rate_limit import LIMITER_ACTIVE`. Used for both positive and negative cases (negative monkeypatches `FastAPILimiter.init` to raise, then asserts `LIMITER_ACTIVE is False` + log line).
  - **Subprocess pattern** (`_run_startup_subprocess` in conftest, mirrors Campaign 2 at `test_security_hardening.py:1031-1072`): retained as a defense-in-depth end-to-end smoke test for the Redis-unreachable-on-real-network case. Spawns admin-backend with `REDIS_URL=redis://127.0.0.1:1` (closed port); subprocess prints `LIMITER_ACTIVE` to stdout before SIGTERM; parent grep + assert. Single test, fast.

  New test cases in `TestRateLimiterStartup`:
  - `test_startup_initializes_limiter_against_fakeredis` (in-process) — TestClient enters lifespan with `fakeredis[lua]`, no exception, `from app.utils.rate_limit import LIMITER_ACTIVE; assert LIMITER_ACTIVE is True`.
  - `test_init_failure_leaves_limiter_inactive` (in-process, **round-2 codex-r1 fix for observability**) — monkeypatch `fastapi_limiter.FastAPILimiter.init` to raise `RedisError("simulated")`; call `await _init_rate_limiter(fake_redis_conn)` directly; assert `rate_limit_mod.LIMITER_ACTIVE is False` AND `caplog` contains `"rate_limiter_init_failed"`. This is the negative-case test that round-1's subprocess-only design couldn't observe.
  - `test_dev_mode_does_not_set_limiter_active` (in-process) — `DEV_MODE=true`, drive `with TestClient(app):` (which takes the DEV branch and skips `_init_rate_limiter`), assert `LIMITER_ACTIVE is False` after lifespan startup. Confirms the codex-r1 HIGH "DEV_MODE limiter break" is fixed.
  - `test_subprocess_redis_unreachable_smoke` (subprocess, retained) — `DEV_MODE=false REDIS_URL=redis://127.0.0.1:1`, subprocess prints `from app.utils.rate_limit import LIMITER_ACTIVE; print(f'LIMITER_ACTIVE={LIMITER_ACTIVE}')` after lifespan startup, parent asserts stdout contains `LIMITER_ACTIVE=False` AND `"rate_limiter_init_failed"` in stderr/log.

  **Test-dep update (codex-r1 fakeredis[lua])**: add `fakeredis[lua]>=2.21,<3` to `admin/backend/requirements.txt` with a `# test-only — Lua extra required by fastapi-limiter==0.1.6 script_load/evalsha` comment. Plain `fakeredis` lacks Lua; `fakeredis[lua]` installs `lupa` for full script execution. `>=2.21` for Python 3.11 compatibility, `<3` to pin a major. Mirrors Campaign 2's `pytest-httpserver` test-only precedent (OQ2 resolution).
- **Out of scope**:
  - Attaching the limiter to any route (Phase 4).
  - Lockout-counter logic in the login handler (Phase 4).
  - Changing the existing module-level `redis_client` at `main.py:111`.
- **Commit**: `feat(auth-rate-limit-bypass): phase 3 — fastapi-limiter wiring + AthenaConfig login_* fields (xander:10, ATHENA-14)`

### Phase 4 — xander:10c — Login handler: lockout state machine + 400ms floor + rate-limit dep

- **What**: Rewrite `local_login` in `admin/backend/app/routes/local_auth.py` to (a) reject locked users with a generic 401 (OQ3 resolution), (b) reject **inactive users with a generic 401** (round-2 Decision D — was 403), (c) lazily expire stale locks, (d) atomically increment `failed_login_count` via SQL UPDATE (xander:39), (e) lock after `login_lockout_threshold` failures, (f) reset count + `locked_until` on success, (g) enforce a **400 ms** minimum on every failure path (xander:38 / codex-r1), (h) compute a dummy PBKDF2 hash on the user-not-found path so unknown usernames pay the same CPU cost (xander:38). Attach the rate-limit dep to the route via `Depends(login_rate_limit_dep)`. Update `admin/frontend/auth.js:215` for 429 UX (ian-#2).
- **Findings addressed**: xander:10 (handler-logic half — final closure), xander:38 (dummy-hash + 400ms floor), xander:39 / tessa:2 (atomic UPDATE), tessa:4 (test iterations override), tessa:6 (429 doesn't increment), tessa:7 / xander:41 (tz-aware fixtures), ian-#2 (frontend 429), ian-#7 (db.refresh in fixtures), OQ3 (401 generic), OQ4 (frontend one-liner), **codex-r1 MEDIUM "inactive 403 oracle"** (round-2 user-locked Decision D — inactive → 401 generic).
- **Files**:
  - `admin/backend/app/routes/local_auth.py` — handler rewrite + module-level `_DUMMY_PBKDF2_HASH` constant.
  - `admin/backend/tests/test_security_hardening.py` — append the **11** cases named in D4 under `class TestLocalLoginLockout` (round-1 was 10; round-2 adds inactive → 401 case).
  - `admin/frontend/auth.js:215` — one-line change: `data.detail || data.error || 'Login failed'`. Codex-r1 confirmed FastAPI returns `{"detail":"Too Many Requests"}` from fastapi-limiter 0.1.6's `HTTPException(429, ...)`, so `data.detail` already works; the `data.error` OR-fallback stays as defensive future-proofing for any subsequent fastapi-limiter API change.
- **Implementation notes** (target shape — jackson refines line-level):
  ```python
  """Local authentication routes."""
  import asyncio
  import time
  from datetime import datetime, timedelta, timezone

  from fastapi import APIRouter, Depends, HTTPException, Request, status
  from pydantic import BaseModel
  import sqlalchemy as sa
  from sqlalchemy.orm import Session
  import structlog
  from starsessions import load_session

  from app.auth.oidc import create_access_token
  from app.database import get_db
  from app.models import User
  from app.utils.passwords import hash_password, verify_password
  from app.utils.rate_limit import login_rate_limit_dep
  from shared.config import get_config

  logger = structlog.get_logger()
  router = APIRouter(prefix="/api/auth", tags=["auth"])

  # Module-level dummy hash for constant-time user lookup (xander:38).
  # Pre-computed once at import; verify_password against this on the user-not-found
  # path so unknown usernames pay the same PBKDF2 cost as known ones.
  _DUMMY_PBKDF2_HASH = hash_password("dummy-password-not-used-for-auth")


  class LocalLoginRequest(BaseModel):
      username: str
      password: str


  async def _enforce_minimum_delay(start_monotonic: float) -> None:
      """Equalize all failure paths to ≥ login_minimum_delay_ms wall time.

      Anchored at handler entry, applied just before the failure raise.
      Successful logins skip this (helper called only in failure branches).
      """
      floor_seconds = get_config().login_minimum_delay_ms / 1000.0
      elapsed = time.monotonic() - start_monotonic
      if elapsed < floor_seconds:
          await asyncio.sleep(floor_seconds - elapsed)


  @router.post(
      "/local-login",
      dependencies=[Depends(login_rate_limit_dep)],   # bob:1 / xander:32 / ian-#1
  )
  async def local_login(payload: LocalLoginRequest, request: Request, db: Session = Depends(get_db)):
      """Authenticate a local Athena account."""
      start = time.monotonic()
      await load_session(request)
      cfg = get_config()
      now = datetime.now(timezone.utc)
      username = payload.username.strip()

      user = db.query(User).filter(User.username == username).first()

      # Branch 1: user not found OR not local OR no password hash.
      # Verify against dummy hash so we pay PBKDF2 cost regardless (xander:38).
      if not user or user.auth_provider != "local" or not user.password_hash:
          verify_password(payload.password, _DUMMY_PBKDF2_HASH)
          await _enforce_minimum_delay(start)
          raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

      # Branch 2: inactive.  Round-2 user-locked Decision D — return 401 generic
      # to match wrong-password / locked branches.  Previously 403, which was a
      # status-code oracle for "this username exists but is disabled" (codex-r1
      # MEDIUM).  All four failure branches now return identical 401 +
      # "Invalid username or password", honoring the campaign's enumeration-
      # protection claim.
      if not user.active:
          await _enforce_minimum_delay(start)
          raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

      # Branch 3: locked.  Lazy-expire if the lock has passed.  OQ3 resolution:
      # locked → 401 generic (don't disclose lockout state).
      if user.locked_until and user.locked_until > now:
          await _enforce_minimum_delay(start)
          raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
      elif user.locked_until and user.locked_until <= now:
          # Lock expired — atomic clear via UPDATE (xander:39).
          db.execute(
              sa.update(User)
              .where(User.id == user.id)
              .values(failed_login_count=0, locked_until=None)
          )
          db.commit()
          db.refresh(user)

      # Branch 4: wrong password.  Atomic increment + conditional lock (xander:39).
      if not verify_password(payload.password, user.password_hash):
          new_count_q = (
              sa.update(User)
              .where(User.id == user.id)
              .values(failed_login_count=User.failed_login_count + 1)
              .returning(User.failed_login_count)
          )
          new_count = db.execute(new_count_q).scalar_one()
          if new_count >= cfg.login_lockout_threshold:
              db.execute(
                  sa.update(User)
                  .where(User.id == user.id)
                  .values(locked_until=now + timedelta(minutes=cfg.login_lockout_minutes))
              )
              logger.warning(
                  "local_login_account_locked",
                  user_id=user.id,
                  failed_count=new_count,
                  lockout_minutes=cfg.login_lockout_minutes,
              )
          db.commit()
          await _enforce_minimum_delay(start)
          raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

      # Branch 5: success.  Reset counters, issue token.
      db.execute(
          sa.update(User)
          .where(User.id == user.id)
          .values(failed_login_count=0, locked_until=None, last_login=now)
      )
      db.commit()
      db.refresh(user)

      token = create_access_token({
          "user_id": user.id,
          "username": user.username,
          "role": user.role,
      })
      request.session["access_token"] = token
      request.session["user_id"] = user.id
      request.session["auth_method"] = "local"
      logger.info("local_user_authenticated", user_id=user.id, username=user.username)

      return {
          "token": token,
          "user": {
              "id": user.id,
              "username": user.username,
              "email": user.email,
              "full_name": user.full_name,
              "auth_provider": user.auth_provider,
              "role": user.role,
              "last_login": user.last_login.isoformat() if user.last_login else None,
          }
      }
  ```
  Notes:
  - `Depends(login_rate_limit_dep)` resolves at request time — `LIMITER_ACTIVE` and `cfg.login_rate_limit_per_minute` are both read inside the dep function body. No import-time freezing (bob:1 / xander:32 / ian-#1; round-2 codex-r1 flag rename).
  - `datetime.now(timezone.utc)` (timezone-aware) replaces `datetime.utcnow()` (timezone-naive) used at the original `:44`. The `User.locked_until` column is `DateTime(timezone=True)` so timezone-aware comparison is required.
  - **`UPDATE ... RETURNING` portability** (codex-r1b polish): SQLite ≥ 3.35 (Mar 2021) supports `RETURNING`; PostgreSQL has it. Project's `python:3.11-slim` Docker base ships SQLite 3.40+ (verified at `Dockerfile:5`). **The version gate runs at the TOP of `startup_event` (BEFORE the `if DEV_MODE:` branch) so it fires whether the deploy is DEV_MODE local SQLite or production SQLite-backed.** Original codex-r1 placement was inside `_init_rate_limiter()` which is production-branch-only — codex-r1b correctly identified that DEV_MODE local SQLite would still execute Phase 4's `UPDATE ... RETURNING` without the guard firing. Failing fast at startup with an actionable message ("Upgrade SQLite or run on PostgreSQL") is simpler and safer than a two-branch UPDATE-then-SELECT fallback that adds untested code paths and obscures concurrency semantics. Self-host deployers on older Debian/Ubuntu base images get a clear error before the login handler is ever invoked.
  - **Test fixtures (tessa:4 + tessa:7 + ian-#7 + xander:41)**:
    - Override PBKDF2 iterations: fixture creates users with `password_hash = hash_password(password, iterations=1000)` to keep success-path under 100 ms (D4 case 8).
    - After fixture inserts a User row with `failed_login_count` or `locked_until` set, MUST call `db.commit(); db.refresh(user)` before the test asserts on those values — otherwise SQLAlchemy's session-cached row carries `None` rather than the DB-side default.
    - All comparisons in tests use `datetime.now(timezone.utc)`; the helper `_make_user(locked_until=now - timedelta(minutes=1))` always inserts tz-aware datetimes. SQLite stores these as ISO strings; SQLAlchemy reattaches tzinfo on load when `DateTime(timezone=True)` is declared.
  - **TOCTOU test (xander:39)**: D4 case 9 uses `httpx.AsyncClient` + `asyncio.gather` to fire 5 concurrent wrong-password POSTs against `failed_login_count=0`. Final read must be exactly 5. With ORM read-modify-write (the previous draft), this would intermittently land on values 1–5 depending on session interleaving; with atomic UPDATE it's deterministic.
- **Reversible**: yes. Revert the file to its 71-line shape; the DB columns from Phase 2 simply hold defaults again.
- **Verification (commands jackson runs)**:
  ```bash
  # All 8 cases from D4.
  cd admin/backend
  pytest tests/test_security_hardening.py::TestLocalLoginLockout -v

  # Then the full file, to confirm Phases 1+2+3 still pass.
  pytest tests/test_security_hardening.py -v

  # Then the full backend test suite, to confirm no cross-test regression.
  pytest -v
  ```
  Each of the 8 D4 cases must pass. The timing assertions (cases 6/7/8) use `time.monotonic()` start/end stamps in the test:
  ```python
  def test_failed_login_minimum_delay_enforced(...):
      t0 = time.monotonic()
      r = client.post("/api/auth/local-login", json={"username": "alice", "password": "wrong"})
      elapsed_ms = (time.monotonic() - t0) * 1000
      assert r.status_code == 401
      assert elapsed_ms >= 360, f"expected >= 360ms (400ms floor minus 40ms slack), got {elapsed_ms:.1f}ms"
  ```
- **Out of scope**:
  - Surfacing lockout state in the admin UI (separate ticket; users page can show `failed_login_count` / `locked_until` columns later).
  - An admin "unlock user" endpoint (D5: admin uses `psql` for now).
  - WebSocket auth rate limiting (codex-H2 / ATHENA-13, separate campaign).
  - Switching the legacy `datetime.utcnow()` calls in OTHER routes to timezone-aware (separate ticket; this campaign only changes `local_auth.py`).
- **Commit**: `feat(auth-rate-limit-bypass): phase 4 — local-login lockout + rate-limit + timing equalization (xander:10, ATHENA-14)`

### Phase 5 — Documentation closeout (Stage 12 / scott)

- **What**: Update CLAUDE.md, .env.example, and the wiki entry for the new env vars and the new auth flow.
- **Findings addressed**: stage 12 closeout for xander:10 and xander:11.
- **Files**:
  - `CLAUDE.md` — append a `Login rate limit + lockout` row to the centralized-config table; document the four `login_*` env vars and their defaults.
  - `.env.example` — add the four `LOGIN_*` env vars with comments explaining defaults and tuning rationale.
  - `manifests/athena-prod/config.yaml` (ian-#6) — add commented stubs for `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_LOCKOUT_THRESHOLD`, `LOGIN_LOCKOUT_MINUTES`, `LOGIN_MINIMUM_DELAY_MS` mirroring the `CONTROL_AGENT_ENABLED` pattern. Defaults match `AthenaConfig`; deployers uncomment to override.
  - `wiki.xmojo.net` (page: `homelab/services/athena/auth-hardening`) — new page documenting the lockout flow, how to manually unlock a user via `psql`, and the rate-limit/lockout interaction.
  - `admin/backend/CHANGELOG.md` (if present at HEAD; check) — Campaign 3 entry referencing ATHENA-14.
  - **Plane follow-up ticket — xander:37 lockout-DoS** (filed during Phase 5, NOT shipped this campaign): "Lockout-DoS mitigation: admin-unlock CLI + lockout email notification". One-liner body: "Phase 4 of ATHENA-14 ships per-username lockout, which an attacker who knows a username can weaponize to lock real users out. Mitigation: a `python -m admin.unlock <username>` CLI command and an SMTP notification on lockout. Out of scope for ATHENA-14; track here." Reference the new ticket ID in the plan's Out-of-scope (below) once filed.
  - **Plane follow-up ticket — ATHENA-15 alert/api-key public-write gating** (round-2 user-locked Decision B / codex-r1 public-route triage): "Audit alert-write endpoints (`alerts.py:87`, `:139`, `:178`) and tool-calling api-key-listing endpoints (`tool_calling.py:971`, `:994`) for service-key gating. Each requires identifying every internal caller and threading `X-Service-Key` through them. Out of scope for ATHENA-14 (Campaign 3) which gates only the cheap-cost read-side cluster." Body links back to the **Public-route surface triage** section of this plan.
- **Reversible**: yes (docs only).
- **Verification**:
  ```bash
  # Spot-check that .env.example is complete.
  grep -E "^LOGIN_(RATE_LIMIT_PER_MINUTE|LOCKOUT_THRESHOLD|LOCKOUT_MINUTES|MINIMUM_DELAY_MS)" .env.example | wc -l
  # Expected: 4

  # CLAUDE.md mentions the new vars.
  grep -E "login_rate_limit_per_minute|login_lockout_threshold|login_lockout_minutes|login_minimum_delay_ms" CLAUDE.md
  ```
- **Out of scope**:
  - User-visible UX (e.g., a "your account is locked, try again in N minutes" front-end message). The HTTP 403 with `detail="Account locked"` is sufficient; admin-frontend already renders the `detail`.
- **Commit**: `docs(auth-rate-limit-bypass): phase 5 — close out CLAUDE.md/.env.example/wiki for ATHENA-14`

## Open questions

All four open from round-0 are resolved as of iteration round 1; left here for reviewer audit traceability.

1. **fastapi-limiter version** — **CLOSED**. Pin verified at implementation time via `pip index versions`; behavior contract (`RateLimiter.__call__`) stable across recent versions. No plan change.
2. **`fastapi-limiter` + DEV_MODE test path** — **CLOSED**. Ship `fakeredis[lua]>=2.21,<3` as test-only dep (round-2 codex-r1 fix — fastapi-limiter 0.1.6 uses Lua via `script_load`/`evalsha`, plain fakeredis lacks it); fixture pattern specified per tessa:1 + round-2 codex-r1 (in-process for both positive and negative observability cases; subprocess retained as end-to-end smoke).
3. **Locked-user response code** — **CLOSED → 401 generic** (`"Invalid username or password"`). Unanimous from bob + librarian + xander + tessa. D4 cases 4/5 updated.
4. **Rate-limit response shape (frontend 429 UX)** — **CLOSED → ship one-line frontend fix in Phase 4**. `admin/frontend/auth.js:215` updated to `data.detail || data.error || 'Login failed'` so fastapi-limiter's `{"error": "Too Many Requests"}` body renders cleanly.

## Out of scope (campaign-wide)

- xander:1 (HA token in repo) — user-only, not in deferred queue for OSS.
- xander:2 / xander:7 / xander:8 (Control Agent auth + SSRF + path traversal) — coupled, separate ticket.
- xander:5 (alembic SQL parameterization) — separate ticket.
- xander:9 (DEFAULT_OIDC_ISSUER) — separate ticket.
- xander:12 (security headers) — separate ticket.
- xander:3 / xander:4 / xander:6 / xander:13 / xander:16 / xander:17 — closed in ATHENA-12.
- WebSocket auth (codex-H2 / ATHENA-13).
- librarian:2/4 (BaseRAGService + ResilientHttpClient extraction).
- Admin UI lockout-management page (per-user `failed_login_count` view, manual unlock button).
- Admin "unlock my account" self-service (e.g., email-based reset).
- General authentication refactoring (PBKDF2 → bcrypt/argon2 migration, SSO-only mode, etc.).
- Frontend changes beyond the OQ4 one-liner — Phase 4 ships the `data.detail || data.error || 'Login failed'` patch with the response-shape change; broader UX (e.g., a "your account is locked, try again in N minutes" affordance) deferred. Athena admins see a generic "Invalid username or password" by design (OQ3).
- `datetime.utcnow()` deprecation across the rest of the codebase — only `local_auth.py` is touched in Phase 4. **Latent hazard tracked (ian-#4)**: `admin/backend/app/database.py:243` `seed_dev_data()` uses naive `datetime.utcnow()`. If any future code compares `seed_dev_data`'s naive datetimes against the new tz-aware `locked_until`, `TypeError` results. Out of scope for ATHENA-14; tracked under the deferred utcnow-deprecation campaign.
- Lockout-DoS mitigation (xander:37) — admin-unlock CLI + lockout email notification. Plane ticket filed during Phase 5 (see Phase 5 deliverables); not shipped here. Acceptable risk: per D3, an admin can clear `failed_login_count` via `psql` until the follow-up lands.
- **Public-route auth gating beyond the 4 named** — `defer-to-follow-up` rows in the Public-route surface triage table (alerts write-cluster `alerts.py:87/139/178` + tool_calling api-key listing `tool_calling.py:971/994`). Filed as **ATHENA-15** during Phase 5 (see Phase 5 deliverables). Each requires a caller-audit that's larger than this campaign's budget. The 26 `intentionally-public` rows are not deferred — they are accepted as service-to-service config-fetch surface with non-sensitive contents (see triage justifications).

## Documentation impact (Stage 12 — scott)

- **CLAUDE.md** — append `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_LOCKOUT_THRESHOLD`, `LOGIN_LOCKOUT_MINUTES`, `LOGIN_MINIMUM_DELAY_MS` to the centralized-config bullet (currently lists 12 env vars; becomes 16). One-line description per var.
- **README / module docs** — the `admin/backend/README.md` (if present) gets a new "Authentication hardening" section pointing at the wiki page.
- **Wiki (wiki.xmojo.net)** — new page `homelab/services/athena/auth-hardening` covering: rate-limit + lockout interaction, manual-unlock via `psql`, env-var tuning, observability (which structlog events fire — `local_login_account_locked`, `rate_limiter_init_failed`).
- **API / OpenAPI / GraphQL schema docs** — `POST /api/auth/local-login` now returns 429 in addition to 401 (round-2: 403 for inactive removed; failures unified to 401 generic per Decision D); regenerate OpenAPI from FastAPI's auto-doc.
- **Runbooks / deployment guides** — Operator runbook gets a "How to unlock a locked user" entry: `UPDATE users SET failed_login_count=0, locked_until=NULL WHERE username='...'`.

## Self-review checklist (per PIPELINE.md)

- [x] Every claim about the current codebase is backed by a file I actually read (verified: `local_auth.py`, `rag_service_bypass.py`, `service_auth.py`, `models.py`, `config.py`, `main.py`, alembic `053`, `passwords.py`).
- [x] xander:11 verified as **already shipped at base** (`git show 18c78e5`); plan re-scoped to regression test rather than re-implementation.
- [x] Every step names concrete files and line ranges.
- [x] Decisions are surfaced (D1–D8) with options + recommendation + tradeoff.
- [x] Sequence is correct: Phase 1 (test-only) → Phase 2 (migration before code reads it) → Phase 3 (limiter init before route attaches dep) → Phase 4 (handler rewrite, the load-bearing change) → Phase 5 (docs).
- [x] Risks are named with mitigations (table at top).
- [x] Verification is concrete: commands and pytest classes, not "test it works."
- [x] Documentation impact identified.
- [x] Out-of-scope is explicit.
- [x] Open questions listed (4).
- [x] Plan describes the smallest change that meets the goal: 4 implementation phases, 1 doc phase, no opportunistic refactoring.
