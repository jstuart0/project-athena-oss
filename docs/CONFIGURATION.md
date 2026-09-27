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

## Centralized Configuration via AthenaConfig

`AthenaConfig` (`src/shared/config.py`) is the canonical pydantic-settings `BaseSettings` object for Athena. It centralizes 11 high-leverage env vars migrated in Campaign 4 (ATHENA-7). The remaining ~220 env vars in the codebase continue to use direct `os.getenv` and are migrated per-PR — see `CONTRIBUTING.md` for the extension pattern.

### Reading config in code

```python
from shared.config import get_config

config = get_config()
config.ollama_url      # OLLAMA_URL
config.redis_url       # REDIS_URL
config.database_url    # DATABASE_URL
```

`get_config()` is an `lru_cache`-backed factory — `AthenaConfig` is instantiated exactly once per process. Tests reset it via `_clear_cache_for_tests()`.

### Centralized env vars

| Env var | AthenaConfig field | Default |
|---|---|---|
| `OLLAMA_URL` | `ollama_url` | `http://localhost:11434` |
| `LLM_SERVICE_URL` | `llm_service_url` | `""` |
| `REDIS_URL` | `redis_url` | `redis://redis:6379/0` (in-cluster DNS; local-dev: set to `redis://localhost:6379`) |
| `DATABASE_URL` | `database_url` | `""` |
| `SERVICE_API_KEY` | `service_api_key` | `""` |
| `DEFAULT_TIMEZONE` | `default_timezone` | `UTC` |
| `DEFAULT_CITY` | `default_city` | `""` |
| `OIDC_ISSUER` | `oidc_issuer` | `""` |
| `OIDC_CLIENT_ID` | `oidc_client_id` | `""` |
| `DEMO_MODE` | `demo_mode` | `false` |
| `DEV_MODE` | `dev_mode` | `false` |

There is also a `llm_endpoint` computed property that returns `LLM_SERVICE_URL or OLLAMA_URL` — the dominant precedence used by the orchestrator and gateway.

### admin_url — not env-loadable

