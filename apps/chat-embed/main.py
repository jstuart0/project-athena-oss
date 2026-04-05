"""
Athena Chat Embed API

A lightweight proxy that lets any website embed a chatbot backed by an
Athena instance. Drop this in front of your jarvis-web deployment and point
your site's chat widget at it.

Soul and persona are NOT hardcoded here. They are fetched from the Athena
Admin backend's assistant-profile endpoint at startup and kept in memory.
Change them in the admin UI; restart this service to pick up the update.

Required environment variables:
  ATHENA_CHAT_URL   - URL of the jarvis-web /api/chat endpoint
  ATHENA_ADMIN_URL  - URL of the Athena admin backend (for assistant profile)

Optional:
  CORS_ORIGINS        - Comma-separated allowed origins, default "*"
  RATE_LIMIT_RPM      - Requests per minute per IP, default 30
  SOURCE_TAG          - Analytics source label, default "chatbot"
  STREAM_URL          - jarvis-web /api/chat/stream endpoint (enables /api/chat/stream)
"""

import os
import uuid
import time
import json
import logging
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chat-embed")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ATHENA_CHAT_URL = os.environ["ATHENA_CHAT_URL"]
ATHENA_ADMIN_URL = os.getenv("ATHENA_ADMIN_URL", "")
STREAM_URL = os.getenv("STREAM_URL", "")
SOURCE_TAG = os.getenv("SOURCE_TAG", "chatbot")
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "30"))

_cors_env = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",")] if _cors_env != "*" else ["*"]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Athena Chat Embed", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Rate limiting (in-memory, per IP, sliding window)
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate limit exceeded."""
    now = time.monotonic()
    window = 60.0
    bucket = _rate_buckets[ip]
    # Prune old entries
    _rate_buckets[ip] = [t for t in bucket if now - t < window]
    if len(_rate_buckets[ip]) >= RATE_LIMIT_RPM:
        return False
    _rate_buckets[ip].append(now)
    return True


# ---------------------------------------------------------------------------
# Assistant profile (fetched from admin at startup)
# ---------------------------------------------------------------------------

_assistant_profile: dict = {}


async def _fetch_assistant_profile() -> dict:
    if not ATHENA_ADMIN_URL:
        return {}
    url = f"{ATHENA_ADMIN_URL.rstrip('/')}/api/settings/assistant-profile/public"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            profile = resp.json()
            logger.info(
                "assistant_profile_loaded assistant=%s",
                profile.get("assistant_name", "unknown"),
            )
            return profile
    except Exception as e:
        logger.warning("assistant_profile_fetch_failed error=%s", e)
        return {}


@app.on_event("startup")
async def startup():
    global _assistant_profile
    _assistant_profile = await _fetch_assistant_profile()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please slow down.")

    session_id = req.session_id or str(uuid.uuid4())

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                ATHENA_CHAT_URL,
                json={
                    "message": req.message,
                    "session_id": session_id,
                    "interface_type": "chat",
                    "source": SOURCE_TAG,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Model response timed out")
    except httpx.HTTPError as e:
        logger.error("upstream_error error=%s", e)
        raise HTTPException(status_code=502, detail="AI backend unavailable")

    elapsed = time.time() - start
    response_text = data.get("response", "")
    upstream_session_id = data.get("session_id", session_id)

    if not response_text:
        raise HTTPException(status_code=502, detail="Empty response from model")

    logger.info(
        "chat_ok elapsed=%.1fs session=%s source=%s",
        elapsed, upstream_session_id[:8], SOURCE_TAG,
    )
    return ChatResponse(response=response_text, session_id=upstream_session_id)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    if not STREAM_URL:
        raise HTTPException(status_code=501, detail="Streaming not configured (STREAM_URL not set)")

    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    session_id = req.session_id or str(uuid.uuid4())

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    STREAM_URL,
                    json={
                        "query": req.message,
                        "mode": "owner",
                        "session_id": session_id,
                        "interface_type": "chat",
                        "source": SOURCE_TAG,
                    },
                ) as resp:
                    buffer = ""
                    async for raw in resp.aiter_text():
                        buffer += raw
                        while "\n\n" in buffer:
                            line, buffer = buffer.split("\n\n", 1)
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if not payload:
                                continue
                            try:
                                obj = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            stage = obj.get("stage")
                            if stage == "answer_chunk":
                                token = obj.get("content", "")
                                if token:
                                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                            elif stage == "complete":
                                yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
                            elif stage == "error":
                                yield f"data: {json.dumps({'type': 'error'})}\n\n"
        except Exception as e:
            logger.error("stream_error error=%s", e)
            yield f"data: {json.dumps({'type': 'error'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/profile")
async def profile():
    """Return the current assistant profile (name, identity). Safe to expose publicly."""
    return {
        "assistant_name": _assistant_profile.get("assistant_name", "Jarvis"),
        "identity": _assistant_profile.get("identity", ""),
        "source_tag": SOURCE_TAG,
    }


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(ATHENA_CHAT_URL.replace("/api/chat", "/api/health"))
        upstream = "ok"
    except Exception:
        upstream = "unreachable"
    return {"status": "ok", "upstream": upstream, "profile_loaded": bool(_assistant_profile)}
