"""
Hugging Face Hub Integration for Control Agent

Provides model search, download, and Ollama import capabilities.
Used by the admin backend to enable model management from the UI.
"""

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass
from datetime import datetime

import structlog

logger = structlog.get_logger()

# Download directory on Mac Studio
MODELS_DIR = Path.home() / "dev" / "project-athena" / "models" / "downloads"


@dataclass
class ModelInfo:
    """Information about a model from Hugging Face."""
    repo_id: str
    downloads: int
    likes: int
    updated: Optional[str]
    tags: List[str]
    pipeline_tag: Optional[str]
    has_tool_support: bool


@dataclass
class FileInfo:
    """Information about a file in a HF repo."""
    filename: str
    size_bytes: int
    size_gb: float
    quantization: Optional[str]


@dataclass
class DownloadProgress:
    """Download progress information."""
    job_id: str
    status: str  # pending, downloading, completed, failed, cancelled
    progress_percent: float
    downloaded_bytes: int
    total_bytes: int
    error: Optional[str]


# Active download tracking
_active_downloads: Dict[str, DownloadProgress] = {}
_download_tasks: Dict[str, asyncio.Task] = {}


def extract_quantization(filename: str) -> Optional[str]:
    """Extract quantization type from filename."""
    quant_types = [
        "Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q5_0", "Q5_1",
        "Q4_K_M", "Q4_K_S", "Q4_0", "Q4_1",
        "Q3_K_M", "Q3_K_S", "Q3_K_L",
        "Q2_K", "IQ4_NL", "IQ4_XS", "IQ3_S", "IQ3_XXS",
        "IQ2_XXS", "IQ2_S", "IQ2_XS", "IQ1_S", "IQ1_M",
        "F16", "F32", "BF16",
        "4bit", "8bit"
    ]
    filename_upper = filename.upper()
    for qt in quant_types:
        if qt.upper() in filename_upper:
            return qt
    return None


def detect_tool_support(model_info: Any) -> bool:
    """Detect if model supports function/tool calling."""
    # Check tags
    tool_tags = {"function-calling", "tool-use", "tools", "function_calling"}
    if hasattr(model_info, 'tags') and model_info.tags:
        if any(tag.lower() in tool_tags for tag in model_info.tags):
            return True

    # Check model name patterns
    tool_patterns = ["hermes", "functionary", "gorilla", "firefunction", "toolllama"]
    model_id = model_info.id if hasattr(model_info, 'id') else str(model_info)
    if any(pattern in model_id.lower() for pattern in tool_patterns):
        return True

    return False


