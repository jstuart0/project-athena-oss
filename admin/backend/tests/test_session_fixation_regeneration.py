"""ATHENA-80 -- session fixation: the session ID must rotate on authentication.

xander's finding: the admin-backend never rotated the starsessions session ID
on login. starsessions stores the session_id verbatim as the cookie value
(unsigned -- see starsessions.middleware.SessionMiddleware) and looks up
session data by that exact string, so a cookie value an attacker fixes in the
victim's browser *before* login remains the same cookie the victim's browser
sends *after* login. Without rotation, the victim's JWT gets written to the
store under that attacker-known id -- classic session fixation.

Fix: `regenerate_session_id(request)` (starsessions 2.2.1) is called
immediately after the session is populated on all three login paths:
local_login (app/routes/local_auth.py), demo-mode auth_login, and the OIDC
auth_callback (both in main.py).

auth_logout is deliberately NOT touched. Empirical check against starsessions
2.2.1 (see the file-level comment above auth_logout in main.py) showed that
adding regenerate_session_id() there breaks the store cleanup: the
SessionMiddleware's is_empty-after-request codepath calls handler.destroy()
using whatever handler.session_id currently is, and regenerate_session_id()
overwrites that field to a fresh, never-written id *before* destroy() runs --
so the OLD (JWT-bearing) store entry never gets removed and lingers until its
TTL expires, while the response cookie gets expired either way. Plain
session.clear() (the pre-existing code) already triggers the middleware's own
correct cleanup: the old store entry is removed and the cookie is expired in
the same response. This file's guard tests assert that behavior explicitly so
a future "fix" doesn't silently reintroduce the regression.

Coverage:
  1. Static regression guards -- grep each function's own source (via
     inspect.getsource, mirroring test_service_control_route_parity.py's
     drift-guard style) for the regenerate_session_id call, in the right
     order relative to session population / clear().
  2. Functional TestClient tests per login path -- plant a known
     "attacker-fixed" session cookie, authenticate, and assert (a) the
     Set-Cookie carries a DIFFERENT athena_session value, (b) the OLD cookie
     value was never written to the session store, and (c)
     GET /api/auth/session-token returns 401 with the old cookie and 200 with
     the new one.
  3. A positive control for logout: clear() alone still removes the store
     entry and expires the cookie (proving the "no regenerate needed here"
     judgment call is correct, not just asserted).
"""
import inspect
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-service-key-for-session-fixation-tests")


# ---------------------------------------------------------------------------
# 1. Static regression guards
# ---------------------------------------------------------------------------

def test_local_login_rotates_session_id_after_populating_session():
    from app.routes.local_auth import local_login

    source = inspect.getsource(local_login)
    assert "regenerate_session_id(request)" in source, (
        "local_login must call regenerate_session_id(request) after auth "
        "succeeds (ATHENA-80 session fixation)"
    )
    assert source.index("regenerate_session_id(request)") > source.index(
        'request.session["access_token"] = token'
    ), "regenerate_session_id must run AFTER the session is populated, not before"


def test_demo_mode_auth_login_rotates_session_id_after_populating_session():
    import main as _main

    source = inspect.getsource(_main.auth_login)
    assert "regenerate_session_id(request)" in source, (
        "demo-mode auth_login must call regenerate_session_id(request) after "
        "the demo session is populated (ATHENA-80 session fixation)"
    )
    assert source.index("regenerate_session_id(request)") > source.index(
        "request.session['user_id'] = int(demo_user.id)"
    ), "regenerate_session_id must run AFTER the session is populated, not before"


def test_oidc_auth_callback_rotates_session_id_after_populating_session():
    import main as _main

    source = inspect.getsource(_main.auth_callback)
    assert "regenerate_session_id(request)" in source, (
        "auth_callback must call regenerate_session_id(request) after the "
        "OIDC session is populated (ATHENA-80 session fixation)"
    )
    assert source.index("regenerate_session_id(request)") > source.index(
        "request.session['user_id'] = user.id"
    ), "regenerate_session_id must run AFTER the session is populated, not before"


