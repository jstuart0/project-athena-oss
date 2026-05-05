# Changelog

All notable changes to Project Athena are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Removed

- **`admin/frontend/router.js`**: deleted (~200 lines of dead navigation code that paralleled the live `showTab()` system; no callers besides one in `command-palette.js`, now updated). (audit follow-up: dexter:7, ATHENA-9)

### Changed

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
