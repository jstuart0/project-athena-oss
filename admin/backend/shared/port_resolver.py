"""
Port Resolution Utility

Provides proper port resolution following this hierarchy:
1. Service Registry Database (primary source)
2. Standard ports from quick_start_services.sh (fallback)
3. Environment variables (last resort)
"""

import os
import asyncio
import asyncpg
from typing import Optional
import structlog

logger = structlog.get_logger()

# Standard port assignments from quick_start_services.sh
STANDARD_PORTS = {
    "weather": 8010,
    "airports": 8011,
    "stocks": 8012,
    "flights": 8013,
    "events": 8014,
    "streaming": 8015,
    "news": 8016,
    "sports": 8017,
    "websearch": 8018,
    "dining": 8019,
    "recipes": 8020,
}

async def get_service_port(service_name: str) -> int:
    """
    Get the port for a service following proper hierarchy:
    1. Check service registry database
    2. Use standard port from quick_start_services.sh
    3. Fall back to environment variable (last resort)

    Args:
        service_name: Name of the service (e.g., "weather", "airports")

    Returns:
        Port number to use for the service
    """

    # 1. Try to get from service registry database
    try:
        conn = await asyncpg.connect(
            host=os.getenv('ATHENA_DB_HOST', 'localhost'),
            port=int(os.getenv('ATHENA_DB_PORT', '5432')),
            user=os.getenv('ATHENA_DB_USER', 'psadmin'),
            password=os.getenv('ATHENA_DB_PASSWORD'),  # No default - must be set in environment
            database='athena'
        )

        try:
            # Query for the service's registered port
            result = await conn.fetchrow("""
                SELECT endpoint_url
                FROM rag_services
                WHERE name = $1 AND enabled = true
            """, service_name)

            if result and result['endpoint_url']:
                # Extract port from URL (format: http://host:port)
                url = result['endpoint_url']
                if ':' in url:
                    port_str = url.rsplit(':', 1)[-1].split('/')[0]
                    try:
                        port = int(port_str)
                        logger.info(f"Port from database: {service_name} → {port}")
                        return port
                    except ValueError:
                        pass
        finally:
            await conn.close()
    except Exception as e:
        logger.debug(f"Database lookup failed (will use fallback): {e}")

    # 2. Use standard port from quick_start_services.sh
    if service_name in STANDARD_PORTS:
        port = STANDARD_PORTS[service_name]
        logger.info(f"Using standard port: {service_name} → {port}")
        return port

    # 3. Last resort: environment variable
    env_port = os.getenv("SERVICE_PORT")
    if env_port:
        try:
            port = int(env_port)
            logger.warning(f"Using environment variable (last resort): {service_name} → {port}")
            return port
        except ValueError:
            pass

    # 4. Ultimate fallback (should not reach here normally)
    default_port = 8000
    logger.error(f"No port found for {service_name}, using default: {default_port}")
    return default_port

def get_service_port_sync(service_name: str) -> int:
    """
    Synchronous wrapper for get_service_port.
    Creates event loop if needed (for use in __main__ blocks).
    """
    try:
        # Try to get existing event loop
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # Create new event loop if none exists
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(get_service_port(service_name))