"""Price Comparison RAG Service.

Aggregates product prices from multiple free sources to find the lowest price.

API Endpoints:
- GET /health - Health check
- GET /search - Search for product prices
- GET /compare - Compare prices by UPC/barcode
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.cache import cached, CacheClient
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

from providers.base import PriceProvider, PriceResult
from providers.rapidapi import RapidAPIPriceProvider, GoogleShoppingProvider
from providers.webscraper import DuckDuckGoShoppingProvider, BraveShoppingProvider

# Query expansion for common product abbreviations
# Maps short/ambiguous queries to more specific product searches
QUERY_EXPANSIONS = {
    # Gaming consoles
    "ps5": "PlayStation 5 console",
    "playstation 5": "PlayStation 5 console",
    "playstation5": "PlayStation 5 console",
    "ps4": "PlayStation 4 console",
    "playstation 4": "PlayStation 4 console",
    "xbox series x": "Xbox Series X console",
    "xbox series s": "Xbox Series S console",
    "xbox one": "Xbox One console",
    "switch": "Nintendo Switch console",
    "nintendo switch": "Nintendo Switch console",
    "switch oled": "Nintendo Switch OLED console",
    "steam deck": "Steam Deck gaming handheld",

    # Phones
    "iphone 15": "iPhone 15 smartphone",
    "iphone 15 pro": "iPhone 15 Pro smartphone",
    "iphone 15 pro max": "iPhone 15 Pro Max smartphone",
    "iphone 16": "iPhone 16 smartphone",
    "iphone 16 pro": "iPhone 16 Pro smartphone",
    "iphone 16 pro max": "iPhone 16 Pro Max smartphone",

    # Tablets
    "ipad": "Apple iPad tablet",
    "ipad pro": "Apple iPad Pro tablet",
    "ipad air": "Apple iPad Air tablet",

    # Computers
    "macbook": "Apple MacBook laptop",
    "macbook pro": "Apple MacBook Pro laptop",
    "macbook air": "Apple MacBook Air laptop",
}

# Minimum expected prices for known products to filter out obvious mismatches
# (e.g., filtering out $35 PS5 games when searching for the console)
MINIMUM_PRICES = {
    "playstation 5 console": 400,
    "ps5": 400,
    "playstation 4 console": 200,
    "ps4": 200,
    "xbox series x console": 400,
    "xbox series s console": 250,
    "xbox one console": 150,
    "nintendo switch console": 250,
    "nintendo switch oled console": 300,
    "steam deck": 350,
    "iphone": 400,
    "ipad": 300,
    "macbook": 800,
}


def expand_query(query: str) -> str:
    """Expand ambiguous product queries to more specific searches."""
    query_lower = query.lower().strip()

    # Check for exact matches
    if query_lower in QUERY_EXPANSIONS:
        return QUERY_EXPANSIONS[query_lower]

    # Check for partial matches (e.g., "lowest price on ps5" contains "ps5")
    for abbrev, expansion in QUERY_EXPANSIONS.items():
        if abbrev in query_lower and "console" not in query_lower:
            # Replace the abbreviation with the expansion
            return query_lower.replace(abbrev, expansion)

    return query


def get_minimum_price(query: str) -> float:
    """Get minimum expected price for a product query."""
    query_lower = query.lower()

    for product, min_price in MINIMUM_PRICES.items():
        if product in query_lower:
            return min_price

    return 0  # No minimum if product not recognized


# Configure logging
setup_logging(service_name="price-compare-rag")
logger = structlog.get_logger()

SERVICE_NAME = "price-compare"
SERVICE_PORT = int(os.getenv("PRICE_COMPARE_PORT", "8033"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Global clients
cache: Optional[CacheClient] = None
admin_client = None
providers: List[PriceProvider] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan."""
    global cache, admin_client, providers

    logger.info("price_compare_service.startup", msg="Initializing Price Comparison RAG service")

    # Register service
    await startup_service(SERVICE_NAME, SERVICE_PORT, "Price Comparison Service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Initialize providers based on available API keys
    providers = []

    # Always add free providers (no API key required)
    providers.append(DuckDuckGoShoppingProvider())
    logger.info("provider_added", provider="duckduckgo", requires_key=False)

    # Try to add Brave (uses existing websearch key)
    try:
        brave_config = await admin_client.get_external_api_key("brave-search")
        if brave_config and brave_config.get("api_key"):
            providers.append(BraveShoppingProvider(brave_config["api_key"]))
            logger.info("provider_added", provider="brave-shopping", requires_key=True)
    except Exception as e:
        logger.warning("brave_key_not_available", error=str(e))

    # Try to add RapidAPI price comparison
    try:
        rapidapi_config = await admin_client.get_external_api_key("rapidapi-price")
        if rapidapi_config and rapidapi_config.get("api_key"):
            providers.append(RapidAPIPriceProvider(rapidapi_config["api_key"]))
            logger.info("provider_added", provider="rapidapi", requires_key=True)
    except Exception as e:
        logger.warning("rapidapi_key_not_available", error=str(e))

    # Try to add Google Shopping (via SerpAPI)
    try:
        serpapi_config = await admin_client.get_external_api_key("serpapi")
        if serpapi_config and serpapi_config.get("api_key"):
            providers.append(GoogleShoppingProvider(serpapi_config["api_key"]))
            logger.info("provider_added", provider="google-shopping", requires_key=True)
    except Exception as e:
        logger.warning("serpapi_key_not_available", error=str(e))

    logger.info("providers_initialized", count=len(providers))

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    logger.info("price_compare_service.startup.complete")

    yield

    # Cleanup
    logger.info("price_compare_service.shutdown")
    await unregister_service(SERVICE_NAME)

    for provider in providers:
        await provider.close()

    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="Price Comparison RAG Service",
    description="Find lowest prices across multiple free sources",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "price-compare-rag",
            "providers_active": len(providers),
            "provider_names": [p.name for p in providers]
        }
    )


