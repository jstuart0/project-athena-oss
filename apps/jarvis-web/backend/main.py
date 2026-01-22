"""
Jarvis Web - Smart Home Interface Backend

A lightweight API that provides:
- Current guest information from Athena admin
- Chat proxy to Athena orchestrator
- Session management for conversations
- Dynamic owner/guest mode based on booking status

Mode Logic:
- Owner Mode: When no guest is booked, or manually overridden
- Guest Mode: When a guest is currently booked (restricted tool access)
"""
import os
import uuid
import httpx
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import structlog
import asyncio

# Configure logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

# Configuration from environment
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
ADMIN_BACKEND_URL = os.getenv("ADMIN_BACKEND_URL", "https://athena-admin.xmojo.net")
DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "guest")

# =============================================================================
# Mode Management (Owner vs Guest)
# =============================================================================

# Mode override state: None = auto-detect, "owner" = force owner, "guest" = force guest
mode_override: Optional[str] = None

async def get_current_mode() -> str:
    """
    Determine current mode based on guest status and override.

    Returns "owner" or "guest"
    """
    global mode_override

    # If manual override is set, use it
    if mode_override is not None:
        logger.debug("mode_override_active", mode=mode_override)
        return mode_override

    # Auto-detect based on guest booking
    guest = await get_current_guest()
    if guest and guest.get("has_guest"):
        logger.debug("mode_auto_guest", guest_name=guest.get("guest_name"))
        return "guest"
    else:
        logger.debug("mode_auto_owner", reason="no_guest_booked")
        return "owner"


class ModeState(BaseModel):
    """Current mode information"""
    mode: str  # "owner" or "guest"
    is_override: bool  # True if manually set
    auto_mode: str  # What mode would be without override
    has_guest: bool
    guest_name: Optional[str] = None


class SetModeRequest(BaseModel):
    """Request to set mode override"""
    mode: Optional[str] = None  # "owner", "guest", or None (auto)

# Home Assistant configuration
HA_URL = os.getenv("HA_URL", "https://ha.xmojo.net")
HA_TOKEN = os.getenv("HA_TOKEN", "")

# Voice services configuration (Mac mini STT/TTS)
VOICE_API_URL = os.getenv("VOICE_API_URL", "http://localhost:10201")
CLIMATE_ENTITY = os.getenv("CLIMATE_ENTITY", "climate.thermostat")

# Temperature limits for guest safety
MIN_TEMP = int(os.getenv("MIN_TEMP", "65"))
MAX_TEMP = int(os.getenv("MAX_TEMP", "75"))

app = FastAPI(
    title="Jarvis Web",
    description="Guest interface for Athena AI Assistant",
    version="1.0.0"
)

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store (for simple deployment)
sessions: Dict[str, Dict[str, Any]] = {}


class LocationOverride(BaseModel):
    """Location override from browser geolocation"""
    latitude: float
    longitude: float
    address: Optional[str] = None


class ChatMessage(BaseModel):
    """Chat message from user"""
    message: str
    session_id: Optional[str] = None
    interface_type: Optional[str] = "chat"  # chat, text, or voice
    location: Optional[LocationOverride] = None


class ChatResponse(BaseModel):
    """Response from Athena"""
    response: str
    session_id: str
    processing_time: Optional[float] = None
    tokens: Optional[int] = None
    tokens_per_second: Optional[float] = None
    tool_exec_time: Optional[float] = None
    model_used: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None  # Pass-through for browser_playback, etc.


class GuestInfo(BaseModel):
    """Current guest information"""
    has_guest: bool
    guest_name: Optional[str] = None
    checkin: Optional[str] = None
    checkout: Optional[str] = None


class WelcomeInfo(BaseModel):
    """Welcome information for the interface"""
    guest: GuestInfo
    greeting: str
    subtitle: str
    suggestions: List[str]


class ClimateState(BaseModel):
    """Current climate/thermostat state"""
    current_temp: float
    target_temp: float
    hvac_mode: str
    hvac_action: Optional[str] = None
    humidity: Optional[float] = None
    min_temp: int
    max_temp: int


class SetTemperatureRequest(BaseModel):
    """Request to set temperature"""
    temperature: int