def test_auth_logout_does_not_call_regenerate_session_id():
    """Drift guard for the deliberate divergence from the literal brief.

    See the module docstring and the comment in main.py::auth_logout: calling
    regenerate_session_id() after session.clear() breaks starsessions'
    own store cleanup (proven empirically by
    test_logout_clear_alone_removes_store_entry_and_expires_cookie below). If
    a future change adds it back, this test should fail and force a re-read
    of that comment rather than silently reintroducing the regression.
    """
    import main as _main

    source = inspect.getsource(_main.auth_logout)
    assert "regenerate_session_id(request)" not in source, (
        "auth_logout must not call regenerate_session_id() -- it breaks "
        "starsessions' own store-cleanup path for an emptied session. See "
        "the comment in main.py::auth_logout."
    )
    assert "request.session.clear()" in source


# ---------------------------------------------------------------------------
# Shared fixture: a fresh app + isolated in-memory DB + isolated in-memory
# session store per test, with optional extra env vars set before import.
#
# Mirrors TestLocalLoginLockout.lockout_client (test_security_hardening.py):
# main.py builds its session store and reads DEMO_MODE/OIDC config at import
# time, so each test needs its own fresh import to get an independent,
# uncontaminated session store and to pick up per-test env vars.
# ---------------------------------------------------------------------------

def _fresh_app_client(monkeypatch, extra_env=None):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from shared.config import get_config

    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    _modules_to_evict = [
        name for name in sys.modules
        if name == "main" or name.startswith(("main.", "app.", "shared."))
    ]
    for name in _modules_to_evict:
        del sys.modules[name]
    get_config.cache_clear()

    import main as _main
    from app.database import Base, get_db as _get_db

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    _main.app.dependency_overrides[_get_db] = override_get_db
    client = TestClient(_main.app, raise_server_exceptions=False)
    return _main, client, TestingSessionLocal


class _FakeAuthentikClient:
    """Stand-in for the authlib registered OAuth client. Set directly as an
    instance attribute on main.oauth (bypassing OAuth.__getattr__'s registry
    lookup, which requires configure_oauth_client() -- never called in
    DEV_MODE) so auth_callback can be exercised without a real IdP."""

    def __init__(self, userinfo):
        self._userinfo = userinfo

    async def authorize_access_token(self, request, **kwargs):
        return {"access_token": "fake-oidc-access-token"}

    async def userinfo(self, token):
        return self._userinfo


# ---------------------------------------------------------------------------
# 2a. local_login
# ---------------------------------------------------------------------------

def test_local_login_rotates_cookie_and_old_id_never_gets_the_token(monkeypatch):
    from app.utils.passwords import hash_password

    _main, client, SessionLocal = _fresh_app_client(monkeypatch)
    db = SessionLocal()
    try:
        from app.models import User

        user = User(
            username="alice",
            email="alice@test.example",
            auth_provider="local",
            password_hash=hash_password("password1234", iterations=1000),
            active=True,
            role="viewer",
        )
        db.add(user)
        db.commit()
    finally:
        db.close()

    planted_id = "attacker-fixed-session-id"

    r = client.post(
        "/api/auth/local-login",
        json={"username": "alice", "password": "password1234"},
        cookies={"athena_session": planted_id},
    )
    assert r.status_code == 200, r.text

    set_cookie = r.headers.get("set-cookie", "")
    assert "athena_session=" in set_cookie
    new_id = client.cookies.get("athena_session")
    assert new_id is not None
    assert new_id != planted_id, (
        "local_login did not rotate the session id -- the attacker-planted "
        "cookie value survived authentication (session fixation)"
    )

    # The old, attacker-known id was never written to the store.
    assert planted_id not in _main.session_store.data

    # The old cookie must not resolve to an authenticated session.
    r_old = client.get("/api/auth/session-token", cookies={"athena_session": planted_id})
    assert r_old.status_code == 401, r_old.text

    # The new cookie does resolve.
    r_new = client.get("/api/auth/session-token", cookies={"athena_session": new_id})
    assert r_new.status_code == 200, r_new.text
    assert "token" in r_new.json()

    _main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 2b. demo-mode auth_login
