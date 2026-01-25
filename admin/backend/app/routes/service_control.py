"""
Service Control Routes

Start, stop, restart Athena services and Ollama models.
Uses Control Agent pattern for secure service management.
"""

from datetime import datetime
from typing import List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
import structlog
import httpx
import asyncio

from app.database import get_db
from app.models import AthenaService, User, LLMBackend, SystemSetting
from app.auth.oidc import get_current_user
import os

logger = structlog.get_logger()
router = APIRouter(prefix="/api/service-control", tags=["service-control"])

# Configuration from environment
CONTROL_AGENT_URL = os.getenv("CONTROL_AGENT_URL", "http://localhost:8099")


def get_ollama_url(db: Session) -> str:
    """
    Get centralized Ollama URL from system_settings.

    The system_settings table is the single source of truth.
    Falls back to OLLAMA_URL environment variable if not set.
    """
    setting = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    if setting and setting.value:
        return setting.value
    return os.getenv("OLLAMA_URL", "http://localhost:11434")


# Pydantic Models
class ServiceResponse(BaseModel):
    id: int
    service_name: str
    display_name: str
    description: Optional[str]
    service_type: str
    host: str
    port: int
    health_endpoint: str
    control_method: str
    container_name: Optional[str]
    is_running: bool
    last_health_check: Optional[str]
    last_error: Optional[str]
    auto_start: bool
    enabled: bool

    class Config:
        from_attributes = True


class ServiceActionResponse(BaseModel):
    service_name: str
    action: str
    success: bool
    message: str


class OllamaModelResponse(BaseModel):
    name: str
    size: int
    loaded: bool
    modified_at: str


class ModelActionResponse(BaseModel):
    model_name: str
    action: str
    success: bool
    message: str


