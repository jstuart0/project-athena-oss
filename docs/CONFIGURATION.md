# Project Athena Configuration Reference

Complete reference for all configuration options in Project Athena.

## Table of Contents

1. [Configuration Methods](#configuration-methods)
2. [Required Settings](#required-settings)
3. [Database Configuration](#database-configuration)
4. [Service URLs](#service-urls)
5. [Service Control on Kubernetes](#service-control-on-kubernetes-athena-118)
6. [Infrastructure Services](#infrastructure-services)
7. [Module Settings](#module-settings)
8. [API Keys](#api-keys)
9. [Voice Services](#voice-services)
10. [Security Settings](#security-settings)
11. [Advanced Settings](#advanced-settings)
12. [Telemetry](#telemetry)

---

## Centralized Configuration via AthenaConfig

`AthenaConfig` (`src/shared/config.py`) is the canonical pydantic-settings `BaseSettings` object for Athena. It centralizes 63 env vars, starting with 11 high-leverage vars migrated in Campaign 4 (ATHENA-7) and extended by later campaigns (ATHENA-1, ATHENA-11, ATHENA-12, ATHENA-14, ATHENA-59, ATHENA-88, ATHENA-89). The remaining env vars in the codebase continue to use direct `os.getenv` and are migrated per-PR — see `CONTRIBUTING.md` for the extension pattern.

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

The vars below are the original Campaign 4 batch, kept for illustration. See
`src/shared/config.py` for the complete, current field list (42 fields as of
ATHENA-118) — new fields land there per-PR and this table is not re-synced on
every addition.

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
| `ATHENA_TELEMETRY`, `DO_NOT_TRACK`, `ATHENA_TELEMETRY_ENDPOINT`, `ATHENA_TELEMETRY_MODE` | not fields: read per call by `read_telemetry_env()` from the process env and `.env` | see [Telemetry](#telemetry) |

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
| `MODE_SERVICE_URL` | `http://localhost:8022` | Mode service (ATHENA-69). **Required** on both the orchestrator and the gateway — without it, mode/permission resolution degrades every request (orchestrator: `get_current_mode`'s outage fallback; gateway: `mode_gate.py`'s fast-path check always returns `False`). Also read by admin-backend for the admin modules page and the Guest Mode page's `GET /api/guest-mode/mode-status` proxy — unset there just means that proxy reports `mode_service_url_unset` rather than a live mode. See "Mode and permissions" under Module Settings below. |
| `NOTIFICATIONS_SERVICE_URL` | `http://localhost:8050` | Notifications service |
| `JARVIS_WEB_URL` | *(empty)* | jarvis-web API base for appliance/sensor/media lookups in the orchestrator's smart-home controller; empty skips them. |
| `CONTROL_AGENT_URL` | `http://localhost:8099` | Service management API |
| `CONTROL_AGENT_CALLBACK_BASE_URL` | *(empty)* | Where the Control Agent's host reaches admin-backend, for model-download progress callbacks. See "Admin API authentication". |
| `CONTROL_AGENT_SERVICES_FILE` | *(empty)* | Path to a JSON file (read by the Control Agent process itself, not admin-backend) naming which bare processes, watchdog exclusions, and Docker containers this Control Agent may manage. Empty means it manages nothing. See below. |

**Control Agent managed-services file (ATHENA-99, D46)**: `CONTROL_AGENT_SERVICES_FILE` points at a JSON file with three independent, all-optional, all-default-empty top-level keys:

```json
{
  "processes": {
    "8000": {
      "name": "example-gateway",
      "dir": "src/gateway",
      "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
      "health_path": "/health",
      "enabled": true
    }
  },
  "watchdog_exclude": [8028],
  "containers": ["athena-example-container"]
}
```

- `processes` -- keyed by port (as a JSON string). Each entry needs `name`, `dir` (working directory, relative to the Control Agent's checkout), and `cmd` (argv list). `health_path`, `enabled`, and `service_type` are optional; `enabled: false` drops the entry (it's parsed but never managed). This is exactly the shape the Control Agent's watchdog and startup registry-sync (`POST /api/service-registry/services`) already used, just no longer hard-coded into the module.
  - `service_type` (ATHENA-118, codex diff review r2 Medium #4): one of `rag`, `core`, `infrastructure`. Authoritative when set -- registry-sync forwards it as-is. Falls back to the `-rag`-in-name heuristic when omitted. **Declare this for a RAG service whose name doesn't contain `-rag`** (e.g. `weather`, `sports`) -- otherwise it syncs as `service_type='core'`, which admin-backend's `group_for()` then reads as a non-`rag` group, and Service Control's fail-safe criticality treats every non-`rag` row as critical (owner-only `manage_infrastructure` gate) just like a real infrastructure service.
- `watchdog_exclude` -- ports the 60-second watchdog should never auto-restart, even if they're in `processes`.
- `containers` -- the Docker container-name whitelist for the `/docker/*` control endpoints (`is_container_allowed`). Independent of `processes`: a deployment can leave `processes` empty and still manage Docker containers (or vice versa).

Unset (the default): `processes`/`watchdog_exclude`/`containers` are all empty, so the watchdog loop and the startup registry sync are both no-ops (the latter logs one INFO line, `no_managed_services_configured`), and every `is_port_allowed`/`is_container_allowed` check returns false. A malformed file (bad JSON, wrong top-level shape, or an individual bad entry) logs one ERROR per problem and falls back to managing nothing for that section -- it never crashes the Control Agent process. See `src/control_agent/services.example.json` for a complete example.

Each `processes` entry is additionally hardened at load time (ATHENA-110): `cmd` is rejected if any element contains a shell metacharacter (`; & | $ \` < > \n`) -- the launch path never uses a shell, so this is defence in depth against a future regression. `dir` must be a relative path with no `..` component and must resolve under the Control Agent's own checkout (`PROJECT_ROOT`) -- `dir: "/etc"` or `dir: "../../etc"` are rejected. Either failure logs one ERROR and drops that entry, same as any other malformed entry. Separately, the services file itself is checked for group/world-writable permissions at load time; a writable file logs one WARNING (it still loads -- this is a warn, not a refuse) since it names commands and containers this Control Agent may execute/control.

**Control Agent inbound authentication (ATHENA-110)**: every *mutating* Control Agent route -- `/process/start|stop|restart/{port}`, `/docker/start|stop|restart/{container_name}`, `/ollama/start|stop|restart`, `/huggingface/download` (POST), `/huggingface/download/{job_id}` (DELETE), `/huggingface/import-to-ollama`, `/huggingface/downloaded` (DELETE), and `/watchdog/enable|disable|exclude/{port}|include/{port}` -- requires an `X-Service-Key` header matching the Control Agent's own `SERVICE_API_KEY` env var (`src/control_agent/auth.py::require_service_caller`). A missing or wrong key returns 401; an unset `SERVICE_API_KEY` returns 503 on every mutating route (fail-closed -- the Control Agent has no `DEV_MODE` concept and never falls open). Read-only routes (`/health`, `/docker/list`, `/docker/status/*`, `/ollama/status`, `/ollama/health`, `/process/list`, `/process/status/{port}`, the `/huggingface/search|repo/*/files|downloaded` (GET) reads, `/debug-logs/*`, `/watchdog/status`) are not gated. admin-backend authenticates to the Control Agent via `app.utils.service_auth.control_agent_headers()`, which attaches `X-Service-Key: <SERVICE_API_KEY>` to every outbound Control Agent client (`service_control.py`'s docker/process/ollama calls, `model_downloads.py`'s `call_control_agent` helper); the orchestrator's own gateway-keepalive caller (`ensure_gateway_running` in `src/orchestrator/main.py`) does the same, reusing its existing `SERVICE_API_KEY`.

### Service Discovery Helpers

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_HOST` | `localhost` | Default host for all services |
| `RAG_SERVICE_HOST` | `localhost` | Default host for RAG services |

### Service Registry Health Checks (ATHENA-109 / ATHENA-112)

Every row in `athena_service_registry` carries a `protocol` column that doubles as its **check type** -- there is no separate `check_type` field, since "how do we check this service" and "what scheme does it speak" are the same question. Valid values: `http` (default), `https`, and `tcp`.

- `http` / `https`: the background poller (`admin/backend/app/services/health_poller.py`) issues a `GET` against `health_endpoint` (default `/health`) every `HEALTH_POLL_INTERVAL_SECONDS`. A 200 with a JSON body is `healthy` unless it carries `"configured": false`, which maps to `unconfigured` (an amber "needs setup" state, distinct from a real failure). Non-200, timeout, and connection-refused map to `unhealthy` with a categorical `last_error` (`http_5xx`, `http_4xx`, `timeout`, `connection_refused`).
- `tcp`: a raw TCP connect to `host:port` within `HEALTH_POLL_TIMEOUT_SECONDS`. For a row whose `name` contains `redis` (same substring match `admin/frontend/app.js` already uses to group it under "Database Services"), the poller additionally sends a Redis `PING\r\n` after connecting and requires a `+PONG` **or** `-NOAUTH` reply within the same timeout budget -- proves the process on the far end actually speaks Redis, not just that something accepted the socket. Every other `tcp` row is a plain connect with no banner read. A successful connect (or, for redis rows, a `+PONG`/`-NOAUTH` reply) is `healthy`; a refused connection is `unhealthy` / `last_error=tcp_refused`; a connect that doesn't resolve within the timeout is `unhealthy` / `last_error=tcp_timeout`; a redis row that connects but replies with anything else (`-ERR`, garbage, empty) is `unhealthy` / `last_error=tcp_bad_banner`. Use `tcp` for services that don't speak HTTP or where an HTTP health endpoint doesn't exist.

  **Why `-NOAUTH` counts as healthy**: a password-protected Redis (`requirepass` set) replies `-NOAUTH Authentication required.` to an unauthenticated `PING` -- that reply is itself proof the target is reachable and speaking the Redis wire protocol. The health check's job is "is this Redis up", not "can the poller authenticate to it"; requiring auth here would mean shipping the Redis password into the health-poller's own config, a secret-handling expansion this check doesn't need.

Both check types share the same SSRF allowlist: `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` (comma-separated CIDRs or hostnames) must include a target before the poller will connect to an RFC1918/loopback/ULA address; otherwise the row reads `unhealthy` / `last_error=ssrf_blocked`. This is a **runtime polling** allowlist, separate from the write-boundary `validate_host()` check applied when a row is created/updated via `POST /api/service-registry/services` (which always rejects loopback addresses outright, for both `http(s)` and `tcp` rows, and allows RFC1918 with a warning log).

A Kubernetes in-cluster `Service` DNS name (e.g. a bare `redis` resolving to a `ClusterIP` in the cluster's service CIDR, commonly `10.96.0.0/12`) is not a `.cluster.local`/`.svc` control-plane hostname and is not blocked outright -- it still needs its resolved address (or the literal hostname `redis` itself) present in `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`, same as any other RFC1918 target. **Every OSS deployer running a `tcp` (or `http`) registry row against an in-cluster Service must add that Service's CIDR or hostname to the allowlist** -- there is no built-in exception for "well-known" service names like `redis`.

Registering a `tcp` row via the admin UI's row editor (or directly via `POST /api/service-registry/services?protocol=tcp&host=...&port=...`) takes `host`/`port` directly instead of `endpoint_url` -- a TCP check has no scheme or path to parse a URL out of, and `endpoint_url` is left `null` for these rows.

**RAG self-registration payload (`shared/service_registry.py::register_service`, ATHENA-108)**: every RAG process pings this same upsert route at startup to confirm it's registered. By default the ping omits `endpoint_url` entirely -- `POST /services` is partial-update-safe for an *existing* row (same semantics `service_type`/`cache_ttl`/`enabled` already have), so a seeded row's `host`/`port`/`protocol`/`endpoint_url` (the correct in-cluster K8s Service DNS name, e.g. `athena-rag-weather`) survive the ping untouched. `endpoint_url` remains required to *create* a brand-new, never-seeded row. Set `SERVICE_REGISTRY_ENDPOINT_URL` only for a deployment shape where the self-reported `http://localhost:<port>` view genuinely is the correct address (e.g. an ad-hoc bare-metal dev RAG service that isn't in `OSS_SERVICE_REGISTRY`) -- setting it against a seeded service overwrites that row's real host on every restart.

**Registry name/host-label normalisation (`shared/service_registry.py::to_rag_registry_name` / `to_rag_host_label`)**: the `name` and `host_label` a RAG connector sends on self-registration are derived from its `SERVICE_NAME`, lower-cased with `-`/`_` stripped, then suffixed `-rag` (name) or prefixed `athena-rag-` (host label) -- so `weather` becomes `weather-rag` / `athena-rag-weather`, and a hyphenated connector name like `price-compare` normalises to `pricecompare-rag` / `athena-rag-pricecompare`, matching its seeded row instead of 422ing. `SERVICE_REGISTRY_NAME` overrides the derived base for a seeded row that doesn't follow this convention. The derived name/host label are validated against the same character-set and length rules the admin route's own validation enforces before anything is sent, and a CI static check fails the build if any RAG connector's registration call site would normalise to a name that collides with another seeded row or another connector's base.

**Dashboard aggregation (ATHENA-112)**: `GET /api/service-registry/services` computes `healthy_services` and `overall_health` over **enabled** rows only. A disabled row is reported with `health_status: "disabled"` regardless of its last cached poller value (which goes stale the moment it's disabled) and is excluded from both the counts and the health rollup. `enabled_services` / `disabled_services` are new response fields; `total_services` still counts every row for backward compatibility.

**Mission Control voice-health card and RAG test probes (ATHENA-113b)**: the admin-backend's dashboard (`admin/backend/app/routes/dashboard.py`) and voice-test routes (`admin/backend/app/routes/voice_tests.py`) resolve each RAG service's URL independently via `app.utils.rag_urls.resolve_rag_url`, instead of assuming every RAG shares one host behind `RAG_HOST`/`RAG_SERVICE_HOST` -- in Kubernetes each RAG is its own Service, so a single shared host is an OSS-First violation and (when unset) reported every RAG as "unreachable" rather than "not configured". Resolution order per service:

1. **Service registry** -- a row in `athena_service_registry` for that service name (host/port/protocol, or `endpoint_url` directly), configured via the Admin UI's service registry.
2. **Canonical env var** -- `RAG_<NAME>_URL` (same spelling as `src/orchestrator/urls.py`, e.g. `RAG_WEATHER_URL`, `RAG_SPORTS_URL`, `RAG_DINING_URL`).
3. **Legacy single-host fallback** -- `RAG_HOST` or `RAG_SERVICE_HOST` plus the service's well-known port. Logs one WARNING per service the first time this fallback is used.
4. **Not configured** -- no source resolves. The dashboard's voice-health card shows `not_configured` (amber) for that service instead of probing a broken URL and reporting `unreachable`.

---

## Service Control on Kubernetes (ATHENA-118)

`GET /api/service-control` resolves, per registry row, which control plane
can actually start/stop/restart it: the Control Agent (host-gated — only
when the row's host equals the Control Agent's own host), Kubernetes
(opt-in, this section), or `none`. This section covers the Kubernetes half.

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_CONTROL_K8S_ENABLED` | `false` | Opt-in flag (`AthenaConfig.service_control_k8s_enabled`). Inert on its own — also requires `optional/admin-backend-rbac.yaml` and its automount patch file (steps 1 and 3 below). |

A registry row only resolves to Kubernetes when its `host` reduces to a
Deployment name: a bare RFC1123 label matching the Deployment's name
exactly (**the Service name must equal the Deployment name** — this repo's
manifests always pair a Service and Deployment under the same name, so
this holds by construction, but a hand-edited manifest that names them
differently will never resolve), or `<label>.<namespace>[.svc[.cluster.local]]`
with the suffix stripped.

### Opt-in steps

1. Apply the RBAC manifest (namespaced Role, not applied by a plain
   `kubectl apply -f manifests/athena-prod/` since it lives under `optional/`):
   ```
   kubectl apply -f manifests/athena-prod/optional/admin-backend-rbac.yaml
   ```
2. Set the flag (`AthenaConfig.service_control_k8s_enabled`, default `false`):
   ```
   kubectl -n athena-prod patch cm athena-config --type merge \
     -p '{"data":{"SERVICE_CONTROL_K8S_ENABLED":"true"}}'
   ```
3. Mount a token for the `athena-admin-backend` ServiceAccount (the tracked
   `admin-backend.yaml` deliberately keeps `automountServiceAccountToken:
   false` — see below):
   ```
   kubectl -n athena-prod patch deploy athena-admin-backend --patch-file \
     manifests/athena-prod/optional/admin-backend-k8s-control.patch.yaml
   ```

Without step 1 and step 3, the flag alone is inert: `GET /api/service-control`
reports `kubernetes.available: false` with a `reason`, and the page shows a
visible amber banner rather than silently doing nothing:

| Banner `reason` | Meaning | Fix |
|---|---|---|
| `not_in_cluster` | admin-backend isn't running inside a Kubernetes pod at all (`KUBERNETES_SERVICE_HOST` unset) — e.g. local dev, or a non-K8s deployment with the flag set by mistake. | Only meaningful inside the cluster; unset the flag outside it. |
| `no_service_account_token` | In-cluster, but the pod has no mounted SA token (`automountServiceAccountToken: false`, the tracked default). | Apply step 3's patch file. |
| `forbidden` | The API server rejected a request — the Role (step 1) isn't applied, doesn't cover this action, or the RoleBinding doesn't target the running SA. | Re-apply `admin-backend-rbac.yaml`; check `kubectl auth can-i` for the SA. |

### The exact Role, and why there's no `deployments` patch

`optional/admin-backend-rbac.yaml`'s Role has exactly two rules:
`apps/deployments` `list` (namespace-wide — `resourceNames` cannot
restrict `list`, and a Deployment spec carries no Secret values, only
`secretKeyRef` names; no `get` verb here — the app only ever lists the
collection, never reads a single Deployment by name outside the `/scale`
subresource below), and `apps/deployments/scale` `get`/`patch`, scoped
via `resourceNames` to every Deployment in `manifests/athena-prod/*.yaml`
minus `athena-admin-backend`/`athena-admin-frontend` (currently 30 names).
There is **no** `patch`/`update` on the base `deployments` resource
anywhere — that verb would let a compromised admin-backend rewrite a pod
template (`command`, secret mounts, `serviceAccountName`): real code
execution and secret exposure. Without it, the worst this Role permits is
setting the replica count of the 30 named Deployments — scale-to-0 (DoS) or
scale-to-large-N (resource pressure; there's no `ResourceQuota` in
`athena-prod`). `admin/backend/tests/test_admin_backend_rbac_manifest.py` (T10)
computes the expected `resourceNames` set at **test time** by parsing the
manifests, so it fails loudly the moment a new RAG service is added without
updating this file.

Adding a Deployment: re-run the generator (parse `manifests/athena-prod/*.yaml`
— non-recursive, so `optional/` is excluded by construction — for
`kind: Deployment` docs in `namespace: athena-prod`, minus the two
protected names) and update the Role's `resourceNames` list.

### Restart means downtime

There is no rolling restart (it needs `deployments` PATCH, deliberately not
granted). Restart is: scale to 0 → wait up to 60s for `status.replicas` to
reach 0 → renew the cross-replica lease (see below) → scale back to the
remembered count, inside a `finally` guarded by `asyncio.shield`, so no
exception, timeout, or task cancellation can leave a Deployment at 0 while
the admin-backend process is alive.

Three distinct ways a restart can fail to complete its scale-back, each
handled differently:

- **The scale-back PATCH itself fails** (a transient API error): reported
  as a failure, and an explicit marker (`system_settings` key
  `service_control.interrupted.<deployment>`) is persisted so the row
  reads `restart_interrupted` even though the lease was still released
  normally (see below — an expired-lease check alone would miss this).
- **Another admin-backend replica takes over the lease mid-wait**: the
  lease renewal (below) fails atomically, the scale-back is deliberately
  **skipped** (not raced against whatever the new holder is doing), and
  the action is audited as `restart_superseded` rather than a plain
  failure.
- **The admin-backend process itself dies mid-wait** (its own rollout,
  OOM, node drain): the Deployment stays at 0 until an operator presses
  Start.

In every case above, the row shows `manager_note: "restart_interrupted"`
— derived from the explicit marker **or** an expired restart lease with
the Deployment still observed at 0 replicas (either signal is
sufficient) — and Start recalls the remembered count and clears the
marker. **Any** successful k8s action on the deployment (start, stop, or
a settled restart) also clears the marker. If an out-of-band `kubectl
scale` brings the Deployment back up while the marker is still set, the
badge is dropped and the stale marker is cleared lazily on the next
envelope read — the observed replica count is authoritative over a
possibly-stale marker. There's no automatic recovery beyond this, by
design.

**Don't roll (`kubectl rollout restart`) the admin-backend Deployment
while a restart lease is unexpired (within the last 90s of a k8s
restart/stop/start action).** A rolling admin-backend pod replacement
mid-action orphans the in-flight lease exactly like a crash would — the
new pod has no memory of the action it interrupted, and the target
Deployment relies on `restart_interrupted` recovery (press Start) rather
than resuming automatically.

The pre-stop replica count lives in `system_settings`
(`service_control.replicas.<deployment>`, clamped 1-10 on read, a stored `0`
is impossible by construction — `stop`/`restart` only remember when the
current count is `> 0`).

**Out-of-band scaling during a restart is overwritten**: if something else
(`kubectl scale`, an HPA, a second operator) changes the replica count while
a restart is in flight, the scale-back step still writes the count that was
remembered *before* the restart started — the out-of-band change is
silently lost. This is a known limitation of "remember, then restore"
semantics; don't run a restart through this UI while also manually scaling
the same Deployment.

### The cross-replica lease

When `admin-backend` runs with `replicas: 2`, a k8s start/stop/restart holds
a lease in `system_settings` (`service_control.lock.<deployment>`, category
`service_control`, 90s TTL) for the **whole** action, including restart's
wait — not just the instant of the PATCH. A second replica's action on the
same Deployment while the lease is held gets 409 `action_in_progress`
(audited). An expired lease is taken over via a compare-and-swap `UPDATE`
keyed on the exact previously-observed value, so two replicas racing a
takeover can't both win. The lease is released, by exact value match, only
by the holder that acquired it — a stale holder's release can never delete
a newer holder's lease.

**Renewal before the scale-back PATCH**: a plain read-then-act check right
before restart's scale-back ("do I still hold the lease?") has a TOCTOU
gap — a replica could observe itself as owner right at the TTL edge,
another replica could take over and act on the Deployment in the window
between that read and the PATCH, and the first replica would scale back
regardless. Instead, immediately before the scale-back PATCH, the holder
atomically **renews** the lease (the same compare-and-swap `UPDATE...WHERE
value=<mine>` primitive the takeover itself uses, extending `expires_at`
on success). Renewal failing means someone else already has it — the
scale-back is skipped and the action reads `restart_superseded`, never
racing the new holder.

### Guardrails

- **Protected** (`athena-admin-backend`, `athena-admin-frontend`): never
  targetable, in app code and in RBAC.
- **Critical** (the named set `athena-gateway`, `athena-orchestrator`,
  `athena-mode-service`, `athena-jarvis-web`, `redis`, `qdrant`, `ollama`,
  union any Kubernetes-resolved row whose group isn't `rag`, union any
  **Control-Agent**-resolved row whose group isn't `rag` (the same
  fail-safe, applied identically to both managers — codex diff review r1
  Critical #1), union Ollama under any manager): requires the owner-only
  `manage_infrastructure` permission **and** a typed confirmation
  (`confirm_name` in the request body, and in the envelope's own
  `confirm_name` field — never derived client-side from `manager_target`,
  which for a Control-Agent process is just the bare port number) that
  must equal the **resolved target's** name — the Deployment label for
  Kubernetes, the container name or `process:<port>` for Control Agent,
  `ollama` for Ollama — never an alias row's own name. The owner gate is
  checked before action-availability, so a non-owner always gets 403
  `insufficient_role`, never a 409 about an unavailable action.
- **Target collisions**: if two registry rows (enabled or disabled) resolve
  to the same Kubernetes target, both are blocked (`manager_note:
  "target_collision:<name>"`) — an operator can't alias an obscure row onto
  a critical Deployment to bypass the owner gate.

### Upgrade / reapply warning

`kubectl apply -f manifests/athena-prod/admin-backend.yaml` (or of the whole
directory) resets `serviceAccountName`/`automountServiceAccountToken` back to
the tracked base values (no SA token), silently undoing the automount patch.
**Re-run the patch file (step 3 above) after any such apply**, or Kubernetes
control goes dark (visible amber banner, not a crash) until you do.

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

**Ollama URL write validation and runtime probes (ATHENA-118)**: `POST
/api/settings/ollama-url` rejects a scheme other than `http`/`https`, a URL
over 2048 characters, and a host that's IMDS/link-local/multicast/
unspecified/`.svc`/`.cluster.local` — loopback is allowed only when the
admin-backend process is itself running outside a Kubernetes pod. This
write-boundary check runs **before** the save attempts a reachability
probe, so a rejected URL never leaks a request to the attacker-controlled
host first. Every runtime probe of the configured Ollama URL — this route's
own reachability check, and every Ollama call `service_control.py` makes
(`/api/version`, `/api/tags`, `/api/ps`, model load/unload) — additionally
honors the same runtime SSRF allowlist as the health poller:
`HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` must include the Ollama host before an
RFC1918/loopback/ULA address is actually reached (otherwise the request is
refused and the UI reports `ssrf_blocked`). **An in-cluster Ollama needs its
Service's CIDR or hostname added to `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`**,
the same as any other in-cluster Service (see "Service Registry Health
Checks" above) — there is no separate allowlist for Ollama specifically.

**Local-dev carve-out for model discovery and voice tests (ATHENA-122 / xander diff-review Medium, 2026-09-28)**: `GET /api/component-models/available-models`, `validate_model_exists` (model-assignment validation), and `voice_tests.py`'s `/llm/test` and `/pipeline/test` probes call `app.utils.rag_urls.check_ollama_ssrf_safe()` instead of the bare `check_ssrf_safe()` — it applies the SAME not-in-cluster loopback/RFC1918/ULA carve-out the write-boundary check above already has (`app.utils.url_validators.is_local_host()`), so the OSS default `http://localhost:11434` works for these four probes out of the box on a bare-metal dev machine with **no** `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` entry needed. This carve-out does **not** apply inside a Kubernetes pod (`is_local_host()` always returns `False` there) or to link-local/IMDS-class addresses — an in-cluster `http://ollama:11434` still needs the allowlist entry exactly as described above. `service_control.py`'s Ollama probes and this route's own reachability check are unchanged by this carve-out (pre-existing behavior, out of this fix's scope) — a bare-metal `localhost` Ollama still needs the allowlist for the Service Control panel and the settings reachability check specifically.

**Startup discoverability**: admin-backend logs one WARNING at boot,
`ollama_url_blocked_by_ssrf_guard`, naming the exact env var to set,
whenever the currently configured Ollama URL would genuinely be blocked
(i.e. not covered by the carve-out above) — most commonly an in-cluster
Service DNS name with no matching `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`
entry yet. This is a diagnostic log line only; it never blocks startup.

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
| `FASTEMBED_CACHE_PATH` | `/opt/fastembed_cache` in the image | fastembed's own model cache directory. The admin-backend image bakes the embedding model here at build time. |
| `EMBEDDING_MODEL_REVISION` | set in the image | The Hugging Face commit of the embedding model the image baked (`Qdrant/all-MiniLM-L6-v2-onnx`). It's a build-time pin, not a runtime setting: to change it, edit it in `admin/backend/Dockerfile` together with `admin/backend/embedding-model.sha256` and rebuild. |
| `HF_HUB_OFFLINE` | `1` in the image | Hugging Face's own offline switch. The embedder also always passes `local_files_only=True`, so it never downloads a model at runtime. |

#### Memory vectors

PostgreSQL is the source of truth for memories; the Qdrant collection
`athena_memories` holds one vector per memory and can always be rebuilt
from Postgres. All of admin-backend's Qdrant and embedding access goes
through `admin/backend/app/services/memory_vectors.py`.

- **The collection manages itself.** admin-backend creates `athena_memories`
  (384 dimensions, Cosine) when it's missing and records the embedding
  model (`sentence-transformers/all-MiniLM-L6-v2`) in the collection's
  metadata. Qdrant 1.16 and later keep that metadata; older servers
  (verified on 1.12.1) accept and drop it, so every point also carries an
  `embedding_model` stamp, and points stamped with another model count as
  a model mismatch on every version. A collection with the wrong shape or a
  different recorded model is never used and never modified automatically.
  When admin-backend has to create the collection, it first marks every
  memory that claimed a stored vector as pending, so a collection lost at
  runtime (a storage change, a wiped volume) is rebuilt automatically.
- **Every memory is saved first.** A memory is written to Postgres as
  `vector_status: pending`, then embedded; only a confirmed vector write
  marks it `stored`, and only stored memories are served by semantic
  search. Editing a memory's text, summary, category or importance marks it
  pending until its vector is rewritten. When the vector store is down, the
  memory is still saved (and logged as `memory_vector_store_failed`).
- **Automatic rebuild.** A background task re-checks the store (every 30 s
  while it's unavailable, every 5 minutes while it's healthy) and embeds up
  to 500 pending memories on startup, whenever the store recovers, and every
  10 minutes while any remain. It never deletes vectors.
- **Keyword fallback.** When semantic search can't run, the orchestrator's
  memory recall (`GET /api/memories/internal/search`) falls back to keyword
  matching: `qdrant_available` means "these results are usable",
  `semantic_available` says whether semantic search contributed, and
  `search_type` is `keyword_fallback`. This holds whether or not the
  `hybrid_memory_search` feature is enabled. Keyword matching keeps numbers
  and codes whole (`4417`, `b7x9`), so a door code or Wi-Fi password is
  still found during an outage.
- **Status.** The Memories page shows `GET /api/memories/qdrant/health`:
  `pg_live_count`, `points_count`, `pending_count`, the store state
  (`ready`, `unavailable`, `embedder_unavailable`, `shape_mismatch`,
  `model_mismatch`), the expected and recorded embedding model, and up to
  10 ids of vectors from another model. Postgres and the collection are
  compared by id: `missing_count` (stored memories without a vector),
  `orphan_count` (vectors without a live memory), each with up to 10 sample
  ids. `in_sync` is `true` only when nothing is missing, orphaned or
  pending; it's `null` with `sync_scan: "partial"` when there are too many
  memories to compare in one pass (more than 10,240), never a guessed
  `true`. `status` is `healthy`, `degraded` (reachable but not in sync),
  `unavailable`, or `error` (a mismatch). URLs in the payload have
  credentials removed.
- **Manual rebuild.** `POST /api/memories/vector-store/reindex?mode=missing|all&dry_run=`:
  an owner (the `manage_infrastructure` permission) may run either mode and
  also removes orphan vectors; the `X-Service-Key` caller may run
  `mode=missing` only and never removes anything. One rebuild runs at a
  time across replicas, with a 60 s cooldown after a real run
  (`409 reindex_busy` carries `retry_after_seconds`). Dry runs don't take
  the rebuild lease at all: they have their own lease and 60 s cooldown,
  so repeated dry runs can't hold up a real rebuild or the automatic pass. The Memories page's "Rebuild vectors" button runs
  `mode=missing` for an owner and appears whenever the page isn't in sync. The CLI,
  `python -m app.services.memory_vectors reindex [--mode missing|all] [--dry-run] [--batch-size N]`,
  also prunes orphans and is the only way to drop and re-create the
  collection (`--recreate --confirm-collection athena_memories`). Run the
  CLI as a one-off Pod with its own memory limit, never as an exec into a
  serving admin-backend pod: it loads a second copy of the model.
- **Which Qdrant URL.** The Memories page and the memory store use
  admin-backend's `QDRANT_URL`. The header status bar's Qdrant indicator
  reads the service registry entry instead; point both at the same server.
- **Sizing and limits.** The embedder runs one embed at a time per process,
  on text truncated to 4000 characters (the model itself stops at 256
  tokens), in batches of 16, single-threaded; the image's peak memory is
  gated at 700 MiB in CI. admin-backend requests 512Mi and is limited to
  1Gi. Memory text and search queries are capped at 8192 characters and
  summaries at 255.
- **Development.** Outside the image, fetch the model once into a cache
  directory at the image's `EMBEDDING_MODEL_REVISION` and point
  `FASTEMBED_CACHE_PATH` at it. fastembed's own download takes the latest
  upstream commit and ignores a revision, so follow the Dockerfile's bake
  step: `huggingface_hub.snapshot_download` with `revision=`, then write the
  revision to `models--qdrant--all-MiniLM-L6-v2-onnx/refs/main` in the cache
  so the offline load finds it.

### SearXNG (Web Search)

| Variable | Default | Description |
|----------|---------|-------------|
| `SEARXNG_BASE_URL` | *(empty)* | SearXNG instance URL for the admin status check and the orchestrator's SearXNG provider registration (`parallel_search.py`). Empty means SearXNG is disabled (status reads "not configured", no network probe made). |
| `SEARXNG_URL` | *(none — deprecated)* | Legacy fallback for `SEARXNG_BASE_URL`. If `SEARXNG_BASE_URL` is empty and this is set, `parallel_search.py` uses it and logs a one-time WARNING (`searxng_url_legacy_env_name`). Prefer `SEARXNG_BASE_URL`. |

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

### Mode and permissions (ATHENA-69)

Every Home Assistant write the orchestrator makes is authorized against the
**request's server-derived mode and permissions** before it's sent — never
against a client-supplied `mode` field, and never against whatever the
orchestrator most recently saw for a different caller.

**How mode is determined.** `resolve_request_authorization()` is the single
resolution point, called at every entry point (`/query`, `/query/stream`,
`/query/stream/v2`, `/v1/chat/completions`). A caller's own `mode` field can
only *narrow* an owner-mode house down to guest for that one request — it
can never claim owner. The precedence, most to least trusted:

1. The mode service reports the house is in **owner** mode (no guest
   booking active, or an active voice-PIN override) → the caller gets
   owner unless it explicitly asked to narrow to guest.
2. The mode service reports **guest** mode (a booking is active) → the
   caller gets guest, plus any device-fingerprint-matched guest
   permissions the admin backend has on file.
3. The mode service is **unreachable or erroring** → **degraded** (D4):
   never owner. Degraded permissions are the fallback list alone
   (`HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES`, default: locks, covers,
   alarm panels, cameras, automations, scripts, scenes) — not unioned with
   the guest baseline. Intents aren't restricted during an outage (owners
   keep lights, climate, and media); only those entity/domain-level
   physical-security writes are floored. Outages fail closed, they never
   grant control.

**Caller table.**

| Caller | Mode source | `caller_trust` sent to the orchestrator |
|--------|-------------|------------------------------------------|
| Gateway (satellite/HA voice) | server-resolved via step 1-3 above | `household` — the gateway's fast path (`mode_gate.py`) only takes effect while the house is actually in owner mode |
| SMS (`sms_webhook.py`) | server-resolved | `sms` |
| LiveKit (`livekit_integration.py`) | server-resolved | `household` (a LiveKit room can only be created by a signed-in owner/operator, see below) |
| jarvis-web, signed-in owner/operator or household member | server-resolved via step 1-3 above | `web_authenticated` |
| jarvis-web, home network | server-resolved via step 1-3 above | `web_local` |
| jarvis-web, guest network | always `guest` | `web_guest_net` |
| jarvis-web, anyone else | never forwarded: 401 sign-in required | — |

`household` is not a physical-presence check — it's every in-cluster caller
holding the shared `X-Service-Key`: the gateway's satellite/HA-voice path,
a remote Home Assistant companion-app user reaching HA and then the
gateway, and any other in-cluster pod that has the key. This is an
accepted risk of the service-key trust boundary (not closed by this
campaign), not a claim that `household` implies the caller is physically
in the house.

`caller_trust` is set **only by server code**, never copied from a request
body field, and it affects **only** the owner-PIN voice-override branch —
mode and permissions themselves come entirely from the table above. Only
`household`, `sms`, and `web_authenticated` may attempt the owner-PIN
utterance ("switch to owner mode, pin 123456"); `web_local`,
`web_guest_net`, `web_public` and untagged callers are refused before any
throttle or mode-service call, with zero counter increments. A surface may only ever *say* "owner mode" in a
response if the request actually resolved to owner via the table above —
narration never leads permission.

**Who the assistant addresses by name.** The orchestrator decides this in
one place (`build_query_context` and `resolve_addressee`), from the
`caller_trust` value and the house's own mode:

- **The staying guest's name** only for a guest-class caller —
  `web_guest_net` (jarvis-web's guest network) or `sms` (a phone-matched
  guest) — and only while the house's mode service reports guest mode and
  isn't degraded. A household caller during a stay (the home network, a
  signed-in member, a voice satellite) is never addressed as the guest.
  Voice satellites and device-matched guest sessions aren't addressed by
  name at all yet.
- **A signed-in household member's first name** (see
  `JARVIS_EDGE_NAME_HEADER`) only for `web_authenticated` during a stay. It's
  given to the model as a quoted data field, not an instruction, and never
  logged.
- **The owner** in owner mode, by the `owner_name` base-knowledge entry
  (never a display name); "what is my name" is answered from it directly.
- **Nobody** while the mode service is degraded, and nobody on the public
  audience. A degraded request is also never served from or stored in the
  semantic cache, and its prompt carries no name or owner base-knowledge
  entries.

The guest's and a member's names are given to the model as quoted data
fields, after a clean: letters, marks and `.`, `-`, `'` only (a guest's
name at most 4 words and 64 characters; a member's first word only). A
name that fails the clean isn't used.

Answers addressed to a named caller (the guest by name, a signed-in member)
are never read from or written to the semantic cache, so one caller's name
can't reach another. A new `caller_trust` value must be classified in
`tests/unit/test_trust_classification.py` before CI passes.

**Base-knowledge name entries.** A static `guest_name` entry is ignored (the
guest's name comes from the live stay, per caller; its row id is logged
once as `base_knowledge_static_guest_name_ignored`). `owner_name`/`name`
entries are rendered as "Property owner's name" in owner-mode prompts only.
Every other user/owner entry whose key contains `name`, and every
`owner`-category entry, is likewise rendered only in owner mode, and none
of them while the mode service is degraded.

**SMS conversation ids** are `sms_` + 24 hex characters of an HMAC of the
guest's number keyed on `SERVICE_API_KEY`; the number itself never appears.
Rotating `SERVICE_API_KEY` therefore starts every SMS conversation afresh
(earlier conversations simply expire). With an empty key (`DEV_MODE` only)
the id still hides the number but isn't secret.

**Which stay an SMS belongs to.** An incoming SMS matches a booking only when
the sender's number, as Twilio sends it (E.164, `+` and 10-15 digits), equals
the booking's phone number normalised to E.164. A booking stored as a national
number (`(555) 012-3456`, `020 7946 0000`) gets `SMS_DEFAULT_COUNTRY_CODE`
(default `1`; 1-3 digits, no `+`; an invalid value logs
`sms_default_country_code_invalid` and uses `1`). A mismatched country code
fails closed: the stay doesn't match and the sender gets the unknown-number
reply. Alphanumeric senders never match. Only confirmed, not-deleted bookings
count, in three phases, first match wins:

| Phase | Window | What the SMS may do |
|---|---|---|
| `current` | check-in ≤ now ≤ checkout (on a changeover day, the stay checking out first) | Whatever the guest profile allows |
| `recent` | checked out within the last 24 h | Answer only |
| `upcoming` | checking in within the next 48 h | Answer only |

"Answer only" means: no Home Assistant write; no automation, notification
preference or "text me that" (nothing is written to the admin database); and
only travel and checkout questions (general information, weather, directions,
dining, events, airports, flights) with the matching tools, so house-state
questions ("is the front door locked?") are refused. The phase travels to the
orchestrator in the request context; a request from the SMS caller with no
phase or an unknown one is answer-only too.

**Twilio signatures.** With `TWILIO_AUTH_TOKEN` set, every webhook must carry
a valid `X-Twilio-Signature` (403 otherwise). With it unset:

- `DEV_MODE=true`: validation is skipped with a warning;
- otherwise the webhooks answer **503** and store nothing, unless
  `TWILIO_ALLOW_UNSIGNED` is exactly `true`. Then every webhook is accepted
  unsigned (an ERROR `twilio_unsigned_webhooks_allowed` at startup, a
  WARNING `twilio_unsigned_request_accepted` per request). **Anyone who knows
  a guest's phone number can then text the assistant as that guest and read
  its answers about the stay.** Set the token instead wherever possible.
  `TWILIO_ALLOW_UNSIGNED` is ignored while the token is set.

**Twilio retries.** A `MessageSid` that isn't `SM`/`MM` + 32 hex digits is
rejected with 400. A retry of a message already answered, from the same
number, gets the same reply without asking the orchestrator again (an
unsigned request gets an empty reply instead, since its sender isn't
authenticated). A retry of a message still being answered waits up to 10 s
for the reply, then answers 503 with `Retry-After: 5` so Twilio retries or
uses its fallback URL. The stored reply is always the text sent, never an
error message.

**Guest floor and allowlist baselines.** Every guest's `restricted_entities`
always includes `GUEST_BASELINE_RESTRICTED_ENTITIES` (a floor, unioned in,
never replaceable by admin config) — by default this covers locks, covers,
alarm panels, cameras, automations, scripts, and scenes, leaving
lights/climate/media usable. Separately, `allowed_intents` and
`allowed_domains`: an **empty admin-configured list means "use the
baseline"**, never "allow everything" — `GUEST_BASELINE_ALLOWED_INTENTS`
(default weather/time/general_info/news/recipes/streaming) and
`GUEST_BASELINE_ALLOWED_DOMAINS` (default light/media_player/switch/climate)
apply whenever the admin UI's guest allowlist is empty.

> **Guest-allowed `switch` entities can drive locks or garage-door relays
> inside Home Assistant.** The `switch` domain is guest-allowed by default
> (baseline above) because most `switch.*` entities are lamps and small
> appliances — but HA lets a `switch` entity be wired to a physical-security
> relay. This is deployment-dependent and the guard cannot see HA's own
> entity wiring; if any of your `switch.*` entities control a lock or gate,
> remove `switch` from `GUEST_BASELINE_ALLOWED_DOMAINS` (and the admin UI's
> allowed-domains list) for your deployment.
>
> `notify.*` and `template.*` service calls are not gated by the permission
> guard — they aren't reachable via any current intent's write path, so
> this is a documented gap rather than an active hole. Don't add a new
> code path that calls `notify.*`/`template.*` with guest-controllable
> arguments without gating it first.

**Degraded (outage) behavior.** When the mode service can't be reached or
returns an error, the orchestrator falls back to `degraded_permissions()`
— never owner, never unrestricted. `ha_permission_fallback_disabled`
(logged once at startup) fires only if you deliberately set
`HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES=[]` — an explicit, logged
deployer opt-out, never a silent default.

**Owner PIN.** Set in the admin UI's Guest Mode page, which has a dedicated
Owner PIN section (6-digit input, confirmation input, client-side format
check). The server independently validates the format — exactly six ASCII
digits, `re.fullmatch`, no trailing newline — before hashing and storing it,
rejecting anything else with `422 {"error": "owner_pin_format"}`; a PIN that
passed only the client-side check could otherwise be stored and then be
permanently unverifiable, since `verify-pin` (below) applies the same
fullmatch check before it ever compares against the hash. Verification lives
**only** in the admin backend (`POST /api/internal/guest-mode/verify-pin`,
service-key-only — the mode service holds no PIN state and never hashes or
compares a PIN itself). The hash is PBKDF2-HMAC-SHA256, salted
(`admin/backend/app/utils/passwords.py`). **If you set a PIN before this
version**, it was stored as an unsalted SHA-256 hash; that legacy format is
detected and treated as "PIN must be re-set" (`verify-pin` returns
`not_configured` for it) — the admin UI prompts you to set a fresh PIN
once. A per-trust-tier lockout (`MODE_OVERRIDE_LOCKOUT_THRESHOLD` failed
attempts within the window locks that tier for `MODE_OVERRIDE_LOCKOUT_MINUTES`)
protects against PIN-guessing; **changing the PIN in the admin UI clears
the lockout counter** for every tier. The lockout is per-tier
(`household`/`sms`/`web_authenticated`), never keyed on session id, room,
or device id, so a caller can't dodge it by rotating identifiers.

**State questions are answered, never written.** A question about device
state ("are the office lights on or off right now", "is the front door
locked", "check if the garage is closed") is answered from Home Assistant
state under a read-only permission scope: the permission guard refuses
every write for that request before anything else is checked, whatever the
LLM extracts. Commands phrased as requests ("can you turn off the lights?",
"make sure the doors are locked", "leave the porch light on") are still
commands. A status-sounding command ("turn the office lights on", "set the
temperature to 70") now executes; it used to get a house-wide status read
back instead. The question/command classifier is English-only; it strips
a leading "hey"/"ok" and the assistant's name (the admin profile's
configured name as well as "Athena" and "Jarvis"), and classifies only the
first 300 characters of an utterance. Questions that reach the music or TV
handlers ("is music playing", "did you turn off the TV") run read-only too.

**Large writes need explicit wording.** One utterance may write at most
`HA_WRITE_FANOUT_CONFIRM_THRESHOLD` distinct entities (default 6) unless it
names that scope itself: `all`, `every`, `everything`, `whole`, `entire`,
`house`, the room group's name, or two or more of the rooms it covers. A
plain command ("turn off the office lights") is allowed up to
`HA_WRITE_FANOUT_HARD_LIMIT` (default 18). A target of every entity in a
domain, area, floor or label is never confirmable: without explicit wording
it's always answered with the command to say instead. That covers the
fallbacks for a missing "good night" scene (every light) and "leaving" /
"goodbye" scene (every light and every lock): with no such scene
configured, those phrases now get the commands to say instead of acting.
`0` disables either limit. Both variables are read by every service that
loads the shared configuration (so a shared ConfigMap reaches all of
them); a hard limit below the threshold, other than `0`, logs one ERROR at
startup and both fall back to the defaults, so the check stays on. An HA
light group entity counts as one.

Above the bound, what happens depends on the surface:

| Surface | Response |
|---------|----------|
| Home Assistant Assist (gateway HA conversation route), LiveKit, jarvis-web chat, SMS | "That would turn off 11 lights in the office. Should I go ahead?" A bare "yes" (or "Yes, please.", "okay do it", "go ahead", "do it") within 60 seconds runs exactly those entities; "no" cancels; anything else is treated as a new request. |
| Wyoming satellites, the OpenAI-compatible `/v1/chat/completions` path, any other caller | "That would turn off 11 lights in the office. To do it, say: turn off all the office lights." Nothing is stored. |

Which response a request gets comes from the `/query` body's
`supports_followup` field, set in server code by the callers above. The
orchestrator trusts it from any caller that passes ingress authentication
(`X-Service-Key`), the same trust class as `caller_trust`. Setting it only
changes the phrasing (a prompt instead of the command to say): a
confirmation can be replayed only by a matching caller on the same session,
under full re-authorization.

A confirmation is bound to the caller that got the prompt: the caller
trust tier, device id, room and resolved mode. For Home Assistant Assist,
the gateway forwards the HA device's own id as `voice_device_id`, which is
used only for this binding (it isn't the `device_id` used to look up a
guest session). A caller that sends neither a trust tier nor a device id
can't hold a confirmation and always gets the command to say. A "yes" from
anyone else on the same session gets "I'm not sure what you're agreeing
to." and changes nothing; that caller's own requests on the session store
no context, so they can't overwrite the pending confirmation either. The
replay runs under the replying turn's own permissions, so a mode change in
between is enforced. With Redis configured, a "yes" whose single-use claim
can't reach Redis is treated as already used (nothing runs), so two
replicas can never both replay it. If Home Assistant's gateway
pre-routing (`ha_intent_prerouting`, off by default) is enabled, it may
answer a short "yes" itself before it reaches the orchestrator; the
pending write then simply expires unexecuted.

**Kill switch.** The admin UI feature `state_question_routing_kill_switch`
(seeded **disabled**) is an emergency revert: **enabling** it turns
state-question routing **off** and restores the previous routing. It's
inverted on purpose: the orchestrator treats a missing flag, or an admin
API it can't reach, as disabled, so an enable-style flag would silently
switch the protection off on a fresh install or during an outage. The
read-only guard and the large-write limits stay active either way; to relax
the limits, set both variables above to `0`.

While the kill switch is on, a question goes back to the previous
smart-home path, which can resolve it to a device change. That change is
never made silently, whatever the limits (including `0`): even a
single-device change from a question gets "Should I go ahead?" (or the
exact command to say, on surfaces without a follow-up). That includes
single-device changes such as the thermostat, the bed warmer, motion
overrides and scene or routine activation ("is good night mode on?"),
timed sequences, and the dynamic automation agent's actions (service
calls, creating or deleting automations, notifications). A TV
or music question is answered from the device's state, followed by the
command form ("To turn it off, say: turn off the living room TV."). A
command the classifier misread as a question still works after "yes" or
the suggested wording. As a rollback step, turning the switch on reverts routing
without letting questions write.

Other state-question behaviour:

- A question about a device the classifier can't name ("is the heater
  on") is resolved by the intent extractor, still as a read. If neither
  can name the device, the answer asks which device is meant rather than
  reporting on the lights. The reply ("the thermostat", "the kitchen",
  even "yes") is answered as that question, read-only; it never continues
  an earlier command. An explicit command ("turn on the kitchen lights")
  still runs.
- The house-wide status reports ("which lights are on", "are the doors
  locked") answer only questions about their own device type. A question
  about a specific device ("garage door status", "what's the status of
  the thermostat") gets that device's own state instead.
- A TV question ("is the TV on", "did you turn off the TV") is answered
  from the TV's Home Assistant state.
Public/unauthenticated callers (`web_public`) can never attempt the PIN at
all — see the caller table above.

**Override duration cap.** `POST /mode/override`'s `timeout_minutes` is
clamped server-side to `OVERRIDE_MAX_TIMEOUT_MINUTES` (default 240 / 4
hours), applied regardless of the PIN outcome — a caller-supplied value
above the cap is silently reduced to the cap, never rejected. Every
non-`"owner"`/`"guest"` value for the request's `mode` field (a typo, a
different case, an empty string) is rejected with 422 before any PIN check
runs; a stored override value that somehow predates that validation is
discarded (treated as no override) rather than trusted.

> **PIN-change recovery depends on the admin backend and OIDC being up.**
> If both are down, you can't set or verify a new PIN through the UI.
> Physical control at the Home Assistant device (or the HA app/UI directly)
> is the fallback during that window — this campaign doesn't add an
> out-of-band PIN-reset path. The D28 posture reminders below are
> candidates for promotion to metrics/gauges once ATHENA's monitoring
> stack (`MODULE_MONITORING`) is generally deployed; for now they're
> log-only.

**Mode-service ingress auth.** `MODE_SERVICE_INGRESS_AUTH` gates the mode
service's `/mode*` routes behind `X-Service-Key`, mirroring
`ORCHESTRATOR_INGRESS_AUTH`'s three-value contract (`enforce` default in
code, `warn` in the shipped template for a staged rollout). While `warn` is
active, the mode service logs `mode_service_ingress_auth_warn_active` at
startup and **every hour** — this is the signal to watch for during
rollout; don't leave a production deployment on `warn` indefinitely.

**Last-good config and staleness (D37).** The mode service polls the admin
backend for guest-mode config; on a fetch failure it keeps serving the
last config it successfully loaded rather than failing every request.
`GET /health` reports `config_source` (`"admin"` — the last poll
succeeded; `"last_good"` — currently serving a stale cached config after a
failed poll; `"none"` — no successful load yet since startup, i.e. cold
start, D38: `/mode` reports `mode="degraded"` until the first successful
load) and `config_age_seconds` (how old the currently-served config is).
Once that age exceeds 15 minutes, the mode service logs
`mode_service_config_stale` (ERROR) every 10 minutes until a fresh load
succeeds. **A tightening saved in the admin UI while admin-backend is down
is not applied until admin-backend comes back** — the mode service keeps
the older, possibly more permissive config in the meantime; this is an
accepted residual risk, surfaced by `config_age_seconds` and the stale-log
signal above, not silently hidden.

**Guest-mode booking source.** The mode service decides guest
vs owner from bookings, not from an in-process iCal poll of `calendar_url`
directly.

- **Sources.** `MODE_BOOKINGS_SOURCE` (default `auto`): `auto` reads the
  admin backend's `calendar_events` (fed by `calendar_sources`, incl.
  Lodgify) as the **required** source, plus the legacy `calendar_url`
  iCal feed (if the Guest Mode page has one set) as **advisory-additive**
  — its last successful fetch keeps adding guest time in every state
  (fresh, stale, even expired), but its own freshness never degrades the
  house. `admin`: admin only, no legacy iCal at all. `ical`: the legacy
  URL is the only, required, source (for a deployment with no
  admin-managed bookings) — an empty `calendar_url` in this mode is
  `never_loaded` (logged once, `mode_bookings_source_misconfigured`), and
  clearing or replacing the URL discards everything learned from the old
  one, so the house degrades rather than trusting a feed nobody reads.
- **Legacy iCal URL rules.** The mode service fetches `calendar_url`
  through the same SSRF guard admin-backend applies to calendar-source
  URLs: `https://` only, private/loopback targets blocked unless listed
  in `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`, every redirect re-validated. A
  plain `http://` URL is a failed fetch (never loaded). Events whose end
  isn't after their start, or that last longer than 60 days, are dropped
  at parse time and counted in one `mode_bookings_ical_events_dropped`
  warning per fetch. The Guest Mode page warns when a `calendar_url` is
  set but its source is `never_loaded` or `expired` (the mode-status
  proxy passes each source's status and required flag through).
- **Freshness.** Per source: `never_loaded` (no successful fetch ever) →
  age of the last success > `MODE_BOOKINGS_MAX_AGE_SECONDS` (default
  21600s / 6h, clamped to `[max(300, 2 x the iCal poll interval),
  604800]`) → `expired` → else last attempt ok → `fresh` → else `stale`.
  The fetch window always extends far enough into the future
  (`now + max_age + buffer_before + 1h`) that a scheduled check-in
  during an outage flips the house to guest on the mode service's own
  clock, from the last-good snapshot, with no new fetch needed.
- **Precedence.** Config never loaded → `degraded`. An active, unexpired
  override → that mode. `enabled == false` → `owner` (bookings are still
  fetched in the background, just not consulted). Any active booking from
  a considered source → `guest`: the required source and an advisory
  source are both considered in every state except `never_loaded`, since
  old data can only add guest time. The required source `fresh` or
  `stale` → `owner`. Otherwise → `degraded`. Only the required source's
  status can produce `degraded`.
- **Startup.** The mode service waits at most 5 s for its first booking
  refresh before it starts serving; a slower fetch (e.g. a slow iCal feed)
  finishes in the background and the house reports from whatever has
  loaded so far.
- **The stale-lookahead residual.** While the required source is merely
  `stale` (a fetch failure after a prior success, still within
  `max_age`), a booking created, moved earlier, or **extended** after
  that last success is invisible until the next success. The house can
  read `owner` for part of that window while a guest is actually
  present — narrow (bounded by `max_age`) but real. Cancellations or
  shortenings made after the last success keep the house `guest`, which
  is the restrictive (safe) direction.
- **No PIN escape from `degraded`.** Owner-PIN verification itself
  requires the admin backend, so during the same outage that drives the
  house to `degraded`, `POST /mode/override` also can't verify a PIN
  (`503 owner_pin_verification_unavailable`). The only runtime lever is
  raising `MODE_BOOKINGS_MAX_AGE_SECONDS` (`kubectl set env
  deploy/athena-mode-service ...`), which restarts the pod and only helps
  once the outage is already over.
- **Stay windows.** Active iff `checkin - buffer_before <= now <
  checkout + buffer_after` (half-open — a checkout and the next check-in
  can be flush with no owner gap). Date-only and floating (no `Z`/no
  `TZID`) booking times are localised in `DEFAULT_TIMEZONE` at write time
  (admin sync) and read time (the mode service's own legacy-iCal parsing)
  using PEP 495 `fold=0` semantics — no pytz. A feed entry is a block
  (`status='blocked'`, never a stay) only when its whole summary —
  trimmed, lowercased, runs of whitespace collapsed — is one of its
  source type's labels:
  - `airbnb`: `Not available`, `Airbnb (Not available)`
  - `vrbo`: `Blocked`
  - `lodgify`: `Closed Period`, `Blocked`, `Closed`, `Closed Block`,
    `Owner Block` (Lodgify's export masks guest names with `*`, so none of
    these can be a masked name)
  - `generic_ical`, and the mode service's legacy `calendar_url`: all of
    the above plus `Unavailable`

  It's a whole-string match, so a guest called "Tom Blocked" is a stay,
  and `Reserved` is never a block. A label not in the list reads as a
  stay: the safe direction, since the house goes to guest mode rather than
  owner. If a platform starts exporting a new block label, it shows up as
  a booking until it's added to the list. An owner-set
  `cancelled`/`pending` status is never overwritten by a re-sync. RRULE
  recurrence is **not** expanded in the mode service's legacy-iCal path.
- **Dedupe and suppression.** The same `(source, key)` collapses exactly;
  the same local `(checkin date, checkout date)` pair collapses via a
  union window (never shrinks guest time), which is how a Lodgify-synced
  admin row and its legacy-iCal twin merge into one booking. A
  soft-deleted or cancelled admin row's day pair suppresses a matching
  legacy-iCal booking; admin rows are never suppressed by their own
  deletion (the admin endpoint already excludes them). Residual: a
  rebooking of the same days that exists **only** in the legacy iCal feed
  (not in any admin source) after a soft-delete/cancel is suppressed too
  — visible on the Guest Mode page's source list if it matters to you.
- **Calendar sync** (admin-backend, `calendar_events`). One code path
  writes feed-derived bookings, for the background loop, the per-source
  Sync button and Sync all alike.
  - *Lodgify API authority.* A source whose type is `lodgify`, or whose
    feed host is `lodgify.com`/`*.lodgify.com`, syncs from the Lodgify
    API whenever an enabled Lodgify API key exists (External API Keys,
    service `lodgify`). If the API call fails, or a key is configured but
    can't be decrypted or is blank, the sync is marked failed, writes
    nothing and keeps the existing bookings. It never falls back to the
    iCal export, whose turnover-day slices and fresh-per-fetch UIDs aren't
    bookings the API knows about. With no enabled key, the iCal export is
    used.
  - *iCal natural key.* Feeds can mint a new UID on every fetch (Lodgify's
    export does), so an event whose UID isn't already stored matches this
    source's own row on the same local (check-in date, check-out date) in
    `DEFAULT_TIMEZONE`. A re-fetch with fresh UIDs updates the existing
    rows instead of adding a new set. A block and a booking on the same
    dates become one confirmed row.
  - *Source-scoped keys.* A sync never moves, blocks or hides another
    source's row. When an event's UID (or a Lodgify reservation key) is
    already used by another source's row, or by an orphan, it's stored
    under a source-specific ID instead (`src:<source id>:<hash>`), and the
    source shows "N events stored under a source-specific ID because their
    IDs are used elsewhere" until a sync without them. Nothing is dropped.
    An event with no UID gets an `ical-nouid:` ID and is matched by its
    dates afterwards.
  - *Deleted and cancelled entries stay that way.* A synced row you
    delete or cancel isn't brought back by a re-sync. A new-UID event on
    the same dates matches it (and is left as you set it) only when the
    title is the same **and** that row was listed by the source's previous
    successful sync; the source card and the Sync toast count these as
    "matched entries you deleted or cancelled". Otherwise the event is
    added as a new booking, since a rebooking must not be lost. The one
    residual: a cancellation and a same-title rebooking of the same dates
    with no successful sync in between match the cancelled row, so that
    stay reads as owner until you restore or re-add it.
  - *Sync interval.* `sync_interval_minutes` must be between 5 and 1440.
    The scheduler also treats any smaller stored value as 5.
  - *One writer per source.* Every sync takes a per-source lease
    (`system_settings` key `calendar_sync.lock.<source id>`, 10 minutes;
    the fetch itself is capped at 8), so two admin-backend replicas, or a
    manual sync during a scheduled one, never write the same source at
    once. A second click while a sync runs reports "A sync for this source
    is already running". The key `calendar_sync.last_stamp.<source id>`
    records the previous successful sync for the rule above.
  - *Lodgify API outage.* Existing bookings are kept, but a stay created
    during the outage isn't seen until the API answers again: the house
    reads owner for it while the Calendar Sources card shows the failed
    sync. The mode service still reports the admin source as `fresh`,
    because admin-backend itself is reachable.
  - *Deleted sources.* Deleting a calendar source cancels its current
    and upcoming guest sessions, but leaves its synced rows in place with
    no source (`source_id` becomes empty), and they keep counting as
    bookings until you delete them on the Guest Mode page. A re-added
    source gets its own rows; an orphaned Lodgify API row is taken back by
    the next API sync.
  - *Guest sessions.* After a Lodgify source syncs, its guest sessions
    are updated in a second step. If that step fails, the bookings are
    still saved and the source shows "Guest sessions could not be updated
    (…); bookings were saved"; the next sync, or `POST
    /api/calendar-sources/sync-guest-sessions`, retries it.
  - *Errors.* Sync errors and logs name only the error type and HTTP
    status (`iCal fetch failed (ConnectError); no changes written`), never
    the feed URL or response text.
- **Don't point the legacy `calendar_url` at the Lodgify export when a
  Lodgify API key is set.** The mode service reads that URL directly, so
  the Lodgify export's turnover-day slices would add guest time the API
  doesn't list. The Guest Mode page warns about this combination; clear
  the legacy URL and let Calendar Sources provide the bookings.
- **Calendar Sources API.** Every route except `GET
  /api/calendar-sources/types` needs a signed-in user (Bearer session or
  `X-API-Key`). Listing, `GET /{id}`, `POST /{id}/test` and `POST
  /test-url` need `read`; creating, editing, deleting, syncing, Sync all
  and `POST /sync-guest-sessions` need `write`. An `X-Service-Key` header
  is refused with 401 on every one of them, even alongside a valid user
  credential. The list, create and edit responses carry only
  `ical_url_masked` (`https://host/…`); the full feed URL, which embeds its
  access token, is returned only by `GET /api/calendar-sources/{id}`, and
  each of those reads is recorded in the audit log
  (`calendar_source_url_revealed`, without the URL). `POST /test-url`
  takes the URL in a JSON body (`{"url": …, "source_type": …}`); a `url`
  query parameter is refused with 422, so the URL never lands in access
  logs. Feed URLs must be `https://`. Source changes are recorded in the
  audit log with the masked URL.
- **Manual entries** are still entered and stored in the browser's local
  timezone (unchanged) — the Guest Mode page's manual-entry modal shows
  the property timezone alongside the input for reference.
- **Visibility.** `/mode` reports `bookings_source`, `bookings_status`
  (the required source's status, or `not_required` while guest mode is
  disabled), `bookings_age_seconds`, `property_timezone`,
  `property_timezone_valid`, and `bookings_sources` — a
  `{source: {"status", "required"}}` map. `/health` carries the same plus
  per-source counts (`bookings_by_source`) and the fetch window. A guest
  `reason` names the booking by label, not by guest name: `<source> #<id>`
  for an admin row (e.g. `lodgify #42`), `ical <key prefix>` for a
  legacy-iCal booking. The Guest Mode page shows the
  mode service's live mode and reason and refreshes every 30 s.
- **The admin UI's mode-status panel** (`GET /api/guest-mode/mode-status`)
  needs `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` to cover the mode service's
  ClusterIP/CIDR, or it reports `{"reachable": false, "error":
  "ssrf_blocked"}` instead of the live mode.
- **Changing `DEFAULT_TIMEZONE`** requires restarting both admin-backend
  and the mode service (both read it via `envFrom` at pod start), then a
  re-sync — per-source (`POST /api/calendar-sources/{id}/sync`) or the
  Calendar Sources page's "Sync all" button (now actually enqueues a sync
  per enabled source, rather than being a no-op). Either needs an owner or
  operator session, or an owner/operator `X-API-Key`. For a feed that
  changes UIDs, a zone change that moves a stay's local dates makes its
  existing row miss the date match once, so that sync adds a second row for the stay; delete the old
  one on the Guest Mode page.

**`GET /api/guest-mode/config`'s dual auth path.** This route now accepts
either the existing OIDC/Bearer session (unchanged behavior, full
`has_permission('read')` check) *or* a service key (`X-Service-Key`, used
by the mode service and orchestrator to fetch config without a human
session) — whichever the request presents: an `X-Service-Key` header takes
the service-key path, its absence takes the OIDC/Bearer path. On the
service-key path there's no authenticated user, so the permission check is
skipped entirely (the key itself is the trust boundary). If no config row
exists yet, the service-key path returns the built-in defaults and
**creates nothing** — `created_by_id` is a required column with no user to
attribute it to on this path, so a row can only ever be created via the
OIDC/Bearer path. The response never includes the PIN hash under either
path — only `owner_pin_configured` (and, since Pass H,
`owner_pin_needs_reset` for a legacy-format hash) as booleans.
`POST /api/internal/guest-mode/verify-pin` answers `status: "not_configured"`
for both "no PIN set" and "a legacy pre-D30 hash that can't be verified" --
it does not distinguish the two in its response; the config route's
`owner_pin_needs_reset` is where that distinction is surfaced.

**LiveKit browser token TTL.** `LIVEKIT_USER_TOKEN_TTL_MINUTES` (default
30, clamped 1-1440) bounds how long a browser-facing LiveKit room token
stays valid for *joining* a room — it doesn't disconnect an
already-connected participant early. Server-side Athena participant
tokens are unaffected (they pass an explicit 24-hour TTL).

**jarvis-web: who may use it.** jarvis-web serves a browser without
sign-in only when the request provably comes from the home network, and
requires sign-in for everything else: `401 sign_in_required` (with
`WWW-Authenticate: Jarvis`) on every route, static files included, with no
orchestrator, Home Assistant or admin-backend call. The only anonymous
answers are `/api/health` (`{"status": ...}` and nothing else, for probes),
the static 401 sign-in page at `/`, and the embed relay below. The API docs
(`/docs`, `/redoc`, `/openapi.json`) exist only with
`JARVIS_ENABLE_DOCS=true`, a development flag. With nothing configured,
every browser gets the sign-in page.
Each request resolves to one class, from server-side evidence only:

| Class | How | What it gets |
|---|---|---|
| Home network (`web_local`) | the home rule below, or an attested `home` from an auth proxy | UI, chat and household reads; owner-only routes while the house is in owner mode, else `403 guest_stay_active` |
| Guest network (`web_guest_net`) | the address is in `JARVIS_GUEST_NETWORKS` | UI, chat and push-to-talk, always guest mode (even when the house is vacant or owner mode is forced), view-only controls, only the reads the guest UI loads (`403 guest_network` on sensors, media and appliances); the only browser class the assistant addresses by the staying guest's name |
| Signed in (`web_authenticated`) | an auth proxy's attested identity in a household group, or a Bearer token for an owner/operator | everything |
| Service | the orchestrator's `X-Service-Key` | the household GET routes it uses for voice answers (`/api/appliances/*`, `/api/sensors/*`, `/api/media`), nothing else |
| Embed relay | chat-embed with `JARVIS_RELAY_KEY` | chat only, as the public audience |

**The home rule.** A request is home when all of these hold:

- its candidate address is in `JARVIS_LOCAL_NETWORKS` and not in
  `JARVIS_LOCAL_EXCLUDE`. Behind a reverse proxy the candidate is the hop the
  proxy appended to `X-Forwarded-For` (the peer must be in
  `TRUSTED_PROXY_CIDRS`; `JARVIS_LOCAL_TRUSTED_HOPS` for a proxy chain), never
  a deeper hop and never `Cf-Connecting-Ip`. With
  `JARVIS_DIRECT_CLIENTS=true` it's the TCP peer.
- it carries no non-empty `Cf-Connecting-Ip` or `Cf-Ray` header.
- its `Host` header (port and trailing dot stripped) is in
  `JARVIS_ALLOWED_HOSTS`. This is the DNS-rebinding guard; `X-Forwarded-Host`
  is never read. Without `JARVIS_ALLOWED_HOSTS` home networks are disabled.

At startup jarvis-web drops, with an ERROR, any home or guest entry that
contains its own pod/host address, a trusted proxy (or is wider than one),
or, in direct mode, a default gateway, unless that address is listed in
`JARVIS_LOCAL_EXCLUDE` (so excluding a node's /32 keeps the LAN entry).
Home networks are disabled without `TRUSTED_PROXY_CIDRS` (outside direct
mode). An address in both lists is a guest (with a warning).

**Deployment shapes.**

1. **Household only, behind a proxy.** `TRUSTED_PROXY_CIDRS` (the proxy's
   addresses), `JARVIS_LOCAL_NETWORKS`, `JARVIS_ALLOWED_HOSTS`. This trusts
   that every peer in `TRUSTED_PROXY_CIDRS` is a proxy that appends what it
   saw. On a flat pod network (flannel) any pod can connect and forge that
   hop: apply `optional/networkpolicy-jarvis-web.yaml` on a CNI that
   enforces it, or use shape 3.
2. **Household only, browsers reach jarvis-web directly.**
   `JARVIS_DIRECT_CLIENTS=true`, `JARVIS_LOCAL_NETWORKS`,
   `JARVIS_ALLOWED_HOSTS`, and no `TRUSTED_PROXY_CIDRS`. The source address
   must be preserved (host networking, or a LoadBalancer/NodePort with
   `externalTrafficPolicy: Local`); behind SNAT or `externalTrafficPolicy:
   Cluster` every internet caller looks like a node. In Kubernetes it
   refuses to start until you set
   `JARVIS_DIRECT_CLIENTS_ACK_SOURCE_PRESERVED=true`. Before relying on it,
   send one request from outside your network (for example a phone on
   cellular) and check jarvis-web logs `jarvis_caller_resolved` with
   `caller_class=web_public`.
3. **Household plus sign-in from the internet (edge mode).** An auth proxy
   in front of jarvis-web classifies every request and attests it:
   `optional/jarvis-web-edge-auth.yaml` is a Traefik + Authentik
   forward-auth template (read its header). jarvis-web needs
   `JARVIS_EDGE_ATTESTATION_SECRET` (Secret key `current`, not optional)
   and optionally `JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS` (key `previous`,
   optional), `TRUSTED_PROXY_CIDRS` (the proxy), `JARVIS_LOCAL_NETWORKS` and
   `JARVIS_ALLOWED_HOSTS` (to corroborate an attested home: in edge mode the
   network alone never grants home), and `JARVIS_HOUSEHOLD_GROUPS`. The
   signed-in identity is read from `JARVIS_EDGE_IDENTITY_HEADER` (default
   `X-authentik-username`) and its groups from `JARVIS_EDGE_GROUPS_HEADER`
   (default `X-authentik-groups`), split on `JARVIS_EDGE_GROUPS_SEPARATOR`
   (default `|`, Authentik's format) and matched exactly and
   case-sensitively; anyone else gets `403 not_household`. A household
   member's display name is read from `JARVIS_EDGE_NAME_HEADER` (default
   `X-authentik-name`, which Authentik's outpost sends) and only its first
   word is used, only if it's made of letters, marks and `.`, `-`, `'`
   (1-32 characters); the assistant addresses the member by it during a
   stay, the welcome greeting uses it, and it's never logged. A Bearer
   sign-in carries no name. The edge must strip all three header names
   from inbound requests; a name outside the template's strip list stops
   jarvis-web at startup until `JARVIS_EDGE_HEADERS_ACK_STRIPPED` lists
   exactly those custom names (comma-separated) to confirm you added them
   there; `true`, or an ack naming other headers, doesn't pass (the full
   set it expects stripped is logged as `jarvis_edge_strip_headers`).
   jarvis-web also refuses to start if the identity, groups or name header
   is `X-Jarvis-Edge-Class`, `X-Jarvis-Edge-Attestation`, `X-Service-Key`,
   `X-Jarvis-Relay-Key`, `X-Jarvis-Relay-Client`, `Authorization`,
   `Cookie`, `Host`, `X-Forwarded-For` or `CF-Connecting-IP`, or if two of
   them name the same header.
   Edge mode that can't serve the household doesn't start: no
   `TRUSTED_PROXY_CIDRS`, no usable `JARVIS_LOCAL_NETWORKS` entry, or no
   `JARVIS_ALLOWED_HOSTS` exits with `jarvis_edge_misconfigured`, so a
   rolling deploy stalls instead of going Ready and answering every
   household browser with 401. A deployment with no home network at all
   sets `JARVIS_EDGE_SIGN_IN_ONLY=true` (then everyone signs in; the
   trusted proxy is still required, and `JARVIS_ALLOWED_HOSTS` is still
   needed for WebSockets: with it empty jarvis-web logs the ERROR
   `jarvis_websockets_disabled` and refuses every WebSocket, which stops
   browser music playback). An optional
   sign-in host always requires sign-in, so an owner at home during a guest
   stay can sign in for control (`JARVIS_SIGNIN_URL` puts a "Household
   sign-in" link on the page). The attestation value must be at least 32
   characters and not a placeholder, `SERVICE_API_KEY` or `JARVIS_RELAY_KEY`,
   or jarvis-web won't start. Rotate it with `current` + `previous`: new
   value as `current`, old as `previous`, roll jarvis-web, re-render the
   proxy's Middlewares, drop `previous`, roll again. MFA on the sign-in flow
   is recommended.

`JARVIS_LOGIN_URL` adds a Sign in link to the 401 page; `JARVIS_LOGOUT_URL`
is the page's Sign out link for a signed-in user (point it at a flow that
ends the identity provider's session).

**The embed.** An optional add-on to any shape: chat-embed relays a website
visitor's chat with `JARVIS_RELAY_KEY` (the same value on both sides, off
by default). A relayed message is always the public audience, however it's
decorated, and a wrong key is `401` with no fall-through. Each visitor gets
`JARVIS_RELAY_REQUESTS_PER_MINUTE` (20) and all relayed traffic
`JARVIS_RELAY_GLOBAL_PER_MINUTE` (300), per replica (so 2x with two
replicas). Two supported shapes: chat-embed in the cluster calling
jarvis-web's Service (recommended), or an off-cluster chat-embed through a
proxy route that skips sign-in and attestation for requests carrying the
relay key (commented in the edge template, with its strip Middleware).
Anything else is unsupported. A relayed conversation continues only with
the `session_id` jarvis-web returned, and only for the visitor it was
minted for (the id is bound to the visitor's address, a /64 for IPv6,
under the relay key); anything else starts a new conversation. Rotating
`JARVIS_RELAY_KEY` starts every embedded conversation afresh.

**The public audience** (a relayed visitor, or any orchestrator caller with
`caller_trust="web_public"`) is a hard-coded narrow profile, not the guest
profile: weather, news, recipes, what's streaming and general questions;
no control of anything; no guest identity, base knowledge, home address,
memories, semantic cache or web search. Widening the guest profile never
widens it.

**Guests and house state.** The control handler checks the control
permission before anything else, so a caller without it (a guest, under the
default guest profile) no longer gets presence ("is anyone home"), sensor
readings or device status from it. Add `control` to the guest profile's
allowed intents if guests should have those answers (it also lets them
control what the guest domains allow).

**Voice.** Push-to-talk (`/api/voice/*`) is for every browser class (home network, also during a guest stay; the guest network; signed in); anonymous callers and the embed relay get none. A voice turn is a chat turn, so it is refused whatever chat would refuse. Each client gets `JARVIS_VOICE_REQUESTS_PER_MINUTE` (default 30) voice calls a minute, per replica (`429` beyond that), text to speak is capped at `JARVIS_TTS_MAX_CHARS` (default 5000; `422` beyond), and uploads are decoded as WebM only (ffmpeg with a pinned input format and the file protocol alone). Always-on voice (LiveKit rooms) stays owner-only.

**Browser rules.** Mutating requests from the page carry
`X-Jarvis-Request: 1`; a request without it gets `403 reload_required`
(reload open tabs after upgrading); the page and its scripts are served
with `Cache-Control: no-cache`, so a reload always gets the current ones.
WebSockets need an `Origin` whose host is in `JARVIS_ALLOWED_HOSTS` (the
request's own `Host` is not enough; with `JARVIS_ALLOWED_HOSTS` empty,
jarvis-web logs `jarvis_websockets_disabled` and refuses every WebSocket).
CORS is off unless `JARVIS_CORS_ORIGINS` lists exact origins (`*` and
`null` are refused). Chat session ids are minted by jarvis-web and bound to
the browser that started them; the binding key derives from
`SERVICE_API_KEY`, so every replica shares it. Without `SERVICE_API_KEY`
each process draws its own (logged as
`jarvis_chat_session_key_per_process`): a chat that moves between replicas
or survives a restart starts over. Uncached Bearer checks are limited to 10
a minute per client. `TRUST_CF_CONNECTING_IP=true` (default off) lets the
per-client rate limits read `Cf-Connecting-Ip`, only when the whole
forwarding chain is in `TRUSTED_PROXY_CIDRS` (a Cloudflare tunnel); it never
affects the home rule. chat-embed reads the same variable for its
per-visitor limit.

`JARVIS_PUBLIC_MODE` was removed: `household` now stops jarvis-web at
startup (configure the home network instead: shape 1 or 2), and any other
value is ignored with a warning.

**Denial observability.** Every guard-refused Home Assistant write
increments the `athena_ha_write_denied_total{domain, scope_mode}` Prometheus
counter, regardless of which entry point or node produced it — alert on a
sustained rise if you want to notice a caller hammering a write it doesn't
have. A room light command that drops a light the request may not use
(see "How a room's lights are chosen") adds one increment for that command,
plus one for every denial the guard itself raises; so a single guest command
can count more than once.

#### How a room's lights are chosen

A room light command ("turn on the kitchen lights") resolves to the smallest
set of light entities that cover that room's lights, and writes each physical
bulb at most once.

1. **`HA_LIGHT_GROUPS`.** If the room has an entry, that entity is the room.
   Keys match regardless of case, spaces or underscores (`living room`,
   `Living_Room`). An entry naming an entity Home Assistant doesn't report is
   logged and ignored. The same entry answers "are the kitchen lights on".
2. **Name matching.** Otherwise a light belongs to the room when the room's
   words are whole words at the start of its entity id or friendly name
   (`kitchen` matches `light.kitchen_ceiling`, not `light.master_kitchen`). A
   requested part ("hall and nook") that has no such light falls back to a
   whole-word match anywhere in the name, for that part only. Lights matching
   `HA_ROOM_LIGHT_EXCLUDE_ENTITIES` (by default an id with `led_ring` or
   `status_led`, such as a voice satellite's ring) are left out of this tier
   only.
3. **Synonyms.** `hall`/`hallway`/`corridor`/`foyer`, `bath`/`bathroom`/
   `restroom`/`washroom`, `living room`/`livingroom`/`lounge`, `office`/
   `study`/`home_office`, `basement`/`cellar`, `garage`/`carport`,
   `kitchen`/`kitchenette`, `dining`, `front` (also `entrance`/`entryway`),
   `back`/`backyard`/`rear`/`patio`, `outside` (also `porch`/`outdoor`/
   `patio`), `master bedroom` (also `main_bedroom`/`primary_bedroom`). Saying
   a synonym reaches its room, except that `porch` and `patio` do not reach
   `outside`, and `patio` does not reach `back`: those two bare words open
   other lights' names (`outside_*`, `back_door_*`), so "outside" reaches
   `porch` and `patio` but not the other way round. Broader aliases such as `bed`, `work`, `family`,
   `primary` or a floor name are not synonyms, because as a word that opens
   an id they would pull in other rooms' lights; use `HA_LIGHT_GROUPS` or a
   room group for those.
4. **Cover.** A group that is part of another matched group is dropped, and
   groups that overlap only partly contribute just the lights not already
   covered, as individual lights. A cycle of groups counts as one light.
5. **Guests.** A group containing a light the request's permissions deny is
   replaced by its permitted lights. If nothing permitted is left, the answer
   is the guest-mode refusal; if only some were dropped, the command runs and
   the answer says it did part of it.
6. **The fan-out gate** counts the bulbs a write reaches, not the ids written:
   a room with one group of 20 bulbs counts 20, so it needs the confirmation
   or the "all" wording that 20 individual lights would, and can exceed
   `HA_WRITE_FANOUT_HARD_LIMIT`.

Notes. Group membership and names come from Home Assistant's state list,
cached for five minutes, so a bulb added to a group or a renamed light takes
effect within that window. A nested group that the state list doesn't include
(stale, or hidden from a non-admin token) can't be expanded: its members
aren't visible, so it is permission-checked by its own id only; restrict such
ids directly in `restricted_entities` if needed. Status answers ("are the
kitchen lights on") read light state outside the permission guard and may name
a light a guest couldn't control. The gateway's simple-command fast path builds
a single entity name itself and doesn't use any of this. To debug a room, read
the `room_lights_resolved` and `light_targets_finalized` log events (counts and
tiers only).

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
HMAC fingerprint over an identity plus the first user message (never system
content or later turns), so it stays stable across Home Assistant's
full-history replay on every turn. The HMAC secret is `SERVICE_API_KEY`
(see [Security Settings](#security-settings)); outside `DEV_MODE`, an empty
or placeholder `SERVICE_API_KEY` is fatal at orchestrator startup.

**Sessions: identity precedence.** The identity mixed into the fingerprint is
chosen in this order: (1) the top-level `user` field, if non-empty — room
never enters the key when `user` is present, so a satellite room-detection
flap between turns can't fragment one conversation; (2) otherwise the
detected room, but only if it's a real one (not empty or `"unknown"`); (3)
otherwise no identity at all (a room-less key). **Caveat**: a client that
sends the same `user` value for every conversation (some generic OpenAI-
client setups do this) merges every conversation sharing an opening message
into one session. Send a genuinely per-conversation `user` — or an explicit
`session_id`/`extra_body.session_id` matching `^explicit-[A-Za-z0-9._:-]{1,55}$`
— to avoid this.

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_MAX_COUNT` | `5000` | Cap on concurrent per-conversation sessions (in-memory fallback dict + Redis creation-time index). The oldest session (by last activity / creation time) is evicted once exceeded. |
| `NEW_CONVERSATION_PER_MINUTE_PER_IP` | `120` | Gateway-side sliding-window limit on *new* conversations (first-turn requests with no explicit `session_id`) per rate-limit key (see `TRUSTED_PROXY_CIDRS`), applied to both `/v1/chat/completions` and `/v1/responses`. Raised from an earlier default of 30 — behind a reverse proxy every caller can share one resolved key, making a low per-source limit a whole-house limit. |
| `TRUSTED_PROXY_CIDRS` | *(empty)* | Comma-separated CIDRs/hosts. The new-conversation limiter trusts `X-Forwarded-For`'s original-client address only when the immediate TCP peer (your reverse proxy) falls inside one of these ranges; an untrusted caller can't spoof another source's key via that header. Empty (the default) means every caller's TCP peer address is used directly — correct with no reverse proxy in front of the gateway, but a shared rate-limit bucket for everyone behind one (the gateway logs `trusted_proxy_cidrs_unset` once at startup as a nudge to set this). The header is parsed right-to-left, returning the nearest hop not in this CIDR set (falling back to the TCP peer if every hop is trusted), so a trusted proxy that appends rather than overwrites `X-Forwarded-For` doesn't let an upstream caller forge the left-most value. Example for a flannel/kubeadm-default cluster's pod CIDR: `10.244.0.0/16`. Keep this list scoped to your actual reverse-proxy subnet, not a broad cluster-wide default. |
| `NEW_CONVERSATION_RESET_GRACE_SECONDS` | `120` | A first-turn fingerprint reset is skipped when a session under the same fingerprint was created within this many seconds — protects against Home Assistant's truncated-ASR retry path, which resends the same single-user-message opener for the same turn. |

The new-conversation limiter's counters are backed by Redis (`REDIS_URL`)
when a Redis connection succeeds at gateway startup, so multiple gateway
replicas share one budget per key; it falls back to an in-memory,
single-replica-only counter otherwise (logged at startup either way).

### Orchestrator Ingress Authentication (ATHENA-89)

The orchestrator's query routes (`/query`, `/query/stream`, `/query/stream/v2`,
`/v1/chat/completions`), its four session routes (`GET /sessions`, `GET`/`DELETE
/sessions/{session_id}`, `GET /sessions/{session_id}/export`), `GET
/session/{session_id}/warmup`, and four `/admin/*` maintenance routes (13
routes total) require an `X-Service-Key` header matching `SERVICE_API_KEY`.
`/v1/models` stays ungated (read-only model metadata).

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCHESTRATOR_INGRESS_AUTH` | `enforce` | `enforce` rejects a missing or wrong `X-Service-Key` on the gated routes with 401. `warn` logs `orchestrator_unauthenticated_request` (with `path`, `client_host`, `user_agent`) and allows the request through. Any other value behaves as `enforce` and logs one ERROR (`orchestrator_ingress_auth_invalid_mode`) per process. A header that is present, non-empty, and **wrong** is always rejected with 401, in every mode, including `DEV_MODE` and `warn` — only a missing header is affected by the mode. |

**Rollout recipe**: set `ORCHESTRATOR_INGRESS_AUTH=warn` first if you have callers you haven't audited (a custom Home Assistant integration, a script that calls `/query` directly). Watch for `orchestrator_unauthenticated_request` log lines over a representative window — each one names the unauthenticated caller's path and user agent. Once nothing unexpected shows up, switch to `enforce` (the default). Every in-repo caller (the gateway's orchestrator client, LiveKit integration, the Wyoming bridge, jarvis-web backend, admin-backend's SMS webhook) already sends the header.

**`GET /api/base-knowledge/public` (admin-backend, D44)** requires the same `X-Service-Key` header matching `SERVICE_API_KEY`, or an authenticated admin session (Bearer JWT / `X-API-Key`) — no unauthenticated read of this table (it can hold a home address under `category='property'`). Both in-repo callers already send the header: `shared.admin_config.AdminConfigClient.get_base_knowledge` (used by the orchestrator) and `src/rag/directions/main.py`'s startup fetch. This is unrelated to `ORCHESTRATOR_INGRESS_AUTH` above (a different service, a different dependency — `verify_service_or_oidc`), but reuses the same `SERVICE_API_KEY` secret. An empty `SERVICE_API_KEY` logs `service_api_key_empty` once at startup, and base knowledge then silently drops out of every prompt that reads it (the `/public` call itself gets 401, not 503).

### Admin API authentication

Every admin-backend route requires a credential, except the 13 public routes
listed below; `admin/backend/tests/test_route_auth_population.py` (the
`admin-backend-auth` CI job) fails when a new route has neither a credential
check nor an entry on that list. A path containing `/public` or `/internal`
is a historical name, not a statement about access: those routes need a
credential like any other. Three kinds of route:

- **Signed-in user.** A Bearer session token or a user API key (`X-API-Key`)
  for a user holding the route's permission (`read`/`write`: owner and
  operator; `delete`: owner). An `X-Service-Key` sent to one of these routes
  is refused. Examples: `/api/guests*`, `/api/user-sessions` (except the
  device lookup), `/api/room-groups/available-rooms`, the LLM-memory,
  tool-proposal and Ollama-URL settings, `/api/debug-logs/*`,
  `POST /api/ha-pipelines/mode/set`, `/api/pipeline-events` (reads), and the
  voice-automation hard delete (`DELETE /api/voice-automations/{id}`, owner).
- **Service key or signed-in user.** `X-Service-Key: <SERVICE_API_KEY>` (the
  orchestrator and other services), or a signed-in user as above. A wrong key
  is 401; a key sent while `SERVICE_API_KEY` is unset is 503. Examples:
  `GET /api/user-sessions/device/{id}`, `GET /api/room-groups` and
  `/resolve/{term}`, the house-layout and directions-origin-placeholder
  settings, `GET /api/settings/ollama-url/internal`,
  `GET /api/llm-backends/model/{name}`,
  `GET /api/sms/internal/current-preferences`, `POST /api/pipeline-events/emit`,
  `/api/internal/emerging-intents*`, `/api/internal/intent-metrics`, and
  every `/api/voice-automations` route except the hard delete.
- **Service key only.** `POST /api/sms/internal/log-send`, and the Control
  Agent's download-progress callback,
  `POST /api/model-downloads/internal/{download_id}/progress`. A request with
  no `X-Service-Key` header gets 422 there; a wrong key, 401.

The configuration routes the services read, and the admin UI's read routes
beside them, by family. Permission is `read` for a GET and `write` for
anything else unless noted. Routes under the same prefixes that aren't
listed were already gated and are unchanged.

| Prefix | Service key or signed-in user | Signed-in user only |
|--------|-------------------------------|---------------------|
| `/api/alerts/public` | `POST /create`, `POST /resolve-by-entity` | `GET /active-by-type` (`read:alerts`, so viewer and support roles keep it) |
| `/api/cloud-llm-usage` | `POST` (log a cloud call's cost) | every GET: `/recent`, `/alerts`, `/summary/*`, `/analytics/*` |
| `/api/cloud-providers` | `GET /pricing/{provider}/{model_id}` | the list, `/{provider}`, `/{provider}/health`, `/health/all`, `/pricing/{provider}` |
| `/api/component-models` | `/public`, `/component/{component_name}` | |
| `/api/directions-settings`, `/api/features`, `/api/gateway-config` | `/public` on each | |
| `/api/escalation` | `/presets/public`, `/presets/active/public`, `/state/{session_id}/public`, `POST /state/internal`, `POST /events/internal`, `PUT /state/{session_id}/decrement` | `GET /metrics/prometheus` |
| `/api/follow-me` | `/internal/config` | |
| `/api/ha-pipelines` | | `/pipelines`, `/pipelines/preferred`, `/modes`, `/health` |
| `/api/intent-routing` | `/routing/public`, `/providers/public`, `/strategy/configs/public`, `/strategy/configs/{intent_name}` | |
| `/api/llm-backends` | `GET /public`, `POST /metrics` | `GET /public/mlx-applicability` |
| `/api/mcp-security` | `GET /public`, `POST /check-domain` (`read`: it changes nothing) | |
| `/api/model-configs` | `/public`, `/public/{model_name}` | `/presets` |
| `/api/modules` | | all six: the four reads, `POST /refresh-all`, `POST /{module_id}/refresh` |
| `/api/music-config` | `/internal` | `/browser-playback` |
| `/api/presets` | `/public/active` | |
| `/api/rag-service-bypass` | | the list, `/{service_name}` |
| `/api/room-audio` | `/internal`, `/internal/{room_name}` | |
| `/api/room-tv` | `/internal`, `/internal/{room_name}`, `/apps`, `/features` | |
| `/api/service-registry/services/{service_name}` | `/url` | the row itself |
| `/api/tool-calling` | `/settings/public`, `/triggers/public`, `/tools/stats/public`, `/tools/{tool_id}/api-keys/public`, `/tools/by-name/{tool_name}/api-keys/public` | |
| `/api/tool-proposals` | `POST` (create) | `GET` list, `/stats/summary`, `/{proposal_id}` |
| `/api/voice-config` | `/internal/stt`, `/internal/tts`, `/internal/all`, `/health` | `/running-config`, `/services`, `/services/{service_type}`, `/stt/models`, `/stt/active`, `/tts/voices`, `/tts/active` |
| `/api/voice-interfaces` | `/public`, `/public/{interface_name}`, `/internal/config/{interface_name}`, `/engines/public/stt`, `/engines/public/tts` | |

The exact guard and permission of each route is pinned in
`REVIEWED_BY_FILE` in `admin/backend/tests/test_route_auth_population.py`.

How a route answers, by credential:

| Request carries | Service key or signed-in user | Signed-in user only | Service key only |
|-----------------|-------------------------------|---------------------|------------------|
| nothing | 401 | 401 | 422 |
| the service key | allowed | 401 | allowed |
| a wrong service key (with or without a valid user credential) | 401 | 401 | 401 |
| any service key while `SERVICE_API_KEY` is unset on admin-backend | 503 | 401 | 503 |
| an owner or operator credential | allowed | allowed | 422 |
| a viewer or support credential, on a `read` or `write` route | 403 | 403 | 422 |
| a valid user credential plus an empty `X-Service-Key` header | allowed, as that user | 401 | 401 |

An empty `X-Service-Key` header counts as no key on a route that accepts
either credential (callers and proxies send one when no key is set, and it
carries no authority); a signed-in-user route refuses any `X-Service-Key`
header it's sent. A key that isn't plain ASCII is a wrong key (401).

**Calling a route from a script, a scraper or a dashboard.** Authenticate as
a user: a Bearer session token, or a user API key in `X-API-Key` (created
on the admin UI's User API Keys page), for an owner or operator. A user API
key acts with its user's role. Don't give an
external consumer the service key: it's the services' shared secret, it reads
guest data (below), and the signed-in-user routes refuse it. This applies to
a Prometheus scrape of `/api/escalation/metrics/prometheus` too.

```bash
curl -H "X-API-Key: $ATHENA_USER_API_KEY" http://your-admin-host:8080/api/modules/
```

**The service key's format.** `SERVICE_API_KEY` must be visible ASCII
(`!` to `~`): no space, tab, newline or non-ASCII character. A Secret
created from a file often carries a trailing newline. The callers of the
routes in the table above (`service_key_headers()` in
`src/shared/service_key.py`, `AdminConfigClient`, jarvis-web and the Control
Agent's progress callback) send no `X-Service-Key` at all for a key that
fails this, log `service_api_key_unusable` once with the variable's name
(never the value), and are then refused as anonymous. Older call sites still
hand the key to the HTTP client, which rejects it with an error, so fix the
key rather than rely on the check.

**HTTPS to the admin API.** A call that carries the service key to
admin-backend verifies the server's certificate and doesn't follow
redirects. An in-cluster `http://` admin URL is unaffected. If the admin URL
is `https://` and its certificate comes from a private CA, set
`SSL_CERT_FILE` (or `SSL_CERT_DIR`) on each calling service to a **combined**
bundle: the public roots plus your CA. That variable replaces the trust
store for every outbound HTTPS call in that process, so a bundle holding
only your CA breaks the service's calls to public APIs. A certificate
failure is a connection error, not a refusal: nothing is logged on
admin-backend, and the caller logs its usual fetch-failure line (table
below) and runs on defaults.

**The service key can read guest data.** Current guests' names and phone
numbers, device-to-guest sessions, SMS preferences and voice automations are
all readable with `SERVICE_API_KEY`. Treat it as a guest-data secret: keep it
in a Secret, never in a ConfigMap or an image, and rotate it if it leaks.

**Voice automations are scoped to the caller's stay.** A service call to a
voice-automation route must say whose automations it's acting for:
`X-Athena-Caller-Mode: owner`, or `X-Athena-Caller-Mode: guest` plus
`X-Athena-Guest-Name` (the guest's name, percent-encoded UTF-8) and
`X-Athena-Guest-Stay` (the stay's calendar event id). Anything else is 400.
Guest names aren't unique (every Airbnb booking is "Airbnb Guest"), so a guest
scope lists, archives, restores, creates and reads only the guest automations
of its own stay; any other row is reported as not found. An automation created
before stays were recorded has no stay and is never visible to a guest. A
guest scope named after a calendar-feed placeholder (`Airbnb Guest`, `VRBO
Guest`, `Guest`) is refused with 403, and the name-based bulk routes
(`/archive-guest`, `/restore-guest`) are owner-only. Signed-in users (the
admin UI) always act as the owner.

Where the stay id comes from: the SMS webhook sends the matched booking's id;
jarvis-web's guest network sends the current stay's id (the one
`/api/guest-mode/internal/current-guest` returns); the orchestrator keeps it
only in a named guest house, alongside the guest's name, and drops it
wherever it drops the name.

**Voice "delete"** archives the automation in Athena; the Home Assistant
automation keeps running until the host turns it off there, and the assistant
says so.

**Public routes** (13) and their preconditions: `GET /health` (liveness
only); the sign-in routes `GET /api/auth/login`, `/api/auth/callback` and
`/api/auth/logout` with their `/auth/login`, `/auth/callback` and
`/auth/logout` aliases, plus `GET /api/auth/methods` and
`GET /api/auth/session-token`, where login mints a token only in demo mode,
which production startup refuses; `POST /api/auth/local-login` (rate limit,
lockout and timing floor); `GET /api/calendar-sources/types` (static); and
`GET /api/settings/assistant-profile/public` and
`GET /api/settings/privacy/public` (persona and one boolean). Nothing else
answers without a credential. Restricting `/api/` at your ingress for
networks guests use is still worthwhile as a second layer.

**Refused requests: admin-backend's log.** admin-backend writes one WARNING,
`admin_auth_rejected`, when it refuses a request for its credential: a 401
or 403 from a credential or permission guard, a 503 answered to a request
that carried `X-Service-Key` while no key is configured, or the 422 a
service-key-only route gives a request with no key. Fields:

| Field | Value |
|-------|-------|
| `route` | the route template (`/api/escalation/state/{session_id}/public`), never the concrete path; `<unmatched>` when no route matched |
| `method` | the HTTP method, or `OTHER` |
| `status` | 401, 403, 503 or 422 |
| `reason` | `no_credential`, `service_key_refused`, `user_credential_refused`, `insufficient_permission` (a signed-in user without the route's permission) or `service_key_unconfigured` |
| `credential_presented` | `service_key`, `user` or `none`: which headers the request carried, not who sent it |
| `suppressed` | how many more refusals with the same route and reason were not logged since the previous line |

The line carries no path value, query string, header or key value, or client
address. It's limited to one line per route template and reason per minute,
per admin-backend replica (two replicas can each write one); refusals inside
the minute are counted and reported as `suppressed` on the next line for
that route and reason, or when admin-backend shuts down. Repeating the same
anonymous probe within a minute therefore produces no second line: read
`suppressed`.

What the line does and doesn't tell you:

- `credential_presented="service_key"` means "check the services' own logs".
  Anyone who can reach admin-backend can send a junk `X-Service-Key`, so the
  line alone doesn't prove one of your services is misconfigured.
- `credential_presented="none"` on a route none of your services calls
  anonymously is an outside consumer (a script, a scraper, a dashboard) that
  needs a user credential.
- A failed password login (`POST /api/auth/local-login`) is not logged here;
  that route has its own lockout and failure handling. A refused WebSocket
  ticket isn't either.
- It covers refusals made by the credential and permission guards. It is not
  a record of every 403: a handler that refuses a request after a guard
  accepted it (an owner-only action, for example) is generally not reported.
- A 503 is reported, as `service_key_unconfigured`, only when the request
  carried `X-Service-Key` and no guard accepted it: that is the answer a
  guard gives a key while admin-backend has none configured. A 503 a route
  answers for its own reasons to a caller whose credential was accepted (a
  disabled service, for example) is not reported. On a route with no guard
  at all, a 503 to a request that happens to carry the header is still
  reported under that reason.
- The list of service-key-only routes (for the 422 case) is built the first
  time one is needed, not at startup. If that fails, admin-backend logs one
  ERROR, `admin_auth_rejection_route_walk_failed`, with the error's type,
  keeps serving, and stops reporting the 422 case until it restarts; every
  other refusal is still logged.

**Refused requests: the calling service's log.** A service whose call to
admin-backend is answered 401, 403 or 503 writes one ERROR,
`admin_backend_refused`, with `status` and `route` (the route template, no
query). It makes the one request, doesn't retry, falls back to its default
for that setting, and logs at most one line per route and status per minute.
Startup and readiness probes are unaffected. This line is the authority for
"one of my services is being refused": it means that service's
`SERVICE_API_KEY` is missing, malformed or different from admin-backend's.
Two cases aren't credential problems: a 503 through an ingress or load
balancer while admin-backend restarts, and, during an upgrade, a 401 for
`/api/room-tv/apps` or `/api/room-tv/features` from an upgraded orchestrator
against an admin-backend that hasn't been upgraded yet.

A connection or certificate failure is not a refusal and produces neither
line. After upgrading a service, and before upgrading admin-backend, search
its log for the events a failed connection produces:

| Service | Event | Lost until fixed |
|---------|-------|------------------|
| orchestrator | `tv_configs_fetch_failed`, `app_configs_fetch_failed`, `feature_flags_fetch_failed` | the admin-configured TV rooms (a built-in fallback is used), TV app list, TV feature flags |
| orchestrator | `room_configs_fetch_failed` | the admin-configured room speakers (a built-in fallback is used) |
| gateway | `livekit_credentials_fetch_error` | LiveKit credentials |
| gateway | `feature_flag_check_error`, `wyoming_feature_flag_check_error` | the flag keeps its previous value |
| jarvis-web | `failed_to_fetch_guest`, `failed_to_fetch_tv_configs` | current-guest lookup, TV room list |
| site-scraper RAG | `config_load_failed_using_defaults` | admin-configured allow and block lists |

On the orchestrator, `feature_flags_loaded_from_db` after a restart is the
positive sign that admin calls work.

**Control Agent download-progress callback.** When a model download starts,
admin-backend gives the Control Agent a callback URL and the agent posts
progress to it with `X-Service-Key`.

| Variable | Read by | Default | Description |
|----------|---------|---------|-------------|
| `CONTROL_AGENT_CALLBACK_BASE_URL` | admin-backend | *(empty)* | Where the Control Agent's host reaches admin-backend: scheme, host and optional port, no path (`https://your-admin-host`). Empty: admin-backend hands out `http://localhost:8080` and logs `control_agent_callback_base_url_unset` once, which is only right when the agent runs on admin-backend's host. |
| `ALLOWED_CALLBACK_HOSTS` | Control Agent | *(empty)* | Comma-separated hostnames. Empty: the agent rejects every download request that names a callback, which admin-backend's always do (fail-closed). A callback URL whose hostname is an entry is accepted; with a non-empty list, a hostname that isn't an entry is still accepted when it resolves to a public address. The agent attaches the service key **only** when the callback's hostname is exactly an entry (hostname only, no scheme or port). |
| `SERVICE_API_KEY` | Control Agent | *(empty)* | The same key admin-backend uses; also required for the agent's inbound routes. |

So for an agent on its own host: set `CONTROL_AGENT_CALLBACK_BASE_URL` on
admin-backend, and put that URL's hostname in the agent's
`ALLOWED_CALLBACK_HOSTS`. Don't list `localhost` there unless admin-backend
runs on the agent's host: whatever listens on that host's port 8080 would
receive the key. Over plain `http://` the key crosses the network in clear
text; use `https://` between hosts. When the key is withheld the agent logs
`progress_callback_service_key_withheld` (host not on the list),
`progress_callback_service_key_unset` or `service_api_key_unusable` once,
each callback is refused (`admin_backend_refused` on the agent, at most once
a minute per status), and the download row never shows progress.

**API docs.** `/docs`, `/redoc` and `/openapi.json` are served only with
`DEV_MODE=true`.

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
| `DEFAULT_TIMEZONE` | `UTC` | The property's IANA zone (for example `America/New_York`). It's the assistant's clock: the time and date in prompts and fast-path answers, "today"/"tomorrow", year inference for spoken dates, event/sports/transit day windows, and scheduled "at 7:00" waits. The process `TZ` is never used for these, so a UTC pod still answers in local time. The images ship the zone database (`tzdata`). An empty or unknown value falls back to UTC and logs `local_timezone_invalid` once |
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
| `COMMUNITY_EVENTS_SOURCES` | *(empty)* | JSON array of community-event sources. Each entry's `type` selects the parser: `link_scan`, `event_cards`, `tribe_events_api`, `squarespace_eventlist`. `link_scan` and `event_cards` are best-effort heuristics, validated only against their reference site's HTML structure — a source's markup can drift without notice. `tribe_events_api` and `squarespace_eventlist` are schema-level: they call a documented REST API and a stable Squarespace markup convention, respectively, rather than guessing at page structure. |

**Caveats**:
- **Feed `type` vocabulary.** `search_transit`'s `transit_type` filter is a prefix match on a GTFS feed's `type` (`bus`, `metro`, `light_rail`, `rail`, `ferry`, `commuter_bus`) and on a static service's fixed `"ferry_terminal"` stop type. Use one of the tool's own enum words as the feed's `type`, not a synonym (`metro`, not `subway`) — a mismatch just means the filter never matches, not an error.
- **Departures use container-local time.** `GET /transit/departures` and `/transit/query`'s `departures`/`search`/`nearby` modes read the container's wall clock (`datetime.now()`), not the feed's own timezone.
- **`search_transit` reachability.** The orchestrator offers `search_transit` on a `directions`-intent query only when the query itself reads as transit-phrased (bus, train, stop, departure, route number, ...) — a plain driving/walking directions request never sees it.
- **`bounds` is exclusive.** A GTFS feed's optional `bounds` box (`min_lat`/`max_lat`/`min_lon`/`max_lon`) filters stops with a strict `<` comparison on every edge — a stop exactly on a boundary value is excluded, not included.
- **`allow_private` residual.** Both feed and event-source fetches go through the shared SSRF guard (`shared.url_safety.safe_get`) with, by default, no private hosts allowed on any hop. Setting `allow_private: true` on one feed/source exempts only that entry's own hostname — on every hop, not just the first. That means the named host resolving to a different private address later (DNS rebinding) or redirecting to itself at a private address are both accepted as operator trust for that one host; a redirect to any *other* private host is still blocked. This is stricter than the CONTRIBUTING.md Class 3 exemption, deliberately — these fetch third-party content, and a hijacked upstream redirect shouldn't reach cluster-internal addresses.
- **The maintainer-leak CI gate has documented non-goals** — it deliberately doesn't catch a team/place name that doesn't contain the configured home city, a bare latitude with no longitude, or a Linux `/home/<user>` path (only `/Users/…` is ruled). Run `python3 scripts/check-maintainer-leaks.py --help` for the full list before assuming an example value is automatically safe.

---

## Telemetry

admin-backend sends a small **pseudonymous** heartbeat to the Athena maintainers: a `first_boot` event once, then a `heartbeat` about once a day. It's pseudonymous, not anonymous: every install has a random installation ID, so its heartbeats can be linked to each other. Nothing in it identifies you, your household, your guests or your network.

It's **on by default**. It's separate from the Privacy **analytics mode** in the Admin UI, which records conversation turns in your own database and never leaves the install.

### Turning it off

Any one of these switches it off:

- `ATHENA_TELEMETRY=off` (also `false`, `0` or `no`; any value other than `on`, `true`, `1` or `yes` counts as off)
- `DO_NOT_TRACK=1` (any value other than `0`, `false` or `no`)
- the **Install telemetry** card under **System Configuration** in the Admin UI (owner only)

The two variables are read on every decision from **both** the process environment and the `.env` file in admin-backend's working directory, and an opt-out in either one wins: an empty value in the process environment never overrides `ATHENA_TELEMETRY=off` in `.env`. If `.env` exists but can't be read, or a line naming one of these variables isn't a plain `NAME=value` assignment (for example `ATHENA_TELEMETRY: off`), telemetry is off. An environment opt-out can't be overridden from the Admin UI.

To turn it off before upgrading, set `ATHENA_TELEMETRY=off` in admin-backend's environment (for Kubernetes, the `athena-config` ConfigMap or your private overlay) before the new version starts. The first send is never earlier than 5 minutes after admin-backend starts.

| Variable | Default | Description |
|---|---|---|
| `ATHENA_TELEMETRY` | on | `off` disables telemetry |
| `DO_NOT_TRACK` | unset | `1` disables telemetry |
| `ATHENA_TELEMETRY_ENDPOINT` | the maintainers' collector (`TELEMETRY_DEFAULT_ENDPOINT` in `src/shared/config.py`) | Where heartbeats go. Must be `https://` (or `http://` to `localhost`/a loopback address), with no credentials, query or fragment, at most 200 characters; anything else turns telemetry off. An explicitly empty value turns it off. |
| `ATHENA_TELEMETRY_MODE` | detected | Overrides the install class: `self_hosted_real`, `dev`, `test`, `ci` or `production` (`production` is reserved for the maintainers' own deployments) |

These four variables aren't `AthenaConfig` fields: they're read at call time by `read_telemetry_env()` in `src/shared/config.py`, so a change to `.env` takes effect without a restart.

### When it doesn't send

It stays silent, whatever the settings, when:

- the install class is `ci` (`CI=true`) or `test` (running under pytest) and `ATHENA_TELEMETRY_MODE` isn't set
- the database is in-memory SQLite, which includes every `DEV_MODE` instance (an identity there couldn't outlive the process)
- the endpoint is invalid or explicitly empty

The install class is otherwise `dev` for development and pre-release versions and `self_hosted_real` for a stable release.

### What's sent

The exact bytes of the last payload are shown in the Admin UI card (**Show the last payload sent**), and the full schema is in `docs/telemetry/payload-v1.schema.json`. Counts are buckets (`0`, `1`, `2-5`, `6-20`, `21-50`, `51-200`, `201-1000`, `1001+`), usage is a 30-day average in buckets, and there are no timestamps. Module and Home Assistant signals describe admin-backend's own environment, which matches the other services' when they share one ConfigMap.

| Path | Values | Meaning |
|---|---|---|
| `schema_version` | `1` | Payload version |
| `installation_id` | random UUID | The pseudonymous installation ID |
| `event` | `first_boot`, `heartbeat` | Which event this is |
| `install.version` | e.g. `0.5.0` | Athena version |
| `install.release_channel` | `stable`, `prerelease`, `dev` | From the version |
| `install.install_class` | `production`, `self_hosted_real`, `dev`, `test`, `ci` | See above |
| `install.provenance` | `new`, `upgraded` | `upgraded` when the database already had users or queries more than a day before the ID was created |
| `install.platform` | `linux`, `darwin`, `windows`, `other` | Operating system |
| `install.arch` | `x86_64`, `aarch64`, `other` | CPU architecture |
| `install.deployment` | `kubernetes`, `container`, `bare` | How admin-backend runs |
| `services.core_enabled` | count bucket | Enabled core services in the service registry |
| `services.core_healthy` | count bucket | Of those, healthy |
| `services.rag_enabled` | names of built-in RAG services | Enabled RAG services (built-in names only) |
| `services.rag_healthy` | names of built-in RAG services | Healthy RAG services (built-in names only) |
| `services.rag_custom` | count bucket | RAG services you added (never named) |
| `services.infrastructure_healthy` | `redis`, `qdrant`, `postgres`, `searxng`, `control-agent` | Healthy infrastructure services |
| `llm.components[].component` | a built-in LLM component name | e.g. `intent_classifier` |
| `llm.components[].assignment` | `configured`, `builtin_default` | Whether you assigned a model or the default is used |
| `llm.components[].family` | a public model family, or `custom` | e.g. `qwen3`, `llama3.1`, `gpt-4o`, `claude-sonnet` |
| `llm.components[].size_bucket` | `le3b`, `4-9b`, `10-20b`, `21-40b`, `41-80b`, `gt80b`, `unknown` | Parameter-count bucket |
| `llm.components[].quantized` | `true`, `false` | Whether the model is a quantized build |
| `llm.components[].source` | `ollama-library`, `hf-public-publisher`, `vendor-api`, `custom-registry`, `custom` | Where the model comes from |
| `llm.components[].backend` | `ollama`, `mlx`, `auto`, `openai`, `anthropic`, `google`, `openai_compatible` | Which backend serves it |
| `llm.components[].locality` | `local`, `cloud`, `remote`, `unknown` | Whether inference runs on your network |
| `llm.custom_components` | count bucket | Components you added (never named) |
| `llm.cloud_providers_enabled` | `openai`, `anthropic`, `google` | Enabled cloud LLM providers |
| `features.modules_enabled` | module IDs | Enabled modules (`home_assistant`, `guest_mode`, `notifications`, `monitoring`, `jarvis_web`) |
| `features.flags_enabled` | built-in feature flag names | Enabled feature flags (built-in names only) |
| `home_assistant.configured` | `true`, `false` | Whether `HA_URL` and `HA_TOKEN` are both set. Home Assistant is never contacted. |
| `voice.wyoming_devices` | count bucket | Registered Wyoming devices |
| `voice.jetson_devices` | count bucket | Registered Jetson devices |
| `voice.livekit_enabled` | `true`, `false` | LiveKit enabled |
| `voice.jarvis_web_enabled` | `true`, `false` | The Jarvis web module enabled |
| `voice.stt_engines` | built-in engine names, or `other` | Speech-to-text engines in enabled voice interfaces |
| `voice.tts_engines` | built-in engine names, or `other` | Text-to-speech engines in enabled voice interfaces |
| `guest_mode.enabled` | `true`, `false` | Guest mode enabled |
| `guest_mode.calendar_source_types` | `airbnb`, `vrbo`, `lodgify`, `generic_ical` | Types of enabled calendar sources |
| `guest_mode.legacy_ical_configured` | `true`, `false` | Whether the legacy single iCal URL is set (the URL is never sent) |
| `memory.memories` | count bucket | Stored memories |
| `memory.vector_store` | `ready`, `unavailable`, `embedder_unavailable`, `shape_mismatch`, `other` | Memory vector store state |
| `usage.queries_per_day_30d` | `0`, `<1`, `1-9`, `10-49`, `50-199`, `200+` | Average queries per day over 30 days |
| `usage.intent_mix_30d` | intent → percent in tens, or `null` | Share of queries per intent; omitted below 100 queries |
| `platform_config.auth_modes` | `oidc`, `local` | Sign-in methods in use |
| `platform_config.oidc_configured` | `true`, `false` | Whether an OIDC issuer is set |
| `platform_config.control_agent_enabled` | `true`, `false` | `CONTROL_AGENT_ENABLED` |
| `platform_config.service_control_k8s_enabled` | `true`, `false` | `SERVICE_CONTROL_K8S_ENABLED` |

**Models.** A model is described only by its family, size bucket, a quantized flag and its source; the model name, tag, repository and publisher are never sent. The family comes from a fixed list of public model families; anything else, including a model you named yourself, is reported as `custom`. `source` is `ollama-library` only for a recognized family followed by nothing but size, quantization and version tokens; `llama-my-house` is reported as family `llama` with source `custom`. The combination of these fields across components can make one install distinguishable in the maintainers' dashboard, which is why this is called pseudonymous.

**Never sent:** hostnames, URLs, IP addresses or ports; keys, tokens or passwords; usernames, email addresses, guest or household names; rooms, zones or Home Assistant entities (not even counts); calendar URLs, events or stay dates; queries or conversation text; memory content; SMS data; timezone, city or coordinates; exact counts; timestamps; error messages.

### Where it goes and what's kept

Heartbeats go to the endpoint above, a collector the Athena maintainers run on Cloudflare. Only the maintainers can see the stored data, through a private dashboard; there are no public statistics. Cloudflare sees the source IP address in order to serve the request, and a short-lived edge rate-limit counter is keyed on it; the collector stores no IP address, header or user agent.

- Per-installation daily snapshots are kept **35 days**.
- An installation's current state (its last payload) is deleted **400 days** after its last heartbeat.
- Aggregate daily counts, which carry no installation IDs, are kept 400 days.

The collector accepts at most 2,000 new installations per UTC day, so a burst of forged pings could make new installs fail for the rest of that day. Existing installations are unaffected.

### The install key

Each installation also has a random install key, stored only in its own database. It is never sent: each request carries an HMAC of it bound to the collector's address (`X-Athena-Install-Key`), and the collector stores only a hash of that. It stops someone who learns your installation ID from sending heartbeats in its name. A database copied onto a second install shares the identity, and deleting the key's settings row makes the collector reject heartbeats until the identity is reset.

### Resetting the identity

**Reset telemetry identity** in the Admin UI card deletes the installation ID, the key and all send state; the next heartbeat starts a new ID with a `first_boot` event. This isn't unlinkability: the collector keeps the old ID's rows until retention removes them, and the old and new IDs can be correlated by timing and by what they report.

---

## Home Assistant Entity Mappings

These `AthenaConfig` fields ship with NO house-specific entity IDs baked in.
Unset means the corresponding feature is disabled or falls back cleanly
rather than guessing a device/room your HA instance doesn't have. See
`.env.example` for the full JSON schema and worked examples.

| Variable | Default | Description |
|----------|---------|-------------|
| `HA_SATELLITE_ROOM_MAP` | *(empty)* | JSON object mapping a Voice PE `assist_satellite` entity ID to its room. Used both directions by the gateway: entity→room (conversation room detection) and room→entity (satellite announcements). Room detection resolves each active satellite in two steps: this map first (an entity_id match always wins when both would resolve), then a generic parse of the satellite's HA `friendly_name` against the `"Voice - <Room> Assist"` convention, then `"unknown"`. The map lets you override or correct a name that doesn't fit that convention; it isn't required for basic room detection to work. |
| `HA_TV_ENTITIES` | *(empty)* | Fallback room → Apple TV entity mapping, used only when the admin API's Room TV Config is unreachable. JSON array of `{room, media_player_entity_id, remote_entity_id}` objects, or comma-separated `room:media_player_entity_id[:remote_entity_id]` triples. |
| `HA_MUSIC_PLAYERS` | *(empty)* | Fallback room → Music Assistant `media_player` entity mapping, used only when the admin API's room audio config is unreachable. JSON object `{room: entity_id}` or comma-separated `room:entity_id` pairs. |
| `HA_BED_WARMER_ENTITIES` | *(empty)* | JSON object naming the 5 HA entities a Sunbeam-via-Tuya dual-zone bed-warmer/mattress-pad integration exposes (`level_left`, `level_right`, `power_main`, `power_side_a`, `power_side_b`). |
| `HA_ROOM_LIGHT_EXCLUDE_ENTITIES` | *(empty)* | JSON array of regexes (`re.search` on the entity id). A light picked for a room only by name is dropped when it matches. Unset uses the built-in pattern for an id with a `led_ring` or `status_led` word; `[]` turns the exclusion off; a malformed value falls back to the built-in pattern. Never applied to an `HA_LIGHT_GROUPS` entry or to a member of a picked group. See "How a room's lights are chosen". |
| `HA_LIGHT_GROUPS` | *(empty)* | JSON object mapping a room name to a light-group entity ID (`{"<room>": "<light group entity>"}`). The authoritative group for room light commands and status (keys match regardless of case, spaces or underscores), and read by `smart_home_controller.py`'s scene-activation-failed fallback (dim/turn on that room's lights when the requested scene or script doesn't exist). For the scene fallback a room with no configured group gets no fallback — turning on every light in the house when one room's group isn't configured would be a house-wide regression, not a safe default. |

### jarvis-web appliance and media entities

Plain `os.getenv` reads in `apps/jarvis-web/backend/main.py`, not `AthenaConfig`
fields — set directly on the jarvis-web Deployment/container, not via the
shared `athena-config` ConfigMap. Same contract as the table above: empty
means the endpoint reports the feature isn't configured, instead of
querying a hardcoded entity your HA instance doesn't have.

| Variable | Default | Description |
|----------|---------|-------------|
| `OVEN_ENTITY_ID` | *(empty)* | HA `water_heater` entity for the oven. |
| `FRIDGE_ENTITY_ID` | *(empty)* | HA entity for the fridge. |
| `FREEZER_ENTITY_ID` | *(empty)* | HA entity for the freezer. |
| `STOVE_COOK_MODE_SENSOR_ID` | *(empty)* | HA sensor entity reporting the stove's current cook mode. |
| `STOVE_DISPLAY_TEMP_SENSOR_ID` | *(empty)* | HA sensor entity reporting the stove's displayed temperature. |
| `STOVE_TIMER_SENSOR_ID` | *(empty)* | HA sensor entity reporting the stove's timer state. |
| `FRIDGE_DOOR_SENSOR_ID` | *(empty)* | HA binary-sensor entity reporting whether the fridge door is open. |

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
