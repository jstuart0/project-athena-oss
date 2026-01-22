"""
Wyoming Protocol Bridge for Project Athena

Routes Wyoming protocol events to Athena's configurable voice pipeline.
Enables Home Assistant voice integration with interruption handling.

Usage:
    python -m gateway.wyoming_bridge --port 10400

Wyoming Protocol:
    - TCP-based protocol for voice assistants
    - Supports ASR (STT), TTS, wake word, and satellite modes
    - Used by Home Assistant for voice integration

Features:
    - Barge-in/interruption detection during TTS playback
    - Session state tracking for continued conversation
    - TTS cancellation on user speech
    - Interruption context passed to orchestrator
"""

import asyncio
import os
import time
import uuid
import random
from enum import Enum
from functools import partial
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import httpx
import structlog
import numpy as np

logger = structlog.get_logger()


class WyomingSessionState(Enum):
    """Wyoming session states for interruption handling."""
    IDLE = "idle"                  # Waiting for audio
    LISTENING = "listening"        # Capturing audio
    PROCESSING = "processing"      # STT/Query processing
    SPEAKING = "speaking"          # TTS playback active
    INTERRUPTED = "interrupted"    # User interrupted TTS


@dataclass
class WyomingInterruptionContext:
    """Context preserved when user interrupts TTS."""
    interrupted_response: str = ""
    previous_query: str = ""
    audio_position_ms: int = 0
    interruption_point: float = 0.0

# Check if wyoming is available
try:
    from wyoming.server import AsyncServer
    from wyoming.event import Event
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.asr import Transcribe, Transcript
    from wyoming.tts import Synthesize, SynthesizeVoice
    from wyoming.info import Info, AsrModel, TtsVoice, Describe, Attribution
    from wyoming.handle import AsyncEventHandler
    WYOMING_AVAILABLE = True
except ImportError:
    WYOMING_AVAILABLE = False
    logger.warning("wyoming package not installed - Wyoming bridge disabled")


# Try to import event system
try:
    from shared.events import EventType, EventEmitterFactory, emit_session_start, emit_session_end
    EVENTS_AVAILABLE = True
except ImportError:
    EVENTS_AVAILABLE = False
    logger.warning("Event system not available")


# Try to import voice config
try:
    from shared.voice_config import VoiceConfigFactory
    VOICE_CONFIG_AVAILABLE = True
except ImportError:
    VOICE_CONFIG_AVAILABLE = False
    logger.warning("Voice config not available")


# Configuration
DEFAULT_STT_URL = os.getenv("STT_URL", "http://localhost:10301")
DEFAULT_TTS_URL = os.getenv("TTS_URL", "http://localhost:10201")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:8080")

# AI-initiated follow-up settings (Phase 2)
FOLLOW_UP_DELAY_SECONDS = 3.0  # Wait 3 seconds after TTS before follow-up
FOLLOW_UP_PHRASES: List[str] = [
    "Is there anything else?",
    "Anything else I can help with?",
    "Need anything else?",
]
MAX_FOLLOW_UPS_PER_SESSION = 2  # Don't be annoying

# Import Prometheus metrics from gateway main (will be available when running as part of gateway)
try:
    from gateway.main import (
        stt_duration, tts_duration, llm_duration,
        voice_pipeline_duration, voice_step_counter
    )
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    logger.info("Prometheus metrics not available (standalone mode)")


