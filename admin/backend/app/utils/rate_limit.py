"""Rate-limiter wiring for the local-login endpoint (xander:10 / ATHENA-14).

LIMITER_ACTIVE flag is set True only on successful FastAPILimiter.init().
Single positive truth condition handles all three states:
  - DEV_MODE never inits → False → dep no-ops
  - Redis-fail at startup → init raises → False → dep no-ops
  - Successful init → True → dep enforces

Importing this module does NOT initialize the limiter.  _init_rate_limiter()
in main.py performs init at startup.

Process-wide identifier note (ian:3 / xander:47):
    `main._init_rate_limiter` registers a custom `_ip_identifier` with
    `FastAPILimiter.init`.  ALL rate-limit deps in this module — current and
    future — share that identifier, which keys buckets by `request.client.host`
    only and ignores `X-Forwarded-For`.  This is intentional: the admin-backend
    sits behind in-cluster Traefik / direct Kubernetes Service traffic, so
    `request.client.host` is the trusted Kubernetes/Traefik peer.  If a future
    deployment fronts the admin-backend with a proxy that DOES set
    X-Forwarded-For with trusted values, the identifier must be made
    deployer-configurable rather than individual deps overriding it ad hoc.

Why this module does NOT call fastapi_limiter.depends.RateLimiter (ATHENA-63
M9-R1, Critical — found by manual gate M3 on the isolated local stack):
    fastapi-limiter==0.1.6's RateLimiter.__call__ walks every
    request.app.routes entry and reads route.path to build its rate-limit
    key.  FastAPI 0.141.1 (brought in by this campaign's Phase 4) wraps
    every app.include_router(...) registration in
    fastapi.routing._IncludedRouter, which has no .path — so every
    limiter-protected request raised AttributeError and returned 500
    whenever LIMITER_ACTIVE was True (i.e. whenever Redis was reachable at
    startup: production). fastapi-limiter 0.2.0 has the same unguarded
    loop, so upgrading does not fix it; pinning FastAPI back would reopen
    the Phase 4/7 version decisions. See the ATHENA-63 entry in
    CHANGELOG.md for the product-facing summary.

    _enforce() below reimplements RateLimiter.__call__'s Lua-script check
    directly against the FastAPILimiter class state that main.py's
    _init_rate_limiter() already populates at startup (redis connection,
    identifier, prefix, http_callback, lua_sha, lua_script) — it does not
    touch request.app.routes at all. The rate-limit key is built from the
    route TEMPLATE (`request.scope["route"].path`, e.g.
    "/api/service-registry/services/{service_name}/check"), never the
    concrete substituted path, so a path parameter cannot mint a fresh
    bucket. A route with no resolvable template (should not happen for any
    route actually reachable through routing) falls back to one shared
    "__unrouted__" bucket rather than trusting attacker-influenced
    scope["path"].

    Fail-open (D1): any Redis error other than a recovered NoScriptError
    (script cache lost to a restart / failover / SCRIPT FLUSH — reloaded
    and retried once) logs a warning and lets the request through. This
    matches the existing startup posture (Redis-down at startup already
    leaves LIMITER_ACTIVE False and degrades to the lockout layer); the
    failed_login_count lockout and the 400ms PBKDF2 floor in
    local_auth.py still bound brute force. Under 0.1.6 an infrastructure
    hiccup mid-request turned into a login outage (500); it no longer
    does.
"""
import redis as pyredis  # alias, as fastapi_limiter/depends.py does — never shadowed by a local `redis`
import structlog
from fastapi import Request, Response
from fastapi_limiter import FastAPILimiter

from shared.config import get_config

logger = structlog.get_logger()

LIMITER_ACTIVE: bool = False


