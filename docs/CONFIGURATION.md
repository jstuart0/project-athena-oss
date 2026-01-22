# Project Athena Configuration Reference

Complete reference for all configuration options in Project Athena.

## Table of Contents

1. [Configuration Methods](#configuration-methods)
2. [Required Settings](#required-settings)
3. [Database Configuration](#database-configuration)
4. [Service URLs](#service-urls)
5. [Infrastructure Services](#infrastructure-services)
6. [Module Settings](#module-settings)
7. [API Keys](#api-keys)
8. [Voice Services](#voice-services)
9. [Security Settings](#security-settings)
10. [Advanced Settings](#advanced-settings)

---

## Configuration Methods

Configuration can be set via (in priority order):

1. **Admin Backend Database** - Runtime configuration via UI
2. **Environment Variables** - Set in shell or `.env` file
3. **Code Defaults** - Fallback values (only for non-sensitive settings)

### Using .env Files

```bash
# Copy template
cp .env.example .env

# Edit with your values
nano .env

# Values are automatically loaded by Docker Compose
docker compose up -d
```

### Environment Variable Expansion

Docker Compose supports variable expansion:

```yaml
# docker-compose.yml
services:
  orchestrator:
    environment:
      - OLLAMA_URL=${OLLAMA_URL:-http://localhost:11434}
```

---

## Required Settings

These MUST be set before starting services. Services will fail fast if missing.

| Variable | Description | Generate With |
|----------|-------------|---------------|
| `ATHENA_DB_PASSWORD` | PostgreSQL password | `openssl rand -base64 24 \| tr -d '/+='` |
| `ADMIN_API_URL` | Admin backend URL | Set to your admin server |
| `ENCRYPTION_KEY` | API key encryption | `openssl rand -base64 32` |
| `ENCRYPTION_SALT` | Encryption salt | `openssl rand -base64 16` |
| `SESSION_SECRET_KEY` | Session signing | `openssl rand -base64 32` |
| `JWT_SECRET` | JWT token signing | `openssl rand -base64 32` |

**Example:**
```bash
ATHENA_DB_PASSWORD=MySecurePassword123
ADMIN_API_URL=http://localhost:8080
ENCRYPTION_KEY=abc123def456...
ENCRYPTION_SALT=xyz789...
SESSION_SECRET_KEY=secret123...
JWT_SECRET=jwtsecret...
```

---

## Database Configuration

### PostgreSQL

| Variable | Default | Description |
|----------|---------|-------------|
| `ATHENA_DB_HOST` | `localhost` | Database host |
| `ATHENA_DB_PORT` | `5432` | Database port |
| `ATHENA_DB_NAME` | `athena` | Database name |
| `ATHENA_DB_USER` | `athena` | Database user |
| `ATHENA_DB_PASSWORD` | *required* | Database password |

**Full Connection String (alternative):**
```bash
DATABASE_URL=postgresql://athena:password@localhost:5432/athena
```

### Admin Database (Optional Separate DB)

| Variable | Default | Description |
|----------|---------|-------------|
| `ATHENA_ADMIN_DB_HOST` | `${ATHENA_DB_HOST}` | Admin DB host |
| `ATHENA_ADMIN_DB_PORT` | `${ATHENA_DB_PORT}` | Admin DB port |
| `ATHENA_ADMIN_DB_NAME` | `athena_admin` | Admin DB name |
| `ATHENA_ADMIN_DB_USER` | `${ATHENA_DB_USER}` | Admin DB user |

---

## Service URLs

### Core Services

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `0.0.0.0` | Gateway bind address |
| `GATEWAY_PORT` | `8000` | Gateway port |
| `GATEWAY_URL` | `http://localhost:8000` | Gateway URL (for other services) |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | Orchestrator bind address |
| `ORCHESTRATOR_PORT` | `8001` | Orchestrator port |
| `ORCHESTRATOR_URL` | `http://localhost:8001` | Orchestrator URL |
| `ADMIN_PORT` | `8080` | Admin backend port |
| `ADMIN_API_URL` | *required* | Admin backend URL |

### Module Services

| Variable | Default | Description |
|----------|---------|-------------|
| `MODE_SERVICE_URL` | `http://localhost:8022` | Guest Mode service |
| `NOTIFICATIONS_SERVICE_URL` | `http://localhost:8050` | Notifications service |
| `JARVIS_WEB_URL` | `http://localhost:3001` | Jarvis Web UI |
| `CONTROL_AGENT_URL` | `http://localhost:8099` | Service management API |

### Service Discovery Helpers

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_HOST` | `localhost` | Default host for all services |
| `RAG_SERVICE_HOST` | `localhost` | Default host for RAG services |

---

## Infrastructure Services

### Ollama (LLM)

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `localhost` | Ollama server host |
| `OLLAMA_PORT` | `11434` | Ollama server port |
| `OLLAMA_URL` | `http://localhost:11434` | Full URL (overrides host/port) |

**Kubernetes/Docker DNS:**
```bash
OLLAMA_URL=http://ollama.gpu-workloads.svc.cluster.local:11434
```

### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_URL` | `redis://localhost:6379/0` | Full URL (overrides host/port) |

### Qdrant (Vector Database)

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_HOST` | `localhost` | Qdrant host |
| `QDRANT_PORT` | `6333` | Qdrant port |
| `QDRANT_URL` | `http://localhost:6333` | Full URL (overrides host/port) |

### SearXNG (Web Search)

| Variable | Default | Description |
|----------|---------|-------------|
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG instance URL |

---

## Module Settings

### Module Enable/Disable

| Variable | Default | Description |
|----------|---------|-------------|
| `MODULE_HOME_ASSISTANT` | `true` | Enable Home Assistant integration |
| `MODULE_GUEST_MODE` | `true` | Enable Guest Mode restrictions |
| `MODULE_NOTIFICATIONS` | `true` | Enable proactive notifications |
| `MODULE_JARVIS_WEB` | `true` | Enable browser voice interface |
| `MODULE_MONITORING` | `false` | Enable Grafana/Prometheus |

### Home Assistant

| Variable | Default | Description |
|----------|---------|-------------|
| `HA_URL` | *(empty)* | Home Assistant URL |
| `HA_TOKEN` | *(empty)* | Long-Lived Access Token |
| `HA_WS_URL` | *(derived)* | WebSocket URL (usually auto-derived) |
| `MUSIC_ASSISTANT_URL` | *(empty)* | Music Assistant URL |

### Guest Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_ROOM` | `guest` | Default room for queries |
| `CLIMATE_ENTITY` | `climate.thermostat` | Climate control entity |
| `MIN_TEMP` | `65` | Minimum guest temperature |
| `MAX_TEMP` | `75` | Maximum guest temperature |

### Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus server |
| `GRAFANA_URL` | `http://grafana:3000` | Grafana server |

---

## API Keys

### Weather Services

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `OPENWEATHER_API_KEY` | 1,000/day | [openweathermap.org](https://openweathermap.org/api) |

### Search Services

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `BRAVE_API_KEY` | 2,000/month | [brave.com/search/api](https://brave.com/search/api/) |

### Entertainment

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `TMDB_API_KEY` | 1M/month | [themoviedb.org](https://www.themoviedb.org/settings/api) |
| `TICKETMASTER_API_KEY` | 5,000/day | [developer.ticketmaster.com](https://developer.ticketmaster.com/) |

### News & Information

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `NEWSAPI_KEY` | 100/day | [newsapi.org](https://newsapi.org/register) |

### Food & Dining

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `SPOONACULAR_API_KEY` | 150/day | [spoonacular.com](https://spoonacular.com/food-api) |
| `YELP_API_KEY` | 5,000/day | [yelp.com/developers](https://www.yelp.com/developers/v3/manage_app) |

### Finance & Sports

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `ALPHA_VANTAGE_API_KEY` | 500/day | [alphavantage.co](https://www.alphavantage.co/support/#api-key) |
| `THESPORTSDB_API_KEY` | Yes | [thesportsdb.com](https://www.thesportsdb.com/api.php) |

### Travel

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `FLIGHTAWARE_API_KEY` | Paid only | [flightaware.com](https://www.flightaware.com/commercial/aeroapi/) |

---

## Voice Services

### Wyoming Protocol (STT/TTS)

| Variable | Default | Description |
|----------|---------|-------------|
| `WYOMING_STT_HOST` | `localhost` | Speech-to-text host |
| `WYOMING_STT_PORT` | `10300` | STT port |
| `WYOMING_TTS_HOST` | `localhost` | Text-to-speech host |
| `WYOMING_TTS_PORT` | `10200` | TTS port |

### Voice Control

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICE_CONTROL_URL` | `http://localhost:8098` | Voice control API |
| `VOICE_API_URL` | `http://localhost:10201` | Voice API endpoint |

---

## Security Settings

### Encryption

| Variable | Description |
|----------|-------------|
| `ENCRYPTION_KEY` | 32-byte key for API key encryption |
| `ENCRYPTION_SALT` | 16-byte salt for key derivation |

### Session Management

| Variable | Description |
|----------|-------------|
| `SESSION_SECRET_KEY` | Secret for session signing |
| `JWT_SECRET` | Secret for JWT tokens |

### Authentication (Optional)

| Variable | Description |
|----------|-------------|
| `AUTHENTIK_CLIENT_ID` | Authentik OAuth client ID |
| `AUTHENTIK_CLIENT_SECRET` | Authentik OAuth client secret |
| `AUTHENTIK_ISSUER_URL` | Authentik issuer URL |

---

## Advanced Settings

### Performance Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `MODULE_HEALTH_CACHE_TTL` | `30` | Health check cache (seconds) |

### Development

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT` | `development` | Environment name |
| `DEBUG` | `true` | Enable debug logging |

### Container Registry

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTAINER_REGISTRY` | `docker.io` | Container registry URL |
| `CONTAINER_NAMESPACE` | `athena-voice` | Registry namespace |
| `IMAGE_TAG` | `latest` | Default image tag |

### Personalization

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_CITY` | *(empty)* | Default city for weather |
| `DEFAULT_STATE` | *(empty)* | Default state |
| `DEFAULT_COUNTRY` | `US` | Default country |
| `DEFAULT_TIMEZONE` | `UTC` | Default timezone |
| `DEFAULT_AMTRAK_STATION` | *(empty)* | Default Amtrak station code |

---

## RAG Service URLs

Override these for distributed RAG service deployment:

| Variable | Default Port | Description |
|----------|-------------|-------------|
| `RAG_WEATHER_URL` | 8010 | Weather service |
| `RAG_AIRPORTS_URL` | 8011 | Airports service |
| `RAG_SPORTS_URL` | 8017 | Sports service |
| `RAG_FLIGHTS_URL` | 8013 | Flights service |
| `RAG_EVENTS_URL` | 8014 | Events service |
| `RAG_STREAMING_URL` | 8015 | Streaming service |
| `RAG_STOCKS_URL` | 8012 | Stocks service |
| `RAG_NEWS_URL` | 8016 | News service |
| `RAG_WEBSEARCH_URL` | 8018 | Web search service |
| `RAG_DINING_URL` | 8019 | Dining service |
| `RAG_RECIPES_URL` | 8020 | Recipes service |
| `RAG_DIRECTIONS_URL` | 8030 | Directions service |

---

## Configuration Examples

### Minimal Local Development

```bash
# Required only
ATHENA_DB_PASSWORD=devpassword
ADMIN_API_URL=http://localhost:8080
ENCRYPTION_KEY=$(openssl rand -base64 32)
ENCRYPTION_SALT=$(openssl rand -base64 16)
SESSION_SECRET_KEY=$(openssl rand -base64 32)
JWT_SECRET=$(openssl rand -base64 32)

# Use all defaults for everything else
```

### Production Single Server

```bash
# Security
ATHENA_DB_PASSWORD=ProductionSecurePassword123!
ENCRYPTION_KEY=your-production-encryption-key
ENCRYPTION_SALT=your-production-salt
SESSION_SECRET_KEY=your-production-session-key
JWT_SECRET=your-production-jwt-secret

# URLs
ADMIN_API_URL=https://athena-admin.yourdomain.com
GATEWAY_URL=https://athena.yourdomain.com

# Infrastructure
ATHENA_DB_HOST=your-db-server.yourdomain.com
REDIS_HOST=your-redis-server.yourdomain.com
QDRANT_HOST=your-vector-db.yourdomain.com
OLLAMA_URL=http://your-gpu-server:11434

# Modules
MODULE_HOME_ASSISTANT=true
MODULE_GUEST_MODE=false
MODULE_NOTIFICATIONS=true
MODULE_JARVIS_WEB=true
MODULE_MONITORING=true

# API Keys
OPENWEATHER_API_KEY=your-key
BRAVE_API_KEY=your-key
NEWSAPI_KEY=your-key

# Environment
ENVIRONMENT=production
DEBUG=false
```

### Distributed Kubernetes

```bash
# Database (managed service)
ATHENA_DB_HOST=postgres.athena.svc.cluster.local
ATHENA_DB_PORT=5432

# Service Discovery
ADMIN_API_URL=http://athena-admin-backend.athena.svc.cluster.local:8080
ORCHESTRATOR_URL=http://athena-orchestrator.athena.svc.cluster.local:8001
GATEWAY_URL=http://athena-gateway.athena.svc.cluster.local:8000

# Infrastructure
REDIS_URL=redis://redis.athena.svc.cluster.local:6379/0
QDRANT_URL=http://qdrant.athena.svc.cluster.local:6333
OLLAMA_URL=http://ollama.gpu-workloads.svc.cluster.local:11434

# RAG Services
RAG_SERVICE_HOST=rag-services.athena.svc.cluster.local
```

---

## Validating Configuration

### Check Required Variables

```bash
# Script to validate required vars
for var in ATHENA_DB_PASSWORD ADMIN_API_URL ENCRYPTION_KEY ENCRYPTION_SALT SESSION_SECRET_KEY JWT_SECRET; do
  if [ -z "${!var}" ]; then
    echo "ERROR: $var is not set"
  else
    echo "OK: $var is set"
  fi
done
```

### Test Service Connectivity

```bash
# Test database
psql "postgresql://${ATHENA_DB_USER}:${ATHENA_DB_PASSWORD}@${ATHENA_DB_HOST}:${ATHENA_DB_PORT}/${ATHENA_DB_NAME}" -c "SELECT 1"

# Test Redis
redis-cli -h ${REDIS_HOST} -p ${REDIS_PORT} PING

# Test Ollama
curl ${OLLAMA_URL}/api/tags

# Test Admin Backend
curl ${ADMIN_API_URL}/api/health
```
