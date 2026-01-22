"""
Model Downloads Routes

Search, download, and manage models from Hugging Face Hub.
Uses Control Agent for actual download execution on Mac Studio.
"""

from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import structlog
import httpx

from app.database import get_db
from app.models import ModelDownload, User, ExternalAPIKey, Alert
from app.auth.oidc import get_current_user
from app.utils.encryption import decrypt_value
from app.routes.websocket import broadcast_model_download_event


def create_download_alert(
    db: Session,
    alert_type: str,
    severity: str,
    title: str,
    message: str,
    download_id: int,
    repo_id: str,
    filename: str
):
    """Create an alert for download events."""
    alert = Alert(
        alert_type=alert_type,
        severity=severity,
        title=title,
        message=message,
        entity_id=str(download_id),
        entity_type="model_download",
        alert_data={
            "repo_id": repo_id,
            "filename": filename,
            "download_id": download_id
        },
        dedup_key=f"model_download_{download_id}_{alert_type}"
    )
    db.add(alert)
    db.commit()
    return alert

import os

logger = structlog.get_logger()
router = APIRouter(prefix="/api/model-downloads", tags=["model-downloads"])

# Control Agent URL - configurable via environment
CONTROL_AGENT_URL = os.getenv("CONTROL_AGENT_URL", "http://localhost:8099")


# =============================================================================
# Pydantic Models
# =============================================================================

class HFSearchRequest(BaseModel):
    query: str = ""
    model_format: str = "gguf"  # gguf, mlx, all
    quantizations: Optional[List[str]] = None
    tool_support: bool = False
    author: Optional[str] = None
    limit: int = 20


class HFModelResult(BaseModel):
    repo_id: str
    downloads: int
    likes: int
    updated: Optional[str]
    tags: List[str]
    pipeline_tag: Optional[str]
    has_tool_support: bool


class HFFileResult(BaseModel):
    filename: str
    size_bytes: int
    size_gb: float
    quantization: Optional[str]


class DownloadRequest(BaseModel):
    repo_id: str
    filename: str
    model_format: str = "gguf"
    quantization: Optional[str] = None
    ollama_model_name: Optional[str] = None


class DownloadResponse(BaseModel):
    id: int
    repo_id: str
    filename: str
    model_format: str
    quantization: Optional[str]
    file_size_bytes: Optional[int]
    status: str
    progress_percent: float
    downloaded_bytes: int
    error_message: Optional[str]
    ollama_model_name: Optional[str]
    ollama_imported: bool
    download_path: Optional[str]
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]


class ImportToOllamaRequest(BaseModel):
    model_name: str


# =============================================================================
# Helper Functions
# =============================================================================

async def get_hf_token(db: Session) -> Optional[str]:
    """Get Hugging Face token from external API keys."""
    try:
        key = db.query(ExternalAPIKey).filter(
            ExternalAPIKey.service_name == "huggingface",
            ExternalAPIKey.enabled == True
        ).first()

        if key and key.api_key_encrypted:
            return decrypt_value(key.api_key_encrypted)
        return None
    except Exception as e:
        logger.error("failed_to_get_hf_token", error=str(e))
        return None


