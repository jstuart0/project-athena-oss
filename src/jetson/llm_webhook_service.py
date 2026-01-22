#!/usr/bin/env python3
"""
LLM Webhook Service for Home Assistant Integration
Receives commands from HA Voice and processes them with LLM
"""

import os
import json
from flask import Flask, request, jsonify
from athena_lite_llm import AthenaLiteLLM
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize Athena LLM (this takes a few seconds)
logger.info('Initializing Athena LLM service...')
athena = None

def init_athena():
    global athena
    if athena is None:
        # Set the HA token
        os.environ['HA_TOKEN'] = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4NjNhNWIwMDM3OTE0ODE1YTVlODkyZWUwNTMxMmIwZCIsImlhdCI6MTc2MjE4MzY0MiwiZXhwIjoyMDc3NTQzNjQyfQ.M-vSeDlQl3NvGrpeZ35QKat8OjTXA2z3559Hy96EC4A'
        athena = AthenaLiteLLM()
        logger.info('Athena LLM service ready!')

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'athena-llm-webhook',
        'llm_ready': athena is not None
    })

@app.route('/process_command', methods=['POST'])
def process_command():
    """Process command with LLM and send to HA"""
    try:
        if athena is None:
            init_athena()
        
        data = request.get_json()
        command = data.get('command', '')
        
        if not command:
            return jsonify({'error': 'No command provided'}), 400
        
        logger.info(f'Received command: {command}')
        
        # Process with Athena LLM
        athena.send_to_home_assistant(command)
        
        return jsonify({
            'status': 'success',
            'message': f'Command processed: {command}',
            'processed_by': 'athena-llm'
        })
        
    except Exception as e:
        logger.error(f'Error processing command: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/simple_command', methods=['POST'])  
def simple_command():
    """Process simple command directly (bypass LLM)"""
    try:
        if athena is None:
            init_athena()
            
        data = request.get_json()
        command = data.get('command', '')
        
        if not command:
            return jsonify({'error': 'No command provided'}), 400
            
        logger.info(f'Processing simple command: {command}')
        
        # Force simple processing (bypass LLM)
        athena.send_to_home_assistant(command)
        
        return jsonify({
            'status': 'success', 
            'message': f'Simple command processed: {command}',
            'processed_by': 'direct-ha'
        })
        
    except Exception as e:
        logger.error(f'Error processing simple command: {e}')
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Initialize on startup
    init_athena()
    
    # Run the webhook service
    app.run(
        host='0.0.0.0',  # Listen on all interfaces
        port=5000,       # Port 5000
        debug=False      # Disable debug in production
    )

@app.route('/conversation', methods=['POST'])
def conversation():
    """Handle conversation requests from HA"""
    try:
        data = request.get_json()
        text = data.get('text', '')
        
        if not text:
            return jsonify({'error': 'No text provided'}), 400
            
        logger.info(f"Conversation request: {text}")
        
        # Store the command for processing
        athena.input_text_value = text
        
        # Process the command through Athena LLM
        response = athena.process_command_with_llm(text)
        
        return jsonify({
            'response': response,
            'text': text,
            'processed_by': 'athena-conversation'
        })
        
    except Exception as e:
        logger.error(f"Error in conversation endpoint: {e}")
        return jsonify({'error': str(e)}), 500
