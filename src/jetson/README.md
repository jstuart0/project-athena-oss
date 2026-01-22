# Jetson LLM Webhook Service

## Overview

Flask-based webhook service for NVIDIA Jetson devices that processes voice commands with LLM intelligence.

## Components

- **llm_webhook_service.py** - Main Flask service with 3 endpoints
- **athena_lite.py** - Original Athena Lite voice pipeline
- **athena_lite_llm.py** - Enhanced version with LLM integration
- **config/** - Configuration files for HA integration

## Deployment

Deploy to your Jetson device and configure environment variables:

```bash
# Set the port for the webhook service
export WEBHOOK_PORT=5000

# Configure your Athena orchestrator URL
export ORCHESTRATOR_URL=http://your-orchestrator:8001
```

## Running

```bash
python llm_webhook_service.py
```

The service listens on port 5000 by default.
