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
git clone https://github.com/your-org/project-athena.git
cd project-athena
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

### 3. Start with Docker Compose

```bash
# Minimal deployment (core services only)
docker compose up -d

# Or with specific modules
docker compose --profile home-assistant --profile weather up -d
```

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

## Deployment Options

### Local Development

For development and testing on a single machine:

```bash
# 1. Install Python dependencies
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt

# 2. Start infrastructure (PostgreSQL, Redis)
docker compose up -d postgres redis

# 3. Run database migrations
cd admin/backend
alembic upgrade head
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

#### With Module Profiles

```bash
# Core + Home Assistant module
docker compose --profile home-assistant up -d

# Core + All RAG services
docker compose --profile rag-all up -d

# Full deployment
docker compose --profile full up -d

# Custom selection
docker compose --profile home-assistant --profile weather --profile news up -d
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

```bash
# Create namespace
kubectl create namespace athena

# Create secrets
kubectl -n athena create secret generic athena-db-credentials \
  --from-literal=password=your-db-password

kubectl -n athena create secret generic athena-encryption \
  --from-literal=encryption-key=your-encryption-key \
  --from-literal=encryption-salt=your-salt \
  --from-literal=session-secret=your-session-secret \
  --from-literal=jwt-secret=your-jwt-secret
```

#### Deploy Core Services

```bash
# Apply core manifests
kubectl apply -f k8s/core/

# This deploys:
# - PostgreSQL StatefulSet
# - Redis Deployment
# - Admin Backend Deployment + Service
# - Gateway Deployment + Service
# - Orchestrator Deployment + Service
```

#### Deploy Optional Modules

```bash
# Home Assistant integration
kubectl apply -f k8s/modules/home-assistant/

# Guest Mode
kubectl apply -f k8s/modules/guest-mode/

# Notifications
kubectl apply -f k8s/modules/notifications/

# Jarvis Web
kubectl apply -f apps/jarvis-web/k8s/
```

#### Deploy RAG Services

```bash
# Deploy all RAG services
kubectl apply -f k8s/rag/

# Or deploy individually
kubectl apply -f k8s/rag/weather/
kubectl apply -f k8s/rag/news/
```

#### Configure Ingress

Example Traefik IngressRoute:

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: athena-ingress
  namespace: athena
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
  namespace: athena
data:
  # Admin Backend location
  ADMIN_API_URL: "http://athena-admin-backend.athena.svc.cluster.local:8080"

  # Gateway/Orchestrator on compute nodes
  GATEWAY_URL: "http://athena-gateway.athena.svc.cluster.local:8000"
  ORCHESTRATOR_URL: "http://athena-orchestrator.athena.svc.cluster.local:8001"

  # Ollama on GPU-enabled nodes
  OLLAMA_URL: "http://ollama.gpu-workloads.svc.cluster.local:11434"

  # Infrastructure services
  ATHENA_DB_HOST: "postgres.athena.svc.cluster.local"
  REDIS_HOST: "redis.athena.svc.cluster.local"
  QDRANT_HOST: "qdrant.athena.svc.cluster.local"
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
    value: "http://athena-orchestrator.athena.svc.cluster.local:8001"
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
kubectl -n athena logs -f deployment/athena-orchestrator
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
alembic upgrade head
```

**Reset database (development only):**
```bash
# Drop and recreate
dropdb athena
createdb athena
alembic upgrade head
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

## Next Steps

- [Module Configuration Guide](./MODULES.md) - Detailed module setup
- [Configuration Reference](./CONFIGURATION.md) - All environment variables
- [API Documentation](./API.md) - REST API reference
- [Development Guide](./DEVELOPMENT.md) - Contributing to Athena

---

## Support

- GitHub Issues: [project-athena/issues](https://github.com/your-org/project-athena/issues)
- Documentation: [project-athena/docs](https://github.com/your-org/project-athena/docs)