async def call_control_agent(
    method: str,
    endpoint: str,
    json_data: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = 30.0
) -> tuple[bool, dict]:
    """Call Control Agent endpoint."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            url = f"{CONTROL_AGENT_URL}{endpoint}"

            if method == "GET":
                response = await client.get(url, params=params)
            elif method == "POST":
                response = await client.post(url, json=json_data)
            elif method == "DELETE":
                response = await client.delete(url, params=params)
            else:
                return False, {"error": f"Unknown method: {method}"}

            if response.status_code >= 400:
                return False, {"error": response.text}

            return True, response.json()

    except httpx.TimeoutException:
        return False, {"error": "Control Agent timeout"}
    except httpx.ConnectError:
        return False, {"error": "Control Agent not reachable"}
    except Exception as e:
        logger.error("control_agent_error", error=str(e))
        return False, {"error": str(e)}


# =============================================================================
# Search Endpoints
# =============================================================================

@router.post("/search", response_model=List[HFModelResult])
async def search_models(
    request: HFSearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search Hugging Face Hub for models."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, result = await call_control_agent(
        "POST",
        "/huggingface/search",
        json_data=request.model_dump(),
        timeout=60.0
    )

    if not success:
        raise HTTPException(status_code=502, detail=result.get("error", "Search failed"))

    return result


@router.get("/repo/{repo_id:path}/files", response_model=List[HFFileResult])
async def get_repo_files(
    repo_id: str,
    format_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get files in a Hugging Face repository."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # URL encode the repo_id for the path
    encoded_repo = repo_id.replace("/", "%2F")

    success, result = await call_control_agent(
        "GET",
        f"/huggingface/repo/{encoded_repo}/files",
        params={"format_filter": format_filter} if format_filter else None,
        timeout=30.0
    )

    if not success:
        raise HTTPException(status_code=502, detail=result.get("error", "Failed to get files"))

    return result


# =============================================================================
# Download Management
# =============================================================================

@router.get("", response_model=List[DownloadResponse])
async def list_downloads(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all model downloads."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(ModelDownload).order_by(ModelDownload.created_at.desc())

    if status_filter:
        query = query.filter(ModelDownload.status == status_filter)

    downloads = query.limit(100).all()

    return [DownloadResponse(**d.to_dict()) for d in downloads]


@router.get("/{download_id}", response_model=DownloadResponse)
async def get_download(
    download_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific download."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    download = db.query(ModelDownload).filter(ModelDownload.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    return DownloadResponse(**download.to_dict())


@router.post("", response_model=DownloadResponse)
async def create_download(
    request: DownloadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Start a new model download."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if already exists
    existing = db.query(ModelDownload).filter(
        ModelDownload.repo_id == request.repo_id,
        ModelDownload.filename == request.filename
    ).first()

    if existing and existing.status in ['downloading', 'completed']:
        raise HTTPException(
            status_code=400,
            detail=f"Download already exists with status: {existing.status}"
        )

    # Get file size from HF
    encoded_repo = request.repo_id.replace("/", "%2F")
    success, files = await call_control_agent(
        "GET",
        f"/huggingface/repo/{encoded_repo}/files",
        params={"format_filter": request.model_format}
    )

    file_size = None
    if success and isinstance(files, list):
        for f in files:
            if f.get("filename") == request.filename:
                file_size = f.get("size_bytes")
                break

    # Create or update record
    if existing:
        download = existing
        download.status = "pending"
        download.progress_percent = 0
        download.downloaded_bytes = 0
        download.error_message = None
    else:
        download = ModelDownload(
            repo_id=request.repo_id,
            filename=request.filename,
            model_format=request.model_format,
            quantization=request.quantization,
            file_size_bytes=file_size,
            ollama_model_name=request.ollama_model_name,
            created_by_id=current_user.id
        )
        db.add(download)

    db.commit()
    db.refresh(download)

    # Start download via Control Agent with callback URL
    hf_token = await get_hf_token(db)

    # Callback URL for Control Agent to notify us of completion
    callback_url = "http://localhost:8080/api/model-downloads"

    success, result = await call_control_agent(
        "POST",
        "/huggingface/download",
        json_data={
            "repo_id": request.repo_id,
            "filename": request.filename,
            "model_format": request.model_format,
            "hf_token": hf_token,
            "callback_url": callback_url,
            "download_id": download.id
        },
        timeout=60.0
    )

    if success:
        download.status = "downloading"
        download.started_at = datetime.utcnow()
        db.commit()

        logger.info(
            "download_started",
            download_id=download.id,
            repo_id=request.repo_id,
            filename=request.filename
        )

        # Broadcast WebSocket event
        await broadcast_model_download_event(
            "model_download_started",
            download.id,
            repo_id=request.repo_id,
            filename=request.filename,
            file_size_bytes=file_size
        )
    else:
        download.status = "failed"
        download.error_message = result.get("error", "Failed to start download")
        db.commit()

        # Broadcast WebSocket event
        await broadcast_model_download_event(
            "model_download_failed",
            download.id,
            error_message=download.error_message
        )

    return DownloadResponse(**download.to_dict())


@router.post("/{download_id}/cancel")
async def cancel_download(
    download_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cancel an active download."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    download = db.query(ModelDownload).filter(ModelDownload.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    if download.status != "downloading":
        raise HTTPException(status_code=400, detail="Download is not active")

    download.status = "cancelled"
    db.commit()

    logger.info("download_cancelled", download_id=download_id)

    # Broadcast WebSocket event
    await broadcast_model_download_event(
        "model_download_cancelled",
        download_id
    )

    return {"success": True, "message": "Download cancelled"}


@router.post("/{download_id}/retry")
async def retry_download(
    download_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Retry a failed download."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    download = db.query(ModelDownload).filter(ModelDownload.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    if download.status not in ["failed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Download cannot be retried")

    # Reset and restart
    download.status = "pending"
    download.progress_percent = 0
    download.downloaded_bytes = 0
    download.error_message = None
    db.commit()

    # Trigger download with callback
    hf_token = await get_hf_token(db)
    callback_url = "http://localhost:8080/api/model-downloads"

    success, result = await call_control_agent(
        "POST",
        "/huggingface/download",
        json_data={
            "repo_id": download.repo_id,
            "filename": download.filename,
            "model_format": download.model_format,
            "hf_token": hf_token,
            "callback_url": callback_url,
            "download_id": download.id
        },
        timeout=60.0
    )

    if success:
        download.status = "downloading"
        download.started_at = datetime.utcnow()
    else:
        download.status = "failed"
        download.error_message = result.get("error", "Failed to start download")

    db.commit()

    return DownloadResponse(**download.to_dict())


@router.delete("/{download_id}")
async def delete_download(
    download_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a download record and optionally the file."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    download = db.query(ModelDownload).filter(ModelDownload.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    # Delete file if it exists
    if download.download_path:
        await call_control_agent(
            "DELETE",
            "/huggingface/downloaded",
            params={"file_path": download.download_path}
        )

    # Delete record
    db.delete(download)
    db.commit()

    logger.info("download_deleted", download_id=download_id)

    return {"success": True, "message": "Download deleted"}


# =============================================================================
# Ollama Import
# =============================================================================

@router.post("/{download_id}/import-ollama", response_model=DownloadResponse)
async def import_to_ollama(
    download_id: int,
    request: ImportToOllamaRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Import a downloaded GGUF file into Ollama."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    download = db.query(ModelDownload).filter(ModelDownload.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    if download.status != "completed":
        raise HTTPException(status_code=400, detail="Download not completed")

    if download.model_format != "gguf":
        raise HTTPException(status_code=400, detail="Only GGUF files can be imported to Ollama")

    if not download.download_path:
        raise HTTPException(status_code=400, detail="Download path not available")

    # Call Control Agent to import
    success, result = await call_control_agent(
        "POST",
        "/huggingface/import-to-ollama",
        json_data={
            "gguf_path": download.download_path,
            "model_name": request.model_name
        },
        timeout=120.0
    )

    if success and result.get("success"):
        download.ollama_model_name = request.model_name
        download.ollama_imported = True
        db.commit()

        logger.info(
            "ollama_import_success",
            download_id=download_id,
            model_name=request.model_name
        )
    else:
        error_msg = result.get("message") or result.get("error", "Import failed")
        raise HTTPException(status_code=500, detail=error_msg)

    return DownloadResponse(**download.to_dict())


# =============================================================================
# Downloaded Models (from Control Agent)
# =============================================================================

@router.get("/downloaded/files")
async def list_downloaded_files(
    current_user: User = Depends(get_current_user)
):
    """List all downloaded model files on Mac Studio."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, result = await call_control_agent(
        "GET",
        "/huggingface/downloaded"
    )

    if not success:
        raise HTTPException(status_code=502, detail=result.get("error", "Failed to list files"))

    return result


