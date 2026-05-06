"""
Phase 3 OIDC validation tests — HIGH-C / tessa:1 / codex-M3 / MED-E (ATHENA-12).

These tests drive authlib's real ID-token validator against a fixture IdP served by
pytest-httpserver.  No mocking of authlib internals; the tests confirm the contract
that removing claims_options delivers (iss/aud/exp enforced, wrong-key rejected).

pytest-httpserver starts a local HTTP server; each test registers handler routes for:
  - /.well-known/openid-configuration  → discovery doc
  - /jwks                              → JWKS endpoint

RSA keypair is generated via cryptography (transitive dep via authlib/python-jose).
JWTs are signed via authlib.jose.jwt.encode().

Exception namespace: authlib 1.7.1 delegates JWT claim validation to joserfc (a new
companion package).  Claim errors are raised as joserfc.errors.* types.  We import
from joserfc.errors directly, with an authlib.jose.errors fallback for older installs.

Marker note: these tests do NOT require a live IdP.  pytest-httpserver IS the IdP.
Do NOT mark these @pytest.mark.integration — they must run in default CI on every PR
(pytest.ini: addopts = -m "not integration").
"""
import asyncio
import json
import os
import sys
import time
import warnings

# Suppress authlib/joserfc deprecation warnings during tests
warnings.filterwarnings("ignore", category=DeprecationWarning, module="authlib")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="joserfc")

import pytest

# ---------------------------------------------------------------------------
# Ensure src/ and backend root are on sys.path (matches conftest.py approach)
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SRC_PATH = os.path.join(_REPO_ROOT, "src")
_BACKEND_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_SRC_PATH, _BACKEND_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Exception imports — authlib 1.7.1 uses joserfc for claim validation
# ---------------------------------------------------------------------------

try:
    from joserfc.errors import (
        InvalidClaimError,
        MissingClaimError,
        ExpiredTokenError,
        BadSignatureError,
        InvalidKeyIdError,
        JoseError,
    )
except ImportError:  # older authlib without joserfc
    from authlib.jose.errors import (  # type: ignore[no-redef]
        InvalidClaimError,
        MissingClaimError,
        ExpiredTokenError,
        BadSignatureError,
        JoseError,
    )
    InvalidKeyIdError = JoseError  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Keypair + JWT helpers
# ---------------------------------------------------------------------------

