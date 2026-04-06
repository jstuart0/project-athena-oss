# Project Athena

A privacy-focused AI assistant with a built-in chat interface, voice control, 23 RAG services, smart home integration, and a LangGraph-powered orchestrator. Run it fully local, fully cloud, or anywhere in between.

## Why Athena?

Commercial AI assistants route your data through cloud servers, add latency, and require subscriptions. Project Athena gives you full control over where your data goes and which models you use. Run everything locally for maximum privacy, use cloud APIs for maximum capability, or mix and match per pipeline stage to optimize for cost, speed, and quality.

**What makes it different:**
- **No backend lock-in** — use local models (Ollama, MLX), cloud APIs (OpenAI, Anthropic, any OpenAI-compatible endpoint), or a mix — per pipeline stage, your call
- **Local-first by default** — can run 100% on your hardware with zero cloud dependencies, but cloud is always an option
- **Built-in chat interface** — Jarvis Web provides streaming text chat, push-to-talk voice, and smart home widgets from any browser
- **LangGraph state machine** — an 11,000+ line orchestrator with intent classification, complexity-aware model routing, and multi-intent query decomposition
- **23 RAG services** — specialized microservices for weather, sports, dining, flights, directions, news, stocks, recipes, and more
- **Anti-hallucination pipeline** — 4-layer validation checks LLM responses against source data before delivery
- **Smart home control** — deep Home Assistant integration with 70+ command patterns for lights, locks, thermostats, and more
- **OpenAI-compatible API** — works with Home Assistant, custom apps, or any OpenAI client library

## Interfaces

Athena supports three ways to interact, all backed by the same orchestrator and RAG pipeline:

### Chat Interface (Jarvis Web)

A standalone web application with streaming text chat, push-to-talk voice, and smart home widgets. No wake word or voice hardware needed — type a question or hold a button to speak.

- **Text chat** with real-time streaming responses and markdown rendering
- **Push-to-talk voice** — hold a button to speak, release to send (requires STT/TTS service)
- **LiveKit WebRTC** — optional always-on voice streaming for hands-free browser interaction
- **Smart home widgets** — climate control, media playback, sensor readings directly in the interface
- **Owner/Guest mode** — automatic access scoping based on guest bookings
- **Music integration** — search and play music directly in the browser

Best for: desktop and mobile access, chatbot deployments, development and testing, guest-facing kiosks.

### Voice Assistant

Wake-word-activated voice control through dedicated hardware. Say "Hey Jarvis" and ask your question — the system automatically routes to the right model based on query complexity.

- **Wyoming protocol** integration for hardware voice devices
- **Wake word detection** — "Hey Jarvis" activates listening
- **Continued conversation** — after responding, keeps listening for follow-up questions
- **Multi-zone coverage** — independent voice devices per room
- **TTS normalization** — converts abbreviations, addresses, and numbers to natural speech

Best for: hands-free operation, whole-home coverage, smart home control.

### API

OpenAI-compatible REST endpoints for building custom integrations, connecting to Home Assistant, or building your own frontend.

```bash
# OpenAI-compatible endpoint (works with any OpenAI client library)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "athena", "messages": [{"role": "user", "content": "What is the weather?"}]}'

# Direct orchestrator endpoint with streaming
curl -X POST http://localhost:8001/query/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the weather?", "mode": "owner", "interface_type": "chat"}'
```

Best for: Home Assistant integration, custom applications, automation scripts.

## Architecture

```
                     ┌──────────────────────────────┐
                     │          Interfaces           │
                     │                               │
                     │  Jarvis Web  (text / voice)   │
                     │  Wyoming     (voice hardware) │
                     │  API         (programmatic)   │
                     └──────────────┬────────────────┘
                                    │
                     ┌──────────────▼────────────────┐
                     │           Gateway              │
                     │    Rate Limiting               │
                     │    Circuit Breaker             │
                     │    OpenAI-Compatible API       │
                     └──────────────┬────────────────┘
                                    │
        ┌───────────────────────────▼──────────────────────────┐
        │                  Orchestrator                         │
        │             (LangGraph State Machine)                 │
        │                                                       │
        │  ┌────────────┐  ┌────────────┐  ┌────────────────┐  │
        │  │  Classify   │→│  Retrieve  │→│    Validate     │  │
        │  └────────────┘  └────────────┘  └────────────────┘  │
        │       │                                │              │
        │  ┌────▼───────┐                  ┌─────▼──────────┐  │
        │  │ Complexity  │                 │    Anti-        │  │
        │  │ Detector    │                 │  Hallucination  │  │
        │  │ (no LLM)    │                 │    Pipeline     │  │
        │  └─────────────┘                 └────────────────┘  │
        └──┬───────────────┬─────────────────┬─────────────────┘
           │               │                 │
  ┌────────▼───┐   ┌───────▼───────┐   ┌─────▼───────────┐
  │  LLM       │   │  23 RAG       │   │  Home           │
  │  Router    │   │  Services     │   │  Assistant      │
  │            │   │               │   │  Client         │
  │ simple →   │   │ Weather       │   │                 │
  │  local 4B  │   │ Sports        │   │ Lights          │
  │ complex →  │   │ Dining        │   │ Locks           │
  │  local 14B │   │ Flights ...   │   │ Climate         │
  │  or cloud  │   │               │   │ Media           │
  │  API       │   │               │   │ Scenes          │
  └────────────┘   └───────────────┘   └─────────────────┘
```

