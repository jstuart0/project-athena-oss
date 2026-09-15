"""
Regression tests for the rate-limiter fix (ATHENA-63 M9-R1).

fastapi-limiter==0.1.6's RateLimiter.__call__ walks every request.app.routes
entry and reads route.path.  FastAPI 0.141.1 (Phase 4) wraps every
app.include_router(...) registration in fastapi.routing._IncludedRouter,
which has no .path -- so every limiter-protected request crashed with
AttributeError and returned 500 whenever LIMITER_ACTIVE was True (i.e.
whenever Redis was reachable at startup: production).  See the ATHENA-63
entry in CHANGELOG.md and app/utils/rate_limit.py's own module docstring
for the full design.

These tests go through the REAL main.app (every router included, exactly as
production wires it) with the limiter genuinely active -- fakeredis.aioredis
with the lua backend (lupa), the same FastAPILimiter.init() main.py's own
_init_rate_limiter() performs at startup.  That is deliberate: the bug only
manifests when a request actually reaches RateLimiter.__call__, which no
prior test exercised (DEV_MODE and Redis-down both leave LIMITER_ACTIVE
False and no-op the dependency).

T1, T2, T3 and T7 are written to FAIL against the unmodified rate_limit.py
(500 / AttributeError) and PASS once app/utils/rate_limit.py stops calling
fastapi_limiter.depends.RateLimiter.
"""
import ast
import os
import sys

import pytest
from fastapi import Request, Response


def _reload_app_modules():
    """Evict app/main/shared modules so a fresh import sees this test's env
    and a clean rate_limit.LIMITER_ACTIVE (mirrors lockout_client /
    TestRateLimiterStartup's own module-reload pattern in this file)."""
    for name in list(sys.modules):
        if name == "main" or name.startswith(("main.", "app.", "shared.")):
            del sys.modules[name]


