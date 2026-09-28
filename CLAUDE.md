# CLAUDE.md - Project Athena

This file provides guidance to Claude Code when working with Project Athena.

## ⚠️ CRITICAL: OSS-First Development

**Every fix, feature, and architectural change must be designed to work for any Athena deployment — not just Jay's specific home lab setup.**

This is the public OSS repository. All code must be implementation-agnostic:

1. **No hardcoded values** — IPs, hostnames, credentials, or domains must come from env vars, `src/shared/config.py`, or the admin panel
2. **No assumption of Jay's infrastructure** — code can't assume specific ports, hostnames, or service locations
3. **Configuration over convention** — if a value might differ between deployments, it must be configurable
4. **Test generalizability** — ask "would this work for someone else deploying Athena from scratch?"
5. **Run the maintainer-leak gate before committing** — `python3 scripts/check-maintainer-leaks.py` (also enforced in CI by `.github/workflows/maintainer-leaks.yml` on every PR and push, no `paths:` filter). Allowlist entries in `scripts/.maintainer-leak-allowlist` are exact full-line matches, not substrings. Private/local patterns you don't want in the public repo go in a file outside this checkout (`--extra-patterns` / `MAINTAINER_LEAK_EXTRA_PATTERNS`), never committed here.

**Examples:**
- ❌ `ha_url = "http://192.0.2.10:8123"` as a fallback
- ✅ `ha_url = os.getenv("HA_URL")` with a log warning if missing
- ❌ Hardcoded service URL in a function body
- ✅ Service URL from env var or `config.py` constant

## Project Overview

Project Athena is an AI-powered smart home assistant with voice interface, RAG (Retrieval-Augmented Generation) services, and Home Assistant integration.

## Production Deployment Architecture

### Infrastructure

**Kubernetes Cluster:** Your K8s cluster
**Namespace:** athena-prod
**Container Registry:** Your container registry (e.g., `your-registry:5000`)

### LLM Inference Options

**Option 1: External Ollama (Recommended for Apple Silicon)**
- Run Ollama on a Mac with Apple Silicon for best inference performance
- Configure `OLLAMA_URL` in config.yaml to point to your Mac

**Option 2: In-Cluster Ollama**
- Deploy `manifests/athena-prod/ollama.yaml` for containerized inference
- Slower than Apple Silicon but works on any cluster

### Services Architecture

```
                    ┌─────────────────────────────────────────┐
                    │            External Access              │
                    │  athena.your-domain  │  chat.your-domain│
                    └─────────────────────────────────────────┘
                                      │
                              Ingress Controller
                                      │
        ┌─────────────────────────────┴─────────────────────────────┐
        │                                                           │
        ▼                                                           ▼
┌───────────────────┐                                    ┌──────────────────┐
│  Admin Frontend   │                                    │   Jarvis Web     │
│ (Admin web UI)    │                                    │ (Voice Interface)│
└───────────────────┘                                    └──────────────────┘
        │                                                           │
        ▼                                                           │
┌───────────────────┐                                               │
│  Admin Backend    │◄──────────────────────────────────────────────┤
│   (FastAPI)       │                                               │
└───────────────────┘                                               │
        │                                                           │
        ▼                                                           ▼
┌───────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│     Gateway       │────────►│   Orchestrator  │────────►│   Ollama (LLM)  │
│   (API Router)    │         │  (Query Engine) │         │                 │
└───────────────────┘         └─────────────────┘         └─────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
            ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
            │ RAG Weather │   │ RAG Sports  │   │  RAG News   │
            └─────────────┘   └─────────────┘   └─────────────┘
                              ... 23 RAG services total ...
```

### Service Ports

| Service | Port | Description |
|---------|------|-------------|
| Admin Backend | 8080 | API and admin functions |
| Admin Frontend | 80 | Admin web UI (vanilla JS + nginx) |
| Gateway | 8000 | API gateway/router |
| Orchestrator | 8001 | Query orchestration |
| Mode Service | 8022 | Mode management |
| Jarvis Web backend | 3001 | Chat/voice proxy to orchestrator |
| Redis | 6379 | Caching |
| Ollama | 11434 | LLM inference |
| Control Agent | 8099 | Service control (runs on the Control Agent host) |

### Orchestrator pipeline architecture