@cached(ttl=1800, key_prefix="price_search")
async def aggregate_prices(query: str, max_results: int = 20) -> List[Dict[str, Any]]:
    """
    Search all providers and aggregate results.

    Args:
        query: Product search query
        max_results: Maximum results to return

    Returns:
        List of price results sorted by total price (lowest first)
    """
    # Expand the query if it's an abbreviation
    original_query = query
    expanded_query = expand_query(query)

    if expanded_query != query:
        logger.info("query_expanded", original=query, expanded=expanded_query)
        query = expanded_query

    # Get minimum price filter for known products
    min_price = get_minimum_price(query)
    if min_price > 0:
        logger.info("price_filter_applied", query=query, min_price=min_price)

    # Query all providers in parallel
    tasks = [provider.search(query) for provider in providers]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    # Aggregate results
    all_results: List[PriceResult] = []

    for i, results in enumerate(results_lists):
        if isinstance(results, Exception):
            logger.warning(
                "provider_failed",
                provider=providers[i].name,
                error=str(results)
            )
            continue

        for result in results:
            # Filter out zero/invalid prices
            if result.price <= 0:
                continue

            # Filter out prices below minimum (likely wrong products like games)
            if min_price > 0 and result.price < min_price:
                logger.debug(
                    "price_filtered_below_minimum",
                    product=result.product_name,
                    price=result.price,
                    min_price=min_price
                )
                continue

            all_results.append(result)

    # Sort by total price (including shipping)
    all_results.sort(key=lambda x: x.price + (x.shipping or 0))

    # Deduplicate by retailer (keep lowest price per retailer)
    seen_retailers = set()
    unique_results = []

    for result in all_results:
        retailer_key = result.retailer.lower().strip()
        if retailer_key not in seen_retailers:
            seen_retailers.add(retailer_key)
            unique_results.append(result)

    # Return top results
    return [r.to_dict() for r in unique_results[:max_results]]


@app.get("/search")
async def search_prices(
    query: str = Query(..., description="Product to search for"),
    max_results: int = Query(10, description="Maximum results", ge=1, le=50)
):
    """
    Search for product prices across all sources.

    Aggregates results from multiple free price comparison sources
    and returns them sorted by lowest total price.

    Parameters:
    - query: Product name or description (e.g., "iPhone 15 Pro 256GB")
    - max_results: Maximum number of results to return

    Returns:
        List of price results sorted by lowest price first
    """
    try:
        # Check if query will be expanded
        expanded_query = expand_query(query)
        min_price_filter = get_minimum_price(expanded_query)

        results = await aggregate_prices(query, max_results)

        logger.info(
            "price_compare_service.search.success",
            query=query,
            expanded_query=expanded_query if expanded_query != query else None,
            results_count=len(results)
        )

        # Calculate price summary
        prices = [r["price"] for r in results if r["price"] > 0]

        response = {
            "success": True,
            "query": query,
            "results_count": len(results),
            "summary": {
                "lowest_price": min(prices) if prices else None,
                "highest_price": max(prices) if prices else None,
                "average_price": sum(prices) / len(prices) if prices else None,
                "sources_searched": len(providers)
            },
            "results": results
        }

        # Add expansion info if query was expanded
        if expanded_query != query:
            response["query_expanded"] = expanded_query
            response["note"] = f"Searched for '{expanded_query}' to find the actual product"

        # Add filter info if price filter was applied
        if min_price_filter > 0:
            response["min_price_filter"] = min_price_filter

        # If no results for a known expensive product, add helpful message
        if len(results) == 0 and min_price_filter > 0:
            response["message"] = (
                f"I couldn't find reliable prices for {expanded_query}. "
                f"This product typically costs ${min_price_filter}+ USD. "
                f"I recommend checking major retailers directly: "
                f"Best Buy (bestbuy.com), Amazon (amazon.com), Walmart (walmart.com), or Target (target.com)."
            )

        return response

    except Exception as e:
        logger.error("price_compare_service.search.error", error=str(e), query=query)
        raise HTTPException(status_code=500, detail="Price search failed")


@app.get("/compare")
async def compare_by_upc(
    upc: str = Query(..., description="Product UPC/barcode"),
    max_results: int = Query(10, description="Maximum results", ge=1, le=50)
):
    """
    Compare prices by UPC/barcode.

    Looks up a specific product by its UPC code and finds prices
    across multiple retailers.

    Parameters:
    - upc: UPC or barcode number
    - max_results: Maximum number of results to return

    Returns:
        List of price results for the specific product
    """
    # For UPC, we just search using the UPC as the query
    # Most providers will find the specific product
    return await search_prices(query=upc, max_results=max_results)


@app.get("/providers")
async def list_providers():
    """List active price comparison providers."""
    return {
        "providers": [
            {
                "name": p.name,
                "requires_api_key": p.requires_api_key
            }
            for p in providers
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None
    )
