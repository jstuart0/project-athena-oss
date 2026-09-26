"""URL constants for Athena orchestrator RAG services and mode/notification services.

Single source of truth for every RAG service URL (ATHENA-87, F81). This is the
only module that reads `RAG_<NAME>_URL` / `<NAME>_RAG_URL` environment
variables; `orchestrator.rag_tools` and `orchestrator.utils.constants`
re-export these constants instead of reading their own env vars.

25 module-level URL constants: 23 RAG service URLs, MODE_SERVICE_URL and
NOTIFICATIONS_SERVICE_URL.

Env spelling: canonical is `RAG_<NAME>_URL` (matches every deployment
artifact: the manifest, the live Deployment, docker-compose.yml,
docs/CONFIGURATION.md). The legacy `<NAME>_RAG_URL` spelling is still
honoured for backward compatibility, with a WARNING logged. Canonical wins
when both are set and differ. A blank or whitespace-only value counts as
unset (docker-compose.yml passes `${RAG_*}` through, which arrives as `""`
for an unset host variable).

CONTROL_AGENT_URL is intentionally excluded — it lives inside lifespan()
because it is lifecycle-coupled (used only after gateway startup logic runs).
"""
import os

import structlog

logger = structlog.get_logger()


def _rag_url(canonical_name, canonical_value, legacy_name, legacy_value, default):
    """Resolve one RAG service URL from its canonical and legacy env names.

    Normalizes both values with `strip().rstrip("/")`; a blank result counts
    as unset. Canonical wins when both are set. Logs exactly one WARNING when
    only the legacy name is set, or when both are set and differ.
    """
    canonical = (canonical_value or "").strip().rstrip("/")
    legacy = (legacy_value or "").strip().rstrip("/") if legacy_name else ""

    if canonical:
        if legacy and legacy != canonical:
            logger.warning(
                "rag_url_env_conflict",
                canonical=canonical_name,
                legacy=legacy_name,
                value=canonical,
                conflicting_value=legacy,
            )
        return canonical

    if legacy:
        logger.warning(
            "rag_url_legacy_env_name",
            canonical=canonical_name,
            legacy=legacy_name,
            value=legacy,
        )
        return legacy

    return default


