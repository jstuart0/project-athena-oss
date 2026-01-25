"""
Control Agent for Project Athena

Lightweight service that provides HTTP endpoints to control Docker containers
and Python process services.

This agent enables the admin backend to manage Athena services remotely.
Supports both Docker containers AND bare Python/uvicorn processes.
"""

import asyncio
import subprocess
import os
import signal
from typing import Optional, Dict
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import structlog

logger = structlog.get_logger()

# Project root for service directories
PROJECT_ROOT = Path.home() / "dev" / "project-athena"

# Ollama URL - configurable via environment variable
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

app = FastAPI(
    title="Athena Control Agent",
    description="Service control agent for Project Athena on Mac Studio (Docker + Process)",
    version="2.0.0"
)

# CORS for admin frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to admin frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic Models
class ActionResponse(BaseModel):
    success: bool
    action: str
    target: str
    message: str
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    hostname: str
    timestamp: str


class ContainerStatus(BaseModel):
    name: str
    status: str
    running: bool
    ports: Optional[str]


class ProcessStatus(BaseModel):
    port: int
    service_name: str
    pid: Optional[int]
    running: bool
    status: str


# =============================================================================
# PROCESS CONTROL CONFIGURATION
# =============================================================================
# Maps port -> (service_name, working_directory, startup_command)
# These are Python/uvicorn services running as bare processes

PROCESS_SERVICES: Dict[int, Dict] = {
    # Core services
    8000: {
        "name": "gateway",
        "dir": "src/gateway",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
    },
    8001: {
        "name": "orchestrator",
        "dir": "src/orchestrator",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"],
    },
    8003: {
        "name": "jarvis-web",
        "dir": "apps/jarvis-web/backend",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"],
    },
    # RAG services (under src/rag/) - ports match orchestrator/utils/constants.py
    8010: {
        "name": "weather-rag",
        "dir": "src/rag/weather",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"],
    },
    8011: {
        "name": "airports-rag",
        "dir": "src/rag/airports",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8011"],
    },
    8012: {
        "name": "stocks-rag",
        "dir": "src/rag/stocks",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8012"],
    },
    8013: {
        "name": "flights-rag",
        "dir": "src/rag/flights",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8013"],
    },
    8014: {
        "name": "events-rag",
        "dir": "src/rag/events",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8014"],
    },
    8015: {
        "name": "streaming-rag",
        "dir": "src/rag/streaming",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8015"],
    },
    8016: {
        "name": "news-rag",
        "dir": "src/rag/news",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8016"],
    },
    8017: {
        "name": "sports-rag",
        "dir": "src/rag/sports",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8017"],
    },
    8018: {
        "name": "websearch-rag",
        "dir": "src/rag/websearch",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8018"],
    },
    8019: {
        "name": "dining-rag",
        "dir": "src/rag/dining",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8019"],
    },
    8020: {
        "name": "recipes-rag",
        "dir": "src/rag/recipes",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8020"],
    },
    8023: {
        "name": "mode-service",
        "dir": "src/mode_service",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8023"],
    },
    8027: {
        "name": "amtrak-rag",
        "dir": "src/rag/amtrak",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8027"],
    },
    8030: {
        "name": "directions-rag",
        "dir": "src/rag/directions",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8030"],
    },
    8031: {
        "name": "site-scraper-rag",
        "dir": "src/rag/site_scraper",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8031"],
    },
    8032: {
        "name": "serpapi-events-rag",
        "dir": "src/rag/serpapi_events",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8032"],
    },
    8033: {
        "name": "price-compare-rag",
        "dir": "src/rag/price_compare",
        "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8033"],
    },
}


def is_port_allowed(port: int) -> bool:
    """Check if port is in the allowed process services list."""
    return port in PROCESS_SERVICES