async def search_models(
    query: str = "",
    model_format: str = "gguf",  # "gguf", "mlx", "all"
    quantizations: Optional[List[str]] = None,
    tool_support: bool = False,
    author: Optional[str] = None,
    limit: int = 20
) -> List[ModelInfo]:
    """
    Search Hugging Face Hub for models.

    Args:
        query: Search term
        model_format: Filter by format (gguf, mlx, all)
        quantizations: Filter by quantization types
        tool_support: Only show models with tool/function calling
        author: Filter by author/organization
        limit: Maximum results

    Returns:
        List of ModelInfo objects
    """
    try:
        from huggingface_hub import HfApi

        api = HfApi()

        # Build search query with quantization
        base_query = query
        if quantizations and len(quantizations) == 1:
            base_query = f"{query} {quantizations[0]}" if query else quantizations[0]

        # For "all" format, search both gguf and mlx and merge results
        if model_format == "all":
            gguf_query = f"{base_query} gguf".strip() if base_query else "gguf"
            mlx_query = f"{base_query} mlx".strip() if base_query else "mlx"

            # Search both formats
            gguf_models = list(api.list_models(
                search=gguf_query,
                author=author,
                sort="downloads",
                direction=-1,
                limit=limit * 2,
                full=True
            ))

            mlx_models = list(api.list_models(
                search=mlx_query,
                author=author,
                sort="downloads",
                direction=-1,
                limit=limit * 2,
                full=True
            ))

            # Merge and dedupe by model id
            seen_ids = set()
            models = []
            for m in gguf_models + mlx_models:
                if m.id not in seen_ids:
                    seen_ids.add(m.id)
                    models.append(m)

            # Sort by downloads
            models.sort(key=lambda x: x.downloads or 0, reverse=True)
        else:
            # Single format search
            if model_format == "gguf":
                search_query = f"{base_query} gguf".strip() if base_query else "gguf"
            elif model_format == "mlx":
                search_query = f"{base_query} mlx".strip() if base_query else "mlx"
            else:
                search_query = base_query if base_query else "gguf"

            # Search HF Hub
            models = list(api.list_models(
                search=search_query,
                author=author,
                sort="downloads",
                direction=-1,
                limit=limit * 3,
                full=True
            ))

        results = []
        for model in models:
            # Check tool support if required
            has_tools = detect_tool_support(model)
            if tool_support and not has_tools:
                continue

            # Get updated date (convert datetime to ISO string)
            updated = None
            if hasattr(model, 'lastModified') and model.lastModified:
                if hasattr(model.lastModified, 'isoformat'):
                    updated = model.lastModified.isoformat()
                else:
                    updated = str(model.lastModified)

            results.append(ModelInfo(
                repo_id=model.id,
                downloads=model.downloads or 0,
                likes=model.likes or 0,
                updated=updated,
                tags=list(model.tags) if model.tags else [],
                pipeline_tag=model.pipeline_tag,
                has_tool_support=has_tools
            ))

            if len(results) >= limit:
                break

        logger.info(
            "hf_search_completed",
            query=query,
            format=model_format,
            results=len(results)
        )

        return results

    except ImportError:
        logger.error("huggingface_hub_not_installed")
        raise RuntimeError("huggingface_hub package not installed. Run: pip install huggingface_hub")
    except Exception as e:
        logger.error("hf_search_failed", error=str(e))
        raise


async def get_repo_files(
    repo_id: str,
    format_filter: Optional[str] = None  # "gguf", "mlx"
) -> List[FileInfo]:
    """
    Get list of files in a Hugging Face repo with sizes.

    Args:
        repo_id: HF repository ID (e.g., "TheBloke/Llama-2-7B-GGUF")
        format_filter: Filter by extension

    Returns:
        List of FileInfo objects
    """
    try:
        from huggingface_hub import HfApi

        api = HfApi()
        repo_info = api.repo_info(repo_id=repo_id, files_metadata=True)

        # Determine file extensions to include
        if format_filter == "gguf":
            extensions = [".gguf"]
        elif format_filter == "mlx":
            extensions = [".safetensors", ".bin"]
        else:
            extensions = [".gguf", ".safetensors", ".bin"]

        files = []
        for sibling in repo_info.siblings:
            # Check extension
            if not any(sibling.rfilename.lower().endswith(ext) for ext in extensions):
                continue

            size_bytes = sibling.size or 0
            size_gb = size_bytes / (1024 ** 3)

            files.append(FileInfo(
                filename=sibling.rfilename,
                size_bytes=size_bytes,
                size_gb=round(size_gb, 2),
                quantization=extract_quantization(sibling.rfilename)
            ))

        # Sort by size
        files.sort(key=lambda f: f.size_bytes)

        logger.info(
            "hf_repo_files",
            repo_id=repo_id,
            file_count=len(files)
        )

        return files

    except ImportError:
        raise RuntimeError("huggingface_hub package not installed")
    except Exception as e:
        logger.error("hf_get_files_failed", repo_id=repo_id, error=str(e))
        raise


async def download_file(
    repo_id: str,
    filename: str,
    model_format: str = "gguf",
    hf_token: Optional[str] = None,
    progress_callback: Optional[Callable[[float, int, int], None]] = None
) -> str:
    """
    Download a file from Hugging Face Hub.

    Args:
        repo_id: HF repository ID
        filename: File to download
        model_format: Format type for directory organization
        hf_token: Optional HF token for gated models
        progress_callback: Callback(progress_percent, downloaded_bytes, total_bytes)

    Returns:
        Path to downloaded file
    """
    try:
        from huggingface_hub import hf_hub_download

        # Create download directory
        target_dir = MODELS_DIR / model_format
        target_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "hf_download_starting",
            repo_id=repo_id,
            filename=filename,
            target_dir=str(target_dir)
        )

        # Download file
        file_path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            cache_dir=str(target_dir),
            token=hf_token,
            resume_download=True
        )

        logger.info(
            "hf_download_completed",
            repo_id=repo_id,
            filename=filename,
            path=file_path
        )

        return file_path

    except ImportError:
        raise RuntimeError("huggingface_hub package not installed")
    except Exception as e:
        logger.error(
            "hf_download_failed",
            repo_id=repo_id,
            filename=filename,
            error=str(e)
        )
        raise


