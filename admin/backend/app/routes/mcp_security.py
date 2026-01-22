"""
MCP Security API routes.

Provides CRUD operations for MCP security configuration:
- Domain allowlist/blocklist management
- Execution limits
- Approval workflow settings
"""
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog
from urllib.parse import urlparse

from app.database import get_db
from app.models import MCPSecurity, ToolApprovalQueue
from app.auth.oidc import get_current_user, User

logger = structlog.get_logger()
router = APIRouter(prefix="/api/mcp-security", tags=["mcp-security"])


# =============================================================================
# Pydantic Models
# =============================================================================

class MCPSecurityResponse(BaseModel):
    """Response model for MCP security configuration."""
    id: int
    allowed_domains: List[str]
    blocked_domains: List[str]
    max_execution_time_ms: int
    max_concurrent_tools: int
    require_owner_approval: bool
    auto_approve_patterns: List[str]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class MCPSecurityUpdate(BaseModel):
    """Request model for updating MCP security configuration."""
    allowed_domains: Optional[List[str]] = None
    blocked_domains: Optional[List[str]] = None
    max_execution_time_ms: Optional[int] = Field(None, ge=1000, le=300000)
    max_concurrent_tools: Optional[int] = Field(None, ge=1, le=20)
    require_owner_approval: Optional[bool] = None
    auto_approve_patterns: Optional[List[str]] = None


class DomainCheckRequest(BaseModel):
    """Request model for checking if a domain is allowed."""
    url: str


class DomainCheckResponse(BaseModel):
    """Response model for domain check."""
    url: str
    domain: str
    allowed: bool
    reason: str


class ToolApprovalQueueResponse(BaseModel):
    """Response model for tool approval queue item."""
    id: int
    tool_name: str
    display_name: Optional[str]
    description: Optional[str]
    source_domain: str
    discovered_at: str
    status: str
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    rejection_reason: Optional[str]

    class Config:
        from_attributes = True


class ToolApprovalAction(BaseModel):
    """Request model for approving/rejecting a tool."""
    action: str = Field(..., pattern="^(approve|reject)$")
    rejection_reason: Optional[str] = None


# =============================================================================
# Security Configuration Endpoints
# =============================================================================

