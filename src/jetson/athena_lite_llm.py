#!/usr/bin/env python3
"""
Athena Lite with LLM Integration
Enhanced version with intelligent command processing
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
from transformers import AutoTokenizer, AutoModelForCausalLM
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
HOME_ASSISTANT_TOKEN = os.environ.get('HA_TOKEN', '')

# Audio settings
CHUNK_SIZE = 1280
SAMPLE_RATE = 16000
FORMAT = pyaudio.paInt16
CHANNELS = 1

# Detection thresholds
WAKE_WORD_THRESHOLD = 0.3  # Lowered for better detection
MAX_RECORDING_DURATION = 5
SILENCE_DURATION = 1.5

class AthenaLiteLLM:
    def __init__(self):
        logger.info('Initializing Athena Lite with LLM...')
        
        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        
        # Initialize wake word detection
        logger.info('Loading wake word models...')
        self.oww_model = Model(
            wakeword_models=WAKE_WORD_MODELS,
            inference_framework='tflite'
        )
        logger.info(f'Wake word models loaded: {list(self.oww_model.models.keys())}')
        
        # Initialize Whisper
        logger.info(f'Loading Whisper model: {WHISPER_MODEL}...')
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f'Using device: {self.device}')
        
        self.whisper_model = whisper.load_model(
            WHISPER_MODEL,
            download_root=WHISPER_MODEL_PATH,
            device=self.device
        )
        logger.info('Whisper model loaded')
        
        # Initialize VAD
        self.vad = webrtcvad.Vad(2)
        logger.info('VAD initialized with aggressiveness level 2')
        
        # Initialize LLM
        logger.info('Loading LLM for intelligent processing...')
        self.init_llm()
        
        if not HOME_ASSISTANT_TOKEN:
            logger.warning('HA_TOKEN not set. Commands will not be sent to Home Assistant.')
        
        logger.info('Athena Lite with LLM ready!')
    
    def init_llm(self):
        """Initialize the LLM for intelligent command processing"""
        try:
            model_name = "microsoft/DialoGPT-small"
            
            self.llm_tokenizer = AutoTokenizer.from_pretrained(model_name)
            if self.llm_tokenizer.pad_token is None:
                self.llm_tokenizer.pad_token = self.llm_tokenizer.eos_token
            
            self.llm_model = AutoModelForCausalLM.from_pretrained(
                model_name,
                torch_dtype=torch.float32,
                cache_dir='/mnt/nvme/athena-lite/models'
            )
            
            logger.info('LLM loaded successfully')
            
        except Exception as e:
            logger.error(f'Failed to load LLM: {e}')
            self.llm_model = None
            self.llm_tokenizer = None
    
    def is_complex_command(self, command):
        """Determine if command needs LLM processing"""
        complex_indicators = [
            "help", "explain", "how", "what", "why", "when", "where",
            "scene", "mood", "routine", "schedule", "please", "can you",
            "turn off all", "goodnight", "good morning", "movie", "dinner"
        ]
        
        command_lower = command.lower()
        return any(indicator in command_lower for indicator in complex_indicators)
    
    def process_with_llm(self, command):
        """Process command with LLM for intelligent response"""
        if not self.llm_model:
            return command  # Fallback to original
        
        try:
            # Create a simple prompt for home automation
            prompt = f"Convert this voice command into a clear home automation instruction: {command}"
            
            inputs = self.llm_tokenizer.encode(prompt, return_tensors='pt')
            
            with torch.no_grad():
                outputs = self.llm_model.generate(
            
            response = self.llm_tokenizer.decode(outputs[0], skip_special_tokens=True)
            
            # Extract the generated part (after original prompt)
            generated = response[len(prompt):].strip()
            
            return generated if generated else command
            
        except Exception as e:
            logger.error(f'LLM processing error: {e}')
            return command
    
    def send_to_home_assistant(self, command):
        """Send command to Home Assistant with intelligent routing"""
        if not HOME_ASSISTANT_TOKEN:
            logger.warning('HA_TOKEN not set, skipping command submission')
            return
        
        # Route command through LLM if complex
        if self.is_complex_command(command):
            logger.info(f'Complex command detected, processing with LLM: {command}')
            processed_command = self.process_with_llm(command)
            logger.info(f'LLM processed command: {processed_command}')
            final_command = processed_command
        else:
            logger.info(f'Simple command, sending directly: {command}')
            final_command = command
        
        try:
            headers = {
                'Authorization': f'Bearer {HOME_ASSISTANT_TOKEN}',
                'Content-Type': 'application/json'
            }
            data = {
                'text': final_command
            }
            response = requests.post(
                f'{HOME_ASSISTANT_URL}/api/conversation/process',
                headers=headers,
                json=data,
                timeout=10,
                verify=False
            )
            
            if response.status_code == 200:
                result = response.json()
                speech = result.get('response', {}).get('speech', {}).get('plain', {}).get('speech', '')
                logger.info(f'Home Assistant: {speech}')
            else:
                logger.error(f'Home Assistant returned status {response.status_code}')
                
        except Exception as e:
            logger.error(f'Error communicating with Home Assistant: {e}')

# Test function
def test_intelligent_commands():
    """Test the intelligent command routing"""
    print("Testing Athena Lite with LLM integration...")
    
    # Set token for testing
    os.environ['HA_TOKEN'] = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4NjNhNWIwMDM3OTE0ODE1YTVlODkyZWUwNTMxMmIwZCIsImlhdCI6MTc2MjE4MzY0MiwiZXhwIjoyMDc3NTQzNjQyfQ.M-vSeDlQl3NvGrpeZ35QKat8OjTXA2z3559Hy96EC4A'
    
    try:
        athena = AthenaLiteLLM()
        
        test_commands = [
            "turn on office lights",        # Simple - direct routing
            "what time is it",             # Simple - direct routing  
            "help me turn off all lights", # Complex - LLM processing
            "set the mood for dinner",     # Complex - LLM processing
        ]
        
        for cmd in test_commands:
            print(f"\n=== Testing: {cmd} ===")
            athena.send_to_home_assistant(cmd)
            time.sleep(1)  # Brief pause between commands
            
        print("\n✅ All tests completed!")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_intelligent_commands()
