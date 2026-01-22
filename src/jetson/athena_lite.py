#!/usr/bin/env python3
"""
Athena Lite - Dual Wake Word Voice Assistant for Home Assistant
Wake words: 'Jarvis' and 'Athena'
OPTIMIZED VERSION with VAD and GPU acceleration
"""

import os
import sys
import time
import wave
import numpy as np
import pyaudio
import whisper
import torch
import requests
import webrtcvad
from openwakeword.model import Model
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
WAKE_WORD_MODELS = [
    '/mnt/nvme/athena-lite/models/jarvis.tflite',
    '/mnt/nvme/athena-lite/models/athena.tflite'
]
WHISPER_MODEL = 'tiny.en'
WHISPER_MODEL_PATH = '/mnt/nvme/athena-lite/models'
HOME_ASSISTANT_URL = "http://localhost:8123"
HOME_ASSISTANT_TOKEN = os.environ.get('HA_TOKEN', '')  # Set via environment variable

# Audio settings
CHUNK_SIZE = 1280  # 80ms chunks for wake word detection
SAMPLE_RATE = 16000
FORMAT = pyaudio.paInt16
CHANNELS = 1

# Detection thresholds
WAKE_WORD_THRESHOLD = 0.5
MAX_RECORDING_DURATION = 5  # Maximum seconds to record after wake word
SILENCE_DURATION = 1.5  # Seconds of silence to stop recording

