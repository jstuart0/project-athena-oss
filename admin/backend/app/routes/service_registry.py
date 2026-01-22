"""Service Registry API routes."""
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional
import asyncpg
import os
import logging
import httpx
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/service-registry", tags=["service-registry"])

async def get_db_connection():
    """Get connection to the Athena database."""
    password = os.getenv('ATHENA_DB_PASSWORD')
    if not password:
        raise HTTPException(status_code=500, detail="ATHENA_DB_PASSWORD not configured")
    return await asyncpg.connect(
        host=os.getenv('ATHENA_DB_HOST', 'localhost'),
        port=int(os.getenv('ATHENA_DB_PORT', '5432')),
        user=os.getenv('ATHENA_DB_USER', 'psadmin'),
        password=password,
        database=os.getenv('ATHENA_DB_NAME', 'athena')
    )

@router.get("/services")
async def get_all_services() -> Dict[str, Any]:
    """Get all services from the registry with their status."""
    conn = await get_db_connection()

    try:
        # Fetch all services from the registry
        rows = await conn.fetch("""
            SELECT
                name,
                display_name,
                service_type,
                endpoint_url,
                enabled,
                cache_ttl,
                timeout,
                rate_limit,
                created_at,
                updated_at
            FROM rag_services
            ORDER BY name
        """)

        services = []
        healthy_count = 0
        total_count = 0

        # Check health status for each service
        for row in rows:
            service = dict(row)
            total_count += 1

            # Extract port from endpoint_url
            if service['endpoint_url']:
                try:
                    parsed = urlparse(service['endpoint_url'])
                    service['host'] = parsed.hostname
                    service['port'] = parsed.port or 80
                except:
                    service['host'] = 'unknown'
                    service['port'] = 0
            else:
                service['host'] = 'unknown'
                service['port'] = 0

            # Check if service is healthy by calling its health endpoint
            service['status'] = 'unknown'
            service['health_message'] = ''

            if service['enabled'] and service['endpoint_url']:
                try:
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        health_url = f"{service['endpoint_url']}/health"
                        response = await client.get(health_url)
                        if response.status_code == 200:
                            service['status'] = 'healthy'
                            healthy_count += 1
                            health_data = response.json()
                            service['health_message'] = health_data.get('message', 'Service is running')
                        else:
                            service['status'] = 'unhealthy'
                            service['health_message'] = f'Health check returned {response.status_code}'
                except httpx.ConnectError:
                    service['status'] = 'offline'
                    service['health_message'] = 'Cannot connect to service'
                except httpx.TimeoutException:
                    service['status'] = 'timeout'
                    service['health_message'] = 'Health check timed out'
                except Exception as e:
                    service['status'] = 'error'
                    service['health_message'] = str(e)
            elif not service['enabled']:
                service['status'] = 'disabled'
                service['health_message'] = 'Service is disabled'

            # Convert timestamps to ISO format
            if service.get('created_at'):
                service['created_at'] = service['created_at'].isoformat()
            if service.get('updated_at'):
                service['updated_at'] = service['updated_at'].isoformat()

            services.append(service)

        return {
            'services': services,
            'total_services': total_count,
            'healthy_services': healthy_count,
            'overall_health': 'healthy' if healthy_count == total_count else
                             'degraded' if healthy_count > 0 else 'unhealthy'
        }

    finally:
        await conn.close()

@router.get("/services/{service_name}")
async def get_service(service_name: str) -> Dict[str, Any]:
    """Get a single service by name with its status."""
    conn = await get_db_connection()

    try:
        row = await conn.fetchrow("""
            SELECT
                name,
                display_name,
                service_type,
                endpoint_url,
                enabled,
                cache_ttl,
                timeout,
                rate_limit,
                created_at,
                updated_at
            FROM rag_services
            WHERE name = $1
        """, service_name)

        if not row:
            raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

        service = dict(row)

        # Extract port from endpoint_url
        if service['endpoint_url']:
            try:
                parsed = urlparse(service['endpoint_url'])
                service['host'] = parsed.hostname
                service['port'] = parsed.port or 80
            except:
                service['host'] = 'unknown'
                service['port'] = 0
        else:
            service['host'] = 'unknown'
            service['port'] = 0

        # Check if service is healthy
        service['status'] = 'unknown'
        service['health_message'] = ''

        if service['enabled'] and service['endpoint_url']:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    health_url = f"{service['endpoint_url']}/health"
                    response = await client.get(health_url)
                    if response.status_code == 200:
                        service['status'] = 'healthy'
                        health_data = response.json()
                        service['health_message'] = health_data.get('message', 'Service is running')
                    else:
                        service['status'] = 'unhealthy'
                        service['health_message'] = f'Health check returned {response.status_code}'
            except httpx.ConnectError:
                service['status'] = 'offline'
                service['health_message'] = 'Cannot connect to service'
            except httpx.TimeoutException:
                service['status'] = 'timeout'
                service['health_message'] = 'Health check timed out'
            except Exception as e:
                service['status'] = 'error'
                service['health_message'] = str(e)
        elif not service['enabled']:
            service['status'] = 'disabled'
            service['health_message'] = 'Service is disabled'

        # Convert timestamps to ISO format
        if service.get('created_at'):
            service['created_at'] = service['created_at'].isoformat()
        if service.get('updated_at'):
            service['updated_at'] = service['updated_at'].isoformat()

        return service

    finally:
        await conn.close()


