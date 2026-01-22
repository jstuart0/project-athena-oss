"""Ollama LLM client for Project Athena"""

import os
import httpx
from typing import Optional, Dict, Any, AsyncIterator


class OllamaClient:
    """Client for interacting with Ollama LLM server"""
    
    def __init__(self, url: Optional[str] = None):
        self.url = url or os.getenv("OLLAMA_URL", "http://localhost:11434")
        self.client = httpx.AsyncClient(base_url=self.url, timeout=60.0)
    
    async def generate(
        self,
        model: str,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        stream: bool = False
    ) -> AsyncIterator[Dict[str, Any]]:
        """Generate completion from Ollama"""
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
            "options": {"temperature": temperature}
        }
        
        if system:
            payload["system"] = system
        
        async with self.client.stream("POST", "/api/generate", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    import json
                    yield json.loads(line)
    
    async def chat(
        self,
        model: str,
        messages: list,
        temperature: float = 0.7,
        stream: bool = False
    ) -> AsyncIterator[Dict[str, Any]]:
        """Chat completion from Ollama"""
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": temperature}
        }
        
        async with self.client.stream("POST", "/api/chat", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    import json
                    yield json.loads(line)
    
    async def list_models(self) -> Dict[str, Any]:
        """List available models"""
        response = await self.client.get("/api/tags")
        response.raise_for_status()
        return response.json()
    
    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()
