"""
Secret management API routes.

Provides CRUD operations for encrypted API keys and credentials.
Uses application-level encryption before storing in database.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
import structlog
import os
import hmac
import hashlib

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Secret, AuditLog
from app.utils.encryption import encrypt_value, decrypt_value

logger = structlog.get_logger()

router = APIRouter(prefix="/api/secrets", tags=["secrets"])

# Service-to-service API key (for orchestrator, gateway, etc.)
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "dev-service-key-change-in-production")


class SecretCreate(BaseModel):
    """Request model for creating a secret."""
    service_name: str
    value: str  # Plain text value, will be encrypted
    description: str = None


class SecretUpdate(BaseModel):
    """Request model for updating a secret."""
    value: str = None  # New plain text value
    description: str = None


class SecretResponse(BaseModel):
    """Response model for secret data (excludes actual value)."""
    id: int
    service_name: str
    description: str = None
    created_by: str
    created_at: str
    updated_at: str
    last_rotated: str = None

    class Config:
        from_attributes = True


class SecretValueResponse(BaseModel):
    """Response model when revealing a secret value."""
    id: int
    service_name: str
    value: str  # Decrypted value
    description: str = None


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    secret: Secret,
    request: Request = None,
    revealed: bool = False
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='secret',
        resource_id=secret.id,
        secret_id=secret.id,
        new_value={'service_name': secret.service_name, 'revealed': revealed} if revealed else None,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='secret',
                resource_id=secret.id, revealed=revealed)


@router.get("", response_model=List[SecretResponse])
async def list_secrets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all secrets (without revealing values)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    secrets = db.query(Secret).order_by(Secret.service_name).all()

    return [
        SecretResponse(
            id=s.id,
            service_name=s.service_name,
            description=s.description,
            created_by=s.creator.username,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
            last_rotated=s.last_rotated.isoformat() if s.last_rotated else None
        )
        for s in secrets
    ]


@router.get("/{secret_id}", response_model=SecretResponse)
async def get_secret(
    secret_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get secret metadata (without revealing value)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    return SecretResponse(
        id=secret.id,
        service_name=secret.service_name,
        description=secret.description,
        created_by=secret.creator.username,
        created_at=secret.created_at.isoformat(),
        updated_at=secret.updated_at.isoformat(),
        last_rotated=secret.last_rotated.isoformat() if secret.last_rotated else None
    )


@router.get("/{secret_id}/reveal", response_model=SecretValueResponse)
async def reveal_secret(
    secret_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Reveal the actual secret value.

    SECURITY: This action is logged in audit logs.
    Requires manage_secrets permission.
    """
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to reveal secrets")

    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    # Decrypt value
    try:
        decrypted_value = decrypt_value(secret.encrypted_value)
    except Exception as e:
        logger.error("secret_decryption_failed", secret_id=secret_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to decrypt secret")

    # Audit log for revealing
    create_audit_log(db, current_user, 'reveal', secret, request=request, revealed=True)

    logger.warning("secret_revealed", secret_id=secret_id, service_name=secret.service_name,
                   user=current_user.username, ip=request.client.host)

    return SecretValueResponse(
        id=secret.id,
        service_name=secret.service_name,
        value=decrypted_value,
        description=secret.description
    )


@router.post("", response_model=SecretResponse, status_code=201)
async def create_secret(
    secret_data: SecretCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new secret."""
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if secret already exists
    existing = db.query(Secret).filter(Secret.service_name == secret_data.service_name).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Secret for service '{secret_data.service_name}' already exists"
        )

    # Encrypt value
    try:
        encrypted_value = encrypt_value(secret_data.value)
    except Exception as e:
        logger.error("secret_encryption_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to encrypt secret")

    # Create secret
    secret = Secret(
        service_name=secret_data.service_name,
        encrypted_value=encrypted_value,
        description=secret_data.description,
        created_by_id=current_user.id
    )
    db.add(secret)
    db.commit()
    db.refresh(secret)

    # Audit log
    create_audit_log(db, current_user, 'create', secret, request=request)

    logger.info("secret_created", secret_id=secret.id, service_name=secret.service_name,
                user=current_user.username)

    return SecretResponse(
        id=secret.id,
        service_name=secret.service_name,
        description=secret.description,
        created_by=current_user.username,
        created_at=secret.created_at.isoformat(),
        updated_at=secret.updated_at.isoformat(),
        last_rotated=None
    )


@router.put("/{secret_id}", response_model=SecretResponse)
async def update_secret(
    secret_id: int,
    secret_data: SecretUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing secret (rotate value or update description)."""
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    # Update value if provided (rotation)
    if secret_data.value is not None:
        try:
            secret.encrypted_value = encrypt_value(secret_data.value)
            secret.last_rotated = datetime.utcnow()
        except Exception as e:
            logger.error("secret_encryption_failed", error=str(e))
            raise HTTPException(status_code=500, detail="Failed to encrypt secret")

    # Update description if provided
    if secret_data.description is not None:
        secret.description = secret_data.description

    db.commit()
    db.refresh(secret)

    # Audit log
    action = 'rotate' if secret_data.value is not None else 'update'
    create_audit_log(db, current_user, action, secret, request=request)

    logger.info("secret_updated", secret_id=secret.id, service_name=secret.service_name,
                action=action, user=current_user.username)

    return SecretResponse(
        id=secret.id,
        service_name=secret.service_name,
        description=secret.description,
        created_by=secret.creator.username,
        created_at=secret.created_at.isoformat(),
        updated_at=secret.updated_at.isoformat(),
        last_rotated=secret.last_rotated.isoformat() if secret.last_rotated else None
    )


@router.delete("/{secret_id}", status_code=204)
async def delete_secret(
    secret_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a secret permanently."""
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    service_name = secret.service_name

    # Audit log before deletion
    create_audit_log(db, current_user, 'delete', secret, request=request)

    # Permanent deletion
    db.delete(secret)
    db.commit()

    logger.warning("secret_deleted", secret_id=secret_id, service_name=service_name,
                   user=current_user.username)

    return None


# ============================================================================
# Service-to-Service API (for orchestrator, gateway, etc.)
# ============================================================================

def verify_service_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    Verify service-to-service API key.

    This allows internal services (orchestrator, gateway) to fetch secrets
    without user authentication.
    """
    if not hmac.compare_digest(x_api_key, SERVICE_API_KEY):
        logger.warning("service_api_key_invalid", provided_key=x_api_key[:8] + "...")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


@router.get("/service/{service_name}", response_model=SecretValueResponse)
async def get_service_secret(
    service_name: str,
    db: Session = Depends(get_db),
    _verified: bool = Depends(verify_service_api_key)
):
    """
    Get decrypted secret value for a service (service-to-service endpoint).

    This endpoint is used by internal services (orchestrator, gateway, RAG services)
    to fetch configuration secrets like API tokens.

    Authentication: Requires X-API-Key header with valid service API key.
    """
    secret = db.query(Secret).filter(Secret.service_name == service_name).first()
    if not secret:
        logger.warning("service_secret_not_found", service_name=service_name)
        raise HTTPException(status_code=404, detail=f"Secret '{service_name}' not found")

    # Decrypt value
    try:
        decrypted_value = decrypt_value(secret.encrypted_value)
    except Exception as e:
        logger.error("service_secret_decryption_failed", service_name=service_name, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to decrypt secret")

    logger.info("service_secret_retrieved", service_name=service_name)

    return SecretValueResponse(
        id=secret.id,
        service_name=secret.service_name,
        value=decrypted_value,
        description=secret.description
    )