if WYOMING_AVAILABLE:

    class AthenaWyomingHandler(AsyncEventHandler):
        """
        Wyoming protocol handler that routes to Athena's configurable pipeline.

        Handles:
        - Audio streaming from Wyoming satellites
        - STT transcription via configured engine
        - Query processing via Orchestrator
        - TTS synthesis via configured engine
        - Barge-in/interruption detection during playback
        - Continued conversation support
        """

        # Audio analysis constants
        SAMPLE_RATE = 16000
        SILENCE_THRESHOLD = 0.02  # RMS threshold for silence detection

        def __init__(self, *args, interface_name: str = 'home_assistant', **kwargs):
            super().__init__(*args, **kwargs)
            self.interface_name = interface_name
            self.audio_buffer = bytearray()
            self.session_id: Optional[str] = None
            self._voice_manager = None

            # State tracking for interruption handling
            self.state: WyomingSessionState = WyomingSessionState.IDLE
            self.last_query: str = ""
            self.current_response: str = ""
            self.tts_position_ms: int = 0
            self.tts_cancelled: bool = False
            self.interruption_context: Optional[WyomingInterruptionContext] = None

            # TTS playback control
            self._tts_cancel_event: Optional[asyncio.Event] = None

            # Pipeline timing (for total duration metric)
            self.pipeline_start_time: Optional[float] = None

            # AI-initiated follow-ups (Phase 2)
            self.last_response_time: float = 0.0
            self.follow_up_count: int = 0
            self._follow_up_task: Optional[asyncio.Task] = None
            self._follow_ups_enabled: bool = False
            self._last_feature_flag_check: float = 0.0
            self._feature_flag_check_interval: float = 60.0  # Check every 60 seconds

        async def _get_voice_manager(self):
            """Get or create voice config manager."""
            if VOICE_CONFIG_AVAILABLE and self._voice_manager is None:
                self._voice_manager = VoiceConfigFactory.get(self.interface_name)
            return self._voice_manager

        async def _refresh_feature_flags(self):
            """Refresh feature flags from admin API periodically."""
            now = time.time()
            if now - self._last_feature_flag_check < self._feature_flag_check_interval:
                return

            self._last_feature_flag_check = now

            try:
                async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                    response = await client.get(f"{ADMIN_API_URL}/api/features/public")
                    if response.status_code == 200:
                        features = response.json()
                        for feature in features:
                            if feature.get("name") == "ai_follow_ups_enabled":
                                new_value = feature.get("enabled", False)
                                if new_value != self._follow_ups_enabled:
                                    logger.info("wyoming_feature_flag_updated",
                                               flag="ai_follow_ups_enabled",
                                               old_value=self._follow_ups_enabled,
                                               new_value=new_value)
                                self._follow_ups_enabled = new_value
                                return
            except Exception as e:
                logger.debug("wyoming_feature_flag_check_error", error=str(e))

        def _cancel_follow_up_task(self):
            """Cancel any pending follow-up task."""
            if self._follow_up_task and not self._follow_up_task.done():
                self._follow_up_task.cancel()
                self._follow_up_task = None

        async def _schedule_follow_up(self):
            """Schedule a follow-up question after delay if conditions are met."""
            # Don't schedule if disabled or at limit
            if not self._follow_ups_enabled:
                return
            if self.follow_up_count >= MAX_FOLLOW_UPS_PER_SESSION:
                return

            # Cancel any existing follow-up task
            self._cancel_follow_up_task()

            # Schedule the follow-up
            self._follow_up_task = asyncio.create_task(self._delayed_follow_up())

        async def _delayed_follow_up(self):
            """Wait and then ask follow-up if still idle."""
            try:
                await asyncio.sleep(FOLLOW_UP_DELAY_SECONDS)

                # Check if still idle and no new audio came in
                if self.state != WyomingSessionState.IDLE:
                    return

                # Check if still within time window (no new activity)
                if time.time() - self.last_response_time < FOLLOW_UP_DELAY_SECONDS:
                    return

                # Ask follow-up
                phrase = random.choice(FOLLOW_UP_PHRASES)
                self.follow_up_count += 1

                logger.info("wyoming_ai_initiated_follow_up",
                           session_id=self.session_id,
                           phrase=phrase,
                           follow_up_count=self.follow_up_count)

                # Synthesize and play the follow-up
                await self._synthesize(phrase)

            except asyncio.CancelledError:
                # Task was cancelled (new audio came in)
                pass
            except Exception as e:
                logger.error("wyoming_follow_up_error", error=str(e))

        def _calculate_audio_energy(self, audio_data: bytes) -> float:
            """Calculate RMS energy of audio data for speech detection."""
            try:
                samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                if len(samples) == 0:
                    return 0.0
                rms = np.sqrt(np.mean(samples ** 2))
                return float(rms / 32768.0)  # Normalize to 0-1 range
            except Exception:
                return 0.0

        def _is_speech(self, audio_data: bytes) -> bool:
            """Detect if audio contains speech (not silence)."""
            energy = self._calculate_audio_energy(audio_data)
            return energy > self.SILENCE_THRESHOLD

        async def handle_event(self, event: Event):
            """Handle incoming Wyoming events with state tracking."""
            try:
                if Describe.is_type(event.type):
                    return await self._describe()

                elif AudioStart.is_type(event.type):
                    return await self._audio_start()

                elif AudioChunk.is_type(event.type):
                    return await self._audio_chunk(event)

                elif AudioStop.is_type(event.type):
                    return await self._audio_stop()

                elif Transcribe.is_type(event.type):
                    return await self._transcribe()

                elif Synthesize.is_type(event.type):
                    synth = Synthesize.from_event(event)
                    return await self._synthesize(synth.text)

            except Exception as e:
                logger.error("wyoming_event_error", error=str(e), event_type=event.type)
                self.state = WyomingSessionState.IDLE
                raise

        async def _describe(self) -> Event:
            """Return service capabilities."""
            manager = await self._get_voice_manager()

            stt_description = "Athena STT (default)"
            tts_description = "Athena TTS (default)"

            if manager and manager._initialized:
                config = await manager.get_interface_config(self.interface_name)
                if config:
                    if config.stt_engine:
                        stt_description = f"Athena STT ({config.stt_engine.display_name})"
                    if config.tts_engine:
                        tts_description = f"Athena TTS ({config.tts_engine.display_name})"

            return Info(
                asr=[AsrModel(
                    name='athena-stt',
                    description=stt_description,
                    languages=['en-US', 'en'],
                    attribution=Attribution(
                        name="Project Athena",
                        url="https://github.com/project-athena"
                    )
                )],
                tts=[TtsVoice(
                    name='athena-tts',
                    description=tts_description,
                    languages=['en-US', 'en'],
                    attribution=Attribution(
                        name="Project Athena",
                        url="https://github.com/project-athena"
                    )
                )],
            ).event()

        async def _audio_start(self):
            """Handle audio session start."""
            # Cancel any pending follow-up (new audio came in)
            self._cancel_follow_up_task()

            # Refresh feature flags periodically
            await self._refresh_feature_flags()

            # Check if we're interrupting TTS playback
            if self.state == WyomingSessionState.SPEAKING:
                logger.info("wyoming_barge_in_detected",
                           session_id=self.session_id,
                           tts_position_ms=self.tts_position_ms)

                # Store interruption context
                self.interruption_context = WyomingInterruptionContext(
                    interrupted_response=self.current_response,
                    previous_query=self.last_query,
                    audio_position_ms=self.tts_position_ms,
                    interruption_point=time.time()
                )
                self.state = WyomingSessionState.INTERRUPTED
                self.tts_cancelled = True

                # Signal TTS to stop
                if self._tts_cancel_event:
                    self._tts_cancel_event.set()

            self.audio_buffer.clear()
            self.session_id = str(uuid.uuid4())
            self.state = WyomingSessionState.LISTENING

            # Start pipeline timing
            self.pipeline_start_time = time.time()

            if EVENTS_AVAILABLE:
                await emit_session_start(
                    self.session_id,
                    self.interface_name,
                    {'protocol': 'wyoming', 'interrupted': self.interruption_context is not None}
                )

            logger.debug("wyoming_audio_start",
                        session_id=self.session_id,
                        was_interrupted=self.interruption_context is not None)

        async def _audio_chunk(self, event: Event):
            """Handle audio chunk with barge-in detection."""
            chunk = AudioChunk.from_event(event)
            audio_data = chunk.audio

            # If we're in speaking state, check for barge-in
            if self.state == WyomingSessionState.SPEAKING:
                if self._is_speech(audio_data):
                    logger.info("wyoming_barge_in_speech_detected",
                               session_id=self.session_id)

                    self.interruption_context = WyomingInterruptionContext(
                        interrupted_response=self.current_response,
                        previous_query=self.last_query,
                        audio_position_ms=self.tts_position_ms,
                        interruption_point=time.time()
                    )
                    self.state = WyomingSessionState.INTERRUPTED
                    self.tts_cancelled = True

                    if self._tts_cancel_event:
                        self._tts_cancel_event.set()

                    # Start capturing the new utterance
                    self.state = WyomingSessionState.LISTENING

            self.audio_buffer.extend(audio_data)

        async def _audio_stop(self):
            """Handle audio session stop - triggers transcription."""
            return await self._transcribe()

        async def _transcribe(self) -> Event:
            """Transcribe accumulated audio using configured STT engine."""
            if not self.audio_buffer:
                logger.debug("wyoming_transcribe_empty")
                return Transcript(text='').event()

            session_id = self.session_id or str(uuid.uuid4())

            # Get STT URL from config or use default
            stt_url = DEFAULT_STT_URL
            stt_engine = "whisper"  # Default engine name for metrics
            manager = await self._get_voice_manager()
            if manager and manager._initialized:
                config = await manager.get_stt_config(self.interface_name)
                if config and config.get('wyoming_url'):
                    # For Wyoming STT, use REST endpoint
                    stt_url = config.get('wyoming_url').replace('tcp://', 'http://').replace(':10300', ':10301')
                if config and config.get('engine'):
                    stt_engine = config.get('engine')

            logger.info("wyoming_transcribe_start",
                       session_id=session_id,
                       audio_bytes=len(self.audio_buffer),
                       stt_url=stt_url)

            stt_start_time = time.time()

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    # Send audio to STT service
                    response = await client.post(
                        f"{stt_url}/v1/audio/transcriptions",
                        files={'file': ('audio.wav', bytes(self.audio_buffer), 'audio/wav')},
                        data={'model': 'whisper-1', 'language': 'en'},
                    )

                    stt_elapsed = time.time() - stt_start_time

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get('text', '').strip()

                        # Record STT metrics
                        if METRICS_AVAILABLE:
                            stt_duration.labels(
                                engine=stt_engine,
                                interface=self.interface_name
                            ).observe(stt_elapsed)
                            voice_step_counter.labels(
                                step="stt",
                                status="success",
                                interface=self.interface_name
                            ).inc()

                        logger.info("wyoming_transcribe_complete",
                                   session_id=session_id,
                                   text=text[:100],
                                   stt_duration_ms=int(stt_elapsed * 1000))

                        # If we got a transcription, send it to orchestrator for processing
                        if text:
                            await self._process_query(text, session_id)

                        return Transcript(text=text).event()
                    else:
                        # Record STT failure
                        if METRICS_AVAILABLE:
                            voice_step_counter.labels(
                                step="stt",
                                status="error",
                                interface=self.interface_name
                            ).inc()

                        logger.error("wyoming_stt_error",
                                    status=response.status_code,
                                    response=response.text[:200],
                                    stt_duration_ms=int(stt_elapsed * 1000))
                        return Transcript(text='').event()

            except Exception as e:
                # Record STT failure
                if METRICS_AVAILABLE:
                    voice_step_counter.labels(
                        step="stt",
                        status="error",
                        interface=self.interface_name
                    ).inc()

                logger.error("wyoming_transcribe_error", error=str(e))
                return Transcript(text='').event()

        async def _process_query(self, text: str, session_id: str):
            """Send query to orchestrator for processing with interruption context."""
            self.state = WyomingSessionState.PROCESSING
            self.last_query = text  # Store for potential interruption

            llm_start_time = time.time()
            llm_model = "unknown"  # Will be updated from response if available

            try:
                # Build request with interruption context if available
                request_data = {
                    'query': text,
                    'mode': 'owner',
                    'room': self.interface_name,
                    'session_id': session_id,
                    'interface_type': 'voice',  # Enables TTS text normalization
                }

                # Include interruption context if this was a barge-in
                if self.interruption_context:
                    request_data['interruption_context'] = {
                        'previous_query': self.interruption_context.previous_query,
                        'interrupted_response': self.interruption_context.interrupted_response,
                        'audio_position_ms': self.interruption_context.audio_position_ms,
                        'interruption_point': self.interruption_context.interruption_point,
                    }
                    logger.info("wyoming_query_with_interruption_context",
                               session_id=session_id,
                               previous_query=self.interruption_context.previous_query[:50])

                    # Clear after use
                    self.interruption_context = None

                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        f"{ORCHESTRATOR_URL}/query",
                        json=request_data
                    )

                    llm_elapsed = time.time() - llm_start_time

                    if response.status_code == 200:
                        result = response.json()
                        self.current_response = result.get('response', '')
                        llm_model = result.get('model', 'orchestrator')

                        # Record LLM metrics
                        if METRICS_AVAILABLE:
                            llm_duration.labels(
                                model=llm_model,
                                interface=self.interface_name
                            ).observe(llm_elapsed)
                            voice_step_counter.labels(
                                step="llm",
                                status="success",
                                interface=self.interface_name
                            ).inc()

                        logger.info("wyoming_query_complete",
                                   session_id=session_id,
                                   response_preview=self.current_response[:100],
                                   llm_duration_ms=int(llm_elapsed * 1000),
                                   model=llm_model)
                    else:
                        # Record LLM failure
                        if METRICS_AVAILABLE:
                            voice_step_counter.labels(
                                step="llm",
                                status="error",
                                interface=self.interface_name
                            ).inc()

                        logger.warning("wyoming_orchestrator_error",
                                      status=response.status_code,
                                      llm_duration_ms=int(llm_elapsed * 1000))

            except Exception as e:
                # Record LLM failure
                if METRICS_AVAILABLE:
                    voice_step_counter.labels(
                        step="llm",
                        status="error",
                        interface=self.interface_name
                    ).inc()

                logger.error("wyoming_process_query_error", error=str(e))
            finally:
                if self.state == WyomingSessionState.PROCESSING:
                    self.state = WyomingSessionState.IDLE

        async def _synthesize(self, text: str):
            """Synthesize speech using configured TTS engine with cancellation support."""
            session_id = self.session_id or str(uuid.uuid4())

            # Store current response for interruption context
            self.current_response = text
            self.tts_position_ms = 0
            self.tts_cancelled = False
            self._tts_cancel_event = asyncio.Event()

            # Get TTS URL from config or use default
            tts_url = DEFAULT_TTS_URL
            voice_id = "en_US-lessac-medium"
            tts_engine = "piper"  # Default engine name for metrics

            manager = await self._get_voice_manager()
            if manager and manager._initialized:
                config = await manager.get_tts_config(self.interface_name)
                if config:
                    if config.get('wyoming_url'):
                        tts_url = config.get('wyoming_url').replace('tcp://', 'http://').replace(':10200', ':10201')
                    if config.get('voice_id'):
                        voice_id = config.get('voice_id')
                    if config.get('engine'):
                        tts_engine = config.get('engine')

            logger.info("wyoming_synthesize_start",
                       session_id=session_id,
                       text_length=len(text),
                       tts_url=tts_url)

            # Enter speaking state
            self.state = WyomingSessionState.SPEAKING
            tts_start_time = time.time()

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    # Use /tts/synthesize endpoint with correct parameters
                    response = await client.post(
                        f"{tts_url}/tts/synthesize",
                        json={
                            'text': text,
                            'voice': voice_id,
                        },
                    )

                    tts_elapsed = time.time() - tts_start_time

                    if response.status_code == 200:
                        audio_data = response.content

                        # Record TTS metrics
                        if METRICS_AVAILABLE:
                            tts_duration.labels(
                                engine=tts_engine,
                                voice=voice_id,
                                interface=self.interface_name
                            ).observe(tts_elapsed)
                            voice_step_counter.labels(
                                step="tts",
                                status="success",
                                interface=self.interface_name
                            ).inc()

                        logger.info("wyoming_synthesize_complete",
                                   session_id=session_id,
                                   audio_bytes=len(audio_data),
                                   tts_duration_ms=int(tts_elapsed * 1000))

                        # Return audio chunks with cancellation check
                        chunk_size = 16000 * 2  # 1 second of 16kHz 16-bit audio
                        chunk_duration_ms = 1000  # 1 second per chunk

                        for i in range(0, len(audio_data), chunk_size):
                            # Check for cancellation before each chunk
                            if self.tts_cancelled or self._tts_cancel_event.is_set():
                                logger.info("wyoming_tts_cancelled",
                                           session_id=session_id,
                                           position_ms=self.tts_position_ms)
                                break

                            chunk = audio_data[i:i + chunk_size]
                            yield AudioChunk(audio=chunk, rate=16000, width=2, channels=1).event()
                            self.tts_position_ms += chunk_duration_ms

                    else:
                        # Record TTS failure
                        if METRICS_AVAILABLE:
                            voice_step_counter.labels(
                                step="tts",
                                status="error",
                                interface=self.interface_name
                            ).inc()

                        logger.error("wyoming_tts_error",
                                    status=response.status_code,
                                    response=response.text[:200],
                                    tts_duration_ms=int(tts_elapsed * 1000))

            except Exception as e:
                # Record TTS failure
                if METRICS_AVAILABLE:
                    voice_step_counter.labels(
                        step="tts",
                        status="error",
                        interface=self.interface_name
                    ).inc()

                logger.error("wyoming_synthesize_error", error=str(e))

            finally:
                # Reset state
                if self.state == WyomingSessionState.SPEAKING:
                    self.state = WyomingSessionState.IDLE
                self._tts_cancel_event = None

                # Track response time for follow-ups (only if not cancelled)
                if not self.tts_cancelled:
                    self.last_response_time = time.time()
                    # Schedule follow-up question if enabled
                    await self._schedule_follow_up()

                # Record total pipeline duration
                if METRICS_AVAILABLE and self.pipeline_start_time:
                    pipeline_elapsed = time.time() - self.pipeline_start_time
                    voice_pipeline_duration.labels(
                        interface=self.interface_name
                    ).observe(pipeline_elapsed)

                    logger.info("wyoming_pipeline_complete",
                               session_id=session_id,
                               pipeline_duration_ms=int(pipeline_elapsed * 1000),
                               cancelled=self.tts_cancelled)

                    # Reset pipeline timer
                    self.pipeline_start_time = None

            # End session
            if EVENTS_AVAILABLE:
                await emit_session_end(
                    session_id,
                    self.interface_name,
                    success=not self.tts_cancelled
                )


    async def run_wyoming_server(host: str = "0.0.0.0", port: int = 10400, interface_name: str = "home_assistant"):
        """Run the Wyoming protocol server."""
        server = AsyncServer.from_uri(f'tcp://{host}:{port}')

        logger.info("wyoming_bridge_starting",
                   host=host,
                   port=port,
                   interface=interface_name)

        await server.run(
            partial(AthenaWyomingHandler, interface_name=interface_name)
        )

else:
    # Stub implementation when Wyoming is not available
    async def run_wyoming_server(host: str = "0.0.0.0", port: int = 10400, interface_name: str = "home_assistant"):
        logger.error("wyoming_not_available", message="Install wyoming package to enable Wyoming bridge")
        raise RuntimeError("Wyoming package not installed")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Athena Wyoming Bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=10400, help="Port to bind")
    parser.add_argument("--interface", default="home_assistant", help="Interface name")

    args = parser.parse_args()

    asyncio.run(run_wyoming_server(args.host, args.port, args.interface))