@router.get("", response_model=MCPSecurityResponse)
async def get_mcp_security(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get MCP security configuration."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    security = db.query(MCPSecurity).first()
    if not security:
        # Create default configuration (includes Thor's n8n service)
        security = MCPSecurity(
            allowed_domains=["localhost", "127.0.0.1", "localhost", "*"],
            blocked_domains=[],
            max_execution_time_ms=30000,
            max_concurrent_tools=5,
            require_owner_approval=True,
            auto_approve_patterns=[],
        )
        db.add(security)
        db.commit()
        db.refresh(security)

    logger.info("get_mcp_security", user=current_user.username)
    return MCPSecurityResponse(**security.to_dict())


@router.get("/public", response_model=MCPSecurityResponse)
async def get_mcp_security_public(db: Session = Depends(get_db)):
    """
    Get MCP security configuration (public endpoint).

    Used by services to check domain allowlists without authentication.
    """
    security = db.query(MCPSecurity).first()
    if not security:
        # Return default configuration (includes Thor's n8n service)
        return MCPSecurityResponse(
            id=0,
            allowed_domains=["localhost", "127.0.0.1", "localhost", "*"],
            blocked_domains=[],
            max_execution_time_ms=30000,
            max_concurrent_tools=5,
            require_owner_approval=True,
            auto_approve_patterns=[],
            created_at=None,
            updated_at=None,
        )

    logger.debug("get_mcp_security_public")
    return MCPSecurityResponse(**security.to_dict())


@router.put("", response_model=MCPSecurityResponse)
async def update_mcp_security(
    data: MCPSecurityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update MCP security configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    security = db.query(MCPSecurity).first()
    if not security:
        security = MCPSecurity()
        db.add(security)

    # Update fields
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(security, field, value)

    db.commit()
    db.refresh(security)

    logger.info(
        "update_mcp_security",
        updated_fields=list(update_data.keys()),
        user=current_user.username
    )

    return MCPSecurityResponse(**security.to_dict())


# =============================================================================
# Domain Management Endpoints
# =============================================================================

@router.post("/domains/allow", response_model=MCPSecurityResponse)
async def add_allowed_domain(
    domain: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add a domain to the allowlist."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    security = db.query(MCPSecurity).first()
    if not security:
        security = MCPSecurity(allowed_domains=[])
        db.add(security)

    # Normalize domain
    domain = domain.lower().strip()

    # Validate domain format
    if not _is_valid_domain(domain):
        raise HTTPException(status_code=400, detail=f"Invalid domain format: {domain}")

    # Add if not already present
    if security.allowed_domains is None:
        security.allowed_domains = []

    if domain not in security.allowed_domains:
        security.allowed_domains = security.allowed_domains + [domain]
        db.commit()
        db.refresh(security)
        logger.info("add_allowed_domain", domain=domain, user=current_user.username)

    return MCPSecurityResponse(**security.to_dict())


@router.delete("/domains/allow/{domain}", response_model=MCPSecurityResponse)
async def remove_allowed_domain(
    domain: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a domain from the allowlist."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    security = db.query(MCPSecurity).first()
    if not security or not security.allowed_domains:
        raise HTTPException(status_code=404, detail="Domain not found in allowlist")

    domain = domain.lower().strip()
    if domain not in security.allowed_domains:
        raise HTTPException(status_code=404, detail="Domain not found in allowlist")

    security.allowed_domains = [d for d in security.allowed_domains if d != domain]
    db.commit()
    db.refresh(security)

    logger.info("remove_allowed_domain", domain=domain, user=current_user.username)
    return MCPSecurityResponse(**security.to_dict())


@router.post("/domains/block", response_model=MCPSecurityResponse)
async def add_blocked_domain(
    domain: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add a domain to the blocklist."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    security = db.query(MCPSecurity).first()
    if not security:
        security = MCPSecurity(blocked_domains=[])
        db.add(security)

    domain = domain.lower().strip()

    if not _is_valid_domain(domain):
        raise HTTPException(status_code=400, detail=f"Invalid domain format: {domain}")

    if security.blocked_domains is None:
        security.blocked_domains = []

    if domain not in security.blocked_domains:
        security.blocked_domains = security.blocked_domains + [domain]
        db.commit()
        db.refresh(security)
        logger.info("add_blocked_domain", domain=domain, user=current_user.username)

    return MCPSecurityResponse(**security.to_dict())


@router.delete("/domains/block/{domain}", response_model=MCPSecurityResponse)
async def remove_blocked_domain(
    domain: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a domain from the blocklist."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    security = db.query(MCPSecurity).first()
    if not security or not security.blocked_domains:
        raise HTTPException(status_code=404, detail="Domain not found in blocklist")

    domain = domain.lower().strip()
    if domain not in security.blocked_domains:
        raise HTTPException(status_code=404, detail="Domain not found in blocklist")

    security.blocked_domains = [d for d in security.blocked_domains if d != domain]
    db.commit()
    db.refresh(security)

    logger.info("remove_blocked_domain", domain=domain, user=current_user.username)
    return MCPSecurityResponse(**security.to_dict())


@router.post("/check-domain", response_model=DomainCheckResponse)
async def check_domain(
    request: DomainCheckRequest,
    db: Session = Depends(get_db)
):
    """
    Check if a URL's domain is allowed for MCP connections.

    Public endpoint used by services to validate MCP endpoints.
    """
    url = request.url
    domain = _extract_domain(url)

    if not domain:
        return DomainCheckResponse(
            url=url,
            domain="",
            allowed=False,
            reason="Invalid URL format"
        )

    security = db.query(MCPSecurity).first()

    # Default to allowing localhost services if no config
    if not security:
        default_allowed = ["localhost", "127.0.0.1", "localhost"]
        allowed = domain in default_allowed or domain.endswith("")
        reason = "Allowed (default)" if allowed else "No security config, domain not in defaults"
        return DomainCheckResponse(url=url, domain=domain, allowed=allowed, reason=reason)

    # Check blocklist first (takes precedence)
    blocked_domains = security.blocked_domains or []
    if _domain_matches(domain, blocked_domains):
        logger.warning("mcp_domain_blocked", domain=domain, url=url)
        return DomainCheckResponse(
            url=url,
            domain=domain,
            allowed=False,
            reason="Domain is in blocklist"
        )

    # Check allowlist
    allowed_domains = security.allowed_domains or []
    if not allowed_domains:
        # Empty allowlist means only localhost
        allowed = domain in ["localhost", "127.0.0.1"]
        reason = "Allowed (localhost)" if allowed else "Empty allowlist, only localhost allowed"
    elif _domain_matches(domain, allowed_domains):
        allowed = True
        reason = "Domain is in allowlist"
    else:
        allowed = False
        reason = "Domain not in allowlist"

    logger.info("mcp_domain_check", domain=domain, url=url, allowed=allowed, reason=reason)
    return DomainCheckResponse(url=url, domain=domain, allowed=allowed, reason=reason)


# =============================================================================
# Tool Approval Queue Endpoints
# =============================================================================

@router.get("/approval-queue", response_model=List[ToolApprovalQueueResponse])
async def list_pending_approvals(
    status: str = "pending",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List tools pending approval."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(ToolApprovalQueue)
    if status:
        query = query.filter(ToolApprovalQueue.status == status)

    items = query.order_by(ToolApprovalQueue.discovered_at.desc()).all()

    logger.info("list_pending_approvals", status=status, count=len(items), user=current_user.username)
    return [ToolApprovalQueueResponse(**item.to_dict()) for item in items]


@router.post("/approval-queue/{item_id}/review", response_model=ToolApprovalQueueResponse)
async def review_tool_approval(
    item_id: int,
    action: ToolApprovalAction,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Approve or reject a tool in the approval queue."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    item = db.query(ToolApprovalQueue).filter(ToolApprovalQueue.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Approval queue item not found")

    if item.status != "pending":
        raise HTTPException(status_code=400, detail=f"Tool already {item.status}")

    from datetime import datetime

    if action.action == "approve":
        item.status = "approved"
        item.reviewed_by = current_user.username
        item.reviewed_at = datetime.utcnow()
        logger.info("tool_approved", tool_name=item.tool_name, user=current_user.username)
    else:
        item.status = "rejected"
        item.reviewed_by = current_user.username
        item.reviewed_at = datetime.utcnow()
        item.rejection_reason = action.rejection_reason
        logger.info("tool_rejected", tool_name=item.tool_name, reason=action.rejection_reason, user=current_user.username)

    db.commit()
    db.refresh(item)

    return ToolApprovalQueueResponse(**item.to_dict())


# =============================================================================
# Helper Functions
# =============================================================================

def _extract_domain(url: str) -> Optional[str]:
    """Extract domain from URL."""
    try:
        # Handle URLs without scheme
        if not url.startswith(('http://', 'https://')):
            url = 'http://' + url

        parsed = urlparse(url)
        domain = parsed.hostname
        return domain.lower() if domain else None
    except Exception:
        return None


def _is_valid_domain(domain: str) -> bool:
    """Validate domain format."""
    if not domain:
        return False

    # Allow localhost and IP addresses
    if domain in ["localhost", "127.0.0.1"]:
        return True

    # Check for valid domain characters
    import re
    # Allow wildcards (*.example.com), standard domains, and IP addresses
    pattern = r'^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$|^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
    return bool(re.match(pattern, domain))


def _domain_matches(domain: str, patterns: List[str]) -> bool:
    """Check if domain matches any pattern in list (supports wildcards)."""
    domain = domain.lower()

    for pattern in patterns:
        pattern = pattern.lower()

        # Exact match
        if domain == pattern:
            return True

        # Wildcard match (*.example.com matches sub.example.com)
        if pattern.startswith("*."):
            suffix = pattern[2:]  # Remove "*."
            if domain.endswith(suffix) or domain == suffix:
                return True

    return False