@router.get("/services/{service_name}/url")
async def get_service_url(service_name: str) -> Dict[str, Any]:
    """Get just the URL for a service (lightweight endpoint for service discovery)."""
    conn = await get_db_connection()

    try:
        row = await conn.fetchrow("""
            SELECT endpoint_url, enabled
            FROM rag_services
            WHERE name = $1
        """, service_name)

        if not row:
            raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

        if not row['enabled']:
            raise HTTPException(status_code=503, detail=f"Service {service_name} is disabled")

        return {
            'service': service_name,
            'url': row['endpoint_url']
        }

    finally:
        await conn.close()


@router.post("/services")
async def register_service(
    name: str,
    endpoint_url: str,
    display_name: Optional[str] = None,
    service_type: str = 'api',
    cache_ttl: int = 300,
    timeout: int = 5000,
    rate_limit: int = 100
) -> Dict[str, Any]:
    """Register or update a service in the registry."""
    conn = await get_db_connection()

    try:
        # Check if service exists
        existing = await conn.fetchrow(
            "SELECT id FROM rag_services WHERE name = $1",
            name
        )

        if existing:
            # Update existing service
            await conn.execute("""
                UPDATE rag_services
                SET endpoint_url = $2,
                    display_name = COALESCE($3, display_name),
                    service_type = $4,
                    cache_ttl = $5,
                    timeout = $6,
                    rate_limit = $7,
                    enabled = true,
                    updated_at = NOW()
                WHERE name = $1
            """, name, endpoint_url, display_name, service_type, cache_ttl, timeout, rate_limit)

            return {
                'service': name,
                'action': 'updated',
                'url': endpoint_url,
                'message': f"Service {name} has been updated"
            }
        else:
            # Insert new service
            await conn.execute("""
                INSERT INTO rag_services (
                    name, display_name, service_type, endpoint_url,
                    headers, cache_ttl, timeout, rate_limit, enabled
                )
                VALUES ($1, $2, $3, $4, '{"Content-Type": "application/json"}'::jsonb,
                        $5, $6, $7, true)
            """, name, display_name or name.replace('-', ' ').title(), service_type,
                 endpoint_url, cache_ttl, timeout, rate_limit)

            return {
                'service': name,
                'action': 'created',
                'url': endpoint_url,
                'message': f"Service {name} has been registered"
            }

    finally:
        await conn.close()


@router.post("/services/{service_name}/toggle")
async def toggle_service(service_name: str) -> Dict[str, Any]:
    """Enable or disable a service."""
    conn = await get_db_connection()

    try:
        # Get current state
        current = await conn.fetchrow(
            "SELECT enabled FROM rag_services WHERE name = $1",
            service_name
        )

        if not current:
            raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

        # Toggle the state
        new_state = not current['enabled']

        await conn.execute("""
            UPDATE rag_services
            SET enabled = $1, updated_at = NOW()
            WHERE name = $2
        """, new_state, service_name)

        return {
            'service': service_name,
            'enabled': new_state,
            'message': f"Service {service_name} has been {'enabled' if new_state else 'disabled'}"
        }

    finally:
        await conn.close()

@router.post("/services/{service_name}/refresh")
async def refresh_service(service_name: str) -> Dict[str, Any]:
    """Refresh a service by updating its registration timestamp."""
    conn = await get_db_connection()

    try:
        result = await conn.execute("""
            UPDATE rag_services
            SET updated_at = NOW()
            WHERE name = $1
        """, service_name)

        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

        return {
            'service': service_name,
            'message': f"Service {service_name} registration refreshed"
        }

    finally:
        await conn.close()

@router.delete("/services/{service_name}")
async def remove_service(service_name: str) -> Dict[str, Any]:
    """Remove a service from the registry."""
    conn = await get_db_connection()

    try:
        result = await conn.execute(
            "DELETE FROM rag_services WHERE name = $1",
            service_name
        )

        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

        return {
            'service': service_name,
            'message': f"Service {service_name} has been removed from registry"
        }

    finally:
        await conn.close()