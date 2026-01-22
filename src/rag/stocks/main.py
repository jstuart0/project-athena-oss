"""Stocks RAG Service - Alpha Vantage API Integration

Provides stock quotes, market data, and company information.

API Endpoints:
- GET /health - Health check
- GET /stocks/quote - Get real-time stock quote
- GET /stocks/intraday - Get intraday time series
- GET /stocks/daily - Get daily time series
- GET /stocks/search - Search for stock symbols
"""

import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
setup_logging(service_name="stocks-rag")
logger = structlog.get_logger()

SERVICE_NAME = "stocks"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8012"))
# Alpha Vantage API Configuration
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"

# Global clients (will be initialized in lifespan)
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifespan - initialize and cleanup resources.

    Setup:
    - Admin client for fetching API keys
    - HTTP client for Alpha Vantage API calls
    - Logging configuration

    Cleanup:
    - Close HTTP client and admin client connections
    """
    global http_client, admin_client, ALPHA_VANTAGE_API_KEY

    logger.info("stocks_service.startup", msg="Initializing Stocks RAG service")

    # Initialize admin client for configuration management
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("alpha-vantage")
        if api_config and api_config.get("api_key"):
            ALPHA_VANTAGE_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="alpha-vantage")
        else:
            logger.info("api_key_from_env", service="alpha-vantage")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="alpha-vantage")
        logger.info("api_key_from_env_fallback", service="alpha-vantage")

    # Validate API key
    if not ALPHA_VANTAGE_API_KEY:
        logger.warning(
            "stocks_service.config.missing_key",
            msg="ALPHA_VANTAGE_API_KEY not set - service will return errors"
        )

    # Initialize HTTP client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        params={"apikey": ALPHA_VANTAGE_API_KEY} if ALPHA_VANTAGE_API_KEY else {}
    )

    logger.info("stocks_service.startup.complete", msg="Stocks RAG service ready")

    yield  # Application runs here

    # Cleanup
    logger.info("stocks_service.shutdown", msg="Shutting down Stocks RAG service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

# Create FastAPI app
app = FastAPI(
    title="Stocks RAG Service",
    description="Stock market data and quotes via Alpha Vantage API",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=60)  # Cache for 1 minute (market data changes frequently)
async def get_stock_quote(symbol: str) -> Dict[str, Any]:
    """
    Get real-time stock quote for a symbol.

    Args:
        symbol: Stock symbol (e.g., "AAPL", "MSFT", "GOOGL")

    Returns:
        Dictionary containing quote data

    Raises:
        ValueError: If symbol is invalid or API key not configured
        httpx.HTTPStatusError: If Alpha Vantage API request fails
    """
    if not ALPHA_VANTAGE_API_KEY:
        raise ValueError("Alpha Vantage API key not configured")

    if not symbol or len(symbol) == 0:
        raise ValueError("Stock symbol is required")

    logger.info("stocks_service.get_quote", symbol=symbol)

    # Make API request
    response = await http_client.get(
        ALPHA_VANTAGE_BASE_URL,
        params={
            "function": "GLOBAL_QUOTE",
            "symbol": symbol.upper()
        }
    )
    response.raise_for_status()

    data = response.json()

    # Check for error
    if "Error Message" in data:
        raise ValueError(f"Invalid stock symbol: {symbol}")

    # Extract quote data
    quote = data.get("Global Quote", {})

    if not quote:
        raise ValueError(f"No quote data available for symbol: {symbol}")

    return {
        "symbol": quote.get("01. symbol"),
        "price": float(quote.get("05. price", 0)),
        "change": float(quote.get("09. change", 0)),
        "change_percent": quote.get("10. change percent", "0%").rstrip("%"),
        "volume": int(quote.get("06. volume", 0)),
        "latest_trading_day": quote.get("07. latest trading day"),
        "previous_close": float(quote.get("08. previous close", 0)),
        "open": float(quote.get("02. open", 0)),
        "high": float(quote.get("03. high", 0)),
        "low": float(quote.get("04. low", 0))
    }

@cached(ttl=300)  # Cache for 5 minutes
async def get_intraday_data(
    symbol: str,
    interval: str = "5min",
    outputsize: str = "compact"
) -> Dict[str, Any]:
    """
    Get intraday time series data for a stock.

    Args:
        symbol: Stock symbol
        interval: Time interval (1min, 5min, 15min, 30min, 60min)
        outputsize: compact (100 points) or full (complete history)

    Returns:
        Dictionary containing intraday time series data

    Raises:
        ValueError: If parameters are invalid
        httpx.HTTPStatusError: If Alpha Vantage API request fails
    """
    if not ALPHA_VANTAGE_API_KEY:
        raise ValueError("Alpha Vantage API key not configured")

    if not symbol:
        raise ValueError("Stock symbol is required")

    valid_intervals = ["1min", "5min", "15min", "30min", "60min"]
    if interval not in valid_intervals:
        raise ValueError(f"Invalid interval: {interval}. Must be one of {valid_intervals}")

    logger.info(
        "stocks_service.get_intraday",
        symbol=symbol,
        interval=interval,
        outputsize=outputsize
    )

    # Make API request
    response = await http_client.get(
        ALPHA_VANTAGE_BASE_URL,
        params={
            "function": "TIME_SERIES_INTRADAY",
            "symbol": symbol.upper(),
            "interval": interval,
            "outputsize": outputsize
        }
    )
    response.raise_for_status()

    data = response.json()

    # Check for error
    if "Error Message" in data:
        raise ValueError(f"Invalid stock symbol: {symbol}")

    # Extract time series data
    time_series_key = f"Time Series ({interval})"
    time_series = data.get(time_series_key, {})

    if not time_series:
        raise ValueError(f"No intraday data available for symbol: {symbol}")

    # Transform time series data
    data_points = []
    for timestamp, values in list(time_series.items())[:100]:  # Limit to 100 points
        data_points.append({
            "timestamp": timestamp,
            "open": float(values.get("1. open", 0)),
            "high": float(values.get("2. high", 0)),
            "low": float(values.get("3. low", 0)),
            "close": float(values.get("4. close", 0)),
            "volume": int(values.get("5. volume", 0))
        })

    metadata = data.get("Meta Data", {})

    return {
        "symbol": metadata.get("2. Symbol"),
        "interval": interval,
        "last_refreshed": metadata.get("3. Last Refreshed"),
        "data_points": data_points,
        "count": len(data_points)
    }

@cached(ttl=3600)  # Cache for 1 hour
async def get_daily_data(
    symbol: str,
    outputsize: str = "compact"
) -> Dict[str, Any]:
    """
    Get daily time series data for a stock.

    Args:
        symbol: Stock symbol
        outputsize: compact (100 days) or full (20+ years)

    Returns:
        Dictionary containing daily time series data

    Raises:
        ValueError: If parameters are invalid
        httpx.HTTPStatusError: If Alpha Vantage API request fails
    """
    if not ALPHA_VANTAGE_API_KEY:
        raise ValueError("Alpha Vantage API key not configured")

    if not symbol:
        raise ValueError("Stock symbol is required")

    logger.info(
        "stocks_service.get_daily",
        symbol=symbol,
        outputsize=outputsize
    )

    # Make API request
    response = await http_client.get(
        ALPHA_VANTAGE_BASE_URL,
        params={
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol.upper(),
            "outputsize": outputsize
        }
    )
    response.raise_for_status()

    data = response.json()

    # Check for error
    if "Error Message" in data:
        raise ValueError(f"Invalid stock symbol: {symbol}")

    # Extract time series data
    time_series = data.get("Time Series (Daily)", {})

    if not time_series:
        raise ValueError(f"No daily data available for symbol: {symbol}")

    # Transform time series data
    data_points = []
    for date, values in list(time_series.items())[:100]:  # Limit to 100 days
        data_points.append({
            "date": date,
            "open": float(values.get("1. open", 0)),
            "high": float(values.get("2. high", 0)),
            "low": float(values.get("3. low", 0)),
            "close": float(values.get("4. close", 0)),
            "volume": int(values.get("5. volume", 0))
        })

    metadata = data.get("Meta Data", {})

    return {
        "symbol": metadata.get("2. Symbol"),
        "last_refreshed": metadata.get("3. Last Refreshed"),
        "data_points": data_points,
        "count": len(data_points)
    }

@cached(ttl=86400)  # Cache for 24 hours
async def search_symbol(keywords: str) -> Dict[str, Any]:
    """
    Search for stock symbols by company name or keywords.

    Args:
        keywords: Search keywords (company name, etc.)

    Returns:
        Dictionary containing matching symbols

    Raises:
        ValueError: If keywords are invalid
        httpx.HTTPStatusError: If Alpha Vantage API request fails
    """
    if not ALPHA_VANTAGE_API_KEY:
        raise ValueError("Alpha Vantage API key not configured")

    if not keywords or len(keywords.strip()) == 0:
        raise ValueError("Search keywords are required")

    logger.info("stocks_service.search", keywords=keywords)

    # Make API request
    response = await http_client.get(
        ALPHA_VANTAGE_BASE_URL,
        params={
            "function": "SYMBOL_SEARCH",
            "keywords": keywords
        }
    )
    response.raise_for_status()

    data = response.json()

    # Extract search results
    best_matches = data.get("bestMatches", [])

    matches = []
    for match in best_matches:
        matches.append({
            "symbol": match.get("1. symbol"),
            "name": match.get("2. name"),
            "type": match.get("3. type"),
            "region": match.get("4. region"),
            "market_open": match.get("5. marketOpen"),
            "market_close": match.get("6. marketClose"),
            "timezone": match.get("7. timezone"),
            "currency": match.get("8. currency"),
            "match_score": float(match.get("9. matchScore", 0))
        })

    return {
        "matches": matches,
        "count": len(matches),
        "keywords": keywords
    }

@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        200 OK if service is healthy
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "stocks-rag",
            "api_key_configured": ALPHA_VANTAGE_API_KEY is not None
        }
    )

@app.get("/stocks/quote")
async def get_quote(
    symbol: str = Query(..., description="Stock symbol (e.g., AAPL, MSFT, GOOGL)")
):
    """
    Get real-time stock quote.

    Parameters:
    - symbol: Stock symbol (required)

    Returns:
        JSON response with quote data

    Raises:
        404: If symbol is invalid or not found
        502: If Alpha Vantage API is unavailable
        500: For unexpected errors
    """
    try:
        result = await get_stock_quote(symbol)

        logger.info(
            "stocks_service.quote.success",
            symbol=symbol,
            price=result.get("price")
        )

        return result

    except ValueError as e:
        logger.warning(
            "stocks_service.quote.invalid_request",
            error=str(e),
            symbol=symbol
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "stocks_service.quote.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Alpha Vantage API error: {e}")

    except Exception as e:
        logger.error(
            "stocks_service.quote.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/stocks/intraday")
async def get_intraday(
    symbol: str = Query(..., description="Stock symbol"),
    interval: str = Query("5min", description="Time interval (1min, 5min, 15min, 30min, 60min)"),
    outputsize: str = Query("compact", description="Output size (compact=100 points, full=all)")
):
    """
    Get intraday time series data.

    Parameters:
    - symbol: Stock symbol (required)
    - interval: Time interval (default: 5min)
    - outputsize: compact or full (default: compact)

    Returns:
        JSON response with intraday time series

    Raises:
        404: If symbol is invalid or parameters invalid
        502: If Alpha Vantage API is unavailable
        500: For unexpected errors
    """
    try:
        result = await get_intraday_data(symbol, interval, outputsize)

        logger.info(
            "stocks_service.intraday.success",
            symbol=symbol,
            interval=interval,
            data_points=len(result.get("data_points", []))
        )

        return result

    except ValueError as e:
        logger.warning(
            "stocks_service.intraday.invalid_request",
            error=str(e),
            symbol=symbol
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "stocks_service.intraday.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Alpha Vantage API error: {e}")

    except Exception as e:
        logger.error(
            "stocks_service.intraday.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/stocks/daily")
async def get_daily(
    symbol: str = Query(..., description="Stock symbol"),
    outputsize: str = Query("compact", description="Output size (compact=100 days, full=20+ years)")
):
    """
    Get daily time series data.

    Parameters:
    - symbol: Stock symbol (required)
    - outputsize: compact or full (default: compact)

    Returns:
        JSON response with daily time series

    Raises:
        404: If symbol is invalid
        502: If Alpha Vantage API is unavailable
        500: For unexpected errors
    """
    try:
        result = await get_daily_data(symbol, outputsize)

        logger.info(
            "stocks_service.daily.success",
            symbol=symbol,
            data_points=len(result.get("data_points", []))
        )

        return result

    except ValueError as e:
        logger.warning(
            "stocks_service.daily.invalid_request",
            error=str(e),
            symbol=symbol
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "stocks_service.daily.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Alpha Vantage API error: {e}")

    except Exception as e:
        logger.error(
            "stocks_service.daily.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/stocks/search")
async def search(
    keywords: str = Query(..., description="Search keywords (company name, etc.)")
):
    """
    Search for stock symbols.

    Parameters:
    - keywords: Search keywords (required)

    Returns:
        JSON response with matching symbols

    Raises:
        404: If keywords are invalid
        502: If Alpha Vantage API is unavailable
        500: For unexpected errors
    """
    try:
        result = await search_symbol(keywords)

        logger.info(
            "stocks_service.search.success",
            keywords=keywords,
            matches=len(result.get("matches", []))
        )

        return result

    except ValueError as e:
        logger.warning(
            "stocks_service.search.invalid_request",
            error=str(e),
            keywords=keywords
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "stocks_service.search.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Alpha Vantage API error: {e}")

    except Exception as e:
        logger.error(
            "stocks_service.search.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8016"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None  # Use structlog configuration
    )
