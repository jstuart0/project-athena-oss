"""
Settings management API routes.

Provides endpoints for managing application settings including OIDC configuration.
Settings are stored as encrypted secrets in the database.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Secret, SystemSetting
from app.utils.encryption import encrypt_value, decrypt_value

logger = structlog.get_logger()

router = APIRouter(prefix="/api/settings", tags=["settings"])


class OIDCSettings(BaseModel):
    """OIDC configuration settings."""
    provider_url: str
    client_id: str
    client_secret: Optional[str] = None  # Only included when saving
    redirect_uri: str


class OIDCSettingsResponse(BaseModel):
    """OIDC settings response (excludes client_secret)."""
    provider_url: str
    client_id: str
    redirect_uri: str


@router.get("/oidc", response_model=OIDCSettingsResponse)
async def get_oidc_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get OIDC authentication settings.

    Returns current OIDC configuration from database.
    Client secret is not included in response for security.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Load OIDC settings from secrets table
        provider_url_secret = db.query(Secret).filter(Secret.service_name == "oidc_provider_url").first()
        client_id_secret = db.query(Secret).filter(Secret.service_name == "oidc_client_id").first()
        redirect_uri_secret = db.query(Secret).filter(Secret.service_name == "oidc_redirect_uri").first()

        # Decrypt values
        provider_url = decrypt_value(provider_url_secret.encrypted_value) if provider_url_secret else ""
        client_id = decrypt_value(client_id_secret.encrypted_value) if client_id_secret else ""
        redirect_uri = decrypt_value(redirect_uri_secret.encrypted_value) if redirect_uri_secret else "http://localhost:8080/api/auth/callback"

        logger.info("oidc_settings_retrieved", user=current_user.username)

        return OIDCSettingsResponse(
            provider_url=provider_url,
            client_id=client_id,
            redirect_uri=redirect_uri
        )

    except Exception as e:
        logger.error("failed_to_retrieve_oidc_settings", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve OIDC settings")


@router.post("/oidc")
async def save_oidc_settings(
    settings: OIDCSettings,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save OIDC authentication settings.

    Stores OIDC configuration as encrypted secrets in database.
    Requires manage_secrets permission.

    Note: Backend must be restarted for changes to take effect.
    """
    if not current_user.has_permission('manage_secrets'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Save or update each OIDC setting as a secret
        def save_or_update_secret(service_name: str, value: str):
            """Helper to save or update a secret."""
            secret = db.query(Secret).filter(Secret.service_name == service_name).first()

            if secret:
                # Update existing
                secret.encrypted_value = encrypt_value(value)
            else:
                # Create new
                secret = Secret(
                    service_name=service_name,
                    encrypted_value=encrypt_value(value),
                    description=f"OIDC configuration - {service_name}",
                    created_by_id=current_user.id
                )
                db.add(secret)

        # Save provider URL
        save_or_update_secret("oidc_provider_url", settings.provider_url)

        # Save client ID
        save_or_update_secret("oidc_client_id", settings.client_id)

        # Save client secret (only if provided)
        if settings.client_secret:
            save_or_update_secret("oidc_client_secret", settings.client_secret)

        # Save redirect URI
        save_or_update_secret("oidc_redirect_uri", settings.redirect_uri)

        db.commit()

        logger.info(
            "oidc_settings_saved",
            user=current_user.username,
            provider_url=settings.provider_url,
            ip=request.client.host
        )

        return {
            "status": "success",
            "message": "OIDC settings saved successfully. Backend restart required for changes to take effect."
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_oidc_settings", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to save OIDC settings: {str(e)}")


# ============================================================================
# LLM Memory Settings
# ============================================================================

class LLMMemorySettings(BaseModel):
    """LLM memory management settings."""
    keep_models_loaded: bool = True
    default_keep_alive_seconds: int = -1  # -1 = forever, 0 = unload immediately, >0 = seconds


class LLMMemorySettingsResponse(BaseModel):
    """Response model for LLM memory settings."""
    keep_models_loaded: bool
    default_keep_alive_seconds: int
    keep_alive_description: str


@router.get("/llm-memory", response_model=LLMMemorySettingsResponse)
async def get_llm_memory_settings(
    db: Session = Depends(get_db)
):
    """
    Get LLM memory management settings.

    This endpoint is public (no auth) to allow services to fetch settings.

    Returns:
        - keep_models_loaded: Whether to keep models in memory
        - default_keep_alive_seconds: Default keep_alive duration
    """
    try:
        # Get settings from system_settings table
        keep_loaded_setting = db.query(SystemSetting).filter(
            SystemSetting.key == "llm_keep_models_loaded"
        ).first()

        keep_alive_setting = db.query(SystemSetting).filter(
            SystemSetting.key == "llm_default_keep_alive_seconds"
        ).first()

        # Parse values with defaults
        keep_models_loaded = True
        if keep_loaded_setting:
            keep_models_loaded = keep_loaded_setting.value.lower() in ("true", "1", "yes")

        default_keep_alive = -1
        if keep_alive_setting:
            try:
                default_keep_alive = int(keep_alive_setting.value)
            except ValueError:
                default_keep_alive = -1

        # Generate description
        if default_keep_alive == -1:
            description = "Models stay loaded forever (until manually unloaded)"
        elif default_keep_alive == 0:
            description = "Models unload immediately after each request"
        else:
            minutes = default_keep_alive // 60
            if minutes > 0:
                description = f"Models unload after {minutes} minute(s) of inactivity"
            else:
                description = f"Models unload after {default_keep_alive} second(s) of inactivity"

        return LLMMemorySettingsResponse(
            keep_models_loaded=keep_models_loaded,
            default_keep_alive_seconds=default_keep_alive,
            keep_alive_description=description
        )

    except Exception as e:
        logger.error("failed_to_get_llm_memory_settings", error=str(e))
        # Return defaults on error
        return LLMMemorySettingsResponse(
            keep_models_loaded=True,
            default_keep_alive_seconds=-1,
            keep_alive_description="Models stay loaded forever (until manually unloaded)"
        )


@router.post("/llm-memory")
async def save_llm_memory_settings(
    settings: LLMMemorySettings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save LLM memory management settings.

    Controls how long models stay loaded in memory after requests.

    Args:
        keep_models_loaded: Master toggle for keeping models loaded
        default_keep_alive_seconds: Default duration
            - -1: Keep forever (never unload)
            - 0: Unload immediately after each request
            - >0: Unload after this many seconds of inactivity
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Helper to save or update system setting
        def save_setting(key: str, value: str, description: str, category: str = "performance"):
            setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
            if setting:
                setting.value = value
            else:
                setting = SystemSetting(
                    key=key,
                    value=value,
                    description=description,
                    category=category
                )
                db.add(setting)

        # Save keep_models_loaded
        save_setting(
            "llm_keep_models_loaded",
            str(settings.keep_models_loaded).lower(),
            "When enabled, keeps LLM models loaded in memory to avoid cold start delays"
        )

        # Save default_keep_alive_seconds
        save_setting(
            "llm_default_keep_alive_seconds",
            str(settings.default_keep_alive_seconds),
            "Default keep_alive duration for models. -1 = forever, 0 = unload immediately"
        )

        db.commit()

        logger.info(
            "llm_memory_settings_saved",
            user=current_user.username,
            keep_models_loaded=settings.keep_models_loaded,
            default_keep_alive=settings.default_keep_alive_seconds
        )

        return {
            "status": "success",
            "message": "LLM memory settings saved successfully"
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_llm_memory_settings", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to save LLM memory settings: {str(e)}")


# ============================================================================
# House Layout Settings (for occupancy estimation)
# ============================================================================

class HouseLayoutSettings(BaseModel):
    """House layout description for occupancy estimation."""
    layout_description: str


class HouseLayoutSettingsResponse(BaseModel):
    """Response model for house layout settings."""
    layout_description: str
    has_layout: bool


@router.get("/house-layout", response_model=HouseLayoutSettingsResponse)
async def get_house_layout_settings(
    db: Session = Depends(get_db)
):
    """
    Get house layout description for occupancy estimation.

    This endpoint is public (no auth) to allow orchestrator to fetch the layout.

    Returns:
        - layout_description: Text description of house layout
        - has_layout: Whether a layout has been configured
    """
    try:
        layout_setting = db.query(SystemSetting).filter(
            SystemSetting.key == "house_layout_description"
        ).first()

        if layout_setting and layout_setting.value:
            return HouseLayoutSettingsResponse(
                layout_description=layout_setting.value,
                has_layout=True
            )

        return HouseLayoutSettingsResponse(
            layout_description="",
            has_layout=False
        )

    except Exception as e:
        logger.error("failed_to_get_house_layout", error=str(e))
        return HouseLayoutSettingsResponse(
            layout_description="",
            has_layout=False
        )


@router.post("/house-layout")
async def save_house_layout_settings(
    settings: HouseLayoutSettings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save house layout description for occupancy estimation.

    The layout description helps the LLM understand room relationships
    for better occupancy estimation based on motion sensor data.

    Example layout description:
    ```
    2-story house in Baltimore.

    First floor:
    - Living room, dining room, kitchen (open floor plan, connected)
    - Half bathroom near kitchen

    Second floor:
    - Office (front of house, above living room)
    - Master bedroom with attached master bath
    - Hallway connecting to alpha and beta (guest bedrooms, adjacent)

    Basement: Unfinished, laundry area

    Travel times:
    - Office to kitchen: ~20 seconds (stairs + through living room)
    - Master bedroom to master bath: ~5 seconds (attached)
    - Alpha to beta: ~5 seconds (next door)
    ```
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        setting = db.query(SystemSetting).filter(
            SystemSetting.key == "house_layout_description"
        ).first()

        if setting:
            setting.value = settings.layout_description
        else:
            setting = SystemSetting(
                key="house_layout_description",
                value=settings.layout_description,
                description="House layout description for LLM-based occupancy estimation",
                category="intelligence"
            )
            db.add(setting)

        db.commit()

        logger.info(
            "house_layout_saved",
            user=current_user.username,
            length=len(settings.layout_description)
        )

        return {
            "status": "success",
            "message": "House layout saved successfully"
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_house_layout", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to save house layout: {str(e)}")


# ============================================================================
# Tool Proposal Settings (Auto-Approve Mode)
# ============================================================================

class ToolProposalSettings(BaseModel):
    """Tool proposal settings."""
    auto_approve_enabled: bool = False


class ToolProposalSettingsResponse(BaseModel):
    """Response model for tool proposal settings."""
    auto_approve_enabled: bool
    description: str


@router.get("/tool-proposals", response_model=ToolProposalSettingsResponse)
async def get_tool_proposal_settings(
    db: Session = Depends(get_db)
):
    """
    Get tool proposal settings.

    This endpoint is public (no auth) to allow services to fetch settings.

    Returns:
        - auto_approve_enabled: Whether new proposals are auto-approved
    """
    try:
        auto_approve_setting = db.query(SystemSetting).filter(
            SystemSetting.key == "tool_proposals_auto_approve"
        ).first()

        auto_approve = False
        if auto_approve_setting:
            auto_approve = auto_approve_setting.value.lower() in ("true", "1", "yes")

        description = "New tool proposals are auto-approved" if auto_approve else "New tool proposals require manual approval"

        return ToolProposalSettingsResponse(
            auto_approve_enabled=auto_approve,
            description=description
        )

    except Exception as e:
        logger.error("failed_to_get_tool_proposal_settings", error=str(e))
        return ToolProposalSettingsResponse(
            auto_approve_enabled=False,
            description="New tool proposals require manual approval"
        )


@router.post("/tool-proposals")
async def save_tool_proposal_settings(
    settings: ToolProposalSettings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save tool proposal settings.

    When auto_approve_enabled is True, new tool proposals from the LLM
    are automatically approved without requiring manual review.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        setting = db.query(SystemSetting).filter(
            SystemSetting.key == "tool_proposals_auto_approve"
        ).first()

        if setting:
            setting.value = str(settings.auto_approve_enabled).lower()
        else:
            setting = SystemSetting(
                key="tool_proposals_auto_approve",
                value=str(settings.auto_approve_enabled).lower(),
                description="When enabled, new tool proposals from LLM are automatically approved",
                category="tools"
            )
            db.add(setting)

        db.commit()

        logger.info(
            "tool_proposal_settings_saved",
            user=current_user.username,
            auto_approve_enabled=settings.auto_approve_enabled
        )

        return {
            "status": "success",
            "message": f"Tool proposal auto-approve {'enabled' if settings.auto_approve_enabled else 'disabled'}"
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_tool_proposal_settings", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to save tool proposal settings: {str(e)}")


# ============================================================================
# Directions Origin Placeholder Settings
# ============================================================================

class DirectionsOriginPlaceholderSettings(BaseModel):
    """Settings for placeholder values that LLMs use instead of real addresses."""
    placeholder_patterns: str  # Comma-separated list


class DirectionsOriginPlaceholderSettingsResponse(BaseModel):
    """Response model for directions origin placeholder settings."""
    placeholder_patterns: str
    patterns_list: list
    description: str


@router.get("/directions-origin-placeholders", response_model=DirectionsOriginPlaceholderSettingsResponse)
async def get_directions_origin_placeholders(
    db: Session = Depends(get_db)
):
    """
    Get directions origin placeholder patterns.

    This endpoint is public (no auth) to allow orchestrator to fetch settings.

    These are placeholder values that LLMs commonly use instead of real addresses.
    When detected as the origin in a get_directions call, they are replaced with
    the user's actual current location.

    Returns:
        - placeholder_patterns: Comma-separated list of patterns
        - patterns_list: Parsed list for easy consumption
    """
    try:
        setting = db.query(SystemSetting).filter(
            SystemSetting.key == "directions_origin_placeholders"
        ).first()

        default_patterns = "current location,my location,here,current,my current location,starting point,start,user location,your location,origin"

        if setting and setting.value:
            patterns = setting.value
        else:
            patterns = default_patterns

        patterns_list = [p.strip().lower() for p in patterns.split(",") if p.strip()]

        return DirectionsOriginPlaceholderSettingsResponse(
            placeholder_patterns=patterns,
            patterns_list=patterns_list,
            description=f"{len(patterns_list)} placeholder patterns configured"
        )

    except Exception as e:
        logger.error("failed_to_get_directions_origin_placeholders", error=str(e))
        default_patterns = "current location,my location,here,current,my current location,starting point,start,user location,your location,origin"
        return DirectionsOriginPlaceholderSettingsResponse(
            placeholder_patterns=default_patterns,
            patterns_list=[p.strip() for p in default_patterns.split(",")],
            description="Using default patterns (error fetching from database)"
        )


@router.post("/directions-origin-placeholders")
async def save_directions_origin_placeholders(
    settings: DirectionsOriginPlaceholderSettings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save directions origin placeholder patterns.

    When the LLM provides one of these values as the origin in a get_directions call,
    the orchestrator replaces it with the user's actual current location.

    Common patterns:
    - "current location", "my location" - Common LLM outputs
    - "here", "start" - Short placeholders
    - "user location", "origin" - System-like placeholders

    Args:
        placeholder_patterns: Comma-separated list of patterns to detect
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Normalize the patterns
        patterns = [p.strip().lower() for p in settings.placeholder_patterns.split(",") if p.strip()]
        normalized = ",".join(patterns)

        setting = db.query(SystemSetting).filter(
            SystemSetting.key == "directions_origin_placeholders"
        ).first()

        if setting:
            setting.value = normalized
        else:
            setting = SystemSetting(
                key="directions_origin_placeholders",
                value=normalized,
                description="Comma-separated list of placeholder values that LLMs use instead of real addresses. Detected as origin, replaced with user's actual location.",
                category="directions"
            )
            db.add(setting)

        db.commit()

        logger.info(
            "directions_origin_placeholders_saved",
            user=current_user.username,
            pattern_count=len(patterns)
        )

        return {
            "status": "success",
            "message": f"Saved {len(patterns)} placeholder patterns",
            "patterns": patterns
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_directions_origin_placeholders", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to save placeholder patterns: {str(e)}")


# ============================================================================
# Ollama URL Settings (Centralized LLM Backend Configuration)
# ============================================================================

class OllamaUrlSettings(BaseModel):
    """Ollama URL configuration."""
    ollama_url: str


class OllamaUrlResponse(BaseModel):
    """Response model for Ollama URL settings."""
    ollama_url: str
    is_reachable: bool = False
    version: Optional[str] = None
    error: Optional[str] = None


@router.get("/ollama-url", response_model=OllamaUrlResponse)
async def get_ollama_url(
    db: Session = Depends(get_db)
):
    """
    Get the centralized Ollama URL.

    This is the single source of truth for all LLM services.
    Public endpoint (no auth) to allow services to fetch the URL.

    Returns:
        - ollama_url: The configured Ollama API URL
        - is_reachable: Whether the URL is currently reachable
        - version: Ollama version if reachable
    """
    import httpx
    import os

    try:
        # Get from system_settings
        setting = db.query(SystemSetting).filter(
            SystemSetting.key == "ollama_url"
        ).first()

        # Fallback to environment variable if not in DB
        ollama_url = setting.value if setting else os.getenv("OLLAMA_URL", "http://localhost:11434")

        # Check if Ollama is reachable
        is_reachable = False
        version = None
        error = None

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{ollama_url}/api/version")
                if response.status_code == 200:
                    is_reachable = True
                    data = response.json()
                    version = data.get("version")
        except Exception as e:
            error = str(e)

        return OllamaUrlResponse(
            ollama_url=ollama_url,
            is_reachable=is_reachable,
            version=version,
            error=error
        )

    except Exception as e:
        logger.error("failed_to_get_ollama_url", error=str(e))
        return OllamaUrlResponse(
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
            is_reachable=False,
            error=str(e)
        )


@router.post("/ollama-url")
async def save_ollama_url(
    settings: OllamaUrlSettings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save the centralized Ollama URL.

    This sets the primary Ollama API URL used by all LLM services.
    Changes take effect immediately for new requests.

    Args:
        ollama_url: The Ollama API URL (e.g., http://192.168.1.100:11434)
    """
    import httpx

    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate URL format
    url = settings.ollama_url.strip().rstrip('/')
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    try:
        # Test connectivity before saving
        is_reachable = False
        version = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{url}/api/version")
                if response.status_code == 200:
                    is_reachable = True
                    data = response.json()
                    version = data.get("version")
        except Exception as e:
            logger.warning("ollama_url_not_reachable", url=url, error=str(e))

        # Save to system_settings
        setting = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
        if setting:
            setting.value = url
        else:
            setting = SystemSetting(
                key="ollama_url",
                value=url,
                description="Primary Ollama API URL. All LLM requests use this endpoint unless overridden per-model.",
                category="llm"
            )
            db.add(setting)

        # Also update llm_backends to use this URL (keep them in sync)
        from app.models import LLMBackend
        db.query(LLMBackend).filter(LLMBackend.backend_type == 'ollama').update(
            {"endpoint_url": url},
            synchronize_session=False
        )

        db.commit()

        logger.info(
            "ollama_url_saved",
            user=current_user.username,
            url=url,
            is_reachable=is_reachable
        )

        return {
            "status": "success",
            "message": "Ollama URL saved successfully",
            "ollama_url": url,
            "is_reachable": is_reachable,
            "version": version
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_ollama_url", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to save Ollama URL: {str(e)}")


@router.get("/ollama-url/internal")
async def get_ollama_url_internal(
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for services to fetch Ollama URL.

    No authentication required. Returns just the URL string for easy consumption.
    Used by orchestrator, gateway, and other services.
    """
    import os

    try:
        setting = db.query(SystemSetting).filter(
            SystemSetting.key == "ollama_url"
        ).first()

        url = setting.value if setting else os.getenv("OLLAMA_URL", "http://localhost:11434")
        return {"ollama_url": url}

    except Exception as e:
        logger.error("failed_to_get_ollama_url_internal", error=str(e))
        return {"ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434")}