def _build_client(monkeypatch, *, login_limit=None, service_limit=None,
                   login_delay_ms=0, activate_limiter=True):
    """Fresh main.app + in-memory SQLite + (optionally) an active fakeredis
    limiter. Returns (client, TestingSessionLocal, fake_redis_or_None,
    main_module). Caller must call _teardown() in a finally block."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from shared.config import get_config

    if login_limit is not None:
        monkeypatch.setenv("LOGIN_RATE_LIMIT_PER_MINUTE", str(login_limit))
    if service_limit is not None:
        monkeypatch.setenv("SERVICE_REGISTRY_WRITE_PER_MINUTE", str(service_limit))
    monkeypatch.setenv("LOGIN_MINIMUM_DELAY_MS", str(login_delay_ms))

    _reload_app_modules()
    get_config.cache_clear()

    import main as main_mod
    from app.database import Base, get_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    main_mod.app.dependency_overrides[get_db] = override_get_db

    client = TestClient(main_mod.app, raise_server_exceptions=False)
    client.__enter__()

    fake_redis = None
    if activate_limiter:
        import fakeredis.aioredis

        # Must run FastAPILimiter.init() on the SAME event loop the app's own
        # requests will execute on. TestClient's portal owns that loop once
        # entered; a separate asyncio.run() binds the fakeredis connection to
        # a different loop entirely, and the very first evalsha then raises
        # "bound to a different event loop" -- a real async-plumbing hazard
        # this fixture must get right, not a bug in _enforce() itself.
        fake_redis = fakeredis.aioredis.FakeRedis()
        client.portal.call(main_mod._init_rate_limiter, fake_redis)
        from app.utils import rate_limit as rate_limit_mod
        assert rate_limit_mod.LIMITER_ACTIVE is True, "test setup: limiter must be active"

    return client, TestingSessionLocal, fake_redis, main_mod


def _teardown(client, main_mod):
    # Close FastAPILimiter's redis connection on the SAME loop (the portal's)
    # it was opened on, before that loop goes away with client.__exit__().
    from fastapi_limiter import FastAPILimiter
    if getattr(FastAPILimiter, "redis", None) is not None:
        try:
            client.portal.call(FastAPILimiter.close)
        except Exception:
            pass

    client.__exit__(None, None, None)
    main_mod.app.dependency_overrides.clear()

    from app.utils import rate_limit as rate_limit_mod
    rate_limit_mod.LIMITER_ACTIVE = False
    from shared.config import get_config
    get_config.cache_clear()


class TestRateLimiterActiveUnderFastAPI0141:
    """T1, T2, T2b, T3, T7: real requests through the real app with the
    limiter active. All FAIL (500) on unmodified rate_limit.py."""

    def test_t1_local_login_never_500_then_429(self, monkeypatch):
        """T1: wrong-credential local-login attempts 1..N return 401, never
        500; attempt N+1 returns 429 with Retry-After."""
        N = 2
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=N, service_limit=100,
        )
        try:
            for i in range(N):
                r = client.post(
                    "/api/auth/local-login",
                    json={"username": "no-such-user", "password": "wrong"},
                )
                assert r.status_code == 401, (
                    f"attempt {i + 1}/{N}: expected 401, got {r.status_code}: {r.text}"
                )

            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 429, f"expected 429 after {N} attempts, got {r.status_code}: {r.text}"
            assert "retry-after" in {k.lower() for k in r.headers.keys()}, r.headers
        finally:
            _teardown(client, main_mod)

    def test_t2_ws_ticket_never_500_then_429(self, monkeypatch):
        """T2: authenticated ws-ticket mint returns 200 under the limit
        (never 500), 429 over it. DEV_MODE dev-admin bypass authenticates
        (no X-API-Key header supplied)."""
        N = 2
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=N, service_limit=100,
        )
        try:
            for i in range(N):
                r = client.post("/api/auth/ws-ticket")
                assert r.status_code == 200, (
                    f"attempt {i + 1}/{N}: expected 200, got {r.status_code}: {r.text}"
                )
                assert "ticket" in r.json(), r.json()

            r = client.post("/api/auth/ws-ticket")
            assert r.status_code == 429, f"expected 429 after {N} attempts, got {r.status_code}: {r.text}"
        finally:
            _teardown(client, main_mod)

    def test_t2b_login_and_ws_ticket_buckets_are_isolated(self, monkeypatch):
        """T2b: exhausting /api/auth/local-login must not 429
        /api/auth/ws-ticket, and vice versa -- same 'login' budget config,
        separate per-route buckets (D2)."""
        N = 2
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=N, service_limit=100,
        )
        try:
            for _ in range(N):
                r = client.post(
                    "/api/auth/local-login",
                    json={"username": "no-such-user", "password": "wrong"},
                )
                assert r.status_code == 401, r.text
            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 429, "local-login should now be exhausted"

            # ws-ticket must be unaffected -- separate bucket.
            r = client.post("/api/auth/ws-ticket")
            assert r.status_code == 200, (
                f"ws-ticket bucket must be independent of local-login's; got {r.status_code}: {r.text}"
            )
        finally:
            _teardown(client, main_mod)

        # Reverse direction: exhaust ws-ticket, confirm local-login unaffected.
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=N, service_limit=100,
        )
        try:
            for _ in range(N):
                r = client.post("/api/auth/ws-ticket")
                assert r.status_code == 200, r.text
            r = client.post("/api/auth/ws-ticket")
            assert r.status_code == 429, "ws-ticket should now be exhausted"

            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 401, (
                f"local-login bucket must be independent of ws-ticket's; got {r.status_code}: {r.text}"
            )
        finally:
            _teardown(client, main_mod)

    def test_t3_service_registry_write_never_500_then_429_and_isolated(self, monkeypatch):
        """T3: POST /api/service-registry/services/poll-now with a valid
        X-Service-Key returns its normal status under the limit (never
        500), 429 over it. Login and service-registry budgets are
        isolated both ways."""
        N = 2
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=100, service_limit=N,
        )
        service_key = os.environ.get("SERVICE_API_KEY", "test-service-key-for-hardening-tests")
        try:
            for i in range(N):
                r = client.post(
                    "/api/service-registry/services/poll-now",
                    headers={"X-Service-Key": service_key},
                )
                assert r.status_code != 500, (
                    f"attempt {i + 1}/{N}: must not 500, got {r.status_code}: {r.text}"
                )
                assert r.status_code == 200, f"expected normal 200, got {r.status_code}: {r.text}"

            r = client.post(
                "/api/service-registry/services/poll-now",
                headers={"X-Service-Key": service_key},
            )
            assert r.status_code == 429, f"expected 429 after {N} writes, got {r.status_code}: {r.text}"

            # Isolation: login budget must be untouched by service-registry exhaustion.
            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 401, (
                f"login bucket must be independent of service-registry's; got {r.status_code}: {r.text}"
            )
        finally:
            _teardown(client, main_mod)

        # Reverse: exhaust login, confirm service-registry write is unaffected.
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=N, service_limit=100,
        )
        try:
            for _ in range(N):
                r = client.post(
                    "/api/auth/local-login",
                    json={"username": "no-such-user", "password": "wrong"},
                )
                assert r.status_code == 401, r.text
            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 429, "login should now be exhausted"

            r = client.post(
                "/api/service-registry/services/poll-now",
                headers={"X-Service-Key": service_key},
            )
            assert r.status_code == 200, (
                f"service-registry bucket must be independent of login's; got {r.status_code}: {r.text}"
            )
        finally:
            _teardown(client, main_mod)

    def test_t7_path_param_variation_cannot_evade_the_bucket(self, monkeypatch):
        """T7: POST /api/service-registry/services/{service_name}/check with
        DIFFERENT service_name values past the limit still gets 429 --
        the key uses the route TEMPLATE (scope['route'].path), not the
        concrete substituted path, so varying service_name cannot mint
        fresh buckets."""
        N = 2
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=100, service_limit=N,
        )
        service_key = os.environ.get("SERVICE_API_KEY", "test-service-key-for-hardening-tests")
        try:
            names = ["svc-a", "svc-b", "svc-c", "svc-d", "svc-e"]
            statuses = []
            for name in names:
                r = client.post(
                    f"/api/service-registry/services/{name}/check",
                    headers={"X-Service-Key": service_key},
                )
                assert r.status_code != 500, f"{name}: must not 500, got {r.status_code}: {r.text}"
                statuses.append(r.status_code)

            assert 429 in statuses, (
                f"expected a 429 among {len(names)} calls with distinct service_name after "
                f"a limit of {N} if the bucket keys on the route template; got {statuses}"
            )
            # The first N calls (regardless of service_name) must NOT be 429 --
            # a per-concrete-path bucket would never produce a 429 at all here.
            assert 429 not in statuses[:N], (
                f"first {N} calls should still be under budget; got {statuses[:N]}"
            )
        finally:
            _teardown(client, main_mod)

    def test_t4_zero_limit_bypasses_rate_limiting(self, monkeypatch):
        """T4: LOGIN_RATE_LIMIT_PER_MINUTE=0 means no 429 even after many
        requests -- the documented bypass in login_rate_limit_dep."""
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=0, service_limit=100,
        )
        try:
            for i in range(6):
                r = client.post(
                    "/api/auth/local-login",
                    json={"username": "no-such-user", "password": "wrong"},
                )
                assert r.status_code == 401, f"attempt {i + 1}: expected 401, got {r.status_code}: {r.text}"
        finally:
            _teardown(client, main_mod)

    def test_t5_inactive_limiter_is_a_noop(self, monkeypatch):
        """T5: with LIMITER_ACTIVE False (limiter never initialized), the
        dependencies no-op -- existing (non-limiter) tests stay green."""
        client, _Session, _redis, main_mod = _build_client(
            monkeypatch, login_limit=1, service_limit=1, activate_limiter=False,
        )
        try:
            from app.utils import rate_limit as rate_limit_mod
            assert rate_limit_mod.LIMITER_ACTIVE is False

            for i in range(5):
                r = client.post(
                    "/api/auth/local-login",
                    json={"username": "no-such-user", "password": "wrong"},
                )
                assert r.status_code == 401, f"attempt {i + 1}: expected 401, got {r.status_code}: {r.text}"
        finally:
            _teardown(client, main_mod)

    def test_t9_mid_request_redis_outage_fails_open(self, monkeypatch):
        """T9: after a successful init, a mid-request Redis outage
        (ConnectionError from evalsha) must fail OPEN -- the login request
        proceeds to its normal 401, not 500 -- and a warning event is
        logged."""
        import redis as pyredis
        import structlog.testing

        client, _Session, fake_redis, main_mod = _build_client(
            monkeypatch, login_limit=100, service_limit=100,
        )
        try:
            async def _boom(*args, **kwargs):
                raise pyredis.exceptions.ConnectionError("test-forced outage")

            monkeypatch.setattr(fake_redis, "evalsha", _boom)

            with structlog.testing.capture_logs() as logs:
                r = client.post(
                    "/api/auth/local-login",
                    json={"username": "no-such-user", "password": "wrong"},
                )
            assert r.status_code == 401, f"must fail open to normal 401, got {r.status_code}: {r.text}"
            events = [entry.get("event") for entry in logs]
            assert "rate_limit_redis_unavailable_fail_open" in events, events
        finally:
            _teardown(client, main_mod)


class TestNoScriptErrorRecovery:
    """T8 (Critical): a lost Lua script cache (Redis restart / SCRIPT FLUSH /
    failover) must be transparently recovered, not turned into a 500."""

    def test_t8_noscripterror_triggers_reload_and_retry(self, monkeypatch):
        import redis as pyredis

        client, _Session, fake_redis, main_mod = _build_client(
            monkeypatch, login_limit=100, service_limit=100,
        )
        try:
            calls = {"evalsha": 0}
            orig_evalsha = fake_redis.evalsha
            orig_script_load = fake_redis.script_load
            script_load_calls = {"n": 0}

            async def flaky_evalsha(*args, **kwargs):
                calls["evalsha"] += 1
                if calls["evalsha"] == 1:
                    raise pyredis.exceptions.NoScriptError("test-forced: script cache lost")
                return await orig_evalsha(*args, **kwargs)

            async def counting_script_load(*args, **kwargs):
                script_load_calls["n"] += 1
                return await orig_script_load(*args, **kwargs)

            monkeypatch.setattr(fake_redis, "evalsha", flaky_evalsha)
            monkeypatch.setattr(fake_redis, "script_load", counting_script_load)

            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 401, f"must recover and return normal 401, got {r.status_code}: {r.text}"
            assert calls["evalsha"] == 2, f"expected exactly one retry (2 evalsha calls), got {calls['evalsha']}"
            assert script_load_calls["n"] == 1, f"expected exactly one script_load reload, got {script_load_calls['n']}"
        finally:
            _teardown(client, main_mod)

    def test_t8_is_non_vacuous_against_the_shadowed_name_bug(self, monkeypatch):
        """Proves T8 actually exercises the except clause for real: a naive
        implementation that shadows the module-level `redis` alias with a
        local variable bound to the redis CONNECTION (`redis =
        FastAPILimiter.redis`) and then writes
        `except redis.exceptions.NoScriptError` crashes instead of
        recovering, because by the time Python evaluates that except
        clause, `redis` no longer refers to the `redis` package -- it's
        the connection object, which has no `.exceptions` attribute. That
        raises a fresh AttributeError while handling the NoScriptError,
        which FastAPI's exception middleware turns into a 500 (with
        raise_server_exceptions=False the TestClient surfaces the 500
        response rather than propagating the exception).
        """
        import redis as pyredis
        from fastapi_limiter import FastAPILimiter

        client, _Session, fake_redis, main_mod = _build_client(
            monkeypatch, login_limit=100, service_limit=100,
        )
        try:
            calls = {"evalsha": 0}
            orig_evalsha = fake_redis.evalsha

            async def flaky_evalsha(*args, **kwargs):
                calls["evalsha"] += 1
                if calls["evalsha"] == 1:
                    raise pyredis.exceptions.NoScriptError("test-forced: script cache lost")
                return await orig_evalsha(*args, **kwargs)

            monkeypatch.setattr(fake_redis, "evalsha", flaky_evalsha)

            async def shadowed_bug_enforce(request: Request, response: Response, *, times, window_ms, budget):
                # The bug, reproduced verbatim: a local `redis` name bound to
                # the CONNECTION shadows the module-level `redis` package
                # alias used in the except clause below.
                redis = FastAPILimiter.redis  # noqa: F841 -- intentional shadow, this IS the bug
                rate_key = await FastAPILimiter.identifier(request)
                route = request.scope.get("route")
                route_path = getattr(route, "path", None)
                route_id = f"{route_path}:{request.method}" if route_path else f"__unrouted__:{request.method}"
                key = f"{FastAPILimiter.prefix}:{rate_key}:{budget}:{route_id}"
                try:
                    pexpire = await redis.evalsha(FastAPILimiter.lua_sha, 1, key, str(times), str(window_ms))
                except redis.exceptions.NoScriptError:
                    FastAPILimiter.lua_sha = await redis.script_load(FastAPILimiter.lua_script)
                    pexpire = await redis.evalsha(FastAPILimiter.lua_sha, 1, key, str(times), str(window_ms))
                if pexpire != 0:
                    return await FastAPILimiter.http_callback(request, response, pexpire)

            from app.utils import rate_limit as rate_limit_mod

            async def shadowed_login_dep(request: Request, response: Response):
                from shared.config import get_config
                cfg = get_config()
                if cfg.login_rate_limit_per_minute <= 0:
                    return
                await shadowed_bug_enforce(
                    request, response,
                    times=cfg.login_rate_limit_per_minute, window_ms=60000, budget="login",
                )

            # Swap the dependency the route actually resolves at call time.
            main_mod.app.dependency_overrides[rate_limit_mod.login_rate_limit_dep] = shadowed_login_dep

            r = client.post(
                "/api/auth/local-login",
                json={"username": "no-such-user", "password": "wrong"},
            )
            assert r.status_code == 500, (
                "non-vacuity check: the shadowed-name variant was expected to 500 on a "
                f"NoScriptError (proving T8 exercises a real danger), got {r.status_code}: {r.text}"
            )
        finally:
            _teardown(client, main_mod)


class TestRateLimiterImplementationGuard:
    """T6: static regression guard -- no module under admin/backend may call
    fastapi_limiter.depends.RateLimiter again. fastapi-limiter 0.1.6's
    RateLimiter.__call__ crashes under FastAPI 0.141.1's _IncludedRouter
    routing (it walks request.app.routes reading .path, which
    _IncludedRouter doesn't have). See app/utils/rate_limit.py's module
    docstring for the full design."""

    _backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def _iter_py_files(self):
        for dirpath, dirnames, filenames in os.walk(self._backend_dir):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "tests")]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)
        yield os.path.join(self._backend_dir, "main.py")

    def test_t6_no_module_imports_or_calls_fastapi_limiter_RateLimiter(self):
        violations = []
        seen = set()
        for path in self._iter_py_files():
            if path in seen:
                continue
            seen.add(path)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=path)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "fastapi_limiter.depends":
                    for alias in node.names:
                        if alias.name == "RateLimiter":
                            violations.append(f"{path}:{node.lineno}: imports RateLimiter from fastapi_limiter.depends")
                if isinstance(node, ast.Attribute) and node.attr == "RateLimiter":
                    violations.append(f"{path}:{node.lineno}: references .RateLimiter")
                if isinstance(node, ast.Name) and node.id == "RateLimiter":
                    violations.append(f"{path}:{node.lineno}: references bare name RateLimiter")

        assert not violations, (
            "rate_limit.py must not use fastapi_limiter.depends.RateLimiter: it crashes "
            "under FastAPI's _IncludedRouter (walks request.app.routes reading .path). "
            "Violations:\n" + "\n".join(violations)
        )