def generate_job_id() -> str:
    """Generate unique job ID for download tracking."""
    import uuid
    return str(uuid.uuid4())[:8]


def generate_ollama_model_name(filename: str) -> str:
    """
    Generate a clean Ollama model name from a GGUF filename.

    Examples:
        mistral-7b-instruct-v0.2.Q4_K_M.gguf -> mistral-7b-instruct-v0.2-q4_k_m
        Phi-3-mini-4k-instruct-q4.gguf -> phi-3-mini-4k-instruct-q4
    """
    # Remove .gguf extension
    name = filename.lower()
    if name.endswith('.gguf'):
        name = name[:-5]

    # Replace invalid characters with hyphens
    name = name.replace('_', '-').replace('.', '-').replace(' ', '-')

    # Remove consecutive hyphens
    while '--' in name:
        name = name.replace('--', '-')

    # Remove leading/trailing hyphens
    name = name.strip('-')

    # Ensure it starts with a letter (Ollama requirement)
    if name and not name[0].isalpha():
        name = 'model-' + name

    return name


async def start_download(
    repo_id: str,
    filename: str,
    model_format: str = "gguf",
    hf_token: Optional[str] = None,
    callback_url: Optional[str] = None,
    download_id: Optional[int] = None
) -> str:
    """
    Start async download and return job ID for tracking.

    Args:
        repo_id: HF repository ID
        filename: File to download
        model_format: Format type
        hf_token: Optional HF token
        callback_url: URL to call when download completes (admin backend)
        download_id: Admin backend download ID for callback

    Returns:
        Job ID for tracking progress
    """
    job_id = generate_job_id()

    # Get file size first
    files = await get_repo_files(repo_id, model_format)
    file_info = next((f for f in files if f.filename == filename), None)
    total_bytes = file_info.size_bytes if file_info else 0

    # Initialize progress
    _active_downloads[job_id] = DownloadProgress(
        job_id=job_id,
        status="downloading",
        progress_percent=0,
        downloaded_bytes=0,
        total_bytes=total_bytes,
        error=None
    )

    async def download_task():
        ollama_model_name = None
        ollama_imported = False

        try:
            file_path = await download_file(
                repo_id=repo_id,
                filename=filename,
                model_format=model_format,
                hf_token=hf_token
            )

            _active_downloads[job_id].status = "completed"
            _active_downloads[job_id].progress_percent = 100
            _active_downloads[job_id].downloaded_bytes = total_bytes

            # Auto-import GGUF files to Ollama
            if model_format == "gguf" and filename.lower().endswith(".gguf"):
                # Generate model name from filename
                ollama_model_name = generate_ollama_model_name(filename)
                logger.info(
                    "auto_importing_to_ollama",
                    filename=filename,
                    model_name=ollama_model_name
                )
                success, message = await import_to_ollama(file_path, ollama_model_name)
                if success:
                    logger.info("auto_import_success", model_name=ollama_model_name)
                    ollama_imported = True
                else:
                    logger.warning("auto_import_failed", model_name=ollama_model_name, error=message)

            # Callback to admin backend with completion status
            if callback_url and download_id:
                await send_progress_callback(
                    callback_url,
                    download_id,
                    status="completed",
                    progress_percent=100,
                    downloaded_bytes=total_bytes,
                    download_path=file_path,
                    ollama_model_name=ollama_model_name,
                    ollama_imported=ollama_imported
                )

            return file_path

        except Exception as e:
            _active_downloads[job_id].status = "failed"
            _active_downloads[job_id].error = str(e)

            # Callback to admin backend with failure
            if callback_url and download_id:
                await send_progress_callback(
                    callback_url,
                    download_id,
                    status="failed",
                    error_message=str(e)
                )

            raise

    task = asyncio.create_task(download_task())
    _download_tasks[job_id] = task

    return job_id


