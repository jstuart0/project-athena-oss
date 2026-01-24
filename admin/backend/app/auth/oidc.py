"""
OIDC (OpenID Connect) authentication with Authentik.

Provides SSO integration for Athena Admin interface using Authentik
as the identity provider.

Configuration is loaded from database (secrets table) on startup,
with fallback to environment variables if database is unavailable.

DEV_MODE Support:
    When DEV_MODE=true, authentication is bypassed and a mock admin user
    is automatically returned for all authenticated endpoints.
"""
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Request, HTTPException, status, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
import structlog

from app.database import get_db, DEV_MODE
from app.models import User, Secret, UserAPIKey
from app.utils.encryption import decrypt_value
from app.utils.api_keys import verify_api_key, extract_key_prefix, is_valid_key_format

logger = structlog.get_logger()

# DEV_MODE check
if DEV_MODE:
    logger.info("oidc_dev_mode_enabled", message="Authentication will be bypassed")

# Default OIDC configuration from environment (fallback)
DEFAULT_OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
DEFAULT_OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
DEFAULT_OIDC_ISSUER = os.getenv("OIDC_ISSUER", "http://localhost:9000/application/o/athena-admin/")
DEFAULT_OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "http://localhost:8080/auth/callback")
DEFAULT_OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid profile email")

# Active OIDC configuration (set during startup)
OIDC_CLIENT_ID = DEFAULT_OIDC_CLIENT_ID
OIDC_CLIENT_SECRET = DEFAULT_OIDC_CLIENT_SECRET
OIDC_ISSUER = DEFAULT_OIDC_ISSUER
OIDC_REDIRECT_URI = DEFAULT_OIDC_REDIRECT_URI
OIDC_SCOPES = DEFAULT_OIDC_SCOPES

# Session configuration
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "")  # Must be set in production
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(60 * 60 * 8)))  # 8 hours default

# JWT configuration for internal tokens
JWT_SECRET = os.getenv("JWT_SECRET", SESSION_SECRET_KEY)
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION = int(os.getenv("JWT_EXPIRATION", str(60 * 60 * 8)))  # 8 hours

# Security
security = HTTPBearer()

# OAuth client configuration (configured during startup)
oauth = OAuth()


def load_oidc_config_from_db() -> Dict[str, str]:
    """
    Load OIDC configuration from database.

    Returns:
        Dict with OIDC configuration loaded from database,
        or empty dict if database is unavailable.
    """
    try:
        # Create database connection
        database_url = os.getenv("DATABASE_URL", "postgresql://psadmin@localhost:5432/athena_admin")
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # Query secrets table for OIDC configuration
            from sqlalchemy import text

            secrets = {}
            for secret_name in ['oidc_provider_url', 'oidc_client_id', 'oidc_client_secret', 'oidc_redirect_uri']:
                result = conn.execute(
                    text("SELECT encrypted_value FROM secrets WHERE service_name = :name"),
                    {"name": secret_name}
                ).fetchone()

                if result:
                    # Decrypt the value
                    encrypted_value = result[0]
                    decrypted_value = decrypt_value(encrypted_value)
                    secrets[secret_name] = decrypted_value

            if secrets:
                logger.info(
                    "oidc_config_loaded_from_database",
                    has_provider=bool(secrets.get('oidc_provider_url')),
                    has_client_id=bool(secrets.get('oidc_client_id')),
                    has_secret=bool(secrets.get('oidc_client_secret')),
                    has_redirect=bool(secrets.get('oidc_redirect_uri'))
                )
                return secrets
            else:
                logger.info("oidc_config_not_in_database_using_env_fallback")
                return {}

    except Exception as e:
        logger.warning("oidc_config_db_load_failed_using_env_fallback", error=str(e))
        return {}


def configure_oauth_client():
    """
    Configure OAuth client with OIDC settings from database or environment.

    This should be called during application startup after database is available.
    """
    global OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER, OIDC_REDIRECT_URI, OIDC_SCOPES

    # Try loading from database first
    db_config = load_oidc_config_from_db()

    if db_config:
        # Use database configuration
        OIDC_ISSUER = db_config.get('oidc_provider_url', DEFAULT_OIDC_ISSUER)
        OIDC_CLIENT_ID = db_config.get('oidc_client_id', DEFAULT_OIDC_CLIENT_ID)
        OIDC_CLIENT_SECRET = db_config.get('oidc_client_secret', DEFAULT_OIDC_CLIENT_SECRET)
        OIDC_REDIRECT_URI = db_config.get('oidc_redirect_uri', DEFAULT_OIDC_REDIRECT_URI)
        logger.info("oidc_using_database_configuration")
    else:
        # Use environment variable defaults
        OIDC_CLIENT_ID = DEFAULT_OIDC_CLIENT_ID
        OIDC_CLIENT_SECRET = DEFAULT_OIDC_CLIENT_SECRET
        OIDC_ISSUER = DEFAULT_OIDC_ISSUER
        OIDC_REDIRECT_URI = DEFAULT_OIDC_REDIRECT_URI
        logger.info("oidc_using_environment_configuration")

    # Register OAuth client with active configuration
    oauth.register(
        name='authentik',
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f'{OIDC_ISSUER}.well-known/openid-configuration',
        client_kwargs={
            'scope': OIDC_SCOPES,
        }
    )

    logger.info(
        "oauth_client_configured",
        issuer=OIDC_ISSUER,
        client_id_set=bool(OIDC_CLIENT_ID),
        client_secret_set=bool(OIDC_CLIENT_SECRET)
    )


