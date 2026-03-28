# Project Athena

A privacy-focused, fully local AI voice assistant with 23 RAG services, smart home control, and a LangGraph-powered orchestrator — all running on your own hardware.

## Why Athena?

Commercial voice assistants route your voice through cloud servers, add latency, and require subscriptions. Project Athena processes everything locally: your voice data never leaves your network, responses arrive in 2-5 seconds, and there are zero recurring costs.

**What makes it different:**
- **100% local processing** — all LLM inference, speech processing, and data retrieval runs on your hardware
- **LangGraph state machine** — an 11,000+ line orchestrator with intent classification, complexity-aware model routing, and multi-intent query decomposition
- **23 RAG services** — specialized microservices for weather, sports, dining, flights, directions, news, stocks, recipes, and more
- **Anti-hallucination pipeline** — 4-layer validation checks LLM responses against source data before delivery
- **Smart home control** — deep Home Assistant integration with 70+ command patterns for lights, locks, thermostats, and more
- **OpenAI-compatible API** — works with Home Assistant's Extended OpenAI Conversation integration out of the box

## Architecture

```
                         ┌──────────────────────┐
                         │    Voice Input        │
                         │ (Wyoming / Web / API) │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │      Gateway          │
                         │  Rate Limiting         │
                         │  Circuit Breaker       │
                         │  OpenAI-Compatible API │
                         └──────────┬───────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │            Orchestrator                     │
              │         (LangGraph State Machine)           │
              │                                             │
              │  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
              │  │ Classify  │→│ Retrieve  │→│ Validate  │ │
              │  └──────────┘  └──────────┘  └──────────┘ │
              │       │                            │       │
              │  ┌────▼────┐                  ┌────▼────┐  │
              │  │Complexity│                  │  Anti-  │  │
              │  │Detector  │                  │Halluci- │  │
              │  │(no LLM)  │                  │nation   │  │
              │  └─────────┘                  └─────────┘  │
              └──┬──────────────┬────────────────┬─────────┘
                 │              │                │
        ┌────────▼──┐  ┌───────▼──────┐  ┌─────▼──────┐
        │  LLM      │  │ 23 RAG       │  │  Home      │
        │  Router   │  │ Services     │  │  Assistant  │
        │           │  │              │  │  Client     │
        │ simple →  │  │ Weather      │  │             │
        │  4B model │  │ Sports       │  │ Lights      │
        │ complex → │  │ Dining       │  │ Locks       │
        │  14B model│  │ Flights ...  │  │ Climate     │
        │ super →   │  │              │  │ Media       │
        │  32B model│  │              │  │ Scenes      │
        └───────────┘  └──────────────┘  └─────────────┘
```

### Request Flow

1. Wake word ("Hey Jarvis") triggers voice capture via Wyoming protocol
2. Speech-to-text transcribes audio locally
3. **Gateway** applies rate limiting, circuit breaking, and routes to orchestrator
4. **Orchestrator** runs 6-layer deterministic preprocessing (STT error correction, slang normalization, false memory detection, emotional context, pattern classification) before any LLM call
5. **Complexity detector** scores the query (regex-only, no LLM) and selects the appropriate model tier
6. **RAG services** fetch real-time data from external APIs
7. **LLM synthesizes** a natural response using retrieved data
8. **Validation pipeline** checks for hallucinated facts against source data
9. **TTS normalizer** converts abbreviations, addresses, and numbers to natural speech
10. Audio streams back to the originating room's speaker

## RAG Services

23 specialized microservices, each independently deployable with its own caching, API key management, and health checks:

| Category | Services | Description |
|----------|----------|-------------|
| **Weather** | `weather`, `onecall` | Current conditions, hourly/daily forecasts, alerts |
| **Sports** | `sports` | Live scores, schedules, standings (ESPN, TheSportsDB, API-Football) |
| **Dining** | `dining`, `recipes` | Restaurant search, recommendations, recipe lookup |
| **Travel** | `flights`, `airports`, `amtrak`, `directions`, `transportation` | Flight tracking, airport info, train schedules, route planning, transit |
| **Entertainment** | `streaming`, `events`, `seatgeek_events`, `serpapi_events`, `community_events`, `media` | What's on Netflix, concerts, local events, media requests |
| **News & Finance** | `news`, `stocks` | Headlines, market data |
| **Shopping** | `price_compare` | Cross-retailer price comparison |
| **Vehicle** | `tesla` | Tesla Fleet API integration |
| **Research** | `websearch`, `site_scraper`, `brightdata` | Web search, page scraping, data extraction |