async def get_pid_by_port(port: int) -> Optional[int]:
    """Find the PID of process LISTENING on a port (not client connections)."""
    try:
        # Use lsof with TCP state filter to only get LISTEN sockets
        process = await asyncio.create_subprocess_exec(
            "lsof", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()

        if process.returncode == 0 and stdout:
            # lsof may return multiple PIDs, take the first one
            pids = stdout.decode().strip().split('\n')
            return int(pids[0]) if pids[0] else None
        return None
    except Exception as e:
        logger.error("get_pid_failed", port=port, error=str(e))
        return None


async def stop_process_by_port(port: int) -> tuple[bool, str]:
    """Stop a process listening on a port."""
    pid = await get_pid_by_port(port)

    if not pid:
        return True, f"No process found on port {port} (already stopped)"

    try:
        os.kill(pid, signal.SIGTERM)

        # Wait for process to terminate (up to 10 seconds)
        for _ in range(20):
            await asyncio.sleep(0.5)
            check_pid = await get_pid_by_port(port)
            if not check_pid:
                return True, f"Process {pid} stopped successfully"

        # Force kill if still running
        os.kill(pid, signal.SIGKILL)
        await asyncio.sleep(1)

        return True, f"Process {pid} force killed"

    except ProcessLookupError:
        return True, f"Process {pid} already terminated"
    except PermissionError:
        return False, f"Permission denied to stop process {pid}"
    except Exception as e:
        return False, f"Failed to stop process: {str(e)}"


async def start_process_by_port(port: int) -> tuple[bool, str]:
    """Start a service on a port using its configured command."""
    if port not in PROCESS_SERVICES:
        return False, f"No service configuration for port {port}"

    # Check if already running
    existing_pid = await get_pid_by_port(port)
    if existing_pid:
        return False, f"Port {port} already in use by PID {existing_pid}"

    config = PROCESS_SERVICES[port]
    working_dir = PROJECT_ROOT / config["dir"]

    if not working_dir.exists():
        return False, f"Service directory not found: {working_dir}"

    try:
        # Use explicit Python path to avoid shell activation issues
        python_path = PROJECT_ROOT / ".venv" / "bin" / "python"

        # Build command with explicit Python path
        cmd_parts = config["cmd"].copy()
        if cmd_parts[0] == "python":
            cmd_parts[0] = str(python_path)
        cmd_str = f"cd {working_dir} && " + " ".join(cmd_parts)

        process = await asyncio.create_subprocess_shell(
            cmd_str,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(working_dir),
            start_new_session=True  # Detach from control agent
        )

        # Wait a moment for service to start
        await asyncio.sleep(2)

        # Verify it's running
        new_pid = await get_pid_by_port(port)
        if new_pid:
            return True, f"Service started on port {port} (PID {new_pid})"
        else:
            return False, f"Service failed to start on port {port}"

    except Exception as e:
        logger.error("start_process_failed", port=port, error=str(e))
        return False, f"Failed to start service: {str(e)}"


# =============================================================================
# DOCKER CONTROL CONFIGURATION
# =============================================================================
# Security: Whitelist of allowed containers to control
# These must match container_name in athena_services table
ALLOWED_CONTAINERS = {
    # Core services
    "athena-gateway",
    "athena-orchestrator",
    # RAG services (match migration container names)
    "athena-weather",
    "athena-airports",
    "athena-flights",
    "athena-news",
    "athena-stocks",
    "athena-recipes",
    "athena-events",
    "athena-sports",
    "athena-streaming",
    "athena-dining",
    "athena-websearch",
    # Infrastructure services
    "qdrant",
    "redis",
}


def is_container_allowed(container_name: str) -> bool:
    """Check if container is in whitelist."""
    return container_name in ALLOWED_CONTAINERS


async def run_docker_command(args: list) -> tuple[bool, str]:
    """Execute a docker command asynchronously."""
    try:
        cmd = ["docker"] + args
        logger.info("docker_command", cmd=" ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=60.0  # 60 second timeout
        )

        if process.returncode == 0:
            return True, stdout.decode().strip() or "Success"
        else:
            return False, stderr.decode().strip() or f"Exit code: {process.returncode}"

    except asyncio.TimeoutError:
        return False, "Command timed out after 60 seconds"
    except Exception as e:
        logger.error("docker_command_failed", error=str(e))
        return False, str(e)


# Health Endpoint
@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    import socket
    return HealthResponse(
        status="healthy",
        hostname=socket.gethostname(),
        timestamp=datetime.utcnow().isoformat()
    )


# Docker Container Control
@app.post("/docker/start/{container_name}", response_model=ActionResponse)
async def start_container(container_name: str):
    """Start a Docker container."""
    if not is_container_allowed(container_name):
        raise HTTPException(
            status_code=403,
            detail=f"Container '{container_name}' is not in the allowed list"
        )

    success, message = await run_docker_command(["start", container_name])

    logger.info("container_start", container=container_name, success=success)

    return ActionResponse(
        success=success,
        action="start",
        target=container_name,
        message=message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.post("/docker/stop/{container_name}", response_model=ActionResponse)
async def stop_container(container_name: str):
    """Stop a Docker container."""
    if not is_container_allowed(container_name):
        raise HTTPException(
            status_code=403,
            detail=f"Container '{container_name}' is not in the allowed list"
        )

    success, message = await run_docker_command(["stop", container_name])

    logger.info("container_stop", container=container_name, success=success)

    return ActionResponse(
        success=success,
        action="stop",
        target=container_name,
        message=message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.post("/docker/restart/{container_name}", response_model=ActionResponse)
async def restart_container(container_name: str):
    """Restart a Docker container."""
    if not is_container_allowed(container_name):
        raise HTTPException(
            status_code=403,
            detail=f"Container '{container_name}' is not in the allowed list"
        )

    success, message = await run_docker_command(["restart", container_name])

    logger.info("container_restart", container=container_name, success=success)

    return ActionResponse(
        success=success,
        action="restart",
        target=container_name,
        message=message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.get("/docker/status/{container_name}", response_model=ContainerStatus)
async def container_status(container_name: str):
    """Get status of a Docker container."""
    if not is_container_allowed(container_name):
        raise HTTPException(
            status_code=403,
            detail=f"Container '{container_name}' is not in the allowed list"
        )

    # Get container status with formatting
    success, output = await run_docker_command([
        "ps", "-a",
        "--filter", f"name=^{container_name}$",
        "--format", "{{.Status}}|{{.Ports}}"
    ])

    if not success or not output:
        return ContainerStatus(
            name=container_name,
            status="not found",
            running=False,
            ports=None
        )

    parts = output.split("|")
    status = parts[0] if parts else "unknown"
    ports = parts[1] if len(parts) > 1 else None
    running = "Up" in status

    return ContainerStatus(
        name=container_name,
        status=status,
        running=running,
        ports=ports
    )


@app.get("/docker/list", response_model=list[ContainerStatus])
async def list_containers():
    """List all Athena-related containers."""
    success, output = await run_docker_command([
        "ps", "-a",
        "--filter", "name=athena",
        "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"
    ])

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to list containers: {output}")

    containers = []
    for line in output.split("\n"):
        if line:
            parts = line.split("|")
            name = parts[0] if parts else ""
            status = parts[1] if len(parts) > 1 else "unknown"
            ports = parts[2] if len(parts) > 2 else None

            containers.append(ContainerStatus(
                name=name,
                status=status,
                running="Up" in status,
                ports=ports
            ))

    return containers


# Ollama Model Control (via launchd/brew services)
@app.post("/ollama/restart", response_model=ActionResponse)
async def restart_ollama():
    """Restart Ollama service using brew services."""
    try:
        # Stop ollama
        stop_process = await asyncio.create_subprocess_exec(
            "brew", "services", "stop", "ollama",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await stop_process.wait()

        # Wait a moment
        await asyncio.sleep(2)

        # Start ollama
        start_process = await asyncio.create_subprocess_exec(
            "brew", "services", "start", "ollama",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await start_process.communicate()

        success = start_process.returncode == 0
        message = stdout.decode().strip() if success else stderr.decode().strip()

        logger.info("ollama_restart", success=success)

        return ActionResponse(
            success=success,
            action="restart",
            target="ollama",
            message=message or "Ollama restarted",
            timestamp=datetime.utcnow().isoformat()
        )

    except Exception as e:
        logger.error("ollama_restart_failed", error=str(e))
        return ActionResponse(
            success=False,
            action="restart",
            target="ollama",
            message=str(e),
            timestamp=datetime.utcnow().isoformat()
        )


@app.get("/ollama/status", response_model=ActionResponse)
async def ollama_status():
    """Check Ollama service status."""
    try:
        process = await asyncio.create_subprocess_exec(
            "brew", "services", "info", "ollama", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        running = "started" in stdout.decode().lower()

        return ActionResponse(
            success=True,
            action="status",
            target="ollama",
            message="running" if running else "stopped",
            timestamp=datetime.utcnow().isoformat()
        )

    except Exception as e:
        return ActionResponse(
            success=False,
            action="status",
            target="ollama",
            message=str(e),
            timestamp=datetime.utcnow().isoformat()
        )


class OllamaHealthResponse(BaseModel):
    healthy: bool
    status: str
    api_reachable: bool
    models_loaded: int
    version: Optional[str] = None
    timestamp: str


@app.get("/ollama/health", response_model=OllamaHealthResponse)
async def ollama_health():
    """
    Check Ollama health by actually querying the API.
    More reliable than brew services status.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Check if API is reachable
            try:
                version_resp = await client.get(f"{OLLAMA_URL}/api/version")
                api_reachable = version_resp.status_code == 200
                version = version_resp.json().get("version") if api_reachable else None
            except Exception:
                api_reachable = False
                version = None

            # Check loaded models
            models_loaded = 0
            if api_reachable:
                try:
                    ps_resp = await client.get(f"{OLLAMA_URL}/api/ps")
                    if ps_resp.status_code == 200:
                        models_loaded = len(ps_resp.json().get("models", []))
                except Exception:
                    pass

            healthy = api_reachable
            if healthy:
                status = "healthy" if models_loaded > 0 else "idle"
            else:
                status = "offline"

            return OllamaHealthResponse(
                healthy=healthy,
                status=status,
                api_reachable=api_reachable,
                models_loaded=models_loaded,
                version=version,
                timestamp=datetime.utcnow().isoformat()
            )

    except Exception as e:
        logger.error("ollama_health_check_failed", error=str(e))
        return OllamaHealthResponse(
            healthy=False,
            status="error",
            api_reachable=False,
            models_loaded=0,
            version=None,
            timestamp=datetime.utcnow().isoformat()
        )


@app.post("/ollama/start", response_model=ActionResponse)
async def start_ollama():
    """Start Ollama service using brew services."""
    try:
        process = await asyncio.create_subprocess_exec(
            "brew", "services", "start", "ollama",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        success = process.returncode == 0
        message = stdout.decode().strip() if success else stderr.decode().strip()

        # Wait for Ollama to be ready
        if success:
            import httpx
            for _ in range(10):  # Wait up to 10 seconds
                await asyncio.sleep(1)
                try:
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        resp = await client.get(f"{OLLAMA_URL}/api/version")
                        if resp.status_code == 200:
                            message = "Ollama started and ready"
                            break
                except Exception:
                    continue

        logger.info("ollama_start", success=success)

        return ActionResponse(
            success=success,
            action="start",
            target="ollama",
            message=message or "Ollama started",
            timestamp=datetime.utcnow().isoformat()
        )

    except Exception as e:
        logger.error("ollama_start_failed", error=str(e))
        return ActionResponse(
            success=False,
            action="start",
            target="ollama",
            message=str(e),
            timestamp=datetime.utcnow().isoformat()
        )


@app.post("/ollama/stop", response_model=ActionResponse)
async def stop_ollama():
    """Stop Ollama service using brew services."""
    try:
        process = await asyncio.create_subprocess_exec(
            "brew", "services", "stop", "ollama",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        success = process.returncode == 0
        message = stdout.decode().strip() if success else stderr.decode().strip()

        logger.info("ollama_stop", success=success)

        return ActionResponse(
            success=success,
            action="stop",
            target="ollama",
            message=message or "Ollama stopped",
            timestamp=datetime.utcnow().isoformat()
        )

    except Exception as e:
        logger.error("ollama_stop_failed", error=str(e))
        return ActionResponse(
            success=False,
            action="stop",
            target="ollama",
            message=str(e),
            timestamp=datetime.utcnow().isoformat()
        )


# =============================================================================
# PROCESS CONTROL ENDPOINTS
# =============================================================================
# Control Python/uvicorn processes by port number

@app.post("/process/stop/{port}", response_model=ActionResponse)
async def stop_process(port: int):
    """Stop a process listening on a port."""
    if not is_port_allowed(port):
        raise HTTPException(
            status_code=403,
            detail=f"Port {port} is not in the allowed services list"
        )

    success, message = await stop_process_by_port(port)
    service_name = PROCESS_SERVICES.get(port, {}).get("name", f"port-{port}")

    logger.info("process_stop", port=port, service=service_name, success=success)

    return ActionResponse(
        success=success,
        action="stop",
        target=service_name,
        message=message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.post("/process/start/{port}", response_model=ActionResponse)
async def start_process(port: int):
    """Start a service on a port."""
    if not is_port_allowed(port):
        raise HTTPException(
            status_code=403,
            detail=f"Port {port} is not in the allowed services list"
        )

    success, message = await start_process_by_port(port)
    service_name = PROCESS_SERVICES.get(port, {}).get("name", f"port-{port}")

    logger.info("process_start", port=port, service=service_name, success=success)

    return ActionResponse(
        success=success,
        action="start",
        target=service_name,
        message=message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.post("/process/restart/{port}", response_model=ActionResponse)
async def restart_process(port: int):
    """Restart a service on a port (stop then start)."""
    if not is_port_allowed(port):
        raise HTTPException(
            status_code=403,
            detail=f"Port {port} is not in the allowed services list"
        )

    service_name = PROCESS_SERVICES.get(port, {}).get("name", f"port-{port}")

    # Stop first
    stop_success, stop_message = await stop_process_by_port(port)
    if not stop_success and "already stopped" not in stop_message.lower():
        logger.error("process_restart_stop_failed", port=port, message=stop_message)
        return ActionResponse(
            success=False,
            action="restart",
            target=service_name,
            message=f"Failed to stop: {stop_message}",
            timestamp=datetime.utcnow().isoformat()
        )

    # Brief pause
    await asyncio.sleep(1)

    # Start
    start_success, start_message = await start_process_by_port(port)

    logger.info("process_restart", port=port, service=service_name, success=start_success)

    return ActionResponse(
        success=start_success,
        action="restart",
        target=service_name,
        message=start_message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.get("/process/status/{port}", response_model=ProcessStatus)
async def process_status(port: int):
    """Get status of a process on a port."""
    if not is_port_allowed(port):
        raise HTTPException(
            status_code=403,
            detail=f"Port {port} is not in the allowed services list"
        )

    service_name = PROCESS_SERVICES.get(port, {}).get("name", f"port-{port}")
    pid = await get_pid_by_port(port)

    return ProcessStatus(
        port=port,
        service_name=service_name,
        pid=pid,
        running=pid is not None,
        status="running" if pid else "stopped"
    )


@app.get("/process/list", response_model=list[ProcessStatus])
async def list_processes():
    """List all configured process services and their status."""
    results = []

    for port, config in PROCESS_SERVICES.items():
        pid = await get_pid_by_port(port)
        results.append(ProcessStatus(
            port=port,
            service_name=config["name"],
            pid=pid,
            running=pid is not None,
            status="running" if pid else "stopped"
        ))

    return results


# =============================================================================
# HUGGING FACE MODEL MANAGEMENT
# =============================================================================

from typing import List as TypingList

class HFSearchRequest(BaseModel):
    query: str = ""
    model_format: str = "gguf"  # gguf, mlx, all
    quantizations: Optional[TypingList[str]] = None
    tool_support: bool = False
    author: Optional[str] = None
    limit: int = 20


class HFModelResult(BaseModel):
    repo_id: str
    downloads: int
    likes: int
    updated: Optional[str]
    tags: TypingList[str]
    pipeline_tag: Optional[str]
    has_tool_support: bool


class HFFileResult(BaseModel):
    filename: str
    size_bytes: int
    size_gb: float
    quantization: Optional[str]


class HFDownloadRequest(BaseModel):
    repo_id: str
    filename: str
    model_format: str = "gguf"
    hf_token: Optional[str] = None
    callback_url: Optional[str] = None  # URL to call when download completes
    download_id: Optional[int] = None   # Admin backend download ID for callback


class HFDownloadStatus(BaseModel):
    job_id: str
    status: str
    progress_percent: float
    downloaded_bytes: int
    total_bytes: int
    error: Optional[str]


class HFImportRequest(BaseModel):
    gguf_path: str
    model_name: str


class HFDownloadedModel(BaseModel):
    filename: str
    path: str
    format: str
    size_bytes: int
    size_gb: float
    quantization: Optional[str]


@app.post("/huggingface/search", response_model=TypingList[HFModelResult])
async def hf_search(request: HFSearchRequest):
    """Search Hugging Face Hub for models."""
    try:
        from huggingface import search_models

        results = await search_models(
            query=request.query,
            model_format=request.model_format,
            quantizations=request.quantizations,
            tool_support=request.tool_support,
            author=request.author,
            limit=request.limit
        )

        return [
            HFModelResult(
                repo_id=r.repo_id,
                downloads=r.downloads,
                likes=r.likes,
                updated=r.updated,
                tags=r.tags,
                pipeline_tag=r.pipeline_tag,
                has_tool_support=r.has_tool_support
            )
            for r in results
        ]

    except Exception as e:
        logger.error("hf_search_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/huggingface/repo/{repo_id:path}/files", response_model=TypingList[HFFileResult])
async def hf_repo_files(repo_id: str, format_filter: Optional[str] = None):
    """Get files in a Hugging Face repository."""
    try:
        from huggingface import get_repo_files

        files = await get_repo_files(repo_id=repo_id, format_filter=format_filter)

        return [
            HFFileResult(
                filename=f.filename,
                size_bytes=f.size_bytes,
                size_gb=f.size_gb,
                quantization=f.quantization
            )
            for f in files
        ]

    except Exception as e:
        logger.error("hf_files_error", repo_id=repo_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/huggingface/download", response_model=HFDownloadStatus)
async def hf_download(request: HFDownloadRequest):
    """Start downloading a model file from Hugging Face."""
    try:
        from huggingface import start_download, get_download_status

        job_id = await start_download(
            repo_id=request.repo_id,
            filename=request.filename,
            model_format=request.model_format,
            hf_token=request.hf_token,
            callback_url=request.callback_url,
            download_id=request.download_id
        )

        status = get_download_status(job_id)

        return HFDownloadStatus(
            job_id=job_id,
            status=status.status if status else "unknown",
            progress_percent=status.progress_percent if status else 0,
            downloaded_bytes=status.downloaded_bytes if status else 0,
            total_bytes=status.total_bytes if status else 0,
            error=status.error if status else None
        )

    except Exception as e:
        logger.error("hf_download_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/huggingface/download/{job_id}/status", response_model=HFDownloadStatus)
async def hf_download_status(job_id: str):
    """Get status of a download job."""
    from huggingface import get_download_status

    status = get_download_status(job_id)

    if not status:
        raise HTTPException(status_code=404, detail=f"Download job {job_id} not found")

    return HFDownloadStatus(
        job_id=status.job_id,
        status=status.status,
        progress_percent=status.progress_percent,
        downloaded_bytes=status.downloaded_bytes,
        total_bytes=status.total_bytes,
        error=status.error
    )


@app.delete("/huggingface/download/{job_id}")
async def hf_cancel_download(job_id: str):
    """Cancel an active download."""
    from huggingface import cancel_download

    success = cancel_download(job_id)

    if success:
        return {"success": True, "message": f"Download {job_id} cancelled"}
    else:
        raise HTTPException(status_code=404, detail=f"Download job {job_id} not found or already completed")


@app.post("/huggingface/import-to-ollama", response_model=ActionResponse)
async def hf_import_to_ollama(request: HFImportRequest):
    """Import a downloaded GGUF file into Ollama."""
    from huggingface import import_to_ollama

    success, message = await import_to_ollama(
        gguf_path=request.gguf_path,
        model_name=request.model_name
    )

    return ActionResponse(
        success=success,
        action="import",
        target=request.model_name,
        message=message,
        timestamp=datetime.utcnow().isoformat()
    )


@app.get("/huggingface/downloaded", response_model=TypingList[HFDownloadedModel])
async def hf_list_downloaded():
    """List all downloaded model files."""
    from huggingface import list_downloaded_models

    models = list_downloaded_models()

    return [
        HFDownloadedModel(
            filename=m["filename"],
            path=m["path"],
            format=m["format"],
            size_bytes=m["size_bytes"],
            size_gb=m["size_gb"],
            quantization=m["quantization"]
        )
        for m in models
    ]


@app.delete("/huggingface/downloaded")
async def hf_delete_downloaded(file_path: str):
    """Delete a downloaded model file."""
    from huggingface import delete_downloaded_model

    success, message = delete_downloaded_model(file_path)

    if success:
        return {"success": True, "message": message}
    else:
        raise HTTPException(status_code=400, detail=message)


# =============================================================================
# DEBUG LOGS ENDPOINTS
# =============================================================================
# Serve debug logs from Mac Studio filesystem

import re
import json
from datetime import timedelta

# Log directory on Mac Studio
LOG_DIR = Path.home() / "dev" / "project-athena" / "logs" / "debug"


class LogEntry(BaseModel):
    timestamp: Optional[str] = None
    level: Optional[str] = None
    service: Optional[str] = None
    event: Optional[str] = None
    message: str
    raw: str
    line_number: int


class LogFile(BaseModel):
    name: str
    path: str
    size: int
    modified: str
    service: str
    date: str


class LogSearchResult(BaseModel):
    total_lines: int
    returned_lines: int
    entries: TypingList[LogEntry]
    file: str


class DebugStatusResponse(BaseModel):
    debug_mode: bool
    log_directory: str
    directory_exists: bool
    file_count: int
    total_size_mb: float
    recent_files: TypingList[str]


def parse_log_line(line: str, line_number: int) -> LogEntry:
    """Parse a log line into structured entry."""
    try:
        # Try to parse as JSON
        data = json.loads(line)
        return LogEntry(
            timestamp=data.get("timestamp"),
            level=data.get("level"),
            service=data.get("service"),
            event=data.get("event"),
            message=data.get("event") or data.get("message") or line[:100],
            raw=line,
            line_number=line_number
        )
    except json.JSONDecodeError:
        # Plain text log line
        level = None
        if "ERROR" in line.upper():
            level = "error"
        elif "WARN" in line.upper():
            level = "warning"
        elif "INFO" in line.upper():
            level = "info"
        elif "DEBUG" in line.upper():
            level = "debug"

        return LogEntry(
            timestamp=None,
            level=level,
            service=None,
            event=None,
            message=line[:200] if len(line) > 200 else line,
            raw=line,
            line_number=line_number
        )


@app.get("/debug-logs/status", response_model=DebugStatusResponse)
async def debug_logs_status():
    """Check debug mode and log directory status."""
    import os
    debug_mode = os.getenv("ATHENA_DEBUG_MODE", "false").lower() == "true"

    log_files = []
    total_size = 0

    if LOG_DIR.exists():
        for f in LOG_DIR.glob("*.log"):
            total_size += f.stat().st_size
            log_files.append(f.name)

    return DebugStatusResponse(
        debug_mode=debug_mode,
        log_directory=str(LOG_DIR),
        directory_exists=LOG_DIR.exists(),
        file_count=len(log_files),
        total_size_mb=round(total_size / (1024 * 1024), 2),
        recent_files=sorted(log_files, reverse=True)[:10]
    )


@app.get("/debug-logs/files", response_model=TypingList[LogFile])
async def list_log_files(days: int = 7):
    """List available log files."""
    if not LOG_DIR.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    files = []

    for f in LOG_DIR.glob("*.log"):
        try:
            stat = f.stat()
            modified = datetime.fromtimestamp(stat.st_mtime)

            if modified < cutoff:
                continue

            # Parse filename: service_YYYY-MM-DD.log
            name_parts = f.stem.rsplit('_', 1)
            service = name_parts[0] if len(name_parts) > 1 else "unknown"
            date = name_parts[1] if len(name_parts) > 1 else "unknown"

            files.append(LogFile(
                name=f.name,
                path=str(f),
                size=stat.st_size,
                modified=modified.isoformat(),
                service=service,
                date=date
            ))
        except Exception:
            continue

    return sorted(files, key=lambda x: x.modified, reverse=True)


@app.get("/debug-logs/search", response_model=LogSearchResult)
async def search_logs(
    query: Optional[str] = None,
    file: Optional[str] = None,
    service: Optional[str] = None,
    level: Optional[str] = None,
    hours: int = 24,
    limit: int = 500,
    offset: int = 0
):
    """Search log files with optional filters."""
    if not LOG_DIR.exists():
        raise HTTPException(status_code=404, detail="Log directory not found")

    entries = []
    total_lines = 0
    target_file = None

    # Determine which files to search
    if file:
        target_file = LOG_DIR / file
        if not target_file.exists():
            raise HTTPException(status_code=404, detail=f"Log file not found: {file}")
        files_to_search = [target_file]
    else:
        cutoff = datetime.now() - timedelta(hours=hours)
        files_to_search = []
        for f in LOG_DIR.glob("*.log"):
            if service and service not in f.stem:
                continue
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
                    files_to_search.append(f)
            except:
                continue
        files_to_search = sorted(files_to_search, key=lambda x: x.stat().st_mtime, reverse=True)

    # Compile search pattern
    search_pattern = None
    if query:
        try:
            search_pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            search_pattern = re.compile(re.escape(query), re.IGNORECASE)

    # Search files
    for log_file in files_to_search:
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    total_lines += 1

                    # Apply filters
                    if search_pattern and not search_pattern.search(line):
                        continue

                    # Parse JSON log entry
                    entry = parse_log_line(line, line_num)

                    # Filter by level
                    if level and entry.level and entry.level.lower() != level.lower():
                        continue

                    entries.append(entry)

                    if len(entries) >= offset + limit:
                        break
        except Exception:
            continue

        if len(entries) >= offset + limit:
            break

    # Apply pagination
    paginated = entries[offset:offset + limit]

    return LogSearchResult(
        total_lines=total_lines,
        returned_lines=len(paginated),
        entries=paginated,
        file=str(target_file) if target_file else "multiple"
    )


@app.get("/debug-logs/tail/{filename}", response_model=LogSearchResult)
async def tail_log(filename: str, lines: int = 100):
    """Get the last N lines of a log file."""
    log_file = LOG_DIR / filename

    if not log_file.exists():
        raise HTTPException(status_code=404, detail=f"Log file not found: {filename}")

    entries = []

    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()
            total = len(all_lines)

            for i, line in enumerate(all_lines[-lines:], total - lines + 1):
                line = line.strip()
                if line:
                    entries.append(parse_log_line(line, i))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return LogSearchResult(
        total_lines=total,
        returned_lines=len(entries),
        entries=entries,
        file=filename
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8099,
        reload=True
    )