async def send_progress_callback(
    callback_url: str,
    download_id: int,
    status: str,
    progress_percent: float = 0,
    downloaded_bytes: int = 0,
    error_message: Optional[str] = None,
    download_path: Optional[str] = None,
    ollama_model_name: Optional[str] = None,
    ollama_imported: bool = False
):
    """Send progress callback to admin backend."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{callback_url}/internal/{download_id}/progress",
                json={
                    "status": status,
                    "progress_percent": progress_percent,
                    "downloaded_bytes": downloaded_bytes,
                    "error_message": error_message,
                    "download_path": download_path,
                    "ollama_model_name": ollama_model_name,
                    "ollama_imported": ollama_imported
                }
            )
            if response.status_code != 200:
                logger.warning(
                    "callback_failed",
                    download_id=download_id,
                    status_code=response.status_code
                )
    except Exception as e:
        logger.error("callback_error", download_id=download_id, error=str(e))


def get_download_status(job_id: str) -> Optional[DownloadProgress]:
    """Get status of a download job."""
    return _active_downloads.get(job_id)


def cancel_download(job_id: str) -> bool:
    """Cancel an active download."""
    if job_id in _download_tasks:
        task = _download_tasks[job_id]
        task.cancel()
        _active_downloads[job_id].status = "cancelled"
        return True
    return False


async def import_to_ollama(
    gguf_path: str,
    model_name: str
) -> tuple[bool, str]:
    """
    Import a GGUF file into Ollama.

    Creates a Modelfile and runs `ollama create` to register the model.

    Args:
        gguf_path: Path to the GGUF file
        model_name: Name to use in Ollama

    Returns:
        (success, message)
    """
    try:
        # Verify file exists
        if not os.path.exists(gguf_path):
            return False, f"GGUF file not found: {gguf_path}"

        # Create Modelfile
        modelfile_content = f"FROM {gguf_path}\n"
        modelfile_path = MODELS_DIR / "gguf" / f"Modelfile.{model_name}"

        with open(modelfile_path, "w") as f:
            f.write(modelfile_content)

        logger.info(
            "creating_ollama_model",
            model_name=model_name,
            gguf_path=gguf_path,
            modelfile=str(modelfile_path)
        )

        # Run ollama create
        process = await asyncio.create_subprocess_exec(
            "ollama", "create", model_name, "-f", str(modelfile_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            logger.info("ollama_model_created", model_name=model_name)
            return True, f"Model '{model_name}' created successfully"
        else:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            logger.error("ollama_create_failed", model_name=model_name, error=error_msg)
            return False, f"Failed to create model: {error_msg}"

    except Exception as e:
        logger.error("import_to_ollama_failed", error=str(e))
        return False, f"Import failed: {str(e)}"


def list_downloaded_models() -> List[Dict[str, Any]]:
    """List all downloaded model files."""
    models = []

    for format_dir in ["gguf", "mlx"]:
        dir_path = MODELS_DIR / format_dir
        if not dir_path.exists():
            continue

        # Walk through HF cache structure
        for root, dirs, files in os.walk(dir_path):
            for f in files:
                if f.endswith((".gguf", ".safetensors", ".bin")):
                    file_path = Path(root) / f
                    size_bytes = file_path.stat().st_size
                    models.append({
                        "filename": f,
                        "path": str(file_path),
                        "format": format_dir,
                        "size_bytes": size_bytes,
                        "size_gb": round(size_bytes / (1024 ** 3), 2),
                        "quantization": extract_quantization(f)
                    })

    return models


def delete_downloaded_model(file_path: str) -> tuple[bool, str]:
    """Delete a downloaded model file."""
    try:
        path = Path(file_path)
        if not path.exists():
            return False, "File not found"

        # Security check: must be in models directory
        if not str(path.resolve()).startswith(str(MODELS_DIR.resolve())):
            return False, "File outside allowed directory"

        path.unlink()
        logger.info("model_deleted", path=file_path)
        return True, "File deleted"

    except Exception as e:
        logger.error("delete_failed", path=file_path, error=str(e))
        return False, str(e)
