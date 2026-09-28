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

---

## Centralized Configuration via AthenaConfig

`AthenaConfig` (`src/shared/config.py`) is the canonical pydantic-settings `BaseSettings` object for Athena. It centralizes 41 env vars, starting with 11 high-leverage vars migrated in Campaign 4 (ATHENA-7) and extended by later campaigns (ATHENA-1, ATHENA-11, ATHENA-12, ATHENA-14, ATHENA-59, ATHENA-88, ATHENA-89). The remaining env vars in the codebase continue to use direct `os.getenv` and are migrated per-PR — see `CONTRIBUTING.md` for the extension pattern.

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
| `MODE_SERVICE_URL` | `http://localhost:8022` | Mode service (ATHENA-69). **Required** on both the orchestrator and the gateway — without it, mode/permission resolution degrades every request (orchestrator: `get_current_mode`'s outage fallback; gateway: `mode_gate.py`'s fast-path check always returns `False`). See "Mode and permissions" under Module Settings below. |
| `NOTIFICATIONS_SERVICE_URL` | `http://localhost:8050` | Notifications service |
| `JARVIS_WEB_URL` | `http://localhost:3001` | Jarvis Web UI |
| `CONTROL_AGENT_URL` | `http://localhost:8099` | Service management API |
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
| jarvis-web, signed-in owner/operator | server-resolved via step 1-3 above | `web_authenticated` |
| jarvis-web, unauthenticated | forced `guest` (or `JARVIS_PUBLIC_MODE=household`'s legacy behavior) | `web_public` |

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
utterance ("switch to owner mode, pin 123456"); `web_public` and untagged
callers are refused before any throttle or mode-service call, with zero
counter increments. A surface may only ever *say* "owner mode" in a
response if the request actually resolved to owner via the table above —
narration never leads permission.

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

**jarvis-web (`JARVIS_PUBLIC_MODE`).** See [Service URLs](#service-urls)
above and `manifests/athena-prod/jarvis-web.yaml`'s inline comment. Default
`guest`: an unauthenticated caller is a guest and every owner_only write
route (climate, media, Apple TV, appliances, music playback, LiveKit room
management, mode changes — 29 routes total, HTTP and the two WebSocket
proxies) answers `403 {"detail":"sign_in_required"}` (or closes the
WebSocket upgrade with code 1008 before accepting it). `household` is the
legacy pre-ATHENA-69 behavior for a LAN-only deployment: every caller gets
the household's actual mode and the write-route gate is bypassed.

**Reads are never gated.** Only *writes* go through the permission guard
and the jarvis-web sign-in gate. `GET` routes — climate/media/appliance/
Apple TV/sensor state, `/api/mode`, `/livekit/config`, music config — stay
open to every caller, authenticated or not. This matters beyond jarvis-web
itself: the **orchestrator directly reads several jarvis-web GET routes**
for voice answers (`smart_home_controller.py`, hardcoded to jarvis-web's
in-cluster/co-located URL) — `GET /api/appliances/oven`,
`GET /api/appliances/fridge`, `GET /api/sensors/motion`,
`GET /api/sensors/illuminance`, `GET /api/sensors/summary`, and
`GET /api/media` — none of which are gated, so this integration keeps
working unchanged regardless of `JARVIS_PUBLIC_MODE` or who's signed in.
(`GET /api/mode` is the one partial exception: it suppresses the guest's
name from its response for a `web_public` caller, but still returns
`mode`/`has_guest`/etc. — see D31.)

**Denial observability.** Every guard-refused Home Assistant write
increments the `athena_ha_write_denied_total{domain, scope_mode}` Prometheus
counter, regardless of which entry point or node produced it — alert on a
sustained rise if you want to notice a caller hammering a write it doesn't
have.

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
| `COMMUNITY_EVENTS_SOURCES` | *(empty)* | JSON array of community-event sources. Each entry's `type` selects the parser: `link_scan`, `event_cards`, `tribe_events_api`, `squarespace_eventlist`. `link_scan` and `event_cards` are best-effort heuristics, validated only against their reference site's HTML structure — a source's markup can drift without notice. `tribe_events_api` and `squarespace_eventlist` are schema-level: they call a documented REST API and a stable Squarespace markup convention, respectively, rather than guessing at page structure. |

**Caveats**:
- **Feed `type` vocabulary.** `search_transit`'s `transit_type` filter is a prefix match on a GTFS feed's `type` (`bus`, `metro`, `light_rail`, `rail`, `ferry`, `commuter_bus`) and on a static service's fixed `"ferry_terminal"` stop type. Use one of the tool's own enum words as the feed's `type`, not a synonym (`metro`, not `subway`) — a mismatch just means the filter never matches, not an error.
- **Departures use container-local time.** `GET /transit/departures` and `/transit/query`'s `departures`/`search`/`nearby` modes read the container's wall clock (`datetime.now()`), not the feed's own timezone.
- **`search_transit` reachability.** The orchestrator offers `search_transit` on a `directions`-intent query only when the query itself reads as transit-phrased (bus, train, stop, departure, route number, ...) — a plain driving/walking directions request never sees it.
- **`bounds` is exclusive.** A GTFS feed's optional `bounds` box (`min_lat`/`max_lat`/`min_lon`/`max_lon`) filters stops with a strict `<` comparison on every edge — a stop exactly on a boundary value is excluded, not included.
- **`allow_private` residual.** Both feed and event-source fetches go through the shared SSRF guard (`shared.url_safety.safe_get`) with, by default, no private hosts allowed on any hop. Setting `allow_private: true` on one feed/source exempts only that entry's own hostname — on every hop, not just the first. That means the named host resolving to a different private address later (DNS rebinding) or redirecting to itself at a private address are both accepted as operator trust for that one host; a redirect to any *other* private host is still blocked. This is stricter than the CONTRIBUTING.md Class 3 exemption, deliberately — these fetch third-party content, and a hijacked upstream redirect shouldn't reach cluster-internal addresses.
- **The maintainer-leak CI gate has documented non-goals** — it deliberately doesn't catch a team/place name that doesn't contain the configured home city, a bare latitude with no longitude, or a Linux `/home/<user>` path (only `/Users/…` is ruled). Run `python3 scripts/check-maintainer-leaks.py --help` for the full list before assuming an example value is automatically safe.

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
| `HA_LIGHT_GROUPS` | *(empty)* | JSON object mapping a room name to a light-group entity ID (`{"<room>": "<light group entity>"}`), read by `smart_home_controller.py`'s scene-activation-failed fallback (dim/turn on that room's lights when the requested scene or script doesn't exist). A room with no configured group gets no fallback — turning on every light in the house when one room's group isn't configured would be a house-wide regression, not a safe default. |

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