async def _enforce(request: Request, response: Response, *, times: int, window_ms: int, budget: str) -> None:
    """Reimplementation of fastapi_limiter.depends.RateLimiter.__call__'s
    Lua-script check that never touches request.app.routes (see module
    docstring for why). Raises via FastAPILimiter.http_callback (default:
    429 + Retry-After) when the budget is exceeded; returns None otherwise.
    """
    redis_conn = FastAPILimiter.redis  # set by init(); the LIMITER_ACTIVE gate is upstream of this call
    rate_key = await FastAPILimiter.identifier(request)  # registered _ip_identifier (client.host only)
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    # Route TEMPLATE, not the concrete scope["path"] (attacker-influenced via
    # path params) — so /services/a/check and /services/b/check share one
    # bucket instead of each path param value minting a fresh one (D3).
    route_id = f"{route_path}:{request.method}" if route_path else f"__unrouted__:{request.method}"
    key = f"{FastAPILimiter.prefix}:{rate_key}:{budget}:{route_id}"
    try:
        try:
            pexpire = await redis_conn.evalsha(FastAPILimiter.lua_sha, 1, key, str(times), str(window_ms))
        except pyredis.exceptions.NoScriptError:
            # Script cache lost (Redis restart / failover / SCRIPT FLUSH) —
            # reload once and retry, rather than surfacing a 500 for an
            # entirely recoverable condition.
            FastAPILimiter.lua_sha = await redis_conn.script_load(FastAPILimiter.lua_script)
            pexpire = await redis_conn.evalsha(FastAPILimiter.lua_sha, 1, key, str(times), str(window_ms))
    except pyredis.exceptions.RedisError as exc:
        # D1: fail open. Connection/timeout/etc. mid-request — the lockout
        # counter and the 400ms constant-time floor (local-login only)
        # still bound brute force; a Redis hiccup must not become a login
        # outage.
        logger.warning(
            "rate_limit_redis_unavailable_fail_open",
            budget=budget,
            route=route_id,
            error=type(exc).__name__,
        )
        return
    if pexpire != 0:
        return await FastAPILimiter.http_callback(request, response, pexpire)


async def login_rate_limit_dep(request: Request, response: Response) -> None:
    """FastAPI dependency for /local-login.

    Resolves LIMITER_ACTIVE at REQUEST time (not import time — bob:1 / xander:32 /
    codex-r1 fix).  When the limiter wasn't initialized (DEV_MODE or Redis-down),
    this dep is a no-op and request flow continues to the lockout layer.
    """
    if not LIMITER_ACTIVE:
        return
    cfg = get_config()
    if cfg.login_rate_limit_per_minute <= 0:
        # Documented disable: LOGIN_RATE_LIMIT_PER_MINUTE=0 (or negative) means the
        # rate limiter is bypassed entirely.  fastapi-limiter's Lua script treats
        # times=0 as "allow 1, then 429" — NOT disabled — so we must short-circuit
        # here before constructing RateLimiter.  The lockout layer (failed_login_count
        # + locked_until) and the 400 ms constant-time floor still protect /local-login.
        # codex-r2:4 / ATHENA-14.
        return
    await _enforce(request, response, times=cfg.login_rate_limit_per_minute, window_ms=60000, budget="login")


async def service_registry_rate_limit_dep(request: Request, response: Response) -> None:
    """FastAPI dependency for service-registry write endpoints.

    Separate rate-limit budget from login_rate_limit_dep so that service-registry
    writes (POST/toggle/refresh/DELETE) cannot consume the login bucket or vice
    versa.  Uses service_registry_write_per_minute from AthenaConfig (default 60).

    Resolves LIMITER_ACTIVE at REQUEST time — same pattern as login_rate_limit_dep.
    When the limiter wasn't initialized (DEV_MODE or Redis-down) this is a no-op.
    (xander HIGH-4 / ATHENA-1 Phase 2)
    """
    if not LIMITER_ACTIVE:
        return
    cfg = get_config()
    limit = cfg.service_registry_write_per_minute
    if limit <= 0:
        return
    await _enforce(request, response, times=limit, window_ms=60000, budget="service_registry")
