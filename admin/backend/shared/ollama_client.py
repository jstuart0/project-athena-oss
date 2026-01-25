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

    async def has_model(self, model: str) -> bool:
        """Check if a model is available locally"""
        try:
            models_data = await self.list_models()
            models = models_data.get("models", [])
            # Check exact match or base name match (e.g., "qwen3:4b" matches "qwen3:4b")
            model_names = [m.get("name", "") for m in models]
            return model in model_names or any(m.startswith(model.split(":")[0]) for m in model_names)
        except Exception:
            return False

    async def pull_model(self, model: str, stream: bool = True) -> AsyncIterator[Dict[str, Any]]:
        """
        Pull a model from the Ollama library.

        Args:
            model: Model name to pull (e.g., "qwen3:4b")
            stream: Whether to stream progress updates

        Yields:
            Progress updates as dictionaries with 'status' and optionally 'completed', 'total'
        """
        payload = {"name": model, "stream": stream}

        # Use a longer timeout for model pulling (can take several minutes)
        async with httpx.AsyncClient(base_url=self.url, timeout=600.0) as client:
            if stream:
                async with client.stream("POST", "/api/pull", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            import json
                            yield json.loads(line)
            else:
                response = await client.post("/api/pull", json=payload)
                response.raise_for_status()
                yield response.json()

    async def ensure_model(self, model: str) -> bool:
        """
        Ensure a model is available, pulling it if necessary.

        Args:
            model: Model name to ensure is available

        Returns:
            True if model is available (was present or successfully pulled)
        """
        if await self.has_model(model):
            return True

        try:
            # Pull the model
            async for progress in self.pull_model(model):
                status = progress.get("status", "")
                if "error" in status.lower():
                    return False
            return True
        except Exception:
            return False

    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()
