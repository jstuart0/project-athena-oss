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
- Centralized configuration: 12 env vars (`OLLAMA_URL`, `LLM_SERVICE_URL`, `REDIS_URL`, `DATABASE_URL`, `SERVICE_API_KEY`, `DEFAULT_TIMEZONE`, `DEFAULT_CITY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `DEMO_MODE`, `DEV_MODE`, `CONTROL_AGENT_ENABLED`) are read via `get_config()` from `src/shared/config.py::AthenaConfig` (pydantic-settings BaseSettings). New env vars: prefer adding fields to `AthenaConfig` over inline `os.getenv` — see `CONTRIBUTING.md`.
- **Admin-backend startup gates** (ATHENA-12, Campaign 2): the admin-backend will not start under the following misconfigurations — all raise `SystemExit` before any DB or auth initialization:
  - `DEV_MODE=true` with a non-SQLite `DATABASE_URL` (xander:6)
  - `DEMO_MODE=true` with `DEV_MODE=false` (xander:16)
  - `OIDC_CLIENT_ID` set to `""`, `"demo-mode"`, or `"CONFIGURE_ME_OIDC_CLIENT_ID"` in production (xander:13/17)
  - `OIDC_ISSUER` empty, missing, or matching the `CONFIGURE_ME` placeholder in production (pre-existing gate from ATHENA-2, still in force)
  - IdP unreachable or `.well-known/openid-configuration` missing `issuer` field at startup (MED-E discovery-doc gate)
  - DB-loaded runtime issuer empty or placeholder after `configure_oauth_client()` (MED-A runtime-issuer gate)
  For local development: set `DEV_MODE=true` (uses SQLite in-memory; bypasses OIDC gates). For production: `OIDC_ISSUER` must point to a reachable, conformant OIDC IdP with an `issuer` field in its discovery document.

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
- The orchestrator timeout is 120 seconds to accommodate slower LLM inference
- qwen3 models have `/no_think` optimization enabled to reduce response time

## Plane Project
- Workspace: agile-solutions-group
- Project ID: 4f49cfbf-1257-45da-8c67-f56fc2ad5ad8
- Project Name: Project Athena
- Identifier: ATHENA
