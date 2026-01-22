"""
Web Search Module for Project Athena

Provides web search capabilities using DuckDuckGo (no API key required).
Falls back to LLM if search fails or returns no results.
"""
import httpx
import structlog
from typing import List, Dict, Optional
from urllib.parse import quote_plus

logger = structlog.get_logger()


class WebSearchClient:
    """Client for web search using DuckDuckGo."""
    
    def __init__(self):
        """Initialize web search client."""
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
        )
    
    async def search(self, query: str, max_results: int = 3) -> List[Dict[str, str]]:
        """
        Search the web using DuckDuckGo.
        
        Args:
            query: Search query
            max_results: Maximum number of results to return
            
        Returns:
            List of search results with title, snippet, and URL
        """
        try:
            logger.info("web_search_started", query=query, max_results=max_results)
            
            # DuckDuckGo instant answer API
            url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
            
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()
            
            results = []
            
            # Abstract (instant answer)
            if data.get("Abstract"):
                results.append({
                    "title": data.get("Heading", "Instant Answer"),
                    "snippet": data.get("Abstract"),
                    "url": data.get("AbstractURL", ""),
                    "source": data.get("AbstractSource", "DuckDuckGo")
                })
            
            # Related topics
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append({
                        "title": topic.get("Text", "")[:100],
                        "snippet": topic.get("Text", ""),
                        "url": topic.get("FirstURL", ""),
                        "source": "DuckDuckGo"
                    })
            
            logger.info("web_search_completed", 
                       query=query, 
                       results_count=len(results))
            
            return results[:max_results]
            
        except Exception as e:
            logger.error("web_search_failed", query=query, error=str(e))
            return []
    
    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


async def search_web(query: str, max_results: int = 3) -> Optional[str]:
    """
    Search the web and format results as a summary.
    
    Args:
        query: Search query
        max_results: Maximum number of results
        
    Returns:
        Formatted search results or None if no results
    """
    client = WebSearchClient()
    try:
        results = await client.search(query, max_results)
        
        if not results:
            return None
        
        # Format results into a summary
        summary_parts = []
        for i, result in enumerate(results, 1):
            snippet = result.get("snippet", "")
            source = result.get("source", "")
            
            if snippet:
                summary_parts.append(f"{i}. {snippet}")
                if source:
                    summary_parts.append(f"   (Source: {source})")
        
        if summary_parts:
            return "\n".join(summary_parts)
        
        return None
        
    finally:
        await client.close()