`src/orchestrator/main.py` is the LangGraph pipeline entry point (8,758 lines as of ATHENA-10). The orchestrator package has 12 sibling modules extracted from the original god-object:

**Sibling modules at `src/orchestrator/`**

| Module | Purpose |
|---|---|
| `state.py` | Canonical definitions for `OrchestratorState`, `IntentCategory`, `ModelTier`, `ConversationContext`. Do not redefine these in `main.py` or elsewhere. |
| `helpers.py` | 17 stateless helpers. Helpers that need runtime singletons call `_runtime.get_X()` at call time (Pattern 1). |
| `mode_permission.py` | 6 mode/permission helpers (`get_current_mode`, `detect_owner_mode_command`, `extract_pin_from_query`, `activate_owner_override`, `check_intent_permission`, `check_entity_permission`) plus `OWNER_MODE_PATTERNS`. |
| `urls.py` | 25 service URL constants (23 RAG + `MODE_SERVICE_URL` + `NOTIFICATIONS_SERVICE_URL`); canonical env spelling `RAG_<NAME>_URL`, legacy `<NAME>_RAG_URL` accepted with a warning. |
| `metrics.py` | 7 Prometheus metric objects (`request_counter`, `request_duration`, `node_duration`, `tool_call_breakdown`, `validation_counter`, `hallucination_counter`, `validation_layer_duration`). |

**`nodes/` package at `src/orchestrator/nodes/`**

`nodes/__init__.py` exports all 10 node functions via `__all__`. Each node module also contains one or more proxy classes that defer singleton access to `_runtime.get_X()` at call time.

| Module | Node function |
|---|---|
| `route_info.py` | `route_info_node` |
| `send_sms.py` | `send_sms_node` |
| `notification_pref.py` | `notification_pref_node` |
| `synthesize.py` | `synthesize_node` |
| `validate.py` | `validate_node` |
| `finalize.py` | `finalize_node` |
| `route_control.py` | `route_control_node` |
| `route_music.py` | `route_music_node` |
| `route_tv.py` | `route_tv_node` |
| `retrieve.py` | `retrieve_node` |

**`nodes/_runtime.py` — the runtime accessor**

`_runtime.py` lives at `src/orchestrator/nodes/_runtime.py`. Import it as `from orchestrator.nodes import _runtime`.

- `_runtime.set_X(value)` — called from `main.py`'s lifespan, once per process, to register each singleton.
- `_runtime.get_X()` — called at node/helper invocation time, never at import time.
- `_runtime.is_ready() -> bool` — returns `True` when all required singletons are set.
- `_runtime.missing_required() -> list[str]` — names of required singletons not yet set (used by lifespan readiness assertion).
- `_runtime.required_singletons() -> tuple[str, ...]` — public accessor for the required-singleton tuple (do not read `_runtime._REQUIRED_SINGLETONS` directly).
- `_runtime.reset_for_test()` — resets all slots and strict-mode flag; use in test teardown.

**Invariants (enforced throughout; violations are bugs)**

- **R2-C3**: sibling modules (`helpers.py`, `mode_permission.py`, `urls.py`, `metrics.py`) and node modules MUST NOT `from orchestrator.main import ...`. Use `_runtime.get_X()` for runtime singletons; import from `helpers.py`, `mode_permission.py`, `state.py`, `urls.py`, or `metrics.py` for everything else.
- **R2-H1 (lifespan construction)**: lifespan constructs each client as a local variable first, then calls `_runtime.set_X(local)`. Never invert this (e.g., `_runtime.set_X(SomeClass(_runtime.get_other()))` is forbidden).

**`validate_node` training-knowledge bypass (ATHENA-39)**

`validate_node` bypasses the Layer 4 LLM fact-check when `is_training_knowledge_fallback` is true:

```
is_training_knowledge_path = (intent == GENERAL_INFO) OR (conversation_history OR history_summary populated)
is_training_knowledge_fallback = is_training_knowledge_path AND not retrieved_data AND not base_knowledge_populated AND intent != WEBSEARCH
```

