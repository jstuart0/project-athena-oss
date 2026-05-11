"""
Debug logs API - Proxies requests to Control Agent.

The orchestrator logs are stored on the service host.
This route proxies requests to the Control Agent which has filesystem access.

If CONTROL_AGENT_URL is not set or the agent is unreachable, all endpoints
degrade gracefully (200 with empty/unavailable responses) rather than 503.
"""
import os
import httpx
from typing import List, Optional
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from shared.config import get_config

router = APIRouter(prefix="/api/debug-logs", tags=["debug-logs"])

# Control Agent URL - configurable via environment variable.
# Set CONTROL_AGENT_URL to enable this feature. If unset, all endpoints
# return a graceful "unavailable" response.
CONTROL_AGENT_URL = os.getenv("CONTROL_AGENT_URL", "")


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
    entries: List[LogEntry]
    file: str


class DebugStatusResponse(BaseModel):
    debug_mode: bool
    log_directory: str
    directory_exists: bool
    file_count: int
    total_size_mb: float
    recent_files: List[str]
    available: bool = True
    unavailable_reason: Optional[str] = None


def _unavailable_status(reason: str) -> DebugStatusResponse:
    return DebugStatusResponse(
        debug_mode=False,
        log_directory="",
        directory_exists=False,
        file_count=0,
        total_size_mb=0.0,
        recent_files=[],
        available=False,
        unavailable_reason=reason,
    )


async def proxy_to_control_agent(path: str, params: dict = None) -> Optional[dict]:
    """
    Proxy request to the Control Agent.

    Returns the parsed JSON response on success.
    Returns None if the Control Agent is disabled, not configured, or unreachable.
    Raises HTTPException for application-level errors (404, 5xx from agent).
    """
    if not get_config().control_agent_enabled or not CONTROL_AGENT_URL:
        return None

    url = f"{CONTROL_AGENT_URL}{path}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)

            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=response.json().get("detail", "Not found"))
            elif response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Control Agent error: {response.text}"
                )

            return response.json()
    except httpx.ConnectError:
        return None
    except httpx.TimeoutException:
        return None


@router.get("/status", response_model=DebugStatusResponse)
async def get_debug_status():
    """Check if debug mode is enabled and get log directory info."""
    if not get_config().control_agent_enabled:
        return _unavailable_status("Control Agent is disabled (CONTROL_AGENT_ENABLED=false)")
    if not CONTROL_AGENT_URL:
        return _unavailable_status("CONTROL_AGENT_URL is not configured")

    data = await proxy_to_control_agent("/debug-logs/status")
    if data is None:
        return _unavailable_status(f"Control Agent not reachable at {CONTROL_AGENT_URL}")

    return DebugStatusResponse(**data)


@router.get("/files", response_model=List[LogFile])
async def list_log_files(
    days: int = Query(7, ge=1, le=30, description="Number of days to look back")
):
    """List available log files."""
    data = await proxy_to_control_agent("/debug-logs/files", {"days": days})
    if data is None:
        return []
    return [LogFile(**f) for f in data]


@router.get("/search", response_model=LogSearchResult)
async def search_logs(
    query: Optional[str] = Query(None, description="Search query (regex supported)"),
    file: Optional[str] = Query(None, description="Specific log file to search"),
    service: Optional[str] = Query(None, description="Filter by service name"),
    level: Optional[str] = Query(None, description="Filter by log level (info, warning, error)"),
    hours: int = Query(24, ge=1, le=168, description="Hours to look back"),
    limit: int = Query(500, ge=1, le=5000, description="Max lines to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    """Search log files with optional filters."""
    data = await proxy_to_control_agent("/debug-logs/search", {
        "hours": hours,
        "limit": limit,
        "offset": offset,
        **({"query": query} if query else {}),
        **({"file": file} if file else {}),
        **({"service": service} if service else {}),
        **({"level": level} if level else {}),
    })
    if data is None:
        return LogSearchResult(total_lines=0, returned_lines=0, entries=[], file="")

    return LogSearchResult(
        total_lines=data["total_lines"],
        returned_lines=data["returned_lines"],
        entries=[LogEntry(**e) for e in data["entries"]],
        file=data["file"]
    )


@router.get("/tail/{filename}", response_model=LogSearchResult)
async def tail_log(
    filename: str,
    lines: int = Query(100, ge=1, le=1000)
):
    """Get the last N lines of a log file."""
    data = await proxy_to_control_agent(f"/debug-logs/tail/{filename}", {"lines": lines})
    if data is None:
        return LogSearchResult(total_lines=0, returned_lines=0, entries=[], file=filename)

    return LogSearchResult(
        total_lines=data["total_lines"],
        returned_lines=data["returned_lines"],
        entries=[LogEntry(**e) for e in data["entries"]],
        file=data["file"]
    )
