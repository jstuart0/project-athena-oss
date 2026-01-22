# Project Athena - Applications Directory

This directory contains all the microservices and applications for the new Mac Studio/mini implementation.

## Directory Structure

```
apps/
├── gateway/           # LiteLLM OpenAI-compatible gateway
├── orchestrator/      # LangGraph orchestration service
├── rag/              # RAG microservices
│   ├── weather/      # OpenWeatherMap integration
│   ├── airports/     # FlightAware airport data
│   └── sports/       # TheSportsDB integration
├── shared/           # Shared utilities and clients
├── validators/       # Anti-hallucination validators
└── share-service/    # SMS/Email share service
```

## Component Descriptions

### Gateway (`apps/gateway/`)
- **Purpose:** OpenAI-compatible API gateway using LiteLLM
- **Technology:** LiteLLM proxy server
- **Configuration:** Model routing, fallbacks, retries
- **Port:** 8000
- **Deployment:** Docker container on Mac Studio

### Orchestrator (`apps/orchestrator/`)
- **Purpose:** LangGraph-based state machine for query processing
- **Technology:** Python, LangGraph, FastAPI
- **Flow:** Classify → Route → Retrieve → Synthesize → Validate → Finalize
- **State Management:** Per-request state with trace IDs
- **Port:** 8001
- **Deployment:** Docker container on Mac Studio

### RAG Services (`apps/rag/`)
Each RAG service is a standalone microservice providing domain-specific knowledge:

#### Weather (`apps/rag/weather/`)
- **API:** OpenWeatherMap
- **Cache:** 48-hour TTL
- **Queries:** Current conditions, forecasts, alerts
- **Port:** 8010

#### Airports (`apps/rag/airports/`)
- **API:** FlightAware
- **Cache:** 1-hour TTL
- **Airports:** PHL, BWI, EWR, LGA, JFK, IAD, DCA
- **Port:** 8011

#### Sports (`apps/rag/sports/`)
- **API:** TheSportsDB
- **Cache:** 15-min (live), 24-hour (historical)
- **Teams:** Baltimore Ravens, Baltimore Orioles
- **Port:** 8012

### Shared (`apps/shared/`)
- **Purpose:** Common utilities and clients used across services
- **Contents:**
  - Home Assistant client (async)
  - Qdrant vector database client
  - Redis cache utilities
  - Prometheus metrics
  - Logging configuration

### Validators (`apps/validators/`)
- **Purpose:** Anti-hallucination and response validation
- **Phase 1:** Basic fact checking (keywords, entity verification)
- **Phase 2:** Advanced validation (LLM cross-validation, confidence scoring)
- **Port:** 8020

### Share Service (`apps/share-service/`)
- **Purpose:** SMS and email sharing for guest experiences
- **Technology:** Twilio (SMS), SMTP (email)
- **Phase 1:** Stubbed (no-op)
- **Phase 2+:** Full implementation
- **Port:** 8030

## Development Workflow

### Creating a New Service

1. **Create service directory:**
   ```bash
   mkdir -p apps/my-service
   cd apps/my-service
   ```

2. **Add Python files:**
   ```
   apps/my-service/
   ├── __init__.py
   ├── main.py          # FastAPI entrypoint
   ├── handler.py       # Core logic
   ├── models.py        # Pydantic models
   ├── config.py        # Configuration
   └── Dockerfile       # Container definition
   ```

3. **Add dependencies:**
   ```bash
   # Create requirements.txt
   echo "fastapi==0.104.1" > requirements.txt
   echo "uvicorn==0.24.0" >> requirements.txt
   ```

4. **Create Docker configuration:**
   ```dockerfile
   FROM python:3.11-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   COPY . .
   CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
   ```

5. **Add to docker-compose:**
   ```yaml
   my-service:
     build: ./apps/my-service
     ports:
       - "8040:8000"
     environment:
       - LOG_LEVEL=INFO
   ```

## Migration from Jetson Code

The old Jetson implementation (archived in `archive/jetson-implementation` branch) contains:
- 43 versions of intent classifiers
- Multiple RAG handlers (weather, airports, sports, etc.)
- Comprehensive test suites

**Migration process:**
1. Reference old handler in `archive/jetson-implementation:src/jetson/facade/handlers/`
2. Extract core logic and API integration
3. Adapt to new microservice structure
4. Add async support and health checks
5. Implement Prometheus metrics
6. Test with integration suite

**Example migration:**
```bash
# View old handler
git show archive/jetson-implementation:src/jetson/facade/handlers/weather.py

# Extract patterns and adapt to new structure
```

## Testing

Each service should include:
- Unit tests (`test_handler.py`)
- Integration tests (`test_api.py`)
- Load tests (optional for Phase 1)

Run tests:
```bash
cd apps/weather
pytest tests/
```

## Deployment

### Local Development
```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Restart service
docker-compose restart weather
```

### Production (Phase 2)
- Kubernetes manifests in `/manifests/`
- Helm charts for configuration
- Monitoring with Prometheus + Grafana

## Documentation

Each service directory should contain:
- `README.md` - Service overview and API documentation
- `CHANGELOG.md` - Version history
- `CONFIG.md` - Configuration options

## Related Documentation

- **Phase 1 Implementation:** `thoughts/shared/plans/2025-11-11-phase1-core-services-implementation.md`
- **Component Deep-Dive:** `thoughts/shared/plans/2025-11-11-component-deep-dive-plans.md`
- **Full Bootstrap:** `thoughts/shared/plans/2025-11-11-full-bootstrap-implementation.md`
- **Architecture:** `docs/ARCHITECTURE.md`

---

**Status:** Directory structure created, awaiting Phase 1 implementation
**Last Updated:** 2025-11-11
