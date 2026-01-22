"""
Wake Word Detection for LiveKit Audio Streams.

Uses OpenWakeWord for detecting "Jarvis" and "Athena" wake words
in real-time audio streams.
"""

import asyncio
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field
import struct
import io
import numpy as np
import structlog

logger = structlog.get_logger()

# Try to import openwakeword
try:
    import openwakeword
    from openwakeword.model import Model as OWWModel
    OPENWAKEWORD_AVAILABLE = True
except ImportError:
    OPENWAKEWORD_AVAILABLE = False
    logger.warning("openwakeword_not_available",
                   message="Install with: pip install openwakeword")


@dataclass
class WakeWordConfig:
    """Configuration for wake word detection."""
    wake_words: List[str] = field(default_factory=lambda: ["jarvis", "athena"])
    threshold: float = 0.5
    sample_rate: int = 16000
    chunk_size: int = 1280  # 80ms at 16kHz
    cooldown_seconds: float = 2.0  # Prevent rapid re-detection


class WakeWordDetector:
    """
    Detects wake words in audio streams using OpenWakeWord.

    Supports "Jarvis" and "Athena" wake words with configurable thresholds.
    """

    def __init__(self, config: Optional[WakeWordConfig] = None):
        self.config = config or WakeWordConfig()
        self._model: Optional[Any] = None
        self._initialized = False
        self._last_detection: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def is_available(self) -> bool:
        """Check if wake word detection is available."""
        return OPENWAKEWORD_AVAILABLE and self._initialized

    async def initialize(self) -> bool:
        """Initialize the wake word model."""
        if not OPENWAKEWORD_AVAILABLE:
            logger.warning("wake_word_init_skipped", reason="openwakeword not installed")
            return False

        async with self._lock:
            if self._initialized:
                return True

            try:
                # Download models if needed
                logger.info("wake_word_downloading_models")

                # OpenWakeWord provides pre-trained models
                # We'll use the default models and custom models if available
                model_paths = []

                # Try to find custom Jarvis/Athena models
                import os
                model_dir = os.environ.get("WAKE_WORD_MODEL_DIR", "/models/wake_words")

                for wake_word in self.config.wake_words:
                    custom_model = os.path.join(model_dir, f"{wake_word}.onnx")
                    if os.path.exists(custom_model):
                        model_paths.append(custom_model)
                        logger.info("wake_word_custom_model_found", wake_word=wake_word)

                # Load model
                if model_paths:
                    self._model = OWWModel(wakeword_models=model_paths)
                else:
                    # Use default hey_jarvis model if available
                    self._model = OWWModel(wakeword_models=["hey_jarvis_v0.1"])
                    logger.info("wake_word_using_default_model", model="hey_jarvis_v0.1")

                self._initialized = True
                logger.info("wake_word_initialized",
                           wake_words=self.config.wake_words,
                           threshold=self.config.threshold)
                return True

            except Exception as e:
                logger.error("wake_word_init_failed", error=str(e))
                return False

    async def detect(self, audio_data: bytes) -> Optional[str]:
        """
        Detect wake word in audio data.

        Args:
            audio_data: Raw PCM audio bytes (16-bit, mono, 16kHz)

        Returns:
            Detected wake word name or None
        """
        if not self._initialized or not self._model:
            return await self._fallback_detect(audio_data)

        try:
            # Convert bytes to numpy array
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            # Process through model
            prediction = self._model.predict(audio_array)

            # Check each wake word
            import time
            current_time = time.time()

            for wake_word in self.config.wake_words:
                # Get prediction score for this wake word
                score = self._get_score(prediction, wake_word)

                if score >= self.config.threshold:
                    # Check cooldown
                    last_time = self._last_detection.get(wake_word, 0)
                    if current_time - last_time >= self.config.cooldown_seconds:
                        self._last_detection[wake_word] = current_time
                        logger.info("wake_word_detected",
                                   wake_word=wake_word,
                                   score=score)
                        return wake_word

            return None

        except Exception as e:
            logger.error("wake_word_detection_error", error=str(e))
            return None

    def _get_score(self, prediction: Dict[str, Any], wake_word: str) -> float:
        """Extract score for a specific wake word from prediction."""
        # OpenWakeWord returns dict with model names as keys
        for key, value in prediction.items():
            if wake_word.lower() in key.lower():
                # Value is typically a list of scores, take the max
                if isinstance(value, (list, np.ndarray)):
                    return float(max(value)) if len(value) > 0 else 0.0
                return float(value)
        return 0.0

    async def _fallback_detect(self, audio_data: bytes) -> Optional[str]:
        """
        Fallback detection when OpenWakeWord is not available.

        Uses simple energy-based VAD as a placeholder.
        Returns None (no detection) - actual wake word detection requires the model.
        """
        # Without OpenWakeWord, we can't detect wake words
        # In production, this should trigger a warning
        return None

    def reset(self):
        """Reset detection state (e.g., after handling a wake word)."""
        self._last_detection.clear()