async def get_authentik_userinfo(access_token: str) -> Dict[str, Any]:
    """
    Get user information from Authentik using access token.

    Args:
        access_token: OAuth2 access token from Authentik

    Returns:
        Dict containing user information (sub, email, name, groups, etc.)

    Raises:
        HTTPException: If token is invalid or userinfo request fails
    """
    try:
        async with httpx.AsyncClient() as client:
            # Construct userinfo URL from issuer
            # OIDC_ISSUER is like: https://auth.example.com/application/o/athena/
            # Userinfo endpoint is: https://auth.example.com/application/o/userinfo/
            issuer_base = OIDC_ISSUER.rstrip('/')
            # Extract base URL (remove application-specific path)
            if '/application/o/' in issuer_base:
                base_url = issuer_base.split('/application/o/')[0]
                userinfo_url = f"{base_url}/application/o/userinfo/"
            else:
                # Fallback: try to get from OIDC discovery
                userinfo_url = f"{issuer_base}/userinfo"

            logger.debug("fetching_userinfo", userinfo_url=userinfo_url)
            response = await client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error("authentik_userinfo_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to fetch user information from Authentik"
        )


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT access token for internal use.

    Args:
        data: Dictionary of claims to include in token
        expires_delta: Optional expiration time delta

    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(seconds=JWT_EXPIRATION))
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})

    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT access token.

    Args:
        token: JWT token string

    Returns:
        Dictionary of decoded claims

    Raises:
        HTTPException: If token is invalid or expired
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as e:
        logger.warning("jwt_decode_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_or_create_user(db: Session, userinfo: Dict[str, Any]) -> User:
    """
    Get existing user or create new user from Authentik userinfo.

    Args:
        db: Database session
        userinfo: User information from Authentik

    Returns:
        User object

    Note:
        Default role is 'viewer'. Admins should manually promote users to
        'operator' or 'owner' roles through database or future UI.
    """
    authentik_id = userinfo.get('sub')
    email = userinfo.get('email')
    username = userinfo.get('preferred_username') or email.split('@')[0]
    full_name = userinfo.get('name', '')

    # Check if user exists
    user = db.query(User).filter(User.authentik_id == authentik_id).first()

    if user:
        # Update last login
        user.last_login = datetime.utcnow()
        user.email = email  # Update email in case it changed
        user.full_name = full_name
        db.commit()
        db.refresh(user)
        logger.info("user_login", user_id=user.id, username=user.username)
    else:
        # Create new user with viewer role
        user = User(
            authentik_id=authentik_id,
            username=username,
            email=email,
            full_name=full_name,
            role='viewer',  # Default role
            active=True,
            last_login=datetime.utcnow()
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("user_created", user_id=user.id, username=user.username, role=user.role)

    return user


class OptionalHTTPBearer(HTTPBearer):
    """HTTPBearer that doesn't fail when no token - allows API key fallback."""

    async def __call__(self, request: Request) -> Optional[HTTPAuthorizationCredentials]:
        try:
            return await super().__call__(request)
        except HTTPException:
            # No Bearer token - that's OK, we might have an API key
            return None


# Always use optional security to allow API key authentication
optional_security = OptionalHTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(optional_security),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    request: Request = None,
) -> User:
    """
    FastAPI dependency to get current authenticated user.

    Supports multiple authentication methods (in order of precedence):
    1. DEV_MODE bypass - Returns mock admin for local development
    2. API Key (X-API-Key header) - For programmatic access
    3. JWT (Authorization: Bearer) - For browser sessions via OIDC

    Args:
        credentials: HTTP Bearer token from request (optional in DEV_MODE)
        x_api_key: API key from X-API-Key header (optional)
        db: Database session
        request: FastAPI request object for storing auth metadata

    Returns:
        Current authenticated User object

    Raises:
        HTTPException: If authentication fails
    """
    # DEV_MODE: Return mock admin user without authentication (UNCHANGED)
    if DEV_MODE:
        dev_user = db.query(User).filter(User.username == "dev-admin").first()
        if dev_user:
            logger.debug("dev_mode_auth_bypass", user="dev-admin")
            return dev_user

        dev_user = User(
            authentik_id="dev-admin-001",
            username="dev-admin",
            email="dev-admin@localhost",
            full_name="Development Admin",
            role="owner",
            active=True,
            last_login=datetime.utcnow()
        )
        db.add(dev_user)
        db.commit()
        db.refresh(dev_user)
        logger.info("dev_mode_user_created", user="dev-admin")
        return dev_user

    # NEW: Try API Key authentication first
    if x_api_key:
        user = await _authenticate_api_key(x_api_key, db, request)
        if user:
            return user
        # If API key was provided but invalid, don't fall through to JWT
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # EXISTING: JWT authentication (UNCHANGED)
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_access_token(token)

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload"
        )

    user = db.query(User).filter(User.id == user_id, User.active == True).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive"
        )

    # Store auth method in request state for audit logging
    if request:
        request.state.auth_method = "jwt"

    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(optional_security),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    request: Request = None,
) -> Optional[User]:
    """
    Optional authentication - returns None instead of raising exception.

    Useful for endpoints that work differently for authenticated vs anonymous users.
    """
    try:
        return await get_current_user(credentials, x_api_key, db, request)
    except HTTPException:
        return None