`config.admin_url` is a `@computed_field` that delegates to `get_admin_url()` (Campaign 3, `src/shared/admin_url.py`). Setting an `ADMIN_URL` env var has no effect. See the `ADMIN_API_URL` resolution order in [Required Settings](#required-settings).

### Extending AthenaConfig

See `CONTRIBUTING.md` — Configuration Guidelines — for the step-by-step pattern to add a new field.

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
| `ADMIN_API_URL` | Admin backend URL (see resolution order below) | Set to your admin server |
| `ENCRYPTION_KEY` | API key encryption | `openssl rand -base64 32` |
| `ENCRYPTION_SALT` | Encryption salt | `openssl rand -base64 16` |
| `SESSION_SECRET_KEY` | Secret used for JWT signing when `JWT_SECRET` is unset; must not be the default in production | `openssl rand -base64 32` |
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

**`SESSION_SECRET_KEY`/`JWT_SECRET` must be plain random strings, never a copy-pasted key file.** If either value contains a PEM header or an SSH key-type substring, the JWT library mistakes it for asymmetric key material: both token minting and token validation fail with a `500`, instead of authenticating normally. The `openssl rand -base64 32` command above already produces a safe value — this only matters if a secret is set some other way (e.g. pasted from an existing `.pem`/`.pub` file).

**`ADMIN_API_URL` resolution order** (`src/shared/admin_url.py`):
1. `ADMIN_API_URL` — canonical; set this in all deployments
2. `ADMIN_BACKEND_URL` — accepted alias for backward compatibility
3. `ADMIN_INTERNAL_URL` — **DEPRECATED** alias; will be removed in a future release
4. `LOCAL_DEV=true` — resolves to `http://localhost:8080` (local-dev escape hatch when no env var is set)
5. K8s in-cluster (`KUBERNETES_SERVICE_HOST` set) — resolves to `http://athena-admin-backend:8080`
6. Empty string + warning log — callers will receive connection errors

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
| `REDIS_HOST` | `localhost` | Redis host (deprecated for RAG services — use `REDIS_URL` instead) |
| `REDIS_PORT` | `6379` | Redis port (deprecated for RAG services — kubelet auto-injects this name for K8s Services) |
| `REDIS_URL` | `redis://localhost:6379/0` | Full Redis URL used by all services. DB index encoded in path. |
| `COMMUNITY_EVENTS_REDIS_URL` | `redis://localhost:6379/1` | Redis URL for the community_events RAG service (uses DB 1 to isolate its event cache). Must encode the DB in the URL path. |

### Qdrant (Vector Database)

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_HOST` | `localhost` | Qdrant host |
| `QDRANT_PORT` | `6333` | Qdrant port |
| `QDRANT_URL` | `http://localhost:6333` | Full URL (overrides host/port) |

### SearXNG (Web Search)

| Variable | Default | Description |
|----------|---------|-------------|
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG instance URL used by `parallel_search.py`'s search provider |
| `SEARXNG_BASE_URL` | *(empty)* | SearXNG instance URL for the admin status check and the orchestrator's SearXNG provider registration. Empty means SearXNG is disabled (status reads "not configured", no network probe made). |

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

### Key source and precedence (ATHENA-88 / F91)

Every RAG API key below can be set two ways: as an env var (via the
`athena-api-keys` Secret in Kubernetes, or this file's `.env.example`
counterpart for local dev), or via the admin UI's **External API Keys**
page (a database-backed key store). Both are optional per key — a RAG
service starts fine with neither and simply errors on queries that need
the missing key.

- **The admin key store wins at startup.** If the store returns a key for
  a service, it overwrites the env var value. An env-only key (nothing in
  the store) is used as-is; the store returning nothing (404, non-2xx, or
  a connection error) falls back to the env var.
- **Changes in the admin UI aren't live** — the store is only consulted at
  service startup. Restart the affected `athena-rag-<service>` pod after
  changing a key there.
- `scripts/generate-rag-manifests.py`'s `SERVICES` table names exactly the
  env var each service's own code reads; `scripts/check-rag-key-env.py`
  enforces that in CI (`rag-generator-drift.yml`) so the manifest,
  `create-secrets.sh`, and the code can't drift apart again.

### Weather Services

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `OPENWEATHER_API_KEY` | 1,000/day | [openweathermap.org](https://openweathermap.org/api) |

### Search Services

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `BRAVE_API_KEY` | 2,000/month | [brave.com/search/api](https://brave.com/search/api/) — also used by Sitescraper |

### Entertainment

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `TMDB_API_KEY` | 1M/month | [themoviedb.org](https://www.themoviedb.org/settings/api) |
| `TICKETMASTER_API_KEY` | 5,000/day | [developer.ticketmaster.com](https://developer.ticketmaster.com/) |

### News & Information

News reads its key from the admin key store only — its own code has no
`os.getenv`/`os.environ` read for an env-based key. Configure it via the
admin UI's External API Keys page (store keys `api-newsapiai` / `api-webz`).

### Food & Dining

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `SPOONACULAR_API_KEY` | 150/day | [spoonacular.com](https://spoonacular.com/food-api) |
| `GOOGLE_PLACES_API_KEY` | See pricing | [developers.google.com/maps](https://developers.google.com/maps/documentation/places/web-service) — also used by Directions |

### Finance & Sports

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `ALPHA_VANTAGE_API_KEY` | 500/day | [alphavantage.co](https://www.alphavantage.co/support/#api-key) |
| `THESPORTSDB_API_KEY` | Yes | [thesportsdb.com](https://www.thesportsdb.com/api.php) |
| `GNEWS_API_KEY` | Optional, sports RAG only | [gnews.io](https://gnews.io/) |
| `API_FOOTBALL_KEY` | Optional, sports RAG only | [api-football.com](https://www.api-football.com/) |

### Travel

| Variable | Free Tier | Sign Up |
|----------|-----------|---------|
| `FLIGHTAWARE_API_KEY` | Paid only | [flightaware.com](https://www.flightaware.com/commercial/aeroapi/) — also used by Airports |

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
| `SESSION_SECRET_KEY` | Secret used for JWT signing when `JWT_SECRET` is unset; must not be the default in production |
| `JWT_SECRET` | Secret for JWT tokens |

### OpenAI-Compatible Conversation Sessions (ATHENA-88)

Distinct from the admin-backend session settings above — these bound the
orchestrator's per-conversation OpenAI-compatible sessions
(`/v1/chat/completions`, `/v1/responses`). Every conversation is keyed by an
HMAC fingerprint over room + user + the first user message (never system
content or later turns), so it stays stable across Home Assistant's
full-history replay on every turn. The HMAC secret is `SERVICE_API_KEY`
(see [Security Settings](#security-settings)); outside `DEV_MODE`, an empty
or placeholder `SERVICE_API_KEY` is fatal at orchestrator startup.

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_MAX_COUNT` | `5000` | Cap on concurrent per-conversation sessions (in-memory fallback dict + Redis creation-time index). The oldest session (by last activity / creation time) is evicted once exceeded. |
| `NEW_CONVERSATION_PER_MINUTE_PER_IP` | `120` | Gateway-side sliding-window limit on *new* conversations (first-turn requests with no explicit `session_id`) per rate-limit key (see `TRUSTED_PROXY_CIDRS`), applied to both `/v1/chat/completions` and `/v1/responses`. Raised from an earlier default of 30 — behind a reverse proxy every caller can share one resolved key, making a low per-source limit a whole-house limit. |
| `TRUSTED_PROXY_CIDRS` | `10.244.0.0/16` | Comma-separated CIDRs/hosts. The new-conversation limiter trusts `X-Forwarded-For`'s original-client address only when the immediate TCP peer (your reverse proxy) falls inside one of these ranges; an untrusted caller can't spoof another source's key via that header. The header is parsed right-to-left, returning the nearest hop not in this CIDR set (falling back to the TCP peer if every hop is trusted), so a trusted proxy that appends rather than overwrites `X-Forwarded-For` doesn't let an upstream caller forge the left-most value. Keep this list scoped to your actual reverse-proxy subnet, not a broad cluster-wide default. |
| `NEW_CONVERSATION_RESET_GRACE_SECONDS` | `120` | A first-turn fingerprint reset is skipped when a session under the same fingerprint was created within this many seconds — protects against Home Assistant's truncated-ASR retry path, which resends the same single-user-message opener for the same turn. |

The new-conversation limiter's counters are backed by Redis (`REDIS_URL`)
when a Redis connection succeeds at gateway startup, so multiple gateway
replicas share one budget per key; it falls back to an in-memory,
single-replica-only counter otherwise (logged at startup either way).

### Authentication (Optional)

| Variable | Description |
|----------|-------------|
| `AUTHENTIK_CLIENT_ID` | Authentik OAuth client ID |
| `AUTHENTIK_CLIENT_SECRET` | Authentik OAuth client secret |
| `AUTHENTIK_ISSUER_URL` | Authentik issuer URL |

### OIDC / SSO Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OIDC_ISSUER` | `""` | IdP issuer URL. In production: must be non-empty and not the `CONFIGURE_ME` placeholder. Must match the `iss` claim in issued ID tokens exactly. Startup exits if invalid. |
| `OIDC_CLIENT_ID` | `""` | Client ID registered with your IdP. Rejected values (all cause `SystemExit` at startup in production): `""`, `"demo-mode"`, `"CONFIGURE_ME_OIDC_CLIENT_ID"`. |
| `OIDC_CLIENT_SECRET` | `""` | Client secret from your IdP. |
| `OIDC_REDIRECT_URI` | `""` | Callback URL registered with your IdP. |
| `OIDC_SCOPES` | `openid profile email` | Requested OIDC scopes. |
| `OIDC_USERINFO_URL` | *(derived)* | Override for the OIDC userinfo endpoint. Auto-derived from discovery doc when unset. |

### DEV_MODE and DEMO_MODE

| Variable | Default | Description |
|----------|---------|-------------|
| `DEV_MODE` | `false` | Development mode: uses SQLite in-memory, skips OIDC gates, auto-creates a `dev-admin` owner account on unauthenticated requests. **Never set `true` in production.** If `DEV_MODE=true` and `DATABASE_URL` points to a non-SQLite database, the startup gate applies a three-arm check (xander:6): (1) K8s pod (`KUBERNETES_SERVICE_HOST` set, or `IN_CLUSTER=true`) → **FATAL** — ClusterIPs are RFC1918 but are never a safe local-dev environment; (2) local host (loopback, RFC1918, ULA) → **WARNING** and continue — valid for a developer running a local Postgres instance; (3) remote host → **FATAL** — pairing DEV_MODE with a remote DB grants owner-level access without credentials. `IN_CLUSTER=true` is treated identically to `KUBERNETES_SERVICE_HOST` for the K8s guard (arm 1). |
| `DEMO_MODE` | `false` | Demo mode: pre-seeded data and demo-user bypass in `auth_login`. Rejected at startup if `DEV_MODE=false` — running `DEMO_MODE=true` in production bypasses the authentication path via the demo-user branch (xander:16). To use demo mode, also set `DEV_MODE=true`. |
| `DATABASE_URL` | `""` | SQLAlchemy connection string. When `DEV_MODE=true`, this value is ignored by `database.py` (SQLite in-memory is used), but the startup gate checks it first and will exit if the value is a non-SQLite URL. Set to `sqlite:///:memory:` or leave empty for local dev with `DEV_MODE=true`. |

### FRONTEND_URL

`FRONTEND_URL` controls the redirect target after a successful OIDC callback and after a `DEMO_MODE` login. The backend appends `?logged_in=1` to this URL. Do not set `FRONTEND_URL` to a value that already contains a query string — a value like `http://example.com?foo=bar` would produce a malformed double-query-string redirect (`http://example.com?foo=bar?logged_in=1`).

### Admin-backend startup security gates (ATHENA-12)

The admin-backend enforces fail-closed checks in `startup_event()`. All of these raise `SystemExit` before the service accepts connections:

| Condition | Error |
|-----------|-------|
| `DEV_MODE=true` + non-SQLite `DATABASE_URL` in K8s pod (`KUBERNETES_SERVICE_HOST` or `IN_CLUSTER=true` set) | `FATAL: DEV_MODE=true is not permitted in a Kubernetes pod` |
| `DEV_MODE=true` + non-SQLite `DATABASE_URL` on a **remote** host | `FATAL: DEV_MODE=true is incompatible with a non-SQLite DATABASE_URL` |
| `DEV_MODE=true` + non-SQLite `DATABASE_URL` on a **local/private** host (loopback, RFC1918, ULA) | WARNING logged; startup continues |
| `DEMO_MODE=true` + `DEV_MODE=false` | `FATAL: DEMO_MODE=true requires DEV_MODE=true` |
| `OIDC_CLIENT_ID` is `""`, `"demo-mode"`, or `"CONFIGURE_ME_OIDC_CLIENT_ID"` in production | `insecure_default_secret_detected` |
| `OIDC_ISSUER` empty or `CONFIGURE_ME`-prefixed in production | `insecure_default_secret_detected` |
| IdP unreachable or `.well-known/openid-configuration` missing `issuer` | `FATAL: OIDC discovery metadata fetch failed` |
| DB-loaded runtime issuer empty or placeholder after `configure_oauth_client()` | `FATAL: OIDC runtime issuer is empty or placeholder` |

The `DEV_MODE=true` condition bypasses all OIDC gates; the other gates run in both dev and production (except where noted by the `in production` qualifier above, which means `DEV_MODE=false`).

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

### Region-Configurable RAG Services

The transportation and community_events services ship with no region baked
in. Unset means the service reports "not configured" via `/health` and its
data routes return 503. See `.env.example` for the full JSON schema and a
worked Denver example, including a feed with an optional `bounds` box.

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSIT_REGION_NAME` | *(empty)* | Display label only, purely cosmetic |
| `TRANSIT_GTFS_FEEDS` | *(empty)* | JSON object of GTFS feed definitions (`{feed_id: {name, agency, url, type, free, bounds?, max_bytes?, allow_private?}}`) |
| `TRANSIT_STATIC_SERVICES` | *(empty)* | JSON object of non-GTFS transit services with fixed schedules (e.g. a seasonal ferry) |
| `COMMUNITY_EVENTS_SOURCES` | *(empty)* | JSON array of community-event sources. Each entry's `type` selects the parser: `link_scan`, `event_cards`, `tribe_events_api`, `squarespace_eventlist`. `link_scan` and `event_cards` are best-effort heuristics, validated only against their reference site's HTML structure — a source's markup can drift without notice. |

---

## Home Assistant Entity Mappings

All four ship with NO house-specific entity IDs baked in. Unset means the
corresponding feature is disabled or falls back cleanly rather than
guessing a device/room your HA instance doesn't have. See `.env.example`
for the full JSON schema and worked examples.

| Variable | Default | Description |
|----------|---------|-------------|
| `HA_SATELLITE_ROOM_MAP` | *(empty)* | JSON object mapping a Voice PE `assist_satellite` entity ID to its room. Used both directions by the gateway: entity→room (conversation room detection) and room→entity (satellite announcements). |
| `HA_TV_ENTITIES` | *(empty)* | Fallback room → Apple TV entity mapping, used only when the admin API's Room TV Config is unreachable. JSON array of `{room, media_player_entity_id, remote_entity_id}` objects, or comma-separated `room:media_player_entity_id[:remote_entity_id]` triples. |
| `HA_MUSIC_PLAYERS` | *(empty)* | Fallback room → Music Assistant `media_player` entity mapping, used only when the admin API's room audio config is unreachable. JSON object `{room: entity_id}` or comma-separated `room:entity_id` pairs. |
| `HA_BED_WARMER_ENTITIES` | *(empty)* | JSON object naming the 5 HA entities a Sunbeam-via-Tuya dual-zone bed-warmer/mattress-pad integration exposes (`level_left`, `level_right`, `power_main`, `power_side_a`, `power_side_b`). |

---

## RAG Service URLs

Override these for distributed RAG service deployment:

| Variable | Default Port | Description |
|----------|-------------|-------------|
| `RAG_WEATHER_URL` | 8010 | Weather service |
| `RAG_ONECALL_URL` | 8021 | OneCall weather provider |
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
| `RAG_COMMUNITY_URL` | 8026 | Community events provider |
| `RAG_SERPAPI_URL` | 8032 | SerpAPI events provider |
| `RAG_SEATGEEK_URL` | 8024 | SeatGeek events provider |
| `RAG_TRANSPORTATION_URL` | 8025 | Transportation service |
| `RAG_AMTRAK_URL` | 8027 | Amtrak service |
| `RAG_SITESCRAPER_URL` | 8031 | Site scraper service |
| `RAG_PRICECOMPARE_URL` | 8033 | Price comparison service |
| `RAG_TESLA_URL` | 8028 | Tesla service |
| `RAG_MEDIA_URL` | 8029 | Media service |
| `RAG_BRIGHTDATA_URL` | 8040 | BrightData service |

`orchestrator/urls.py` is the single reader of these env vars. Each also
accepts a deprecated `<NAME>_RAG_URL` alias (e.g. `WEATHER_RAG_URL` for
`RAG_WEATHER_URL`) for backward compatibility; using it logs a WARNING, and
the canonical `RAG_<NAME>_URL` name wins if both are set. See
`.env.example` for the full deprecated-alias list.

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
# News is key-store only -- configure via the admin UI's External API
# Keys page; the service's own code reads no env var.

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
ADMIN_API_URL=http://athena-admin-backend:8080
ORCHESTRATOR_URL=http://athena-orchestrator.athena-prod.svc.cluster.local:8001
GATEWAY_URL=http://athena-gateway.athena-prod.svc.cluster.local:8000

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