### Request Flow

1. User sends a query via Jarvis Web, voice device, or API call
2. Speech-to-text transcribes audio locally (voice interfaces only)
3. **Gateway** applies rate limiting, circuit breaking, and routes to orchestrator
4. **Orchestrator** runs 6-layer deterministic preprocessing (STT error correction, slang normalization, false memory detection, emotional context, pattern classification) before any LLM call
5. **Complexity detector** scores the query (regex-only, no LLM) and selects the appropriate model tier
6. **RAG services** fetch real-time data from external APIs
7. **LLM synthesizes** a natural response using retrieved data
8. **Validation pipeline** checks for hallucinated facts against source data
9. Response streams back to the interface — text for chat, speech-normalized audio for voice
10. Voice interfaces continue listening for follow-up questions

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

### Chat-Only Setup

The fastest path to a working Athena instance — text chat with full RAG capabilities, no voice hardware required.

```bash
git clone https://github.com/jstuart0/project-athena-oss.git
cd project-athena-oss

# Configure environment
cp .env.example .env
# Edit .env — set required values:
#   ATHENA_DB_PASSWORD, ADMIN_API_URL, ENCRYPTION_KEY,
#   ENCRYPTION_SALT, SESSION_SECRET_KEY, JWT_SECRET

# Disable modules you don't need:
#   MODULE_HOME_ASSISTANT=false   # skip if no Home Assistant
#   MODULE_JARVIS_WEB=true        # enable the chat interface
```

**Option A: Docker Compose**

```bash
docker compose up -d
```

**Option B: Manual**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1: Admin backend
cd admin/backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8080

# Terminal 2: Orchestrator
cd src/orchestrator && python -m uvicorn main:app --host 0.0.0.0 --port 8001

# Terminal 3: Gateway
cd src/gateway && python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 4: Jarvis Web interface
cd apps/jarvis-web/backend && pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 3001
```

Open `http://localhost:3001` to start chatting. Add RAG services (weather, sports, dining, etc.) as needed — each runs independently on its own port.

### Full Setup (Voice + Chat + RAG)

```bash
git clone https://github.com/jstuart0/project-athena-oss.git
cd project-athena-oss

cp .env.example .env
# Edit .env with your full configuration (see docs/CONFIGURATION.md)
```

**Option 1: Docker Compose (recommended)**

```bash
docker compose up -d
```

**Option 2: Manual**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start core services
cd src/gateway && python -m uvicorn main:app --host 0.0.0.0 --port 8000
cd src/orchestrator && python -m uvicorn main:app --host 0.0.0.0 --port 8001

# Start RAG services as needed
cd src/rag/weather && python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

**Option 3: Kubernetes**

Manifests are provided in `manifests/athena-prod/` for full cluster deployment.

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for detailed setup instructions including voice device configuration.

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
MODULE_HOME_ASSISTANT=true
MODULE_JARVIS_WEB=true
MODULE_GUEST_MODE=false
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for all options and [docs/MODULES.md](docs/MODULES.md) for the module reference.

## Jarvis Web Interface

Jarvis Web is a standalone web application that provides chat and voice access to the full Athena pipeline. It runs as a FastAPI backend serving a single-page frontend.

### Features

| Feature | Description |
|---------|-------------|
| **Text Chat** | Streaming responses via SSE with markdown rendering |
| **Push-to-Talk** | Hold-to-record voice input, transcribed locally via STT |
| **LiveKit Streaming** | Optional always-on WebRTC voice (requires LiveKit server) |
| **Smart Home Widgets** | Climate, media, and sensor controls embedded in the interface |
| **Guest Mode** | Automatic access scoping based on guest bookings |
| **Music Playback** | Search and stream music directly in the browser |