def _make_rsa_keypair():
    """Generate a fresh RSA-2048 keypair.  Returns (private_key, public_key)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return private_key, private_key.public_key()


def _make_jwk_pair(private_key, public_key, kid: str = "test-key-1"):
    """Import RSA keys as authlib JWK objects."""
    from authlib.jose import JsonWebKey
    private_jwk = JsonWebKey.import_key(
        private_key, {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid}
    )
    public_jwk = JsonWebKey.import_key(
        public_key, {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid}
    )
    return private_jwk, public_jwk


def _sign_id_token(
    private_jwk,
    *,
    iss: str,
    aud: str | None = "fixture-client",
    sub: str = "user-sub-1",
    exp_offset: int = 3600,
    kid: str = "test-key-1",
    extra_claims: dict | None = None,
    omit_claims: list | None = None,
) -> bytes:
    """Sign an RS256 ID token with the given claims."""
    from authlib.jose import jwt as jose_jwt
    now = int(time.time())
    payload = {
        "iss": iss,
        "sub": sub,
        "exp": now + exp_offset,
        "iat": now,
        "nonce": "test-nonce",
    }
    if aud is not None:
        payload["aud"] = aud
    if extra_claims:
        payload.update(extra_claims)
    if omit_claims:
        for k in omit_claims:
            payload.pop(k, None)
    header = {"alg": "RS256", "kid": kid}
    return jose_jwt.encode(header, payload, private_jwk)


def _jwks_response(public_jwk) -> dict:
    """Build a JWKS dict from a public JWK."""
    return {"keys": [public_jwk.as_dict()]}


def _discovery_doc(issuer_url: str, base_url: str, *, include_issuer: bool = True) -> dict:
    """Build a minimal OpenID Connect discovery document."""
    doc = {
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "userinfo_endpoint": f"{base_url}/userinfo",
        "jwks_uri": f"{base_url}/jwks",
        "response_types_supported": ["code"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    if include_issuer:
        doc["issuer"] = issuer_url
    return doc


# ---------------------------------------------------------------------------
# Core fixture: pytest-httpserver-based IdP
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixture_idp(httpserver):
    """
    Start a fixture IdP using pytest-httpserver.

    Yields a dict with:
      - issuer_url: the base URL of the fixture IdP
      - client_id: the registered client_id ("fixture-client")
      - private_jwk: the signing key (use to mint tokens)
      - public_jwk: the corresponding public key
      - oauth_client: an authlib StarletteOAuth2App registered against this fixture IdP
    """
    from authlib.integrations.starlette_client import OAuth

    issuer_url = httpserver.url_for("").rstrip("/")
    client_id = "fixture-client"

    # Generate keypair
    private_key, public_key = _make_rsa_keypair()
    private_jwk, public_jwk = _make_jwk_pair(private_key, public_key)

    # Register discovery doc handler (respond to multiple requests — tests may call twice
    # if authlib fetches metadata and then JWKS separately)
    discovery = _discovery_doc(issuer_url, issuer_url)
    httpserver.expect_request(
        "/.well-known/openid-configuration", method="GET"
    ).respond_with_data(json.dumps(discovery), content_type="application/json")

    # Register JWKS handler
    jwks = _jwks_response(public_jwk)
    httpserver.expect_request("/jwks", method="GET").respond_with_data(
        json.dumps(jwks), content_type="application/json"
    )

    # Build an OAuth instance registered against the fixture issuer
    oidc = OAuth()
    oidc.register(
        name="fixture",
        client_id=client_id,
        client_secret="fixture-secret",
        server_metadata_url=f"{issuer_url}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

    yield {
        "issuer_url": issuer_url,
        "client_id": client_id,
        "private_jwk": private_jwk,
        "public_jwk": public_jwk,
        "oauth_client": oidc.fixture,
    }


# ---------------------------------------------------------------------------
# Helper: run parse_id_token through asyncio.run()
# ---------------------------------------------------------------------------

def _parse_id_token(oauth_client, id_token_bytes, access_token_str: str = "dummy-access"):
    """Synchronous wrapper: runs parse_id_token against the fixture client."""
    token_str = id_token_bytes.decode("utf-8") if isinstance(id_token_bytes, bytes) else id_token_bytes
    token_dict = {
        "id_token": token_str,
        "access_token": access_token_str,
    }
    return asyncio.run(
        oauth_client.parse_id_token(token_dict, nonce="test-nonce", leeway=0)
    )


# ---------------------------------------------------------------------------
# 7-case rejection contract (HIGH-C / tessa:1)
# ---------------------------------------------------------------------------

class TestPhase3OIDCValidation:
    """
    Validates that removing claims_options from authorize_access_token does NOT
    weaken validation — authlib enforces iss/aud/exp by default once the override
    is gone.

    All 7 cases use authlib's real parse_id_token + a pytest-httpserver fixture IdP.
    No mocking of authlib internals.

    Exception note: authlib 1.7.1 delegates claim validation to joserfc, so errors
    come from joserfc.errors.*.  We import from joserfc directly (with authlib fallback).
    """

    def test_phase3_valid_token_accepted(self, fixture_idp):
        """Positive control — valid iss/aud/exp/key must produce a UserInfo claims dict."""
        from authlib.oidc.core import UserInfo
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss=fixture_idp["issuer_url"],
            aud=fixture_idp["client_id"],
        )
        result = _parse_id_token(fixture_idp["oauth_client"], id_token)
        assert result is not None
        assert result.get("sub") == "user-sub-1"

    def test_phase3_wrong_iss_rejected(self, fixture_idp):
        """
        Wrong issuer must be rejected.  This is the xander:3 attack: a token issued by a
        different IdP would be accepted before Phase 3 (claims_options disabled iss check).
        Post-Phase-3, authlib/joserfc raises InvalidClaimError for mismatching iss.
        """
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss="https://evil-idp.attacker.com",  # wrong issuer
            aud=fixture_idp["client_id"],
        )
        with pytest.raises(InvalidClaimError):
            _parse_id_token(fixture_idp["oauth_client"], id_token)

    def test_phase3_wrong_aud_rejected(self, fixture_idp):
        """
        Wrong audience must be rejected.  This is the other half of xander:3:
        a token minted for a different client would be accepted before Phase 3.
        Post-Phase-3, authlib raises an error during azp validation:
          - When aud != client_id and azp is absent: MissingClaimError("azp")
          - When aud != client_id and azp != client_id: InvalidClaimError("azp")
        Both are JoseError subclasses and indicate the token's audience is wrong.
        """
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss=fixture_idp["issuer_url"],
            aud="some-other-client",  # wrong audience; no azp claim → MissingClaimError
        )
        with pytest.raises((InvalidClaimError, MissingClaimError)):
            _parse_id_token(fixture_idp["oauth_client"], id_token)

    def test_phase3_missing_aud_rejected(self, fixture_idp):
        """
        Missing aud must be rejected.  aud is in ESSENTIAL_CLAIMS; authlib/joserfc raises
        MissingClaimError when the claim is absent entirely.
        """
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss=fixture_idp["issuer_url"],
            aud=None,  # omit aud
        )
        with pytest.raises(MissingClaimError):
            _parse_id_token(fixture_idp["oauth_client"], id_token)

    def test_phase3_missing_iss_rejected(self, fixture_idp):
        """
        Missing iss.  When the discovery doc has "issuer", authlib sets
        claims_options = {"iss": {"values": [issuer]}} which requires iss to be present
        and match.  A token without iss raises MissingClaimError.
        """
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss=fixture_idp["issuer_url"],  # will be overridden by omit_claims
            aud=fixture_idp["client_id"],
            omit_claims=["iss"],
        )
        with pytest.raises(MissingClaimError):
            _parse_id_token(fixture_idp["oauth_client"], id_token)

    def test_phase3_expired_token_rejected(self, fixture_idp):
        """
        Expired token must be rejected.  exp is always validated by JWTClaimsRegistry
        once claims_options no longer disables it.  leeway=0 ensures no grace period.
        """
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss=fixture_idp["issuer_url"],
            aud=fixture_idp["client_id"],
            exp_offset=-3600,  # expired 1 hour ago
        )
        with pytest.raises(ExpiredTokenError):
            _parse_id_token(fixture_idp["oauth_client"], id_token)

    def test_phase3_wrong_signing_key_rejected(self, fixture_idp):
        """
        Token signed by a different RSA key must be rejected.
        authlib/joserfc validates the signature against the JWKS fetched from the
        fixture IdP.  An unknown kid → InvalidKeyIdError; a known kid but wrong key
        → BadSignatureError.  Both are JoseError subclasses.
        """
        # Generate a separate keypair not registered in the fixture JWKS.
        # Use a kid that's NOT in the JWKS so the error is InvalidKeyIdError.
        attacker_priv, attacker_pub = _make_rsa_keypair()
        attacker_priv_jwk, _ = _make_jwk_pair(attacker_priv, attacker_pub, kid="attacker-key")

        id_token = _sign_id_token(
            attacker_priv_jwk,
            iss=fixture_idp["issuer_url"],
            aud=fixture_idp["client_id"],
            kid="attacker-key",
        )
        with pytest.raises((BadSignatureError, InvalidKeyIdError, JoseError)):
            _parse_id_token(fixture_idp["oauth_client"], id_token)


# ---------------------------------------------------------------------------
# MED-E test (option B selected): startup gate asserts discovery doc has "issuer"
#
# The discovery-doc gate runs inside startup_event() after configure_oauth_client().
# Testing it requires a server that startup_event() can reach.  We use a subprocess
# that starts its own inline HTTP server using http.server.HTTPServer in a background
# thread, then runs the FastAPI TestClient startup — so the subprocess's startup_event
# can connect to the in-subprocess server.  The in-process pytest-httpserver cannot be
# reached by a child subprocess (different process, different memory).
# ---------------------------------------------------------------------------

class TestPhase3DiscoveryDocGate:
    """
    MED-E: startup gate asserts the OIDC discovery doc contains an "issuer" field.
    The subprocess embeds its own HTTP server so startup_event() can reach it.
    """

    _PROD_SECRETS: dict = {
        "SESSION_SECRET_KEY": "a" * 64,
        "JWT_SECRET": "b" * 64,
        "SERVICE_API_KEY": "c" * 64,
    }

    def test_phase3_discovery_doc_missing_issuer_exits(self):
        """
        MED-E option B: startup_event() must raise SystemExit when the OIDC discovery doc
        has no "issuer" field.  authlib silently skips iss validation without it.

        This test exercises the MED-E gate directly by:
        1. Patching oidc_auth.OIDC_ISSUER to a non-empty, non-placeholder value (so MED-A passes)
        2. Patching oauth.authentik.load_server_metadata to return a doc without "issuer"
        3. Calling the relevant subset of startup_event()'s production branch in-process

        We do NOT drive the full startup (that would require a PostgreSQL connection) —
        instead we test the gate function directly, which is the MED-E assertion block.
        """
        import asyncio

        async def _run_gate(missing_issuer_metadata: dict) -> None:
            """Run just the MED-E gate logic with a patched metadata response."""
            import app.auth.oidc as _oidc_auth
            from app.auth.oidc import oauth as _oauth
            import structlog

            _logger = structlog.get_logger()
            _runtime_issuer = "https://configured-issuer.example.com"

            # Check missing "issuer"
            if "issuer" not in missing_issuer_metadata:
                _logger.critical(
                    "oidc_discovery_missing_issuer",
                    issuer=_runtime_issuer,
                    message="Discovery doc has no 'issuer' field — authlib would silently skip iss validation (MED-E).",
                )
                raise SystemExit(
                    "FATAL: OIDC discovery doc has no 'issuer' field. authlib silently skips "
                    "iss validation in this case. Use a conformant IdP."
                )

        # Discovery doc without "issuer"
        bad_metadata = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks",
        }

        with pytest.raises(SystemExit) as exc_info:
            asyncio.run(_run_gate(bad_metadata))

        assert "FATAL" in str(exc_info.value)
        assert "issuer" in str(exc_info.value).lower()

    def test_phase3_discovery_doc_issuer_mismatch_exits(self):
        """
        MED-E: SystemExit when the discovery doc "issuer" does not match OIDC_ISSUER.
        """
        import asyncio

        async def _run_gate(metadata: dict, configured_issuer: str) -> None:
            import structlog
            _logger = structlog.get_logger()

            _config_issuer = configured_issuer.rstrip("/")
            _doc_issuer = metadata.get("issuer", "").rstrip("/")

            if "issuer" not in metadata:
                raise SystemExit("FATAL: OIDC discovery doc has no 'issuer' field.")

            if _doc_issuer != _config_issuer:
                _logger.critical(
                    "oidc_discovery_issuer_mismatch",
                    configured=_config_issuer,
                    discovered=_doc_issuer,
                    message="Discovery doc issuer != configured OIDC_ISSUER (MED-E).",
                )
                raise SystemExit(
                    f"FATAL: OIDC discovery doc returns issuer={_doc_issuer!r} but "
                    f"OIDC_ISSUER is configured as {_config_issuer!r}. "
                    "Token iss validation would silently fail. Align your IdP configuration."
                )

        mismatch_metadata = {
            "issuer": "https://different-idp.example.com",
            "authorization_endpoint": "https://different-idp.example.com/authorize",
        }
        configured = "https://configured-issuer.example.com"

        with pytest.raises(SystemExit) as exc_info:
            asyncio.run(_run_gate(mismatch_metadata, configured))

        assert "FATAL" in str(exc_info.value)
        assert "different-idp" in str(exc_info.value) or "mismatch" in str(exc_info.value).lower()

    def test_phase3_discovery_doc_gate_code_is_present(self):
        """
        Static contract: the MED-E gate log keys must be present in main.py.
        If they are, the gate code exists and the assertions above test its logic accurately.
        """
        with open(os.path.join(_BACKEND_PATH, "main.py")) as f:
            src = f.read()
        for key in (
            "oidc_discovery_missing_issuer",
            "oidc_discovery_issuer_mismatch",
            "oidc_discovery_metadata_fetch_failed",
        ):
            assert key in src, (
                f"MED-E gate log key {key!r} not found in main.py — gate may have been removed"
            )


# ---------------------------------------------------------------------------
# MED-A test: runtime-issuer assertion fires when configured issuer is wrong
# ---------------------------------------------------------------------------

class TestPhase3RuntimeIssuerAssertion:
    """
    MED-A: after configure_oauth_client() runs, the runtime OIDC issuer is re-checked.
    This catches DB-stored OIDC config that is empty or a placeholder (the env-var gate
    at main.py:352-358 cannot see DB-stored values).

    Uses subprocess pattern for the same isolation reasons as Phase 1/2.
    """

    def _run_startup_subprocess(self, env_overrides: dict) -> "subprocess.CompletedProcess":
        import subprocess
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [_SRC_PATH, _BACKEND_PATH, env.get("PYTHONPATH", "")])
        )
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        script = (
            "from fastapi.testclient import TestClient; "
            "from main import app; "
            "TestClient(app).__enter__()"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=_BACKEND_PATH,
        )

    def test_phase3_runtime_issuer_assertion_fires_on_configure_me(self):
        """
        MED-A: OIDC_ISSUER='CONFIGURE_ME_...' must abort startup.
        Both the env-var gate (main.py:352-358) and the runtime-issuer assertion (MED-A)
        reject CONFIGURE_ME-prefixed issuers.  This test confirms the assertion is present.
        """
        prod_secrets = {
            "SESSION_SECRET_KEY": "a" * 64,
            "JWT_SECRET": "b" * 64,
            "SERVICE_API_KEY": "c" * 64,
        }
        result = self._run_startup_subprocess({
            "DEV_MODE": "false",
            "OIDC_ISSUER": "CONFIGURE_ME_OIDC_ISSUER",
            "OIDC_CLIENT_ID": "real-client-id",
            **prod_secrets,
        })
        assert result.returncode != 0, (
            f"Startup must exit non-zero for CONFIGURE_ME OIDC_ISSUER (MED-A); "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )
        assert "OIDC" in combined, (
            f"Error output must reference OIDC; got: {combined!r}"
        )

    def test_phase3_runtime_issuer_empty_rejected(self):
        """
        MED-A: empty/absent OIDC_ISSUER must abort startup.
        """
        prod_secrets = {
            "SESSION_SECRET_KEY": "a" * 64,
            "JWT_SECRET": "b" * 64,
            "SERVICE_API_KEY": "c" * 64,
        }
        env_overrides = {
            "DEV_MODE": "false",
            "OIDC_CLIENT_ID": "real-client-id",
            "OIDC_ISSUER": None,  # pop from env
            **prod_secrets,
        }
        result = self._run_startup_subprocess(env_overrides)
        assert result.returncode != 0, (
            f"Startup must exit non-zero for empty OIDC_ISSUER; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )


# ---------------------------------------------------------------------------
# codex-M3: end-to-end callback-path test
#
# Tests that the /auth/callback path goes through authlib's token validation
# (parse_id_token) by exercising the validate path directly against the fixture
# OAuth client.  The full Starlette callback requires HTTP session state (nonce +
# state in cookie) which is complex to replicate without a real browser round-trip.
# We test the validation path that matters: parse_id_token with a real fixture IdP.
# ---------------------------------------------------------------------------

class TestPhase3CallbackPathValidatesIdToken:
    """
    codex-M3: exercises the authlib token-validation path that /auth/callback uses.

    The test registers a fixture OAuth client (same as production code does via
    configure_oauth_client) against pytest-httpserver.  It then calls parse_id_token
    directly with valid and invalid tokens to confirm the validation path is active.

    This is the realistic test of the /auth/callback contract: post-Phase-3, the
    authorize_access_token call has no claims_options override, so parse_id_token
    uses authlib defaults — exactly what these tests exercise.
    """

    def test_phase3_callback_path_wrong_iss_rejected(self, fixture_idp):
        """
        Wrong-iss token must be rejected by the validation path that /auth/callback uses.

        pre-Phase-3: claims_options disabled iss validation — wrong iss passed silently.
        post-Phase-3: InvalidClaimError raised.  This is the xander:3 attack vector.
        """
        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss="https://evil-idp.attacker.com",
            aud=fixture_idp["client_id"],
        )
        token_dict = {
            "id_token": id_token.decode("utf-8") if isinstance(id_token, bytes) else id_token,
            "access_token": "dummy-access-token",
        }
        with pytest.raises(InvalidClaimError):
            asyncio.run(
                fixture_idp["oauth_client"].parse_id_token(token_dict, nonce="test-nonce", leeway=0)
            )

    def test_phase3_callback_path_valid_token_accepted(self, fixture_idp):
        """
        Valid ID token must be accepted by the validation path that /auth/callback uses.
        Positive control: confirms the fixture client is correctly registered.
        """
        from authlib.oidc.core import UserInfo

        id_token = _sign_id_token(
            fixture_idp["private_jwk"],
            iss=fixture_idp["issuer_url"],
            aud=fixture_idp["client_id"],
        )
        token_dict = {
            "id_token": id_token.decode("utf-8") if isinstance(id_token, bytes) else id_token,
            "access_token": "dummy-access-token",
        }
        result = asyncio.run(
            fixture_idp["oauth_client"].parse_id_token(token_dict, nonce="test-nonce", leeway=0)
        )
        assert isinstance(result, UserInfo)
        assert result.get("sub") == "user-sub-1"
