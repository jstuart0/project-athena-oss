# CLAUDE.md - Project Athena

This file provides guidance to Claude Code when working with Project Athena.

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
│    (React UI)     │                                    │ (Voice Interface)│
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
| Admin Frontend | 80 | React web UI |
| Gateway | 8000 | API gateway/router |
| Orchestrator | 8001 | Query orchestration |
| Mode Service | 8022 | Mode management |
| Jarvis Web | 8000 | Voice web interface |
| Redis | 6379 | Caching |
| Ollama | 11434 | LLM inference |

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
psql -h YOUR_DB_HOST -U psadmin -d athena_prod

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

## File Structure

```
os-project-athena/
├── admin/
│   ├── backend/          # FastAPI admin backend
│   └── frontend/         # React admin UI
├── apps/
│   └── jarvis-web/       # Voice web interface
├── src/
│   ├── gateway/          # API gateway
│   ├── orchestrator/     # Query orchestration
│   ├── mode_service/     # Mode management
│   ├── rag/              # RAG services (23 services)
│   └── shared/           # Shared Python modules
├── manifests/
│   └── athena-prod/      # Kubernetes manifests
├── scripts/              # Build and deployment scripts
└── thoughts/             # Planning documents
```

## Important Notes

- **ALWAYS build with `--platform linux/amd64`** when building from Apple Silicon - the K8s cluster is AMD64 and images will fail with "exec format error" otherwise
- All services use `imagePullPolicy: Always` during development
- RAG services without required API keys will start but return errors for queries
- The orchestrator timeout is 120 seconds to accommodate slower LLM inference
- qwen3 models have `/no_think` optimization enabled to reduce response time
