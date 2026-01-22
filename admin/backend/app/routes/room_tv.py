"""
Room TV Configuration API routes.

Provides endpoints for managing Apple TV configuration per room:
- Room-to-TV mapping
- App configuration (profile screens, delays, guest access)
- Feature flags (multi-TV, auto profile select)
- Apple TV entity discovery from Home Assistant
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime
import structlog
import httpx
import os

from app.database import get_db
from app.models import RoomTVConfig, TVAppConfig, TVFeatureFlag
from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/room-tv", tags=["room-tv"])

# Home Assistant configuration
HA_URL = os.getenv("HA_URL", "http://localhost:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "")


# Pydantic models
class RoomTVConfigCreate(BaseModel):
    room_name: str
    display_name: Optional[str] = None
    media_player_entity_id: str
    remote_entity_id: str
    enabled: bool = True


class RoomTVConfigUpdate(BaseModel):
    display_name: Optional[str] = None
    media_player_entity_id: Optional[str] = None
    remote_entity_id: Optional[str] = None
    enabled: Optional[bool] = None


class TVAppConfigUpdate(BaseModel):
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    has_profile_screen: Optional[bool] = None
    profile_select_delay_ms: Optional[int] = None
    guest_allowed: Optional[bool] = None
    deep_link_scheme: Optional[str] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class TVAppConfigCreate(BaseModel):
    app_name: str
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    has_profile_screen: bool = False
    profile_select_delay_ms: int = 1500
    guest_allowed: bool = True
    deep_link_scheme: Optional[str] = None
    enabled: bool = True
    sort_order: int = 0


# ============================================================================
# Room TV Configuration CRUD
# ============================================================================

@router.get("")
async def list_room_tv_configs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """List all room TV configurations."""
    configs = db.query(RoomTVConfig).order_by(RoomTVConfig.room_name).all()
    return [config.to_dict() for config in configs]


@router.get("/internal")
async def list_room_tv_configs_internal(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List enabled room TV configs for orchestrator (no auth)."""
    configs = db.query(RoomTVConfig).filter(
        RoomTVConfig.enabled == True
    ).order_by(RoomTVConfig.room_name).all()
    return [config.to_dict() for config in configs]


