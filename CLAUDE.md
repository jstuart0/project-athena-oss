# CLAUDE.md - Project Athena

This file provides guidance to Claude Code when working with Project Athena.

## ⚠️ CRITICAL: OSS-First Development

**Every fix, feature, and architectural change must be designed to work for any Athena deployment — not just Jay's specific home lab setup.**

This is the public OSS repository. All code must be implementation-agnostic:

1. **No hardcoded values** — IPs, hostnames, credentials, or domains must come from env vars, `src/shared/config.py`, or the admin panel
2. **No assumption of Jay's infrastructure** — code can't assume specific ports, hostnames, or service locations
3. **Configuration over convention** — if a value might differ between deployments, it must be configurable
4. **Test generalizability** — ask "would this work for someone else deploying Athena from scratch?"

**Examples:**
- ❌ `ha_url = "http://192.168.10.168:8123"` as a fallback
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
| Control Agent | 8099 | Service control (runs on Mac Studio) |

### Orchestrator pipeline architecture

`src/orchestrator/main.py` is the LangGraph pipeline entry point (8,758 lines as of ATHENA-10). The orchestrator package has 12 sibling modules extracted from the original god-object:

**Sibling modules at `src/orchestrator/`**

| Module | Purpose |
|---|---|
| `state.py` | Canonical definitions for `OrchestratorState`, `IntentCategory`, `ModelTier`, `ConversationContext`. Do not redefine these in `main.py` or elsewhere. |
| `helpers.py` | 17 stateless helpers. Helpers that need runtime singletons call `_runtime.get_X()` at call time (Pattern 1). |
| `mode_permission.py` | 6 mode/permission helpers (`get_current_mode`, `detect_owner_mode_command`, `extract_pin_from_query`, `activate_owner_override`, `check_intent_permission`, `check_entity_permission`) plus `OWNER_MODE_PATTERNS`. |
| `urls.py` | 15 service URL constants (13 RAG + `MODE_SERVICE_URL` + `NOTIFICATIONS_SERVICE_URL`). |
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

---

### Control Agent

The Control Agent runs on the Mac Studio (192.168.10.108) where Ollama runs. It provides HTTP endpoints to manage Ollama and other services.

**Location:** `src/control_agent/` (copied to Mac Studio at `~/dev/control_agent/`)

**Starting the Control Agent on Mac Studio:**
```bash
ssh jstuart@192.168.10.108
cd ~/dev/control_agent
nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8099 > /tmp/control_agent.log 2>&1 &
```

**Verify it's running:**
```bash
curl http://192.168.10.108:8099/health
curl http://192.168.10.108:8099/ollama/health
```

**K8s Configuration:**
The admin-backend needs `CONTROL_AGENT_URL=http://<your-control-agent-host>:8099` environment variable set.

**OSS-First default — opt-in required:**
The Control Agent is disabled by default (`CONTROL_AGENT_ENABLED=false`). Set `CONTROL_AGENT_ENABLED=true` in your private overlay or `.env` only if a Control Agent process is actually running on a host. OSS deployers without a Control Agent no longer see connection errors from admin-backend or orchestrator startup. For Jay's homelab, add both env vars to the private kubeconfig overlay:
```env
CONTROL_AGENT_ENABLED=true
CONTROL_AGENT_URL=http://192.168.10.108:8099
```

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
| RAG Services | `src/rag/<service>/Dockerfile` | `src/rag/<service>/` |

### Kubernetes Operations

