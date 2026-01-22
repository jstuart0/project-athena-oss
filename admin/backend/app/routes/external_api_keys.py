"""
External API key management routes.

Stores encrypted API credentials for external providers (e.g., sports APIs)
and exposes a public, service-to-service endpoint for retrieval.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ExternalAPIKey
from app.utils.encryption import encrypt_value, decrypt_value

logger = structlog.get_logger()

router = APIRouter(prefix="/api/external-api-keys", tags=["external-api-keys"])


class ExternalAPIKeyCreate(BaseModel):
    """Request model for creating/updating an external API key."""
    service_name: str = Field(..., description="Unique service identifier (e.g., api-football)")
    api_name: str = Field(..., description="Human-readable API name")
    api_key: str = Field(..., description="API key (will be encrypted)")
    endpoint_url: str = Field(..., description="Base API endpoint URL")
    enabled: bool = Field(default=True, description="Enable/disable API")
    description: Optional[str] = Field(None, description="Admin notes")
    rate_limit_per_minute: Optional[int] = Field(None, description="Rate limit")

    # OAuth 2.0 support (optional)
    client_id: Optional[str] = Field(None, description="OAuth client ID")
    client_secret: Optional[str] = Field(None, description="OAuth client secret")
    oauth_token_url: Optional[str] = Field(None, description="OAuth token endpoint URL")
    oauth_scopes: Optional[str] = Field(None, description="OAuth scopes (comma-separated)")

    # Multiple keys support (optional)
    key_type: Optional[str] = Field(None, description="Key type: api_key, oauth, combined")
    key_purpose: Optional[str] = Field(None, description="Purpose of this key set")
    api_key2: Optional[str] = Field(None, description="Second API key (optional)")
    api_key2_label: Optional[str] = Field(None, description="Label for second key")
    api_key3: Optional[str] = Field(None, description="Third API key (optional)")
    api_key3_label: Optional[str] = Field(None, description="Label for third key")
    extra_config: Optional[dict] = Field(None, description="Additional configuration (JSON)")


class ExternalAPIKeyResponse(BaseModel):
    """Response model for external API key (masked)."""
    id: int
    service_name: str
    api_name: str
    api_key_masked: str
    endpoint_url: str
    enabled: bool
    description: Optional[str]
    rate_limit_per_minute: Optional[int]
    created_at: datetime
    updated_at: datetime
    last_used: Optional[datetime]

    # OAuth fields (masked)
    client_id_masked: Optional[str] = None
    has_client_secret: bool = False
    oauth_token_url: Optional[str] = None
    oauth_scopes: Optional[str] = None

    # Multiple keys support
    key_type: Optional[str] = None
    key_purpose: Optional[str] = None
    api_key2_masked: Optional[str] = None
    api_key2_label: Optional[str] = None
    api_key3_masked: Optional[str] = None
    api_key3_label: Optional[str] = None
    extra_config: Optional[dict] = None

    class Config:
        from_attributes = True


def _mask_key(encrypted: str) -> str:
    """Mask decrypted API key to last 4 characters."""
    try:
        decrypted = decrypt_value(encrypted)
        return f"{'*' * max(len(decrypted) - 4, 0)}{decrypted[-4:]}"
    except Exception:
        return "****"


def _to_response(key: ExternalAPIKey) -> ExternalAPIKeyResponse:
    """Convert DB model to response payload."""
    return ExternalAPIKeyResponse(
        id=key.id,
        service_name=key.service_name,
        api_name=key.api_name,
        api_key_masked=_mask_key(key.api_key_encrypted),
        endpoint_url=key.endpoint_url,
        enabled=key.enabled,
        description=key.description,
        rate_limit_per_minute=key.rate_limit_per_minute,
        created_at=key.created_at,
        updated_at=key.updated_at,
        last_used=key.last_used,
        # OAuth fields
        client_id_masked=_mask_key(key.client_id_encrypted) if key.client_id_encrypted else None,
        has_client_secret=bool(key.client_secret_encrypted),
        oauth_token_url=key.oauth_token_url,
        oauth_scopes=key.oauth_scopes,
        # Multiple keys
        key_type=key.key_type,
        key_purpose=key.key_purpose,
        api_key2_masked=_mask_key(key.api_key2_encrypted) if key.api_key2_encrypted else None,
        api_key2_label=key.api_key2_label,
        api_key3_masked=_mask_key(key.api_key3_encrypted) if key.api_key3_encrypted else None,
        api_key3_label=key.api_key3_label,
        extra_config=key.extra_config
    )


@router.post("", response_model=ExternalAPIKeyResponse, status_code=201)
async def create_external_api_key(
    key_data: ExternalAPIKeyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new external API key (encrypted at rest)."""
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check for existing key with same service_name and key_type
    existing_query = db.query(ExternalAPIKey).filter_by(service_name=key_data.service_name)
    if key_data.key_type:
        existing_query = existing_query.filter_by(key_type=key_data.key_type)
    else:
        existing_query = existing_query.filter(ExternalAPIKey.key_type.is_(None))

    existing = existing_query.first()
    if existing:
        detail = f"API key for '{key_data.service_name}'"
        if key_data.key_type:
            detail += f" with type '{key_data.key_type}'"
        detail += " already exists"
        raise HTTPException(status_code=400, detail=detail)

    # Encrypt all provided keys
    encrypted_key = encrypt_value(key_data.api_key)
    encrypted_client_id = encrypt_value(key_data.client_id) if key_data.client_id else None
    encrypted_client_secret = encrypt_value(key_data.client_secret) if key_data.client_secret else None
    encrypted_key2 = encrypt_value(key_data.api_key2) if key_data.api_key2 else None
    encrypted_key3 = encrypt_value(key_data.api_key3) if key_data.api_key3 else None

    new_key = ExternalAPIKey(
        service_name=key_data.service_name,
        api_name=key_data.api_name,
        api_key_encrypted=encrypted_key,
        endpoint_url=key_data.endpoint_url,
        enabled=key_data.enabled,
        description=key_data.description,
        rate_limit_per_minute=key_data.rate_limit_per_minute,
        # OAuth fields
        client_id_encrypted=encrypted_client_id,
        client_secret_encrypted=encrypted_client_secret,
        oauth_token_url=key_data.oauth_token_url,
        oauth_scopes=key_data.oauth_scopes,
        # Multiple keys
        key_type=key_data.key_type,
        key_purpose=key_data.key_purpose,
        api_key2_encrypted=encrypted_key2,
        api_key2_label=key_data.api_key2_label,
        api_key3_encrypted=encrypted_key3,
        api_key3_label=key_data.api_key3_label,
        extra_config=key_data.extra_config,
        # Audit
        created_by_id=current_user.id
    )

    db.add(new_key)
    db.commit()
    db.refresh(new_key)

    logger.info(
        "external_api_key_created",
        service_name=new_key.service_name,
        key_type=new_key.key_type,
        user=current_user.username
    )
    return _to_response(new_key)