This matches exactly the two synthesize-node branches that explicitly permit training-knowledge synthesis (`synthesize.py:129-144` for `GENERAL_INFO` and `synthesize.py:152-166` for any intent with conversation context). First-turn current-domain queries (WEATHER, SPORTS, STOCKS, NEWS, etc.) with no RAG data do NOT match and continue to run Layer 4 — `synthesize.py:167-179` tells the LLM not to invent specifics for that branch, so Layer 4 protection is correctly aligned. WEBSEARCH is carved out even with conversation context because the user explicitly requested fresh data. Layer 1 (length) and Layer 2 (pattern detection) still run on the bypass path; `hallucination_counter` does NOT increment on bypass because the `*_unsupported` counters were specifically counting cases where Layer 4 then ran. Observable via `validation_counter{passed="true", reason="training_knowledge_fallback"}`. When adding new intents that should always have retrieval, add an explicit WEBSEARCH-style carve-out to the bypass condition.

**What stays in `main.py` for now**

`classify_node` (2,473 lines), `tool_call_node`, route handlers, and streaming functions remain in `main.py`. Extraction is deferred: `classify_node` to Campaign 2, `tool_call_node` to Campaign 1.3, route handlers to Campaign 1.5.

When adding a new node, helper, or utility:
- New stateless helpers → `helpers.py`
- New mode/permission logic → `mode_permission.py`
- New service URL → `urls.py`
- New Prometheus metric → `metrics.py`
- New pipeline node → `src/orchestrator/nodes/<node_name>.py`, exported from `nodes/__init__.py`
- New data type shared across the pipeline → `state.py`

**Ingress authentication (ATHENA-89, D10)**

Orchestrator ingress routes (`/query*`, `/v1/chat/completions`, `/sessions*`, `/session/{id}/warmup`, `/admin/*` — 13 routes) are gated by `orchestrator/ingress_auth.py::require_service_caller`; mode via `ORCHESTRATOR_INGRESS_AUTH` (`warn`/`enforce`); every in-cluster caller must send `X-Service-Key` and is checked by `tests/unit/test_orchestrator_callers_send_service_key.py`.

---

### Control Agent

The Control Agent runs on a host alongside Ollama (e.g., an Apple Silicon Mac or bare-metal node). It provides HTTP endpoints to manage Ollama and other services.

**Location:** `src/control_agent/` (copied to the control-agent host at `~/<path>/control_agent`)

**Starting the Control Agent on its host:**
```bash
ssh <ssh-user>@<control-agent-host>
cd ~/<path>/control_agent
CONTROL_AGENT_SERVICES_FILE=/path/to/services.json \
    nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8099 > /tmp/control_agent.log 2>&1 &
```

**`CONTROL_AGENT_SERVICES_FILE` (ATHENA-99, D46, OSS-First)**: path to a JSON file naming exactly which bare Python/uvicorn processes, watchdog exclusions, and Docker containers this Control Agent instance may manage. Unset (the default): the Control Agent manages nothing -- the 60s watchdog and the startup registry sync are no-ops, and every `is_port_allowed`/`is_container_allowed` check returns false. There is no built-in process list; a deployment that wants the watchdog/registry-sync/Docker-control features must opt in explicitly with this file. See `src/control_agent/services.example.json` for the schema (`processes` keyed by port, `watchdog_exclude`, `containers`) and `docs/CONFIGURATION.md`. A malformed file logs an ERROR per problem and falls back to managing nothing -- it never crashes the process. `processes` can be empty while `containers` is non-empty (a container-only deployment, e.g. managing `whisper-wyoming`/`athena-piper-tts` with no bare processes at all).

**Verify it's running:**
```bash
curl http://192.0.2.10:8099/health
curl http://192.0.2.10:8099/ollama/health
```

**K8s Configuration:**
The admin-backend needs `CONTROL_AGENT_URL=http://<your-control-agent-host>:8099` environment variable set.

**OSS-First default — opt-in required:**
The Control Agent is disabled by default (`CONTROL_AGENT_ENABLED=false`). Set `CONTROL_AGENT_ENABLED=true` in your private overlay or `.env` only if a Control Agent process is actually running on a host. OSS deployers without a Control Agent no longer see connection errors from admin-backend or orchestrator startup. Deployment-specific host/IP values (SSH targets, private kubeconfig overlay entries) belong in `CLAUDE.local.md` (untracked; see `.gitignore`), not here.

