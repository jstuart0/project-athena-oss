"""
Base RAG Service with Database Configuration
Loads configuration from PostgreSQL and provides caching
"""

import os
import json
import httpx
import asyncio
import hashlib
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import asyncpg
import redis.asyncio as redis
import structlog

logger = structlog.get_logger()


class BaseRAGService:
    """Base class for all RAG services with database configuration"""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.config: Dict[str, Any] = {}
        self.templates: List[Dict] = []
        self.parameters: List[Dict] = []
        self.db_pool: Optional[asyncpg.Pool] = None
        self.redis_client: Optional[redis.Redis] = None
        self.http_client: Optional[httpx.AsyncClient] = None
        self.last_config_refresh = None

    async def initialize(self):
        """Initialize the RAG service"""
        # Connect to database
        import urllib.parse
        db_url = os.getenv('DATABASE_URL')
        if not db_url:
            # Build connection URL from environment variables (no hardcoded defaults)
            db_password = os.getenv('ATHENA_DB_PASSWORD')
            if not db_password:
                raise ValueError("ATHENA_DB_PASSWORD environment variable must be set")
            password = urllib.parse.quote(db_password, safe='')
            db_user = os.getenv('ATHENA_DB_USER', 'psadmin')
            db_host = os.getenv('ATHENA_DB_HOST', 'localhost')
            db_port = os.getenv('ATHENA_DB_PORT', '5432')
            db_name = os.getenv('ATHENA_DB_NAME', 'athena')
            db_url = f'postgresql://{db_user}:{password}@{db_host}:{db_port}/{db_name}'
        self.db_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)

        # Connect to Redis
        redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
        self.redis_client = redis.from_url(redis_url)

        # Create HTTP client
        self.http_client = httpx.AsyncClient(timeout=30.0)

        # Load configuration
        await self.load_configuration()

        # Start configuration refresh task
        asyncio.create_task(self._refresh_config_periodically())

        logger.info(f"RAG service {self.service_name} initialized")

    async def load_configuration(self):
        """Load service configuration from database"""
        try:
            async with self.db_pool.acquire() as conn:
                # Load service configuration
                service_row = await conn.fetchrow("""
                    SELECT * FROM rag_services
                    WHERE name = $1 AND enabled = true
                """, self.service_name)

                if service_row:
                    self.config = dict(service_row)

                    # Load service parameters
                    self.parameters = [
                        dict(row) for row in await conn.fetch("""
                            SELECT * FROM rag_service_params
                            WHERE service_id = $1
                        """, self.config['id'])
                    ]

                    # Load response templates
                    self.templates = [
                        dict(row) for row in await conn.fetch("""
                            SELECT * FROM rag_response_templates
                            WHERE service_id = $1 AND enabled = true
                            ORDER BY priority DESC
                        """, self.config['id'])
                    ]

                    self.last_config_refresh = datetime.now()
                    logger.info(
                        f"Loaded configuration for {self.service_name}",
                        endpoint=self.config.get('endpoint_url'),
                        templates=len(self.templates),
                        parameters=len(self.parameters)
                    )
                else:
                    logger.warning(f"No configuration found for service {self.service_name}")

        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")

    async def _refresh_config_periodically(self):
        """Refresh configuration every 5 minutes"""
        while True:
            await asyncio.sleep(300)  # 5 minutes
            await self.load_configuration()

    def _get_cache_key(self, query: str, params: Dict[str, Any]) -> str:
        """Generate cache key for query and parameters"""
        cache_data = f"{self.service_name}:{query}:{json.dumps(params, sort_keys=True)}"
        return hashlib.md5(cache_data.encode()).hexdigest()

    async def _get_cached_response(self, cache_key: str) -> Optional[Dict]:
        """Get cached response from Redis"""
        if not self.redis_client:
            return None

        try:
            cached = await self.redis_client.get(cache_key)
            if cached:
                logger.debug(f"Cache hit for {self.service_name}")
                return json.loads(cached)
        except Exception as e:
            logger.error(f"Cache retrieval error: {e}")

        return None

    async def _cache_response(self, cache_key: str, response: Dict):
        """Cache response in Redis"""
        if not self.redis_client:
            return

        try:
            ttl = self.config.get('cache_ttl', 300)
            await self.redis_client.setex(
                cache_key,
                ttl,
                json.dumps(response)
            )
            logger.debug(f"Cached response for {self.service_name} (TTL: {ttl}s)")
        except Exception as e:
            logger.error(f"Cache storage error: {e}")

    async def _make_api_call(self, params: Dict[str, Any]) -> Dict:
        """Make API call to configured endpoint"""
        if not self.config.get('endpoint_url'):
            raise ValueError(f"No endpoint configured for {self.service_name}")

        url = self.config['endpoint_url']
        headers = self.config.get('headers', {})

        # Add API key if configured
        api_key = self.config.get('api_key_encrypted')
        if api_key:
            headers['Authorization'] = f"Bearer {api_key}"

        # Build query parameters
        query_params = {}
        for param in self.parameters:
            if param['param_type'] == 'query':
                value = params.get(param['param_name'], param.get('default_value'))
                if value:
                    query_params[param['param_name']] = value

        try:
            response = await self.http_client.get(
                url,
                headers=headers,
                params=query_params,
                timeout=self.config.get('timeout', 5000) / 1000
            )
            response.raise_for_status()
            return response.json()

        except httpx.TimeoutException:
            logger.error(f"API call to {self.service_name} timed out")
            raise
        except Exception as e:
            logger.error(f"API call to {self.service_name} failed: {e}")
            raise

    def _apply_template(self, template: Dict, data: Dict) -> str:
        """Apply response template to API data"""
        template_text = template.get('template_text', '')
        variables = template.get('variables', {})

        # Simple variable substitution
        result = template_text
        for var_name, var_path in variables.items():
            # Extract value from data using dot notation
            value = self._extract_value(data, var_path)
            result = result.replace(f"{{{var_name}}}", str(value))

        return result

    def _extract_value(self, data: Dict, path: str) -> Any:
        """Extract value from nested dict using dot notation"""
        parts = path.split('.')
        value = data

        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                idx = int(part)
                value = value[idx] if idx < len(value) else None
            else:
                return None

        return value

    async def query(self, query: str, intent: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Query the RAG service"""
        if not self.config.get('enabled', False):
            return {
                'success': False,
                'error': f"Service {self.service_name} is disabled"
            }

        # Build parameters
        params = {'query': query}
        params.update(kwargs)

        # Check cache
        cache_key = self._get_cache_key(query, params)
        cached = await self._get_cached_response(cache_key)
        if cached:
            return cached

        try:
            # Make API call
            api_response = await self._make_api_call(params)

            # Find matching template
            template = None
            for tmpl in self.templates:
                if not intent or tmpl.get('intent_category') == intent:
                    template = tmpl
                    break

            # Apply template if found
            if template:
                formatted_response = self._apply_template(template, api_response)
            else:
                formatted_response = json.dumps(api_response)

            result = {
                'success': True,
                'data': api_response,
                'formatted': formatted_response,
                'service': self.service_name,
                'cached': False
            }

            # Cache the result
            await self._cache_response(cache_key, result)

            # Update health status
            await self._update_health_status(True)

            return result

        except Exception as e:
            logger.error(f"Query failed for {self.service_name}: {e}")
            await self._update_health_status(False)

            return {
                'success': False,
                'error': str(e),
                'service': self.service_name
            }

    async def _update_health_status(self, is_healthy: bool):
        """Update service health status in database"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    UPDATE rag_services
                    SET health_status = $1,
                        last_health_check = CURRENT_TIMESTAMP
                    WHERE name = $2
                """, 'healthy' if is_healthy else 'unhealthy', self.service_name)
        except Exception as e:
            logger.error(f"Failed to update health status: {e}")

    async def health_check(self) -> bool:
        """Perform health check on the service"""
        if not self.config.get('health_check_url'):
            # No health check configured, assume healthy if config exists
            return bool(self.config)

        try:
            response = await self.http_client.get(
                self.config['health_check_url'],
                timeout=5.0
            )
            is_healthy = response.status_code == 200
            await self._update_health_status(is_healthy)
            return is_healthy
        except Exception:
            await self._update_health_status(False)
            return False

    async def close(self):
        """Clean up resources"""
        if self.http_client:
            await self.http_client.aclose()
        if self.redis_client:
            await self.redis_client.close()
        if self.db_pool:
            await self.db_pool.close()