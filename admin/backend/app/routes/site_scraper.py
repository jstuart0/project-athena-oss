"""Site Scraper Configuration API Routes."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, SiteScraperConfig

logger = structlog.get_logger()
router = APIRouter(prefix="/api/site-scraper", tags=["site-scraper"])


class SiteScraperConfigResponse(BaseModel):
    id: int
    owner_mode_any_url: bool
    guest_mode_any_url: bool
    allowed_domains: List[str]
    blocked_domains: List[str]
    max_content_length: int
    cache_ttl: int

    class Config:
        from_attributes = True


class SiteScraperConfigUpdate(BaseModel):
    owner_mode_any_url: Optional[bool] = None
    guest_mode_any_url: Optional[bool] = None
    allowed_domains: Optional[List[str]] = None
    blocked_domains: Optional[List[str]] = None
    max_content_length: Optional[int] = None
    cache_ttl: Optional[int] = None


def get_or_create_config(db: Session) -> SiteScraperConfig:
    """Get existing config or create default."""
    config = db.query(SiteScraperConfig).first()
    if not config:
        config = SiteScraperConfig(
            owner_mode_any_url=True,
            guest_mode_any_url=False,
            allowed_domains=[],
            blocked_domains=[],
            max_content_length=50000,
            cache_ttl=1800
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


# Public endpoint for services
@router.get("/config/public", response_model=SiteScraperConfigResponse)
async def get_config_public(db: Session = Depends(get_db)):
    """Get site scraper configuration (public, no auth)."""
    config = get_or_create_config(db)
    return SiteScraperConfigResponse(**config.to_dict())


@router.get("/config", response_model=SiteScraperConfigResponse)
async def get_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get site scraper configuration."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = get_or_create_config(db)
    return SiteScraperConfigResponse(**config.to_dict())


@router.put("/config", response_model=SiteScraperConfigResponse)
async def update_config(
    config_data: SiteScraperConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update site scraper configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = get_or_create_config(db)

    update_data = config_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)

    logger.info(
        "site_scraper_config_updated",
        user=current_user.username,
        fields=list(update_data.keys())
    )

    return SiteScraperConfigResponse(**config.to_dict())