```bash
# Always verify context first
kubectl config current-context

# Deploy all manifests
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
- Centralized configuration: 21 env vars (`OLLAMA_URL`, `LLM_SERVICE_URL`, `REDIS_URL`, `DATABASE_URL`, `SERVICE_API_KEY`, `DEFAULT_TIMEZONE`, `DEFAULT_CITY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `DEMO_MODE`, `DEV_MODE`, `CONTROL_AGENT_ENABLED`, `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_LOCKOUT_THRESHOLD`, `LOGIN_LOCKOUT_MINUTES`, `LOGIN_MINIMUM_DELAY_MS`, `SERVICE_REGISTRY_WRITE_PER_MINUTE`, `HEALTH_POLL_INTERVAL_SECONDS`, `HEALTH_POLL_TIMEOUT_SECONDS`, `HEALTH_POLL_CONCURRENCY`, `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`) are read via `get_config()` from `src/shared/config.py::AthenaConfig` (pydantic-settings BaseSettings). New env vars: prefer adding fields to `AthenaConfig` over inline `os.getenv` — see `CONTRIBUTING.md`.
- **Admin-backend startup gates** (ATHENA-12, Campaign 2): the admin-backend will not start under the following misconfigurations — all raise `SystemExit` before any DB or auth initialization:
  - `DEV_MODE=true` with a non-SQLite `DATABASE_URL` (xander:6)
  - `DEMO_MODE=true` with `DEV_MODE=false` (xander:16)
  - `OIDC_CLIENT_ID` set to `""`, `"demo-mode"`, or `"CONFIGURE_ME_OIDC_CLIENT_ID"` in production (xander:13/17)
  - `OIDC_ISSUER` empty, missing, or matching the `CONFIGURE_ME` placeholder in production (pre-existing gate from ATHENA-2, still in force)
  - IdP unreachable or `.well-known/openid-configuration` missing `issuer` field at startup (MED-E discovery-doc gate)
  - DB-loaded runtime issuer empty or placeholder after `configure_oauth_client()` (MED-A runtime-issuer gate)
  - `SERVICE_API_KEY` empty or set to `dev-service-key-change-in-production` in production (ATHENA-21, Campaign 5) — production startup raises `SystemExit` via the `_INSECURE_DEFAULTS` loop in `admin/backend/main.py:427-475` (`kind="secret"` treats empty AND placeholder as fatal). Bypass: set `DEV_MODE=true` for local development. Helper-level: `verify_service_or_oidc` returns 503 to any caller that sends `X-Service-Key` while `SERVICE_API_KEY` is unset (the dispatcher does not silently fall through to OIDC).
  For local development: set `DEV_MODE=true` (uses SQLite in-memory; bypasses OIDC gates). For production: `OIDC_ISSUER` must point to a reachable, conformant OIDC IdP with an `issuer` field in its discovery document.
- **Service-registry architecture (ATHENA-1, Campaign 4)**: The admin DB (`athena_service_registry` table) is the source of truth for all service definitions. The Control Agent is a health augmenter, not the authority — on startup it POSTs its local `PROCESS_SERVICES` manifest to `POST /api/service-registry/services` (authenticated with `X-Service-Key`), which upserts entries into `athena_service_registry` via `sync_registry_loop`. A background async health poller (`admin/backend/app/services/health_poller.py`) runs on a `HEALTH_POLL_INTERVAL_SECONDS` (default 30s) schedule and writes `health_status`/`last_health_check`/`last_error`/`last_response_time_ms` back to the table; the admin UI reads the cache instead of blocking on live pings. The unified service catalog is `GET /api/service-registry/services` (requires auth); on-demand refresh is `POST /api/service-registry/services/poll-now`. OSS deployers without a Control Agent have a fully functional service registry — CA is purely additive. Five new env vars govern this subsystem: `SERVICE_REGISTRY_WRITE_PER_MINUTE` (default 60), `HEALTH_POLL_INTERVAL_SECONDS` (default 30), `HEALTH_POLL_TIMEOUT_SECONDS` (default 5), `HEALTH_POLL_CONCURRENCY` (default 8), `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` (CIDRs/hostnames that override the RFC1918/loopback/ULA SSRF block — empty by default, required for K8s operators with in-cluster services on private subnets).
- **Auth-hardening startup behavior** (ATHENA-14, Campaign 3): `POST /api/auth/local-login` is defended by three layers applied on top of the existing PBKDF2-600k password hash. (1) Per-IP rate limit via fastapi-limiter (Redis-backed; custom identifier uses `request.client.host` only, ignoring `X-Forwarded-For` to defeat IP-rotation bypass; default 5 req/min/IP, configurable via `LOGIN_RATE_LIMIT_PER_MINUTE`). (2) Per-username DB lockout: `users.failed_login_count` is incremented atomically on each wrong-password attempt; the account locks for `LOGIN_LOCKOUT_MINUTES` (default 30) once `LOGIN_LOCKOUT_THRESHOLD` (default 10) is reached; lockout is idempotent — past-threshold attempts do not extend the window. (3) `LOGIN_MINIMUM_DELAY_MS` (default 400 ms) wall-time floor applied to every failure path so all four branches (not-found, inactive, locked, wrong-password) take the same wall time, closing timing enumeration. All four failure branches return the same 401 generic response (`"Invalid username or password"`) — `403 Account inactive` no longer emitted (behavioral change from pre-ATHENA-14). In `DEV_MODE=true` the rate limiter no-ops; lockout and timing floor remain active.

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