## Smart Home Control

Deep Home Assistant integration (4,500+ lines) with:

- **Lights** — on/off, dimming, color (including sports team colors), room-specific and whole-house
- **Locks** — lock/unlock with 70+ natural language patterns ("lock it down for the night", "did I lock the front door?")
- **Climate** — thermostat adjustments, fan control
- **Occupancy** — "Is anyone home?", "Who's in the kitchen?"
- **Media** — TV control, music playback
- **Response variety** — randomized response templates to avoid robotic repetition
- **Smart exclusions** — "Turn off all lights" knows to leave accent lighting alone

## Quick Start

### Prerequisites

- Python 3.10+
- PostgreSQL database
- Ollama (or any OpenAI-compatible LLM API)
- Redis (optional, for session caching)
- Home Assistant (optional, for smart home control)

### Installation

```bash
git clone https://github.com/jstuart0/project-athena-oss.git
cd project-athena-oss

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your configuration (see docs/CONFIGURATION.md)
```

### Running Services

**Option 1: Docker Compose (recommended)**

```bash
# Configure environment
cp .env.example .env
# Edit .env with required values

docker compose up -d
```

**Option 2: Manual**

```bash
# Start the gateway (OpenAI-compatible API layer)
cd src/gateway
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Start the orchestrator
cd src/orchestrator
python -m uvicorn main:app --host 0.0.0.0 --port 8001

# Start RAG services as needed
cd src/rag/weather
python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

**Option 3: Kubernetes**

Manifests are provided in `manifests/athena-prod/` for full cluster deployment.

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for detailed setup instructions.

### Configuration

All configuration is via environment variables with sensible defaults:

```bash
# Required
ATHENA_DB_PASSWORD=your-db-password
ADMIN_API_URL=http://localhost:8080
ENCRYPTION_KEY=your-encryption-key
SESSION_SECRET_KEY=your-session-key
JWT_SECRET=your-jwt-secret

# Recommended
OLLAMA_URL=http://localhost:11434
HA_URL=http://your-home-assistant:8123
HA_TOKEN=your-long-lived-access-token

# Module toggles
MODULE_WEATHER=true
MODULE_SPORTS=true
MODULE_HOME_ASSISTANT=true
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for all options and [docs/MODULES.md](docs/MODULES.md) for the module reference.

## API Usage

### Gateway (OpenAI-Compatible)

```bash
# Works with any OpenAI-compatible client or Home Assistant
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "athena",
    "messages": [{"role": "user", "content": "What is the weather like?"}]
  }'
```

### Orchestrator (Direct)

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Turn on the living room lights and check the weather",
    "mode": "owner",
    "room": "office",
    "interface_type": "voice"
  }'
```

### Home Assistant Integration

1. Install the **Extended OpenAI Conversation** integration
2. Set the base URL to your gateway: `http://your-athena-host:8000/v1`
3. Voice commands are automatically routed through the full pipeline

## Project Structure

