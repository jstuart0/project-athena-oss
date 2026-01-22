"""
Directions settings API routes.

Provides endpoints for managing Directions RAG service configuration.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import List, Dict, Any
import structlog

from app.database import get_db
from app.models import DirectionsSettings
from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/directions-settings", tags=["directions"])


@router.get("", response_model=List[Dict[str, Any]])
async def get_all_settings(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Get all directions settings."""
    settings = db.query(DirectionsSettings).order_by(
        DirectionsSettings.category,
        DirectionsSettings.setting_key
    ).all()
    return [s.to_dict() for s in settings]


@router.get("/public", response_model=Dict[str, Any])
async def get_public_settings(db: Session = Depends(get_db)):
    """Get directions settings without auth (for RAG service)."""
    settings = db.query(DirectionsSettings).all()

    # Return as key-value dict with typed values
    return {s.setting_key: s.to_dict()['setting_value'] for s in settings}


@router.get("/{setting_key}")
async def get_setting(
    setting_key: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Get a specific setting by key."""
    setting = db.query(DirectionsSettings).filter(
        DirectionsSettings.setting_key == setting_key
    ).first()

    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{setting_key}' not found")

    return setting.to_dict()


@router.put("/{setting_id}")
async def update_setting(
    setting_id: int,
    data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Update a directions setting."""
    setting = db.query(DirectionsSettings).filter(
        DirectionsSettings.id == setting_id
    ).first()

    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    new_value = data.get('setting_value')
    if new_value is None:
        raise HTTPException(status_code=400, detail="setting_value is required")

    # Validate value based on type
    if setting.setting_type == 'boolean':
        if str(new_value).lower() not in ('true', 'false'):
            raise HTTPException(status_code=400, detail="Boolean value must be 'true' or 'false'")
        new_value = str(new_value).lower()
    elif setting.setting_type == 'integer':
        try:
            int(new_value)
        except ValueError:
            raise HTTPException(status_code=400, detail="Integer value required")
        new_value = str(new_value)
    else:
        new_value = str(new_value)

    setting.setting_value = new_value
    db.commit()
    db.refresh(setting)

    logger.info("directions_setting_updated", setting_key=setting.setting_key, new_value=new_value)

    return setting.to_dict()


@router.post("/reset")
async def reset_to_defaults(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Reset all settings to defaults."""
    defaults = {
        'default_travel_mode': 'driving',
        'default_transit_mode': 'train',
        'include_traffic': 'false',
        'cache_ttl_seconds': '300',
        'offer_sms': 'true',
        'include_step_details': 'false',
        'google_maps_link': 'true',
        'max_alternatives': '1',
        'waypoints_enabled': 'true',
        'max_waypoints': '3',
        'default_stop_position': 'halfway',
        'places_search_radius_meters': '5000',
        'prefer_chain_restaurants': 'false',
        'min_rating_for_stops': '4.0',
    }

    settings = db.query(DirectionsSettings).all()

    for setting in settings:
        if setting.setting_key in defaults:
            setting.setting_value = defaults[setting.setting_key]

    db.commit()

    logger.info("directions_settings_reset_to_defaults")
    return {"message": "Settings reset to defaults", "count": len(settings)}
