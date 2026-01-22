"""
Feature Flag Management API Routes.

Provides CRUD operations for system feature flags and latency impact analysis.
Enables toggling features on/off and performing what-if analysis on system performance.
"""

import asyncio
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel, Field
import structlog
import httpx

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Feature, LLMPerformanceMetric

import os

logger = structlog.get_logger()

router = APIRouter(prefix="/api/features", tags=["features"])

# Service URLs from environment
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")

# Service endpoints for cache invalidation
CACHE_INVALIDATION_ENDPOINTS = [
    f"{GATEWAY_URL}/admin/invalidate-feature-cache",
    f"{ORCHESTRATOR_URL}/admin/invalidate-feature-cache",
]


async def _notify_services_of_flag_change(flag_names: List[str]):
    """
    Notify Gateway and Orchestrator to invalidate their feature flag caches.

    This enables instant propagation of feature flag changes without waiting
    for cache TTL expiration.

    Args:
        flag_names: List of flag names that changed
    """
    async with httpx.AsyncClient(timeout=2.0) as client:
        tasks = []
        for endpoint in CACHE_INVALIDATION_ENDPOINTS:
            tasks.append(
                client.post(endpoint, json={"flags": flag_names})
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for endpoint, result in zip(CACHE_INVALIDATION_ENDPOINTS, results):
            if isinstance(result, Exception):
                logger.warning(
                    "cache_invalidation_failed",
                    endpoint=endpoint,
                    error=str(result)
                )
            else:
                logger.info(
                    "cache_invalidated",
                    endpoint=endpoint,
                    status_code=result.status_code
                )


# Pydantic models for request/response
class FeatureResponse(BaseModel):
    """Response model for feature data."""
    id: int
    name: str
    display_name: str
    description: Optional[str] = None
    category: str
    enabled: bool
    avg_latency_ms: Optional[float] = None
    hit_rate: Optional[float] = None
    required: Optional[bool] = False  # Default to not required if NULL
    priority: Optional[int] = 0  # Default to 0 if NULL
    config: Optional[Dict[str, Any]] = None  # Feature-specific configuration (e.g., mode selection)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class FeatureUpdate(BaseModel):
    """Request model for updating feature configuration."""
    enabled: Optional[bool] = None
    avg_latency_ms: Optional[float] = None
    hit_rate: Optional[float] = None


class FeatureConfigUpdate(BaseModel):
    """Request model for updating feature-specific config (JSONB field)."""
    mode: Optional[str] = None
    available_modes: Optional[List[str]] = None
    # Generic config fields - allows any key-value pairs
    class Config:
        extra = "allow"  # Allow additional fields for flexible config


class FeatureImpact(BaseModel):
    """Response model for feature latency impact."""
    feature_name: str
    display_name: str
    category: str
    enabled: bool
    avg_latency_ms: float
    request_count: int
    percent_of_total: float


class WhatIfScenario(BaseModel):
    """Response model for what-if analysis."""
    scenario_name: str
    description: str
    total_latency_ms: float
    change_from_current_ms: float
    change_percent: float
    features_enabled: Dict[str, bool]


# API Routes

# Public endpoint (no auth) for services to query feature flags
@router.get("/public", response_model=List[FeatureResponse])
async def list_features_public(
    category: Optional[str] = None,
    enabled_only: bool = False,
    db: Session = Depends(get_db)
):
    """
    List all system features (public endpoint, no auth required).

    This endpoint is used by services (Gateway, Orchestrator, etc.) to check
    feature flag configuration without requiring authentication.

    Query params:
    - category: Filter by category (optional)
    - enabled_only: If true, only return enabled features

    Returns:
        List of features with their configuration
    """
    logger.info("list_features_public", category=category, enabled_only=enabled_only, source="public")

    query = db.query(Feature)

    if category:
        query = query.filter(Feature.category == category)
    if enabled_only:
        query = query.filter(Feature.enabled == True)

    features = query.order_by(Feature.category, Feature.priority).all()

    return [FeatureResponse(**feature.to_dict()) for feature in features]


@router.get("", response_model=List[FeatureResponse])
async def list_features(
    category: Optional[str] = None,
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all system features (authenticated endpoint).

    Query params:
    - category: Filter by category (optional)
    - enabled_only: If true, only return enabled features

    Returns:
        List of features with their configuration and performance metrics
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_features", user=current_user.username, category=category, enabled_only=enabled_only)

    query = db.query(Feature)

    if category:
        query = query.filter(Feature.category == category)
    if enabled_only:
        query = query.filter(Feature.enabled == True)

    features = query.order_by(Feature.category, Feature.priority).all()

    return [FeatureResponse(**feature.to_dict()) for feature in features]


@router.get("/{feature_id}", response_model=FeatureResponse)
async def get_feature(
    feature_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get specific feature by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    feature = db.query(Feature).filter(Feature.id == feature_id).first()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    logger.info("get_feature", feature_id=feature_id, user=current_user.username)

    return FeatureResponse(**feature.to_dict())


@router.put("/{feature_id}/toggle", response_model=FeatureResponse)
async def toggle_feature(
    feature_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Toggle feature enabled/disabled status.

    Cannot toggle required features (they must always be enabled).
    Automatically notifies Gateway and Orchestrator to invalidate caches.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    feature = db.query(Feature).filter(Feature.id == feature_id).first()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    # Check if feature is required
    if feature.required and feature.enabled:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot disable required feature '{feature.display_name}'"
        )

    feature.enabled = not feature.enabled
    db.commit()
    db.refresh(feature)

    logger.info(
        "toggle_feature",
        feature_id=feature_id,
        feature_name=feature.name,
        enabled=feature.enabled,
        user=current_user.username
    )

    # Notify services to invalidate cache immediately
    asyncio.create_task(_notify_services_of_flag_change([feature.name]))
    logger.info(
        "feature_flag_cache_invalidation_triggered",
        feature=feature.name,
        enabled=feature.enabled
    )

    return FeatureResponse(**feature.to_dict())


@router.put("/service/{feature_id}/toggle", response_model=FeatureResponse)
async def service_toggle_feature(
    feature_id: int,
    db: Session = Depends(get_db),
    api_key: str = Header(None, alias="X-API-Key")
):
    """
    Service-to-service toggle feature endpoint.

    Allows services to toggle features using API key authentication
    instead of OIDC. Useful for testing and automation.

    Headers:
    - X-API-Key: Service API key

    Cannot toggle required features (they must always be enabled).
    Automatically notifies Gateway and Orchestrator to invalidate caches.
    """
    import os

    # Verify service API key
    expected_key = os.getenv("SERVICE_API_KEY", "dev-service-key-change-in-production")
    if not api_key or api_key != expected_key:
        logger.warning("service_toggle_feature_unauthorized", provided_key=bool(api_key))
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

    feature = db.query(Feature).filter(Feature.id == feature_id).first()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    # Check if feature is required
    if feature.required and feature.enabled:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot disable required feature '{feature.display_name}'"
        )

    feature.enabled = not feature.enabled
    db.commit()
    db.refresh(feature)

    logger.info(
        "service_toggle_feature",
        feature_id=feature_id,
        feature_name=feature.name,
        enabled=feature.enabled,
        source="service_api"
    )

    # Notify services to invalidate cache immediately
    asyncio.create_task(_notify_services_of_flag_change([feature.name]))

    return FeatureResponse(**feature.to_dict())


@router.put("/{feature_id}", response_model=FeatureResponse)
async def update_feature(
    feature_id: int,
    feature_data: FeatureUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update feature configuration.

    Automatically notifies Gateway and Orchestrator to invalidate caches
    when the enabled state changes.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    feature = db.query(Feature).filter(Feature.id == feature_id).first()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    # Check if trying to disable required feature
    if feature.required and feature_data.enabled is False:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot disable required feature '{feature.display_name}'"
        )

    # Track if enabled state changed
    enabled_changed = feature_data.enabled is not None and feature_data.enabled != feature.enabled

    # Update fields
    update_data = feature_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(feature, field, value)

    db.commit()
    db.refresh(feature)

    logger.info(
        "update_feature",
        feature_id=feature_id,
        feature_name=feature.name,
        updated_fields=list(update_data.keys()),
        user=current_user.username
    )

    # Notify services to invalidate cache if enabled state changed
    if enabled_changed:
        asyncio.create_task(_notify_services_of_flag_change([feature.name]))
        logger.info(
            "feature_flag_cache_invalidation_triggered",
            feature=feature.name,
            enabled=feature.enabled
        )

    return FeatureResponse(**feature.to_dict())


@router.put("/{feature_id}/config", response_model=FeatureResponse)
async def update_feature_config(
    feature_id: int,
    config_data: FeatureConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update feature-specific configuration (JSONB config field).

    Used for features with mode selection (e.g., automation_system_mode)
    or other feature-specific settings.

    Automatically notifies Gateway and Orchestrator to invalidate caches.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    feature = db.query(Feature).filter(Feature.id == feature_id).first()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    # Get existing config or initialize empty dict
    current_config = feature.config or {}

    # Update with new values (merge, don't replace)
    update_dict = config_data.model_dump(exclude_unset=True)
    current_config.update(update_dict)

    # Set the updated config
    feature.config = current_config
    db.commit()
    db.refresh(feature)

    logger.info(
        "update_feature_config",
        feature_id=feature_id,
        feature_name=feature.name,
        config=current_config,
        user=current_user.username
    )

    # Notify services to invalidate cache for config changes
    asyncio.create_task(_notify_services_of_flag_change([feature.name]))
    logger.info(
        "feature_config_cache_invalidation_triggered",
        feature=feature.name,
        config=current_config
    )

    return FeatureResponse(**feature.to_dict())


@router.get("/impact/analysis", response_model=List[FeatureImpact])
async def get_feature_impact(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Calculate latency impact of each feature based on historical data.

    Analyzes llm_performance_metrics to determine:
    - Average latency contribution per feature
    - Number of requests where feature was used
    - Percentage of total latency

    Returns:
        List of features with their performance impact metrics
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("get_feature_impact", user=current_user.username)

    # Get all features
    features = db.query(Feature).all()

    # Calculate total average latency from recent metrics
    total_avg_latency_raw = db.query(
        func.avg(LLMPerformanceMetric.latency_seconds) * 1000
    ).scalar()
    total_avg_latency = float(total_avg_latency_raw) if total_avg_latency_raw else 0.0

    impact_data = []

    for feature in features:
        # Calculate average latency for this feature
        # In real implementation, this would analyze component latencies
        # For now, use the feature's avg_latency_ms if available
        feature_latency = float(feature.avg_latency_ms) if feature.avg_latency_ms else 0.0

        # Count requests where this feature was enabled (from features_enabled JSONB)
        request_count = db.query(func.count(LLMPerformanceMetric.id)).filter(
            LLMPerformanceMetric.features_enabled.contains({feature.name: True})
        ).scalar() or 0

        percent_of_total = (feature_latency / total_avg_latency * 100) if total_avg_latency > 0 else 0.0

        impact_data.append(FeatureImpact(
            feature_name=feature.name,
            display_name=feature.display_name,
            category=feature.category,
            enabled=feature.enabled,
            avg_latency_ms=feature_latency,
            request_count=request_count,
            percent_of_total=percent_of_total
        ))

    # Sort by latency impact (highest first)
    impact_data.sort(key=lambda x: x.avg_latency_ms, reverse=True)

    return impact_data


@router.get("/what-if/scenarios", response_model=List[WhatIfScenario])
async def get_what_if_scenarios(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate what-if scenarios showing projected latency for different feature combinations.

    Scenarios:
    - Current configuration (baseline)
    - All optimizations enabled
    - No RAG (only direct LLM)
    - No caching
    - Minimal features (required only)

    Returns:
        List of scenarios with projected latency and enabled features
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("get_what_if_scenarios", user=current_user.username)

    # Get all features
    features = db.query(Feature).all()
    features_dict = {f.name: f for f in features}

    # Calculate current total latency (convert Decimal to float)
    current_latency = sum(float(f.avg_latency_ms) if f.avg_latency_ms else 0.0 for f in features if f.enabled)
    current_features = {f.name: f.enabled for f in features}

    scenarios = []

    # Scenario 1: Current configuration (baseline)
    scenarios.append(WhatIfScenario(
        scenario_name="Current Configuration",
        description="Current system configuration with all enabled features",
        total_latency_ms=current_latency,
        change_from_current_ms=0.0,
        change_percent=0.0,
        features_enabled=current_features
    ))

    # Scenario 2: All optimizations enabled
    optimized_features = current_features.copy()
    for name in ['redis_caching', 'mlx_backend', 'response_streaming']:
        if name in features_dict:
            optimized_features[name] = True

    optimized_latency = sum(
        float(features_dict[name].avg_latency_ms) if features_dict[name].avg_latency_ms else 0.0
        for name, enabled in optimized_features.items()
        if enabled and name in features_dict
    )

    scenarios.append(WhatIfScenario(
        scenario_name="All Optimizations",
        description="Enable all optimization features (caching, MLX, streaming)",
        total_latency_ms=optimized_latency,
        change_from_current_ms=optimized_latency - current_latency,
        change_percent=((optimized_latency - current_latency) / current_latency * 100) if current_latency > 0 else 0.0,
        features_enabled=optimized_features
    ))

    # Scenario 3: No RAG (disable all RAG features)
    no_rag_features = current_features.copy()
    for name in ['rag_weather', 'rag_sports', 'rag_airports']:
        if name in features_dict:
            no_rag_features[name] = False

    no_rag_latency = sum(
        float(features_dict[name].avg_latency_ms) if features_dict[name].avg_latency_ms else 0.0
        for name, enabled in no_rag_features.items()
        if enabled and name in features_dict
    )

    scenarios.append(WhatIfScenario(
        scenario_name="No RAG",
        description="Disable all RAG services (direct LLM only)",
        total_latency_ms=no_rag_latency,
        change_from_current_ms=no_rag_latency - current_latency,
        change_percent=((no_rag_latency - current_latency) / current_latency * 100) if current_latency > 0 else 0.0,
        features_enabled=no_rag_features
    ))

    # Scenario 4: No caching
    no_cache_features = current_features.copy()
    if 'redis_caching' in features_dict:
        no_cache_features['redis_caching'] = False

    no_cache_latency = sum(
        float(features_dict[name].avg_latency_ms) if features_dict[name].avg_latency_ms else 0.0
        for name, enabled in no_cache_features.items()
        if enabled and name in features_dict
    )

    scenarios.append(WhatIfScenario(
        scenario_name="No Caching",
        description="Disable Redis caching (all requests hit services)",
        total_latency_ms=no_cache_latency,
        change_from_current_ms=no_cache_latency - current_latency,
        change_percent=((no_cache_latency - current_latency) / current_latency * 100) if current_latency > 0 else 0.0,
        features_enabled=no_cache_features
    ))

    # Scenario 5: Minimal features (required only)
    # Convert None to False for features_enabled dict (Pydantic requires bool)
    minimal_features = {name: bool(f.required) for name, f in features_dict.items()}
    minimal_latency = sum(
        float(features_dict[name].avg_latency_ms) if features_dict[name].avg_latency_ms else 0.0
        for name, enabled in minimal_features.items()
        if enabled and name in features_dict
    )

    scenarios.append(WhatIfScenario(
        scenario_name="Minimal (Required Only)",
        description="Only required features enabled (bare minimum)",
        total_latency_ms=minimal_latency,
        change_from_current_ms=minimal_latency - current_latency,
        change_percent=((minimal_latency - current_latency) / current_latency * 100) if current_latency > 0 else 0.0,
        features_enabled=minimal_features
    ))

    return scenarios
