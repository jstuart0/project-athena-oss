"""
Athena Chat Embed API

A lightweight proxy that lets any website embed a chatbot backed by an
Athena instance. Drop this in front of your jarvis-web deployment and point
your site's chat widget at it.

Soul and persona are NOT hardcoded here. They are fetched from the Athena
Admin backend's assistant-profile endpoint at startup and kept in memory.
Change them in the admin UI; restart this service to pick up the update.

Every relayed message is an anonymous website visitor: jarvis-web serves
it as the narrow public audience (no household data, no controls), never as
the household. chat-embed authenticates itself to jarvis-web with a shared
relay key and names the visitor it resolved, so jarvis-web can rate-limit
each visitor; it never forwards the browser's own headers or credentials.

Required environment variables:
  ATHENA_CHAT_URL   - URL of the jarvis-web /api/chat endpoint
  JARVIS_RELAY_KEY  - the same value as jarvis-web's JARVIS_RELAY_KEY
                      (without it every relayed message is refused)
  ATHENA_ADMIN_URL  - URL of the Athena admin backend (for assistant profile)

Optional:
  CORS_ORIGINS          - Comma-separated origins allowed to call this API
                          from a browser. Default empty: no browser can
                          call it until you list your site. "*" and "null"
                          are refused.
  RATE_LIMIT_RPM        - Requests per minute per visitor, default 20
  TRUSTED_PROXY_CIDRS   - Proxies in front of chat-embed whose
                          X-Forwarded-For is trusted to name the visitor
  TRUST_CF_CONNECTING_IP - "true" behind a Cloudflare tunnel whose whole
                          chain is trusted
  SOURCE_TAG            - Analytics source label, default "chatbot"
  STREAM_URL            - jarvis-web /api/chat/stream endpoint (enables /api/chat/stream)
"""

import hashlib
import os
import time
import json
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import client_throttle as throttle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chat-embed")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ATHENA_CHAT_URL = os.environ["ATHENA_CHAT_URL"]
ATHENA_ADMIN_URL = os.getenv("ATHENA_ADMIN_URL", "")
STREAM_URL = os.getenv("STREAM_URL", "")
SOURCE_TAG = os.getenv("SOURCE_TAG", "chatbot")
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))
JARVIS_RELAY_KEY = os.getenv("JARVIS_RELAY_KEY", "")
TRUSTED_PROXIES = throttle.parse_networks(os.getenv("TRUSTED_PROXY_CIDRS", ""))
TRUST_CF = os.getenv("TRUST_CF_CONNECTING_IP", "").strip().lower() in {"1", "true", "yes", "on"}

if not JARVIS_RELAY_KEY:
    logger.error("jarvis_relay_key_unset: set JARVIS_RELAY_KEY to jarvis-web's value; every message will be refused")

_IN_CLUSTER_SUFFIXES = (".svc", ".cluster.local")


def _plaintext_off_cluster(url: str) -> bool:
    """True for a plain-http URL whose host could be across a network:
    anything but loopback, a Kubernetes Service name (dotless, *.svc or
    *.cluster.local) or a dotless container name. The relay key rides on
    every request, so it must not cross a network in cleartext."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return True
    if parsed.scheme == "https":
        return False
    host = (parsed.host or "").lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(_IN_CLUSTER_SUFFIXES) or "." not in host.replace(":", "."):
        return False
    addr = throttle.parse_ip(host)
    return not (addr is not None and addr.is_loopback)


for _name, _url in (("ATHENA_CHAT_URL", ATHENA_CHAT_URL), ("STREAM_URL", STREAM_URL)):
    if _url and _plaintext_off_cluster(_url):
        logger.error(
            "upstream_plaintext_off_cluster setting=%s: the relay key would cross the network in cleartext; "
            "use https, or an in-cluster (*.svc / *.cluster.local) or loopback address",
            _name,
        )


def _cors_origins(raw: str) -> list:
    origins = []
    for origin in (o.strip() for o in raw.split(",")):
        if not origin:
            continue
        if origin in {"*", "null"}:
            logger.error("cors_origin_refused origin=%s (list your site's exact origin instead)", origin)
            continue
        origins.append(origin.rstrip("/"))
    return origins


CORS_ORIGINS = _cors_origins(os.getenv("CORS_ORIGINS", ""))
if not CORS_ORIGINS:
    logger.warning("embed disabled for browsers until CORS_ORIGINS is set")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Athena Chat Embed", version="1.0.0")

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

# ---------------------------------------------------------------------------
# The visitor, and its rate limit (in-memory, per replica)
# ---------------------------------------------------------------------------

_limiter = throttle.SlidingWindowLimiter(per_minute=RATE_LIMIT_RPM)


def _visitor(request: Request) -> str:
    """The visitor's address, resolved through trusted proxies only. No
    usable address -> 503 here, never a shared value upstream."""
    resolved = throttle.resolve_rate_client(
        request.client.host if request.client else None,
        throttle.read_forwarded_for(request.headers),
        request.headers.get("cf-connecting-ip"),
        TRUSTED_PROXIES,
        TRUST_CF,
    )
    if throttle.parse_ip(resolved.ip) is None:
        logger.error("visitor_unresolvable source=%s", resolved.source)
        raise HTTPException(status_code=503, detail="Chat is unavailable right now.")
    return resolved.ip


async def _admit(request: Request) -> str:
    visitor = _visitor(request)
    if not await _limiter.allow(throttle.rate_limit_key(visitor)):
        raise HTTPException(
            status_code=429,
            detail="You're going a bit fast. Please wait a minute, then try again.",
            headers={"Retry-After": "60"},
        )
    return visitor


def _relay_headers(visitor: str) -> dict:
    """What jarvis-web needs to accept the relay. Nothing from the
    browser's request (cookies, Authorization) is ever forwarded."""
    return {"X-Jarvis-Relay-Key": JARVIS_RELAY_KEY, "X-Jarvis-Relay-Client": visitor}