# RAG service URLs (23 constants; canonical env RAG_<NAME>_URL, legacy
# <NAME>_RAG_URL where one exists). Default ports match each service's own
# SERVICE_PORT default in src/rag/<svc>/main.py.
WEATHER_SERVICE_URL = _rag_url(
    "RAG_WEATHER_URL", os.getenv("RAG_WEATHER_URL"),
    "WEATHER_RAG_URL", os.getenv("WEATHER_RAG_URL"),
    "http://localhost:8010",
)
ONECALL_SERVICE_URL = _rag_url(
    "RAG_ONECALL_URL", os.getenv("RAG_ONECALL_URL"),
    None, None,
    "http://localhost:8021",
)
AIRPORTS_SERVICE_URL = _rag_url(
    "RAG_AIRPORTS_URL", os.getenv("RAG_AIRPORTS_URL"),
    "AIRPORTS_RAG_URL", os.getenv("AIRPORTS_RAG_URL"),
    "http://localhost:8011",
)
STOCKS_SERVICE_URL = _rag_url(
    "RAG_STOCKS_URL", os.getenv("RAG_STOCKS_URL"),
    "STOCKS_RAG_URL", os.getenv("STOCKS_RAG_URL"),
    "http://localhost:8012",
)
FLIGHTS_SERVICE_URL = _rag_url(
    "RAG_FLIGHTS_URL", os.getenv("RAG_FLIGHTS_URL"),
    "FLIGHTS_RAG_URL", os.getenv("FLIGHTS_RAG_URL"),
    "http://localhost:8013",
)
EVENTS_SERVICE_URL = _rag_url(
    "RAG_EVENTS_URL", os.getenv("RAG_EVENTS_URL"),
    "EVENTS_RAG_URL", os.getenv("EVENTS_RAG_URL"),
    "http://localhost:8014",
)
STREAMING_SERVICE_URL = _rag_url(
    "RAG_STREAMING_URL", os.getenv("RAG_STREAMING_URL"),
    "STREAMING_RAG_URL", os.getenv("STREAMING_RAG_URL"),
    "http://localhost:8015",
)
NEWS_SERVICE_URL = _rag_url(
    "RAG_NEWS_URL", os.getenv("RAG_NEWS_URL"),
    "NEWS_RAG_URL", os.getenv("NEWS_RAG_URL"),
    "http://localhost:8016",
)
SPORTS_SERVICE_URL = _rag_url(
    "RAG_SPORTS_URL", os.getenv("RAG_SPORTS_URL"),
    "SPORTS_RAG_URL", os.getenv("SPORTS_RAG_URL"),
    "http://localhost:8017",
)
WEBSEARCH_SERVICE_URL = _rag_url(
    "RAG_WEBSEARCH_URL", os.getenv("RAG_WEBSEARCH_URL"),
    "WEBSEARCH_RAG_URL", os.getenv("WEBSEARCH_RAG_URL"),
    "http://localhost:8018",
)
DINING_SERVICE_URL = _rag_url(
    "RAG_DINING_URL", os.getenv("RAG_DINING_URL"),
    "DINING_RAG_URL", os.getenv("DINING_RAG_URL"),
    "http://localhost:8019",
)
RECIPES_SERVICE_URL = _rag_url(
    "RAG_RECIPES_URL", os.getenv("RAG_RECIPES_URL"),
    "RECIPES_RAG_URL", os.getenv("RECIPES_RAG_URL"),
    "http://localhost:8020",
)
DIRECTIONS_SERVICE_URL = _rag_url(
    "RAG_DIRECTIONS_URL", os.getenv("RAG_DIRECTIONS_URL"),
    "DIRECTIONS_RAG_URL", os.getenv("DIRECTIONS_RAG_URL"),
    "http://localhost:8030",
)
COMMUNITY_EVENTS_SERVICE_URL = _rag_url(
    "RAG_COMMUNITY_URL", os.getenv("RAG_COMMUNITY_URL"),
    "COMMUNITY_EVENTS_RAG_URL", os.getenv("COMMUNITY_EVENTS_RAG_URL"),
    "http://localhost:8026",
)
SERPAPI_EVENTS_SERVICE_URL = _rag_url(
    "RAG_SERPAPI_URL", os.getenv("RAG_SERPAPI_URL"),
    "SERPAPI_EVENTS_RAG_URL", os.getenv("SERPAPI_EVENTS_RAG_URL"),
    "http://localhost:8032",
)
SEATGEEK_EVENTS_SERVICE_URL = _rag_url(
    "RAG_SEATGEEK_URL", os.getenv("RAG_SEATGEEK_URL"),
    "SEATGEEK_EVENTS_RAG_URL", os.getenv("SEATGEEK_EVENTS_RAG_URL"),
    "http://localhost:8024",
)
TRANSPORTATION_SERVICE_URL = _rag_url(
    "RAG_TRANSPORTATION_URL", os.getenv("RAG_TRANSPORTATION_URL"),
    "TRANSPORTATION_RAG_URL", os.getenv("TRANSPORTATION_RAG_URL"),
    "http://localhost:8025",
)
AMTRAK_SERVICE_URL = _rag_url(
    "RAG_AMTRAK_URL", os.getenv("RAG_AMTRAK_URL"),
    "AMTRAK_RAG_URL", os.getenv("AMTRAK_RAG_URL"),
    "http://localhost:8027",
)
SITE_SCRAPER_SERVICE_URL = _rag_url(
    "RAG_SITESCRAPER_URL", os.getenv("RAG_SITESCRAPER_URL"),
    "SITE_SCRAPER_RAG_URL", os.getenv("SITE_SCRAPER_RAG_URL"),
    "http://localhost:8031",
)
PRICE_COMPARE_SERVICE_URL = _rag_url(
    "RAG_PRICECOMPARE_URL", os.getenv("RAG_PRICECOMPARE_URL"),
    "PRICE_COMPARE_RAG_URL", os.getenv("PRICE_COMPARE_RAG_URL"),
    "http://localhost:8033",
)
TESLA_SERVICE_URL = _rag_url(
    "RAG_TESLA_URL", os.getenv("RAG_TESLA_URL"),
    "TESLA_RAG_URL", os.getenv("TESLA_RAG_URL"),
    "http://localhost:8028",
)
MEDIA_SERVICE_URL = _rag_url(
    "RAG_MEDIA_URL", os.getenv("RAG_MEDIA_URL"),
    "MEDIA_RAG_URL", os.getenv("MEDIA_RAG_URL"),
    "http://localhost:8029",
)
BRIGHTDATA_SERVICE_URL = _rag_url(
    "RAG_BRIGHTDATA_URL", os.getenv("RAG_BRIGHTDATA_URL"),
    "BRIGHTDATA_RAG_URL", os.getenv("BRIGHTDATA_RAG_URL"),
    "http://localhost:8040",
)

# Mode service
MODE_SERVICE_URL = os.getenv("MODE_SERVICE_URL", "http://localhost:8022")

# Notifications service (for proactive notification preferences)
NOTIFICATIONS_SERVICE_URL = os.getenv("NOTIFICATIONS_SERVICE_URL", "http://localhost:8050")
