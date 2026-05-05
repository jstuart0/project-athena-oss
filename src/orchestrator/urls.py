"""URL constants for Athena orchestrator RAG services and mode/notification services.

These are the 15 module-level URL constants previously declared in main.py
(lines 1075–1097). All values are read from environment variables at
module-import time with localhost fallbacks for local development.

CONTROL_AGENT_URL is intentionally excluded — it lives inside lifespan()
because it is lifecycle-coupled (used only after gateway startup logic runs).
"""
import os

# Phase 1 RAG Services
WEATHER_SERVICE_URL = os.getenv("RAG_WEATHER_URL", "http://localhost:8010")
ONECALL_SERVICE_URL = os.getenv("RAG_ONECALL_URL", "http://localhost:8021")
AIRPORTS_SERVICE_URL = os.getenv("RAG_AIRPORTS_URL", "http://localhost:8011")
FLIGHTS_SERVICE_URL = os.getenv("RAG_FLIGHTS_URL", "http://localhost:8012")

# Phase 2 RAG Services
EVENTS_SERVICE_URL = os.getenv("RAG_EVENTS_URL", "http://localhost:8013")
STREAMING_SERVICE_URL = os.getenv("RAG_STREAMING_URL", "http://localhost:8014")
NEWS_SERVICE_URL = os.getenv("RAG_NEWS_URL", "http://localhost:8015")
STOCKS_SERVICE_URL = os.getenv("RAG_STOCKS_URL", "http://localhost:8016")
SPORTS_SERVICE_URL = os.getenv("RAG_SPORTS_URL", "http://localhost:8017")
WEBSEARCH_SERVICE_URL = os.getenv("RAG_WEBSEARCH_URL", "http://localhost:8018")
DINING_SERVICE_URL = os.getenv("RAG_DINING_URL", "http://localhost:8019")
RECIPES_SERVICE_URL = os.getenv("RAG_RECIPES_URL", "http://localhost:8020")
DIRECTIONS_SERVICE_URL = os.getenv("RAG_DIRECTIONS_URL", "http://localhost:8030")

# Mode service
MODE_SERVICE_URL = os.getenv("MODE_SERVICE_URL", "http://localhost:8022")

# Notifications service (for proactive notification preferences)
NOTIFICATIONS_SERVICE_URL = os.getenv("NOTIFICATIONS_SERVICE_URL", "http://localhost:8050")
