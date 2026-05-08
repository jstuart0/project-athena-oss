# Changelog

All notable changes to Project Athena are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **Phase 4** (xander:4): JWT is no longer passed as `?token=<jwt>` in the OIDC callback redirect URL or the `DEMO_MODE` redirect URL. The backend now writes the JWT to the server session and redirects to `<FRONTEND_URL>?logged_in=1`. The admin frontend detects `?logged_in=1`, clears any stale `localStorage.auth_token` (preventing cross-user contamination on shared devices), and fetches the JWT from the existing `/api/auth/session-token` endpoint. The `?token=` URL query parameter is no longer emitted; the `?logged_in=1` hint is idempotent and carries no credentials. Closes the JWT-leak-via-URL chain: 8-hour bearer tokens are no longer written to reverse-proxy access logs, browser history, or `Referer` headers on every admin login. Note: the `admin-jarvis.js` WebSocket URL (`?token=` in upgrade request) is a related but distinct exposure requiring a backend protocol change; it is deferred to a separate Plane ticket (codex-H2 sibling of xander:4). (`33db179`, `41f0b57`)

### Notes

- This is **Campaign 2 of 6** in the audit-deferred security-hardening sequence. Findings closed: xander:3, xander:4, xander:6, xander:13 (audit-named scope) plus xander:16, xander:17 (pre-existing findings pulled into Campaign 2 by user direction). The admin-jarvis.js WebSocket query-token (codex-H2) is explicitly out of scope — deferred to a follow-up campaign.
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
