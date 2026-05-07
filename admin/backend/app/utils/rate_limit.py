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
    `request.client.host` is the trusted peer.  If a future deployment fronts
    the admin-backend with a proxy that DOES set X-Forwarded-For with trusted
    values, the identifier must be made deployer-configurable rather than
    individual deps overriding it ad hoc.
"""
from fastapi import Request, Response
from fastapi_limiter.depends import RateLimiter

from shared.config import get_config

LIMITER_ACTIVE: bool = False


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
    await RateLimiter(times=cfg.login_rate_limit_per_minute, seconds=60)(request, response)
