"""
LiveKit Integration with Athena Pipeline.

Connects LiveKit audio processing to the orchestrator:
- Query handling (audio → STT → orchestrator → TTS → audio)
- Event emission for Admin Jarvis
- Session management
"""

import os
import asyncio
from typing import Optional, Any, Dict
import structlog

from gateway.livekit_service import (
    LiveKitService,
    LiveKitSession,
    get_livekit_service,
    initialize_livekit_service
)

logger = structlog.get_logger()

# Configuration
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_SERVICE_URL", "http://localhost:8001")
STT_SERVICE_URL = os.getenv("STT_SERVICE_URL", "http://localhost:10301")
TTS_SERVICE_URL = os.getenv("TTS_SERVICE_URL", "http://localhost:10201")


class LiveKitIntegration:
    """
    Integrates LiveKit audio streaming with Athena's query pipeline.

    Responsibilities:
    - Set up callbacks for LiveKit events
    - Route audio queries to orchestrator
    - Handle TTS responses
    - Emit events for Admin Jarvis monitoring
    """

    def __init__(
        self,
        orchestrator_url: str = ORCHESTRATOR_URL,
        stt_url: str = STT_SERVICE_URL,
        tts_url: str = TTS_SERVICE_URL
    ):
        self.orchestrator_url = orchestrator_url
        self.stt_url = stt_url
        self.tts_url = tts_url

        self.livekit_service: Optional[LiveKitService] = None
        self._http_client = None

    async def initialize(self):
        """Initialize LiveKit service with integration callbacks."""
        import httpx

        # Create HTTP client for orchestrator calls
        self._http_client = httpx.AsyncClient(
            base_url=self.orchestrator_url,
            timeout=60.0
        )

        # Create STT client wrapper
        stt_client = STTClient(self.stt_url)

        # Create TTS client wrapper
        tts_client = TTSClient(self.tts_url)

        # Initialize LiveKit service
        self.livekit_service = await initialize_livekit_service(
            stt_client=stt_client,
            tts_client=tts_client
        )

        # Set up query handler
        self.livekit_service.set_query_handler(self._handle_query)

        # Set up session handlers for event emission
        self.livekit_service.set_session_handlers(
            on_start=self._on_session_start,
            on_end=self._on_session_end
        )

        logger.info("livekit_integration_initialized")

    async def _handle_query(
        self,
        session_id: str,
        transcript: str,
        interface: str = "livekit",
        room: str = "unknown",
        interruption_context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Handle a transcribed query from LiveKit.

        Sends to orchestrator and returns the response text.
        """
        try:
            logger.info("livekit_query_received",
                       session_id=session_id,
                       query=transcript[:50],
                       has_interruption_context=interruption_context is not None)

            # Emit event for Admin Jarvis
            await self._emit_event("query_received", session_id, {
                "query": transcript,
                "interface": interface,
                "room": room,
                "interruption_context": interruption_context
            })

            # Build request payload
            request_data = {
                "query": transcript,
                "mode": "owner",
                "room": room,
                "session_id": session_id,
                "interface_type": "voice"
            }

            # Include interruption context if user interrupted previous response
            if interruption_context:
                request_data["interruption_context"] = interruption_context
                logger.info("livekit_interruption_context_forwarded",
                           session_id=session_id,
                           previous_query=interruption_context.get("previous_query", "")[:30])

            # Call orchestrator
            response = await self._http_client.post(
                "/query",
                json=request_data
            )
            response.raise_for_status()
            result = response.json()

            answer = result.get("answer", "I'm sorry, I couldn't process that request.")

            logger.info("livekit_query_response",
                       session_id=session_id,
                       response=answer[:50])

            # Emit response event
            await self._emit_event("response_generated", session_id, {
                "response": answer,
                "intent": result.get("intent"),
                "tools_used": result.get("tools_used", [])
            })

            return answer

        except Exception as e:
            logger.error("livekit_query_error",
                        session_id=session_id,
                        error=str(e))

            await self._emit_event("error", session_id, {
                "error": str(e),
                "query": transcript
            })

            return "I encountered an error processing your request. Please try again."

    async def _on_session_start(self, session: LiveKitSession):
        """Handle LiveKit session start."""
        await self._emit_event("session_start", session.session_id, {
            "room_name": session.room_name,
            "interface": session.interface,
            "participant": session.participant_id
        })

    async def _on_session_end(self, session: LiveKitSession):
        """Handle LiveKit session end."""
        duration_ms = int((session.last_activity - session.created_at) * 1000)

        await self._emit_event("session_end", session.session_id, {
            "room_name": session.room_name,
            "duration_ms": duration_ms
        })

    async def _emit_event(self, event_type: str, session_id: str, data: dict):
        """Emit event for Admin Jarvis monitoring."""
        try:
            from shared.events import EventEmitterFactory, EventType

            emitter = EventEmitterFactory.get()
            if emitter:
                # Map to EventType enum
                type_map = {
                    "session_start": EventType.SESSION_START,
                    "session_end": EventType.SESSION_END,
                    "query_received": EventType.STT_COMPLETE,
                    "response_generated": EventType.RESPONSE_GENERATED,
                    "error": EventType.ERROR
                }

                et = type_map.get(event_type, EventType.CUSTOM)
                await emitter.emit(et, session_id, data, interface="livekit")

        except ImportError:
            pass  # Events module not available
        except Exception as e:
            logger.warning("event_emit_failed", error=str(e))

    async def shutdown(self):
        """Shutdown integration and cleanup."""
        if self.livekit_service:
            await self.livekit_service.shutdown()

        if self._http_client:
            await self._http_client.aclose()

        logger.info("livekit_integration_shutdown")


class STTClient:
    """Simple STT client for transcribing audio."""

    def __init__(self, stt_url: str):
        self.stt_url = stt_url

    async def transcribe(self, audio_data: bytes) -> str:
        """Transcribe audio bytes to text."""
        import subprocess
        import tempfile
        import json
        import wave
        import io

        try:
            # Convert raw PCM to WAV format
            # LiveKit audio is configured for 16kHz mono int16 (optimal for Whisper)
            sample_rate = 16000
            channels = 1
            sample_width = 2  # 16-bit = 2 bytes

            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(sample_width)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(audio_data)

            wav_data = wav_buffer.getvalue()
            logger.info("audio_converted_to_wav",
                       raw_size=len(audio_data),
                       wav_size=len(wav_data))

            # Use curl as workaround for httpx connectivity issues on macOS
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_data)
                tmp_path = tmp.name

            try:
                result = subprocess.run(
                    ["curl", "-s", "-m", "30", "-X", "POST",
                     "-F", f"audio=@{tmp_path};type=audio/wav",
                     f"{self.stt_url}/stt/transcribe"],
                    capture_output=True,
                    text=True,
                    timeout=35
                )

                if result.returncode != 0 or not result.stdout:
                    logger.error("stt_curl_failed",
                               returncode=result.returncode,
                               stderr=result.stderr[:200] if result.stderr else "")
                    return ""

                response_data = json.loads(result.stdout)
                text = response_data.get("text", "")
                logger.info("stt_transcribed", text_length=len(text))
                return text

            finally:
                import os
                try:
                    os.unlink(tmp_path)
                except:
                    pass

        except Exception as e:
            logger.error("stt_transcription_failed", error=str(e))
            return ""

    async def _local_transcribe(self, audio_data: bytes) -> str:
        """Fallback to local Whisper transcription."""
        try:
            import whisper
            import numpy as np
            import io
            import wave

            # Convert bytes to numpy array
            with io.BytesIO(audio_data) as buf:
                with wave.open(buf, 'rb') as wav:
                    frames = wav.readframes(wav.getnframes())
                    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

            # Load model and transcribe
            model = whisper.load_model("tiny.en")
            result = model.transcribe(audio)
            return result.get("text", "")

        except ImportError:
            return ""
        except Exception as e:
            logger.warning("local_transcription_failed", error=str(e))
            return ""


class TTSClient:
    """Simple TTS client for synthesizing speech."""

    def __init__(self, tts_url: str):
        self.tts_url = tts_url

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to audio bytes."""
        import subprocess
        import tempfile
        import json

        try:
            # Use curl as workaround for httpx connectivity issues on macOS
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            result = subprocess.run(
                ["curl", "-s", "-m", "30", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"text": text}),
                 "-o", tmp_path,
                 f"{self.tts_url}/tts/synthesize"],
                capture_output=True,
                text=True,
                timeout=35
            )

            if result.returncode != 0:
                logger.error("tts_curl_failed",
                           returncode=result.returncode,
                           stderr=result.stderr[:200] if result.stderr else "")
                return b""

            # Read the audio file
            import os
            with open(tmp_path, "rb") as f:
                audio_data = f.read()

            os.unlink(tmp_path)
            logger.info("tts_synthesized", audio_size=len(audio_data))
            return audio_data

        except Exception as e:
            logger.error("tts_synthesis_failed", error=str(e))

            # Fallback: Use local Piper if available
            try:
                return await self._local_synthesize(text)
            except:
                return b""

    async def _local_synthesize(self, text: str) -> bytes:
        """Fallback to local Piper TTS."""
        try:
            import subprocess
            import tempfile

            # Create temp file for output
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                output_path = f.name

            # Run Piper
            process = await asyncio.create_subprocess_exec(
                "piper",
                "--model", "en_US-lessac-medium",
                "--output_file", output_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            await process.communicate(input=text.encode())

            # Read output
            with open(output_path, "rb") as f:
                audio_data = f.read()

            # Cleanup
            import os
            os.unlink(output_path)

            return audio_data

        except Exception as e:
            logger.warning("local_synthesis_failed", error=str(e))
            return b""


# Singleton integration instance
_integration: Optional[LiveKitIntegration] = None


async def get_livekit_integration() -> Optional[LiveKitIntegration]:
    """Get or create LiveKit integration."""
    global _integration
    return _integration


async def initialize_livekit_integration() -> LiveKitIntegration:
    """Initialize LiveKit integration."""
    global _integration

    if _integration is None:
        _integration = LiveKitIntegration()
        await _integration.initialize()

    return _integration


async def shutdown_livekit_integration():
    """Shutdown LiveKit integration."""
    global _integration

    if _integration:
        await _integration.shutdown()
        _integration = None
