"""
High-performance content fetching and extraction.
Fetches ONLY high-value URLs identified by structured_data module.

This module provides Tier 2 (selective fetch) content extraction by
fetching 1-2 high-value pages and extracting structured content quickly.

For JS-rendered sites (React/Vue/Angular SPAs), a Playwright headless browser
is used as a smart fallback. A fast heuristic (_is_js_shell) detects SPA shells
after the initial httpx fetch, tries JSON-LD extraction first (often in <head>),
then falls through to browser rendering if needed.

Optional deps (install in your service's requirements.txt if you need full extraction):
  trafilatura  — article text extraction
  extruct      — JSON-LD structured data extraction
  pandas       — HTML table extraction
  playwright   — headless browser fallback for JS-rendered sites

Consumers:
  src/rag/site_scraper/main.py
  src/orchestrator/helpers.py
"""
import asyncio
import re
import time
import httpx
from typing import Optional, Dict, Any, List

from .logging_config import configure_logging

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

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logger.info("playwright not installed - JS-rendered site fallback unavailable")


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

        # Playwright browser (lazy-initialized on first JS-render request)
        self._playwright = None
        self._browser: Optional[Any] = None
        self._browser_context: Optional[Any] = None
        self._browser_lock = asyncio.Lock()
        self._browser_semaphore = asyncio.Semaphore(2)  # Max 2 concurrent browser pages

    async def fetch_structured_content(
        self,
        url: str,
        extraction_hint: str = "auto"
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch and extract content from URL.

        Flow:
        1. httpx GET to fetch raw HTML
        2. Check if HTML is a JS shell (SPA mount point + empty body)
        3. If JS shell: try JSON-LD from <head>, then browser render
        4. If real HTML: try extractors (JSON-LD → Tables → Article)
        5. If extractors fail: try browser render as last resort

        Args:
            url: URL to fetch
            extraction_hint: Preferred extraction type ("jsonld", "table", "article", "auto")

        Returns:
            Dict with extracted data or None if failed
            Format: {
                "type": "jsonld" | "table" | "article",
                "data": <extracted content>,
                "source_url": <url>,
                "extraction_time_ms": <time taken>,
                "rendered": True  # only if browser-rendered
            }
        """
        start_time = time.time()

        try:
            logger.info(f"Fetching content from {url} (hint: {extraction_hint})")

            # Fetch the page
            response = await self.client.get(url)
            response.raise_for_status()
            html = response.text

            fetch_time = (time.time() - start_time) * 1000
            logger.debug(f"Fetch completed in {fetch_time:.0f}ms ({len(html)} chars)")

            # Check if this looks like a JS-rendered shell page
            if self._is_js_shell(html):
                # JS shell detected — but JSON-LD lives in <head> and may be present
                # even in shell HTML. Try it first (fast, <10ms) before browser rendering.
                if extraction_hint == "jsonld" or extraction_hint == "auto":
                    json_ld = await self._extract_jsonld(html)
                    if json_ld:
                        total_time = (time.time() - start_time) * 1000
                        logger.info(f"JSON-LD found in JS shell HTML in {total_time:.0f}ms (no browser needed)")
                        return {
                            "type": "jsonld",
                            "data": json_ld,
                            "source_url": url,
                            "extraction_time_ms": total_time,
                        }

                # No JSON-LD in shell — skip table/article extractors (they need body content),
                # go straight to browser rendering
                if HAS_PLAYWRIGHT:
                    logger.info(f"JS shell detected, using browser rendering for {url}")
                    browser_html = await self._fetch_with_browser(url)
                    if browser_html:
                        result = await self._run_extraction_pipeline(
                            browser_html, url, extraction_hint, start_time, rendered=True
                        )
                        if result:
                            return result
                        logger.warning(f"Browser rendered HTML but extraction still failed for {url}")
                    else:
                        logger.warning(f"Browser fetch failed for JS shell page {url}")
                else:
                    # Playwright not available — fall back to running full extraction pipeline
                    # on the shell HTML as a last resort (unlikely to find much, but not a regression)
                    logger.warning(f"JS shell detected but playwright not available, trying extractors as fallback for {url}")
                    result = await self._run_extraction_pipeline(
                        html, url, extraction_hint, start_time, rendered=False
                    )
                    if result:
                        return result

                logger.warning(f"No content extracted from JS shell page {url}")
                return None

            # Normal path: real HTML content, try extraction pipeline
            result = await self._run_extraction_pipeline(
                html, url, extraction_hint, start_time, rendered=False
            )
            if result:
                return result

            # All extractors failed on real HTML — try browser as last resort
            if HAS_PLAYWRIGHT:
                logger.info(f"Extractors failed on real HTML, trying browser fallback for {url}")
                browser_html = await self._fetch_with_browser(url)
                if browser_html:
                    result = await self._run_extraction_pipeline(
                        browser_html, url, extraction_hint, start_time, rendered=True
                    )
                    if result:
                        return result
                    logger.warning(f"Browser rendering produced HTML but extraction still failed for {url}")

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

    def _is_js_shell(self, html: str) -> bool:
        """
        Fast heuristic to detect JS-rendered shell pages (React/Vue/Angular SPAs).

        Checks for BOTH conditions:
        1. A known SPA mount point pattern exists (<div id="root">, etc.)
        2. The visible body text content is very short (<200 chars)

        Both conditions must be true. This prevents false positives on short
        but valid pages (error pages, small static pages with real content).
        """
        if not html:
            return False

        # Check for common SPA mount point patterns (REQUIRED)
        spa_indicators = [
            '<div id="root"></div>',
            '<div id="root">',
            '<div id="app"></div>',
            '<div id="app">',
            '<div id="__next">',
            '<div id="__nuxt">',
        ]
        html_lower = html.lower()

        has_spa_mount = any(indicator.lower() in html_lower for indicator in spa_indicators)

        if not has_spa_mount:
            return False

        # SPA mount point found — now check if body is mostly empty
        body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
        if not body_match:
            return False

        body_content = body_match.group(1)

        # Strip script and style tags to get actual visible content
        visible = re.sub(r'<script[^>]*>.*?</script>', '', body_content, flags=re.DOTALL | re.IGNORECASE)
        visible = re.sub(r'<style[^>]*>.*?</style>', '', visible, flags=re.DOTALL | re.IGNORECASE)
        visible = re.sub(r'<[^>]+>', '', visible)
        visible = visible.strip()

        # Both conditions met: SPA mount point + very little visible text
        if len(visible) < 200:
            logger.info(f"JS shell detected: SPA mount point found, only {len(visible)} chars of visible content")
            return True

        return False

    async def _run_extraction_pipeline(self, html: str, url: str, extraction_hint: str, start_time: float, rendered: bool = False) -> Optional[Dict[str, Any]]:
        """
        Run the extraction pipeline (JSON-LD → Tables → Article) on HTML content.

        Returns extraction result dict or None if all methods fail.
        """
        source = "Browser" if rendered else "HTTP"

        if extraction_hint == "jsonld" or extraction_hint == "auto":
            json_ld = await self._extract_jsonld(html)
            if json_ld:
                total_time = (time.time() - start_time) * 1000
                logger.info(f"{source} + JSON-LD extraction successful in {total_time:.0f}ms")
                result = {
                    "type": "jsonld",
                    "data": json_ld,
                    "source_url": url,
                    "extraction_time_ms": total_time,
                }
                if rendered:
                    result["rendered"] = True
                return result

        if extraction_hint == "table" or extraction_hint == "auto":
            tables = await self._extract_tables(html, url)
            if tables:
                total_time = (time.time() - start_time) * 1000
                logger.info(f"{source} + Table extraction successful in {total_time:.0f}ms ({len(tables)} tables)")
                result = {
                    "type": "table",
                    "data": tables,
                    "source_url": url,
                    "extraction_time_ms": total_time,
                }
                if rendered:
                    result["rendered"] = True
                return result

        if extraction_hint == "article" or extraction_hint == "auto":
            article_text = await self._extract_article(html)
            if article_text:
                total_time = (time.time() - start_time) * 1000
                logger.info(f"{source} + Article extraction successful in {total_time:.0f}ms ({len(article_text)} chars)")
                result = {
                    "type": "article",
                    "data": article_text,
                    "source_url": url,
                    "extraction_time_ms": total_time,
                }
                if rendered:
                    result["rendered"] = True
                return result

        return None

    async def _ensure_browser(self):
        """Lazy-initialize Playwright browser on first use."""
        if self._browser_context is not None:
            return

        async with self._browser_lock:
            if self._browser_context is not None:
                return

            if not HAS_PLAYWRIGHT:
                raise RuntimeError("playwright not installed")

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--no-first-run",
                ]
            )
            self._browser_context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
            )
            logger.info("playwright_browser_initialized")

    async def _reset_browser(self):
        """Reset browser if it becomes unresponsive."""
        logger.warning("Resetting browser due to error")
        try:
            if self._browser_context:
                await self._browser_context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

        self._browser_context = None
        self._browser = None
        self._playwright = None

    async def _fetch_with_browser(self, url: str, wait_seconds: float = 3.0) -> Optional[str]:
        """Fetch page with browser, enforcing a hard 20s timeout."""
        try:
            return await asyncio.wait_for(
                self._fetch_with_browser_inner(url, wait_seconds),
                timeout=20.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"Browser fetch hard timeout (20s) for {url}")
            await self._reset_browser()
            return None

    async def _fetch_with_browser_inner(self, url: str, wait_seconds: float = 3.0) -> Optional[str]:
        """
        Fetch page content using headless Chromium for JS-rendered sites.

        Args:
            url: URL to fetch
            wait_seconds: Time to wait for JS rendering after page load

        Returns:
            Rendered HTML content or None on failure
        """
        if not HAS_PLAYWRIGHT:
            logger.debug("playwright not available, skipping browser fetch")
            return None

        try:
            await self._ensure_browser()

            async with self._browser_semaphore:
                page = await self._browser_context.new_page()
                try:
                    await page.goto(url, wait_until="networkidle", timeout=15000)
                    await page.wait_for_timeout(int(wait_seconds * 1000))
                    html = await page.content()

                    content_length = len(html) if html else 0
                    logger.info(f"Browser fetch completed: {content_length} chars from {url}")
                    return html
                finally:
                    await page.close()

        except Exception as e:
            logger.warning(f"Browser fetch failed for {url}: {e}")
            await self._reset_browser()
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
        """Close HTTP client, browser, and cleanup resources."""
        await self.client.aclose()

        if self._browser_context:
            try:
                await self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

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