```
project-athena/
├── src/
│   ├── orchestrator/        # LangGraph state machine (11,500+ lines)
│   │   ├── main.py          # Core orchestration graph
│   │   ├── smart_home_controller.py  # HA integration
│   │   ├── music_handler.py # Music/audio control
│   │   ├── tts_normalizer.py # Speech normalization
│   │   ├── complexity_detector.py    # Query complexity scoring
│   │   ├── semantic_cache.py # Intent-aware response caching
│   │   ├── circuit_breaker.py
│   │   └── search_providers/ # Pluggable search backends
│   ├── gateway/             # API gateway (3,000+ lines)
│   │   ├── main.py          # OpenAI-compatible endpoints
│   │   ├── wyoming_bridge.py # Wyoming protocol integration
│   │   ├── livekit_integration.py    # WebRTC voice streaming
│   │   └── circuit_breaker.py
│   ├── rag/                 # 23 RAG microservices
│   │   ├── weather/         # OpenWeatherMap
│   │   ├── sports/          # ESPN + TheSportsDB + API-Football
│   │   ├── dining/          # Google Places
│   │   ├── directions/      # Route planning
│   │   ├── flights/         # Flight tracking
│   │   └── ...              # 18 more services
│   ├── shared/              # Shared libraries
│   │   ├── config.py        # Centralized env-var configuration
│   │   ├── module_registry.py # Module enable/disable system
│   │   ├── llm_router.py    # Multi-backend LLM routing
│   │   ├── privacy_filter.py # PII scrubbing for cloud LLMs
│   │   └── admin_config.py  # Admin API client
│   ├── control_agent/       # Service watchdog (auto-restart)
│   ├── mode_service/        # Owner/guest mode management
│   └── jetson/              # NVIDIA Jetson edge deployment
├── admin/
│   ├── backend/             # FastAPI admin API (50+ DB migrations)
│   │   └── app/routes/      # 62 route modules
│   └── frontend/            # Admin web UI (58 JS modules)
├── apps/
│   └── jarvis-web/          # Push-to-talk web interface
├── manifests/
│   └── athena-prod/         # Kubernetes deployment manifests
├── scripts/                 # Build, deploy, and setup automation
├── tests/                   # Unit, integration, and E2E tests
├── docs/
│   ├── INSTALLATION.md
│   ├── CONFIGURATION.md
│   └── MODULES.md
├── docker-compose.yml       # Full-stack Docker deployment
├── .env.example             # Environment variable template
└── LICENSE                  # PolyForm Noncommercial 1.0.0
```

## Admin Interface

The optional admin backend provides a web UI for runtime configuration without code changes:

- **Model configuration** — assign LLM models per pipeline stage (classifier, synthesizer, validator)
- **Feature flags** — toggle capabilities at runtime
- **API key management** — encrypted storage for external service credentials
- **Service registry** — monitor and configure RAG services
- **Device management** — register and configure voice devices per room
- **Guest mode** — restricted access for household guests
- **Analytics** — query performance, latency metrics, usage patterns
- **Audit logging** — full trail of configuration changes
- **Memory management** — episodic and semantic conversation memory

## Key Technical Decisions

| Decision | Rationale |
|----------|-----------|
| **Deterministic preprocessing before LLM** | 6 regex/pattern layers handle 80%+ of queries without LLM calls, cutting latency significantly |
| **Complexity-based model routing** | Simple queries (time, lights) use a fast 4B model; complex queries escalate to 14B/32B — no wasted compute |
| **Separate validation model** | A dedicated smaller model fact-checks the response model's output against source data |
| **Microservice RAG** | Each data domain has different caching, rate limits, and failure modes — monolithic RAG would be fragile |
| **OpenAI-compatible gateway** | Drop-in compatibility with Home Assistant and any OpenAI client library |
| **Environment-variable configuration** | Zero hardcoded values — fully configurable for any deployment |

## Hardware Requirements

**Minimum (basic functionality):**
- 16GB RAM, 4-core CPU
- Ollama with a 3-4B parameter model
- Works on Mac Mini, small Linux server, or NUC

**Recommended (full experience):**
- Apple Silicon Mac with 32-64GB unified memory (for local LLM inference)
- Dedicated machine for vector DB and Redis cache
- 10GbE network for low-latency inter-service communication

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE)

- **Personal use:** Allowed
- **Research/Education:** Allowed
- **Commercial use:** Requires separate license

For commercial licensing inquiries, contact: jay@xmojo.net

## Acknowledgments

Built with:
- [LangGraph](https://github.com/langchain-ai/langgraph) — State machine orchestration
- [Ollama](https://ollama.ai/) — Local LLM inference
- [Home Assistant](https://www.home-assistant.io/) — Smart home platform
- [FastAPI](https://fastapi.tiangolo.com/) — API framework
- [Wyoming Protocol](https://github.com/rhasspy/wyoming) — Voice assistant protocol
- [LiveKit](https://livekit.io/) — WebRTC voice streaming
