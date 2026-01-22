"""
Voice testing API routes.

Provides testing endpoints for STT, TTS, LLM, RAG, and full pipeline tests.
Adapted for Mac Studio/mini architecture (no Jetson wake word detection).
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timedelta
import structlog
import aiohttp
import asyncio
import time
import os
import uuid
import wave
import io

# Wyoming protocol imports
from wyoming.client import AsyncTcpClient
from wyoming.asr import Transcribe, Transcript
from wyoming.tts import Synthesize
from wyoming.audio import AudioStart, AudioChunk, AudioStop, wav_to_chunks
from wyoming.event import Event

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, VoiceTest, VoiceTestFeedback, LLMPerformanceMetric

logger = structlog.get_logger()

router = APIRouter(prefix="/api/voice-tests", tags=["voice-tests"])

# Wyoming service configuration
WYOMING_STT_HOST = os.getenv("WYOMING_STT_HOST", "localhost")
WYOMING_STT_PORT = int(os.getenv("WYOMING_STT_PORT", "10300"))
WYOMING_TTS_HOST = os.getenv("WYOMING_TTS_HOST", "localhost")
WYOMING_TTS_PORT = int(os.getenv("WYOMING_TTS_PORT", "10200"))

# LLM and service configuration
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
RAG_SERVICE_HOST = os.getenv("RAG_SERVICE_HOST", "localhost")


async def wyoming_transcribe(audio_path: str) -> dict:
    """
    Transcribe audio using Wyoming Whisper STT service.

    Args:
        audio_path: Path to WAV audio file

    Returns:
        Dict with transcript text and metadata
    """
    client = AsyncTcpClient(host=WYOMING_STT_HOST, port=WYOMING_STT_PORT)

    try:
        await client.connect()

        # Send transcribe request
        transcribe_event = Transcribe(language="en").event()
        await client.write_event(transcribe_event)

        # Read WAV file and send audio chunks
        with wave.open(audio_path, "rb") as wav_file:
            rate = wav_file.getframerate()
            width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()

            # Send AudioStart
            audio_start = AudioStart(rate=rate, width=width, channels=channels).event()
            await client.write_event(audio_start)

            # Send audio chunks (1024 samples at a time)
            samples_per_chunk = 1024
            while True:
                audio_data = wav_file.readframes(samples_per_chunk)
                if not audio_data:
                    break
                audio_chunk = AudioChunk(
                    rate=rate, width=width, channels=channels, audio=audio_data
                ).event()
                await client.write_event(audio_chunk)

            # Send AudioStop
            audio_stop = AudioStop().event()
            await client.write_event(audio_stop)

        # Wait for transcript response
        transcript_text = ""
        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=30.0)
            if event is None:
                break
            if Transcript.is_type(event.type):
                transcript = Transcript.from_event(event)
                transcript_text = transcript.text
                break

        return {
            "text": transcript_text,
            "language": "en",
        }

    finally:
        await client.disconnect()


async def wyoming_synthesize(text: str, voice: Optional[str] = None) -> bytes:
    """
    Synthesize speech using Wyoming Piper TTS service.

    Args:
        text: Text to synthesize
        voice: Optional voice name

    Returns:
        Raw PCM audio bytes
    """
    client = AsyncTcpClient(host=WYOMING_TTS_HOST, port=WYOMING_TTS_PORT)

    try:
        await client.connect()

        # Send synthesize request
        synthesize_event = Synthesize(text=text).event()
        await client.write_event(synthesize_event)

        # Collect audio response
        audio_chunks = []
        audio_info = None

        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=30.0)
            if event is None:
                break

            if AudioStart.is_type(event.type):
                audio_info = AudioStart.from_event(event)
            elif AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                audio_chunks.append(chunk.audio)
            elif AudioStop.is_type(event.type):
                break

        # Combine all audio chunks
        pcm_audio = b"".join(audio_chunks)

        return pcm_audio, audio_info

    finally:
        await client.disconnect()


class TestQuery(BaseModel):
    """Request model for test queries."""
    text: str = None
    model: str = None
    voice: str = None
    connector: str = None


@router.post("/stt/test")
async def test_speech_to_text(
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Test Wyoming Whisper STT service.

    Uploads audio file and transcribes using Faster-Whisper via Wyoming protocol.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Save uploaded audio
    audio_path = f"/tmp/test_audio_{uuid.uuid4()}.wav"
    try:
        with open(audio_path, "wb") as f:
            f.write(await audio.read())

        # Call Wyoming STT service via TCP protocol
        start = time.time()

        try:
            transcript_result = await wyoming_transcribe(audio_path)
            transcript = transcript_result.get("text", "")
            elapsed = time.time() - start

            result = {
                "transcript": transcript,
                "confidence": 0.95,  # Wyoming doesn't return confidence, use default
                "processing_time": int(elapsed * 1000),
                "model": "faster-whisper-tiny.en",
                "service": "mac-studio-whisper",
                "wyoming_host": f"{WYOMING_STT_HOST}:{WYOMING_STT_PORT}"
            }

        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Wyoming STT service timeout ({WYOMING_STT_HOST}:{WYOMING_STT_PORT})"
            )
        except ConnectionRefusedError:
            raise HTTPException(
                status_code=503,
                detail=f"Wyoming STT service unavailable ({WYOMING_STT_HOST}:{WYOMING_STT_PORT})"
            )

        # Store test result
        test = VoiceTest(
            test_type="stt",
            test_input=audio.filename,
            test_config={"audio_file": audio.filename, "wyoming_host": WYOMING_STT_HOST},
            result=result,
            success=True,
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        logger.info("stt_test_completed", user=current_user.username, success=True,
                   transcript_length=len(transcript))

        return {
            "success": True,
            **result
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("stt_test_failed", error=str(e), error_type=type(e).__name__)

        # Store failure
        test = VoiceTest(
            test_type="stt",
            test_input=audio.filename,
            test_config={"audio_file": audio.filename},
            result={},
            success=False,
            error_message=str(e),
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # Cleanup
        if os.path.exists(audio_path):
            os.remove(audio_path)


@router.post("/tts/test")
async def test_text_to_speech(
    query: TestQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Test Wyoming Piper TTS service.

    Generates audio from text using Piper TTS via Wyoming protocol.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not query.text:
        raise HTTPException(status_code=400, detail="Text is required")

    audio_path = f"/tmp/tts_output_{uuid.uuid4()}.wav"

    try:
        start = time.time()

        try:
            # Call Wyoming TTS service via TCP protocol
            pcm_audio, audio_info = await wyoming_synthesize(query.text, query.voice)
            elapsed = time.time() - start

            # Save PCM audio as WAV file
            if audio_info and pcm_audio:
                with wave.open(audio_path, "wb") as wav_file:
                    wav_file.setnchannels(audio_info.channels)
                    wav_file.setsampwidth(audio_info.width)
                    wav_file.setframerate(audio_info.rate)
                    wav_file.writeframes(pcm_audio)

                audio_duration_ms = int(len(pcm_audio) / (audio_info.rate * audio_info.width * audio_info.channels) * 1000)
            else:
                audio_duration_ms = 0

            result = {
                "audio_path": audio_path,
                "text": query.text,
                "voice": query.voice or "en_US-lessac-medium",
                "processing_time": int(elapsed * 1000),
                "audio_duration_ms": audio_duration_ms,
                "audio_bytes": len(pcm_audio) if pcm_audio else 0,
                "model": "piper-tts",
                "service": "mac-studio-piper",
                "wyoming_host": f"{WYOMING_TTS_HOST}:{WYOMING_TTS_PORT}"
            }

        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Wyoming TTS service timeout ({WYOMING_TTS_HOST}:{WYOMING_TTS_PORT})"
            )
        except ConnectionRefusedError:
            raise HTTPException(
                status_code=503,
                detail=f"Wyoming TTS service unavailable ({WYOMING_TTS_HOST}:{WYOMING_TTS_PORT})"
            )

        # Store test result
        test = VoiceTest(
            test_type="tts",
            test_input=query.text,
            test_config={"voice": query.voice, "wyoming_host": WYOMING_TTS_HOST},
            result=result,
            success=True,
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        logger.info("tts_test_completed", user=current_user.username, success=True,
                   audio_bytes=result.get("audio_bytes", 0))

        return {
            "success": True,
            **result
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("tts_test_failed", error=str(e), error_type=type(e).__name__)

        test = VoiceTest(
            test_type="tts",
            test_input=query.text,
            test_config={"voice": query.voice},
            result={},
            success=False,
            error_message=str(e),
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # Note: We keep the audio file for the user to access
        # It will be cleaned up by system temp file cleanup
        pass


@router.post("/llm/test")
async def test_llm_processing(
    query: TestQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Test Ollama LLM.

    Processes prompt using Phi-3 or Llama 3.1 models.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not query.text:
        raise HTTPException(status_code=400, detail="Prompt is required")

    model = query.model or "phi3:mini"

    try:
        url = f"{OLLAMA_URL}/api/generate"
        payload = {
            "model": model,
            "prompt": query.text,
            "stream": False
        }

        start = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                elapsed = time.time() - start
                if resp.status == 200:
                    data = await resp.json()
                    # Sanitize Unicode characters to prevent DB encoding issues
                    response_text = data.get("response", "")
                    response_text = response_text.replace('\u2018', "'").replace('\u2019', "'")  # Smart single quotes
                    response_text = response_text.replace('\u201c', '"').replace('\u201d', '"')  # Smart double quotes
                    response_text = response_text.replace('\u2013', '-').replace('\u2014', '-')  # En/Em dashes

                    result = {
                        "response": response_text,
                        "processing_time": int(elapsed * 1000),
                        "model": model,
                        "tokens": data.get("eval_count", 0),
                        "tokens_per_second": round(data.get("eval_count", 0) / elapsed, 2) if elapsed > 0 else 0,
                        "service": "mac-studio-ollama"
                    }

                    # Store test result
                    test = VoiceTest(
                        test_type="llm",
                        test_input=query.text,
                        test_config={"model": model},
                        result=result,
                        success=True,
                        executed_by_id=current_user.id
                    )
                    db.add(test)
                    db.commit()

                    logger.info("llm_test_completed", model=model, user=current_user.username, success=True)

                    return {
                        "success": True,
                        **result
                    }
                else:
                    error_text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {error_text}")

    except Exception as e:
        logger.error("llm_test_failed", error=str(e))

        test = VoiceTest(
            test_type="llm",
            test_input=query.text,
            test_config={"model": model},
            result={},
            success=False,
            error_message=str(e),
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/test")
async def test_rag_query(
    query: TestQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Test RAG service with query.

    Tests weather, airports, or sports RAG connectors.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not query.text:
        raise HTTPException(status_code=400, detail="Query is required")

    connector = query.connector or "weather"

    try:
        # Determine RAG service URL
        port_map = {
            "weather": 8010,
            "airports": 8011,
            "flights": 8012
        }
        port = port_map.get(connector, 8010)

        # Build URL based on connector type
        if connector == "weather":
            url = f"http://{RAG_SERVICE_HOST}:{port}/weather/current?location={query.text}"
        elif connector == "airports":
            url = f"http://{RAG_SERVICE_HOST}:{port}/airports/{query.text}"
        elif connector == "flights":
            url = f"http://{RAG_SERVICE_HOST}:{port}/flights/{query.text}"
        else:
            raise HTTPException(status_code=400, detail=f"Unknown connector: {connector}")

        start = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                elapsed = time.time() - start
                if resp.status == 200:
                    data = await resp.json()
                    result = {
                        "response": data,
                        "processing_time": int(elapsed * 1000),
                        "connector": connector,
                        "cached": resp.headers.get('X-Cache-Hit', 'false') == 'true',
                        "service": f"mac-studio-rag-{connector}"
                    }

                    # Store test result
                    test = VoiceTest(
                        test_type="rag_query",
                        test_input=query.text,
                        test_config={"connector": connector},
                        result=result,
                        success=True,
                        executed_by_id=current_user.id
                    )
                    db.add(test)
                    db.commit()

                    logger.info("rag_test_completed", connector=connector, user=current_user.username, success=True)

                    return {
                        "success": True,
                        **result
                    }
                else:
                    error_text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {error_text}")

    except Exception as e:
        logger.error("rag_test_failed", error=str(e))

        test = VoiceTest(
            test_type="rag_query",
            test_input=query.text,
            test_config={"connector": connector},
            result={},
            success=False,
            error_message=str(e),
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pipeline/test")
async def test_full_pipeline(
    query: TestQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Test full voice pipeline adapted for Mac architecture.

    Pipeline: LLM Processing → (optional RAG) → (optional HA execution) → TTS
    Note: No wake word or STT since this starts with text input
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not query.text:
        raise HTTPException(status_code=400, detail="Query is required")

    try:
        timings = {}
        results = {}

        # 1. LLM Processing
        start = time.time()
        llm_url = f"{OLLAMA_URL}/api/generate"
        async with aiohttp.ClientSession() as session:
            async with session.post(llm_url, json={
                "model": "phi3:mini",
                "prompt": query.text,
                "stream": False
            }, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    timings["llm"] = time.time() - start
                    # Sanitize Unicode characters to prevent DB encoding issues
                    response_text = data.get("response", "")
                    response_text = response_text.replace('\u2018', "'").replace('\u2019', "'")  # Smart single quotes
                    response_text = response_text.replace('\u201c', '"').replace('\u201d', '"')  # Smart double quotes
                    response_text = response_text.replace('\u2013', '-').replace('\u2014', '-')  # En/Em dashes
                    results["llm_response"] = response_text
                else:
                    raise Exception(f"LLM failed: HTTP {resp.status}")

        # 2. RAG Enhancement (check if query needs real-time data)
        rag_result = None
        rag_keywords = {
            "weather": ["weather", "temperature", "forecast", "rain", "sunny", "cold", "hot"],
            "airports": ["airport", "flight", "airline", "terminal", "gate"],
            "sports": ["score", "game", "match", "team", "sports", "football", "basketball", "baseball"]
        }

        query_lower = query.text.lower()
        rag_connector = None
        for connector, keywords in rag_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                rag_connector = connector
                break

        if rag_connector:
            try:
                start = time.time()
                port_map = {"weather": 8010, "airports": 8011, "sports": 8017}
                port = port_map.get(rag_connector, 8010)

                # Call appropriate RAG service
                if rag_connector == "weather":
                    rag_url = f"http://{RAG_SERVICE_HOST}:{port}/weather/current?location=Baltimore,MD"
                elif rag_connector == "airports":
                    rag_url = f"http://{RAG_SERVICE_HOST}:{port}/airports/BWI"
                else:
                    rag_url = f"http://{RAG_SERVICE_HOST}:{port}/scores"

                async with aiohttp.ClientSession() as session:
                    async with session.get(rag_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            rag_result = await resp.json()
                            timings["rag"] = time.time() - start
                            results["rag_connector"] = rag_connector
                            results["rag_data"] = rag_result
                        else:
                            timings["rag"] = time.time() - start
                            results["rag_error"] = f"HTTP {resp.status}"
            except Exception as e:
                timings["rag"] = time.time() - start if "start" in dir() else 0
                results["rag_error"] = str(e)
                logger.warning("rag_enhancement_failed", connector=rag_connector, error=str(e))

        # 3. Home Assistant Integration (if command detected)
        # Note: HA integration handled by orchestrator, not admin pipeline

        # 4. TTS Generation - Convert LLM response to speech
        tts_result = None
        try:
            start = time.time()
            # Use Wyoming TTS to synthesize speech from LLM response
            tts_text = results.get("llm_response", "")
            if tts_text and len(tts_text) < 500:  # Limit TTS to reasonable length
                pcm_audio, audio_info = await wyoming_synthesize(tts_text)
                timings["tts"] = time.time() - start

                if pcm_audio and audio_info:
                    audio_duration_ms = int(len(pcm_audio) / (audio_info.rate * audio_info.width * audio_info.channels) * 1000)
                    results["tts_audio_bytes"] = len(pcm_audio)
                    results["tts_audio_duration_ms"] = audio_duration_ms
                    results["tts_voice"] = "en_US-lessac-medium"
                else:
                    results["tts_error"] = "No audio generated"
            else:
                timings["tts"] = 0
                results["tts_skipped"] = "Response too long for TTS" if len(tts_text) >= 500 else "No response to synthesize"
        except asyncio.TimeoutError:
            timings["tts"] = time.time() - start if "start" in dir() else 0
            results["tts_error"] = f"TTS timeout ({WYOMING_TTS_HOST}:{WYOMING_TTS_PORT})"
        except ConnectionRefusedError:
            timings["tts"] = 0
            results["tts_error"] = f"TTS unavailable ({WYOMING_TTS_HOST}:{WYOMING_TTS_PORT})"
        except Exception as e:
            timings["tts"] = time.time() - start if "start" in dir() else 0
            results["tts_error"] = str(e)
            logger.warning("tts_generation_failed", error=str(e))

        total_time = sum(timings.values())

        # Determine pipeline stages completed
        stages_completed = ["llm"]
        if "rag_connector" in results:
            stages_completed.append("rag")
        if "tts_audio_bytes" in results:
            stages_completed.append("tts")

        result = {
            "timings": {k: int(v * 1000) for k, v in timings.items()},
            "total_time": int(total_time * 1000),
            "results": results,
            "target_met": total_time < 5.0,
            "stages_completed": stages_completed,
            "note": f"Pipeline: {' → '.join(stages_completed).upper()}"
        }

        # Store test result
        test = VoiceTest(
            test_type="full_pipeline",
            test_input=query.text,
            test_config={
                "model": "phi3:mini",
                "rag_connector": rag_connector,
                "tts_enabled": True
            },
            result=result,
            success=True,
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()
        db.refresh(test)  # Get the ID

        # Also save to performance metrics table so it appears on metrics page
        try:
            # Extract token count from LLM response (if available)
            tokens_generated = data.get("eval_count", 0)
            tokens_per_second = tokens_generated / total_time if total_time > 0 else 0

            metric = LLMPerformanceMetric(
                timestamp=datetime.utcnow(),
                model="phi3:mini",
                backend="ollama",
                latency_seconds=total_time,
                tokens_generated=tokens_generated,
                tokens_per_second=tokens_per_second,
                request_id=f"voice_test_{test.id}",
                user_id=current_user.username,
                intent=query.text[:100],  # Use query as intent (truncated)
                source="admin_voice_test"
            )
            db.add(metric)
            db.commit()

            logger.info("performance_metric_saved", test_id=test.id, tokens_per_sec=tokens_per_second)
        except Exception as e:
            logger.warning("failed_to_save_performance_metric", test_id=test.id, error=str(e))
            # Don't fail the test if metric save fails

        logger.info("pipeline_test_completed", user=current_user.username, success=True,
                   total_time=total_time, test_id=test.id)

        return {
            "success": True,
            "test_id": test.id,  # Return test ID for feedback
            **result
        }

    except Exception as e:
        logger.error("pipeline_test_failed", error=str(e))

        test = VoiceTest(
            test_type="full_pipeline",
            test_input=query.text,
            test_config={},
            result={},
            success=False,
            error_message=str(e),
            executed_by_id=current_user.id
        )
        db.add(test)
        db.commit()

        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tests/history")
async def get_test_history(
    test_type: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get historical test results."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(VoiceTest)

    if test_type:
        query = query.filter(VoiceTest.test_type == test_type)

    tests = query.order_by(VoiceTest.executed_at.desc()).limit(limit).all()

    return {
        "tests": [t.to_dict() for t in tests],
        "total": len(tests)
    }


class FeedbackRequest(BaseModel):
    """Request model for voice test feedback."""
    test_id: int
    feedback: str  # 'correct' or 'incorrect'
    query: str
    notes: str = None


@router.post("/feedback")
async def save_test_feedback(
    feedback_data: FeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save user feedback on voice test results for active learning.

    Allows users to mark LLM responses as correct/incorrect to improve
    system quality over time.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate feedback type
    if feedback_data.feedback not in ['correct', 'incorrect']:
        raise HTTPException(status_code=400, detail="Feedback must be 'correct' or 'incorrect'")

    try:
        # Verify test exists and get the response
        test = db.query(VoiceTest).filter(VoiceTest.id == feedback_data.test_id).first()
        if not test:
            raise HTTPException(status_code=404, detail=f"Test {feedback_data.test_id} not found")

        # Extract response from test result
        llm_response = None
        if test.result and isinstance(test.result, dict):
            results = test.result.get('results', {})
            llm_response = results.get('llm_response', '')

        # Create feedback record
        feedback = VoiceTestFeedback(
            test_id=feedback_data.test_id,
            feedback_type=feedback_data.feedback,
            query=feedback_data.query,
            response=llm_response,
            user_id=current_user.id,
            notes=feedback_data.notes
        )

        db.add(feedback)
        db.commit()
        db.refresh(feedback)

        logger.info(
            "test_feedback_saved",
            test_id=feedback_data.test_id,
            feedback_type=feedback_data.feedback,
            user=current_user.username
        )

        return {
            "success": True,
            "feedback_id": feedback.id,
            "message": f"Feedback recorded: {feedback_data.feedback}",
            "learning_enabled": True
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("feedback_save_failed", error=str(e), test_id=feedback_data.test_id)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save feedback: {str(e)}")