**Inbound authentication (ATHENA-110)**: every mutating Control Agent route (`/process/*`, `/docker/*`, `/ollama/start|stop|restart`, `/huggingface/download|import-to-ollama|downloaded`-DELETE, `/watchdog/*`) requires an `X-Service-Key` header matching this host's `SERVICE_API_KEY` env var — a missing/wrong key gets 401, an unset key gets 503 on every mutating route (fail-closed; the Control Agent has no `DEV_MODE` bypass). Read-only routes are unaffected. The start command above is unchanged; set `SERVICE_API_KEY` in the same environment the Control Agent runs in, matching the value admin-backend and the orchestrator already use for `/api/*` service-to-service auth. See `docs/CONFIGURATION.md` for the full route list and `src/control_agent/auth.py` for the dependency.

**Ollama control via the Control Agent (ATHENA-118)**: `resolve_manager`/`resolve_ollama_manager` only ever dispatch an Ollama start/stop/restart through the Control Agent when the Control Agent's own host equals the configured Ollama host (both normalized the same way as every other row) — a Control Agent running elsewhere on the network never gets Ollama lifecycle calls routed to it, even if `CONTROL_AGENT_ENABLED=true`. If the hosts don't match, Ollama falls through to Kubernetes resolution (if opted in) or `manager: 'none'` ("Managed on its host" in the admin UI).

## Development Commands

### Building Images

**IMPORTANT: Target Architecture**
The Kubernetes cluster runs on `linux/amd64`. When building from Apple Silicon (M1/M2/M3/M4), you MUST specify `--platform linux/amd64` or images will fail with "exec format error".

```bash
# Build all images for linux/amd64 and push to registry
./scripts/build-and-push.sh

# Build single image (ALWAYS include --platform linux/amd64)
docker build --platform linux/amd64 -t YOUR_REGISTRY/athena-orchestrator:latest -f src/orchestrator/Dockerfile src/
docker push YOUR_REGISTRY/athena-orchestrator:latest

# Force rebuild without cache
docker build --platform linux/amd64 --no-cache -t YOUR_REGISTRY/athena-orchestrator:latest -f src/orchestrator/Dockerfile src/
```

