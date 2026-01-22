"""
RAG connector management API routes.

Provides CRUD operations for RAG connectors and testing functionality.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timedelta
import structlog
import aiohttp
import time

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, RAGConnector, ServiceRegistry, RAGStats, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/rag-connectors", tags=["rag-connectors"])


class RAGConnectorCreate(BaseModel):
    """Request model for creating a RAG connector."""
    name: str
    connector_type: str
    service_id: int = None
    enabled: bool = True
    config: dict = None
    cache_config: dict = None


class RAGConnectorUpdate(BaseModel):
    """Request model for updating a RAG connector."""
    connector_type: str = None
    service_id: int = None
    enabled: bool = None
    config: dict = None
    cache_config: dict = None


class RAGConnectorResponse(BaseModel):
    """Response model for RAG connector data."""
    id: int
    name: str
    connector_type: str
    service_id: Optional[int] = None
    service_name: Optional[str] = None
    enabled: bool
    config: Optional[dict] = None
    cache_config: Optional[dict] = None
    created_by: Optional[str] = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    connector: RAGConnector,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='rag_connector',
        resource_id=connector.id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='rag_connector', resource_id=connector.id)


@router.get("")
async def list_connectors(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all RAG connectors."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(RAGConnector)
    if enabled_only:
        query = query.filter(RAGConnector.enabled == True)

    connectors = query.order_by(RAGConnector.name).all()
    return {"connectors": [c.to_dict() for c in connectors]}


@router.get("/{connector_id}", response_model=RAGConnectorResponse)
async def get_connector(
    connector_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific RAG connector by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    return connector.to_dict()


@router.post("", response_model=RAGConnectorResponse, status_code=201)
async def create_connector(
    connector_data: RAGConnectorCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new RAG connector."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if connector name already exists
    existing = db.query(RAGConnector).filter(RAGConnector.name == connector_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Connector '{connector_data.name}' already exists")

    # Verify service exists if service_id provided
    if connector_data.service_id:
        service = db.query(ServiceRegistry).filter(ServiceRegistry.id == connector_data.service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail=f"Service ID {connector_data.service_id} not found")

    # Create connector
    connector = RAGConnector(
        name=connector_data.name,
        connector_type=connector_data.connector_type,
        service_id=connector_data.service_id,
        enabled=connector_data.enabled,
        config=connector_data.config,
        cache_config=connector_data.cache_config,
        created_by_id=current_user.id
    )
    db.add(connector)
    db.commit()
    db.refresh(connector)

    # Audit log
    create_audit_log(
        db, current_user, 'create', connector,
        new_value={'name': connector.name, 'type': connector.connector_type, 'enabled': connector.enabled},
        request=request
    )

    logger.info("rag_connector_created", connector_id=connector.id, name=connector.name, user=current_user.username)

    return connector.to_dict()


@router.put("/{connector_id}", response_model=RAGConnectorResponse)
async def update_connector(
    connector_id: int,
    connector_data: RAGConnectorUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing RAG connector."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    # Store old values for audit
    old_value = {
        'connector_type': connector.connector_type,
        'service_id': connector.service_id,
        'enabled': connector.enabled
    }

    # Update fields
    if connector_data.connector_type is not None:
        connector.connector_type = connector_data.connector_type
    if connector_data.service_id is not None:
        # Verify service exists
        service = db.query(ServiceRegistry).filter(ServiceRegistry.id == connector_data.service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail=f"Service ID {connector_data.service_id} not found")
        connector.service_id = connector_data.service_id
    if connector_data.enabled is not None:
        connector.enabled = connector_data.enabled
    if connector_data.config is not None:
        connector.config = connector_data.config
    if connector_data.cache_config is not None:
        connector.cache_config = connector_data.cache_config

    db.commit()
    db.refresh(connector)

    # Audit log
    new_value = {
        'connector_type': connector.connector_type,
        'service_id': connector.service_id,
        'enabled': connector.enabled
    }
    create_audit_log(db, current_user, 'update', connector, old_value=old_value, new_value=new_value, request=request)

    logger.info("rag_connector_updated", connector_id=connector.id, name=connector.name, user=current_user.username)

    return connector.to_dict()


@router.post("/{connector_id}/enable")
async def enable_connector(
    connector_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Enable a RAG connector."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    connector.enabled = True
    db.commit()

    create_audit_log(db, current_user, 'enable', connector, request=request)
    logger.info("rag_connector_enabled", connector_id=connector.id, name=connector.name)

    return {"connector_id": connector.id, "name": connector.name, "enabled": True}


@router.post("/{connector_id}/disable")
async def disable_connector(
    connector_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Disable a RAG connector."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    connector.enabled = False
    db.commit()

    create_audit_log(db, current_user, 'disable', connector, request=request)
    logger.info("rag_connector_disabled", connector_id=connector.id, name=connector.name)

    return {"connector_id": connector.id, "name": connector.name, "enabled": False}


@router.post("/{connector_id}/test")
async def test_connector(
    connector_id: int,
    test_query: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Test a RAG connector with a sample query."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    if not connector.service:
        return {
            "success": False,
            "error": "No service associated with this connector"
        }

    # Determine test based on connector name
    try:
        if "weather" in connector.name.lower():
            result = await test_weather_connector(connector, test_query)
        elif "airport" in connector.name.lower() or "flight" in connector.name.lower():
            result = await test_airports_connector(connector, test_query)
        elif "sport" in connector.name.lower():
            result = await test_sports_connector(connector, test_query)
        elif "qdrant" in connector.name.lower():
            result = await test_qdrant_connector(connector)
        elif "redis" in connector.name.lower():
            result = await test_redis_connector(connector)
        else:
            result = await test_generic_connector(connector)

        logger.info("rag_connector_tested", connector_id=connector.id, name=connector.name,
                   success=result.get('success'), user=current_user.username)

        return result

    except Exception as e:
        logger.error("rag_connector_test_failed", connector_id=connector.id, error=str(e))
        return {
            "success": False,
            "error": str(e)
        }


async def test_weather_connector(connector: RAGConnector, test_query: str = None):
    """Test weather RAG connector."""
    city = test_query or "Chicago"
    url = f"http://{connector.service.server.ip_address}:{connector.service.port}/weather?location={city}"

    start = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            elapsed = time.time() - start
            if resp.status == 200:
                data = await resp.json()
                return {
                    "success": True,
                    "response_time": int(elapsed * 1000),
                    "sample_data": data,
                    "cached": resp.headers.get('X-Cache-Hit', 'false') == 'true',
                    "test_query": city
                }
            else:
                return {"success": False, "error": f"HTTP {resp.status}"}


async def test_airports_connector(connector: RAGConnector, test_query: str = None):
    """Test airports/flights RAG connector."""
    airport = test_query or "ORD"
    url = f"http://{connector.service.server.ip_address}:{connector.service.port}/airport?code={airport}"

    start = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            elapsed = time.time() - start
            if resp.status == 200:
                data = await resp.json()
                return {
                    "success": True,
                    "response_time": int(elapsed * 1000),
                    "sample_data": data,
                    "cached": resp.headers.get('X-Cache-Hit', 'false') == 'true',
                    "test_query": airport
                }
            else:
                return {"success": False, "error": f"HTTP {resp.status}"}


async def test_sports_connector(connector: RAGConnector, test_query: str = None):
    """Test sports RAG connector."""
    team = test_query or "Chicago Bulls"
    url = f"http://{connector.service.server.ip_address}:{connector.service.port}/team?name={team}"

    start = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            elapsed = time.time() - start
            if resp.status == 200:
                data = await resp.json()
                return {
                    "success": True,
                    "response_time": int(elapsed * 1000),
                    "sample_data": data,
                    "cached": resp.headers.get('X-Cache-Hit', 'false') == 'true',
                    "test_query": team
                }
            else:
                return {"success": False, "error": f"HTTP {resp.status}"}


async def test_qdrant_connector(connector: RAGConnector):
    """Test Qdrant vector database connector."""
    url = f"http://{connector.service.server.ip_address}:{connector.service.port}/"

    start = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            elapsed = time.time() - start
            if resp.status == 200:
                # Get collections
                async with session.get(f"{url}collections") as coll_resp:
                    collections = await coll_resp.json() if coll_resp.status == 200 else {}

                return {
                    "success": True,
                    "response_time": int(elapsed * 1000),
                    "sample_data": collections,
                    "service_type": "qdrant"
                }
            else:
                return {"success": False, "error": f"HTTP {resp.status}"}


async def test_redis_connector(connector: RAGConnector):
    """Test Redis cache connector."""
    import socket

    # TCP connection test
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((connector.service.server.ip_address, connector.service.port))
        sock.close()
        elapsed = time.time() - start

        if result == 0:
            return {
                "success": True,
                "response_time": int(elapsed * 1000),
                "service_type": "redis",
                "note": "TCP connection successful (PING command not tested)"
            }
        else:
            return {"success": False, "error": "Connection refused"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def test_generic_connector(connector: RAGConnector):
    """Test generic HTTP connector."""
    if not connector.service.health_endpoint:
        return {"success": False, "error": "No health endpoint configured"}

    url = f"{connector.service.protocol}://{connector.service.server.ip_address}:{connector.service.port}{connector.service.health_endpoint}"

    start = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            elapsed = time.time() - start
            if resp.status == 200:
                return {
                    "success": True,
                    "response_time": int(elapsed * 1000),
                    "service_type": "generic"
                }
            else:
                return {"success": False, "error": f"HTTP {resp.status}"}


@router.get("/{connector_id}/stats")
async def get_connector_stats(
    connector_id: int,
    time_range: str = "1h",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get statistics for a RAG connector."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    # Parse time range
    time_ranges = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30)
    }
    delta = time_ranges.get(time_range, timedelta(hours=1))
    since = datetime.utcnow() - delta

    # Query stats
    stats = db.query(RAGStats).filter(
        RAGStats.connector_id == connector_id,
        RAGStats.timestamp >= since
    ).order_by(RAGStats.timestamp.desc()).all()

    if not stats:
        return {
            "connector_id": connector_id,
            "connector_name": connector.name,
            "time_range": time_range,
            "stats": [],
            "summary": {
                "total_requests": 0,
                "total_cache_hits": 0,
                "total_cache_misses": 0,
                "cache_hit_rate": 0.0,
                "avg_response_time": 0
            }
        }

    # Calculate summary
    total_requests = sum(s.requests_count for s in stats)
    total_cache_hits = sum(s.cache_hits for s in stats)
    total_cache_misses = sum(s.cache_misses for s in stats)
    cache_hit_rate = (total_cache_hits / (total_cache_hits + total_cache_misses) * 100) if (total_cache_hits + total_cache_misses) > 0 else 0.0
    avg_response_time = sum(s.avg_response_time for s in stats if s.avg_response_time) / len([s for s in stats if s.avg_response_time])

    return {
        "connector_id": connector_id,
        "connector_name": connector.name,
        "time_range": time_range,
        "stats": [
            {
                "timestamp": s.timestamp.isoformat(),
                "requests_count": s.requests_count,
                "cache_hits": s.cache_hits,
                "cache_misses": s.cache_misses,
                "avg_response_time": s.avg_response_time,
                "error_count": s.error_count
            }
            for s in stats
        ],
        "summary": {
            "total_requests": total_requests,
            "total_cache_hits": total_cache_hits,
            "total_cache_misses": total_cache_misses,
            "cache_hit_rate": round(cache_hit_rate, 2),
            "avg_response_time": int(avg_response_time) if avg_response_time else 0
        }
    }


@router.get("/{connector_id}/cache")
async def get_cache_info(
    connector_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get cache information for a RAG connector."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    return {
        "connector_id": connector.id,
        "connector_name": connector.name,
        "cache_config": connector.cache_config or {},
        "note": "Cache details retrieved from configuration"
    }


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a RAG connector."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connector = db.query(RAGConnector).filter(RAGConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    connector_name = connector.name

    # Audit log before deletion
    old_value = {'name': connector.name, 'type': connector.connector_type}
    create_audit_log(db, current_user, 'delete', connector, old_value=old_value, request=request)

    # Delete
    db.delete(connector)
    db.commit()

    logger.info("rag_connector_deleted", connector_id=connector_id, name=connector_name, user=current_user.username)

    return None