@router.get("", response_model=List[ExternalAPIKeyResponse])
async def list_external_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all external API keys (masked)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    keys = db.query(ExternalAPIKey).order_by(ExternalAPIKey.service_name).all()
    return [_to_response(key) for key in keys]


@router.get("/{service_name}", response_model=ExternalAPIKeyResponse)
async def get_external_api_key(
    service_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Retrieve a specific external API key by service name (masked)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    key = db.query(ExternalAPIKey).filter_by(service_name=service_name).first()
    if not key:
        raise HTTPException(status_code=404, detail=f"API key '{service_name}' not found")

    return _to_response(key)


@router.put("/{service_name}", response_model=ExternalAPIKeyResponse)
async def update_external_api_key(
    service_name: str,
    key_data: ExternalAPIKeyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing external API key."""
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    key = db.query(ExternalAPIKey).filter_by(service_name=service_name).first()
    if not key:
        raise HTTPException(status_code=404, detail=f"API key '{service_name}' not found")

    # Update basic fields
    key.api_name = key_data.api_name
    key.endpoint_url = key_data.endpoint_url
    key.enabled = key_data.enabled
    key.description = key_data.description
    key.rate_limit_per_minute = key_data.rate_limit_per_minute

    # Update primary API key if provided
    if key_data.api_key:
        key.api_key_encrypted = encrypt_value(key_data.api_key)

    # Update OAuth fields
    if key_data.client_id:
        key.client_id_encrypted = encrypt_value(key_data.client_id)
    if key_data.client_secret:
        key.client_secret_encrypted = encrypt_value(key_data.client_secret)
    key.oauth_token_url = key_data.oauth_token_url
    key.oauth_scopes = key_data.oauth_scopes

    # Update multiple keys
    key.key_type = key_data.key_type
    key.key_purpose = key_data.key_purpose
    if key_data.api_key2:
        key.api_key2_encrypted = encrypt_value(key_data.api_key2)
    key.api_key2_label = key_data.api_key2_label
    if key_data.api_key3:
        key.api_key3_encrypted = encrypt_value(key_data.api_key3)
    key.api_key3_label = key_data.api_key3_label
    key.extra_config = key_data.extra_config

    db.commit()
    db.refresh(key)

    logger.info(
        "external_api_key_updated",
        service_name=key.service_name,
        key_type=key.key_type,
        user=current_user.username
    )
    return _to_response(key)


@router.delete("/{service_name}")
async def delete_external_api_key(
    service_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an external API key."""
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    key = db.query(ExternalAPIKey).filter_by(service_name=service_name).first()
    if not key:
        raise HTTPException(status_code=404, detail=f"API key '{service_name}' not found")

    db.delete(key)
    db.commit()

    logger.info(
        "external_api_key_deleted",
        service_name=service_name,
        user=current_user.username
    )
    return {"message": f"API key '{service_name}' deleted"}


@router.get("/public/{service_name}/key", include_in_schema=False)
async def get_api_key_for_service(
    service_name: str,
    db: Session = Depends(get_db)
):
    """
    Public (service-to-service) endpoint to fetch decrypted API key.
    Intended for internal services; no authentication enforced here.
    """
    key = db.query(ExternalAPIKey).filter_by(
        service_name=service_name,
        enabled=True
    ).first()

    if not key:
        raise HTTPException(status_code=404, detail=f"Enabled API key '{service_name}' not found")

    key.last_used = datetime.utcnow()
    db.commit()

    try:
        decrypted_key = decrypt_value(key.api_key_encrypted)
    except Exception as e:
        logger.error("external_api_key_decrypt_failed", service_name=service_name, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to decrypt API key")

    return {
        "api_key": decrypted_key,
        "endpoint_url": key.endpoint_url,
        "rate_limit_per_minute": key.rate_limit_per_minute
    }


@router.get("/public/{service_name}/credentials", include_in_schema=False)
async def get_credentials_for_service(
    service_name: str,
    db: Session = Depends(get_db)
):
    """
    Public (service-to-service) endpoint to fetch all decrypted credentials.
    Returns api_key, api_key2 (secret), and endpoint_url.
    Intended for services that need both key and secret (e.g., LiveKit, OAuth).
    """
    key = db.query(ExternalAPIKey).filter_by(
        service_name=service_name,
        enabled=True
    ).first()

    if not key:
        raise HTTPException(status_code=404, detail=f"Enabled API key '{service_name}' not found")

    key.last_used = datetime.utcnow()
    db.commit()

    try:
        decrypted_key = decrypt_value(key.api_key_encrypted)
        decrypted_secret = decrypt_value(key.api_key2_encrypted) if key.api_key2_encrypted else None
    except Exception as e:
        logger.error("external_api_key_decrypt_failed", service_name=service_name, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to decrypt API credentials")

    return {
        "api_key": decrypted_key,
        "api_secret": decrypted_secret,
        "api_key2_label": key.api_key2_label,
        "endpoint_url": key.endpoint_url,
        "rate_limit_per_minute": key.rate_limit_per_minute
    }

