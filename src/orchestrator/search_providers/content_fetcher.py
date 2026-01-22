"""
High-performance content fetching and extraction.
Fetches ONLY high-value URLs identified by structured_data module.

This module provides Tier 2 (selective fetch) content extraction by
fetching 1-2 high-value pages and extracting structured content quickly.
"""
import asyncio
import httpx
from typing import Optional, Dict, Any, List
import logging

# Import shared utilities
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))
from shared.logging_config import configure_logging

logger = configure_logging("content-fetcher")

# Conditional imports for optional dependencies
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    logger.warning("trafilatura not installed - article extraction will be limited")

try:
    import extruct
    HAS_EXTRUCT = True
except ImportError:
    HAS_EXTRUCT = False
    logger.warning("extruct not installed - JSON-LD extraction will be limited")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    logger.warning("pandas not installed - table extraction disabled")


class ContentFetcher:
    """
    High-performance content fetcher with multiple extraction strategies.

    Fetches 1-2 high-value URLs in parallel and extracts structured content
    using the fastest appropriate method (JSON-LD, tables, or article text).
    """

    def __init__(self, timeout: float = 2.0, max_concurrent: int = 2):
        """
        Initialize content fetcher.

        Args:
            timeout: Max time to wait for each fetch (seconds)
            max_concurrent: Max pages to fetch in parallel (keep low for performance)
        """
        self.timeout = timeout
        self.max_concurrent = max_concurrent
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "User-Agent": "Mozilla/5.0 (Athena/1.0; +https://your-domain.com/bot)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1"
            },
            follow_redirects=True,
            max_redirects=3
        )

    async def fetch_structured_content(
        self,
        url: str,
        extraction_hint: str = "auto"
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch and extract content from URL.

        Tries multiple extraction methods in order:
        1. JSON-LD (fastest, most structured)
        2. HTML tables (for schedules/scores)
        3. Article text (fallback)

        Args:
            url: URL to fetch
            extraction_hint: Preferred extraction type ("jsonld", "table", "article", "auto")

        Returns:
            Dict with extracted data or None if failed
            Format: {
                "type": "jsonld" | "table" | "article",
                "data": <extracted content>,
                "source_url": <url>,
                "extraction_time_ms": <time taken>
            }
        """
        import time
        start_time = time.time()

        try:
            logger.info(f"Fetching content from {url} (hint: {extraction_hint})")

            # Fetch the page
            response = await self.client.get(url)
            response.raise_for_status()
            html = response.text

            fetch_time = (time.time() - start_time) * 1000
            logger.debug(f"Fetch completed in {fetch_time:.0f}ms")

            # Try extraction methods based on hint
            if extraction_hint == "jsonld" or extraction_hint == "auto":
                json_ld = await self._extract_jsonld(html)
                if json_ld:
                    total_time = (time.time() - start_time) * 1000
                    logger.info(f"JSON-LD extraction successful in {total_time:.0f}ms")
                    return {
                        "type": "jsonld",
                        "data": json_ld,
                        "source_url": url,
                        "extraction_time_ms": total_time
                    }

            if extraction_hint == "table" or extraction_hint == "auto":
                tables = await self._extract_tables(html, url)
                if tables:
                    total_time = (time.time() - start_time) * 1000
                    logger.info(f"Table extraction successful in {total_time:.0f}ms ({len(tables)} tables)")
                    return {
                        "type": "table",
                        "data": tables,
                        "source_url": url,
                        "extraction_time_ms": total_time
                    }

            if extraction_hint == "article" or extraction_hint == "auto":
                article_text = await self._extract_article(html)
                if article_text:
                    total_time = (time.time() - start_time) * 1000
                    logger.info(f"Article extraction successful in {total_time:.0f}ms ({len(article_text)} chars)")
                    return {
                        "type": "article",
                        "data": article_text,
                        "source_url": url,
                        "extraction_time_ms": total_time
                    }

            logger.warning(f"No content extracted from {url}")
            return None

        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching {url} (>{self.timeout}s)")
            return None
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP {e.response.status_code} for {url}")
            return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}", exc_info=True)
            return None

    async def _extract_jsonld(self, html: str) -> Optional[List[Dict]]:
        """
        Extract JSON-LD structured data (fastest, most accurate).

        JSON-LD is embedded structured data that many modern sites include.
        Example: SportsEvent schema with game schedules, scores, teams.

        Args:
            html: HTML content

        Returns:
            List of JSON-LD objects or None
        """
        if not HAS_EXTRUCT:
            logger.debug("extruct not available, skipping JSON-LD extraction")
            return None

        try:
            # Use extruct to extract JSON-LD
            data = extruct.extract(html, syntaxes=['json-ld'])
            json_ld_items = data.get('json-ld', [])

            if json_ld_items and len(json_ld_items) > 0:
                logger.debug(f"Found {len(json_ld_items)} JSON-LD items")
                return json_ld_items

        except Exception as e:
            logger.debug(f"JSON-LD extraction failed: {e}")

        return None

    async def _extract_tables(self, html: str, url: str) -> Optional[List[Dict[str, Any]]]:
        """
        Extract HTML tables as structured data.

        Great for sports schedules, scores, event listings, etc.

        Args:
            html: HTML content
            url: Source URL (for context)

        Returns:
            List of tables with metadata or None
        """
        if not HAS_PANDAS:
            logger.debug("pandas not available, skipping table extraction")
            return None

        try:
            from io import StringIO

            # Use pandas to extract all tables
            tables = pd.read_html(StringIO(html))

            if not tables or len(tables) == 0:
                return None

            # Convert tables to structured format
            extracted_tables = []
            for idx, table in enumerate(tables):
                # Filter out very small tables (likely navigation/menus)
                if len(table) < 2 or len(table.columns) < 2:
                    continue

                # Filter out very large tables (likely data dumps)
                if len(table) > 100:
                    table = table.head(50)  # Take first 50 rows

                # Convert to records format
                records = table.to_dict('records')

                extracted_tables.append({
                    "table_index": idx,
                    "rows": len(table),
                    "columns": len(table.columns),
                    "column_names": list(table.columns),
                    "data": records
                })

            if extracted_tables:
                logger.debug(f"Extracted {len(extracted_tables)} tables")
                return extracted_tables

        except Exception as e:
            logger.debug(f"Table extraction failed: {e}")

        return None

    async def _extract_article(self, html: str) -> Optional[str]:
        """
        Extract main article content using trafilatura.

        Trafilatura is very fast (~50ms) and removes boilerplate
        (headers, footers, ads, navigation).

        Args:
            html: HTML content

        Returns:
            Extracted text or None
        """
        if not HAS_TRAFILATURA:
            logger.debug("trafilatura not available, skipping article extraction")
            return None

        try:
            # Trafilatura is very fast and accurate
            text = trafilatura.extract(
                html,
                include_tables=True,
                include_links=False,
                include_images=False,
                output_format='txt',
                no_fallback=False
            )

            if text and len(text) > 100:
                return text

        except Exception as e:
            logger.debug(f"Article extraction failed: {e}")

        return None

    async def fetch_multiple_urls(
        self,
        url_configs: List[Dict[str, Any]]
    ) -> List[Optional[Dict[str, Any]]]:
        """
        Fetch multiple URLs in parallel (up to max_concurrent).

        Args:
            url_configs: List of dicts with 'url' and optional 'extraction_hint'
                Example: [
                    {"url": "https://espn.com/scoreboard", "extraction_hint": "table"},
                    {"url": "https://cbssports.com/nfl", "extraction_hint": "auto"}
                ]

        Returns:
            List of extraction results (same order as input, None for failures)
        """
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def _fetch_with_semaphore(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            async with semaphore:
                return await self.fetch_structured_content(
                    url=config["url"],
                    extraction_hint=config.get("extraction_hint", "auto")
                )

        tasks = [_fetch_with_semaphore(config) for config in url_configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to None
        return [
            result if not isinstance(result, Exception) else None
            for result in results
        ]

    async def close(self):
        """Close HTTP client and cleanup resources."""
        await self.client.aclose()
        logger.debug("ContentFetcher closed")


# Convenience function for single-use fetching
async def fetch_url_content(url: str, extraction_hint: str = "auto", timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    """
    Convenience function to fetch a single URL without managing a ContentFetcher instance.

    Args:
        url: URL to fetch
        extraction_hint: Extraction method hint
        timeout: Timeout in seconds

    Returns:
        Extracted content dict or None
    """
    fetcher = ContentFetcher(timeout=timeout)
    try:
        result = await fetcher.fetch_structured_content(url, extraction_hint)
        return result
    finally:
        await fetcher.close()
