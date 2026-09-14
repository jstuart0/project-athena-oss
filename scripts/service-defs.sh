# shellcheck shell=bash
# Sourced by build-and-push.sh, smoke-rag-images.sh and smoke-images.sh — keep
# side-effect-free. No set -e, no exit, no logging, no top-level execution.
#
# ADMIN_SERVICES uses ${PROJECT_ROOT} which callers must set before sourcing.
# RAG_SERVICES, CORE_SRC_SERVICES, SPECIAL_PYTHON_IMAGES and
# NON_PYTHON_DOCKERFILES are repo-relative (no PROJECT_ROOT needed).
# smoke-rag-images.sh reads RAG_SERVICES. smoke-images.sh (all 29 Python
# images) derives its population from RAG_SERVICES + CORE_SRC_SERVICES (both
# already "name:src-subdir", src/ context) plus SPECIAL_PYTHON_IMAGES for the
# three build recipes that shape doesn't cover.

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

# Non-Python Dockerfiles in the repo, declared so smoke-images.sh's
# --list-excluded output is an assertable fact rather than an implicit
# absence from --list. admin/frontend is `FROM nginx:alpine` (static assets
# behind nginx, no Python dependency environment to smoke).
NON_PYTHON_DOCKERFILES=(
    "admin/frontend/Dockerfile"
)

# Python images whose build recipe isn't "src/ context, name:src-subdir"
# (RAG_SERVICES and CORE_SRC_SERVICES already cover that shape). Consumed
# only by smoke-images.sh.
#   Format: name|dockerfile_relpath|context_relpath|needs_shared_copy
#   needs_shared_copy=1 replicates build-and-push.sh's admin-backend special
#   case: `cp -r src/shared <context>/shared` before build, removed after
#   (see build-and-push.sh's build_push, the athena-admin-backend branch).
SPECIAL_PYTHON_IMAGES=(
    "athena-admin-backend|admin/backend/Dockerfile|admin/backend|1"
    "athena-chat-embed|apps/chat-embed/Dockerfile|apps/chat-embed|0"
    "athena-jarvis-web|apps/jarvis-web/Dockerfile|.|0"
)
