# Project Athena

A privacy-focused, locally-hosted AI voice assistant with RAG (Retrieval-Augmented Generation) capabilities for smart home integration.

## Overview

Project Athena is a modular voice assistant system that runs entirely on your own hardware. Unlike cloud-based assistants, all processing happens locally - your voice data never leaves your network.

**Key Capabilities:**
- Natural language voice commands for smart home control
- RAG-powered information retrieval (weather, sports, dining, news, etc.)
- Home Assistant integration for device control
- Configurable LLM backends (Ollama, OpenAI-compatible APIs)
- Modular architecture - enable only what you need

## Features

### Voice Processing
- Speech-to-text transcription
- Text-to-speech response generation
- Wake word detection support
- Multi-room/zone awareness

### RAG Services (20+ Modules)
| Category | Services |
|----------|----------|
| **Weather** | Current conditions, forecasts, alerts |
| **Sports** | Live scores, schedules, standings |
| **Dining** | Restaurant search, recommendations |
| **Travel** | Flights, airports, Amtrak, directions |
| **Entertainment** | Streaming availability, events, news |
| **Shopping** | Price comparison across retailers |
| **Local** | Community events, nearby places |

### Smart Home
- Home Assistant integration
- Device control (lights, locks, thermostats, etc.)
- Scene activation
- Status queries

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Voice Input                            │
│              (Wyoming Protocol / Web UI)                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                     Gateway                                 │
│         (Authentication, Rate Limiting, Routing)            │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                   Orchestrator                              │
│        (Intent Classification, Tool Selection)              │
│                        │                                    │
│    ┌──────────────────┼──────────────────┐                  │
│    ▼                  ▼                  ▼                  │
│ ┌───────┐       ┌──────────┐      ┌───────────┐             │
│ │ LLM   │       │RAG Tools │      │ HA Client │             │
│ │(Local)│       │(20+ svcs)│      │           │             │
│ └───────┘       └──────────┘      └───────────┘             │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites
- Python 3.10+
- PostgreSQL database
- Ollama (or OpenAI-compatible LLM API)
- Redis (optional, for caching)

### Installation

```bash
# Clone the repository
git clone https://github.com/jstuart0/project-athena-oss.git
cd project-athena-oss

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your configuration
```

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for detailed setup instructions.

### Configuration

```bash
# Required environment variables
export OLLAMA_HOST=your-ollama-server
export ATHENA_DB_HOST=your-postgres-host
export ATHENA_DB_PASSWORD=your-db-password
export ADMIN_API_URL=http://localhost:8080
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for all configuration options.

### Running Services

```bash
# Start the orchestrator
cd src/orchestrator
python -m uvicorn main:app --host 0.0.0.0 --port 8001

# Start the gateway
cd src/gateway
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Start RAG services (example: weather)
cd src/rag/weather
python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

## Project Structure

```
project-athena/
├── src/
│   ├── orchestrator/      # Query routing and LLM coordination
│   ├── gateway/           # API gateway and authentication
│   ├── rag/               # RAG service modules
│   │   ├── weather/       # Weather forecasts
│   │   ├── sports/        # Sports scores and schedules
│   │   ├── dining/        # Restaurant recommendations
│   │   ├── flights/       # Flight tracking
│   │   └── ...            # 20+ additional services
│   ├── shared/            # Shared utilities and config
│   ├── control_agent/     # Service management API
│   └── mode_service/      # User mode management
├── admin/                 # Admin UI (optional)
├── apps/                  # Web interfaces
├── docs/                  # Documentation
│   ├── INSTALLATION.md
│   ├── CONFIGURATION.md
│   └── MODULES.md
├── tests/                 # Test suites
└── .env.example           # Environment template
```

## Modules

Project Athena uses a module system that lets you enable/disable features:

```bash
# Enable specific modules
MODULE_WEATHER=true
MODULE_SPORTS=true
MODULE_HOME_ASSISTANT=true
MODULE_DINING=false  # Disable if not needed
```

See [docs/MODULES.md](docs/MODULES.md) for the complete module reference.

## API Usage

### Query Endpoint

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is the weather like?",
    "mode": "owner",
    "room": "office"
  }'
```

### OpenAI-Compatible Endpoint

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "athena",
    "messages": [{"role": "user", "content": "Turn on the living room lights"}]
  }'
```

## Home Assistant Integration

Project Athena integrates with Home Assistant via the REST API:

1. Generate a Long-Lived Access Token in Home Assistant
2. Configure the token in your environment or admin panel
3. Enable the `home_assistant` module

Voice commands like "turn on the lights" or "lock the front door" will be routed to Home Assistant.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

- **Personal use:** Allowed
- **Research/Education:** Allowed
- **Commercial use:** Requires separate license

For commercial licensing inquiries, contact: jay@xmojo.net

## Acknowledgments

Built with:
- [Ollama](https://ollama.ai/) - Local LLM inference
- [Home Assistant](https://www.home-assistant.io/) - Smart home platform
- [FastAPI](https://fastapi.tiangolo.com/) - API framework
- [Wyoming Protocol](https://github.com/rhasspy/wyoming) - Voice assistant protocol