# ---------------------------------------------------------------------------

def test_demo_mode_auth_login_rotates_cookie_and_old_id_never_gets_the_token(monkeypatch):
    _main, client, _ = _fresh_app_client(monkeypatch, extra_env={"DEMO_MODE": "true"})

    planted_id = "attacker-fixed-demo-session-id"

    r = client.get(
        "/api/auth/login",
        follow_redirects=False,
        cookies={"athena_session": planted_id},
    )
    assert r.status_code in (302, 307), r.text

    new_id = client.cookies.get("athena_session")
    assert new_id is not None
    assert new_id != planted_id, (
        "demo-mode auth_login did not rotate the session id -- the "
        "attacker-planted cookie value survived authentication"
    )
    assert planted_id not in _main.session_store.data

    r_old = client.get("/api/auth/session-token", cookies={"athena_session": planted_id})
    assert r_old.status_code == 401, r_old.text

    r_new = client.get("/api/auth/session-token", cookies={"athena_session": new_id})
    assert r_new.status_code == 200, r_new.text

    _main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 2c. OIDC auth_callback
# ---------------------------------------------------------------------------

def test_oidc_auth_callback_rotates_cookie_and_old_id_never_gets_the_token(monkeypatch):
    _main, client, _ = _fresh_app_client(monkeypatch)

    fake_userinfo = {
        "sub": "oidc-user-001",
        "email": "oidcuser@test.example",
        "preferred_username": "oidcuser",
        "name": "OIDC User",
    }
    monkeypatch.setattr(_main.oauth, "authentik", _FakeAuthentikClient(fake_userinfo), raising=False)

    planted_id = "attacker-fixed-oidc-session-id"

    r = client.get(
        "/api/auth/callback",
        params={"code": "fake-code", "state": "fake-state"},
        follow_redirects=False,
        cookies={"athena_session": planted_id},
    )
    assert r.status_code in (302, 307), r.text

    new_id = client.cookies.get("athena_session")
    assert new_id is not None
    assert new_id != planted_id, (
        "auth_callback did not rotate the session id -- the attacker-planted "
        "cookie value survived the OIDC login"
    )
    assert planted_id not in _main.session_store.data

    r_old = client.get("/api/auth/session-token", cookies={"athena_session": planted_id})
    assert r_old.status_code == 401, r_old.text

    r_new = client.get("/api/auth/session-token", cookies={"athena_session": new_id})
    assert r_new.status_code == 200, r_new.text

    _main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 3. Positive control: logout's existing clear()-only cleanup is sufficient
# ---------------------------------------------------------------------------

def test_logout_clear_alone_removes_store_entry_and_expires_cookie(monkeypatch):
    _main, client, _ = _fresh_app_client(monkeypatch, extra_env={"DEMO_MODE": "true"})

    r = client.get("/api/auth/login", follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    session_id = client.cookies.get("athena_session")
    assert session_id in _main.session_store.data

    r2 = client.get("/api/auth/logout", follow_redirects=False)
    assert r2.status_code in (302, 307), r2.text

    # The store entry that held the JWT is gone -- this is what would break
    # if regenerate_session_id() were added to auth_logout (see module
    # docstring and the comment in main.py::auth_logout).
    assert session_id not in _main.session_store.data

    r3 = client.get("/api/auth/session-token", cookies={"athena_session": session_id})
    assert r3.status_code == 401, r3.text

    _main.app.dependency_overrides.clear()