async def _authenticate_api_key(
    api_key: str,
    db: Session,
    request: Request = None,
) -> Optional[User]:
    """
    Authenticate request using API key.

    Args:
        api_key: Raw API key from X-API-Key header
        db: Database session
        request: Request object for storing metadata

    Returns:
        User if authenticated, None if key format invalid

    Raises:
        HTTPException: If key is invalid, expired, or revoked
    """
    # Validate format
    if not is_valid_key_format(api_key):
        logger.warning("api_key_invalid_format", key_prefix=api_key[:8] if api_key else "empty")
        return None  # Let caller decide to fall through or reject

    # Extract prefix for efficient lookup
    key_prefix = extract_key_prefix(api_key)

    # Lookup by prefix (uses index)
    key_record = db.query(UserAPIKey).filter(
        UserAPIKey.key_prefix == key_prefix
    ).first()

    if not key_record:
        logger.warning("api_key_not_found", key_prefix=key_prefix)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # Verify hash (constant-time comparison)
    if not verify_api_key(api_key, key_record.key_hash):
        logger.warning("api_key_hash_mismatch", key_id=key_record.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # Check revocation
    if key_record.revoked:
        logger.warning("api_key_revoked", key_id=key_record.id, key_name=key_record.name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has been revoked"
        )

    # Check expiration
    if not key_record.is_valid():
        logger.warning("api_key_expired", key_id=key_record.id, expires_at=key_record.expires_at)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired"
        )

    # Get associated user
    user = db.query(User).filter(User.id == key_record.user_id, User.active == True).first()
    if not user:
        logger.warning("api_key_user_inactive", key_id=key_record.id, user_id=key_record.user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive"
        )

    # Update usage tracking
    key_record.last_used_at = datetime.utcnow()
    key_record.request_count += 1
    if request:
        key_record.last_used_ip = request.client.host if request.client else None
        request.state.auth_method = "api_key"
        request.state.api_key_id = key_record.id
        request.state.api_key_scopes = key_record.scopes
    db.commit()

    logger.info(
        "api_key_authenticated",
        user_id=user.id,
        key_id=key_record.id,
        key_name=key_record.name,
        request_count=key_record.request_count,
    )

    return user


def require_role(required_role: str):
    """
    Decorator factory for requiring specific role.

    Args:
        required_role: Required role ('owner', 'operator', 'viewer', 'support')

    Returns:
        FastAPI dependency function

    Usage:
        @app.post("/api/policies")
        def create_policy(
            current_user: User = Depends(require_role('operator'))
        ):
            ...
    """
    role_hierarchy = {
        'viewer': 0,
        'support': 1,
        'operator': 2,
        'owner': 3,
    }

    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        user_level = role_hierarchy.get(current_user.role, 0)
        required_level = role_hierarchy.get(required_role, 0)

        if user_level < required_level:
            logger.warning(
                "authorization_failed",
                user_id=current_user.id,
                user_role=current_user.role,
                required_role=required_role
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {required_role}"
            )

        return current_user

    return role_checker


def require_permission(permission: str):
    """
    Decorator factory for requiring specific permission.

    Args:
        permission: Required permission ('read', 'write', 'delete', 'manage_users', etc.)

    Returns:
        FastAPI dependency function

    Usage:
        @app.delete("/api/policies/{policy_id}")
        def delete_policy(
            policy_id: int,
            current_user: User = Depends(require_permission('delete'))
        ):
            ...
    """
    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.has_permission(permission):
            logger.warning(
                "permission_denied",
                user_id=current_user.id,
                user_role=current_user.role,
                required_permission=permission
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}"
            )

        return current_user

    return permission_checker


# Export public API
__all__ = [
    'oauth',
    'get_authentik_userinfo',
    'create_access_token',
    'decode_access_token',
    'get_or_create_user',
    'get_current_user',
    'require_role',
    'require_permission',
]