**Service Dockerfile Locations:**
| Service | Dockerfile Path | Build Context |
|---------|-----------------|---------------|
| Admin Backend | `admin/backend/Dockerfile` | `admin/backend/` |
| Admin Frontend | `admin/frontend/Dockerfile` | `admin/frontend/` |
| Orchestrator | `src/orchestrator/Dockerfile` | `src/` |
| Gateway | `src/gateway/Dockerfile` | `src/` |
| Mode Service | `src/mode_service/Dockerfile` | `src/` |
| Jarvis Web | `apps/jarvis-web/Dockerfile` | `apps/jarvis-web/` |
| RAG Services | `src/rag/<service>/Dockerfile` | `src/` (every RAG Dockerfile does `COPY shared /app/shared`; see `scripts/build-and-push.sh`'s `build_push_src`) |

### Kubernetes Operations

```bash
# Always verify context first
kubectl config current-context

# Deploy all manifests
# WARNING: manifests/athena-prod/ is an OSS template with placeholder
# values. This command is safe against a FRESH, unconfigured namespace
# only. Against an already-configured namespace it silently reverts every
# ConfigMap/Secret key back to the placeholder defaults. To change a
# running deployment's config, patch or edit the live resource
# (`kubectl patch`/`kubectl edit`) or apply a private overlay — never
# `kubectl apply -f manifests/athena-prod/` (or the directory) against a
# namespace that already has house-specific values.
kubectl apply -f manifests/athena-prod/

# Check deployment status
kubectl -n athena-prod get pods
kubectl -n athena-prod get pods -w  # Watch

# View logs
kubectl -n athena-prod logs -f deploy/athena-orchestrator

# Port forward for local testing
kubectl -n athena-prod port-forward svc/athena-admin-backend 8080:8080
```

### Database Operations

```bash
# Connect to database
psql -h YOUR_DB_HOST -U athena -d athena

# Run migrations (from admin-backend pod)
kubectl -n athena-prod exec -it deploy/athena-admin-backend -- alembic upgrade head
```

## Configuration

### LLM Model Configuration

Models are configured via the Admin UI at **LLM Components** page. All 11 components can be independently configured:

- **Orchestrator Components:** intent_classifier, intent_discovery, response_synthesis, tool_calling_simple/complex/super_complex, conversation_summarizer
- **Validation Components:** fact_check_validation, response_validator_primary/secondary
- **Control Components:** smart_home_control

### Environment Variables

Key configuration in `manifests/athena-prod/config.yaml`:

- `OLLAMA_URL` - LLM inference endpoint
- `ATHENA_DEFAULT_MODEL` - Default model for seeding
- `ATHENA_DOMAIN` / `CHAT_DOMAIN` - Your domain names
- `ADMIN_API_URL` - Admin backend URL; resolution order (`ADMIN_API_URL` → `ADMIN_BACKEND_URL` → `ADMIN_INTERNAL_URL` [deprecated] → `LOCAL_DEV=true` → K8s auto-discovery → `""`) is centralized in `src/shared/admin_url.py::get_admin_url()` — do not add new `os.getenv("ADMIN_*_URL")` calls outside that module
- Centralized configuration: 42 env vars (`OLLAMA_URL`, `LLM_SERVICE_URL`, `REDIS_URL`, `DATABASE_URL`, `SERVICE_API_KEY`, `DEFAULT_TIMEZONE`, `DEFAULT_CITY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_VALIDATE_ISS`, `DEV_MODE`, `DEMO_MODE`, `CONTROL_AGENT_ENABLED`, `SERVICE_CONTROL_K8S_ENABLED`, `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_LOCKOUT_THRESHOLD`, `LOGIN_LOCKOUT_MINUTES`, `LOGIN_MINIMUM_DELAY_MS`, `SERVICE_REGISTRY_WRITE_PER_MINUTE`, `HEALTH_POLL_INTERVAL_SECONDS`, `HEALTH_POLL_TIMEOUT_SECONDS`, `HEALTH_POLL_CONCURRENCY`, `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`, `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`, `CONTENT_FETCHER_ALLOW_BROWSER_FETCH`, `SESSION_MAX_COUNT`, `NEW_CONVERSATION_PER_MINUTE_PER_IP`, `TRUSTED_PROXY_CIDRS`, `NEW_CONVERSATION_RESET_GRACE_SECONDS`, `ORCHESTRATOR_INGRESS_AUTH`, `MUSIC_ASSISTANT_URL`, `SEARXNG_BASE_URL`, `TRANSIT_REGION_NAME`, `TRANSIT_GTFS_FEEDS`, `TRANSIT_STATIC_SERVICES`, `COMMUNITY_EVENTS_SOURCES`, `DEFAULT_AMTRAK_STATION`, `HA_SATELLITE_ROOM_MAP`, `HA_TV_ENTITIES`, `HA_MUSIC_PLAYERS`, `HA_BED_WARMER_ENTITIES`, `HA_LIGHT_GROUPS`) are read via `get_config()` from `src/shared/config.py::AthenaConfig` (pydantic-settings BaseSettings). New env vars: prefer adding fields to `AthenaConfig` over inline `os.getenv` — see `CONTRIBUTING.md`.
  - `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` — comma-separated CIDRs/hostnames that the sitescraper SSRF guard will allow. Empty by default (all private IPs blocked). Warning: wide CIDRs like `10.0.0.0/8` bypass the guard for the entire RFC-1918 10/8 range; scope narrowly.
  - `CONTENT_FETCHER_ALLOW_BROWSER_FETCH` — `true` to enable Playwright-based browser fetching in ContentFetcher (default `false`). Playwright fetches bypass the SSRF guard for user-supplied URLs; only enable in isolated environments where the sitescraper allowlist is already locked down.
  - `HA_URL` — Home Assistant base URL for artist-search endpoints in `music_config.py`. No hardcoded default (previously a hardcoded maintainer IP). The server logs a warning at startup if unset; HA artist-search endpoints will return errors until this is set.
- **Admin-backend startup gates** (ATHENA-12, Campaign 2): the admin-backend will not start under the following misconfigurations — all raise `SystemExit` before any DB or auth initialization:
  - `DEV_MODE=true` with a non-SQLite `DATABASE_URL` on a **non-private host** or **inside a K8s pod** → `SystemExit` (xander:6). Soft-warning carve-out: if `KUBERNETES_SERVICE_HOST` is unset AND the DB host resolves to loopback/RFC1918/ULA, admin-backend emits `logger.warning("dev_mode_with_local_non_sqlite_database", ...)` and continues — this covers developers running a local Postgres instance. K8s pods always raise SystemExit even when the ClusterIP is RFC1918 (xander M4 guard). Link-local (169.254/16, fe80::/10) is NOT in the carve-out and continues to raise SystemExit. Helper: `is_local_host(hostname)` in `admin/backend/app/utils/url_validators.py`; wrapper `_is_local_database_url(url)` in `admin/backend/main.py`.
  - `DEMO_MODE=true` with `DEV_MODE=false` (xander:16)
  - `OIDC_CLIENT_ID` set to `""`, `"demo-mode"`, or `"CONFIGURE_ME_OIDC_CLIENT_ID"` in production (xander:13/17)
  - `OIDC_ISSUER` empty, missing, or matching the `CONFIGURE_ME` placeholder in production (pre-existing gate from ATHENA-2, still in force)
  - IdP unreachable or `.well-known/openid-configuration` missing `issuer` field at startup (MED-E discovery-doc gate)
  - DB-loaded runtime issuer empty or placeholder after `configure_oauth_client()` (MED-A runtime-issuer gate)
  - `SERVICE_API_KEY` empty or set to `dev-service-key-change-in-production` in production (ATHENA-21, Campaign 5) — production startup raises `SystemExit` via the `_INSECURE_DEFAULTS` loop in `admin/backend/main.py:427-475` (`kind="secret"` treats empty AND placeholder as fatal). Bypass: set `DEV_MODE=true` for local development. Helper-level: `verify_service_or_oidc` returns 503 to any caller that sends `X-Service-Key` while `SERVICE_API_KEY` is unset (the dispatcher does not silently fall through to OIDC).
  For local development: set `DEV_MODE=true` (uses SQLite in-memory; bypasses OIDC gates). For production: `OIDC_ISSUER` must point to a reachable, conformant OIDC IdP with an `issuer` field in its discovery document.
- **Service-registry architecture (ATHENA-1, Campaign 4)**: The admin DB (`athena_service_registry` table) is the source of truth for all service definitions. The Control Agent is a health augmenter, not the authority — on startup it POSTs its local `PROCESS_SERVICES` manifest to `POST /api/service-registry/services` (authenticated with `X-Service-Key`), which upserts entries into `athena_service_registry` via `sync_registry_loop`. A background async health poller (`admin/backend/app/services/health_poller.py`) runs on a `HEALTH_POLL_INTERVAL_SECONDS` (default 30s) schedule and writes `health_status`/`last_health_check`/`last_error`/`last_response_time_ms` back to the table; the admin UI reads the cache instead of blocking on live pings. The unified service catalog is `GET /api/service-registry/services` (requires auth); on-demand refresh is `POST /api/service-registry/services/poll-now`. OSS deployers without a Control Agent have a fully functional service registry — CA is purely additive. Five new env vars govern this subsystem: `SERVICE_REGISTRY_WRITE_PER_MINUTE` (default 60), `HEALTH_POLL_INTERVAL_SECONDS` (default 30), `HEALTH_POLL_TIMEOUT_SECONDS` (default 5), `HEALTH_POLL_CONCURRENCY` (default 8), `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` (CIDRs/hostnames that override the RFC1918/loopback/ULA SSRF block — empty by default, required for K8s operators with in-cluster services on private subnets). **Health-poller leader election (ATHENA-18)**: when admin-backend runs with `replicas: 2`, the background health poller uses a Redis SETNX-based lease (`athena:health_poller:leader`, `LEASE_TTL_SECONDS=40`) so only one replica polls and writes per cycle; a per-cycle heartbeat task renews the lease every `HEARTBEAT_INTERVAL_SECONDS=20` and cancels the in-flight poll if the lease cannot be renewed (strict-abort policy). Invariant: `LEASE_TTL_SECONDS (40) > HEALTH_POLL_INTERVAL_SECONDS` — setting `HEALTH_POLL_INTERVAL_SECONDS > 40` fails startup with `SystemExit FATAL`. Shared-Redis multi-tenant deployments must isolate lease keys by namespace; this is currently unsupported in code (the key is hardcoded). No new env vars — uses the existing `REDIS_URL`.
- **Service Control managers (ATHENA-118)**: `GET /api/service-control` returns one envelope (`services`, `counts`, `control_agent`, `kubernetes`) instead of a bare list. Each row's run state (`running`/`stopped`/`disabled`) is derived from health + `enabled` (`app/utils/service_state.py::derive_run_state`), never from the deprecated `is_running` column (unwritten since ATHENA-118; kept only because `main.py`'s startup schema gate lists it). `app/services/service_managers.py::resolve_manager` picks a manager per row, in order: **Control Agent** (only when the row's host equals the CA's own host — the retired-stack port/name collisions this gate exists to prevent are exactly what let an operator alias an obscure row onto `athena-orchestrator`), else **Kubernetes** (opt-in: `SERVICE_CONTROL_K8S_ENABLED`, default false; the host reduces to a bare RFC1123 label or `<label>.<namespace>[.svc[.cluster.local]]`, matched against the live Deployment list), else `none`. Kubernetes control is scale-only (`app/services/k8s_control.py`): `deployments` get/list plus `deployments/scale` get/patch, name-scoped to `optional/admin-backend-rbac.yaml`'s `resourceNames` (computed from every `manifests/athena-prod/*.yaml` Deployment minus `athena-admin-backend`/`athena-admin-frontend`, which are never controllable, in app code and in RBAC). There is no `deployments` PATCH anywhere — restart is scale-to-0, a bounded 60s wait for pods to actually terminate, then scale back to a remembered count (`system_settings` key `service_control.replicas.<deployment>`, clamped 1-10, a 0 is never stored) inside a shielded `finally`, so no exception/timeout/cancellation can strand a Deployment at 0 while the process survives — brief downtime, not a rolling restart (rolling would need `deployments` PATCH, deliberately not granted). A cross-replica lease (`system_settings` key `service_control.lock.<deployment>`, 90s TTL, atomic INSERT + conditional-UPDATE takeover when expired) serializes actions across admin-backend replicas; contention is 409 `action_in_progress`. An expired `restart` lease with the Deployment still at 0 replicas surfaces as `manager_note: 'restart_interrupted'` — press Start to recover; there's no automatic recovery. Two enabled/disabled rows resolving to the same Kubernetes target block each other (`manager_note: 'target_collision:<name>'`) so an alias can't hijack a critical target. Critical targets — the named set (`athena-gateway`, `athena-orchestrator`, `athena-mode-service`, `athena-jarvis-web`, `redis`, `qdrant`, `ollama`) union any Kubernetes-resolved row whose group isn't `rag` (fail-safe for a renamed core service), union Ollama under **any** manager — require the owner-only `manage_infrastructure` permission (`User.ROLE_PERMISSIONS['owner']`) plus a server-enforced typed confirmation (`confirm_name` in the request body) that must equal the **resolved target's** name, never an alias row's own name. The owner gate is checked before action-availability, so a non-owner always gets 403 `insufficient_role`, never a 409 about an unavailable action. Every lifecycle POST is audited (`old_value`/`new_value`, including refused 403/409s once resolution has run) and rate-limited on a dedicated `service_control` budget. Ollama control agent routes (`/ollama/start|stop|restart`) are registered before the generic `/{service_name}/{action}` routes — Starlette matches in registration order, and the parametrized route would otherwise silently swallow them. The Ollama card (`GET /ollama/health`, `POST /ollama/{action}`) resolves through `resolve_ollama_manager`, which runs the **same** `resolve_manager` every registry row uses — there is no separate Ollama-specific manager-resolution path. Every admin-backend HTTP call to the resolved Ollama URL (health probe, `/api/tags`, `/api/ps`, model load/unload, and the two `GET`/`POST /api/settings/ollama-url` reachability probes) is gated by the runtime `check_ssrf_safe` allowlist (`HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`); `POST /api/settings/ollama-url` additionally validates the URL at the write boundary (scheme, length, IMDS/link-local/multicast/unspecified/`.svc`/`.cluster.local` blocklist, loopback allowed only outside a K8s pod) **before** that reachability probe ever runs.
- **Auth-hardening startup behavior** (ATHENA-14, Campaign 3): `POST /api/auth/local-login` is defended by three layers applied on top of the existing PBKDF2-600k password hash. (1) Per-IP rate limit via fastapi-limiter (Redis-backed; custom identifier uses `request.client.host` only, ignoring `X-Forwarded-For` to defeat IP-rotation bypass; default 5 req/min/IP, configurable via `LOGIN_RATE_LIMIT_PER_MINUTE`). (2) Per-username DB lockout: `users.failed_login_count` is incremented atomically on each wrong-password attempt; the account locks for `LOGIN_LOCKOUT_MINUTES` (default 30) once `LOGIN_LOCKOUT_THRESHOLD` (default 10) is reached; lockout is idempotent — past-threshold attempts do not extend the window. (3) `LOGIN_MINIMUM_DELAY_MS` (default 400 ms) wall-time floor applied to every failure path so all four branches (not-found, inactive, locked, wrong-password) take the same wall time, closing timing enumeration. All four failure branches return the same 401 generic response (`"Invalid username or password"`) — `403 Account inactive` no longer emitted (behavioral change from pre-ATHENA-14). In `DEV_MODE=true` the rate limiter no-ops; lockout and timing floor remain active.