class AudioStreamProcessor:
    """
    Processes continuous audio streams for wake word detection.

    Handles buffering, chunking, and state management for real-time
    audio processing.
    """

    def __init__(
        self,
        detector: WakeWordDetector,
        on_wake_word: Optional[Callable[[str], None]] = None,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_speech_end: Optional[Callable[[bytes], None]] = None
    ):
        self.detector = detector
        self.on_wake_word = on_wake_word
        self.on_speech_start = on_speech_start
        self.on_speech_end = on_speech_end

        # Audio buffering
        self._buffer = io.BytesIO()
        self._chunk_buffer = bytearray()

        # State
        self._listening_for_query = False
        self._speech_buffer = io.BytesIO()
        self._silence_frames = 0
        self._speech_frames = 0

        # VAD config
        self._vad_threshold = 500  # RMS threshold
        self._silence_timeout_frames = 30  # ~2 seconds at 16kHz/1280 chunk
        self._min_speech_frames = 5  # Minimum frames to consider speech

    async def process_chunk(self, audio_chunk: bytes) -> Optional[str]:
        """
        Process an audio chunk.

        Returns wake word if detected, None otherwise.
        """
        # Add to chunk buffer
        self._chunk_buffer.extend(audio_chunk)

        # Process in fixed-size chunks
        chunk_size = 1280 * 2  # 80ms at 16kHz, 16-bit

        while len(self._chunk_buffer) >= chunk_size:
            chunk = bytes(self._chunk_buffer[:chunk_size])
            del self._chunk_buffer[:chunk_size]

            if self._listening_for_query:
                # We're collecting speech after wake word
                await self._process_speech_chunk(chunk)
            else:
                # Looking for wake word
                wake_word = await self.detector.detect(chunk)
                if wake_word:
                    self._listening_for_query = True
                    self._speech_buffer = io.BytesIO()
                    self._silence_frames = 0
                    self._speech_frames = 0

                    if self.on_wake_word:
                        self.on_wake_word(wake_word)

                    return wake_word

        return None

    async def _process_speech_chunk(self, chunk: bytes):
        """Process a chunk while collecting speech."""
        # Calculate RMS energy
        audio_array = np.frombuffer(chunk, dtype=np.int16)
        rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))

        is_speech = rms > self._vad_threshold

        if is_speech:
            self._silence_frames = 0
            self._speech_frames += 1

            if self._speech_frames == 1 and self.on_speech_start:
                self.on_speech_start()
        else:
            self._silence_frames += 1

        # Always write to buffer when listening
        self._speech_buffer.write(chunk)

        # Check for end of speech
        if self._silence_frames >= self._silence_timeout_frames:
            if self._speech_frames >= self._min_speech_frames:
                # Speech ended, emit the audio
                speech_data = self._speech_buffer.getvalue()
                if self.on_speech_end:
                    self.on_speech_end(speech_data)

            # Reset state
            self._listening_for_query = False
            self._speech_buffer = io.BytesIO()
            self._silence_frames = 0
            self._speech_frames = 0

    def reset(self):
        """Reset processor state."""
        self._buffer = io.BytesIO()
        self._chunk_buffer = bytearray()
        self._listening_for_query = False
        self._speech_buffer = io.BytesIO()
        self._silence_frames = 0
        self._speech_frames = 0
        self.detector.reset()


# Singleton instance
_detector: Optional[WakeWordDetector] = None


async def get_wake_word_detector() -> WakeWordDetector:
    """Get or create the wake word detector singleton."""
    global _detector

    if _detector is None:
        _detector = WakeWordDetector()
        await _detector.initialize()

    return _detector


def create_stream_processor(
    on_wake_word: Optional[Callable[[str], None]] = None,
    on_speech_start: Optional[Callable[[], None]] = None,
    on_speech_end: Optional[Callable[[bytes], None]] = None
) -> AudioStreamProcessor:
    """
    Create an audio stream processor with callbacks.

    Args:
        on_wake_word: Called when wake word detected
        on_speech_start: Called when speech begins after wake word
        on_speech_end: Called with complete speech audio after silence

    Returns:
        Configured AudioStreamProcessor
    """
    detector = WakeWordDetector()
    # Note: Must call detector.initialize() before using

    return AudioStreamProcessor(
        detector=detector,
        on_wake_word=on_wake_word,
        on_speech_start=on_speech_start,
        on_speech_end=on_speech_end
    )