@router.get("/internal/{room_name}")
async def get_room_tv_config_internal(
    room_name: str,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get room TV config by name for orchestrator (no auth)."""
    config = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == room_name.lower(),
        RoomTVConfig.enabled == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    return config.to_dict()


@router.get("/{room_name}")
async def get_room_tv_config(
    room_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get a specific room TV configuration by name."""
    config = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == room_name.lower()
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    return config.to_dict()


@router.post("")
async def create_room_tv_config(
    data: RoomTVConfigCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a new room TV configuration."""
    # Check for duplicate
    existing = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == data.room_name.lower()
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Room already configured")

    config = RoomTVConfig(
        room_name=data.room_name.lower(),
        display_name=data.display_name or data.room_name.replace("_", " ").title(),
        media_player_entity_id=data.media_player_entity_id,
        remote_entity_id=data.remote_entity_id,
        enabled=data.enabled,
    )

    db.add(config)
    db.commit()
    db.refresh(config)

    logger.info("room_tv_config_created", room=config.room_name)
    return config.to_dict()


@router.put("/{config_id}")
async def update_room_tv_config(
    config_id: int,
    data: RoomTVConfigUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update a room TV configuration."""
    config = db.query(RoomTVConfig).filter(RoomTVConfig.id == config_id).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(config, key, value)

    db.commit()
    db.refresh(config)

    logger.info("room_tv_config_updated", room=config.room_name)
    return config.to_dict()


@router.delete("/{config_id}")
async def delete_room_tv_config(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, str]:
    """Delete a room TV configuration."""
    config = db.query(RoomTVConfig).filter(RoomTVConfig.id == config_id).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    room_name = config.room_name
    db.delete(config)
    db.commit()

    logger.info("room_tv_config_deleted", room=room_name)
    return {"status": "deleted", "room": room_name}


# ============================================================================
# TV App Configuration
# ============================================================================

@router.get("/apps")
async def list_app_configs(
    guest_mode: bool = False,
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all app configurations, optionally filtered for guest mode (no auth - used by orchestrator)."""
    query = db.query(TVAppConfig).filter(TVAppConfig.enabled == True)
    if guest_mode:
        query = query.filter(TVAppConfig.guest_allowed == True)
    apps = query.order_by(TVAppConfig.sort_order, TVAppConfig.display_name).all()
    return [app.to_dict() for app in apps]


@router.get("/apps/all")
async def list_all_app_configs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """List all app configurations including disabled ones (admin only)."""
    apps = db.query(TVAppConfig).order_by(TVAppConfig.sort_order, TVAppConfig.display_name).all()
    return [app.to_dict() for app in apps]


@router.post("/apps")
async def create_app_config(
    data: TVAppConfigCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a new app configuration."""
    existing = db.query(TVAppConfig).filter(TVAppConfig.app_name == data.app_name).first()
    if existing:
        raise HTTPException(status_code=400, detail="App already configured")

    app = TVAppConfig(
        app_name=data.app_name,
        display_name=data.display_name or data.app_name,
        icon_url=data.icon_url,
        has_profile_screen=data.has_profile_screen,
        profile_select_delay_ms=data.profile_select_delay_ms,
        guest_allowed=data.guest_allowed,
        deep_link_scheme=data.deep_link_scheme,
        enabled=data.enabled,
        sort_order=data.sort_order,
    )

    db.add(app)
    db.commit()
    db.refresh(app)

    logger.info("tv_app_config_created", app=app.app_name)
    return app.to_dict()


@router.put("/apps/{app_id}")
async def update_app_config(
    app_id: int,
    data: TVAppConfigUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update an app configuration by ID."""
    app = db.query(TVAppConfig).filter(TVAppConfig.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(app, key, value)

    db.commit()
    db.refresh(app)

    logger.info("tv_app_config_updated", app=app.app_name)
    return app.to_dict()


@router.delete("/apps/{app_id}")
async def delete_app_config(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, str]:
    """Delete an app configuration."""
    app = db.query(TVAppConfig).filter(TVAppConfig.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    app_name = app.app_name
    db.delete(app)
    db.commit()

    logger.info("tv_app_config_deleted", app=app_name)
    return {"status": "deleted", "app": app_name}


# ============================================================================
# Feature Flags
# ============================================================================

@router.get("/features")
async def list_feature_flags(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all TV feature flags (no auth - used by orchestrator)."""
    flags = db.query(TVFeatureFlag).all()
    return [flag.to_dict() for flag in flags]


@router.put("/features/{feature_name}")
async def update_feature_flag(
    feature_name: str,
    enabled: bool,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Enable/disable a TV feature."""
    flag = db.query(TVFeatureFlag).filter(TVFeatureFlag.feature_name == feature_name).first()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature not found")

    flag.enabled = enabled
    db.commit()

    logger.info("tv_feature_flag_updated", feature=feature_name, enabled=enabled)
    return flag.to_dict()


# ============================================================================
# Apple TV Discovery
# ============================================================================

@router.get("/discover")
async def discover_apple_tvs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """
    Discover Apple TV entities from Home Assistant.

    Returns media_player entities with 10+ apps in source_list (Apple TVs).
    """
    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{HA_URL}/api/states",
                headers={"Authorization": f"Bearer {HA_TOKEN}"}
            )
            response.raise_for_status()
            entities = response.json()

    except httpx.HTTPError as e:
        logger.error("ha_discovery_failed", error=str(e))
        raise HTTPException(status_code=502, detail=f"Home Assistant connection failed: {str(e)}")

    # Find Apple TVs (media_players with 10+ source apps)
    apple_tvs = []
    for entity in entities:
        entity_id = entity.get("entity_id", "")
        if entity_id.startswith("media_player."):
            source_list = entity.get("attributes", {}).get("source_list", [])
            if len(source_list) >= 10:
                # Get existing config if any
                room_name = entity_id.replace("media_player.", "").replace("_tv", "").replace("_", " ")
                existing = db.query(RoomTVConfig).filter(
                    RoomTVConfig.media_player_entity_id == entity_id
                ).first()

                apple_tvs.append({
                    "entity_id": entity_id,
                    "friendly_name": entity.get("attributes", {}).get("friendly_name", entity_id),
                    "remote_entity_id": entity_id.replace("media_player.", "remote."),
                    "app_count": len(source_list),
                    "source_list": source_list,
                    "state": entity.get("state"),
                    "current_app": entity.get("attributes", {}).get("app_name"),
                    "suggested_room": room_name,
                    "already_configured": existing is not None,
                    "config_id": existing.id if existing else None,
                })

    logger.info("apple_tv_discovery_complete", count=len(apple_tvs))
    return apple_tvs


@router.get("/apps/sync/{entity_id:path}")
async def sync_apps_from_device(
    entity_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Sync app list from an Apple TV device to the database.

    Adds any new apps found on the device that aren't in the database.
    """
    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{HA_URL}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"}
            )
            response.raise_for_status()
            entity = response.json()

    except httpx.HTTPError as e:
        logger.error("ha_app_sync_failed", error=str(e))
        raise HTTPException(status_code=502, detail=f"Home Assistant connection failed: {str(e)}")

    source_list = entity.get("attributes", {}).get("source_list", [])
    if not source_list:
        raise HTTPException(status_code=400, detail="No apps found on device")

    # Get existing apps
    existing_apps = {app.app_name for app in db.query(TVAppConfig).all()}

    # Add new apps
    new_apps = []
    max_sort_order = db.query(TVAppConfig).count() * 10 + 100  # Start new apps after existing

    for app_name in source_list:
        if app_name not in existing_apps:
            app = TVAppConfig(
                app_name=app_name,
                display_name=app_name,
                has_profile_screen=False,
                guest_allowed=True,
                enabled=True,
                sort_order=max_sort_order,
            )
            db.add(app)
            new_apps.append(app_name)
            max_sort_order += 10

    db.commit()

    logger.info("app_sync_complete", device=entity_id, new_apps=len(new_apps))
    return {
        "status": "synced",
        "device": entity_id,
        "total_apps": len(source_list),
        "new_apps_added": len(new_apps),
        "new_apps": new_apps,
    }


# ============================================================================
# Apple TV Control (Direct Control from Admin UI)
# ============================================================================

@router.post("/control/{room_name}/launch")
async def launch_app_on_tv(
    room_name: str,
    app_name: str,
    send_select: bool = True,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Launch an app on a room's Apple TV.

    Optionally sends a 'select' command after launch to bypass profile screens.
    """
    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    # Get room config
    config = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == room_name.lower(),
        RoomTVConfig.enabled == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    # Get app config for delay settings
    app_config = db.query(TVAppConfig).filter(TVAppConfig.app_name == app_name).first()
    delay_ms = app_config.profile_select_delay_ms if app_config else 1500

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            # Launch the app
            response = await client.post(
                f"{HA_URL}/api/services/media_player/select_source",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                json={
                    "entity_id": config.media_player_entity_id,
                    "source": app_name,
                }
            )
            response.raise_for_status()

            result = {
                "status": "launched",
                "room": room_name,
                "app": app_name,
                "entity_id": config.media_player_entity_id,
            }

            # Send select command after delay if requested
            if send_select and app_config and app_config.has_profile_screen:
                import asyncio
                await asyncio.sleep(delay_ms / 1000)

                response = await client.post(
                    f"{HA_URL}/api/services/remote/send_command",
                    headers={"Authorization": f"Bearer {HA_TOKEN}"},
                    json={
                        "entity_id": config.remote_entity_id,
                        "command": "select",
                    }
                )
                response.raise_for_status()
                result["profile_select_sent"] = True

            logger.info("tv_app_launched", room=room_name, app=app_name)
            return result

    except httpx.HTTPError as e:
        logger.error("tv_app_launch_failed", room=room_name, app=app_name, error=str(e))
        raise HTTPException(status_code=502, detail=f"Home Assistant call failed: {str(e)}")


@router.post("/control/{room_name}/power")
async def power_control(
    room_name: str,
    action: str,  # "on" or "off"
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Turn Apple TV on or off."""
    if action not in ("on", "off"):
        raise HTTPException(status_code=400, detail="Action must be 'on' or 'off'")

    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    config = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == room_name.lower(),
        RoomTVConfig.enabled == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            service = "turn_on" if action == "on" else "turn_off"
            response = await client.post(
                f"{HA_URL}/api/services/media_player/{service}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                json={"entity_id": config.media_player_entity_id}
            )
            response.raise_for_status()

        logger.info("tv_power_control", room=room_name, action=action)
        return {"status": "success", "room": room_name, "action": action}

    except httpx.HTTPError as e:
        logger.error("tv_power_control_failed", room=room_name, action=action, error=str(e))
        raise HTTPException(status_code=502, detail=f"Home Assistant call failed: {str(e)}")


@router.post("/control/{room_name}/remote")
async def send_remote_command(
    room_name: str,
    command: str,
    repeat: int = 1,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Send a remote command (up, down, left, right, select, menu, home, play, pause)."""
    valid_commands = ["up", "down", "left", "right", "select", "menu", "home", "play", "pause", "stop"]
    if command not in valid_commands:
        raise HTTPException(status_code=400, detail=f"Command must be one of: {', '.join(valid_commands)}")

    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    config = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == room_name.lower(),
        RoomTVConfig.enabled == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            import asyncio
            for i in range(repeat):
                response = await client.post(
                    f"{HA_URL}/api/services/remote/send_command",
                    headers={"Authorization": f"Bearer {HA_TOKEN}"},
                    json={
                        "entity_id": config.remote_entity_id,
                        "command": command,
                    }
                )
                response.raise_for_status()
                if i < repeat - 1:
                    await asyncio.sleep(0.3)  # Small delay between repeats

        logger.info("tv_remote_command", room=room_name, command=command, repeat=repeat)
        return {"status": "success", "room": room_name, "command": command, "repeat": repeat}

    except httpx.HTTPError as e:
        logger.error("tv_remote_command_failed", room=room_name, command=command, error=str(e))
        raise HTTPException(status_code=502, detail=f"Home Assistant call failed: {str(e)}")


@router.get("/control/{room_name}/state")
async def get_tv_state(
    room_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get current state of a room's Apple TV."""
    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    config = db.query(RoomTVConfig).filter(
        RoomTVConfig.room_name == room_name.lower(),
        RoomTVConfig.enabled == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room TV config not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{HA_URL}/api/states/{config.media_player_entity_id}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"}
            )
            response.raise_for_status()
            entity = response.json()

        attrs = entity.get("attributes", {})
        return {
            "room": room_name,
            "entity_id": config.media_player_entity_id,
            "state": entity.get("state"),
            "app_name": attrs.get("app_name"),
            "app_id": attrs.get("app_id"),
            "media_title": attrs.get("media_title"),
            "media_artist": attrs.get("media_artist"),
            "media_duration": attrs.get("media_duration"),
            "media_position": attrs.get("media_position"),
            "source_list": attrs.get("source_list", []),
        }

    except httpx.HTTPError as e:
        logger.error("tv_state_failed", room=room_name, error=str(e))
        raise HTTPException(status_code=502, detail=f"Home Assistant call failed: {str(e)}")