### How It Works

```
Browser → Jarvis Web Backend → Orchestrator → LLM + RAG Services
                              ↘ Home Assistant (for widgets)
```

The backend proxies chat requests to the Orchestrator and smart home requests to Home Assistant. All LLM processing happens server-side — the browser renders streamed responses.

### Deployment

**Local development:**

```bash
cd apps/jarvis-web/backend
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 3001
```

**Docker:**

```bash
cd apps/jarvis-web
docker build -t jarvis-web .
docker run -p 3001:8000 \
  -e ORCHESTRATOR_URL=http://host.docker.internal:8001 \
  -e GATEWAY_URL=http://host.docker.internal:8000 \
  jarvis-web
```

**Kubernetes:**

```bash
kubectl apply -f apps/jarvis-web/k8s/deployment.yaml
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCHESTRATOR_URL` | `http://localhost:8001` | Orchestrator endpoint |
| `GATEWAY_URL` | `http://localhost:8000` | Gateway endpoint (for LiveKit proxy) |
| `ADMIN_BACKEND_URL` | `http://localhost:8080` | Admin API (for guest mode) |
| `HA_URL` | — | Home Assistant URL (for smart home widgets) |
| `HA_TOKEN` | — | Home Assistant long-lived access token |
| `VOICE_API_URL` | — | STT/TTS endpoint (for push-to-talk voice) |
| `DEFAULT_ROOM` | `guest` | Room name sent to orchestrator |

Text chat works with just `ORCHESTRATOR_URL`. Voice features require `VOICE_API_URL`. Smart home widgets require `HA_URL` and `HA_TOKEN`.

## API Usage

### Gateway (OpenAI-Compatible)

```bash
# Works with any OpenAI-compatible client, Home Assistant, or custom apps
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "athena",
    "messages": [{"role": "user", "content": "What is the weather like?"}]
  }'
```

### Orchestrator (Direct)

```bash
# Text chat — use interface_type "chat" for detailed responses
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Turn on the living room lights and check the weather",
    "mode": "owner",
    "room": "office",
    "interface_type": "chat"
  }'

# Voice — use interface_type "voice" for TTS-normalized responses
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What time is the Orioles game?",
    "mode": "owner",
    "room": "kitchen",
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
│   └── jarvis-web/          # Chat interface with text, voice, and smart home widgets
│       ├── backend/         # FastAPI proxy to orchestrator and Home Assistant
│       ├── frontend/        # Single-page app (HTML + JS)
│       └── k8s/             # Kubernetes deployment manifests
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
| **No backend lock-in** | Each pipeline stage (classifier, synthesizer, validator) can target a different backend — local Ollama for simple queries, cloud API for complex ones, or all-local / all-cloud if you prefer |
| **Separate validation model** | A dedicated smaller model fact-checks the response model's output against source data |
| **Microservice RAG** | Each data domain has different caching, rate limits, and failure modes — monolithic RAG would be fragile |
| **OpenAI-compatible gateway** | Drop-in compatibility with Home Assistant and any OpenAI client library |
| **Environment-variable configuration** | Zero hardcoded values — fully configurable for any deployment |
| **Standalone chat interface** | Jarvis Web runs independently — use it without voice hardware, Wyoming devices, or Home Assistant |

## Hardware Requirements

**Minimum (chat-only, local models):**
- 16GB RAM, 4-core CPU
- Ollama with a 3-4B parameter model
- Works on Mac Mini, small Linux server, or NUC
- No voice hardware required

**Minimum (chat-only, cloud models):**
- Any machine that can run Python and PostgreSQL
- An API key for OpenAI, Anthropic, or any OpenAI-compatible provider
- No GPU or large RAM required — LLM inference happens remotely

**Recommended (full experience):**
- Apple Silicon Mac with 32-64GB unified memory (for local LLM inference)
- Dedicated machine for vector DB and Redis cache
- 10GbE network for low-latency inter-service communication
- Wyoming-compatible voice devices for hands-free operation
- Mix local and cloud models per pipeline stage to optimize cost vs. latency

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

---

## Did it work?

If Athena is running on your hardware, let us know:

- **It worked?** Give the repo a star — it helps others find the project
- **Something broke?** [Open an issue](https://github.com/jstuart0/project-athena-oss/issues) — we want to fix it
- **Have ideas?** [Start a discussion](https://github.com/jstuart0/project-athena-oss/discussions)
