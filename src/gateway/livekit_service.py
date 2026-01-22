"""
LiveKit WebRTC Audio Streaming Service for Project Athena.

Provides real-time two-way audio streaming via WebRTC:
- Browser clients connect via LiveKit
- Server receives audio stream
- Wake word detection on streamed audio
- Low-latency STT/TTS pipeline
- Interruption handling

Architecture:
    Browser → LiveKit Server → Athena Gateway → Orchestrator
                    ↓
            Audio Stream Processing
                    ↓
        Wake Word → STT → LLM → TTS → Audio Out
"""

import os
import time
import asyncio
import hashlib
import random
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from enum import Enum
from datetime import timedelta
import struct
import io

import structlog
import httpx
import numpy as np

# LiveKit imports
try:
    from livekit import api, rtc
    from livekit.api import AccessToken, VideoGrants
    LIVEKIT_AVAILABLE = True
except ImportError:
    LIVEKIT_AVAILABLE = False
    api = None
    rtc = None

logger = structlog.get_logger()

# Configuration - defaults, will be overridden by admin API
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# Admin API for fetching credentials
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:8080")


async def fetch_livekit_credentials() -> Dict[str, str]:
    """
    Fetch LiveKit credentials from the Athena Admin API.
    Returns dict with api_key, api_secret, and endpoint_url.
    Falls back to environment variables if admin API fails.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/external-api-keys/public/livekit/credentials"
            )
            if response.status_code == 200:
                data = response.json()
                logger.info("livekit_credentials_fetched",
                           url=data.get("endpoint_url"),
                           has_key=bool(data.get("api_key")),
                           has_secret=bool(data.get("api_secret")))
                return {
                    "api_key": data.get("api_key", ""),
                    "api_secret": data.get("api_secret", ""),
                    "endpoint_url": data.get("endpoint_url", "")
                }
            else:
                logger.warning("livekit_credentials_fetch_failed",
                              status=response.status_code,
                              detail=response.text[:100])
    except Exception as e:
        logger.warning("livekit_credentials_fetch_error", error=str(e))

    # Fallback to environment variables
    return {
        "api_key": LIVEKIT_API_KEY,
        "api_secret": LIVEKIT_API_SECRET,
        "endpoint_url": LIVEKIT_URL
    }

# Audio settings
SAMPLE_RATE = 16000  # 16kHz for speech
CHANNELS = 1  # Mono
CHUNK_DURATION_MS = 100  # 100ms audio chunks

# AI-initiated follow-up settings (Phase 2)
FOLLOW_UP_SILENCE_THRESHOLD_MS = 3000  # 3 seconds of silence before follow-up
FOLLOW_UP_PHRASES = [
    "Is there anything else?",
    "Anything else I can help with?",
    "Need anything else?",
]
MAX_FOLLOW_UPS_PER_SESSION = 2  # Don't be annoying


class SessionState(Enum):
    """LiveKit session states."""
    IDLE = "idle"                    # Waiting for wake word
    LISTENING = "listening"          # After wake word, capturing query
    PROCESSING = "processing"        # Query sent to orchestrator
    RESPONDING = "responding"        # Playing TTS response
    INTERRUPTED = "interrupted"      # User interrupted response


@dataclass
class InterruptionContext:
    """Stores context when user interrupts."""
    interrupted_response: str = ""  # The response that was interrupted
    interruption_point: float = 0.0  # When interruption occurred
    audio_position_ms: int = 0  # How far into TTS playback
    previous_query: str = ""  # The query that generated interrupted response


@dataclass
class LiveKitSession:
    """Represents an active LiveKit audio session."""
    session_id: str
    room_name: str
    participant_id: str
    state: SessionState = SessionState.IDLE

    # Audio buffers
    audio_buffer: bytes = b""
    wake_word_buffer: bytes = b""

    # Timing
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    wake_word_detected_at: Optional[float] = None

    # Context
    interface: str = "livekit"
    room: str = "unknown"
    user_id: Optional[str] = None
    last_query: Optional[str] = None  # Store last query for interruption context

    # Interruption handling
    interruption_context: Optional[InterruptionContext] = None
    tts_playback_active: bool = False
    tts_audio_position_ms: int = 0

    # AI-initiated follow-ups (Phase 2)
    last_response_time: float = 0.0  # When last TTS response completed
    follow_up_count: int = 0  # Number of follow-ups asked this session

    # Callbacks
    on_transcription: Optional[Callable] = None
    on_response: Optional[Callable] = None


class LiveKitService:
    """
    Manages LiveKit WebRTC connections for voice streaming.

    Handles:
    - Room creation and participant token generation
    - Audio stream processing
    - Wake word detection on audio stream
    - Integration with STT/TTS pipeline
    - Session state management
    """

    def __init__(
        self,
        livekit_url: str = LIVEKIT_URL,
        api_key: str = LIVEKIT_API_KEY,
        api_secret: str = LIVEKIT_API_SECRET,
        wake_word_detector: Optional[Any] = None,
        stt_client: Optional[Any] = None,
        tts_client: Optional[Any] = None
    ):
        self.livekit_url = livekit_url
        self.api_key = api_key
        self.api_secret = api_secret
        self._credentials_loaded = False

        # External clients
        self.wake_word_detector = wake_word_detector
        self.stt_client = stt_client
        self.tts_client = tts_client

        # Active sessions
        self._sessions: Dict[str, LiveKitSession] = {}

        # Room connections (for server-side audio handling)
        self._rooms: Dict[str, Any] = {}

        # Callbacks for events
        self._on_query_ready: Optional[Callable] = None
        self._on_session_start: Optional[Callable] = None
        self._on_session_end: Optional[Callable] = None

        # Settings
        self.silence_timeout_ms = 2000  # End listening after 2s silence
        self.max_query_duration_ms = 30000  # Max 30s query
        self.wake_word_buffer_ms = 3000  # Keep 3s of audio for wake word detection

        # Feature flags (loaded from admin API)
        self._follow_ups_enabled = False  # AI-initiated follow-ups (Phase 2)
        self._last_feature_flag_check = 0.0
        self._feature_flag_check_interval = 60.0  # Check every 60 seconds

        if not LIVEKIT_AVAILABLE:
            logger.warning("livekit_not_installed",
                          message="pip install livekit livekit-api required")

    async def load_credentials(self) -> bool:
        """
        Load LiveKit credentials from admin API.
        Called during service initialization.
        """
        if self._credentials_loaded:
            return self.is_available

        credentials = await fetch_livekit_credentials()
        if credentials.get("api_key") and credentials.get("api_secret"):
            self.api_key = credentials["api_key"]
            self.api_secret = credentials["api_secret"]
            if credentials.get("endpoint_url"):
                self.livekit_url = credentials["endpoint_url"]
            self._credentials_loaded = True
            logger.info("livekit_credentials_loaded",
                       url=self.livekit_url,
                       available=self.is_available)
            return True
        else:
            logger.warning("livekit_credentials_missing",
                          has_key=bool(credentials.get("api_key")),
                          has_secret=bool(credentials.get("api_secret")))
            return False

    @property
    def is_available(self) -> bool:
        """Check if LiveKit is properly configured."""
        return (
            LIVEKIT_AVAILABLE and
            bool(self.api_key) and
            bool(self.api_secret)
        )

    async def _refresh_feature_flags(self):
        """
        Refresh feature flags from admin API periodically.
        Called during audio processing to avoid blocking startup.
        """
        now = time.time()
        if now - self._last_feature_flag_check < self._feature_flag_check_interval:
            return

        self._last_feature_flag_check = now

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                # Use the public features list endpoint and filter for our flag
                response = await client.get(
                    f"{ADMIN_API_URL}/api/features/public"
                )
                if response.status_code == 200:
                    features = response.json()
                    # Find our specific feature flag
                    for feature in features:
                        if feature.get("name") == "ai_follow_ups_enabled":
                            new_value = feature.get("enabled", False)
                            if new_value != self._follow_ups_enabled:
                                logger.info("feature_flag_updated",
                                           flag="ai_follow_ups_enabled",
                                           old_value=self._follow_ups_enabled,
                                           new_value=new_value)
                            self._follow_ups_enabled = new_value
                            return
                    # Feature not found in list - keep default (disabled)
                    logger.debug("feature_flag_not_found", flag="ai_follow_ups_enabled")
                else:
                    logger.debug("feature_flag_check_failed",
                                status=response.status_code)
        except Exception as e:
            logger.debug("feature_flag_check_error", error=str(e))
            # Keep existing value on error

    def generate_room_token(
        self,
        room_name: str,
        participant_name: str,
        participant_identity: Optional[str] = None,
        duration_hours: int = 24
    ) -> str:
        """
        Generate a LiveKit access token for a participant.

        Args:
            room_name: Name of the room to join
            participant_name: Display name for the participant
            participant_identity: Unique identifier (defaults to generated)
            duration_hours: Token validity duration

        Returns:
            JWT token string for LiveKit connection
        """
        if not self.is_available:
            raise RuntimeError("LiveKit not configured")

        identity = participant_identity or f"user_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"

        # Grant permissions
        grants = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True
        )

        # Build token using new SDK builder pattern
        token = (
            AccessToken(self.api_key, self.api_secret)
            .with_identity(identity)
            .with_name(participant_name)
            .with_ttl(timedelta(hours=duration_hours))
            .with_grants(grants)
        )

        return token.to_jwt()

    async def create_room(
        self,
        room_name: Optional[str] = None,
        empty_timeout: int = 300,  # 5 minutes
        max_participants: int = 2  # User + Athena
    ) -> Dict[str, Any]:
        """
        Create a new LiveKit room for audio session.

        Args:
            room_name: Optional room name (auto-generated if not provided)
            empty_timeout: Seconds before empty room is destroyed
            max_participants: Maximum participants allowed

        Returns:
            Room info including name, token, and connection URL
        """
        if not self.is_available:
            raise RuntimeError("LiveKit not configured")

        # Generate room name if not provided
        if not room_name:
            room_name = f"athena_{hashlib.md5(str(time.time()).encode()).hexdigest()[:12]}"

        try:
            # Create room via LiveKit API (new SDK format)
            lkapi = api.LiveKitAPI(
                self.livekit_url.replace("wss://", "https://"),
                self.api_key,
                self.api_secret
            )

            try:
                room = await lkapi.room.create_room(
                    api.CreateRoomRequest(
                        name=room_name,
                        empty_timeout=empty_timeout,
                        max_participants=max_participants
                    )
                )
            finally:
                await lkapi.aclose()

            # Generate token for user
            user_token = self.generate_room_token(
                room_name=room_name,
                participant_name="User",
                participant_identity=f"user_{room_name}"
            )

            # Generate token for Athena (server-side participant)
            athena_token = self.generate_room_token(
                room_name=room_name,
                participant_name="Athena",
                participant_identity=f"athena_{room_name}"
            )

            logger.info("livekit_room_created", room_name=room_name)

            return {
                "room_name": room_name,
                "livekit_url": self.livekit_url,
                "user_token": user_token,
                "athena_token": athena_token,
                "created_at": time.time()
            }

        except Exception as e:
            logger.error("livekit_room_creation_failed", error=str(e))
            raise

    async def join_room_as_athena(self, room_name: str, athena_token: str) -> bool:
        """
        Join a room as the Athena participant to receive/send audio.

        This allows server-side processing of the user's audio stream.
        """
        if not LIVEKIT_AVAILABLE:
            return False

        try:
            room = rtc.Room()

            # Set up audio track handlers (sync callbacks that spawn async tasks)
            def on_track_subscribed(track, publication, participant):
                if isinstance(track, rtc.AudioTrack):
                    logger.info("audio_track_subscribed",
                               participant=participant.identity,
                               room=room_name)
                    # Start processing audio in background task
                    asyncio.create_task(
                        self._process_audio_track(room_name, track, participant.identity)
                    )

            def on_participant_left(participant):
                logger.info("participant_left",
                           participant=participant.identity,
                           room=room_name)
                asyncio.create_task(self._cleanup_session(room_name))

            room.on("track_subscribed")(on_track_subscribed)
            room.on("participant_disconnected")(on_participant_left)

            # Connect to room
            await room.connect(self.livekit_url, athena_token)
            self._rooms[room_name] = room

            logger.info("athena_joined_room", room=room_name)
            return True

        except Exception as e:
            logger.error("athena_join_failed", room=room_name, error=str(e))
            return False

    async def _process_audio_track(
        self,
        room_name: str,
        track: Any,  # rtc.AudioTrack
        participant_id: str
    ):
        """
        Process incoming audio from a participant.

        Handles:
        - Buffering audio for wake word detection
        - Detecting wake word in audio stream
        - Capturing query audio after wake word
        - Sending to STT when query complete
        """
        session_id = f"lk_{room_name}_{int(time.time())}"

        session = LiveKitSession(
            session_id=session_id,
            room_name=room_name,
            participant_id=participant_id
        )
        self._sessions[session_id] = session

        logger.info("audio_processing_started",
                   session_id=session_id,
                   room=room_name)

        # Create audio stream from track
        # Configure audio stream for 16kHz mono (optimal for Whisper STT)
        audio_stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)

        silence_start = None

        async for frame_event in audio_stream:
            # Extract raw PCM bytes from AudioFrameEvent -> AudioFrame -> data (memoryview)
            audio_data = bytes(frame_event.frame.data.cast("B"))

            if session.state == SessionState.IDLE:
                # Periodically refresh feature flags
                await self._refresh_feature_flags()

                # Check for AI-initiated follow-up (Phase 2)
                # This will ask "Is there anything else?" after silence
                if await self._check_for_follow_up(session):
                    # Follow-up was triggered, now listening for response
                    continue

                # If no wake word detector, start listening immediately when speech detected
                if not self.wake_word_detector:
                    # Check if user started speaking (not silence)
                    if not self._is_silence(audio_data):
                        logger.info("speech_detected_starting_listen",
                                   session_id=session_id,
                                   mode="no_wake_word")
                        session.state = SessionState.LISTENING
                        session.wake_word_detected_at = time.time()
                        session.audio_buffer = audio_data  # Include this frame

                        # Notify listeners
                        if self._on_session_start:
                            await self._on_session_start(session)
                else:
                    # Accumulate audio for wake word detection
                    session.wake_word_buffer += audio_data

                    # Keep only last N seconds
                    max_buffer_size = int(SAMPLE_RATE * CHANNELS * 2 *
                                         (self.wake_word_buffer_ms / 1000))
                    if len(session.wake_word_buffer) > max_buffer_size:
                        session.wake_word_buffer = session.wake_word_buffer[-max_buffer_size:]

                    # Check for wake word
                    if await self._detect_wake_word(session.wake_word_buffer):
                        logger.info("wake_word_detected", session_id=session_id)
                        session.state = SessionState.LISTENING
                        session.wake_word_detected_at = time.time()
                        session.audio_buffer = b""

                        # Notify listeners
                        if self._on_session_start:
                            await self._on_session_start(session)

            elif session.state == SessionState.LISTENING:
                # Capture query audio
                session.audio_buffer += audio_data
                session.last_activity = time.time()

                # Check for silence (voice activity detection)
                if self._is_silence(audio_data):
                    if silence_start is None:
                        silence_start = time.time()
                    elif (time.time() - silence_start) * 1000 > self.silence_timeout_ms:
                        # End of query - send to STT
                        session.state = SessionState.PROCESSING
                        logger.info("query_complete",
                                   session_id=session_id,
                                   duration_ms=int((time.time() - session.wake_word_detected_at) * 1000))

                        # Process the captured audio
                        asyncio.create_task(
                            self._process_query(session)
                        )
                else:
                    silence_start = None

                # Check for max duration
                if session.wake_word_detected_at:
                    duration = (time.time() - session.wake_word_detected_at) * 1000
                    if duration > self.max_query_duration_ms:
                        session.state = SessionState.PROCESSING
                        asyncio.create_task(self._process_query(session))

            elif session.state == SessionState.RESPONDING:
                # Check for interruption (user speaking during response)
                if not self._is_silence(audio_data):
                    # Detect immediate stop commands
                    is_stop_command = await self._detect_stop_command(audio_data)

                    logger.info("user_interrupt_detected",
                               session_id=session_id,
                               is_stop_command=is_stop_command)

                    session.state = SessionState.INTERRUPTED
                    if session.interruption_context:
                        session.interruption_context.interruption_point = time.time()

                    # Stop TTS playback immediately
                    await self._stop_tts_playback(session)

                    if is_stop_command:
                        # Don't process as new query, just acknowledge
                        session.state = SessionState.IDLE
                        # Optionally play brief acknowledgment
                        await self._play_brief_ack(session, "okay")
                    else:
                        # Go back to listening for the new query
                        session.state = SessionState.LISTENING
                        session.audio_buffer = audio_data  # Include this frame
                        session.wake_word_detected_at = time.time()

    async def _detect_wake_word(self, audio_buffer: bytes) -> bool:
        """
        Detect wake word in audio buffer.

        Uses OpenWakeWord or similar for detection.
        """
        if not self.wake_word_detector:
            # No detector configured - use simple trigger for testing
            return False

        try:
            # Convert bytes to numpy array for processing
            import numpy as np
            audio_array = np.frombuffer(audio_buffer, dtype=np.int16)

            # Run wake word detection
            predictions = self.wake_word_detector.predict(audio_array)

            # Check for "jarvis" or "athena" wake words
            for wake_word, confidence in predictions.items():
                if confidence > 0.5:  # Threshold
                    logger.debug("wake_word_candidate",
                                word=wake_word,
                                confidence=confidence)
                    return True

            return False

        except Exception as e:
            logger.warning("wake_word_detection_error", error=str(e))
            return False

    def _is_silence(self, audio_data: bytes, threshold: int = 2000) -> bool:
        """
        Check if audio frame is silence (below threshold).

        Simple energy-based VAD.
        """
        try:
            import numpy as np
            samples = np.frombuffer(audio_data, dtype=np.int16)
            energy = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
            return energy < threshold
        except:
            return True

    async def _process_query(self, session: LiveKitSession):
        """
        Process captured audio query through STT and orchestrator.
        """
        try:
            # Transcribe audio
            if self.stt_client:
                transcript = await self.stt_client.transcribe(session.audio_buffer)
            else:
                # Fallback for testing
                transcript = "[Audio captured but STT not configured]"

            # Store query for interruption context
            session.last_query = transcript

            logger.info("query_transcribed",
                       session_id=session.session_id,
                       transcript=transcript[:50])

            # Notify that query is ready
            if self._on_query_ready:
                response = await self._on_query_ready(
                    session_id=session.session_id,
                    transcript=transcript,
                    interface="livekit",
                    room=session.room,
                    interruption_context=self._get_interruption_context_dict(session)
                )

                # Clear interruption context after use
                session.interruption_context = None

                # Play TTS response
                if response:
                    session.state = SessionState.RESPONDING
                    await self._play_tts_response(session, response)

            # Return to idle state
            session.state = SessionState.IDLE
            session.audio_buffer = b""

        except Exception as e:
            logger.error("query_processing_error",
                        session_id=session.session_id,
                        error=str(e))
            session.state = SessionState.IDLE

    def _get_interruption_context_dict(self, session: LiveKitSession) -> Optional[Dict[str, Any]]:
        """Convert InterruptionContext to dict for API calls."""
        if not session.interruption_context:
            return None
        ctx = session.interruption_context
        return {
            "previous_query": ctx.previous_query,
            "interrupted_response": ctx.interrupted_response,
            "audio_position_ms": ctx.audio_position_ms,
            "interruption_point": ctx.interruption_point,
        }

    async def _check_for_follow_up(self, session: LiveKitSession) -> bool:
        """
        Check if we should ask a follow-up question after silence.
        Returns True if follow-up was triggered, False otherwise.

        Phase 2: AI-initiated follow-ups inspired by Vocalis.
        """
        # Only check when idle and not during active processing
        if session.state != SessionState.IDLE:
            return False

        # Don't exceed max follow-ups per session (avoid being annoying)
        if session.follow_up_count >= MAX_FOLLOW_UPS_PER_SESSION:
            return False

        # Need a previous response to follow up on
        if session.last_response_time == 0.0:
            return False

        # Check if enough silence has passed since last response
        silence_duration_ms = (time.time() - session.last_response_time) * 1000
        if silence_duration_ms < FOLLOW_UP_SILENCE_THRESHOLD_MS:
            return False

        # Check feature flag (defaults to disabled)
        if not self._follow_ups_enabled:
            return False

        # Ask follow-up question
        phrase = random.choice(FOLLOW_UP_PHRASES)
        session.follow_up_count += 1

        logger.info("ai_initiated_follow_up",
                   session_id=session.session_id,
                   phrase=phrase,
                   follow_up_count=session.follow_up_count,
                   silence_duration_ms=int(silence_duration_ms))

        # Play the follow-up phrase via TTS
        await self._play_tts_response(session, phrase)

        # Return to listening for their response
        session.state = SessionState.LISTENING
        session.wake_word_detected_at = time.time()
        return True

    async def _play_tts_response(self, session: LiveKitSession, text: str):
        """
        Synthesize and play TTS response to the room with interruption support.
        """
        if not self.tts_client:
            logger.warning("tts_not_configured")
            return

        try:
            room = self._rooms.get(session.room_name)
            if not room:
                return

            # Generate TTS audio
            audio_data = await self.tts_client.synthesize(text)

            # Store current response for interruption context
            session.interruption_context = InterruptionContext(
                interrupted_response=text,
                previous_query=session.last_query or ""
            )
            session.tts_playback_active = True
            session.tts_audio_position_ms = 0

            # Create audio source and track
            source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track("athena_response", source)

            # Store track reference for interruption
            room._tts_track = track

            # Publish track to room
            await room.local_participant.publish_track(track)

            # Stream audio frames with position tracking
            chunk_size = int(SAMPLE_RATE * CHANNELS * 2 * (CHUNK_DURATION_MS / 1000))
            chunk_duration_ms = CHUNK_DURATION_MS
            total_chunks = len(audio_data) // chunk_size

            for i in range(0, len(audio_data), chunk_size):
                # Check for interruption BEFORE each chunk
                if session.state == SessionState.INTERRUPTED or not session.tts_playback_active:
                    if session.interruption_context:
                        session.interruption_context.audio_position_ms = session.tts_audio_position_ms
                    logger.info("tts_interrupted",
                               session_id=session.session_id,
                               position_ms=session.tts_audio_position_ms,
                               total_chunks=total_chunks)
                    break

                chunk = audio_data[i:i + chunk_size]
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=SAMPLE_RATE,
                    num_channels=CHANNELS,
                    samples_per_channel=len(chunk) // (CHANNELS * 2)
                )
                await source.capture_frame(frame)
                session.tts_audio_position_ms += chunk_duration_ms

            # Unpublish track
            await room.local_participant.unpublish_track(track)
            if hasattr(room, '_tts_track'):
                delattr(room, '_tts_track')

            session.tts_playback_active = False
            session.last_response_time = time.time()  # Track when response completed for follow-ups

        except Exception as e:
            logger.error("tts_playback_error", error=str(e))
            session.tts_playback_active = False

    async def _stop_tts_playback(self, session: LiveKitSession):
        """Stop current TTS playback for interruption handling."""
        # Mark playback as inactive - the playback loop will stop on next chunk
        session.tts_playback_active = False

        # Try to unpublish the track immediately if we have a reference
        try:
            room = self._rooms.get(session.room_name)
            if room and hasattr(room, '_tts_track'):
                await room.local_participant.unpublish_track(room._tts_track)
                delattr(room, '_tts_track')
                logger.info("tts_track_stopped", session_id=session.session_id)
        except Exception as e:
            logger.debug("tts_stop_cleanup", error=str(e))

    async def _detect_stop_command(self, audio_data: bytes) -> bool:
        """
        Quick detection of stop commands in audio.

        Uses a lightweight local model or keyword spotting to detect
        words like "stop", "cancel", "nevermind" without full STT.
        """
        # For now, use a simple energy + duration heuristic
        # Short bursts of speech are more likely to be stop commands
        # TODO: Implement proper keyword spotting model (e.g., Vosk or OpenWakeWord)

        if len(audio_data) < SAMPLE_RATE * 2 * 0.3:  # Less than 300ms
            # Very short utterance - likely "stop" or similar
            energy = self._calculate_energy(audio_data)
            if energy > 0.1:  # Confident speech
                return True
        return False

    def _calculate_energy(self, audio_data: bytes) -> float:
        """Calculate RMS energy of audio data."""
        try:
            samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return 0.0
            rms = np.sqrt(np.mean(samples ** 2))
            return float(rms / 32768.0)  # Normalize to 0-1 range
        except Exception:
            return 0.0

    async def _play_brief_ack(self, session: LiveKitSession, ack_type: str = "okay"):
        """Play a brief acknowledgment sound."""
        try:
            room = self._rooms.get(session.room_name)
            if not room or not self.tts_client:
                return

            # Short acknowledgment phrases
            ack_phrases = {
                "okay": "Okay.",
                "stopped": "Stopped.",
                "understood": "Got it.",
            }

            text = ack_phrases.get(ack_type, "Okay.")
            audio_data = await self.tts_client.synthesize(text)

            source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track("athena_ack", source)
            await room.local_participant.publish_track(track)

            # Play the short audio without interruption checking
            chunk_size = int(SAMPLE_RATE * CHANNELS * 2 * (CHUNK_DURATION_MS / 1000))
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i + chunk_size]
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=SAMPLE_RATE,
                    num_channels=CHANNELS,
                    samples_per_channel=len(chunk) // (CHANNELS * 2)
                )
                await source.capture_frame(frame)

            await room.local_participant.unpublish_track(track)

        except Exception as e:
            logger.debug("brief_ack_error", error=str(e))

    async def _cleanup_session(self, room_name: str):
        """Clean up session when participant leaves."""
        # Find and remove session
        sessions_to_remove = [
            sid for sid, s in self._sessions.items()
            if s.room_name == room_name
        ]

        for sid in sessions_to_remove:
            session = self._sessions.pop(sid, None)
            if session and self._on_session_end:
                await self._on_session_end(session)

        # Disconnect from room
        room = self._rooms.pop(room_name, None)
        if room:
            await room.disconnect()

        logger.info("session_cleaned_up", room=room_name)

    def set_query_handler(self, handler: Callable):
        """Set callback for when query is ready for processing."""
        self._on_query_ready = handler

    def set_session_handlers(
        self,
        on_start: Optional[Callable] = None,
        on_end: Optional[Callable] = None
    ):
        """Set callbacks for session lifecycle events."""
        self._on_session_start = on_start
        self._on_session_end = on_end

    async def get_active_sessions(self) -> List[Dict[str, Any]]:
        """Get list of active LiveKit sessions."""
        return [
            {
                "session_id": s.session_id,
                "room_name": s.room_name,
                "state": s.state.value,
                "created_at": s.created_at,
                "last_activity": s.last_activity
            }
            for s in self._sessions.values()
        ]

    async def shutdown(self):
        """Clean up all sessions and disconnect from rooms."""
        for room_name in list(self._rooms.keys()):
            await self._cleanup_session(room_name)

        self._sessions.clear()
        self._rooms.clear()
        logger.info("livekit_service_shutdown")


# Singleton instance
_livekit_service: Optional[LiveKitService] = None


def get_livekit_service() -> LiveKitService:
    """Get or create LiveKit service singleton."""
    global _livekit_service
    if _livekit_service is None:
        _livekit_service = LiveKitService()
    return _livekit_service


async def initialize_livekit_service(
    wake_word_detector: Optional[Any] = None,
    stt_client: Optional[Any] = None,
    tts_client: Optional[Any] = None
) -> LiveKitService:
    """Initialize LiveKit service with required clients."""
    global _livekit_service
    _livekit_service = LiveKitService(
        wake_word_detector=wake_word_detector,
        stt_client=stt_client,
        tts_client=tts_client
    )
    # Load credentials from admin API
    await _livekit_service.load_credentials()
    return _livekit_service