class AthenaLite:
    def __init__(self):
        logger.info('Initializing Athena Lite...')
        
        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        
        # Initialize wake word detection
        logger.info('Loading wake word models...')
        self.oww_model = Model(
            wakeword_models=WAKE_WORD_MODELS,
            inference_framework='tflite'
        )
        logger.info(f'Wake word models loaded: {list(self.oww_model.models.keys())}')
        
        # Initialize Whisper with GPU support
        logger.info(f'Loading Whisper model: {WHISPER_MODEL}...')
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f'Using device: {self.device}')
        
        self.whisper_model = whisper.load_model(
            WHISPER_MODEL,
            device=self.device,
            download_root=WHISPER_MODEL_PATH
        )
        logger.info('Whisper model loaded')
        
        # Initialize VAD
        self.vad = webrtcvad.Vad(2)  # Aggressiveness 0-3, 2 is balanced
        logger.info('VAD initialized with aggressiveness level 2')
        
        # Verify Home Assistant connection
        if HOME_ASSISTANT_TOKEN:
            self.verify_ha_connection()
        else:
            logger.warning('HA_TOKEN not set. Commands will not be sent to Home Assistant.')
        
        logger.info('Athena Lite ready!')
    
    def verify_ha_connection(self):
        """Test connection to Home Assistant"""
        try:
            headers = {
                'Authorization': f'Bearer {HOME_ASSISTANT_TOKEN}',
                'Content-Type': 'application/json'
            }
            response = requests.get(
                f'{HOME_ASSISTANT_URL}/api/',
                headers=headers,
                timeout=5
            )
            if response.status_code == 200:
                logger.info(f'Connected to Home Assistant at {HOME_ASSISTANT_URL}')
            else:
                logger.warning(f'Home Assistant responded with status {response.status_code}')
        except Exception as e:
            logger.warning(f'Could not connect to Home Assistant: {e}')
    
    def record_audio_with_vad(self, max_duration=5, silence_duration=1.5):
        """
        Record audio with Voice Activity Detection
        Stops when silence_duration seconds of silence detected after speech
        """
        logger.info(f'Recording command (max {max_duration}s, stops on {silence_duration}s silence)...')
        
        stream = self.audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE
        )
        
        frames = []
        silence_chunks = 0
        silence_threshold = int((silence_duration * SAMPLE_RATE) / CHUNK_SIZE)
        max_chunks = int((max_duration * SAMPLE_RATE) / CHUNK_SIZE)
        
        speech_detected = False
        
        for i in range(max_chunks):
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            frames.append(data)
            
            # Check if speech is present
            try:
                is_speech = self.vad.is_speech(data, SAMPLE_RATE)
            except Exception as e:
                # If VAD fails on this chunk, assume it's not speech
                is_speech = False
            
            if is_speech:
                speech_detected = True
                silence_chunks = 0
            elif speech_detected:
                silence_chunks += 1
                
                # If we've had enough silence after speech, stop
                if silence_chunks >= silence_threshold:
                    recording_time = len(frames) * CHUNK_SIZE / SAMPLE_RATE
                    logger.info(f'Silence detected after {recording_time:.2f}s')
                    break
        
        stream.stop_stream()
        stream.close()
        
        # Save to temporary file
        temp_file = '/tmp/athena_command.wav'
        with wave.open(temp_file, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(self.audio.get_sample_size(FORMAT))
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b''.join(frames))
        
        return temp_file
    
    def transcribe_audio(self, audio_file):
        """Transcribe audio using Whisper with GPU acceleration"""
        logger.info('Transcribing...')
        result = self.whisper_model.transcribe(audio_file)
        return result['text'].strip()
    
    def send_to_home_assistant(self, command):
        """Send command to Home Assistant Assist"""
        if not HOME_ASSISTANT_TOKEN:
            logger.warning('HA_TOKEN not set, skipping command submission')
            return
        
        try:
            headers = {
                'Authorization': f'Bearer {HOME_ASSISTANT_TOKEN}',
                'Content-Type': 'application/json'
            }
            data = {
                'text': command
            }
            response = requests.post(
                f'{HOME_ASSISTANT_URL}/api/conversation/process',
                headers=headers,
                json=data,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                speech = result.get('response', {}).get('speech', {}).get('plain', {}).get('speech', '')
                logger.info(f'Home Assistant: {speech}')
            else:
                logger.error(f'Home Assistant returned status {response.status_code}')
        except Exception as e:
            logger.error(f'Error communicating with Home Assistant: {e}')
    
    def handle_command(self):
        """Handle voice command with performance timing"""
        timings = {}
        start_total = time.time()
        
        try:
            # Record command with VAD
            start = time.time()
            audio_file = self.record_audio_with_vad(
                max_duration=MAX_RECORDING_DURATION,
                silence_duration=SILENCE_DURATION
            )
            timings['recording'] = time.time() - start
            
            # Transcribe
            start = time.time()
            command = self.transcribe_audio(audio_file)
            timings['transcription'] = time.time() - start
            
            logger.info(f'Command: {command}')
            
            # Send to Home Assistant
            if command:
                start = time.time()
                self.send_to_home_assistant(command)
                timings['execution'] = time.time() - start
            
            # Clean up
            try:
                os.remove(audio_file)
            except:
                pass
            
            timings['total'] = time.time() - start_total
            
            # Log performance metrics
            logger.info(
                f'Performance: Record={timings["recording"]:.2f}s, '
                f'Transcribe={timings["transcription"]:.2f}s, '
                f'Execute={timings.get("execution", 0):.2f}s, '
                f'Total={timings["total"]:.2f}s'
            )
            
        except Exception as e:
            logger.error(f'Error handling command: {e}', exc_info=True)
    
    def listen_for_wake_word(self):
        """Continuously listen for wake word"""
        logger.info('\nListening for wake words (Jarvis, Athena)...')
        
        stream = self.audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE
        )
        
        try:
            while True:
                # Read audio chunk
                audio_data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                
                # Convert to int16 array for wake word detection
                audio_array = np.frombuffer(audio_data, dtype=np.int16)
                
                # Get predictions
                prediction = self.oww_model.predict(audio_array)
                
                # Check if any wake word was detected
                for model_name, score in prediction.items():
                    if score > WAKE_WORD_THRESHOLD:
                        wake_word = model_name.replace('_v0.1', '').replace('hey_', '').replace('_', ' ').title()
                        logger.info(f'\nWake word detected: {wake_word} (confidence: {score:.2f})')
                        
                        # Stop listening stream temporarily
                        stream.stop_stream()
                        
                        # Handle the command
                        self.handle_command()
                        
                        # Resume listening
                        stream.start_stream()
                        logger.info('\nListening for wake words (Jarvis, Athena)...')
                        break
        
        except KeyboardInterrupt:
            logger.info('\nStopping Athena Lite...')
        finally:
            stream.stop_stream()
            stream.close()
    
    def cleanup(self):
        """Clean up resources"""
        self.audio.terminate()

def main():
    try:
        athena = AthenaLite()
        athena.listen_for_wake_word()
    except Exception as e:
        logger.error(f'Error: {e}', exc_info=True)
    finally:
        if 'athena' in locals():
            athena.cleanup()

if __name__ == '__main__':
    main()
