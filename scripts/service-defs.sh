# shellcheck shell=bash
# Sourced by build-and-push.sh and smoke-rag-images.sh — keep side-effect-free.
# No set -e, no exit, no logging, no top-level execution.
#
# ADMIN_SERVICES uses ${PROJECT_ROOT} which callers must set before sourcing.
# RAG_SERVICES and CORE_SRC_SERVICES are repo-relative (no PROJECT_ROOT needed).
# The smoke script only reads RAG_SERVICES.

# Admin services (use their own directory as context; requires $PROJECT_ROOT)
ADMIN_SERVICES=(
    "athena-admin-backend:${PROJECT_ROOT}/admin/backend"
    "athena-admin-frontend:${PROJECT_ROOT}/admin/frontend"
    "athena-jarvis-web:${PROJECT_ROOT}:apps/jarvis-web/Dockerfile"
    "athena-chat-embed:${PROJECT_ROOT}/apps/chat-embed"
)

# Core services that need src/ context (have shared module dependency)
CORE_SRC_SERVICES=(
    "athena-gateway:gateway"
    "athena-orchestrator:orchestrator"
    "athena-mode-service:mode_service"
)

# RAG services - name:path (built with src/ context)
# Five image names diverge from their directory names:
#   athena-rag-sitescraper   -> rag/site_scraper
#   athena-rag-pricecompare  -> rag/price_compare
#   athena-rag-community     -> rag/community_events
#   athena-rag-seatgeek      -> rag/seatgeek_events
#   athena-rag-serpapi       -> rag/serpapi_events
RAG_SERVICES=(
    "athena-rag-weather:rag/weather"
    "athena-rag-airports:rag/airports"
    "athena-rag-stocks:rag/stocks"
    "athena-rag-flights:rag/flights"
    "athena-rag-events:rag/events"
    "athena-rag-streaming:rag/streaming"
    "athena-rag-news:rag/news"
    "athena-rag-sports:rag/sports"
    "athena-rag-websearch:rag/websearch"
    "athena-rag-dining:rag/dining"
    "athena-rag-recipes:rag/recipes"
    "athena-rag-onecall:rag/onecall"
    "athena-rag-seatgeek:rag/seatgeek_events"
    "athena-rag-transportation:rag/transportation"
    "athena-rag-community:rag/community_events"
    "athena-rag-amtrak:rag/amtrak"
    "athena-rag-tesla:rag/tesla"
    "athena-rag-media:rag/media"
    "athena-rag-directions:rag/directions"
    "athena-rag-sitescraper:rag/site_scraper"
    "athena-rag-serpapi:rag/serpapi_events"
    "athena-rag-pricecompare:rag/price_compare"
    "athena-rag-brightdata:rag/brightdata"
)