### Admin Frontend Escaping (ATHENA-66)

`admin/frontend/escape-html.js` is the **only** file that may define
`escapeHtml`/`escapeJsAttr` — enforced by `scripts/check-escape-html-uniqueness.py`
and frozen against runtime overwrite via `Object.defineProperty`. Full
contract (which primitive for which context, the six contexts
`escapeJsAttr` is wrong for, the callee-sink and handler-text-builder
rules, and the "add a new frontend file" checklist) lives in
`admin/frontend/README.md` — read that before touching any `on*=` handler
or adding a new script tag. `.github/workflows/frontend-escaping.yml`
enforces the invariants on every PR; this line points at the scripts
rather than restating rules it cannot enforce (a prior CLAUDE.md invariant
in this file was found violated with zero CI signal — the enforcement
belongs in the gate, not the prose).

## File Structure

```
os-project-athena/
├── admin/
│   ├── backend/          # FastAPI admin backend
│   └── frontend/         # Admin web UI (vanilla JS + nginx)
├── apps/                 # User-facing web apps (chat embed proxy + Jarvis voice/chat web UI)
│   ├── chat-embed/       # CORS-relay proxy for embedding Athena in external sites
│   └── jarvis-web/       # Jarvis voice + chat web interface (LiveKit-based)
├── src/
│   ├── control_agent/    # Service watchdog (runs on the host with Ollama)
│   ├── gateway/          # API gateway
│   ├── jetson/           # NVIDIA Jetson edge deployment
│   ├── mode_service/     # Mode management
│   ├── orchestrator/     # Query orchestration
│   ├── rag/              # RAG services (23 services)
│   ├── shared/           # Shared Python modules
│   └── sms/              # SMS notification service
├── manifests/
│   └── athena-prod/      # Kubernetes manifests
├── scripts/              # Build and deployment scripts
└── thoughts/             # Planning documents
```

## Important Notes

- **ALWAYS build with `--platform linux/amd64`** when building from Apple Silicon - the K8s cluster is AMD64 and images will fail with "exec format error" otherwise
- **NEVER break existing functionality** when adding new features. Changes should be additive and backwards-compatible. If a feature like Service Control relies on the Control Agent, new code must work with that pattern, not bypass it
- **ALWAYS consolidate and expose functionality through the Admin UI** when possible. The Admin UI should be the central management interface for all system operations. When adding new features, health checks, configuration options, or service controls, make sure they are accessible and manageable through the Admin UI rather than requiring command-line access or direct API calls
- All services use `imagePullPolicy: Always` during development
- RAG services without required API keys will start but return errors for queries
- RAG service additions must pass `make smoke-rags SERVICE=<image-name>` before merge; CI enforces this on PRs touching `src/rag/**` or `src/shared/**`
- The orchestrator timeout is 120 seconds to accommodate slower LLM inference
- qwen3 models have `/no_think` optimization enabled to reduce response time

## Plane Project
- Workspace: agile-solutions-group
- Project ID: 4f49cfbf-1257-45da-8c67-f56fc2ad5ad8
- Project Name: Project Athena
- Identifier: ATHENA
