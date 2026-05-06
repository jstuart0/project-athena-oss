# Project Athena Installation Guide

This guide covers installing Project Athena from scratch, including all deployment options, module configuration, and distributed deployment scenarios.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Architecture Overview](#architecture-overview)
4. [Configuration](#configuration)
   - [Required Environment Variables](#required-environment-variables)
   - [Service Location Configuration](#service-location-configuration)
   - [Cross-Service Communication](#cross-service-communication)
   - [LLM Model Configuration](#llm-model-configuration)
5. [Module Selection](#module-selection)
6. [Deployment Options](#deployment-options)
   - [Local Development](#local-development)
   - [Docker Compose](#docker-compose)
   - [Kubernetes](#kubernetes)
7. [Distributed Deployment](#distributed-deployment)
8. [Post-Installation](#post-installation)
9. [Troubleshooting](#troubleshooting)

---

## Deployment Prerequisites Checklist

Before running `deploy.sh` or `kubectl apply`, confirm each item:

- [ ] **Storage class** — a default storage class exists in your cluster (`kubectl get storageclass`)
- [ ] **OIDC provider** — you have an OIDC provider (Authentik, Keycloak, Google, etc.) and have registered the redirect URI
- [ ] **Secrets configured** — `create-secrets.sh` has been run and the required secrets exist in the target namespace: `athena-db-credentials`, `athena-encryption`, `athena-oidc`
- [ ] **`SERVICE_API_KEY` set** — the shared service-to-service key is in your secrets/env; it must be the same value across all services
- [ ] **`ALLOWED_CALLBACK_HOSTS` set** — if using the Control Agent for model downloads, this is non-empty
- [ ] **Manifest placeholders substituted** — manifests in `manifests/athena-prod/` contain `YOUR_REGISTRY` and similar placeholders; substitute them before applying
- [ ] **Container images built and pushed** — run `scripts/build-and-push.sh` to build all images for `linux/amd64` and push to your registry
- [ ] **HA token reviewed** — if upgrading from a deployment that used the Jetson edge module, revoke the Home Assistant token that was in git history at commit `794096b`

The `deploy.sh` script enforces items 3 through 5 automatically: it will abort with an actionable error if the namespace or required secrets are missing.

---

## Prerequisites

### Hardware Requirements

| Deployment | CPU | RAM | Storage | GPU |
|------------|-----|-----|---------|-----|
| Minimal | 4 cores | 8GB | 20GB | Optional |
| Standard | 8 cores | 16GB | 50GB | Recommended |
| Full | 16+ cores | 32GB+ | 100GB+ | Required for local LLM |

### Software Requirements

- **Python 3.11+**
- **Docker & Docker Compose** (for containerized deployment)
- **kubectl** (for Kubernetes deployment)
- **PostgreSQL 15+** (can be containerized)
- **Redis 7+** (can be containerized)

### Optional Dependencies

- **Ollama** - For local LLM inference
- **Qdrant** - For vector memory storage
- **Home Assistant** - For smart home integration

---

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/jstuart0/project-athena-oss.git
cd project-athena-oss
```

### 2. Copy and Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and set the required values:

```bash
# REQUIRED - Generate these values
ATHENA_DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')
ENCRYPTION_KEY=$(openssl rand -base64 32)
ENCRYPTION_SALT=$(openssl rand -base64 16)
SESSION_SECRET_KEY=$(openssl rand -base64 32)
JWT_SECRET=$(openssl rand -base64 32)

# REQUIRED - Set your admin backend URL
ADMIN_API_URL=http://localhost:8080
```

> **Admin URL resolution order** (`src/shared/admin_url.py`): `ADMIN_API_URL` → `ADMIN_BACKEND_URL` → `ADMIN_INTERNAL_URL` (deprecated) → `LOCAL_DEV=true` → K8s in-cluster auto-discovery → empty + warning. Set `ADMIN_API_URL` explicitly in all deployments. `ADMIN_INTERNAL_URL` is accepted as a low-priority fallback for backward compatibility but is deprecated and will be removed in a future release.
>
> **Upgrade note (2026-05-06)**: If you previously set `ADMIN_INTERNAL_URL` specifically to override `ADMIN_API_URL` for jarvis-web internal calls, set `ADMIN_API_URL` directly instead. The variable is still accepted as a low-priority fallback but will be removed in a future release.

### 3. Start with Docker Compose

```bash
# Start the core services (orchestrator + gateway)
docker compose up -d
```

> **Note:** The `docker-compose.yml` is a minimal 2-service file (orchestrator + gateway). For a full local setup including PostgreSQL, Redis, and the admin backend, run those services manually per step 4 below, or see `docs/CONFIGURATION.md` and `CONTRIBUTING.md` for guidance.

### 4. Access the Admin UI

Open http://localhost:8080 in your browser.

---

## Architecture Overview

Project Athena consists of these component groups:

```
┌─────────────────────────────────────────────────────────────────┐
│                         CORE SERVICES                           │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────────────────┐   │
│  │ Gateway  │→ │ Orchestrator │→ │ Admin Backend + Frontend│   │
│  │  :8000   │  │    :8001     │  │         :8080           │   │
│  └──────────┘  └──────────────┘  └─────────────────────────┘   │
├─────────────────────────────────────────────────────────────────┤
│                      INFRASTRUCTURE                             │
│  ┌────────────┐  ┌───────┐  ┌────────┐  ┌────────┐            │
│  │ PostgreSQL │  │ Redis │  │ Qdrant │  │ Ollama │            │
│  │   :5432    │  │ :6379 │  │ :6333  │  │ :11434 │            │
│  └────────────┘  └───────┘  └────────┘  └────────┘            │
├─────────────────────────────────────────────────────────────────┤
│                    OPTIONAL MODULES                             │
│  ┌─────────────────┐  ┌────────────┐  ┌───────────────┐       │
│  │ Home Assistant  │  │ Guest Mode │  │ Notifications │       │
│  │   Integration   │  │   :8022    │  │    :8050      │       │
│  └─────────────────┘  └────────────┘  └───────────────┘       │
├─────────────────────────────────────────────────────────────────┤
│                      RAG SERVICES                               │
│  Weather │ Sports │ News │ Dining │ Stocks │ Flights │ ...    │
│   :8010  │ :8017  │:8016 │ :8019  │ :8012  │ :8013   │        │
└─────────────────────────────────────────────────────────────────┘
```

### Service Responsibilities

| Service | Port | Description |
|---------|------|-------------|
| **Gateway** | 8000 | API entry point, intent pre-routing, session management |
| **Orchestrator** | 8001 | Query processing, LLM coordination, tool execution |
| **Admin Backend** | 8080 | Configuration API, admin UI, credential management |
| **Mode Service** | 8022 | Guest mode restrictions (optional) |
| **Notifications** | 8050 | Proactive voice notifications (optional) |
| **RAG Services** | 8010-8033 | Domain-specific data retrieval |

---

## Configuration

### Required Environment Variables

These MUST be set before starting services:

```bash
# Database (REQUIRED)
ATHENA_DB_PASSWORD=your-secure-password

# Admin Backend (REQUIRED)
ADMIN_API_URL=http://localhost:8080  # Or your admin server URL
ENCRYPTION_KEY=your-32-char-base64-key
ENCRYPTION_SALT=your-16-char-base64-salt
SESSION_SECRET_KEY=your-32-char-base64-key
JWT_SECRET=your-32-char-base64-key

# Service-to-service authentication (REQUIRED in production)
# All internal services send this as the X-Service-Key header.
# Generate: openssl rand -base64 32
SERVICE_API_KEY=your-service-key

# OIDC / SSO (REQUIRED in production for admin UI login)
# The admin backend hard-fails at startup if OIDC_ISSUER is empty or
# set to a placeholder value when ENVIRONMENT != development.
OIDC_ISSUER=https://auth.example.com/application/o/athena-admin/
OIDC_CLIENT_ID=your-oidc-client-id
OIDC_CLIENT_SECRET=your-oidc-client-secret
OIDC_REDIRECT_URI=https://athena.example.com/auth/callback

# Control Agent callback allowlist (REQUIRED if using model downloads via Control Agent)
# Comma-separated hostnames. Empty = fail-closed (all callbacks rejected).
# Local dev: ALLOWED_CALLBACK_HOSTS=localhost
# Kubernetes in-cluster: ALLOWED_CALLBACK_HOSTS=athena-admin-backend.athena-prod.svc.cluster.local
ALLOWED_CALLBACK_HOSTS=

# Location defaults (OPTIONAL — leave blank for no default)
DEFAULT_CITY=
DEFAULT_STATE=
DEFAULT_TIMEZONE=UTC
```

### Service Location Configuration

Configure where each service runs:

```bash
# Core Services
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8000
ORCHESTRATOR_HOST=0.0.0.0
ORCHESTRATOR_PORT=8001
ADMIN_PORT=8080

# Infrastructure Services
ATHENA_DB_HOST=localhost       # PostgreSQL host
ATHENA_DB_PORT=5432
REDIS_HOST=localhost           # Redis host
REDIS_PORT=6379
QDRANT_HOST=localhost          # Qdrant host
QDRANT_PORT=6333
OLLAMA_HOST=localhost          # Ollama host
OLLAMA_PORT=11434

# Or use full URLs (takes precedence)
OLLAMA_URL=http://gpu-server:11434
QDRANT_URL=http://vector-db:6333
REDIS_URL=redis://cache-server:6379/0
```

### OIDC Configuration and Startup Gates

The admin-backend enforces fail-closed startup behavior for OIDC. If any of the checks below fail, the process exits before accepting connections.

**Requirements for a production deployment:**

1. `OIDC_ISSUER` must be a non-empty URL that is not the `CONFIGURE_ME_OIDC_ISSUER` placeholder. The value must exactly match the `iss` claim that your IdP puts in issued ID tokens — mismatches will cause token validation failures after the OIDC callback.
2. `OIDC_CLIENT_ID` must be your IdP's real client ID. The values `""`, `"demo-mode"`, and `"CONFIGURE_ME_OIDC_CLIENT_ID"` are rejected at startup.
3. The IdP must be reachable at startup. The backend fetches `<OIDC_ISSUER>/.well-known/openid-configuration` during `startup_event()`. If the fetch fails or the document does not contain an `issuer` field, the process exits with `FATAL: OIDC discovery metadata fetch failed`. Sequence the admin-backend pod to start after the IdP is available (init container or readiness gate).

**If you are upgrading from a release prior to ATHENA-12:** OIDC `iss`/`aud`/`exp` token validation was previously disabled via a `claims_options` override. That override has been removed. Tokens with an `iss` claim that does not match `OIDC_ISSUER` will now be rejected. Verify your IdP's issuer URL matches `OIDC_ISSUER` before upgrading.

**For local development:** set `DEV_MODE=true`. The backend uses SQLite in-memory and skips all OIDC startup gates.

**Post-OIDC-callback URL contract:** the backend redirects to `<FRONTEND_URL>?logged_in=1` after a successful OIDC callback. The admin frontend reads this signal, clears any stale `localStorage.auth_token`, and fetches the JWT from `/api/auth/session-token`. `FRONTEND_URL` must not include a query string — a `FRONTEND_URL` that already ends in `?foo=bar` would produce a malformed double-query-string redirect.

**Deferred:** the WebSocket connection to admin-jarvis uses `?token=<jwt>` in the upgrade URL. This is a distinct exposure (backend contract change required) tracked separately and not addressed in this release.

---

### Cross-Service Communication

When services run on different hosts, configure these URLs:

```bash
# Tell Orchestrator where Admin Backend is
ADMIN_API_URL=http://admin-server:8080

# Tell Gateway where Orchestrator is
ORCHESTRATOR_URL=http://compute-server:8001

# Tell Admin Backend where services are
GATEWAY_URL=http://compute-server:8000
SERVICE_HOST=compute-server
RAG_SERVICE_HOST=compute-server
```

### LLM Model Configuration

Project Athena automatically configures a default LLM model for all components on first startup. This provides a working out-of-the-box experience.

#### Default Behavior

On startup, the Admin Backend will:
1. **Seed the database** with LLM backend configuration and component model assignments
2. **Auto-pull the model** from Ollama if not already available

#### Configuration Options

```bash
# Default model for all orchestrator components
# Recommended: qwen3:4b (best balance of speed and quality)
# Alternatives: phi3:mini (faster), llama3.2:3b (good alternative)
ATHENA_DEFAULT_MODEL=qwen3:4b

# Enable/disable automatic database seeding (default: true)
# Set to false if manually configuring LLM backends via Admin UI
ATHENA_SEED_DEFAULTS=true

# Enable/disable automatic model downloading (default: true)
# Set to false if pre-pulling models or using external LLM
ATHENA_AUTO_PULL_MODELS=true
```

#### Component Model Assignments

The following components are automatically configured with the default model:

| Component | Description | Default Temperature |
|-----------|-------------|---------------------|
| `intent_classifier` | Classifies user queries into categories | 0.3 |
| `tool_calling_simple` | Selects RAG tools for simple queries | 0.7 |
| `tool_calling_complex` | Selects RAG tools for complex queries | 0.7 |
| `tool_calling_super_complex` | Handles highly complex queries | 0.7 |
| `response_synthesis` | Generates natural language responses | 0.7 |
| `fact_check_validation` | Validates responses for accuracy | 0.1 |
| `smart_home_control` | Extracts device commands | 0.1 |
| `response_validator_primary` | Primary cross-validation model | 0.1 |
| `response_validator_secondary` | Secondary cross-validation model | 0.1 |
| `conversation_summarizer` | Compresses conversation history | 0.3 |

#### Manual Configuration

If you prefer to configure models manually:

1. Disable automatic seeding:
   ```bash
   ATHENA_SEED_DEFAULTS=false
   ```

2. Pre-pull your desired models:
   ```bash
   ollama pull qwen3:4b
   ollama pull phi3:mini  # Optional: for cross-validation
   ```

3. Configure via Admin UI:
   - Go to Admin UI → LLM → Backends to add LLM backends
   - Go to Admin UI → LLM → Components to assign models to components

#### Using Different Models per Component

After initial setup, you can customize models per component via the Admin UI:

- **Fast tasks** (intent classification): Use smaller models like `phi3:mini`
- **Complex tasks** (response synthesis): Use larger models like `qwen3:4b` or `llama3.2:3b`
- **Validation**: Use different model families for cross-validation accuracy

---

## Module Selection

### Available Modules

| Module | Env Variable | Default | Description |
|--------|-------------|---------|-------------|
| Home Assistant | `MODULE_HOME_ASSISTANT` | `true` | Smart home control |
| Guest Mode | `MODULE_GUEST_MODE` | `true` | Rental/guest restrictions |
| Notifications | `MODULE_NOTIFICATIONS` | `true` | Proactive voice alerts |
| Jarvis Web | `MODULE_JARVIS_WEB` | `true` | Browser voice interface |
| Monitoring | `MODULE_MONITORING` | `false` | Grafana/Prometheus |

### Enable/Disable Modules

In your `.env` file:

```bash
# Enable these modules
MODULE_HOME_ASSISTANT=true
MODULE_GUEST_MODE=true
MODULE_NOTIFICATIONS=true
MODULE_JARVIS_WEB=true

# Disable monitoring (requires separate Prometheus/Grafana setup)
MODULE_MONITORING=false
```

### RAG Services

Each RAG service can be enabled independently. Add API keys for services you want:

```bash
# Weather (recommended - most commonly used)
OPENWEATHER_API_KEY=your-key

# Web Search (recommended - fallback for unknown queries)
BRAVE_API_KEY=your-key

# News
NEWSAPI_KEY=your-key

# Entertainment
TMDB_API_KEY=your-key
TICKETMASTER_API_KEY=your-key

# Food & Dining
SPOONACULAR_API_KEY=your-key
YELP_API_KEY=your-key

# Finance
ALPHA_VANTAGE_API_KEY=your-key

# Sports
THESPORTSDB_API_KEY=your-key

# Flights (paid tier only)
FLIGHTAWARE_API_KEY=your-key
```

---

## Control Agent (optional)

The Control Agent is a lightweight HTTP server that runs **on the same host as Ollama** (or alongside any Docker / launchd services you want to manage). It gives Athena's admin backend the ability to start, stop, and restart Ollama, query Docker container status, and manage download of Hugging Face models to the host filesystem.

**When you'd want it:**
- Mac Studio / bare-metal deployments where Ollama runs as a local process (brew service or launchd)
- Any deployment that uses the admin UI's Model Downloads or Mission Control panels
- Hosts where you want Athena to auto-restart the gateway process via the orchestrator keepalive

**When you don't need it:**
- Pure Kubernetes deployments where Ollama runs inside the cluster (`ollama` deployment in the same namespace)
- Read-only or chat-only deployments with no model management via the admin UI

### Enabling the Control Agent

Set two environment variables in your `.env` or private kubeconfig overlay:

```bash
CONTROL_AGENT_ENABLED=true
CONTROL_AGENT_URL=http://your-control-agent-host:8099
```

The public `manifests/athena-prod/config.yaml` deliberately does **not** enable the Control Agent — it is host-specific infrastructure. Add these vars to your private overlay rather than editing the public manifest.

### Starting the Control Agent

On the host that runs Ollama:

```bash
cd path/to/project-athena/src/control_agent
nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8099 > /tmp/control_agent.log 2>&1 &
```

Verify it is reachable:

```bash
curl http://your-host:8099/health
curl http://your-host:8099/ollama/health
```

### Disabled-path behavior

When `CONTROL_AGENT_ENABLED=false` (the default), all admin-backend routes and orchestrator startup that would normally contact the Control Agent short-circuit cleanly:

- **Debug Logs panel** — shows "Control Agent is disabled" instead of trying to connect
- **Model Downloads** — `create` / `retry` return HTTP 503 (no ghost DB rows); `delete` returns 503 when an on-host file would need cleanup; record-only deletes succeed
- **Service Control containers** — returns an empty list (no 500 errors)
- **Ollama health** — returns `status: control_agent_disabled`, `host: null`
- **Orchestrator gateway keepalive** — logs `gateway_keepalive_skipped` and continues startup normally

Valid values for `CONTROL_AGENT_ENABLED`: `true` / `false` / `1` / `0`. Do not set to a blank string.

---

## Deployment Options

### Local Development

For development and testing on a single machine:

```bash
# 1. Install Python dependencies
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt

# 2. Start infrastructure (PostgreSQL, Redis)
# Run PostgreSQL and Redis locally or via separate containers.
# The project docker-compose.yml contains only the orchestrator and gateway services;
# start PostgreSQL and Redis independently (e.g., via Homebrew, system packages, or
# docker run) and set ATHENA_DB_HOST / REDIS_HOST in your .env accordingly.

# 3. Run database migrations
cd admin/backend
alembic upgrade 053  # use 053, not head — repo has two alembic heads
cd ../..

# 4. Start services (in separate terminals)
# Terminal 1: Admin Backend
cd admin/backend && uvicorn main:app --host 0.0.0.0 --port 8080

# Terminal 2: Gateway
cd src/gateway && uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 3: Orchestrator
cd src/orchestrator && uvicorn main:app --host 0.0.0.0 --port 8001

# Terminal 4: RAG Services (optional)
cd src/rag/weather && uvicorn main:app --host 0.0.0.0 --port 8010
```

### Docker Compose

#### Basic Deployment

```bash
# Start all core services
docker compose up -d
```

#### Building Images

```bash
# Build all images
docker compose build

# Build specific service
docker compose build orchestrator

# Build with no cache
docker compose build --no-cache
```

### Kubernetes

#### Prerequisites

- Kubernetes cluster (1.25+)
- kubectl configured
- Container registry access
- Helm (optional, for dependencies)

#### Namespace Setup

Use the provided `create-secrets.sh` script, which is idempotent: running it multiple times against a cluster that already has the secrets will skip any secret that already has all required keys rather than rotating them.

```bash
# Populate config.env or .env.secrets with your values, then:
./scripts/create-secrets.sh
```

To create secrets manually:

```bash
kubectl create namespace athena-prod

kubectl -n athena-prod create secret generic athena-db-credentials \
  --from-literal=password=your-db-password

kubectl -n athena-prod create secret generic athena-encryption \
  --from-literal=encryption-key=your-encryption-key \
  --from-literal=encryption-salt=your-salt \
  --from-literal=session-secret=your-session-secret \
  --from-literal=jwt-secret=your-jwt-secret

kubectl -n athena-prod create secret generic athena-oidc \
  --from-literal=oidc-client-id=your-client-id \
  --from-literal=oidc-client-secret=your-client-secret \
  --from-literal=oidc-issuer=https://auth.example.com/application/o/athena-admin/
```

#### Qdrant Persistent Storage

Qdrant uses a PersistentVolumeClaim by default. Before applying `manifests/athena-prod/`, edit `qdrant.yaml` and replace `YOUR_STORAGE_CLASS` with your cluster's StorageClass:

```bash
kubectl get storageclass
# Common values: standard, gp2, local-path, longhorn, ceph-block
```

**Migrating from emptyDir** (existing deployments): conversation memory and embeddings stored on emptyDir are lost when Qdrant pods restart. Migrating to PVC starts fresh — this is a one-time data loss for previously-running deployments. To preserve current memory: snapshot Qdrant collections via the API before applying, then restore after the new PVC mounts.

#### Deploy Core Services

`deploy.sh` runs a pre-flight check before applying manifests. It verifies that the `athena-prod` namespace exists and that the required secrets (`athena-db-credentials`, `athena-encryption`, `athena-oidc`) are present. If any are missing it aborts with an error pointing you to `create-secrets.sh`.

#### First-time deployment (initial setup)

On your first cluster deployment, pass `--first-run` to apply the one-shot Ollama model-pull Job and wait for it to complete before continuing. Without this, Ollama starts with no models loaded and LLM calls will fail with "model not found".

```bash
# First-time deployment — includes the one-shot model-pull Job
./scripts/deploy.sh deploy --first-run

# Subsequent deploys (after initial setup)
./scripts/deploy.sh deploy
```

> **Model note:** The first-run Job pulls `qwen3:4b` (smaller variant) for fast initial deployment; the runtime default in `manifests/athena-prod/config.yaml` is `qwen3:4b-instruct-2507-q4_K_M` (longer-context Q4 quant). Reconciling the two is tracked as a deferred follow-up — for now, the Admin Backend's `ATHENA_AUTO_PULL_MODELS` will pull the configured default on startup if it is not already available.

Or apply manifests directly (no pre-flight):

```bash
# Apply core manifests
kubectl apply -f manifests/athena-prod/

# This deploys core services including:
# - Admin Backend Deployment + Service
# - Admin Frontend Deployment + Service
# - Gateway Deployment + Service
# - Orchestrator Deployment + Service
# - Redis Deployment
# - Ollama Deployment
# - Qdrant Deployment
```

#### Deploy Optional Modules

```bash
# Jarvis Web
kubectl apply -f manifests/athena-prod/jarvis-web.yaml
```

> **Note:** Additional modules (Home Assistant, Guest Mode, Notifications) are configured via environment variables. See [Module Configuration Guide](./MODULES.md) for details.

#### Deploy RAG Services

```bash
# Deploy all RAG services
kubectl apply -f manifests/athena-prod/rag-services.yaml
```

#### Configure Ingress

Example Traefik IngressRoute:

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: athena-ingress
  namespace: athena-prod
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`athena.your-domain.com`)
      kind: Rule
      services:
        - name: athena-gateway
          port: 8000
    - match: Host(`athena-admin.your-domain.com`)
      kind: Rule
      services:
        - name: athena-admin-backend
          port: 8080
  tls:
    secretName: athena-tls
```

---

## Distributed Deployment

Project Athena supports flexible distributed deployment where services run on different hosts.

### Topology Examples

#### Example 1: Separate Admin and Compute

```
┌─────────────────────┐     ┌─────────────────────┐
│   Admin Server      │     │   Compute Server    │
│   192.168.1.10      │     │   192.168.1.20      │
├─────────────────────┤     ├─────────────────────┤
│ • Admin Backend     │────▶│ • Gateway           │
│ • Admin Frontend    │     │ • Orchestrator      │
│ • PostgreSQL        │◀────│ • Ollama            │
│ • Redis             │     │ • RAG Services      │
└─────────────────────┘     └─────────────────────┘
```

**Admin Server `.env`:**
```bash
ADMIN_PORT=8080
ATHENA_DB_HOST=localhost
REDIS_HOST=localhost

# Point to compute server
GATEWAY_URL=http://192.168.1.20:8000
ORCHESTRATOR_URL=http://192.168.1.20:8001
SERVICE_HOST=192.168.1.20
RAG_SERVICE_HOST=192.168.1.20
```

**Compute Server `.env`:**
```bash
GATEWAY_PORT=8000
ORCHESTRATOR_PORT=8001
OLLAMA_HOST=localhost

# Point to admin server
ADMIN_API_URL=http://192.168.1.10:8080
ATHENA_DB_HOST=192.168.1.10
REDIS_HOST=192.168.1.10
```

#### Example 2: Dedicated GPU Server for Ollama

```
┌─────────────────────┐     ┌─────────────────────┐
│   Main Server       │     │   GPU Server        │
│   192.168.1.10      │     │   192.168.1.30      │
├─────────────────────┤     ├─────────────────────┤
│ • All Services      │────▶│ • Ollama            │
│   except Ollama     │     │   (NVIDIA GPU)      │
└─────────────────────┘     └─────────────────────┘
```

**Main Server `.env`:**
```bash
# Point Ollama to GPU server
OLLAMA_URL=http://192.168.1.30:11434
```

**GPU Server:**
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Configure to listen on all interfaces
# Edit /etc/systemd/system/ollama.service
Environment="OLLAMA_HOST=0.0.0.0:11434"

# Restart Ollama
sudo systemctl restart ollama

# Pull required models (qwen3:4b is the default)
ollama pull qwen3:4b

# Optional: additional models for cross-validation or fallback
ollama pull phi3:mini
ollama pull llama3.2:3b
```

> **Note:** If `ATHENA_AUTO_PULL_MODELS=true` (default), the Admin Backend will automatically pull the default model on startup. You only need to manually pull models if auto-pull is disabled or you want additional models.

#### Example 3: Full Kubernetes Distribution

```yaml
# ConfigMap for service discovery
apiVersion: v1
kind: ConfigMap
metadata:
  name: athena-config
  namespace: athena-prod
data:
  # Admin Backend location
  ADMIN_API_URL: "http://athena-admin-backend.athena-prod.svc.cluster.local:8080"

  # Gateway/Orchestrator on compute nodes
  GATEWAY_URL: "http://athena-gateway.athena-prod.svc.cluster.local:8000"
  ORCHESTRATOR_URL: "http://athena-orchestrator.athena-prod.svc.cluster.local:8001"

  # Ollama on GPU-enabled nodes
  OLLAMA_URL: "http://ollama.gpu-workloads.svc.cluster.local:11434"

  # Infrastructure services
  ATHENA_DB_HOST: "postgres.athena-prod.svc.cluster.local"
  REDIS_HOST: "redis.athena-prod.svc.cluster.local"
  QDRANT_HOST: "qdrant.athena-prod.svc.cluster.local"
```

### Service Discovery Patterns

#### Docker Compose (Same Network)

```yaml
services:
  gateway:
    environment:
      - ORCHESTRATOR_URL=http://orchestrator:8001
      - ADMIN_API_URL=http://admin-backend:8080

  orchestrator:
    environment:
      - ADMIN_API_URL=http://admin-backend:8080
      - OLLAMA_URL=http://ollama:11434
```

#### Kubernetes (DNS-Based)

```yaml
# Services automatically get DNS names:
# <service-name>.<namespace>.svc.cluster.local

env:
  - name: ORCHESTRATOR_URL
    value: "http://athena-orchestrator.athena-prod.svc.cluster.local:8001"
```

---

## Post-Installation

### 1. Verify Services

```bash
# Check Gateway health
curl http://localhost:8000/health

# Check Orchestrator health
curl http://localhost:8001/health

# Check Admin Backend health
curl http://localhost:8080/api/health
```

### 2. Create Admin User

Access the Admin UI at http://localhost:8080 and complete the setup wizard.

### 3. Configure Home Assistant (Optional)

If using Home Assistant integration:

1. Go to Admin UI → Settings → Integrations
2. Enter your Home Assistant URL (e.g., `http://homeassistant.local:8123`)
3. Generate a Long-Lived Access Token in Home Assistant
4. Enter the token in the Admin UI

### 4. Add API Keys

Configure RAG services via Admin UI → Settings → API Keys:

- OpenWeatherMap for weather queries
- Brave Search for web searches
- TMDB for movie/TV information
- etc.

### 5. Test the System

```bash
# Send a test query
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the weather like today?", "room": "living_room"}'
```

---

## Troubleshooting

### Service Won't Start

**Check environment variables:**
```bash
# Verify required vars are set
env | grep ATHENA
env | grep ADMIN_API_URL
```

**Check database connection:**
```bash
# Test PostgreSQL connection
psql -h $ATHENA_DB_HOST -U athena -d athena -c "SELECT 1"
```

**Check logs:**
```bash
# Docker Compose
docker compose logs -f orchestrator

# Kubernetes
kubectl -n athena-prod logs -f deployment/athena-orchestrator
```

### Module Not Working

**Check if module is enabled:**
```bash
curl http://localhost:8080/api/modules | jq
```

**Check module health:**
```bash
# For Home Assistant
curl http://localhost:8080/api/integrations/home-assistant/status
```

### LLM Errors

**Check Ollama is running:**
```bash
curl http://localhost:11434/api/tags
```

**Check model is loaded:**
```bash
ollama list
# If model missing (should auto-pull on startup):
ollama pull qwen3:4b
```

**Check LLM backend is configured:**
```bash
# Via Admin API (requires authentication)
curl http://localhost:8080/api/llm-backends

# Or check database directly
psql -h $ATHENA_DB_HOST -U athena -d athena \
  -c "SELECT model_name, enabled FROM llm_backends"
```

**Check component model assignments:**
```bash
psql -h $ATHENA_DB_HOST -U athena -d athena \
  -c "SELECT component_name, model_name FROM component_model_assignments"
```

**Force re-seed defaults:**
```bash
# Restart admin backend with seeding enabled
ATHENA_SEED_DEFAULTS=true
# The admin backend will re-seed on next startup
```

### Database Errors

**Run migrations:**
```bash
cd admin/backend
alembic upgrade 053
```

> **Note — two alembic heads:** this repo has a pre-existing divergence between the `004a` legacy branch and the primary chain. Running `alembic upgrade head` will fail with "Multiple head revisions are present" because there is no single head. Always target the primary chain explicitly with `alembic upgrade 053`. Merging/retiring the `004a` legacy branch is tracked as a separate follow-up campaign.

#### Migration 053 — clear legacy maintainer IPs from gateway_config (post-ATHENA-11)

If you deployed Athena before commit `4f6b159` and your `gateway_config` table
still contains rows with `http://192.168.10.167:*` (the maintainer's homelab IPs
left over from old column defaults), run:

```bash
cd admin/backend
alembic upgrade 053
```

The migration sets `orchestrator_url` and `ollama_fallback_url` to empty strings
for any row whose value matches the legacy IPs (exact match or trailing-slash
variant). Deployer-set values are unaffected. Use `alembic upgrade 053` (not
`head`) — the repo has a pre-existing two-head divergence (`004a` legacy +
primary chain) and `head` is ambiguous.

**Reset database (development only):**
```bash
# Drop and recreate
dropdb athena
createdb athena
alembic upgrade 053  # use 053, not head — repo has two alembic heads
```

### Network Issues (Distributed Deployment)

**Test connectivity:**
```bash
# From orchestrator server, test admin backend
curl http://admin-server:8080/api/health

# From admin server, test orchestrator
curl http://compute-server:8001/health
```

**Check firewall:**
```bash
# Required ports:
# 8000 - Gateway
# 8001 - Orchestrator
# 8080 - Admin Backend
# 5432 - PostgreSQL
# 6379 - Redis
# 11434 - Ollama
```

---

## Production Hardening Checklist {#production-hardening-checklist}

The default manifests in `manifests/athena-prod/` are tuned for fast iteration, not production stability. Before running traffic at scale, address these items.

### imagePullPolicy

All manifests currently set `imagePullPolicy: Always`. This forces a registry round-trip on every pod restart.

**Risk in production:** if your container registry is unreachable during a node drain or rolling restart, Kubernetes cannot pull the image and the pod will not start — even if the image is already cached on the node.

**Operator action:**
1. Pin all image tags to a specific digest or version (replace `:latest` with e.g. `:2026-05-06` or `@sha256:...`).
2. Flip `imagePullPolicy: Always` to `imagePullPolicy: IfNotPresent` in every manifest under `manifests/athena-prod/`.
3. Do steps 1 and 2 together — `IfNotPresent` with `:latest` will never re-pull even when you push a new image.

### Image tags

The Ollama Deployment (`ollama.yaml`) uses `ollama/ollama:latest` and the model-pull Job (`ollama-model-pull-job.yaml`) uses `curlimages/curl:latest`. Pin both to specific tags before running in production.

### Model-pull Job backoffLimit

`ollama-model-pull-job.yaml` does not set `backoffLimit`, so Kubernetes defaults to 6 retries. Consider setting `spec.backoffLimit: 2` or `3` for a cleaner failure signal if the initial pull fails.

### Model divergence (Job vs. ConfigMap default)

The first-run Job pulls `qwen3:4b` and `phi3:mini` (smaller, faster startup). The runtime ConfigMap default is `qwen3:4b-instruct-2507-q4_K_M` (longer-context Q4 quant). The two are intentionally different for fast first-run, but should be reconciled in a follow-up once the deployment is stable.

---

## Next Steps

- [Module Configuration Guide](./MODULES.md) - Detailed module setup
- [Configuration Reference](./CONFIGURATION.md) - All environment variables
- REST API reference — visit `http://localhost:8080/docs` (FastAPI Swagger UI) after starting the admin backend
- [Contributing to Athena](../CONTRIBUTING.md) - Development setup and contribution guidelines

---

## Support

- GitHub Issues: [project-athena-oss/issues](https://github.com/jstuart0/project-athena-oss/issues)
- Documentation: [project-athena-oss/docs](https://github.com/jstuart0/project-athena-oss/docs)