def _visitor_session_id(req: "ChatRequest", visitor: str) -> str:
    """The browser's session id, only if jarvis-web minted it for this
    visitor under this relay key; otherwise "" (jarvis-web starts a fresh
    public session). A caller can't carry on someone else's conversation by
    sending its id."""
    if req.session_id and throttle.relay_session_id_valid(
        req.session_id, JARVIS_RELAY_KEY, throttle.rate_limit_key(visitor),
    ):
        return req.session_id
    return ""


def _relay_body(req: "ChatRequest", visitor: str) -> dict:
    body = {"message": req.message, "interface_type": "chat", "source": SOURCE_TAG}
    session_id = _visitor_session_id(req, visitor)
    if session_id:
        body["session_id"] = session_id
    return body


def _http_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(**kwargs)


_upstream_rate_limited_warned = False


def _note_upstream_status(status: int) -> None:
    global _upstream_rate_limited_warned
    if status == 401:
        logger.error(
            "jarvis_relay_rejected status=401: jarvis-web refused the relay key; "
            "chat-embed's JARVIS_RELAY_KEY must equal jarvis-web's JARVIS_RELAY_KEY (key sha256 prefix %s)",
            hashlib.sha256(JARVIS_RELAY_KEY.encode()).hexdigest()[:12] if JARVIS_RELAY_KEY else "unset",
        )
    elif status == 429 and not _upstream_rate_limited_warned:
        _upstream_rate_limited_warned = True
        logger.warning("upstream_rate_limited: jarvis-web's relay limit was reached")


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
    visitor = await _admit(request)

    start = time.time()
    try:
        async with _http_client(timeout=120.0) as client:
            resp = await client.post(ATHENA_CHAT_URL, json=_relay_body(req, visitor), headers=_relay_headers(visitor))
            _note_upstream_status(resp.status_code)
            if resp.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="You're going a bit fast. Please wait a minute, then try again.",
                    headers={"Retry-After": resp.headers.get("Retry-After", "60")},
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
    upstream_session_id = data.get("session_id") or _visitor_session_id(req, visitor)

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

    visitor = await _admit(request)

    async def generate():
        # Every stream ends with exactly one terminal event: done or error.
        session_id = _visitor_session_id(req, visitor)
        try:
            async with _http_client(timeout=120.0) as client:
                async with client.stream(
                    "POST", STREAM_URL, json=_relay_body(req, visitor), headers=_relay_headers(visitor),
                ) as resp:
                    if resp.status_code != 200:
                        _note_upstream_status(resp.status_code)
                        if resp.status_code == 429:
                            yield f"data: {json.dumps({'type': 'error', 'reason': 'rate_limited'})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'error'})}\n\n"
                        return
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
                            if stage == "session":
                                session_id = obj.get("session_id") or session_id
                            elif stage == "answer_chunk":
                                token = obj.get("content", "")
                                if token:
                                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                            elif stage == "complete":
                                yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
                                return
                            elif stage == "error" or "error" in obj:
                                yield f"data: {json.dumps({'type': 'error'})}\n\n"
                                return
            yield f"data: {json.dumps({'type': 'error'})}\n\n"  # ended without completing
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
