"""ATHENA-80 -- session fixation: the session ID must rotate on authentication.

xander's finding: the admin-backend never rotated the starsessions session ID
on login. starsessions stores the session_id verbatim as the cookie value
(unsigned -- see starsessions.middleware.SessionMiddleware) and looks up
session data by that exact string, so a cookie value an attacker fixes in the
victim's browser *before* login remains the same cookie the victim's browser
sends *after* login. Without rotation, the victim's JWT gets written to the
store under that attacker-known id -- classic session fixation.

Fix: `app.utils.sessions.rotate_session_id(request)` is called immediately
after the session is populated on all three login paths: local_login
(app/routes/local_auth.py), demo-mode auth_login, and the OIDC auth_callback
(both in main.py). It wraps starsessions' `regenerate_session_id()` (2.2.1)
and additionally purges the pre-rotation id from the backing store.

codex diff review (2026-09-28) on the first cut of this fix (bare
regenerate_session_id(), no store purge): regenerate_session_id() only
repoints handler.session_id to a freshly generated id -- it never removes
the OLD entry from the store. Confirmed by reading the INSTALLED
starsessions 2.2.1 source directly (site-packages/starsessions/session.py:
regenerate_id() at lines 153-155 is exactly `self.session_id =
generate_session_id(); return self.session_id` -- no store write, no
removal; save() at 133-142 only ever writes under the NEW self.session_id;
there is no `_remove_data_for_session` or equivalent anywhere in the
installed package). This is a source-code fact, not something the tests
below can distinguish on their own -- a test can observe "the old id is
gone from the store" but can't tell whether the library did that on its
own or app.utils.sessions.rotate_session_id()'s manual store.remove() did
it; the file-level docstring in app/utils/sessions.py carries the citation
so a future reader checks the source rather than re-litigating this from
test behavior. A planted-but-empty attacker cookie is harmless either way
(nothing was ever written under it), but if the old id already held data
before this request -- e.g. an attacker-seeded session that was itself
live, or any other stale pre-existing entry -- that data remained valid
under its own TTL even after rotation. rotate_session_id() captures the
old id before calling regenerate_session_id(), then explicitly removes it
from the store.

xander (residual Medium, 2026-09-28): rotating a caller-supplied cookie
purges whatever session that id held, even if it belonged to a different,
already-authenticated identity (an attacker plants a victim's live cookie,
then logs in as themselves under it -- the victim gets force-logged-out as
a side effect). The purge must still happen (the fixation defense has to
win), but it should be visible: rotate_session_id() now reads the old
entry's user_id before removing it and, if it differs from the identity
that just authenticated, logs a structlog WARNING
(session_rotation_purged_foreign_identity) with both user ids and the
client IP -- never tokens. admin/backend's structlog is unconfigured
(structlog.is_configured() is False; no service calls
shared.logging_config.configure_logging() here), so it falls back to
structlog's default PrintLogger writing straight to stdout -- caplog
(which hooks stdlib logging) won't see it, so the tests below use
capsys.readouterr() instead. test_local_login_warns_on_cross_identity_purge
drives this through the real local_login endpoint (attacker authenticates
under a cookie seeded with a different, victim, user_id) and confirms the
token value itself never appears in the captured output;
test_local_login_same_identity_purge_does_not_warn is the negative control
-- a user re-authenticating under their OWN old id must not warn.

auth_logout is deliberately NOT touched (does not use rotate_session_id
either). Empirical check against starsessions 2.2.1 (see the file-level
comment above auth_logout in main.py) showed that adding
regenerate_session_id() there breaks the store cleanup: the
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
     drift-guard style) for the rotate_session_id call, in the right order
     relative to session population / clear().
  2. Functional TestClient tests per login path -- plant a known
     "attacker-fixed" session cookie, authenticate, and assert (a) the
     Set-Cookie carries a DIFFERENT athena_session value, (b) the OLD cookie
     value was never written to the session store, and (c)
     GET /api/auth/session-token returns 401 with the old cookie and 200 with
     the new one.
  3. Store-purge tests per login path (codex diff review) -- seed the store
     with an old session id that already holds an access_token (simulating a
     pre-existing/attacker-seeded live session under the planted id), plant
     that same cookie, authenticate, and assert the old id is gone from the
     store and 401s afterward.
  4. A positive control for logout: clear() alone still removes the store
     entry and expires the cookie (proving the "no rotate_session_id needed
     here" judgment call is correct, not just asserted).

Note on the OIDC test's realism (codex diff review, Low): the OIDC
functional tests below call /api/auth/callback directly with a pre-set
cookie, skipping the real /auth/login -> authorize_redirect step where
Authlib would normally write `_state_*` nonce data into the session under
that same incoming cookie first. That means the "old id was never written to
the store" assertion in those tests is true only because this synthetic setup
never wrote anything to the old id in the first place -- it would be too
strong a claim against a real OIDC flow, where the old id legitimately holds
OAuth state by the time the callback fires. The store-purge test for the
OIDC path (test_oidc_auth_callback_purges_preexisting_old_id_entry below)
covers the realistic case directly: it seeds the old id with data first (as
codex's own state-seeded design implies), so its "old id is gone from the
store" assertion holds for the correct reason -- rotate_session_id()
unconditionally purges whatever was there -- rather than by the test's setup
happening to have never written anything.
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
    assert "rotate_session_id(request)" in source, (
        "local_login must call rotate_session_id(request) after auth "
        "succeeds (ATHENA-80 session fixation)"
    )
    assert source.index("rotate_session_id(request)") > source.index(
        'request.session["access_token"] = token'
    ), "rotate_session_id must run AFTER the session is populated, not before"


def test_demo_mode_auth_login_rotates_session_id_after_populating_session():
    import main as _main

    source = inspect.getsource(_main.auth_login)
    assert "rotate_session_id(request)" in source, (
        "demo-mode auth_login must call rotate_session_id(request) after "
        "the demo session is populated (ATHENA-80 session fixation)"
    )
    assert source.index("rotate_session_id(request)") > source.index(
        "request.session['user_id'] = int(demo_user.id)"
    ), "rotate_session_id must run AFTER the session is populated, not before"


def test_oidc_auth_callback_rotates_session_id_after_populating_session():
    import main as _main

    source = inspect.getsource(_main.auth_callback)
    assert "rotate_session_id(request)" in source, (
        "auth_callback must call rotate_session_id(request) after the "
        "OIDC session is populated (ATHENA-80 session fixation)"
    )
    assert source.index("rotate_session_id(request)") > source.index(
        "request.session['user_id'] = user.id"
    ), "rotate_session_id must run AFTER the session is populated, not before"


def test_auth_logout_does_not_call_rotate_session_id():
    """Drift guard for the deliberate divergence from the literal brief.

    See the module docstring and the comment in main.py::auth_logout: calling
    regenerate_session_id() (which rotate_session_id wraps) after
    session.clear() breaks starsessions' own store cleanup (proven
    empirically by
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
    assert "rotate_session_id(request)" not in source, (
        "auth_logout must not call rotate_session_id() either -- same "
        "underlying reason. See the comment in main.py::auth_logout."
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
# 3. Store-purge tests (codex diff review, Medium): the OLD id must be
# removed from the backing store, not just orphaned by the handler pointing
# elsewhere. Seed the store directly with a pre-existing entry under the
# planted id -- as if it were an attacker-seeded live session, or any other
# stale entry -- and confirm rotate_session_id() burns it.
# ---------------------------------------------------------------------------

def test_local_login_purges_preexisting_old_id_entry(monkeypatch):
    import json

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

    seeded_id = "pre-existing-session-id"
    _main.session_store.data[seeded_id] = json.dumps({"access_token": "pre-existing-token"}).encode("utf-8")

    r = client.post(
        "/api/auth/local-login",
        json={"username": "alice", "password": "password1234"},
        cookies={"athena_session": seeded_id},
    )
    assert r.status_code == 200, r.text

    assert seeded_id not in _main.session_store.data, (
        "the pre-existing entry under the planted id must be purged, not just "
        "orphaned, after rotation"
    )

    r_old = client.get("/api/auth/session-token", cookies={"athena_session": seeded_id})
    assert r_old.status_code == 401, r_old.text

    _main.app.dependency_overrides.clear()


def test_demo_mode_auth_login_purges_preexisting_old_id_entry(monkeypatch):
    import json

    _main, client, _ = _fresh_app_client(monkeypatch, extra_env={"DEMO_MODE": "true"})

    seeded_id = "pre-existing-demo-session-id"
    _main.session_store.data[seeded_id] = json.dumps({"access_token": "pre-existing-token"}).encode("utf-8")

    r = client.get(
        "/api/auth/login",
        follow_redirects=False,
        cookies={"athena_session": seeded_id},
    )
    assert r.status_code in (302, 307), r.text

    assert seeded_id not in _main.session_store.data

    r_old = client.get("/api/auth/session-token", cookies={"athena_session": seeded_id})
    assert r_old.status_code == 401, r_old.text

    _main.app.dependency_overrides.clear()


def test_oidc_auth_callback_purges_preexisting_old_id_entry(monkeypatch):
    import json

    _main, client, _ = _fresh_app_client(monkeypatch)

    fake_userinfo = {
        "sub": "oidc-user-002",
        "email": "oidcuser2@test.example",
        "preferred_username": "oidcuser2",
        "name": "OIDC User Two",
    }
    monkeypatch.setattr(_main.oauth, "authentik", _FakeAuthentikClient(fake_userinfo), raising=False)

    # Simulates the realistic case codex flagged: the old id already holds
    # data by the time /callback runs (in a real flow this would be Authlib's
    # `_state_*` OAuth nonce, written by the earlier /auth/login ->
    # authorize_redirect step). rotate_session_id() must purge it regardless
    # of what it holds.
    seeded_id = "pre-existing-oidc-session-id"
    _main.session_store.data[seeded_id] = json.dumps({"access_token": "pre-existing-token"}).encode("utf-8")

    r = client.get(
        "/api/auth/callback",
        params={"code": "fake-code", "state": "fake-state"},
        follow_redirects=False,
        cookies={"athena_session": seeded_id},
    )
    assert r.status_code in (302, 307), r.text

    assert seeded_id not in _main.session_store.data

    r_old = client.get("/api/auth/session-token", cookies={"athena_session": seeded_id})
    assert r_old.status_code == 401, r_old.text

    _main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 4. Cross-identity purge warning (xander, residual Medium): purging the old
# id must still happen even when it belonged to a different, already-
# authenticated user, but it must be logged. admin/backend's structlog is
# unconfigured here (falls back to PrintLogger -> stdout), so these use
# capsys rather than caplog -- see the module docstring.
# ---------------------------------------------------------------------------

def test_local_login_warns_on_cross_identity_purge(monkeypatch, capsys):
    import json

    from app.utils.passwords import hash_password

    _main, client, SessionLocal = _fresh_app_client(monkeypatch)
    db = SessionLocal()
    try:
        from app.models import User

        victim = User(
            username="victim",
            email="victim@test.example",
            auth_provider="local",
            password_hash=hash_password("victim-password", iterations=1000),
            active=True,
            role="viewer",
        )
        attacker = User(
            username="attacker",
            email="attacker@test.example",
            auth_provider="local",
            password_hash=hash_password("attacker-password", iterations=1000),
            active=True,
            role="viewer",
        )
        db.add_all([victim, attacker])
        db.commit()
        db.refresh(victim)
        db.refresh(attacker)
        victim_id = victim.id
        attacker_id = attacker.id
    finally:
        db.close()

    assert victim_id != attacker_id

    # Simulates the victim's own live, authenticated session under a cookie
    # the attacker has planted in their own browser.
    shared_cookie = "victim-live-session-id"
    _main.session_store.data[shared_cookie] = json.dumps(
        {"user_id": victim_id, "access_token": "victim-real-jwt-do-not-leak"}
    ).encode("utf-8")

    capsys.readouterr()  # drain any import/startup noise before the request

    r = client.post(
        "/api/auth/local-login",
        json={"username": "attacker", "password": "attacker-password"},
        cookies={"athena_session": shared_cookie},
    )
    assert r.status_code == 200, r.text

    # The fixation defense still wins -- the victim's old id is gone either way.
    assert shared_cookie not in _main.session_store.data

    captured = capsys.readouterr()
    assert "session_rotation_purged_foreign_identity" in captured.out, (
        "cross-identity purge must log a warning"
    )
    assert str(victim_id) in captured.out
    assert str(attacker_id) in captured.out
    assert "victim-real-jwt-do-not-leak" not in captured.out, (
        "the warning must never log token values"
    )

    _main.app.dependency_overrides.clear()


def test_local_login_same_identity_purge_does_not_warn(monkeypatch, capsys):
    """Negative control: a user re-authenticating under their OWN old id
    (e.g. re-login after a session refresh) must not trigger the
    cross-identity warning -- only a genuine identity mismatch should."""
    import json

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
        db.refresh(user)
        user_id = user.id
    finally:
        db.close()

    own_old_cookie = "alices-own-previous-session-id"
    _main.session_store.data[own_old_cookie] = json.dumps(
        {"user_id": user_id, "access_token": "alices-own-earlier-jwt"}
    ).encode("utf-8")

    capsys.readouterr()

    r = client.post(
        "/api/auth/local-login",
        json={"username": "alice", "password": "password1234"},
        cookies={"athena_session": own_old_cookie},
    )
    assert r.status_code == 200, r.text
    assert own_old_cookie not in _main.session_store.data

    captured = capsys.readouterr()
    assert "session_rotation_purged_foreign_identity" not in captured.out, (
        "re-authenticating under one's own old id must not warn"
    )

    _main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 5. Positive control: logout's existing clear()-only cleanup is sufficient
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
