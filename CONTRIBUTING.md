# Contributing to Project Athena

Thank you for your interest in contributing to Project Athena! This document provides guidelines for contributing to the project.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment for everyone.

## How to Contribute

### Reporting Issues

1. Check if the issue already exists in the [Issues](https://github.com/jstuart0/project-athena-oss/issues) tab
2. If not, create a new issue with:
   - A clear, descriptive title
   - Steps to reproduce (if applicable)
   - Expected vs actual behavior
   - Environment details (OS, Python version, etc.)

### Submitting Pull Requests

1. **Fork the repository**
   ```bash
   # Clone your fork
   git clone https://github.com/YOUR_USERNAME/project-athena-oss.git
   cd project-athena-oss

   # Add upstream remote
   git remote add upstream https://github.com/jstuart0/project-athena-oss.git
   ```

2. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes**
   - Follow the existing code style
   - Add tests if applicable
   - Update documentation as needed

4. **Commit your changes**
   ```bash
   git commit -m "Add brief description of changes"
   ```

5. **Keep your branch up to date**
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

6. **Push and create a PR**
   ```bash
   git push origin feature/your-feature-name
   ```
   Then open a Pull Request on GitHub.

### Pull Request Guidelines

- Provide a clear description of the changes
- Reference any related issues
- Ensure all tests pass
- Keep changes focused and atomic
- Be responsive to feedback

### Adding or modifying a RAG service

Before opening a PR that touches `src/rag/<service>/` or `src/shared/`:

1. Add every top-level import's package to `src/rag/<service>/requirements.in`, then run `make lock` to recompile `src/rag/<service>/requirements.txt` (a generated, hashed lock — do not hand-edit it). If `main.py` does `import feedparser`, feedparser must be in the `.in` file.
2. If `main.py` does `from foo.bar import X`, the Dockerfile must `COPY rag/<service>/foo /app/foo` so `foo` lands at `/app/foo` (the `WORKDIR`). Use `SERVICE_EXTRA_COPIES` in `scripts/generate-rag-dockerfiles.py` to codify this.
3. Do not import from `orchestrator/`, `gateway/`, or any non-RAG service module. If you need shared logic, move it to `src/shared/` first.
4. Do not read `REDIS_HOST` or `REDIS_PORT` directly — kubelet auto-injects `REDIS_PORT=tcp://...` for any K8s Service named `redis`. Use `REDIS_URL` via `get_config().redis_url`, or a service-specific `<SERVICE>_REDIS_URL` env var with the DB index in the URL path.
5. Add the service to `RAG_SERVICES` in `scripts/service-defs.sh` (sourced by `build-and-push.sh`, `smoke-rag-images.sh`, and `smoke-images.sh`).
6. Run `make smoke-rags SERVICE=<image-name>` locally before opening a PR (e.g. `make smoke-rags SERVICE=athena-rag-sports`). CI enforces this on every PR touching `src/rag/**` or `src/shared/**`.

### Adding or editing `admin/frontend/` code that renders untrusted data

Read `admin/frontend/README.md` first — it covers the two escaping primitives, the six contexts `escapeJsAttr` is wrong for, and the "add a new frontend file" checklist. `.github/workflows/frontend-escaping.yml` enforces zero wrong-primitive/unescaped handler sites, a single `escapeHtml`/`escapeJsAttr` definition, and load-order/hardening on every PR touching `admin/frontend/**`.

### OSS-First enforcement: no maintainer-identifying values in the tracked tree

Before opening a PR, run `python3 scripts/check-maintainer-leaks.py` (add `--paths <changed files>` to scope it to your diff). It scans for home-lab LAN IPs, a home domain, a home city, home-directory paths, a legacy namespace FQDN, home coordinates, a maintainer's name, and a maintainer's host names — FAIL-class outside docs/tests/examples, WARN-class inside them. `.github/workflows/maintainer-leaks.yml` runs the same scan on every PR and push to `main`.

If a hit is a genuine false positive (a national reference table that happens to include one matching city, a scrubber migration, etc.), add a full-line entry to `scripts/.maintainer-leak-allowlist`: `path-glob<TAB>rule-id|*<TAB>exact-source-line<TAB>reason`. The substring column must be the exact, stripped source line the entry allows, not an arbitrary fragment — one entry can't accidentally cover an unrelated line that shares a shorter substring. An allowlist entry is itself flagged stale (and fails the gate) once the line it names no longer matches anything.

Never commit a genuinely private pattern (a real IP, a private domain, a home address) to the tracked allowlist or rule set. Use `--extra-patterns FILE` (or `MAINTAINER_LEAK_EXTRA_PATTERNS`) with a private `id<TAB>regex` file that lives outside this repository or is gitignored — see `python3 scripts/check-maintainer-leaks.py --help` for the exact format and the rules the tracked gate deliberately doesn't cover.

## Development Setup

1. **Clone and setup**
   ```bash
   git clone https://github.com/jstuart0/project-athena-oss.git
   cd project-athena-oss
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Run tests**

   The test suite uses `pytest.ini` to register an `integration` marker. By default, running `pytest` executes only unit tests (tests not marked `integration`):

   ```bash
   # Unit tests only (default — no live services required)
   pytest tests/

   # Include integration tests (require live PostgreSQL, Redis, and running services)
   pytest -m integration tests/
   ```

   Mark tests that require live services with `@pytest.mark.integration`. New tests should be unit tests unless they genuinely require external state.

   **Test-only dependencies:** a genuinely test-only dependency belongs in `admin/backend/requirements-test.in` (compiled to `requirements-test.txt` via `make lock`, constrained against the production lock so it can never silently diverge from it) — not the production `admin/backend/requirements.in`. The one existing exception is `pytest-httpserver>=1.0.8`, which predates `requirements-test.in` and still lives in `admin/backend/requirements.in`, annotated `# test-only`. It is used by the OIDC validation tests (`admin/backend/tests/test_oidc_validation.py`) to stand up a minimal fixture issuer that serves `/.well-known/openid-configuration` and a JWKS endpoint, allowing tests to drive authlib's real validator without mocking it. This package ships in the production image as a known, grandfathered trade-off — moving it to `requirements-test.in` is deferred to a future campaign (tracked as HIGH-E in `thoughts/shared/plans/active-2026-05-06-deliver-security-hardening.md`).

## Code Style

- Use Python 3.11+ features
- Follow PEP 8 guidelines
- Use type hints where practical
- Keep functions focused and well-documented

## Configuration Guidelines

When contributing code that requires configuration:

- **Never hardcode** IP addresses, hostnames, passwords, or API keys.
- For configuration values modeled in `src/shared/config.py::AthenaConfig`, read them via `from shared.config import get_config; cfg = get_config(); cfg.field_name`.  See the module for the current field list.
- For configuration values not yet in `AthenaConfig`, you have two options:
  1. **(Preferred)** Add a new field to `AthenaConfig` in `src/shared/config.py`, document it in `.env.example`, and read via `get_config().field_name`.  This is how the OSS-First convention extends — every new env var lands in the central object.
  2. Read directly via `os.getenv("VAR_NAME", default)` if the variable is local to a single module and unlikely to be needed elsewhere.  Document in `.env.example`.  Be aware that any other module needing the same value will end up duplicating the resolution logic — prefer option 1 for cross-cutting values.
- Add every new environment variable to `.env.example` with a clear inline comment describing its purpose and an example value.
- Use sensible defaults that work for local development; emit a log warning when a critical variable is missing rather than failing silently or falling back to a hardcoded value.

### Adding a new field to `AthenaConfig`

```python
# In src/shared/config.py:
class AthenaConfig(BaseSettings):
    # ... existing fields ...
    my_new_var: str = Field(default="")  # set MY_NEW_VAR in env
```

Then add a unit test in `tests/unit/test_config.py` mirroring the existing field-coverage tests (use `tests/unit/test_admin_url.py` as the template — it shows the autouse `_clear_cache_for_tests()` fixture pattern), and document the variable in `.env.example`.

### Current `AthenaConfig` fields

51 fields, plus one computed property (`llm_endpoint`). This table is meant
to stay complete — when you add a field, add its row here too.

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `ollama_url` | `OLLAMA_URL` | `http://localhost:11434` | LLM inference endpoint |
| `llm_service_url` | `LLM_SERVICE_URL` | `""` | Overrides `ollama_url` when set |
| `llm_endpoint` | _(computed)_ | falls back to `ollama_url` | `llm_service_url` wins when non-empty |
| `redis_url` | `REDIS_URL` | `redis://redis:6379/0` | In-cluster DNS default |
| `database_url` | `DATABASE_URL` | `""` | PostgreSQL connection string |
| `service_api_key` | `SERVICE_API_KEY` | `""` | Service-to-service auth key; HMAC secret for OpenAI-compatible sessions and the orchestrator's ingress-auth check |
| `default_timezone` | `DEFAULT_TIMEZONE` | `UTC` | |
| `default_city` | `DEFAULT_CITY` | `""` | |
| `oidc_issuer` | `OIDC_ISSUER` | `""` | Whitespace stripped |
| `oidc_client_id` | `OIDC_CLIENT_ID` | `""` | Whitespace stripped |
| `oidc_validate_iss` | `OIDC_VALIDATE_ISS` | `true` | Set `false` only if your IdP deliberately returns a mismatched issuer URL |
| `dev_mode` | `DEV_MODE` | `false` | |
| `demo_mode` | `DEMO_MODE` | `false` | |
| `control_agent_enabled` | `CONTROL_AGENT_ENABLED` | `false` | Opt-in; set `true` only if a Control Agent runs on a host alongside Ollama. Valid values: `true`/`false`/`1`/`0`. Do not set to a blank string. |
| `service_control_k8s_enabled` | `SERVICE_CONTROL_K8S_ENABLED` | `false` | Opt-in (ATHENA-118); requires `optional/admin-backend-rbac.yaml` and the automount patch applied first, or the Kubernetes manager stays unavailable. See `docs/CONFIGURATION.md` § Service Control on Kubernetes. |
| `login_rate_limit_per_minute` | `LOGIN_RATE_LIMIT_PER_MINUTE` | `5` | Max `POST /local-login` attempts per IP per 60s |
| `login_lockout_threshold` | `LOGIN_LOCKOUT_THRESHOLD` | `10` | Cumulative failures before an account locks |
| `login_lockout_minutes` | `LOGIN_LOCKOUT_MINUTES` | `30` | Lockout duration once the threshold is reached |
| `login_minimum_delay_ms` | `LOGIN_MINIMUM_DELAY_MS` | `400` | Wall-time floor on every login-failure branch, to equalize timing |
| `service_registry_write_per_minute` | `SERVICE_REGISTRY_WRITE_PER_MINUTE` | `60` | Rate limit for service-registry POST/toggle/refresh/DELETE |
| `service_registry_endpoint_url` | `SERVICE_REGISTRY_ENDPOINT_URL` | `""` | Explicit `endpoint_url` override for `register_service()`'s self-registration POST; empty (default) omits it so the upsert leaves an existing row's host/port untouched. Set only when this process's own view of itself is genuinely authoritative (e.g. an unseeded bare-metal dev RAG service) |
| `health_poll_interval_seconds` | `HEALTH_POLL_INTERVAL_SECONDS` | `30` | Background health-poller cycle interval |
| `health_poll_timeout_seconds` | `HEALTH_POLL_TIMEOUT_SECONDS` | `5` | Per-service `/health` request timeout |
| `health_poll_concurrency` | `HEALTH_POLL_CONCURRENCY` | `8` | Max simultaneous outbound health pings per cycle |
| `health_poll_allowed_private_hosts` | `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` | `""` | Comma-separated CIDRs/hostnames allowed through the health poller's own SSRF guard |
| `sitescraper_allowed_private_hosts` | `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` | `""` | Comma-separated CIDRs/hostnames allowed through the sitescraper SSRF guard. Scope narrowly — wide CIDRs like `10.0.0.0/8` bypass the guard for all RFC-1918 addresses. |
| `content_fetcher_allow_browser_fetch` | `CONTENT_FETCHER_ALLOW_BROWSER_FETCH` | `false` | Enable Playwright browser fetching in ContentFetcher. Playwright paths bypass the SSRF guard; only enable in isolated/controlled deployments. |
| `session_max_count` | `SESSION_MAX_COUNT` | `5000` | Cap on concurrent per-conversation OpenAI-compatible sessions |
| `new_conversation_per_minute_per_ip` | `NEW_CONVERSATION_PER_MINUTE_PER_IP` | `120` | Gateway sliding-window limit on new (first-turn) conversations per rate-limit key |
| `trusted_proxy_cidrs` | `TRUSTED_PROXY_CIDRS` | `""` | CIDRs/hosts the new-conversation limiter trusts `X-Forwarded-For` from; empty means every caller's TCP peer is trusted directly |
| `new_conversation_reset_grace_seconds` | `NEW_CONVERSATION_RESET_GRACE_SECONDS` | `120` | Grace window before a first-turn fingerprint reset, to tolerate HA's truncated-ASR retry |
| `orchestrator_ingress_auth` | `ORCHESTRATOR_INGRESS_AUTH` | `enforce` | `enforce`\|`warn`; gates the orchestrator's query/session routes behind `X-Service-Key` |
| `music_assistant_url` | `MUSIC_ASSISTANT_URL` | `""` | Empty means Music Assistant isn't configured (no hardcoded-host fallback) |
| `searxng_base_url` | `SEARXNG_BASE_URL` | `""` | Empty means the SearXNG search provider is disabled |
| `jarvis_web_url` | `JARVIS_WEB_URL` | `""` | Empty means the smart-home controller's jarvis-web appliance/sensor/media lookups are skipped |
| `transit_region_name` | `TRANSIT_REGION_NAME` | `""` | Cosmetic label for the configured transit region |
| `transit_gtfs_feeds` | `TRANSIT_GTFS_FEEDS` | `""` | JSON GTFS feed definitions for the transportation RAG service |
| `transit_static_services` | `TRANSIT_STATIC_SERVICES` | `""` | JSON non-GTFS transit services (fixed schedules) |
| `community_events_sources` | `COMMUNITY_EVENTS_SOURCES` | `""` | JSON community-event source definitions |
| `default_amtrak_station` | `DEFAULT_AMTRAK_STATION` | `""` | Default Amtrak origin station code |
| `ha_satellite_room_map` | `HA_SATELLITE_ROOM_MAP` | `""` | Voice PE `assist_satellite` entity → room map (takes priority over the generic friendly-name parse) |
| `ha_tv_entities` | `HA_TV_ENTITIES` | `""` | Fallback room → Apple TV entity map, used only when the admin API is unreachable |
| `ha_bed_warmer_entities` | `HA_BED_WARMER_ENTITIES` | `""` | Entity ids for a Sunbeam-via-Tuya bed-warmer integration |
| `ha_light_groups` | `HA_LIGHT_GROUPS` | `""` | Room → light-group entity map, read only by the scene-activation-failed fallback (per room; empty means no fallback for that room, never house-wide) |
| `ha_music_players` | `HA_MUSIC_PLAYERS` | `""` | Fallback room → Music Assistant entity map, used only when the admin API is unreachable |
| `ha_permission_fallback_restricted_entities` | `HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES` | `""` | Entity-id regex patterns applied as the "degraded" permission set when the mode service is unreachable or rejecting (ATHENA-69 D4); empty string means "use the built-in default list", explicit `[]` means an outage grants unrestricted HA writes |
| `guest_baseline_restricted_entities` | `GUEST_BASELINE_RESTRICTED_ENTITIES` | `""` | Floor unioned into every guest's `restricted_entities` regardless of admin config (D8); always unioned in, never replaced |
| `guest_baseline_allowed_intents` | `GUEST_BASELINE_ALLOWED_INTENTS` | `""` | Baseline used only when the admin-configured guest `allowed_intents` is empty (D22); empty admin list means "use this baseline", never "allow everything" |
| `guest_baseline_allowed_domains` | `GUEST_BASELINE_ALLOWED_DOMAINS` | `""` | Baseline used only when the admin-configured guest `allowed_domains` is empty (D22) |
| `mode_service_ingress_auth` | `MODE_SERVICE_INGRESS_AUTH` | `enforce` | `enforce`\|`warn`; gates the mode service's `/mode*` routes behind `X-Service-Key` (D15) |
| `mode_override_lockout_threshold` | `MODE_OVERRIDE_LOCKOUT_THRESHOLD` | `5` | Failed owner-PIN verifications (per trust tier) before that tier locks out (D16/D25) |
| `mode_override_lockout_minutes` | `MODE_OVERRIDE_LOCKOUT_MINUTES` | `30` | Owner-PIN lockout duration once the threshold is reached |
| `ha_write_fanout_confirm_threshold` | `HA_WRITE_FANOUT_CONFIRM_THRESHOLD` | `6` | Distinct entities a non-command utterance may write before it needs a confirmation or explicit all/group wording; `0` disables |
| `ha_write_fanout_hard_limit` | `HA_WRITE_FANOUT_HARD_LIMIT` | `18` | The same bound for plain commands; `0` disables; `0 < hard_limit < threshold` logs an ERROR and both fall back to `6`/`18` (non-fatal: every service loading the shared config reads it) |
| `livekit_user_token_ttl_minutes` | `LIVEKIT_USER_TOKEN_TTL_MINUTES` | `30` | TTL (clamped 1-1440) for browser-facing LiveKit room tokens (jarvis-web voice sessions); server-side Athena participant tokens are unaffected (D27) |
| `override_max_timeout_minutes` | `OVERRIDE_MAX_TIMEOUT_MINUTES` | `240` | Server-side ceiling on `POST /mode/override`'s `timeout_minutes`, applied regardless of PIN outcome; a requested value above this is clamped, never rejected (ATHENA-69 Pass H2) |

## Fetching user-supplied or admin-supplied URLs (SSRF guard)

**Any code path that fetches a URL from an untrusted source (request body, database row, feature-flag config, admin UI input) MUST use the shared SSRF guard.**

### Three-way URL classification (mandatory checklist for every new fetch site)

Before adding a new HTTP fetch, classify the URL source:

| Class | Description | Required guard |
|-------|-------------|----------------|
| **Class 1** | User/admin-supplied URL: request body field, stored DB record, feature-flag config row, search-result URL, MCP discovery URL, admin-editable config | MUST use `safe_get`/`safe_post`/`safe_request` from `src/shared/url_safety.py` |
| **Class 2** | Operator-trusted infra URL: connector/service health-check target stored in the service registry (admin-stored host, not user-controlled) | Disable auto-redirects (`follow_redirects=False` / `allow_redirects=False`); per-hop revalidation only; do NOT fail-closed-guard |
| **Class 3** | Operator-env service URL: env var at deploy time (`OVERSEERR_URL`, `HA_URL`, `N8N_MCP_URL`, fixed deployment-config endpoints) | Exempt — do NOT guard |

**The classification must be documented in a comment at the call site.**

### Using the guard

```python
from shared.url_safety import safe_get, safe_post, SsrfBlockedError

# Class 1 — user-supplied URL
try:
    response = await safe_get(
        user_supplied_url,
        allowed_private_hosts=get_config().sitescraper_allowed_private_hosts.split(","),
    )
except SsrfBlockedError as exc:
    raise HTTPException(status_code=400, detail=str(exc))
```

`safe_get`/`safe_post` never follow redirects automatically — each hop is re-validated against `validate_url_not_private` and the TCP connect is IP-pinned to the validated address (DNS-rebinding mitigation). POST 307/308 redirects are refused. POST 301/302/303 redirects downgrade to GET and strip the body and credential headers on cross-origin hops.

Operator-configured feed/content URLs (the transportation service's GTFS feeds, the community-events service's sources) are Class 1 too — they fetch through `safe_request` (via `safe_get`) with the same per-hop private-address validation, no `allowed_private_hosts` by default. A per-feed/source `allow_private: true` schema field is the only way to exempt one entry's own hostname, on every hop; it does not touch the shared allowlist any other Class-1 call site uses.

### Do not

- Call `httpx.AsyncClient` directly for a Class-1 URL.
- Pass `allowed_private_hosts` from env without naming it in a comment (D9: the validator never reads env — the caller decides the allowlist).
- Use `validate_url_not_private` with undocumented CIDR allowlists in a Class-1 path.

### httpx version contract

`httpx` is pinned exactly (`httpx==0.28.1`) in every one of this repo's 29 generated, hashed image locks — no image floats on httpx today.

For the 27 images that install `-e src/shared` (all 23 RAG services, `admin/backend`, `gateway`, `mode_service`, `orchestrator`), that pin traces back to `src/shared/pyproject.toml`'s `httpx~=0.28` constraint, but the constraint is enforced at **lock time**, not at image-install time: `scripts/lock-requirements.sh` compiles `src/shared/pyproject.toml` together with each image's own `requirements.in` into that image's lock (`is_no_shared_dir` gates which images skip this — see below). The Dockerfile's shared install itself (`pip install --no-deps --no-build-isolation -e /app/shared`) installs zero dependencies by design; the lock installed in the same stage right after is what actually puts `httpx==0.28.1` on disk. To change the httpx pin for these 27 images: edit `httpx~=0.28` in `src/shared/pyproject.toml`, run `make lock`, and commit the regenerated locks — hand-editing any `requirements.txt` has no effect, since it's regenerated from its `.in` (and, for these 27, from `src/shared/pyproject.toml`) on the next `make lock`.

`apps/jarvis-web/backend` and `apps/chat-embed` are both `NO_SHARED_DIRS` exemptions (`scripts/lock-requirements.sh`) — neither installs `-e src/shared`, so neither is compiled against `src/shared/pyproject.toml`'s constraint; each's `httpx==0.28.1` pin comes entirely from its own `requirements.in`/lock. Neither uses `_build_pinned_transport` or is bound by the SSRF-guard contract below. `_build_pinned_transport` in `src/shared/url_safety.py` — the SSRF guard's IP-pinning connection layer — relies on `httpx.AsyncHTTPTransport._pool`, a private attribute of `httpx ≥ 0.28`. A version bump that removes or restructures `_pool` silently degrades the SNI/IP-pinning protection (there is a runtime feature-detection guard, but it fails open to an unpinned transport rather than failing the request). **Before upgrading httpx past `0.28.x` anywhere in this repo**, re-verify `_build_pinned_transport` against the new version — see the version-requirement notes at the top of `url_safety.py` and the tests that exercise the real (unmocked) pinned transport: `TestPinnedNetworkBackend`, `TestSniPreservation`, `TestBuildPinnedTransportFallback`.

## Module Development

When adding new modules or RAG services:

1. Register the module in `shared/module_registry.py`
2. Add appropriate environment variable controls
3. Ensure the module gracefully handles being disabled
4. Document the module in `docs/MODULES.md`

## Questions?

If you have questions about contributing, please open an issue with the "question" label.

## License

By contributing to Project Athena, you agree that your contributions will be licensed under the PolyForm Noncommercial License 1.0.0.