# =============================================================================
# Progress Updates (called by Control Agent)
# =============================================================================

class ProgressUpdateRequest(BaseModel):
    status: str
    progress_percent: float = 0
    downloaded_bytes: int = 0
    error_message: Optional[str] = None
    download_path: Optional[str] = None
    ollama_model_name: Optional[str] = None
    ollama_imported: bool = False


@router.post("/internal/{download_id}/progress")
async def update_download_progress(
    download_id: int,
    request: ProgressUpdateRequest,
    db: Session = Depends(get_db)
):
    """
    Update download progress (called by Control Agent).

    This is an internal endpoint - no user auth required since it comes from trusted Control Agent.
    """
    download = db.query(ModelDownload).filter(ModelDownload.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    # Update progress
    download.status = request.status
    download.progress_percent = request.progress_percent
    download.downloaded_bytes = request.downloaded_bytes

    if request.error_message:
        download.error_message = request.error_message

    if request.download_path:
        download.download_path = request.download_path

    # Handle auto-import to Ollama
    if request.ollama_model_name:
        download.ollama_model_name = request.ollama_model_name
    if request.ollama_imported:
        download.ollama_imported = True

    if request.status == "completed":
        download.completed_at = datetime.utcnow()

    db.commit()

    logger.info(
        "download_progress_updated",
        download_id=download_id,
        status=request.status,
        progress=request.progress_percent
    )

    # Broadcast WebSocket event based on status
    if request.status == "downloading":
        await broadcast_model_download_event(
            "model_download_progress",
            download_id,
            progress_percent=request.progress_percent,
            downloaded_bytes=request.downloaded_bytes,
            total_bytes=download.file_size_bytes
        )
    elif request.status == "completed":
        await broadcast_model_download_event(
            "model_download_completed",
            download_id,
            download_path=request.download_path
        )
        # Create success alert
        create_download_alert(
            db,
            alert_type="model_download_completed",
            severity="info",
            title="Model Download Complete",
            message=f"Successfully downloaded {download.filename} from {download.repo_id}",
            download_id=download_id,
            repo_id=download.repo_id,
            filename=download.filename
        )
    elif request.status == "failed":
        await broadcast_model_download_event(
            "model_download_failed",
            download_id,
            error_message=request.error_message
        )
        # Create error alert
        create_download_alert(
            db,
            alert_type="model_download_failed",
            severity="error",
            title="Model Download Failed",
            message=f"Failed to download {download.filename}: {request.error_message}",
            download_id=download_id,
            repo_id=download.repo_id,
            filename=download.filename
        )

    return {"success": True}
