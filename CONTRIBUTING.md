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

1. Add every top-level import's package to `src/rag/<service>/requirements.txt`. If `main.py` does `import feedparser`, feedparser must be in that file.
2. If `main.py` does `from foo.bar import X`, the Dockerfile must `COPY rag/<service>/foo /app/foo` so `foo` lands at `/app/foo` (the `WORKDIR`). Use `SERVICE_EXTRA_COPIES` in `scripts/generate-rag-dockerfiles.py` to codify this.
3. Do not import from `orchestrator/`, `gateway/`, or any non-RAG service module. If you need shared logic, move it to `src/shared/` first.
4. Do not read `REDIS_HOST` or `REDIS_PORT` directly — kubelet auto-injects `REDIS_PORT=tcp://...` for any K8s Service named `redis`. Use `REDIS_URL` via `get_config().redis_url`, or a service-specific `<SERVICE>_REDIS_URL` env var with the DB index in the URL path.
5. Add the service to `RAG_SERVICES` in `scripts/service-defs.sh` (sourced by `build-and-push.sh`, `smoke-rag-images.sh`, and `smoke-images.sh`).
6. Run `make smoke-rags SERVICE=<image-name>` locally before opening a PR (e.g. `make smoke-rags SERVICE=athena-rag-sports`). CI enforces this on every PR touching `src/rag/**` or `src/shared/**`.

### Adding or editing `admin/frontend/` code that renders untrusted data

Read `admin/frontend/README.md` first — it covers the two escaping primitives, the six contexts `escapeJsAttr` is wrong for, and the "add a new frontend file" checklist. `.github/workflows/frontend-escaping.yml` enforces zero wrong-primitive/unescaped handler sites, a single `escapeHtml`/`escapeJsAttr` definition, and load-order/hardening on every PR touching `admin/frontend/**`.

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

   **Test-only dependencies:** `pytest-httpserver>=1.0.8` is in `admin/backend/requirements.txt`, annotated `# test-only`. It is used by the OIDC validation tests (`admin/backend/tests/test_oidc_validation.py`) to stand up a minimal fixture issuer that serves `/.well-known/openid-configuration` and a JWKS endpoint, allowing tests to drive authlib's real validator without mocking it. This package is included in the production image as a known trade-off — splitting dev and production requirements is deferred to a future campaign (tracked as HIGH-E in `thoughts/shared/plans/active-2026-05-06-deliver-security-hardening.md`).

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

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `ollama_url` | `OLLAMA_URL` | `http://localhost:11434` | LLM inference endpoint |
| `llm_service_url` | `LLM_SERVICE_URL` | `""` | Overrides `ollama_url` when set |
| `llm_endpoint` | _(computed)_ | falls back to `ollama_url` | `llm_service_url` wins when non-empty |
| `redis_url` | `REDIS_URL` | `redis://redis:6379/0` | In-cluster DNS default |
| `database_url` | `DATABASE_URL` | `""` | PostgreSQL connection string |
| `service_api_key` | `SERVICE_API_KEY` | `""` | Service-to-service auth key |
| `default_timezone` | `DEFAULT_TIMEZONE` | `UTC` | |
| `default_city` | `DEFAULT_CITY` | `""` | |
| `oidc_issuer` | `OIDC_ISSUER` | `""` | Whitespace stripped |
| `oidc_client_id` | `OIDC_CLIENT_ID` | `""` | Whitespace stripped |
| `dev_mode` | `DEV_MODE` | `false` | |
| `demo_mode` | `DEMO_MODE` | `false` | |
| `control_agent_enabled` | `CONTROL_AGENT_ENABLED` | `false` | Opt-in; set `true` only if a Control Agent runs on a host alongside Ollama. Valid values: `true`/`false`/`1`/`0`. Do not set to a blank string. |
| `sitescraper_allowed_private_hosts` | `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` | `""` | Comma-separated CIDRs/hostnames allowed through the sitescraper SSRF guard. Scope narrowly — wide CIDRs like `10.0.0.0/8` bypass the guard for all RFC-1918 addresses. |
| `content_fetcher_allow_browser_fetch` | `CONTENT_FETCHER_ALLOW_BROWSER_FETCH` | `false` | Enable Playwright browser fetching in ContentFetcher. Playwright paths bypass the SSRF guard; only enable in isolated/controlled deployments. |

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

### Do not

- Call `httpx.AsyncClient` directly for a Class-1 URL.
- Pass `allowed_private_hosts` from env without naming it in a comment (D9: the validator never reads env — the caller decides the allowlist).
- Use `validate_url_not_private` with undocumented CIDR allowlists in a Class-1 path.

### httpx version contract

`httpx` is pinned `~=0.28` in `src/shared/pyproject.toml`; `admin/backend/requirements.in` carries its own copy of the same pin and comment (corrected per retroactive review, M8-I2 — this section previously claimed the pin was declared "in every Python service's dependency spec"). Every other Python image that installs `-e src/shared` (all RAG services, `gateway`, `mode_service`, `orchestrator`) inherits the `~=0.28` floor transitively through athena-shared rather than declaring it directly. Per PEP 440, `~=0.28` means `>=0.28, ==0.*` — any `0.x` release at or above `0.28`, not "any `0.28.x` patch release" — so this floor alone does not by itself block a resolve onto, say, `0.29.0`; the private-`_pool` contract below is what actually constrains upgrades. `apps/jarvis-web/backend` and `apps/chat-embed` are both `NO_SHARED_DIRS` exemptions (`scripts/lock-requirements.sh`) — neither installs `-e src/shared`, so neither uses `_build_pinned_transport` or is bound by the SSRF-guard contract below — and each floats its own httpx spec independently: `jarvis-web/backend` pins `httpx==0.28.1`; `chat-embed` (not yet converted to a generated lock — still a hand-pinned `requirements.txt`) floats `httpx>=0.25.0`, below `0.28` but with no SSRF-guard dependency to break since `chat-embed` is a CORS-relay proxy, not a `src/shared` consumer. `_build_pinned_transport` in `src/shared/url_safety.py` — the SSRF guard's IP-pinning connection layer — relies on `httpx.AsyncHTTPTransport._pool`, a private attribute of `httpx ≥ 0.28`. A version bump that removes or restructures `_pool` silently degrades the SNI/IP-pinning protection (there is a runtime feature-detection guard, but it fails open to an unpinned transport rather than failing the request). **Before upgrading httpx past `0.28.x` anywhere in this repo**, re-verify `_build_pinned_transport` against the new version — see the version-requirement notes at the top of `url_safety.py` and the tests that exercise the real (unmocked) pinned transport: `TestPinnedNetworkBackend`, `TestSniPreservation`, `TestBuildPinnedTransportFallback`.

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
