"""Parallel Search Engine

Executes multiple search providers IN PARALLEL for faster, more reliable results.

Architecture:
    PRIMARY RACE: Brave + SearXNG race simultaneously
                → Return first success, cancel loser
                → ~50% faster than single provider

    FALLBACK RACE: If primary fails → Bright Data + DuckDuckGo
                → Return first success, cancel pending
"""

import asyncio
import os
from typing import Dict, List, Optional, Any
from urllib.parse import quote_plus
import structlog
import httpx

logger = structlog.get_logger()

# SearXNG URL - configurable via environment
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")


class ParallelSearchEngine:
    """Execute multiple search providers in parallel."""

    def __init__(self, rag_client, timeout: float = 10.0):
        self.rag_client = rag_client
        self.timeout = timeout
        self._http_client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Athena/1.0)"}
        )

    async def search_primary_parallel(
        self,
        query: str,
        max_results: int = 5,
        timeout: float = 3.0
    ) -> Dict[str, Any]:
        """
        Execute PRIMARY parallel search: Brave + SearXNG race with aggregation.

        Both providers run simultaneously, wait up to timeout seconds,
        then aggregate and deduplicate all results that returned.
        """
        logger.info("primary_parallel_search_start", query=query)

        # Race Brave (via websearch RAG) and SearXNG
        tasks = [
            asyncio.create_task(
                self._search_brave(query, max_results),
                name="brave"
            ),
            asyncio.create_task(
                self._search_searxng(query, max_results),
                name="searxng"
            ),
        ]

        # Wait for all with timeout, aggregate results
        result = await self._aggregate_results(tasks, timeout, max_results)

        if result and result.get("results"):
            sources = result.get("sources", [])
            logger.info("primary_search_success", sources=sources, result_count=len(result["results"]))
            return result

        # Primary failed, try fallback
        logger.warning("primary_search_failed_trying_fallback", query=query)
        return await self.search_with_fallback(query, max_results)

    async def _aggregate_results(
        self,
        tasks: List[asyncio.Task],
        timeout: float,
        max_results: int
    ) -> Optional[Dict[str, Any]]:
        """
        Wait for all tasks (up to timeout), aggregate and dedupe results.

        Returns combined results from all providers that responded in time.
        """
        all_results = []
        sources = []

        # Wait for all tasks with timeout
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.ALL_COMPLETED
        )

        # Cancel any that didn't finish in time
        for task in pending:
            task.cancel()
            logger.debug("search_task_timeout", task=task.get_name())

        # Collect results from completed tasks
        for task in done:
            try:
                result = task.result()
                if result and "results" in result and result["results"]:
                    source = result.get("source", task.get_name())
                    sources.append(source)
                    for item in result["results"]:
                        item["_source"] = source  # Track origin for debugging
                        all_results.append(item)
            except Exception as e:
                logger.debug("search_task_failed", task=task.get_name(), error=str(e))

        if not all_results:
            return None

        # Deduplicate by URL
        seen_urls = set()
        deduped = []
        for item in all_results:
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                deduped.append(item)
            elif not url:
                # Keep items without URLs (they can't be deduped)
                deduped.append(item)

        logger.info("search_aggregated",
                   total=len(all_results),
                   deduped=len(deduped),
                   sources=sources)

        return {
            "source": "aggregated",
            "sources": sources,
            "results": deduped[:max_results]
        }

    async def search_with_fallback(
        self,
        query: str,
        max_results: int = 5
    ) -> Dict[str, Any]:
        """
        Execute FALLBACK parallel search: Bright Data + DuckDuckGo.

        Returns first successful result from any provider.
        Cancels remaining searches once one succeeds.
        """
        logger.info("fallback_search_start", query=query)

        # Create tasks for all fallback providers
        tasks = [
            asyncio.create_task(
                self._search_bright_data(query, max_results),
                name="bright_data"
            ),
            asyncio.create_task(
                self._search_duckduckgo(query, max_results),
                name="duckduckgo"
            ),
        ]

        # Wait for first successful result
        result = await self._first_success(tasks)

        if result:
            logger.info("parallel_search_success", source=result.get("source"))
            return result

        logger.warning("parallel_search_all_failed", query=query)
        return {"error": "All search providers failed", "results": []}

    async def _first_success(
        self,
        tasks: List[asyncio.Task]
    ) -> Optional[Dict[str, Any]]:
        """
        Return first successful result, cancel others.

        Uses asyncio.wait with FIRST_COMPLETED to minimize latency.
        """
        pending = set(tasks)

        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=self.timeout
            )

            for task in done:
                try:
                    result = task.result()
                    if result and "results" in result and len(result["results"]) > 0:
                        # Success! Cancel remaining tasks
                        for p in pending:
                            p.cancel()
                        return result
                except Exception as e:
                    logger.debug("search_task_failed", task=task.get_name(), error=str(e))
                    continue

            if not done:  # Timeout with no completions
                break

        # Cancel any remaining
        for task in pending:
            task.cancel()

        return None

    async def _search_brave(
        self,
        query: str,
        max_results: int
    ) -> Optional[Dict[str, Any]]:
        """Search via Brave (websearch RAG service)."""
        try:
            response = await self.rag_client.get(
                "websearch",
                "/search",
                params={"query": query, "count": max_results}
            )
            if response.success and response.data:
                results = response.data.get("results", [])
                if results:
                    return {
                        "source": "brave",
                        "results": results
                    }
        except Exception as e:
            logger.debug("brave_search_failed", error=str(e))
        return None

    async def _search_searxng(
        self,
        query: str,
        max_results: int
    ) -> Optional[Dict[str, Any]]:
        """Search via SearXNG on Thor cluster."""
        try:
            url = f"{SEARXNG_URL}/search"
            params = {
                "q": query,
                "format": "json",
                "categories": "general",
            }

            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", [])[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "description": item.get("content", ""),
                    "url": item.get("url", ""),
                    "source": "searxng"
                })

            if results:
                return {
                    "source": "searxng",
                    "results": results
                }
        except Exception as e:
            logger.debug("searxng_search_failed", error=str(e))
        return None

    async def _search_bright_data(
        self,
        query: str,
        max_results: int
    ) -> Optional[Dict[str, Any]]:
        """Search via Bright Data RAG service."""
        try:
            response = await self.rag_client.get(
                "brightdata",
                "/search",
                params={"query": query, "count": max_results}
            )
            if response.success and response.data:
                return {
                    "source": "bright_data",
                    "results": response.data.get("results", [])
                }
        except Exception as e:
            logger.debug("bright_data_search_failed", error=str(e))
        return None

    async def _search_duckduckgo(
        self,
        query: str,
        max_results: int
    ) -> Optional[Dict[str, Any]]:
        """Search via DuckDuckGo instant answer API (no API key needed)."""
        try:
            url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1"

            response = await self._http_client.get(url)
            response.raise_for_status()
            data = response.json()

            results = []

            # Abstract (instant answer)
            if data.get("Abstract"):
                results.append({
                    "title": data.get("Heading", "Instant Answer"),
                    "description": data.get("Abstract"),
                    "url": data.get("AbstractURL", ""),
                    "source": "duckduckgo"
                })

            # Related topics
            for topic in data.get("RelatedTopics", [])[:max_results - len(results)]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append({
                        "title": topic.get("Text", "")[:100],
                        "description": topic.get("Text", ""),
                        "url": topic.get("FirstURL", ""),
                        "source": "duckduckgo"
                    })

            if results:
                return {
                    "source": "duckduckgo",
                    "results": results[:max_results]
                }
        except Exception as e:
            logger.debug("duckduckgo_search_failed", error=str(e))
        return None

    async def close(self):
        """Cleanup resources."""
        await self._http_client.aclose()


# Singleton instance
_parallel_search: Optional[ParallelSearchEngine] = None


async def get_parallel_search_engine(rag_client) -> ParallelSearchEngine:
    """Get or create parallel search engine instance."""
    global _parallel_search
    if _parallel_search is None:
        _parallel_search = ParallelSearchEngine(rag_client)
    return _parallel_search