# Service Routes
@router.get("", response_model=List[ServiceResponse])
async def list_services(
    service_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all Athena services."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(AthenaService)
    if service_type:
        query = query.filter(AthenaService.service_type == service_type)

    services = query.order_by(AthenaService.service_type, AthenaService.display_name).all()
    return [ServiceResponse(**s.to_dict()) for s in services]


@router.get("/public", response_model=List[ServiceResponse])
async def list_services_public(
    db: Session = Depends(get_db)
):
    """
    List services (public endpoint for service discovery).
    No authentication required.
    """
    services = db.query(AthenaService).filter(AthenaService.enabled == True).all()
    return [ServiceResponse(**s.to_dict()) for s in services]


@router.post("/refresh-status")
async def refresh_all_service_status(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Trigger health check refresh for all services."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    background_tasks.add_task(check_all_services_health, db)

    return {"message": "Health check refresh started", "status": "pending"}


@router.post("/{service_name}/start", response_model=ServiceActionResponse)
async def start_service(
    service_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Start an Athena service."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(AthenaService).filter(AthenaService.service_name == service_name).first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    success, message = await execute_service_action(service, "start")

    if success:
        service.is_running = True
        service.last_error = None
        db.commit()

    logger.info("service_start", service=service_name, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=service_name,
        action="start",
        success=success,
        message=message
    )


@router.post("/{service_name}/stop", response_model=ServiceActionResponse)
async def stop_service(
    service_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Stop an Athena service."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(AthenaService).filter(AthenaService.service_name == service_name).first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    success, message = await execute_service_action(service, "stop")

    if success:
        service.is_running = False
        db.commit()

    logger.info("service_stop", service=service_name, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=service_name,
        action="stop",
        success=success,
        message=message
    )


@router.post("/{service_name}/restart", response_model=ServiceActionResponse)
async def restart_service(
    service_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Restart an Athena service."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(AthenaService).filter(AthenaService.service_name == service_name).first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    success, message = await execute_service_action(service, "restart")

    logger.info("service_restart", service=service_name, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=service_name,
        action="restart",
        success=success,
        message=message
    )


# Container Status Route (via Control Agent)
@router.get("/containers/status")
async def get_containers_status(
    current_user: User = Depends(get_current_user)
):
    """Get real-time status of Athena containers from Control Agent."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{CONTROL_AGENT_URL}/docker/list")

            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Control Agent error"
                )

    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Control Agent not reachable. Start it on Mac Studio."
        )
    except Exception as e:
        logger.error("container_status_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# Ollama Model Control Routes
@router.get("/ollama/models", response_model=List[OllamaModelResponse])
async def list_ollama_models(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all models available in Ollama."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Use centralized Ollama URL from system_settings
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get available models
            tags_response = await client.get(f"{ollama_url}/api/tags")
            tags_response.raise_for_status()
            available = tags_response.json().get("models", [])

            # Get currently loaded models
            ps_response = await client.get(f"{ollama_url}/api/ps")
            ps_response.raise_for_status()
            loaded_models = [m["name"] for m in ps_response.json().get("models", [])]

        models = []
        for model in available:
            models.append(OllamaModelResponse(
                name=model["name"],
                size=model.get("size", 0),
                loaded=model["name"] in loaded_models,
                modified_at=model.get("modified_at", "")
            ))

        return models

    except Exception as e:
        logger.error("ollama_models_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to list models: {str(e)}")


@router.post("/ollama/models/{model_name:path}/load", response_model=ModelActionResponse)
async def load_ollama_model(
    model_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Load a model into Ollama memory."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Use centralized Ollama URL from system_settings
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Send a simple generate request to load the model
            response = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model_name, "prompt": "hello", "stream": False}
            )
            response.raise_for_status()

        logger.info("ollama_model_loaded", model=model_name, user=current_user.username)

        return ModelActionResponse(
            model_name=model_name,
            action="load",
            success=True,
            message=f"Model '{model_name}' loaded successfully"
        )

    except Exception as e:
        logger.error("ollama_model_load_failed", model=model_name, error=str(e))
        return ModelActionResponse(
            model_name=model_name,
            action="load",
            success=False,
            message=f"Failed to load model: {str(e)}"
        )


@router.post("/ollama/models/{model_name:path}/unload", response_model=ModelActionResponse)
async def unload_ollama_model(
    model_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Unload a model from Ollama memory."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Use centralized Ollama URL from system_settings
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Ollama unloads via generate with keep_alive=0
            response = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model_name, "prompt": "", "keep_alive": 0}
            )
            response.raise_for_status()

        logger.info("ollama_model_unloaded", model=model_name, user=current_user.username)

        return ModelActionResponse(
            model_name=model_name,
            action="unload",
            success=True,
            message=f"Model '{model_name}' unloaded successfully"
        )

    except Exception as e:
        logger.error("ollama_model_unload_failed", model=model_name, error=str(e))
        return ModelActionResponse(
            model_name=model_name,
            action="unload",
            success=False,
            message=f"Failed to unload model: {str(e)}"
        )


# Helper Functions
async def execute_service_action(service: AthenaService, action: str) -> Tuple[bool, str]:
    """Execute start/stop/restart action on a service."""
    if service.control_method == "docker":
        return await docker_service_action(service.container_name, action)
    elif service.control_method == "process":
        return await process_service_action(service.port, action)
    elif service.control_method == "launchd":
        return await launchd_service_action(service.service_name, action)
    elif service.control_method == "none":
        return False, f"Service '{service.service_name}' does not support control actions"
    else:
        return False, f"Unknown control method: {service.control_method}"


async def docker_service_action(container_name: str, action: str) -> Tuple[bool, str]:
    """
    Execute Docker container action via Control Agent.

    Control Agent provides
    HTTP endpoints for secure Docker container management.
    """
    try:
        async with httpx.AsyncClient(timeout=65.0) as client:
            # Map action to Control Agent endpoint
            response = await client.post(
                f"{CONTROL_AGENT_URL}/docker/{action}/{container_name}"
            )

            if response.status_code == 200:
                result = response.json()
                return result.get("success", False), result.get("message", "Unknown response")
            elif response.status_code == 403:
                return False, f"Container '{container_name}' not allowed by Control Agent"
            else:
                return False, f"Control Agent returned status {response.status_code}"

    except httpx.ConnectError:
        logger.warning("control_agent_unreachable", container=container_name, action=action)
        return False, f"Control Agent not reachable. Start it on Mac Studio: python -m control_agent.main"
    except httpx.TimeoutException:
        return False, "Control Agent request timed out"
    except Exception as e:
        logger.error("docker_action_failed", container=container_name, action=action, error=str(e))
        return False, f"Docker control failed: {str(e)}"


async def process_service_action(port: int, action: str) -> Tuple[bool, str]:
    """
    Execute Python process action via Control Agent.

    Control Agent provides
    HTTP endpoints for managing Python/uvicorn processes by port.
    """
    try:
        async with httpx.AsyncClient(timeout=65.0) as client:
            # Map action to Control Agent process endpoint
            response = await client.post(
                f"{CONTROL_AGENT_URL}/process/{action}/{port}"
            )

            if response.status_code == 200:
                result = response.json()
                return result.get("success", False), result.get("message", "Unknown response")
            elif response.status_code == 403:
                return False, f"Port {port} not allowed by Control Agent"
            else:
                return False, f"Control Agent returned status {response.status_code}"

    except httpx.ConnectError:
        logger.warning("control_agent_unreachable", port=port, action=action)
        return False, f"Control Agent not reachable. Start it on Mac Studio: python -m control_agent.main"
    except httpx.TimeoutException:
        return False, "Control Agent request timed out"
    except Exception as e:
        logger.error("process_action_failed", port=port, action=action, error=str(e))
        return False, f"Process control failed: {str(e)}"


async def launchd_service_action(service_name: str, action: str) -> Tuple[bool, str]:
    """
    Execute launchd service action via Control Agent.

    Supports Ollama service start/stop/restart on macOS via brew services.
    """
    # For Ollama, map to the specific endpoint
    if "ollama" in service_name.lower():
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if action == "restart":
                    response = await client.post(f"{CONTROL_AGENT_URL}/ollama/restart")
                elif action == "start":
                    response = await client.post(f"{CONTROL_AGENT_URL}/ollama/start")
                elif action == "stop":
                    response = await client.post(f"{CONTROL_AGENT_URL}/ollama/stop")
                elif action == "status":
                    response = await client.get(f"{CONTROL_AGENT_URL}/ollama/status")
                else:
                    return False, f"Unsupported action '{action}' for Ollama service"

                if response.status_code == 200:
                    result = response.json()
                    return result.get("success", False), result.get("message", "Unknown response")
                else:
                    return False, f"Control Agent returned status {response.status_code}"

        except httpx.ConnectError:
            logger.warning("control_agent_unreachable", service=service_name, action=action)
            return False, f"Control Agent not reachable. Start it on Mac Studio: python -m control_agent.main"
        except Exception as e:
            logger.error("launchd_action_failed", service=service_name, action=action, error=str(e))
            return False, f"Launchd control failed: {str(e)}"

    return False, f"Launchd control not implemented for service: {service_name}"


async def check_all_services_health(db: Session):
    """Background task to check health of all services."""
    services = db.query(AthenaService).filter(AthenaService.enabled == True).all()

    async with httpx.AsyncClient(timeout=5.0) as client:
        for service in services:
            try:
                url = f"http://{service.host}:{service.port}{service.health_endpoint}"
                response = await client.get(url)

                service.is_running = response.status_code == 200
                service.last_error = None if service.is_running else f"HTTP {response.status_code}"
                service.last_health_check = datetime.utcnow()

            except Exception as e:
                service.is_running = False
                service.last_error = str(e)[:500]  # Truncate long errors
                service.last_health_check = datetime.utcnow()

    db.commit()
    logger.info("service_health_check_complete", checked=len(services))


# Ollama Health Response Model
class OllamaHealthResponse(BaseModel):
    healthy: bool
    status: str
    api_reachable: bool
    models_loaded: int
    version: Optional[str] = None
    timestamp: str
    host: Optional[str] = None


@router.get("/ollama/health", response_model=OllamaHealthResponse)
async def get_ollama_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get Ollama health status via Control Agent.

    Returns actual API reachability, not just brew services status.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Get centralized Ollama URL for display
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{CONTROL_AGENT_URL}/ollama/health")

            if response.status_code == 200:
                data = response.json()
                data['host'] = ollama_url
                return OllamaHealthResponse(**data)
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Control Agent error"
                )

    except httpx.ConnectError:
        # Control Agent not reachable - return unhealthy status
        from datetime import datetime
        return OllamaHealthResponse(
            healthy=False,
            status="control_agent_offline",
            api_reachable=False,
            models_loaded=0,
            version=None,
            timestamp=datetime.utcnow().isoformat(),
            host=ollama_url
        )
    except Exception as e:
        logger.error("ollama_health_check_failed", error=str(e))
        from datetime import datetime
        return OllamaHealthResponse(
            healthy=False,
            status="error",
            api_reachable=False,
            models_loaded=0,
            version=None,
            timestamp=datetime.utcnow().isoformat(),
            host=ollama_url
        )


@router.post("/ollama/start", response_model=ServiceActionResponse)
async def start_ollama(
    current_user: User = Depends(get_current_user)
):
    """Start Ollama service via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await launchd_service_action("ollama", "start")

    logger.info("ollama_start", success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name="ollama",
        action="start",
        success=success,
        message=message
    )


@router.post("/ollama/stop", response_model=ServiceActionResponse)
async def stop_ollama(
    current_user: User = Depends(get_current_user)
):
    """Stop Ollama service via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await launchd_service_action("ollama", "stop")

    logger.info("ollama_stop", success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name="ollama",
        action="stop",
        success=success,
        message=message
    )


@router.post("/ollama/restart", response_model=ServiceActionResponse)
async def restart_ollama(
    current_user: User = Depends(get_current_user)
):
    """Restart Ollama service via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await launchd_service_action("ollama", "restart")

    logger.info("ollama_restart", success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name="ollama",
        action="restart",
        success=success,
        message=message
    )


# Port-based service control routes (for Voice Pipelines UI)
@router.post("/port/{port}/start", response_model=ServiceActionResponse)
async def start_service_by_port(
    port: int,
    current_user: User = Depends(get_current_user)
):
    """Start a Python process service by port via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await process_service_action(port, "start")

    logger.info("service_start_by_port", port=port, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=f"port-{port}",
        action="start",
        success=success,
        message=message
    )


@router.post("/port/{port}/stop", response_model=ServiceActionResponse)
async def stop_service_by_port(
    port: int,
    current_user: User = Depends(get_current_user)
):
    """Stop a Python process service by port via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await process_service_action(port, "stop")

    logger.info("service_stop_by_port", port=port, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=f"port-{port}",
        action="stop",
        success=success,
        message=message
    )


@router.post("/port/{port}/restart", response_model=ServiceActionResponse)
async def restart_service_by_port(
    port: int,
    current_user: User = Depends(get_current_user)
):
    """Restart a Python process service by port via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await process_service_action(port, "restart")

    logger.info("service_restart_by_port", port=port, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=f"port-{port}",
        action="restart",
        success=success,
        message=message
    )