async def get_current_guest() -> Optional[Dict[str, Any]]:
    """Fetch current guest from admin backend using internal endpoint."""
    # Use internal cluster service URL for service-to-service calls
    internal_url = os.getenv(
        "ADMIN_INTERNAL_URL",
        "http://athena-admin-backend.athena-admin.svc.cluster.local:8080"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            # Use internal endpoint that doesn't require authentication
            response = await client.get(
                f"{internal_url}/api/guest-mode/internal/current-guest",
                headers={"Accept": "application/json"}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("has_guest"):
                    logger.info(
                        "current_guest_fetched",
                        guest_name=data.get("guest_name"),
                        guest_id=data.get("id")
                    )
                    return data
            return None
    except Exception as e:
        logger.warning("failed_to_fetch_guest", error=str(e))
        return None


import random
from zoneinfo import ZoneInfo

# Baltimore timezone
LOCAL_TZ = ZoneInfo("America/New_York")

def get_time_based_greeting() -> str:
    """Get appropriate greeting based on time of day (in local timezone)"""
    local_time = datetime.now(LOCAL_TZ)
    hour = local_time.hour
    if 5 <= hour < 12:
        return "Good morning"
    elif 12 <= hour < 17:
        return "Good afternoon"
    elif 17 <= hour < 21:
        return "Good evening"
    else:
        return "Hello"


def get_dynamic_subtitle(has_guest: bool, first_name: str = None) -> str:
    """Get a dynamic, personalized subtitle message."""
    if has_guest and first_name:
        guest_messages = [
            f"Welcome to your stay! If you're not {first_name}, just let me know your name.",
            f"I'm here to make your visit wonderful. Not {first_name}? Tell me who you are!",
            f"Ready to help with anything you need! If you're not {first_name}, say hello and introduce yourself.",
            f"Your personal concierge at your service. Different guest? Just let me know!",
        ]
        return random.choice(guest_messages)
    else:
        default_messages = [
            "I'm Jarvis, your AI assistant. How can I help you today?",
            "Welcome! I'm Jarvis. Ask me anything about your stay.",
            "Hello! I'm here to help make your experience great.",
        ]
        return random.choice(default_messages)


def get_guest_suggestions() -> List[str]:
    """Get suggested queries for guests"""
    return [
        "What's the WiFi password?",
        "How do I use the TV?",
        "What restaurants are nearby?",
        "What's the weather today?",
        "Tell me about things to do in the area",
        "How do I adjust the thermostat?"
    ]


@app.get("/api/welcome", response_model=WelcomeInfo)
async def get_welcome():
    """Get welcome information including guest details and suggestions"""
    guest = await get_current_guest()

    base_greeting = get_time_based_greeting()

    if guest and guest.get("guest_name"):
        first_name = guest["guest_name"].split()[0]
        greeting = f"{base_greeting}, {first_name}!"
        subtitle = get_dynamic_subtitle(has_guest=True, first_name=first_name)
        guest_info = GuestInfo(
            has_guest=True,
            guest_name=guest.get("guest_name"),
            checkin=guest.get("checkin"),
            checkout=guest.get("checkout")
        )
    else:
        greeting = f"{base_greeting}!"
        subtitle = get_dynamic_subtitle(has_guest=False)
        guest_info = GuestInfo(has_guest=False)

    return WelcomeInfo(
        guest=guest_info,
        greeting=greeting,
        subtitle=subtitle,
        suggestions=get_guest_suggestions()
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(message: ChatMessage):
    """Send a message to Athena and get a response"""
    start_time = datetime.now()

    # Get or create session
    session_id = message.session_id or str(uuid.uuid4())

    if session_id not in sessions:
        sessions[session_id] = {
            "created": datetime.now().isoformat(),
            "message_count": 0
        }

    sessions[session_id]["message_count"] += 1
    sessions[session_id]["last_message"] = datetime.now().isoformat()

    # Fetch current guest information for context
    guest = await get_current_guest()
    context = {}
    if guest:
        context["guest_id"] = guest.get("id")
        context["guest_name"] = guest.get("guest_name")
        logger.info(
            "guest_context_attached",
            guest_id=guest.get("id"),
            guest_name=guest.get("guest_name")
        )

    try:
        # Get current mode (owner or guest based on booking/override)
        current_mode = await get_current_mode()

        async with httpx.AsyncClient(timeout=60.0) as client:
            request_body = {
                "query": message.message,
                "mode": current_mode,
                "room": DEFAULT_ROOM,
                "session_id": session_id,
                "interface_type": message.interface_type or "chat"  # chat/text prevents TTS normalization
            }

            # Build context with guest info and location override
            if context:
                request_body["context"] = context
            else:
                request_body["context"] = {}

            # Add location override to context if provided
            if message.location:
                request_body["context"]["location_override"] = {
                    "latitude": message.location.latitude,
                    "longitude": message.location.longitude,
                    "address": message.location.address
                }
                logger.info(
                    "location_override_set",
                    address=message.location.address,
                    lat=message.location.latitude,
                    lon=message.location.longitude
                )

            logger.info("chat_request", mode=current_mode, query_preview=message.message[:50])

            response = await client.post(
                f"{ORCHESTRATOR_URL}/query",
                json=request_body
            )

            if response.status_code != 200:
                logger.error("orchestrator_error", status=response.status_code, body=response.text)
                raise HTTPException(
                    status_code=502,
                    detail="Unable to process your request. Please try again."
                )

            data = response.json()
            answer = data.get("answer", "I'm sorry, I couldn't process that request.")

            # Use session_id from orchestrator response (it manages the actual session)
            orchestrator_session_id = data.get("session_id", session_id)

            processing_time = (datetime.now() - start_time).total_seconds()

            # Extract token metrics from orchestrator metadata
            orchestrator_metadata = data.get("metadata", {})

            logger.info(
                "chat_processed",
                session_id=orchestrator_session_id,
                query_length=len(message.message),
                response_length=len(answer),
                processing_time=processing_time,
                tokens=orchestrator_metadata.get("tokens"),
                tokens_per_second=orchestrator_metadata.get("tokens_per_second")
            )

            # Build metadata to pass through browser_playback, music_intent, and was_truncated
            pass_through_metadata = {}
            if orchestrator_metadata.get("browser_playback"):
                pass_through_metadata["browser_playback"] = orchestrator_metadata["browser_playback"]
            if orchestrator_metadata.get("music_intent"):
                pass_through_metadata["music_intent"] = orchestrator_metadata["music_intent"]
            # Always include was_truncated for Continue button functionality
            pass_through_metadata["was_truncated"] = orchestrator_metadata.get("was_truncated", False)

            return ChatResponse(
                response=answer,
                session_id=orchestrator_session_id,
                processing_time=processing_time,
                tokens=orchestrator_metadata.get("tokens"),
                tokens_per_second=orchestrator_metadata.get("tokens_per_second"),
                tool_exec_time=orchestrator_metadata.get("tool_exec_time"),
                model_used=orchestrator_metadata.get("model_used"),
                metadata=pass_through_metadata  # Always include (contains was_truncated)
            )

    except httpx.TimeoutException:
        logger.error("orchestrator_timeout", session_id=session_id)
        raise HTTPException(
            status_code=504,
            detail="Request timed out. Please try again."
        )
    except httpx.RequestError as e:
        logger.error("orchestrator_connection_error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please try again."
        )


@app.post("/api/chat/stream")
async def chat_stream(message: ChatMessage):
    """Stream a response from Athena (if supported)"""
    session_id = message.session_id or str(uuid.uuid4())

    if session_id not in sessions:
        sessions[session_id] = {
            "created": datetime.now().isoformat(),
            "message_count": 0
        }

    sessions[session_id]["message_count"] += 1

    # Fetch current guest information for context
    guest = await get_current_guest()
    context = {}
    if guest:
        context["guest_id"] = guest.get("id")
        context["guest_name"] = guest.get("guest_name")

    # Get current mode before entering generator
    current_mode = await get_current_mode()

    async def generate():
        try:
            request_body = {
                "query": message.message,
                "mode": current_mode,
                "room": DEFAULT_ROOM,
                "session_id": session_id
            }

            # Build context with guest info and location override
            if context:
                request_body["context"] = context
            else:
                request_body["context"] = {}

            # Add location override to context if provided
            if message.location:
                request_body["context"]["location_override"] = {
                    "latitude": message.location.latitude,
                    "longitude": message.location.longitude,
                    "address": message.location.address
                }

            logger.info("chat_stream_request", mode=current_mode, query_preview=message.message[:50])

            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{ORCHESTRATOR_URL}/query/stream",
                    json=request_body
                ) as response:
                    async for chunk in response.aiter_text():
                        yield f"data: {chunk}\n\n"
        except Exception as e:
            logger.error("stream_error", error=str(e))
            yield f'data: {{"error": "Stream failed"}}\n\n'

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@app.get("/api/health")
async def health():
    """Health check endpoint - quick check, doesn't block on slow orchestrator health"""
    orchestrator_healthy = False
    orchestrator_error = None

    try:
        # Use short timeout (2s) - orchestrator health can be slow
        # We just need to know if it's reachable, not wait for full health
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/")
            # Root returns 404 "Not Found" which means orchestrator is running
            if response.status_code in (200, 404):
                orchestrator_healthy = True
    except httpx.TimeoutException:
        orchestrator_error = "timeout"
    except httpx.RequestError as e:
        orchestrator_error = str(e)

    if orchestrator_healthy:
        return {
            "status": "healthy",
            "service": "jarvis-web",
            "orchestrator": "connected",
            "timestamp": datetime.now().isoformat()
        }
    else:
        return {
            "status": "degraded",
            "service": "jarvis-web",
            "orchestrator": "disconnected",
            "orchestrator_error": orchestrator_error,
            "timestamp": datetime.now().isoformat()
        }


@app.get("/api/geocode/reverse")
async def reverse_geocode(lat: float, lon: float):
    """Reverse geocode coordinates to address using Nominatim"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "json",
                    "zoom": 18,  # Street level for precision
                    "addressdetails": 1
                },
                headers={"User-Agent": "JarvisWeb/1.0"}
            )
            if response.status_code == 200:
                data = response.json()
                addr = data.get("address", {})
                logger.info("reverse_geocode_response", lat=lat, lon=lon, address=addr)

                # Build precise address with street-level detail
                location_part = None
                city_part = None

                # Priority for location: road > neighbourhood > suburb > village > hamlet
                for key in ["road", "neighbourhood", "suburb", "village", "hamlet", "county"]:
                    if addr.get(key):
                        location_part = addr[key]
                        break

                # Priority for city: city > town > municipality > county
                for key in ["city", "town", "municipality"]:
                    if addr.get(key):
                        city_part = addr[key]
                        break

                # Build the address string
                parts = []
                if location_part:
                    parts.append(location_part)
                if city_part and city_part != location_part:
                    parts.append(city_part)
                if addr.get("state") and len(parts) < 2:
                    # Only add state if we don't have enough detail
                    parts.append(addr["state"])

                if parts:
                    return {"address": ", ".join(parts)}
                elif addr.get("state"):
                    # Fallback to state if nothing else
                    return {"address": addr["state"]}
                else:
                    return {"address": data.get("display_name", f"{lat:.4f}, {lon:.4f}")}

            return {"address": f"{lat:.4f}, {lon:.4f}"}
    except Exception as e:
        logger.warning("reverse_geocode_failed", error=str(e))
        return {"address": f"{lat:.4f}, {lon:.4f}"}


@app.get("/api/geocode/search")
async def geocode_search(q: str, limit: int = 5):
    """Search for addresses using Nominatim"""
    if not q or len(q) < 3:
        return {"results": []}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": q,
                    "format": "json",
                    "limit": limit,
                    "addressdetails": 1
                },
                headers={"User-Agent": "JarvisWeb/1.0"}
            )
            if response.status_code == 200:
                data = response.json()
                logger.info("geocode_search_response", query=q, num_results=len(data))

                results = []
                for item in data:
                    addr = item.get("address", {})

                    # Build a short name from the address
                    name = None
                    for key in ["road", "neighbourhood", "suburb", "village", "hamlet", "city", "town"]:
                        if addr.get(key):
                            name = addr[key]
                            break

                    results.append({
                        "lat": item.get("lat"),
                        "lon": item.get("lon"),
                        "display_name": item.get("display_name", ""),
                        "name": name or item.get("display_name", "").split(",")[0],
                        "type": item.get("type")
                    })

                return {"results": results}

            return {"results": []}
    except Exception as e:
        logger.warning("geocode_search_failed", error=str(e))
        return {"results": []}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    """Get session information"""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    return sessions[session_id]


# =============================================================================
# Mode Management API
# =============================================================================

@app.get("/api/mode", response_model=ModeState)
async def get_mode():
    """
    Get current mode status.

    Returns the active mode, whether it's overridden, and guest status.
    """
    global mode_override

    guest = await get_current_guest()
    has_guest = bool(guest and guest.get("has_guest"))

    # Determine auto mode (what would be used without override)
    auto_mode = "guest" if has_guest else "owner"

    # Determine active mode
    active_mode = mode_override if mode_override is not None else auto_mode

    return ModeState(
        mode=active_mode,
        is_override=mode_override is not None,
        auto_mode=auto_mode,
        has_guest=has_guest,
        guest_name=guest.get("guest_name") if guest else None
    )


@app.post("/api/mode", response_model=ModeState)
async def set_mode(request: SetModeRequest):
    """
    Set or clear the mode override.

    - mode: "owner" - Force owner mode (full tool access)
    - mode: "guest" - Force guest mode (restricted access)
    - mode: null/None - Clear override, use auto-detection

    Note: When in guest mode, switching to owner mode is not allowed.
    """
    global mode_override

    if request.mode is not None and request.mode not in ["owner", "guest"]:
        raise HTTPException(
            status_code=400,
            detail="Mode must be 'owner', 'guest', or null"
        )

    # Get current mode to check if we're in guest mode
    current_mode = await get_current_mode()

    # SECURITY: Prevent guests from switching to owner mode
    if current_mode == "guest":
        # Check what mode they're trying to switch to
        if request.mode == "owner":
            logger.warning(
                "guest_mode_escalation_blocked",
                attempted_mode="owner",
                current_mode=current_mode
            )
            raise HTTPException(
                status_code=403,
                detail="Cannot switch to owner mode while in guest mode"
            )

        # Also block auto-detect if it would result in owner mode
        if request.mode is None:
            guest = await get_current_guest()
            if not (guest and guest.get("has_guest")):
                # Auto-detect would result in owner mode - block it
                logger.warning(
                    "guest_mode_escalation_blocked",
                    attempted_mode="auto",
                    would_become="owner",
                    current_mode=current_mode
                )
                raise HTTPException(
                    status_code=403,
                    detail="Cannot switch to auto-detect mode (would become owner mode)"
                )

    old_mode = mode_override
    mode_override = request.mode

    logger.info(
        "mode_override_changed",
        old_mode=old_mode,
        new_mode=mode_override,
        is_override=mode_override is not None
    )

    # Return updated state
    return await get_mode()


@app.delete("/api/mode")
async def clear_mode_override():
    """Clear the mode override and return to auto-detection."""
    global mode_override

    old_mode = mode_override
    mode_override = None

    logger.info("mode_override_cleared", old_mode=old_mode)

    return await get_mode()


# =============================================================================
# Climate/Thermostat Control
# =============================================================================

async def get_ha_headers() -> Dict[str, str]:
    """Get Home Assistant API headers with token"""
    token = HA_TOKEN
    if not token:
        # Try to get from environment or kubernetes secret
        logger.warning("HA_TOKEN not configured")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


@app.get("/api/climate", response_model=ClimateState)
async def get_climate():
    """Get current thermostat state"""
    if not HA_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Climate control not configured"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.get(
                f"{HA_URL}/api/states/{CLIMATE_ENTITY}",
                headers=await get_ha_headers()
            )

            if response.status_code != 200:
                logger.error("ha_climate_error", status=response.status_code)
                raise HTTPException(
                    status_code=502,
                    detail="Unable to fetch climate state"
                )

            data = response.json()
            attrs = data.get("attributes", {})

            return ClimateState(
                current_temp=attrs.get("current_temperature", 70),
                target_temp=attrs.get("temperature", 70),
                hvac_mode=data.get("state", "off"),
                hvac_action=attrs.get("hvac_action"),
                humidity=attrs.get("current_humidity"),
                min_temp=MIN_TEMP,
                max_temp=MAX_TEMP
            )

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Unable to connect to climate system"
        )


@app.post("/api/climate/temperature")
async def set_temperature(request: SetTemperatureRequest):
    """Set thermostat target temperature (with guest-safe limits)"""
    if not HA_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Climate control not configured"
        )

    # Enforce guest-safe temperature limits
    temp = request.temperature
    if temp < MIN_TEMP:
        temp = MIN_TEMP
        logger.info("temperature_clamped_to_min", requested=request.temperature, set=temp)
    elif temp > MAX_TEMP:
        temp = MAX_TEMP
        logger.info("temperature_clamped_to_max", requested=request.temperature, set=temp)

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/climate/set_temperature",
                headers=await get_ha_headers(),
                json={
                    "entity_id": CLIMATE_ENTITY,
                    "temperature": temp
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("ha_set_temp_error", status=response.status_code)
                raise HTTPException(
                    status_code=502,
                    detail="Unable to set temperature"
                )

            logger.info("temperature_set", temperature=temp)
            return {
                "success": True,
                "temperature": temp,
                "message": f"Temperature set to {temp}°F"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Unable to connect to climate system"
        )


@app.post("/api/climate/mode/{mode}")
async def set_hvac_mode(mode: str):
    """Set HVAC mode (heat, cool, off, heat_cool)"""
    if not HA_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Climate control not configured"
        )

    valid_modes = ["heat", "cool", "off", "heat_cool", "auto"]
    if mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode. Must be one of: {', '.join(valid_modes)}"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/climate/set_hvac_mode",
                headers=await get_ha_headers(),
                json={
                    "entity_id": CLIMATE_ENTITY,
                    "hvac_mode": mode
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("ha_set_mode_error", status=response.status_code)
                raise HTTPException(
                    status_code=502,
                    detail="Unable to set HVAC mode"
                )

            logger.info("hvac_mode_set", mode=mode)
            return {
                "success": True,
                "mode": mode,
                "message": f"HVAC mode set to {mode}"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Unable to connect to climate system"
        )


# =============================================================================
# Sensor Monitoring
# =============================================================================

# Sensor entity patterns for room-based grouping
MOTION_PATTERNS = ["motion", "occupancy", "presence"]
TEMPERATURE_PATTERNS = ["temperature"]
ILLUMINANCE_PATTERNS = ["illuminance", "lux", "light_level"]

# Room name extraction patterns
ROOM_NAMES = [
    "office", "kitchen", "living_room", "living room", "bedroom", "master_bedroom",
    "master bedroom", "master_bath", "main_bath", "bathroom", "basement",
    "dining_room", "dining room", "hallway", "hall", "powder", "shower",
    "alpha", "beta", "master_closet", "master closet", "front_door", "front door",
    "back_door", "back door", "entrance"
]

# Device ID to room mapping for sensors without room in entity_id
# (e.g., Aqara FP2 sensors that use serial numbers instead of room names)
DEVICE_ROOM_MAP = {
    "fp2_15c0": "Office",
    "fp2_216e": "Living Room",
    "fp2_d84c": "Kitchen",
    "fp2_e3cd": "Bedroom",
}


def extract_room_from_entity(entity_id: str, friendly_name: str) -> str:
    """Extract room name from entity ID or friendly name"""
    entity_lower = entity_id.lower()

    # Check device ID mapping first (for sensors like FP2 with serial numbers)
    for device_id, room in DEVICE_ROOM_MAP.items():
        if device_id in entity_lower:
            return room

    # Fall back to pattern matching
    text = f"{entity_id} {friendly_name}".lower()
    for room in ROOM_NAMES:
        if room.replace("_", " ") in text or room.replace(" ", "_") in text:
            return room.replace("_", " ").title()
    return "Unknown"


async def get_all_states() -> List[Dict[str, Any]]:
    """Fetch all states from Home Assistant"""
    if not HA_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            response = await client.get(
                f"{HA_URL}/api/states",
                headers=await get_ha_headers()
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.error("ha_states_error", error=str(e))
    return []


@app.get("/api/sensors/motion")
async def get_motion_sensors():
    """Get all motion/occupancy sensors grouped by room"""
    states = await get_all_states()
    if not states:
        raise HTTPException(status_code=503, detail="Unable to fetch sensor data")

    sensors = []
    for entity in states:
        entity_id = entity["entity_id"]
        # Only binary sensors for motion
        if not entity_id.startswith("binary_sensor."):
            continue
        if not any(p in entity_id.lower() for p in MOTION_PATTERNS):
            continue
        # Skip non-sensor entries (like input_boolean, etc) and tamper sensors
        if "disable" in entity_id.lower() or "input_" in entity_id:
            continue
        # Skip tamper sensors - they detect cover removal, not motion
        if "tamper" in entity_id.lower():
            continue

        friendly_name = entity["attributes"].get("friendly_name", entity_id)
        room = extract_room_from_entity(entity_id, friendly_name)

        sensors.append({
            "entity_id": entity_id,
            "room": room,
            "name": friendly_name,
            "motion_detected": entity["state"] == "on",
            "last_changed": entity.get("last_changed")
        })

    # Sort by motion detected (active first), then by room
    sensors.sort(key=lambda x: (not x["motion_detected"], x["room"]))

    # Summary
    active_rooms = list(set(s["room"] for s in sensors if s["motion_detected"]))

    return {
        "sensors": sensors,
        "active_rooms": active_rooms,
        "total_sensors": len(sensors),
        "motion_detected_count": sum(1 for s in sensors if s["motion_detected"])
    }


@app.get("/api/sensors/temperature")
async def get_temperature_sensors():
    """Get all temperature sensors"""
    states = await get_all_states()
    if not states:
        raise HTTPException(status_code=503, detail="Unable to fetch sensor data")

    sensors = []
    for entity in states:
        entity_id = entity["entity_id"]
        # Only actual sensor readings
        if not entity_id.startswith("sensor."):
            continue
        if "temperature" not in entity_id.lower():
            continue
        # Skip overtemperature binary sensors and unavailable
        if entity["state"] in ["unavailable", "unknown"]:
            continue

        friendly_name = entity["attributes"].get("friendly_name", entity_id)
        room = extract_room_from_entity(entity_id, friendly_name)

        try:
            temp_value = float(entity["state"])
        except (ValueError, TypeError):
            continue

        unit = entity["attributes"].get("unit_of_measurement", "°F")

        sensors.append({
            "entity_id": entity_id,
            "room": room,
            "name": friendly_name,
            "temperature": temp_value,
            "unit": unit,
            "last_changed": entity.get("last_changed")
        })

    sensors.sort(key=lambda x: x["room"])

    return {
        "sensors": sensors,
        "total_sensors": len(sensors)
    }


@app.get("/api/sensors/illuminance")
async def get_illuminance_sensors():
    """Get all light/illuminance sensors"""
    states = await get_all_states()
    if not states:
        raise HTTPException(status_code=503, detail="Unable to fetch sensor data")

    sensors = []
    for entity in states:
        entity_id = entity["entity_id"]
        if not entity_id.startswith("sensor."):
            continue
        if not any(p in entity_id.lower() for p in ILLUMINANCE_PATTERNS):
            continue
        if entity["state"] in ["unavailable", "unknown"]:
            continue

        friendly_name = entity["attributes"].get("friendly_name", entity_id)
        room = extract_room_from_entity(entity_id, friendly_name)

        try:
            lux_value = float(entity["state"])
        except (ValueError, TypeError):
            continue

        unit = entity["attributes"].get("unit_of_measurement", "lx")

        sensors.append({
            "entity_id": entity_id,
            "room": room,
            "name": friendly_name,
            "illuminance": lux_value,
            "unit": unit,
            "brightness_level": "dark" if lux_value < 10 else "dim" if lux_value < 50 else "normal" if lux_value < 200 else "bright",
            "last_changed": entity.get("last_changed")
        })

    sensors.sort(key=lambda x: x["room"])

    return {
        "sensors": sensors,
        "total_sensors": len(sensors)
    }


@app.get("/api/sensors/summary")
async def get_sensors_summary():
    """Get a summary of all sensors for quick overview"""
    states = await get_all_states()
    if not states:
        raise HTTPException(status_code=503, detail="Unable to fetch sensor data")

    # Motion summary
    motion_active = []
    for entity in states:
        if entity["entity_id"].startswith("binary_sensor."):
            entity_lower = entity["entity_id"].lower()
            if any(p in entity_lower for p in MOTION_PATTERNS):
                # Skip disabled and tamper sensors
                if entity["state"] == "on" and "disable" not in entity_lower and "tamper" not in entity_lower:
                    room = extract_room_from_entity(
                        entity["entity_id"],
                        entity["attributes"].get("friendly_name", "")
                    )
                    if room not in motion_active:
                        motion_active.append(room)

    # Temperature summary
    temps = []
    for entity in states:
        if entity["entity_id"].startswith("sensor.") and "temperature" in entity["entity_id"].lower():
            if entity["state"] not in ["unavailable", "unknown"]:
                try:
                    temps.append(float(entity["state"]))
                except:
                    pass

    # Light level summary
    light_levels = {}
    for entity in states:
        if entity["entity_id"].startswith("sensor."):
            if any(p in entity["entity_id"].lower() for p in ILLUMINANCE_PATTERNS):
                if entity["state"] not in ["unavailable", "unknown"]:
                    try:
                        room = extract_room_from_entity(
                            entity["entity_id"],
                            entity["attributes"].get("friendly_name", "")
                        )
                        light_levels[room] = float(entity["state"])
                    except:
                        pass

    return {
        "motion": {
            "active_rooms": motion_active,
            "rooms_with_motion": len(motion_active)
        },
        "temperature": {
            "average": round(sum(temps) / len(temps), 1) if temps else None,
            "sensor_count": len(temps)
        },
        "illuminance": {
            "by_room": light_levels,
            "darkest_room": min(light_levels, key=light_levels.get) if light_levels else None,
            "brightest_room": max(light_levels, key=light_levels.get) if light_levels else None
        }
    }


# =============================================================================
# Media Player Control
# =============================================================================

# Known media players to expose (friendly names)
MEDIA_PLAYERS = {
    "media_player.living_room": "Living Room Sonos",
    "media_player.living_room_2": "Living Room Apple TV",
    "media_player.master_bedroom_tv": "Master Bedroom Apple TV",
    "media_player.samsung_q80_series_75": "Living Room Samsung TV",
    "media_player.spotify_jay_stuart": "Spotify",
    "media_player.home_speakers": "Home Speakers"
}


@app.get("/api/media")
async def get_media_players():
    """Get all available media players and their states"""
    states = await get_all_states()
    if not states:
        raise HTTPException(status_code=503, detail="Unable to fetch media players")

    players = []
    for entity in states:
        entity_id = entity["entity_id"]
        if not entity_id.startswith("media_player."):
            continue
        if entity["state"] == "unavailable":
            continue

        attrs = entity["attributes"]
        friendly_name = attrs.get("friendly_name", entity_id)

        # Determine device type
        device_type = "unknown"
        if "apple" in entity_id.lower() or "apple" in friendly_name.lower():
            device_type = "apple_tv"
        elif "sonos" in entity_id.lower() or "sonos" in friendly_name.lower():
            device_type = "sonos"
        elif "samsung" in entity_id.lower() or "samsung" in friendly_name.lower():
            device_type = "samsung_tv"
        elif "spotify" in entity_id.lower():
            device_type = "spotify"
        elif "homepod" in entity_id.lower() or "home_speaker" in entity_id.lower():
            device_type = "homepod"

        player = {
            "entity_id": entity_id,
            "name": friendly_name,
            "state": entity["state"],  # off, idle, playing, paused
            "device_type": device_type,
            "volume_level": attrs.get("volume_level"),
            "is_volume_muted": attrs.get("is_volume_muted", False),
            "media_title": attrs.get("media_title"),
            "media_artist": attrs.get("media_artist"),
            "media_album": attrs.get("media_album_name"),
            "source": attrs.get("source"),
            "source_list": attrs.get("source_list", [])[:10],  # Limit to 10 sources
            "app_name": attrs.get("app_name"),
            "supported_features": attrs.get("supported_features", 0)
        }
        players.append(player)

    # Sort by state (playing first) then by name
    state_order = {"playing": 0, "paused": 1, "idle": 2, "off": 3}
    players.sort(key=lambda x: (state_order.get(x["state"], 4), x["name"]))

    return {
        "players": players,
        "total": len(players),
        "playing_count": sum(1 for p in players if p["state"] == "playing")
    }


@app.post("/api/media/{entity_id}/play")
async def media_play(entity_id: str):
    """Start or resume playback"""
    return await _media_command(entity_id, "media_play")


@app.post("/api/media/{entity_id}/pause")
async def media_pause(entity_id: str):
    """Pause playback"""
    return await _media_command(entity_id, "media_pause")


@app.post("/api/media/{entity_id}/stop")
async def media_stop(entity_id: str):
    """Stop playback"""
    return await _media_command(entity_id, "media_stop")


@app.post("/api/media/{entity_id}/next")
async def media_next(entity_id: str):
    """Skip to next track"""
    return await _media_command(entity_id, "media_next_track")


@app.post("/api/media/{entity_id}/previous")
async def media_previous(entity_id: str):
    """Go to previous track"""
    return await _media_command(entity_id, "media_previous_track")


class VolumeRequest(BaseModel):
    volume: float  # 0.0 to 1.0


@app.post("/api/media/{entity_id}/volume")
async def media_volume(entity_id: str, request: VolumeRequest):
    """Set volume level (0.0 to 1.0)"""
    volume = max(0.0, min(1.0, request.volume))
    return await _media_command(entity_id, "volume_set", {"volume_level": volume})


@app.post("/api/media/{entity_id}/mute")
async def media_mute(entity_id: str):
    """Toggle mute"""
    return await _media_command(entity_id, "volume_mute", {"is_volume_muted": True})


@app.post("/api/media/{entity_id}/unmute")
async def media_unmute(entity_id: str):
    """Unmute"""
    return await _media_command(entity_id, "volume_mute", {"is_volume_muted": False})


class SourceRequest(BaseModel):
    source: str


@app.post("/api/media/{entity_id}/source")
async def media_select_source(entity_id: str, request: SourceRequest):
    """Select input source or app"""
    return await _media_command(entity_id, "select_source", {"source": request.source})


@app.post("/api/media/{entity_id}/turn_on")
async def media_turn_on(entity_id: str):
    """Turn on media player"""
    return await _media_command(entity_id, "turn_on")


@app.post("/api/media/{entity_id}/turn_off")
async def media_turn_off(entity_id: str):
    """Turn off media player"""
    return await _media_command(entity_id, "turn_off")


async def _media_command(entity_id: str, service: str, data: Dict[str, Any] = None) -> Dict:
    """Execute a media player command"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    # Ensure entity_id has proper prefix
    if not entity_id.startswith("media_player."):
        entity_id = f"media_player.{entity_id}"

    payload = {"entity_id": entity_id}
    if data:
        payload.update(data)

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/media_player/{service}",
                headers=await get_ha_headers(),
                json=payload
            )

            if response.status_code not in [200, 201]:
                logger.error("media_command_error", status=response.status_code, service=service)
                raise HTTPException(status_code=502, detail=f"Failed to execute {service}")

            logger.info("media_command_executed", entity_id=entity_id, service=service)
            return {
                "success": True,
                "entity_id": entity_id,
                "command": service
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


# =============================================================================
# Kitchen Appliances (Oven, Fridge, Freezer)
# =============================================================================

# GE Appliance entity IDs (exposed as water_heater entities)
OVEN_ENTITY = "water_heater.szrm197097p_oven"
FRIDGE_ENTITY = "water_heater.tl514903_fridge"
FREEZER_ENTITY = "water_heater.tl514903_freezer"

# Related sensors
STOVE_COOK_MODE_SENSOR = "sensor.stove_cook_mode"
STOVE_DISPLAY_TEMP_SENSOR = "sensor.stove_display_temperature"
STOVE_TIMER_SENSOR = "sensor.stove_cook_time_remaining"
FRIDGE_DOOR_SENSOR = "binary_sensor.refrigerator_door"

# Temperature limits for safety
OVEN_MIN_TEMP = 170
OVEN_MAX_TEMP = 550
FRIDGE_MIN_TEMP = 34
FRIDGE_MAX_TEMP = 42
FREEZER_MIN_TEMP = -6
FREEZER_MAX_TEMP = 5


class OvenState(BaseModel):
    """Oven state information"""
    state: str  # off, heating, idle
    current_temp: Optional[float] = None
    target_temp: Optional[float] = None
    cook_mode: Optional[str] = None
    time_remaining: Optional[str] = None
    available_modes: List[str] = []
    min_temp: int = OVEN_MIN_TEMP
    max_temp: int = OVEN_MAX_TEMP


class FridgeState(BaseModel):
    """Fridge/Freezer state information"""
    fridge_target_temp: Optional[float] = None
    fridge_mode: Optional[str] = None
    fridge_min_temp: int = FRIDGE_MIN_TEMP
    fridge_max_temp: int = FRIDGE_MAX_TEMP
    freezer_target_temp: Optional[float] = None
    freezer_mode: Optional[str] = None
    freezer_min_temp: int = FREEZER_MIN_TEMP
    freezer_max_temp: int = FREEZER_MAX_TEMP
    door_open: bool = False
    fridge_modes: List[str] = []
    freezer_modes: List[str] = []


class SetOvenTempRequest(BaseModel):
    """Request to set oven temperature"""
    temperature: int


class SetOvenModeRequest(BaseModel):
    """Request to set oven cooking mode"""
    mode: str


class SetApplianceTempRequest(BaseModel):
    """Request to set fridge/freezer temperature"""
    temperature: int


@app.get("/api/appliances/oven", response_model=OvenState)
async def get_oven_state():
    """Get current oven state, temperature, and cooking mode"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            headers = await get_ha_headers()

            # Get oven entity state
            oven_resp = await client.get(
                f"{HA_URL}/api/states/{OVEN_ENTITY}",
                headers=headers
            )

            if oven_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Unable to fetch oven state")

            oven_data = oven_resp.json()
            attrs = oven_data.get("attributes", {})

            # Get cook mode sensor
            cook_mode = None
            try:
                mode_resp = await client.get(
                    f"{HA_URL}/api/states/{STOVE_COOK_MODE_SENSOR}",
                    headers=headers
                )
                if mode_resp.status_code == 200:
                    cook_mode = mode_resp.json().get("state")
            except:
                pass

            # Get display temperature sensor
            display_temp = None
            try:
                temp_resp = await client.get(
                    f"{HA_URL}/api/states/{STOVE_DISPLAY_TEMP_SENSOR}",
                    headers=headers
                )
                if temp_resp.status_code == 200:
                    temp_state = temp_resp.json().get("state")
                    if temp_state not in ["unavailable", "unknown"]:
                        display_temp = float(temp_state)
            except:
                pass

            # Get timer
            time_remaining = None
            try:
                timer_resp = await client.get(
                    f"{HA_URL}/api/states/{STOVE_TIMER_SENSOR}",
                    headers=headers
                )
                if timer_resp.status_code == 200:
                    timer_state = timer_resp.json().get("state")
                    if timer_state not in ["unavailable", "unknown", "0"]:
                        time_remaining = timer_state
            except:
                pass

            return OvenState(
                state=oven_data.get("state", "off"),
                current_temp=display_temp or attrs.get("current_temperature"),
                target_temp=attrs.get("temperature"),
                cook_mode=cook_mode or attrs.get("operation_mode"),
                time_remaining=time_remaining,
                available_modes=attrs.get("operation_list", []),
                min_temp=int(attrs.get("min_temp", OVEN_MIN_TEMP)),
                max_temp=int(attrs.get("max_temp", OVEN_MAX_TEMP))
            )

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appliances/oven/temperature")
async def set_oven_temperature(request: SetOvenTempRequest):
    """Set oven target temperature"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    temp = request.temperature
    if temp < OVEN_MIN_TEMP or temp > OVEN_MAX_TEMP:
        raise HTTPException(
            status_code=400,
            detail=f"Temperature must be between {OVEN_MIN_TEMP}°F and {OVEN_MAX_TEMP}°F"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/water_heater/set_temperature",
                headers=await get_ha_headers(),
                json={
                    "entity_id": OVEN_ENTITY,
                    "temperature": temp
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("oven_set_temp_error", status=response.status_code)
                raise HTTPException(status_code=502, detail="Unable to set oven temperature")

            logger.info("oven_temperature_set", temperature=temp)
            return {
                "success": True,
                "temperature": temp,
                "message": f"Oven temperature set to {temp}°F"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appliances/oven/mode")
async def set_oven_mode(request: SetOvenModeRequest):
    """Set oven cooking mode (Bake, Convection, etc.)"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/water_heater/set_operation_mode",
                headers=await get_ha_headers(),
                json={
                    "entity_id": OVEN_ENTITY,
                    "operation_mode": request.mode
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("oven_set_mode_error", status=response.status_code)
                raise HTTPException(status_code=502, detail="Unable to set oven mode")

            logger.info("oven_mode_set", mode=request.mode)
            return {
                "success": True,
                "mode": request.mode,
                "message": f"Oven mode set to {request.mode}"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appliances/oven/off")
async def turn_oven_off():
    """Turn off the oven"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/water_heater/set_operation_mode",
                headers=await get_ha_headers(),
                json={
                    "entity_id": OVEN_ENTITY,
                    "operation_mode": "Off"
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("oven_off_error", status=response.status_code)
                raise HTTPException(status_code=502, detail="Unable to turn off oven")

            logger.info("oven_turned_off")
            return {
                "success": True,
                "message": "Oven turned off"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.get("/api/appliances/fridge", response_model=FridgeState)
async def get_fridge_state():
    """Get fridge and freezer state"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            headers = await get_ha_headers()

            # Get fridge state
            fridge_resp = await client.get(
                f"{HA_URL}/api/states/{FRIDGE_ENTITY}",
                headers=headers
            )

            # Get freezer state
            freezer_resp = await client.get(
                f"{HA_URL}/api/states/{FREEZER_ENTITY}",
                headers=headers
            )

            # Get door sensor
            door_open = False
            try:
                door_resp = await client.get(
                    f"{HA_URL}/api/states/{FRIDGE_DOOR_SENSOR}",
                    headers=headers
                )
                if door_resp.status_code == 200:
                    door_open = door_resp.json().get("state") == "on"
            except:
                pass

            fridge_data = fridge_resp.json() if fridge_resp.status_code == 200 else {}
            freezer_data = freezer_resp.json() if freezer_resp.status_code == 200 else {}

            fridge_attrs = fridge_data.get("attributes", {})
            freezer_attrs = freezer_data.get("attributes", {})

            return FridgeState(
                fridge_target_temp=fridge_attrs.get("temperature"),
                fridge_mode=fridge_attrs.get("operation_mode"),
                fridge_min_temp=int(fridge_attrs.get("min_temp", FRIDGE_MIN_TEMP)),
                fridge_max_temp=int(fridge_attrs.get("max_temp", FRIDGE_MAX_TEMP)),
                freezer_target_temp=freezer_attrs.get("temperature"),
                freezer_mode=freezer_attrs.get("operation_mode"),
                freezer_min_temp=int(freezer_attrs.get("min_temp", FREEZER_MIN_TEMP)),
                freezer_max_temp=int(freezer_attrs.get("max_temp", FREEZER_MAX_TEMP)),
                door_open=door_open,
                fridge_modes=fridge_attrs.get("operation_list", []),
                freezer_modes=freezer_attrs.get("operation_list", [])
            )

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appliances/fridge/temperature")
async def set_fridge_temperature(request: SetApplianceTempRequest):
    """Set fridge target temperature"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    temp = request.temperature
    if temp < FRIDGE_MIN_TEMP or temp > FRIDGE_MAX_TEMP:
        raise HTTPException(
            status_code=400,
            detail=f"Fridge temperature must be between {FRIDGE_MIN_TEMP}°F and {FRIDGE_MAX_TEMP}°F"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/water_heater/set_temperature",
                headers=await get_ha_headers(),
                json={
                    "entity_id": FRIDGE_ENTITY,
                    "temperature": temp
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("fridge_set_temp_error", status=response.status_code)
                raise HTTPException(status_code=502, detail="Unable to set fridge temperature")

            logger.info("fridge_temperature_set", temperature=temp)
            return {
                "success": True,
                "temperature": temp,
                "message": f"Fridge temperature set to {temp}°F"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appliances/freezer/temperature")
async def set_freezer_temperature(request: SetApplianceTempRequest):
    """Set freezer target temperature"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    temp = request.temperature
    if temp < FREEZER_MIN_TEMP or temp > FREEZER_MAX_TEMP:
        raise HTTPException(
            status_code=400,
            detail=f"Freezer temperature must be between {FREEZER_MIN_TEMP}°F and {FREEZER_MAX_TEMP}°F"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/water_heater/set_temperature",
                headers=await get_ha_headers(),
                json={
                    "entity_id": FREEZER_ENTITY,
                    "temperature": temp
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("freezer_set_temp_error", status=response.status_code)
                raise HTTPException(status_code=502, detail="Unable to set freezer temperature")

            logger.info("freezer_temperature_set", temperature=temp)
            return {
                "success": True,
                "temperature": temp,
                "message": f"Freezer temperature set to {temp}°F"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


# =============================================================================
# Apple TV Control
# =============================================================================

# Room TV configs are now fetched dynamically from admin backend
# Use internal endpoint for service-to-service calls
ADMIN_INTERNAL_URL = os.getenv(
    "ADMIN_INTERNAL_URL",
    "http://athena-admin-backend.athena-admin.svc.cluster.local:8080"
)


async def get_room_tv_configs() -> Dict[str, Dict[str, str]]:
    """
    Fetch room TV configurations from admin backend.
    Returns dict mapping entity_id to {name, remote} config.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            response = await client.get(
                f"{ADMIN_INTERNAL_URL}/api/room-tv/internal"
            )
            if response.status_code == 200:
                configs = response.json()
                # Convert to dict keyed by entity_id for compatibility
                return {
                    c["media_player_entity_id"]: {
                        "name": c["display_name"],
                        "room_name": c["room_name"],
                        "remote": c["remote_entity_id"]
                    }
                    for c in configs
                }
    except Exception as e:
        logger.warning("failed_to_fetch_tv_configs", error=str(e))

    # Fallback to empty dict - UI will show "no TVs found"
    return {}

# Core streaming apps with their logos and colors
# Logos served locally from /logos/ directory (originally from homarr-labs/dashboard-icons)
STREAMING_APPS = [
    {"name": "Netflix", "logo": "/logos/netflix.svg", "color": "#E50914"},
    {"name": "YouTube", "logo": "/logos/youtube.svg", "color": "#FF0000"},
    {"name": "Disney+", "logo": "/logos/disney-plus.svg", "color": "#113CCF"},
    {"name": "Hulu", "logo": "/logos/hulu.svg", "color": "#1CE783"},
    {"name": "Prime Video", "logo": "/logos/prime-video.svg", "color": "#00A8E1"},
    {"name": "HBO Max", "logo": "/logos/max.svg", "color": "#B026FF"},
    {"name": "Paramount+", "logo": "/logos/paramount-plus.svg", "color": "#0064FF"},
    {"name": "Peacock", "logo": "/logos/peacock.svg", "color": "#000000"},
    {"name": "YouTube TV", "logo": "/logos/youtube-tv.svg", "color": "#FF0000"},
    {"name": "Spotify", "logo": "/logos/spotify.svg", "color": "#1DB954"},
    {"name": "ESPN", "logo": "/logos/espn.svg", "color": "#D00027"},
    {"name": "Apple TV+", "logo": "/logos/apple-tv-plus.svg", "color": "#000000"},
]


class AppleTVState(BaseModel):
    """Apple TV state information"""
    entity_id: str
    name: str
    state: str
    app_name: Optional[str] = None
    app_id: Optional[str] = None
    media_title: Optional[str] = None
    media_artist: Optional[str] = None
    source_list: List[str] = []


class RemoteCommandRequest(BaseModel):
    """Remote control command request"""
    command: str  # up, down, left, right, select, menu, home, play, pause


@app.get("/api/appletv")
async def get_apple_tvs() -> List[AppleTVState]:
    """Get all Apple TVs and their current state"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    # Fetch TV configs dynamically from admin backend
    tv_configs = await get_room_tv_configs()

    states = await get_all_states()
    if not states:
        raise HTTPException(status_code=503, detail="Unable to fetch Apple TV states")

    apple_tvs = []
    for entity_id, config in tv_configs.items():
        # Find this entity in states
        entity = next((e for e in states if e["entity_id"] == entity_id), None)
        if not entity:
            continue

        attrs = entity.get("attributes", {})
        apple_tvs.append(AppleTVState(
            entity_id=entity_id,
            name=config["name"],
            state=entity.get("state", "unavailable"),
            app_name=attrs.get("app_name"),
            app_id=attrs.get("app_id"),
            media_title=attrs.get("media_title"),
            media_artist=attrs.get("media_artist"),
            source_list=attrs.get("source_list", [])
        ))

    return apple_tvs


@app.get("/api/appletv/apps")
async def get_streaming_apps():
    """Get list of available streaming apps"""
    return STREAMING_APPS


@app.post("/api/appletv/{entity_id}/launch/{app_name}")
async def launch_app(entity_id: str, app_name: str):
    """Launch an app on the Apple TV"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    # Ensure entity_id has proper prefix
    if not entity_id.startswith("media_player."):
        entity_id = f"media_player.{entity_id}"

    # Fetch TV configs dynamically and validate
    tv_configs = await get_room_tv_configs()
    if entity_id not in tv_configs:
        raise HTTPException(status_code=404, detail=f"Unknown Apple TV: {entity_id}")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/media_player/select_source",
                headers=await get_ha_headers(),
                json={
                    "entity_id": entity_id,
                    "source": app_name
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("appletv_launch_error", entity_id=entity_id, app=app_name)
                raise HTTPException(status_code=502, detail=f"Failed to launch {app_name}")

            logger.info("appletv_app_launched", entity_id=entity_id, app=app_name)
            return {
                "success": True,
                "entity_id": entity_id,
                "app": app_name,
                "message": f"Launching {app_name}"
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appletv/{entity_id}/remote")
async def send_remote_command(entity_id: str, request: RemoteCommandRequest):
    """Send a remote control command to the Apple TV"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    # Ensure entity_id has proper prefix
    if not entity_id.startswith("media_player."):
        entity_id = f"media_player.{entity_id}"

    # Fetch TV configs dynamically and validate
    tv_configs = await get_room_tv_configs()
    if entity_id not in tv_configs:
        raise HTTPException(status_code=404, detail=f"Unknown Apple TV: {entity_id}")

    # Get remote entity ID from config
    remote_id = tv_configs[entity_id]["remote"]

    valid_commands = ["up", "down", "left", "right", "select", "menu", "home", "play", "pause"]
    if request.command not in valid_commands:
        raise HTTPException(status_code=400, detail=f"Invalid command. Valid: {valid_commands}")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/remote/send_command",
                headers=await get_ha_headers(),
                json={
                    "entity_id": remote_id,
                    "command": request.command
                }
            )

            if response.status_code not in [200, 201]:
                logger.error("appletv_remote_error", remote_id=remote_id, command=request.command)
                raise HTTPException(status_code=502, detail="Remote command failed")

            logger.info("appletv_remote_command", remote_id=remote_id, command=request.command)
            return {
                "success": True,
                "command": request.command
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


@app.post("/api/appletv/{entity_id}/power/{action}")
async def power_control(entity_id: str, action: str):
    """Turn Apple TV on or off"""
    if not HA_TOKEN:
        raise HTTPException(status_code=503, detail="Home Assistant not configured")

    if action not in ["on", "off"]:
        raise HTTPException(status_code=400, detail="Action must be 'on' or 'off'")

    # Ensure entity_id has proper prefix
    if not entity_id.startswith("media_player."):
        entity_id = f"media_player.{entity_id}"

    # Fetch TV configs dynamically and validate
    tv_configs = await get_room_tv_configs()
    if entity_id not in tv_configs:
        raise HTTPException(status_code=404, detail=f"Unknown Apple TV: {entity_id}")

    service = "turn_on" if action == "on" else "turn_off"

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{HA_URL}/api/services/media_player/{service}",
                headers=await get_ha_headers(),
                json={"entity_id": entity_id}
            )

            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=502, detail=f"Failed to turn {action} Apple TV")

            logger.info("appletv_power", entity_id=entity_id, action=action)
            return {
                "success": True,
                "action": action
            }

    except httpx.RequestError as e:
        logger.error("ha_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Unable to connect to Home Assistant")


# =============================================================================
# Voice Services (STT/TTS proxy)
# =============================================================================

class TTSRequest(BaseModel):
    """Text-to-speech request"""
    text: str


@app.post("/api/voice/transcribe")
async def transcribe_audio(request: Request):
    """
    Transcribe audio to text via Voice REST API.

    Expects multipart form data with 'audio' file.
    Returns JSON with transcribed text.
    """
    import subprocess
    import tempfile
    import json as json_module
    import time

    start_time = time.time()

    try:
        form = await request.form()
        audio_file = form.get("audio")

        if not audio_file:
            raise HTTPException(status_code=400, detail="No audio file provided")

        # Read the audio content
        audio_content = await audio_file.read()
        logger.info("audio_received",
                   size_bytes=len(audio_content),
                   filename=getattr(audio_file, 'filename', 'unknown'))

        # Save webm and convert to wav for better STT compatibility
        import os
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp_webm:
            tmp_webm.write(audio_content)
            webm_path = tmp_webm.name

        wav_path = webm_path.replace(".webm", ".wav")

        try:
            # Convert webm to wav using ffmpeg
            convert_result = subprocess.run(
                ["ffmpeg", "-y", "-i", webm_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
                capture_output=True,
                timeout=10
            )
            if convert_result.returncode != 0:
                logger.error("ffmpeg_conversion_failed",
                           stderr=convert_result.stderr.decode()[:200])
                # Fall back to original webm
                wav_path = webm_path
                audio_type = "audio/webm"
            else:
                audio_type = "audio/wav"
                logger.info("audio_converted", format="wav")

            result = subprocess.run(
                ["curl", "-s", "-m", "30", "-X", "POST",
                 "-F", f"audio=@{wav_path};type={audio_type}",
                 f"{VOICE_API_URL}/stt/transcribe"],
                capture_output=True,
                text=True,
                timeout=35
            )

            if result.returncode != 0 or not result.stdout:
                logger.error("stt_error", returncode=result.returncode, stderr=result.stderr)
                raise HTTPException(
                    status_code=502,
                    detail="Speech transcription failed"
                )

            response_data = json_module.loads(result.stdout)
            elapsed_ms = (time.time() - start_time) * 1000
            logger.info("stt_success",
                       text_length=len(response_data.get("text", "")),
                       duration_ms=round(elapsed_ms, 1))
            # Add timing to response
            response_data["timing_ms"] = round(elapsed_ms, 1)
            return response_data

        finally:
            # Clean up temp files
            try:
                os.unlink(webm_path)
            except:
                pass
            try:
                if wav_path != webm_path:
                    os.unlink(wav_path)
            except:
                pass

    except subprocess.TimeoutExpired:
        logger.error("stt_timeout")
        raise HTTPException(status_code=504, detail="Speech transcription timed out")
    except Exception as e:
        logger.error("stt_error", error=str(e))
        raise HTTPException(status_code=503, detail="Voice service unavailable")


@app.post("/api/voice/synthesize")
async def synthesize_speech(request: TTSRequest):
    """
    Synthesize speech from text via Voice REST API.

    Returns WAV audio file.
    """
    import subprocess
    import json as json_module
    import time

    start_time = time.time()

    try:
        # Use curl as workaround for httpx connectivity issues on macOS
        result = subprocess.run(
            ["curl", "-s", "-m", "30", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", json_module.dumps({"text": request.text}),
             f"{VOICE_API_URL}/tts/synthesize"],
            capture_output=True,
            timeout=35
        )

        if result.returncode != 0 or not result.stdout:
            logger.error("tts_error", returncode=result.returncode, stderr=result.stderr.decode() if result.stderr else "")
            raise HTTPException(
                status_code=502,
                detail="Speech synthesis failed"
            )

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info("tts_success",
                   text_length=len(request.text),
                   duration_ms=round(elapsed_ms, 1))

        # Return audio as streaming response with timing header
        return StreamingResponse(
            iter([result.stdout]),
            media_type="audio/wav",
            headers={
                "Content-Disposition": "attachment; filename=response.wav",
                "X-TTS-Duration-Ms": str(round(elapsed_ms, 1))
            }
        )

    except subprocess.TimeoutExpired:
        logger.error("tts_timeout")
        raise HTTPException(status_code=504, detail="Speech synthesis timed out")
    except Exception as e:
        logger.error("tts_connection_error", error=str(e))
        raise HTTPException(status_code=503, detail="Voice service unavailable")


@app.get("/api/voice/health")
async def voice_health():
    """Check health of voice services (STT/TTS)"""
    import subprocess
    import json as json_module

    health_status = {
        "stt": {"status": "unknown"},
        "tts": {"status": "unknown"}
    }

    try:
        # Use curl as workaround for httpx connectivity issues on macOS
        result = subprocess.run(
            ["curl", "-s", "-m", "5", f"{VOICE_API_URL}/health"],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout:
            data = json_module.loads(result.stdout)
            # Voice API returns "whisper" for STT and "piper" for TTS
            whisper = data.get("whisper", {})
            piper = data.get("piper", {})
            health_status["stt"]["status"] = "healthy" if whisper.get("status") == "healthy" else "unhealthy"
            health_status["tts"]["status"] = "healthy" if piper.get("status") == "healthy" else "unhealthy"
        else:
            health_status["stt"]["status"] = "unhealthy"
            health_status["tts"]["status"] = "unhealthy"
    except Exception as e:
        logger.error("voice_health_check_error", error=str(e))
        health_status["stt"]["status"] = "unavailable"
        health_status["tts"]["status"] = "unavailable"

    return health_status


# =============================================================================
# LiveKit Proxy Routes (proxy to Gateway for WebRTC voice streaming)
# =============================================================================

@app.get("/livekit/config")
async def livekit_config():
    """Proxy LiveKit config from Gateway."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{GATEWAY_URL}/livekit/config")
            return response.json()
    except Exception as e:
        logger.error("livekit_config_proxy_error", error=str(e))
        return {"enabled": False, "error": str(e)}


@app.post("/livekit/rooms")
async def livekit_create_room(request: Request):
    """Proxy room creation to Gateway."""
    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GATEWAY_URL}/livekit/rooms",
                json=body
            )
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            return response.json()
    except httpx.RequestError as e:
        logger.error("livekit_room_create_error", error=str(e))
        raise HTTPException(status_code=503, detail=f"Gateway unavailable: {e}")


@app.post("/livekit/rooms/{room_name}/athena-join")
async def livekit_athena_join(room_name: str):
    """Proxy Athena join request to Gateway."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{GATEWAY_URL}/livekit/rooms/{room_name}/athena-join")
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            return response.json()
    except httpx.RequestError as e:
        logger.error("livekit_athena_join_error", error=str(e))
        raise HTTPException(status_code=503, detail=f"Gateway unavailable: {e}")


@app.delete("/livekit/rooms/{room_name}")
async def livekit_delete_room(room_name: str):
    """Proxy room deletion to Gateway."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(f"{GATEWAY_URL}/livekit/rooms/{room_name}")
            return {"status": "ok"}
    except httpx.RequestError as e:
        logger.error("livekit_room_delete_error", error=str(e))
        return {"status": "error", "message": str(e)}


# =============================================================================
# Music Assistant Proxy Routes (for browser playback)
# =============================================================================

@app.get("/api/music/config")
async def get_music_config(request: Request):
    """
    Proxy music config request to Gateway.

    The Gateway returns proxy URLs based on request origin, which allows
    the frontend to connect to Music Assistant regardless of network location.
    """
    try:
        # Build the request URL to Gateway, preserving the scheme
        # so Gateway can return appropriate ws/wss URLs
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{GATEWAY_URL}/api/music/config",
                headers={
                    # Forward original host so Gateway knows the public URL
                    "X-Forwarded-Host": request.headers.get("host", ""),
                    "X-Forwarded-Proto": request.headers.get("x-forwarded-proto", request.url.scheme),
                }
            )

            if response.status_code == 200:
                config = response.json()

                # Override proxy URLs to use Jarvis Web's origin (request origin)
                # This ensures the browser connects to Jarvis Web which proxies to Gateway
                scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                host = request.headers.get("host", request.url.netloc)
                ws_scheme = "wss" if scheme == "https" else "ws"

                config["proxy_ws_url"] = f"{ws_scheme}://{host}/ma/ws"
                config["proxy_stream_url"] = f"{scheme}://{host}/api/music/stream"

                logger.debug("music_config_proxied", host=host, scheme=scheme)
                return config
            else:
                return {"enabled": False, "error": "Gateway returned error"}

    except httpx.RequestError as e:
        logger.warning("music_config_proxy_error", error=str(e))
        return {"enabled": False, "error": str(e)}


@app.get("/api/music/stream/{uri:path}")
async def proxy_music_stream(uri: str, request: Request):
    """
    Proxy music stream from Gateway to browser.

    This allows streaming audio when the browser can't directly reach
    Music Assistant (e.g., when accessing remotely).
    """
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            # Stream from Gateway's stream proxy
            async with client.stream("GET", f"{GATEWAY_URL}/api/music/stream/{uri}") as response:
                headers = {
                    "Content-Type": response.headers.get("Content-Type", "audio/mpeg"),
                    "Accept-Ranges": "bytes"
                }

                if "Content-Length" in response.headers:
                    headers["Content-Length"] = response.headers["Content-Length"]

                async def stream_generator():
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        yield chunk

                return StreamingResponse(
                    stream_generator(),
                    media_type=headers["Content-Type"],
                    headers=headers
                )

    except httpx.RequestError as e:
        logger.error("music_stream_proxy_error", error=str(e))
        raise HTTPException(status_code=502, detail="Failed to proxy music stream")


class MusicSearchRequest(BaseModel):
    query: str
    media_types: List[str] = ["track", "artist"]
    limit: int = 25


class MusicPlayRequest(BaseModel):
    player_id: str
    uri: str
    radio_mode: bool = True


@app.post("/api/music/play")
async def music_play(request: MusicPlayRequest):
    """
    Play media to a specific Music Assistant player.

    This proxies to the Gateway's /api/music/play endpoint which
    triggers MA to stream audio to a Sendspin-registered player.
    """
    try:
        logger.info("music_play_request", player_id=request.player_id, uri=request.uri)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GATEWAY_URL}/api/music/play",
                json={
                    "player_id": request.player_id,
                    "uri": request.uri,
                    "radio_mode": request.radio_mode
                }
            )

            if response.status_code == 200:
                result = response.json()
                logger.info("music_play_success", player_id=request.player_id)
                return result
            else:
                error_detail = response.text
                logger.error("music_play_gateway_error", status=response.status_code, error=error_detail)
                raise HTTPException(status_code=response.status_code, detail=error_detail)

    except httpx.TimeoutException:
        logger.error("music_play_timeout")
        raise HTTPException(status_code=504, detail="Play request timeout")
    except httpx.RequestError as e:
        logger.error("music_play_error", error=str(e))
        raise HTTPException(status_code=502, detail="Play request failed")


@app.post("/api/music/search")
async def music_search(request: MusicSearchRequest):
    """
    Search Music Assistant for tracks/artists.

    This is a direct HTTP endpoint that avoids WebSocket reliability issues.
    It calls the Gateway's MA search API.
    """
    try:
        logger.info("music_search_request", query=request.query, types=request.media_types)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GATEWAY_URL}/api/music/search",
                json={
                    "query": request.query,
                    "media_types": request.media_types,
                    "limit": request.limit
                }
            )

            if response.status_code == 200:
                results = response.json()
                logger.info("music_search_success", tracks=len(results.get("tracks", [])))
                return results
            else:
                logger.error("music_search_gateway_error", status=response.status_code)
                raise HTTPException(status_code=response.status_code, detail="Gateway search failed")

    except httpx.TimeoutException:
        logger.error("music_search_timeout")
        raise HTTPException(status_code=504, detail="Search timeout")
    except httpx.RequestError as e:
        logger.error("music_search_error", error=str(e))
        raise HTTPException(status_code=502, detail="Search failed")


# WebSocket proxy for Music Assistant
# Only enable if websockets library is available
try:
    import websockets
    from starlette.websockets import WebSocket
    from starlette.websockets import WebSocketDisconnect
    MUSIC_WS_AVAILABLE = True
except ImportError:
    MUSIC_WS_AVAILABLE = False
    logger.warning("WebSocket dependencies not available for Music Assistant proxy")

if MUSIC_WS_AVAILABLE:
    @app.websocket("/ma/ws")
    async def music_assistant_websocket_proxy(websocket: WebSocket):
        """
        WebSocket proxy for Music Assistant.

        Proxies WebSocket connections from browser through Jarvis Web
        to the Gateway, which in turn connects to Music Assistant.
        """
        await websocket.accept()
        logger.info("MA WebSocket proxy: Client connected via Jarvis Web")

        # Connect to Gateway's MA WebSocket proxy
        gateway_ws_url = GATEWAY_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ma/ws"

        gateway_ws = None
        try:
            gateway_ws = await websockets.connect(
                gateway_ws_url,
                ping_interval=20,
                ping_timeout=20
            )
            logger.info(f"MA WebSocket proxy: Connected to Gateway at {gateway_ws_url}")

            async def forward_to_client():
                """Forward messages from Gateway to browser client."""
                try:
                    async for message in gateway_ws:
                        await websocket.send_text(message)
                except Exception as e:
                    logger.debug(f"Gateway->Client forward ended: {e}")

            async def forward_to_gateway():
                """Forward messages from browser client to Gateway."""
                try:
                    while True:
                        message = await websocket.receive_text()
                        await gateway_ws.send(message)
                except WebSocketDisconnect:
                    logger.debug("Client disconnected")
                except Exception as e:
                    logger.debug(f"Client->Gateway forward ended: {e}")

            # Run both directions concurrently
            await asyncio.gather(
                forward_to_client(),
                forward_to_gateway(),
                return_exceptions=True
            )

        except Exception as e:
            logger.error(f"MA WebSocket proxy error: {e}")
            await websocket.close(code=1011, reason=str(e))
        finally:
            if gateway_ws:
                await gateway_ws.close()
            logger.info("MA WebSocket proxy: Connection closed")

    @app.websocket("/ma/sendspin")
    async def sendspin_websocket_proxy(websocket: WebSocket):
        """
        WebSocket proxy for Sendspin audio streaming.

        Proxies WebSocket connections from browser through Jarvis Web
        to the Gateway, which connects to Music Assistant's Sendspin endpoint.
        Handles both text (JSON control) and binary (audio) messages.
        """
        await websocket.accept()
        logger.info("Sendspin proxy: Client connected via Jarvis Web")

        # Connect to Gateway's Sendspin proxy
        gateway_ws_url = GATEWAY_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ma/sendspin"

        # Pass through query params (player_id, etc.)
        query_string = websocket.scope.get("query_string", b"").decode()
        if query_string:
            gateway_ws_url += "?" + query_string

        gateway_ws = None
        try:
            gateway_ws = await websockets.connect(
                gateway_ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=None  # Allow large binary messages
            )
            logger.info(f"Sendspin proxy: Connected to Gateway at {gateway_ws_url}")

            async def forward_to_client():
                """Forward messages from Gateway to browser client."""
                try:
                    async for message in gateway_ws:
                        if isinstance(message, bytes):
                            logger.debug(f"Sendspin Gateway->Browser: binary {len(message)} bytes")
                            await websocket.send_bytes(message)
                        else:
                            # Log JSON messages for debugging
                            msg_preview = message[:150] if len(message) > 150 else message
                            logger.info(f"Sendspin Gateway->Browser: {msg_preview}")
                            await websocket.send_text(message)
                except Exception as e:
                    logger.debug(f"Sendspin Gateway->Client forward ended: {e}")

            async def forward_to_gateway():
                """Forward messages from browser client to Gateway."""
                try:
                    while True:
                        message = await websocket.receive()
                        if "bytes" in message:
                            logger.debug(f"Sendspin Browser->Gateway: binary {len(message['bytes'])} bytes")
                            await gateway_ws.send(message["bytes"])
                        elif "text" in message:
                            # Log JSON messages for debugging
                            msg_preview = message["text"][:150] if len(message["text"]) > 150 else message["text"]
                            logger.info(f"Sendspin Browser->Gateway: {msg_preview}")
                            await gateway_ws.send(message["text"])
                        else:
                            logger.info("Sendspin: Received non-text/bytes message, breaking")
                            break
                except WebSocketDisconnect:
                    logger.info("Sendspin: Client disconnected")
                except Exception as e:
                    logger.info(f"Sendspin Client->Gateway forward ended: {e}")

            # Run both directions concurrently
            await asyncio.gather(
                forward_to_client(),
                forward_to_gateway(),
                return_exceptions=True
            )

        except Exception as e:
            logger.error(f"Sendspin proxy error: {e}")
            await websocket.close(code=1011, reason=str(e))
        finally:
            if gateway_ws:
                await gateway_ws.close()
            logger.info("Sendspin proxy: Connection closed")


# Serve static files in production
static_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
logos_path = os.path.join(static_path, "logos")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")
    # Serve logos at /logos for streaming app icons
    if os.path.exists(logos_path):
        app.mount("/logos", StaticFiles(directory=logos_path), name="logos")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(static_path, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
