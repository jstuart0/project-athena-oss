"""
Debug logs API - Proxies requests to Control Agent.

The orchestrator logs are stored on the service host.
This route proxies requests to the Control Agent which has filesystem access.
"""
import os
import httpx
from typing import List, Optional
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/debug-logs", tags=["debug-logs"])

# Control Agent URL - configurable via environment
CONTROL_AGENT_URL = os.getenv("CONTROL_AGENT_URL", "http://localhost:8099")


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


async def proxy_to_control_agent(path: str, params: dict = None) -> dict:
    """Proxy request to Control Agent on Mac Studio."""
    url = f"{CONTROL_AGENT_URL}{path}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
        raise HTTPException(
            status_code=503,
            detail=f"Control Agent not reachable at {CONTROL_AGENT_URL}. Is the Control Agent running?"
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Control Agent request timed out"
        )


@router.get("/status", response_model=DebugStatusResponse)
async def get_debug_status():
    """Check if debug mode is enabled and get log directory info."""
    data = await proxy_to_control_agent("/debug-logs/status")
    return DebugStatusResponse(**data)


@router.get("/files", response_model=List[LogFile])
async def list_log_files(
    days: int = Query(7, ge=1, le=30, description="Number of days to look back")
):
    """List available log files."""
    data = await proxy_to_control_agent("/debug-logs/files", {"days": days})
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
    params = {
        "hours": hours,
        "limit": limit,
        "offset": offset
    }
    if query:
        params["query"] = query
    if file:
        params["file"] = file
    if service:
        params["service"] = service
    if level:
        params["level"] = level

    data = await proxy_to_control_agent("/debug-logs/search", params)
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
    return LogSearchResult(
        total_lines=data["total_lines"],
        returned_lines=data["returned_lines"],
        entries=[LogEntry(**e) for e in data["entries"]],
        file=data["file"]
    )
