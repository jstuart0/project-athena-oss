"""
Unified LLM Router

Routes LLM requests to appropriate backend (Ollama, MLX, etc.) based on
admin configuration. Supports per-model backend selection with automatic
fallback.

Open Source Compatible - No vendor lock-in.
"""
import os
import httpx
import time
from typing import Dict, Any, Optional, List
from enum import Enum
from collections import deque
import structlog

logger = structlog.get_logger()


class BackendType(str, Enum):
    """Supported LLM backend types."""
    OLLAMA = "ollama"
    MLX = "mlx"
    AUTO = "auto"  # Try MLX first, fall back to Ollama
    # Cloud providers - Open Source Compatible
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"

    @classmethod
    def is_cloud(cls, backend_type: str) -> bool:
        """Check if backend type is a cloud provider."""
        return backend_type in (cls.OPENAI.value, cls.ANTHROPIC.value, cls.GOOGLE.value)


# Cloud provider pricing (per 1M tokens in USD) - fallback defaults
# These are updated from database when available
DEFAULT_CLOUD_PRICING = {
    "openai": {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "default": {"input": 0.50, "output": 2.00}
    },
    "anthropic": {
        "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
        "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
        "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
        "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
        "default": {"input": 3.00, "output": 15.00}
    },
    "google": {
        "gemini-2.0-flash": {"input": 0.075, "output": 0.30},
        "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
        "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
        "default": {"input": 0.10, "output": 0.40}
    }
}


class LLMRouter:
    """
    Routes LLM requests to configured backends.

    Usage:
        router = LLMRouter(admin_url="http://localhost:8080")
        response = await router.generate(
            model="phi3:mini",
            prompt="Hello world",
            temperature=0.7
        )
    """

    def __init__(
        self,
        admin_url: Optional[str] = None,
        cache_ttl: int = 60,
        metrics_window_size: int = 100,
        persist_metrics: bool = True
    ):
        """
        Initialize LLM Router.

        Args:
            admin_url: Admin API URL for fetching backend configs
            cache_ttl: Cache TTL in seconds for backend configs
            metrics_window_size: Number of recent requests to track for metrics
            persist_metrics: Whether to persist metrics to database via Admin API
        """
        self.admin_url = admin_url or os.getenv(
            "ADMIN_API_URL",
            "http://localhost:8080"
        )
        self._admin_url_base = self.admin_url
        self.client = httpx.AsyncClient(timeout=120.0)
        self._backend_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_expiry: Dict[str, float] = {}
        self._cache_ttl = cache_ttl
        self._persist_metrics = persist_metrics

        # Performance metrics storage (rolling window)
        self._metrics_window_size = metrics_window_size
        self._metrics: deque = deque(maxlen=metrics_window_size)

        # Model configuration cache (separate from backend config)
        self._model_config_cache: Dict[str, Dict[str, Any]] = {}
        self._model_config_expiry: Dict[str, float] = {}

        # Cloud provider credentials cache
        self._cloud_credentials_cache: Dict[str, Dict[str, Any]] = {}
        self._cloud_credentials_expiry: Dict[str, float] = {}

        # Cloud provider pricing cache (from database)
        self._pricing_cache: Dict[str, Dict[str, Any]] = {}
        self._pricing_cache_expiry: float = 0

        logger.info(
            "llm_router_initialized",
            metrics_window_size=metrics_window_size,
            persist_metrics=persist_metrics
        )

    async def _get_backend_config(self, model: str) -> Dict[str, Any]:
        """
        Fetch backend configuration for a model from admin API.

        Caches results for performance. Also handles cloud models
        (format: provider/model_name like openai/gpt-4o).

        Args:
            model: Model name (e.g., "phi3:mini" or "openai/gpt-4o")

        Returns:
            Backend configuration dict
        """
        now = time.time()

        # Check cache
        if model in self._backend_cache:
            if now < self._cache_expiry.get(model, 0):
                return self._backend_cache[model]

        # Check if this is a cloud model (format: provider/model_name)
        if "/" in model:
            parts = model.split("/", 1)
            provider = parts[0].lower()
            model_id = parts[1] if len(parts) > 1 else model

            if provider in ("openai", "anthropic", "google"):
                logger.info(
                    "detected_cloud_model",
                    model=model,
                    provider=provider,
                    model_id=model_id
                )
                config = {
                    "backend_type": provider,
                    "model_id": model_id,  # The actual model name for the API
                    "endpoint_url": None,  # Cloud providers use their own endpoints
                    "max_tokens": 4096,
                    "temperature_default": 0.7,
                    "timeout_seconds": 120,
                    "keep_alive_seconds": -1
                }

                # Cache
                self._backend_cache[model] = config
                self._cache_expiry[model] = now + self._cache_ttl

                return config

        # Fetch from admin API using public endpoint (avoids URL encoding issues)
        try:
            url = f"{self.admin_url}/api/llm-backends/public"
            response = await self.client.get(url)
            response.raise_for_status()

            backends = response.json()

            # Find the matching model
            config = None
            for backend in backends:
                if backend.get("model_name") == model:
                    config = backend
                    break

            if config is None:
                # No config found - use default Ollama
                logger.warning(
                    "no_backend_config_found",
                    model=model,
                    falling_back="ollama"
                )
                config = {
                    "backend_type": "ollama",
                    "endpoint_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
                    "max_tokens": 2048,
                    "temperature_default": 0.7,
                    "timeout_seconds": 60,
                    "keep_alive_seconds": -1  # Default: keep forever
                }

            # Cache
            self._backend_cache[model] = config
            self._cache_expiry[model] = now + self._cache_ttl

            return config

        except Exception as e:
            logger.error(
                "failed_to_fetch_backend_config",
                model=model,
                error=str(e)
            )
            # Fall back to Ollama on Mac Studio
            return {
                "backend_type": "ollama",
                "endpoint_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
                "max_tokens": 2048,
                "temperature_default": 0.7,
                "timeout_seconds": 60,
                "keep_alive_seconds": -1  # Default: keep forever
            }

    async def _get_model_config(self, model: str) -> Dict[str, Any]:
        """
        Fetch model execution configuration (Ollama/MLX options) from admin API.

        This is separate from backend config - model config contains execution
        options like num_ctx, num_batch, mirostat, top_k, top_p, etc.

        Args:
            model: Model name (e.g., "qwen3:8b")

        Returns:
            Model configuration dict with ollama_options/mlx_options
        """
        now = time.time()

        # Check cache
        if model in self._model_config_cache:
            if now < self._model_config_expiry.get(model, 0):
                return self._model_config_cache[model]

        # Fetch from admin API
        try:
            url = f"{self.admin_url}/api/model-configs/public/{model}"
            response = await self.client.get(url)

            if response.status_code == 200:
                config = response.json()
                logger.debug(
                    "fetched_model_config",
                    model=model,
                    config_model=config.get("model_name"),
                    has_ollama_options=bool(config.get("ollama_options"))
                )
            else:
                # Not found - use empty config (defaults will be used)
                logger.debug(
                    "no_model_config_found",
                    model=model,
                    status=response.status_code
                )
                config = {
                    "model_name": model,
                    "ollama_options": {},
                    "mlx_options": {},
                    "temperature": 0.7,
                    "max_tokens": 2048,
                    "timeout_seconds": 60,
                    "keep_alive_seconds": -1
                }

            # Cache
            self._model_config_cache[model] = config
            self._model_config_expiry[model] = now + self._cache_ttl

            return config

        except Exception as e:
            logger.warning(
                "failed_to_fetch_model_config",
                model=model,
                error=str(e)
            )
            # Return empty config on failure
            return {
                "model_name": model,
                "ollama_options": {},
                "mlx_options": {},
                "temperature": 0.7,
                "max_tokens": 2048,
                "timeout_seconds": 60,
                "keep_alive_seconds": -1
            }

    async def generate(
        self,
        model: str,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        zone: Optional[str] = None,
        intent: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate text using configured backend for the model.

        Args:
            model: Model name (e.g., "phi3:mini")
            prompt: Input prompt
            temperature: Temperature override
            max_tokens: Max tokens override
            request_id: Optional request ID for tracking
            session_id: Optional session ID for conversation tracking
            user_id: Optional user ID for user-specific analytics
            zone: Optional zone/location for geographic analytics
            intent: Optional intent classification for categorization
            **kwargs: Additional backend-specific parameters

        Returns:
            Generated response with metadata
        """
        # Get backend configuration
        config = await self._get_backend_config(model)
        backend_type = config["backend_type"]
        endpoint_url = config["endpoint_url"]

        # Get model execution configuration (Ollama options, etc.)
        model_config = await self._get_model_config(model)
        ollama_options = model_config.get("ollama_options", {})
        mlx_options = model_config.get("mlx_options", {})

        # Apply defaults from config (model_config takes precedence)
        temperature = temperature or model_config.get("temperature") or config.get("temperature_default", 0.7)
        max_tokens = max_tokens or model_config.get("max_tokens") or config.get("max_tokens", 2048)
        timeout = model_config.get("timeout_seconds") or config.get("timeout_seconds", 60)
        keep_alive = model_config.get("keep_alive_seconds") if model_config.get("keep_alive_seconds") is not None else config.get("keep_alive_seconds", -1)

        logger.info(
            "routing_llm_request",
            model=model,
            backend_type=backend_type,
            endpoint=endpoint_url,
            keep_alive=keep_alive,
            has_ollama_options=bool(ollama_options)
        )

        start_time = time.time()
        response = None

        try:
            if backend_type == BackendType.AUTO or backend_type == "auto":
                # Try MLX first, fall back to Ollama
                try:
                    response = await self._generate_mlx(
                        endpoint_url, model, prompt, temperature, max_tokens, timeout, mlx_options
                    )
                except Exception as e:
                    logger.warning(
                        "mlx_failed_falling_back_to_ollama",
                        error=str(e)
                    )
                    # Fall back to Ollama on Mac Studio
                    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
                    response = await self._generate_ollama(
                        ollama_url, model, prompt, temperature, max_tokens, timeout, keep_alive, ollama_options
                    )

            elif backend_type == BackendType.MLX or backend_type == "mlx":
                response = await self._generate_mlx(
                    endpoint_url, model, prompt, temperature, max_tokens, timeout, mlx_options
                )

            elif backend_type == BackendType.OPENAI or backend_type == "openai":
                # Cloud provider: OpenAI
                creds = await self._get_cloud_credentials("openai")
                if not creds or not creds.get("api_key"):
                    raise ValueError("OpenAI API key not configured in admin backend")
                # Use model_id from config (e.g., "gpt-4o" from "openai/gpt-4o")
                model_id = config.get("model_id", model.split("/")[-1] if "/" in model else model)
                response = await self._generate_openai(
                    creds["api_key"], model_id, prompt, temperature, max_tokens,
                    system_prompt=None, request_id=request_id
                )

            elif backend_type == BackendType.ANTHROPIC or backend_type == "anthropic":
                # Cloud provider: Anthropic
                creds = await self._get_cloud_credentials("anthropic")
                if not creds or not creds.get("api_key"):
                    raise ValueError("Anthropic API key not configured in admin backend")
                model_id = config.get("model_id", model.split("/")[-1] if "/" in model else model)
                response = await self._generate_anthropic(
                    creds["api_key"], model_id, prompt, temperature, max_tokens,
                    system_prompt=None, request_id=request_id
                )

            elif backend_type == BackendType.GOOGLE or backend_type == "google":
                # Cloud provider: Google
                creds = await self._get_cloud_credentials("google")
                if not creds or not creds.get("api_key"):
                    raise ValueError("Google API key not configured in admin backend")
                model_id = config.get("model_id", model.split("/")[-1] if "/" in model else model)
                response = await self._generate_google(
                    creds["api_key"], model_id, prompt, temperature, max_tokens,
                    system_prompt=None, request_id=request_id
                )

            else:  # OLLAMA (default)
                response = await self._generate_ollama(
                    endpoint_url, model, prompt, temperature, max_tokens, timeout, keep_alive, ollama_options
                )

            return response

        finally:
            duration = time.time() - start_time

            # Track metrics if response was generated
            if response:
                tokens = response.get("eval_count", 0)
                tokens_per_sec = tokens / duration if duration > 0 and tokens > 0 else 0

                metric = {
                    "timestamp": start_time,
                    "model": model,
                    "backend": response.get("backend"),
                    "latency_seconds": duration,
                    "tokens": tokens,
                    "tokens_per_second": tokens_per_sec,
                    "request_id": request_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "zone": zone,
                    "intent": intent
                }
                self._metrics.append(metric)

                # Persist metric to database asynchronously
                import asyncio
                stage = kwargs.get("stage")
                asyncio.create_task(self._persist_metric(metric, source="orchestrator", stage=stage))

                logger.info(
                    "llm_request_completed",
                    model=model,
                    backend_type=backend_type,
                    duration=duration,
                    tokens_per_sec=round(tokens_per_sec, 2),
                    request_id=request_id,
                    session_id=session_id
                )
            else:
                logger.info(
                    "llm_request_completed",
                    model=model,
                    backend_type=backend_type,
                    duration=duration,
                    request_id=request_id,
                    session_id=session_id
                )

    async def generate_with_tools(
        self,
        model: str,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        backend: str = "openai",
        request_id: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate text with tool calling support.

        Args:
            model: Model name (e.g., "gpt-4o-mini" or "llama3.1:8b")
            messages: Chat messages in OpenAI format
            tools: List of tool definitions in OpenAI function calling format
            temperature: Temperature override
            max_tokens: Max tokens override
            backend: Backend to use ("openai" or "ollama")
            request_id: Optional request ID for tracking
            **kwargs: Additional backend-specific parameters

        Returns:
            Response with tool_calls if LLM wants to use tools, or message content
        """
        # Get backend config to fetch keep_alive setting and detect cloud models
        config = await self._get_backend_config(model)
        keep_alive = config.get("keep_alive_seconds", -1)

        # Auto-detect backend from model config if it's a cloud model
        # This overrides the caller's backend parameter for cloud models
        detected_backend = config.get("backend_type", backend)
        if detected_backend in ("openai", "anthropic", "google"):
            backend = detected_backend

        logger.info(
            "routing_tool_calling_request",
            model=model,
            backend=backend,
            tool_count=len(tools) if tools else 0,
            request_id=request_id,
            keep_alive=keep_alive
        )

        start_time = time.time()
        response = None

        try:
            if backend == "openai":
                # Get API key from admin backend for cloud OpenAI
                api_key = None
                model_id = model
                if "/" in model and model.split("/")[0] == "openai":
                    creds = await self._get_cloud_credentials("openai")
                    if creds and creds.get("api_key"):
                        api_key = creds["api_key"]
                        model_id = config.get("model_id", model.split("/")[-1])
                response = await self._generate_openai_with_tools(
                    model_id, messages, tools, temperature, max_tokens, request_id, api_key=api_key
                )
            elif backend == "anthropic":
                # Anthropic tool calling via their API
                creds = await self._get_cloud_credentials("anthropic")
                if not creds or not creds.get("api_key"):
                    raise ValueError("Anthropic API key not configured in admin backend")
                model_id = config.get("model_id", model.split("/")[-1] if "/" in model else model)
                response = await self._generate_anthropic_with_tools(
                    creds["api_key"], model_id, messages, tools, temperature, max_tokens, request_id
                )
            elif backend == "google":
                # Google Gemini tool calling
                creds = await self._get_cloud_credentials("google")
                if not creds or not creds.get("api_key"):
                    raise ValueError("Google API key not configured in admin backend")
                model_id = config.get("model_id", model.split("/")[-1] if "/" in model else model)
                response = await self._generate_google_with_tools(
                    creds["api_key"], model_id, messages, tools, temperature, max_tokens, request_id
                )
            elif backend == "ollama":
                response = await self._generate_ollama_with_tools(
                    model, messages, tools, temperature, max_tokens, request_id, keep_alive
                )
            else:
                raise ValueError(f"Unsupported backend for tool calling: {backend}")

            return response

        finally:
            duration = time.time() - start_time

            # Persist metrics for tool calling requests
            if response:
                # For tool calling, we estimate tokens from the response
                # Ollama returns eval_count, OpenAI doesn't directly provide this
                tokens = response.get("eval_count", 0)
                if tokens == 0 and "content" in response:
                    # Rough estimate: ~4 chars per token
                    tokens = len(response.get("content", "")) // 4
                tokens_per_sec = tokens / duration if duration > 0 and tokens > 0 else 0

                metric = {
                    "timestamp": start_time,
                    "model": model,
                    "backend": response.get("backend", backend),
                    "latency_seconds": duration,
                    "tokens": tokens,
                    "tokens_per_second": tokens_per_sec,
                    "request_id": request_id,
                    "session_id": kwargs.get("session_id"),
                    "user_id": kwargs.get("user_id"),
                    "zone": kwargs.get("zone"),
                    "intent": kwargs.get("intent")
                }
                self._metrics.append(metric)

                # Persist metric to database asynchronously
                import asyncio
                stage = kwargs.get("stage", "tool_calling")
                asyncio.create_task(self._persist_metric(metric, source="tool_calling", stage=stage))

            logger.info(
                "tool_calling_request_completed",
                model=model,
                backend=backend,
                duration=duration,
                request_id=request_id
            )

    async def _generate_openai_with_tools(
        self,
        model: str,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        request_id: Optional[str],
        api_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate using OpenAI API with tool calling.

        Uses the OpenAI chat completions API with function calling.
        """
        import openai

        # If api_key is provided, this is a cloud call - use standard OpenAI endpoint
        # Otherwise, check for local Ollama-compatible endpoint
        if api_key:
            # Cloud OpenAI call - no base_url needed
            client = openai.AsyncOpenAI(api_key=api_key)
        else:
            # Local server - get backend configuration for endpoint URL
            backend_config = await self._get_backend_config(model)
            endpoint_url = backend_config.get("endpoint_url") if backend_config else None
            api_key = os.getenv("OPENAI_API_KEY", "sk-local")

            if endpoint_url:
                # Strip /v1 suffix if present, as OpenAI client adds it automatically
                base_url = endpoint_url.rstrip("/v1").rstrip("/")
                client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
            else:
                client = openai.AsyncOpenAI(api_key=api_key)

        try:
            # Build request parameters
            request_params = {
                "model": model,
                "messages": messages,
                "temperature": temperature or 0.1,
                "max_tokens": max_tokens or 500
            }

            # Only include tools and tool_choice if tools are provided
            if tools:
                request_params["tools"] = tools
                request_params["tool_choice"] = "auto"

            # Call OpenAI
            response = await client.chat.completions.create(**request_params)

            # Extract response
            choice = response.choices[0]
            message = choice.message

            result = {
                "backend": "openai",
                "model": model,
                "finish_reason": choice.finish_reason
            }

            # Check if tool calls were made
            if message.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",  # Required by OpenAI API
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in message.tool_calls
                ]
                logger.info(
                    "openai_tool_calls_requested",
                    count=len(message.tool_calls),
                    functions=[tc.function.name for tc in message.tool_calls],
                    request_id=request_id
                )
            else:
                # No tool calls, return message content
                result["content"] = message.content

            return result

        except Exception as e:
            logger.error(
                "openai_tool_calling_error",
                model=model,
                error=str(e),
                request_id=request_id
            )
            raise

    async def _generate_anthropic_with_tools(
        self,
        api_key: str,
        model: str,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        request_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Generate using Anthropic API with tool calling.

        Uses the Anthropic messages API with tool use.
        """
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        client = anthropic.AsyncAnthropic(api_key=api_key)

        # Convert OpenAI format tools to Anthropic format
        anthropic_tools = []
        for tool in tools:
            if "function" in tool:
                func = tool["function"]
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                })

        # Convert messages - extract system message if present
        system_prompt = None
        anthropic_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                anthropic_messages.append(msg)

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens or 1024,
                system=system_prompt,
                messages=anthropic_messages,
                tools=anthropic_tools if anthropic_tools else None,
                temperature=temperature or 0.1
            )

            result = {
                "backend": "anthropic",
                "model": model,
                "finish_reason": response.stop_reason
            }

            # Check for tool use blocks
            tool_calls = []
            content = ""
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "function": {
                            "name": block.name,
                            "arguments": str(block.input) if isinstance(block.input, dict) else block.input
                        }
                    })
                elif block.type == "text":
                    content += block.text

            if tool_calls:
                result["tool_calls"] = tool_calls
                logger.info(
                    "anthropic_tool_calls_requested",
                    count=len(tool_calls),
                    functions=[tc["function"]["name"] for tc in tool_calls],
                    request_id=request_id
                )
            if content:
                result["content"] = content

            return result

        except Exception as e:
            logger.error("anthropic_tool_calling_error", model=model, error=str(e), request_id=request_id)
            raise

    async def _generate_google_with_tools(
        self,
        api_key: str,
        model: str,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        request_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Generate using Google Gemini API with tool calling.

        Note: Google tool calling uses a different format than OpenAI.
        """
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("google-generativeai package not installed. Run: pip install google-generativeai")

        genai.configure(api_key=api_key)

        # Convert OpenAI format tools to Google format
        google_tools = []
        for tool in tools:
            if "function" in tool:
                func = tool["function"]
                google_tools.append({
                    "function_declarations": [{
                        "name": func["name"],
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {"type": "object", "properties": {}})
                    }]
                })

        # Build prompt from messages
        prompt_parts = []
        for msg in messages:
            if msg["role"] == "system":
                prompt_parts.append(f"System: {msg['content']}")
            elif msg["role"] == "user":
                prompt_parts.append(msg["content"])
            elif msg["role"] == "assistant":
                prompt_parts.append(f"Assistant: {msg['content']}")

        try:
            gen_model = genai.GenerativeModel(model)
            response = await gen_model.generate_content_async(
                "\n".join(prompt_parts),
                tools=google_tools if google_tools else None,
                generation_config=genai.types.GenerationConfig(
                    temperature=temperature or 0.1,
                    max_output_tokens=max_tokens or 1024
                )
            )

            result = {
                "backend": "google",
                "model": model,
                "finish_reason": response.candidates[0].finish_reason.name if response.candidates else "unknown"
            }

            # Check for function calls
            if response.candidates and response.candidates[0].content.parts:
                tool_calls = []
                content = ""
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'function_call') and part.function_call:
                        tool_calls.append({
                            "id": f"google_{len(tool_calls)}",
                            "function": {
                                "name": part.function_call.name,
                                "arguments": str(dict(part.function_call.args))
                            }
                        })
                    elif hasattr(part, 'text'):
                        content += part.text

                if tool_calls:
                    result["tool_calls"] = tool_calls
                    logger.info(
                        "google_tool_calls_requested",
                        count=len(tool_calls),
                        functions=[tc["function"]["name"] for tc in tool_calls],
                        request_id=request_id
                    )
                if content:
                    result["content"] = content

            return result

        except Exception as e:
            logger.error("google_tool_calling_error", model=model, error=str(e), request_id=request_id)
            raise

    async def _generate_ollama_with_tools(
        self,
        model: str,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        request_id: Optional[str],
        keep_alive: int = -1
    ) -> Dict[str, Any]:
        """
        Generate using Ollama with tool calling support.

        Uses Ollama's native tool calling support (for models like llama3.1:8b).
        """
        endpoint_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        client = httpx.AsyncClient(base_url=endpoint_url, timeout=60.0)

        # Build payload
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {
                "temperature": temperature or 0.1,
                "num_predict": max_tokens or 500
            }
        }

        # Add keep_alive parameter (integer format in seconds)
        payload["keep_alive"] = keep_alive

        # Disable thinking mode for qwen3 models (they output to 'thinking' field by default)
        if "qwen3" in model.lower():
            payload["think"] = False
            logger.info("ollama_think_disabled", model=model)

        try:
            # Call Ollama with tools
            response = await client.post("/api/chat", json=payload)

            response.raise_for_status()
            data = response.json()

            # Extract message
            message = data.get("message", {})

            result = {
                "backend": "ollama",
                "model": model,
                "done": data.get("done", True),
                "eval_count": data.get("eval_count", 0),
                "total_duration": data.get("total_duration", 0)
            }

            # Check if tool calls were made
            if "tool_calls" in message:
                result["tool_calls"] = message["tool_calls"]
                logger.info(
                    "ollama_tool_calls_requested",
                    count=len(message["tool_calls"]),
                    functions=[tc["function"]["name"] for tc in message["tool_calls"]],
                    request_id=request_id
                )
            else:
                # No tool calls, return message content
                result["content"] = message.get("content", "")

            return result

        except Exception as e:
            logger.error(
                "ollama_tool_calling_error",
                model=model,
                error=str(e),
                request_id=request_id
            )
            raise
        finally:
            await client.aclose()

    async def _generate_ollama(
        self,
        endpoint_url: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
        keep_alive: int = -1,
        ollama_options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Generate using Ollama backend.

        Args:
            endpoint_url: Ollama API URL
            model: Model name
            prompt: Input prompt
            temperature: Generation temperature
            max_tokens: Max tokens to generate
            timeout: Request timeout
            keep_alive: How long to keep model loaded (-1=forever)
            ollama_options: Additional Ollama options (num_ctx, num_batch, mirostat, etc.)
        """
        client = httpx.AsyncClient(base_url=endpoint_url, timeout=timeout)

        # Build base options
        options = {
            "temperature": temperature,
            "num_predict": max_tokens
        }

        # Merge in additional Ollama options from model configuration
        if ollama_options:
            # Apply all Ollama options (num_ctx, num_batch, mirostat, top_k, top_p, etc.)
            for key, value in ollama_options.items():
                if value is not None:
                    options[key] = value
            logger.debug(
                "applying_ollama_options",
                model=model,
                options=list(ollama_options.keys())
            )

        # Build request payload
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options
        }

        # Add keep_alive parameter (Ollama uses integer format in seconds)
        # -1 = keep forever, 0 = unload immediately, >0 = seconds
        payload["keep_alive"] = keep_alive

        try:
            response = await client.post("/api/generate", json=payload)

            response.raise_for_status()
            data = response.json()

            return {
                "response": data.get("response"),
                "backend": "ollama",
                "model": model,
                "done": data.get("done", True),
                "total_duration": data.get("total_duration"),
                "eval_count": data.get("eval_count")
            }

        finally:
            await client.aclose()

    async def _generate_ollama_stream(
        self,
        endpoint_url: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
        keep_alive: int = -1,
        ollama_options: Optional[Dict[str, Any]] = None
    ):
        """
        Generate using Ollama backend with streaming.

        Yields tokens as they are generated for reduced time-to-first-token.

        Args:
            endpoint_url: Ollama API URL
            model: Model name
            prompt: Input prompt
            temperature: Generation temperature
            max_tokens: Max tokens to generate
            timeout: Request timeout
            keep_alive: How long to keep model loaded (-1=forever)
            ollama_options: Additional Ollama options

        Yields:
            Dict with 'token' key containing the generated token text
        """
        import json as json_lib

        # Build base options
        options = {
            "temperature": temperature,
            "num_predict": max_tokens
        }

        # Merge in additional Ollama options from model configuration
        if ollama_options:
            for key, value in ollama_options.items():
                if value is not None:
                    options[key] = value

        # Build request payload with stream=True
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,  # Enable streaming
            "options": options,
            "keep_alive": keep_alive
        }

        async with httpx.AsyncClient(base_url=endpoint_url, timeout=timeout) as client:
            async with client.stream("POST", "/api/generate", json=payload) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json_lib.loads(line)
                        token = data.get("response", "")

                        if token:
                            yield {
                                "token": token,
                                "done": data.get("done", False),
                                "model": model,
                                "backend": "ollama"
                            }

                        if data.get("done"):
                            # Final stats
                            yield {
                                "token": "",
                                "done": True,
                                "total_duration": data.get("total_duration"),
                                "eval_count": data.get("eval_count"),
                                "model": model,
                                "backend": "ollama"
                            }
                            break

                    except json_lib.JSONDecodeError:
                        logger.warning("ollama_stream_json_error", line=line[:100])
                        continue

    async def generate_stream(
        self,
        model: str,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: Optional[int] = None,
        backend: Optional[BackendType] = None
    ):
        """
        Generate response with streaming (yields tokens as generated).

        This is used for the streaming pipeline to reduce time-to-first-token.

        Args:
            model: Model name
            prompt: Input prompt
            temperature: Generation temperature
            max_tokens: Max tokens to generate
            timeout: Request timeout (uses model config if not specified)
            backend: Specific backend to use (auto-detected if not specified)

        Yields:
            Dict with 'token' key containing generated token text
        """
        # Get backend configuration (for endpoint URL)
        backend_config = await self._get_backend_config(model)
        endpoint_url = backend_config["endpoint_url"]
        backend_type = backend or backend_config["backend_type"]

        # Get model configuration (for Ollama options, timeout, etc.)
        model_config = await self._get_model_config(model)
        timeout = timeout or model_config.get("timeout_seconds") or backend_config.get("timeout_seconds", 60)
        ollama_options = model_config.get("ollama_options", {})
        keep_alive = model_config.get("keep_alive_seconds") if model_config.get("keep_alive_seconds") is not None else backend_config.get("keep_alive_seconds", -1)

        logger.info(
            "llm_stream_starting",
            model=model,
            backend=backend_type.value if hasattr(backend_type, 'value') else str(backend_type),
            endpoint=endpoint_url[:50] if endpoint_url else "cloud"
        )

        # Route to appropriate streaming backend
        if backend_type == BackendType.OLLAMA:
            async for chunk in self._generate_ollama_stream(
                endpoint_url=endpoint_url,
                model=model,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                keep_alive=keep_alive,
                ollama_options=ollama_options
            ):
                yield chunk

        elif backend_type == BackendType.OPENAI:
            # Cloud streaming - OpenAI
            credentials = await self._get_cloud_credentials("openai")
            if not credentials or not credentials.get("api_key"):
                raise ValueError("OpenAI API key not configured")

            async for chunk in self._generate_openai_stream(
                api_key=credentials["api_key"],
                model=backend_config.get("model_id", model),
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                system_prompt=None
            ):
                yield chunk

        elif backend_type == BackendType.ANTHROPIC:
            # Cloud streaming - Anthropic
            credentials = await self._get_cloud_credentials("anthropic")
            if not credentials or not credentials.get("api_key"):
                raise ValueError("Anthropic API key not configured")

            async for chunk in self._generate_anthropic_stream(
                api_key=credentials["api_key"],
                model=backend_config.get("model_id", model),
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                system_prompt=None
            ):
                yield chunk

        elif backend_type == BackendType.GOOGLE:
            # Google doesn't have good async streaming support yet
            # Fall back to non-streaming and yield all at once
            credentials = await self._get_cloud_credentials("google")
            if not credentials or not credentials.get("api_key"):
                raise ValueError("Google API key not configured")

            result = await self._generate_google(
                api_key=credentials["api_key"],
                model=backend_config.get("model_id", model),
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens
            )
            yield {
                "token": result.get("response", ""),
                "done": True,
                "model": model,
                "backend": "google",
                "eval_count": result.get("output_tokens", 0),
                "prompt_eval_count": result.get("input_tokens", 0)
            }

        else:
            # For MLX and others, fall back to non-streaming and yield all at once
            result = await self._generate_mlx(
                endpoint_url=endpoint_url,
                model=model,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                mlx_options=model_config.get("mlx_options", {})
            )
            yield {
                "token": result.get("response", ""),
                "done": True,
                "model": model,
                "backend": "mlx"
            }

    async def _generate_mlx(
        self,
        endpoint_url: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
        mlx_options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Generate using MLX backend.

        Args:
            endpoint_url: MLX server URL
            model: Model name
            prompt: Input prompt
            temperature: Generation temperature
            max_tokens: Max tokens to generate
            timeout: Request timeout
            mlx_options: Additional MLX options (max_kv_size, quantization, etc.)
        """
        client = httpx.AsyncClient(base_url=endpoint_url, timeout=timeout)

        # Build request payload
        payload = {
            "model": model,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        # Merge in additional MLX options from model configuration
        if mlx_options:
            for key, value in mlx_options.items():
                if value is not None:
                    payload[key] = value
            logger.debug(
                "applying_mlx_options",
                model=model,
                options=list(mlx_options.keys())
            )

        try:
            # MLX server uses OpenAI-compatible API
            response = await client.post("/v1/completions", json=payload)

            response.raise_for_status()
            data = response.json()

            choice = data["choices"][0]

            return {
                "response": choice["text"],
                "backend": "mlx",
                "model": model,
                "done": True,
                "total_duration": None,  # MLX doesn't provide this
                "eval_count": data.get("usage", {}).get("completion_tokens")
            }

        finally:
            await client.aclose()

    # =========================================================================
    # Cloud Provider Methods - Open Source Compatible
    # =========================================================================

    async def _get_cloud_credentials(self, provider: str) -> Optional[Dict[str, Any]]:
        """
        Fetch API credentials for a cloud provider from admin backend.

        Credentials are cached to avoid repeated API calls. The admin backend
        handles encryption/decryption.

        Args:
            provider: Cloud provider name ('openai', 'anthropic', 'google')

        Returns:
            Dict with 'api_key' and optional 'endpoint_url', or None if unavailable
        """
        now = time.time()

        # Check cache
        if provider in self._cloud_credentials_cache:
            if now < self._cloud_credentials_expiry.get(provider, 0):
                return self._cloud_credentials_cache[provider]

        try:
            # Fetch from admin API public endpoint
            url = f"{self.admin_url}/api/external-api-keys/public/{provider}/key"
            response = await self.client.get(url, timeout=5.0)

            if response.status_code == 200:
                data = response.json()
                creds = {
                    "api_key": data.get("api_key"),
                    "endpoint_url": data.get("endpoint_url"),
                    "enabled": data.get("enabled", False)
                }

                # Cache for 5 minutes
                self._cloud_credentials_cache[provider] = creds
                self._cloud_credentials_expiry[provider] = now + 300

                logger.debug(
                    "fetched_cloud_credentials",
                    provider=provider,
                    enabled=creds.get("enabled")
                )
                return creds
            else:
                logger.warning(
                    "cloud_credentials_not_found",
                    provider=provider,
                    status=response.status_code
                )
                return None

        except Exception as e:
            logger.error(
                "failed_to_fetch_cloud_credentials",
                provider=provider,
                error=str(e)
            )
            return None

    async def _get_model_pricing(self, provider: str, model: str) -> Dict[str, float]:
        """
        Get pricing for a cloud model (per 1M tokens).

        Tries database first, falls back to hardcoded defaults.

        Args:
            provider: Cloud provider name
            model: Model ID

        Returns:
            Dict with 'input' and 'output' costs per 1M tokens
        """
        now = time.time()

        # Check pricing cache
        if now < self._pricing_cache_expiry:
            provider_pricing = self._pricing_cache.get(provider, {})
            if model in provider_pricing:
                return provider_pricing[model]

        # Try to fetch from admin API
        try:
            url = f"{self.admin_url}/api/cloud-providers/pricing/{provider}/{model}"
            response = await self.client.get(url, timeout=3.0)

            if response.status_code == 200:
                data = response.json()
                pricing = {
                    "input": data.get("input_cost_per_1m", 0),
                    "output": data.get("output_cost_per_1m", 0)
                }
                # Update cache
                if provider not in self._pricing_cache:
                    self._pricing_cache[provider] = {}
                self._pricing_cache[provider][model] = pricing
                self._pricing_cache_expiry = now + 3600  # Cache for 1 hour

                return pricing
        except Exception as e:
            logger.debug("pricing_fetch_failed", provider=provider, model=model, error=str(e))

        # Fall back to defaults
        provider_defaults = DEFAULT_CLOUD_PRICING.get(provider, {})
        return provider_defaults.get(model, provider_defaults.get("default", {"input": 1.0, "output": 3.0}))

    def _calculate_cloud_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        pricing: Dict[str, float]
    ) -> float:
        """Calculate cost in USD from token counts."""
        input_cost = (input_tokens / 1_000_000) * pricing.get("input", 0)
        output_cost = (output_tokens / 1_000_000) * pricing.get("output", 0)
        return round(input_cost + output_cost, 6)

    async def generate_cloud(
        self,
        provider: str,
        model: str,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        system_prompt: Optional[str] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        zone: Optional[str] = None,
        intent: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate text using a cloud LLM provider.

        Supports OpenAI, Anthropic, and Google. Automatically fetches API keys
        from admin backend and tracks usage/costs.

        Args:
            provider: Cloud provider ('openai', 'anthropic', 'google')
            model: Model name (e.g., 'gpt-4o-mini', 'claude-sonnet-4-20250514')
            prompt: Input prompt
            temperature: Generation temperature
            max_tokens: Max tokens to generate
            system_prompt: Optional system prompt
            request_id: Request ID for tracking
            session_id: Session ID for conversation tracking
            user_id: User ID for analytics
            zone: Zone/room for location tracking
            intent: Classified intent
            **kwargs: Additional provider-specific parameters

        Returns:
            Generated response with metadata including cost
        """
        # Get credentials
        creds = await self._get_cloud_credentials(provider)
        if not creds or not creds.get("api_key"):
            logger.error("cloud_credentials_missing", provider=provider)
            raise ValueError(f"Cloud provider {provider} not configured or API key missing")

        if not creds.get("enabled", True):
            logger.warning("cloud_provider_disabled", provider=provider)
            raise ValueError(f"Cloud provider {provider} is disabled")

        # Get pricing for cost tracking
        pricing = await self._get_model_pricing(provider, model)

        logger.info(
            "routing_cloud_request",
            provider=provider,
            model=model,
            request_id=request_id
        )

        start_time = time.time()
        ttft = None  # Time to first token (for streaming)
        response = None

        try:
            if provider == BackendType.OPENAI.value:
                response = await self._generate_openai(
                    api_key=creds["api_key"],
                    model=model,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    request_id=request_id
                )
            elif provider == BackendType.ANTHROPIC.value:
                response = await self._generate_anthropic(
                    api_key=creds["api_key"],
                    model=model,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    request_id=request_id
                )
            elif provider == BackendType.GOOGLE.value:
                response = await self._generate_google(
                    api_key=creds["api_key"],
                    model=model,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    request_id=request_id
                )
            else:
                raise ValueError(f"Unsupported cloud provider: {provider}")

            return response

        finally:
            duration = time.time() - start_time
            latency_ms = int(duration * 1000)

            # Track cloud usage
            if response:
                input_tokens = response.get("input_tokens", 0)
                output_tokens = response.get("output_tokens", 0)
                cost_usd = self._calculate_cloud_cost(provider, model, input_tokens, output_tokens, pricing)

                # Add cost to response
                response["cost_usd"] = cost_usd
                response["latency_ms"] = latency_ms

                # Persist cloud usage to database
                import asyncio
                asyncio.create_task(self._track_cloud_usage(
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    ttft_ms=ttft,
                    streaming=False,
                    request_id=request_id,
                    session_id=session_id,
                    user_id=user_id,
                    zone=zone,
                    intent=intent
                ))

                logger.info(
                    "cloud_request_completed",
                    provider=provider,
                    model=model,
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    request_id=request_id
                )

    async def _generate_openai(
        self,
        api_key: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate using OpenAI API.

        Uses the official openai library for compatibility and reliability.
        """
        try:
            import openai
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        client = openai.AsyncOpenAI(api_key=api_key)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )

            choice = response.choices[0]
            usage = response.usage

            return {
                "response": choice.message.content,
                "backend": "openai",
                "model": model,
                "done": True,
                "input_tokens": usage.prompt_tokens if usage else 0,
                "output_tokens": usage.completion_tokens if usage else 0,
                "finish_reason": choice.finish_reason
            }

        except Exception as e:
            logger.error("openai_generation_error", model=model, error=str(e), request_id=request_id)
            raise

    async def _generate_anthropic(
        self,
        api_key: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate using Anthropic API.

        Uses the official anthropic library for compatibility and reliability.
        """
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        client = anthropic.AsyncAnthropic(api_key=api_key)

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt or "You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature
            )

            # Extract text from content blocks
            text_content = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text_content += block.text

            return {
                "response": text_content,
                "backend": "anthropic",
                "model": model,
                "done": True,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "finish_reason": response.stop_reason
            }

        except Exception as e:
            logger.error("anthropic_generation_error", model=model, error=str(e), request_id=request_id)
            raise

    async def _generate_google(
        self,
        api_key: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate using Google Gemini API.

        Uses the official google-generativeai library.
        """
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("google-generativeai package not installed. Run: pip install google-generativeai")

        genai.configure(api_key=api_key)

        try:
            gen_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt
            )

            # Configure generation
            generation_config = genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens
            )

            response = await gen_model.generate_content_async(
                prompt,
                generation_config=generation_config
            )

            # Extract usage metadata
            usage_metadata = getattr(response, 'usage_metadata', None)
            input_tokens = usage_metadata.prompt_token_count if usage_metadata else 0
            output_tokens = usage_metadata.candidates_token_count if usage_metadata else 0

            return {
                "response": response.text,
                "backend": "google",
                "model": model,
                "done": True,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "finish_reason": "stop"
            }

        except Exception as e:
            logger.error("google_generation_error", model=model, error=str(e), request_id=request_id)
            raise

    async def _track_cloud_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        ttft_ms: Optional[int] = None,
        streaming: bool = False,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        zone: Optional[str] = None,
        intent: Optional[str] = None,
        was_fallback: bool = False,
        fallback_reason: Optional[str] = None
    ):
        """
        Track cloud LLM usage in the database.

        Persists usage data for cost analytics and monitoring.
        """
        try:
            url = f"{self.admin_url}/api/cloud-llm-usage"
            payload = {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "ttft_ms": ttft_ms,
                "streaming": streaming,
                "request_id": request_id,
                "session_id": session_id,
                "user_id": user_id,
                "zone": zone,
                "intent": intent,
                "was_fallback": was_fallback,
                "fallback_reason": fallback_reason
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=5.0)
                if response.status_code not in (200, 201):
                    logger.warning(
                        "failed_to_track_cloud_usage",
                        status=response.status_code,
                        error=response.text[:100]
                    )
        except Exception as e:
            logger.error("cloud_usage_tracking_error", error=str(e))

    # =========================================================================
    # Cloud Provider Streaming Methods - Phase 4
    # =========================================================================

    async def _generate_openai_stream(
        self,
        api_key: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        zone: Optional[str] = None,
        intent: Optional[str] = None
    ):
        """
        Stream generation using OpenAI API.

        Yields tokens as they are generated for reduced time-to-first-token.
        Uses stream_options to get accurate token counts from the API.
        """
        try:
            import openai
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        client = openai.AsyncOpenAI(api_key=api_key)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        start_time = time.time()
        first_token_time = None
        input_tokens = 0
        output_tokens = 0

        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True}  # Request token counts
            )

            async for chunk in stream:
                # Check for usage metadata in final chunk
                if hasattr(chunk, 'usage') and chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0

                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'content') and delta.content:
                        if first_token_time is None:
                            first_token_time = time.time()

                        yield {
                            "token": delta.content,
                            "done": False,
                            "model": model,
                            "backend": "openai"
                        }

            duration = time.time() - start_time
            ttft_ms = int((first_token_time - start_time) * 1000) if first_token_time else None

            # Calculate cost and track usage
            pricing = await self._get_model_pricing("openai", model)
            cost = self._calculate_cloud_cost("openai", model, input_tokens, output_tokens, pricing)
            await self._track_cloud_usage(
                provider="openai",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=int(duration * 1000),
                ttft_ms=ttft_ms,
                streaming=True,
                request_id=request_id,
                session_id=session_id,
                zone=zone,
                intent=intent
            )

            # Yield final stats
            yield {
                "token": "",
                "done": True,
                "model": model,
                "backend": "openai",
                "eval_count": output_tokens,
                "prompt_eval_count": input_tokens,
                "total_duration": int(duration * 1e9)
            }

        except Exception as e:
            logger.error("openai_stream_error", model=model, error=str(e), request_id=request_id)
            raise

    async def _generate_anthropic_stream(
        self,
        api_key: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        zone: Optional[str] = None,
        intent: Optional[str] = None
    ):
        """
        Stream generation using Anthropic API.

        Yields tokens as they are generated for reduced time-to-first-token.
        Captures accurate token counts from message events.
        """
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        client = anthropic.AsyncAnthropic(api_key=api_key)

        start_time = time.time()
        first_token_time = None
        input_tokens = 0
        output_tokens = 0

        try:
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt or "You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature
            ) as stream:
                async for event in stream:
                    if event.type == "message_start":
                        # Capture input token count from message start
                        if hasattr(event, 'message') and hasattr(event.message, 'usage'):
                            input_tokens = event.message.usage.input_tokens

                    elif event.type == "content_block_delta":
                        if first_token_time is None:
                            first_token_time = time.time()

                        if hasattr(event, 'delta') and hasattr(event.delta, 'text'):
                            yield {
                                "token": event.delta.text,
                                "done": False,
                                "model": model,
                                "backend": "anthropic"
                            }

                    elif event.type == "message_delta":
                        # Capture output token count from final delta
                        if hasattr(event, 'usage') and event.usage:
                            output_tokens = event.usage.output_tokens or 0

            duration = time.time() - start_time
            ttft_ms = int((first_token_time - start_time) * 1000) if first_token_time else None

            # Calculate cost and track usage
            pricing = await self._get_model_pricing("anthropic", model)
            cost = self._calculate_cloud_cost("anthropic", model, input_tokens, output_tokens, pricing)
            await self._track_cloud_usage(
                provider="anthropic",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=int(duration * 1000),
                ttft_ms=ttft_ms,
                streaming=True,
                request_id=request_id,
                session_id=session_id,
                zone=zone,
                intent=intent
            )

            # Yield final stats
            yield {
                "token": "",
                "done": True,
                "model": model,
                "backend": "anthropic",
                "eval_count": output_tokens,
                "prompt_eval_count": input_tokens,
                "total_duration": int(duration * 1e9)
            }

        except Exception as e:
            logger.error("anthropic_stream_error", model=model, error=str(e), request_id=request_id)
            raise

    # =========================================================================
    # Tool Schema Conversion Methods - Phase 4
    # =========================================================================

    def _convert_tools_for_provider(self, tools: List[Dict], provider: str) -> List[Dict]:
        """
        Convert OpenAI-format tool schemas to provider-specific format.

        The orchestrator and RAG services use OpenAI format as the canonical schema.
        This method converts to provider-specific formats for cloud backends.

        Args:
            tools: List of tools in OpenAI format
            provider: Target provider (openai, anthropic, google)

        Returns:
            List of tools in provider-specific format
        """
        if provider == "anthropic":
            return self._tools_to_anthropic_format(tools)
        elif provider == "google":
            return self._tools_to_google_format(tools)
        else:
            # OpenAI format is the default/canonical format
            return tools

    def _tools_to_anthropic_format(self, tools: List[Dict]) -> List[Dict]:
        """
        Convert OpenAI-format tools to Anthropic format.

        OpenAI: {"type": "function", "function": {"name": ..., "parameters": ...}}
        Anthropic: {"name": ..., "input_schema": ...}
        """
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                })
        return anthropic_tools

    def _tools_to_google_format(self, tools: List[Dict]) -> List[Dict]:
        """
        Convert OpenAI-format tools to Google Gemini format.

        OpenAI: {"type": "function", "function": {"name": ..., "parameters": ...}}
        Google: {"function_declarations": [{"name": ..., "parameters": ...}]}
        """
        function_declarations = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                function_declarations.append({
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {"type": "object", "properties": {}})
                })
        return [{"function_declarations": function_declarations}] if function_declarations else []

    def _normalize_tool_response(self, response: Any, provider: str) -> Dict:
        """
        Normalize tool call response to common OpenAI-compatible format.

        This allows the orchestrator to handle tool calls uniformly regardless
        of which provider was used.

        Args:
            response: Provider-specific response object
            provider: Source provider (anthropic, google)

        Returns:
            Normalized response with tool_calls in OpenAI format
        """
        import json as json_lib
        import uuid

        if provider == "anthropic":
            # Anthropic returns tool_use blocks in content
            tool_calls = []
            for block in response.content:
                if hasattr(block, 'type') and block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json_lib.dumps(block.input)
                        }
                    })
            return {"tool_calls": tool_calls, "done": True}

        elif provider == "google":
            # Google returns function_call in parts
            tool_calls = []
            for candidate in response.candidates:
                if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                    for part in candidate.content.parts:
                        if hasattr(part, 'function_call'):
                            fc = part.function_call
                            tool_calls.append({
                                "id": f"google_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": fc.name,
                                    "arguments": json_lib.dumps(dict(fc.args))
                                }
                            })
            return {"tool_calls": tool_calls, "done": True}

        else:
            # OpenAI format - already normalized
            return response

    async def _persist_metric(self, metric: Dict[str, Any], source: Optional[str] = None, stage: Optional[str] = None):
        """
        Persist metric to database via Admin API.

        Args:
            metric: Metric data to persist
            source: Optional source service (gateway, orchestrator, etc.)
            stage: Optional pipeline stage (classify, summarize, tool_selection, validation, synthesize, etc.)

        Note:
            Failures are logged but don't raise exceptions to avoid
            impacting LLM request processing.
        """
        if not self._persist_metrics:
            return

        try:
            # Add source and stage to metric payload if provided
            if source:
                metric["source"] = source
            if stage:
                metric["stage"] = stage

            url = f"{self._admin_url_base}/api/llm-backends/metrics"
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=metric, timeout=5.0)
                if response.status_code != 201:
                    logger.warning(
                        "failed_to_persist_metric",
                        status_code=response.status_code,
                        error=response.text
                    )
        except Exception as e:
            logger.error("metric_persistence_error", error=str(e))

    def report_metrics(self) -> Dict[str, Any]:
        """
        Report aggregated performance metrics from rolling window.

        Returns:
            Dict with overall and per-model metrics including:
            - avg_latency_seconds: Average request latency
            - avg_tokens_per_second: Average token generation speed
            - total_requests: Number of requests tracked
            - by_model: Per-model breakdown
            - by_backend: Per-backend breakdown
        """
        if not self._metrics:
            return {
                "total_requests": 0,
                "avg_latency_seconds": 0.0,
                "avg_tokens_per_second": 0.0,
                "by_model": {},
                "by_backend": {}
            }

        # Overall metrics
        total_requests = len(self._metrics)
        total_latency = sum(m["latency_seconds"] for m in self._metrics)
        total_tokens_per_sec = sum(m["tokens_per_second"] for m in self._metrics if m["tokens_per_second"] > 0)
        requests_with_tokens = sum(1 for m in self._metrics if m["tokens_per_second"] > 0)

        avg_latency = total_latency / total_requests if total_requests > 0 else 0.0
        avg_tokens_per_sec = total_tokens_per_sec / requests_with_tokens if requests_with_tokens > 0 else 0.0

        # Per-model metrics
        by_model: Dict[str, Dict[str, Any]] = {}
        for metric in self._metrics:
            model = metric["model"]
            if model not in by_model:
                by_model[model] = {
                    "requests": 0,
                    "total_latency": 0.0,
                    "total_tokens_per_sec": 0.0,
                    "requests_with_tokens": 0
                }

            by_model[model]["requests"] += 1
            by_model[model]["total_latency"] += metric["latency_seconds"]
            if metric["tokens_per_second"] > 0:
                by_model[model]["total_tokens_per_sec"] += metric["tokens_per_second"]
                by_model[model]["requests_with_tokens"] += 1

        # Calculate averages for each model
        for model, stats in by_model.items():
            stats["avg_latency_seconds"] = stats["total_latency"] / stats["requests"]
            stats["avg_tokens_per_second"] = (
                stats["total_tokens_per_sec"] / stats["requests_with_tokens"]
                if stats["requests_with_tokens"] > 0 else 0.0
            )
            # Remove intermediate totals
            del stats["total_latency"]
            del stats["total_tokens_per_sec"]
            del stats["requests_with_tokens"]

        # Per-backend metrics
        by_backend: Dict[str, Dict[str, Any]] = {}
        for metric in self._metrics:
            backend = metric["backend"]
            if backend not in by_backend:
                by_backend[backend] = {
                    "requests": 0,
                    "total_latency": 0.0,
                    "total_tokens_per_sec": 0.0,
                    "requests_with_tokens": 0
                }

            by_backend[backend]["requests"] += 1
            by_backend[backend]["total_latency"] += metric["latency_seconds"]
            if metric["tokens_per_second"] > 0:
                by_backend[backend]["total_tokens_per_sec"] += metric["tokens_per_second"]
                by_backend[backend]["requests_with_tokens"] += 1

        # Calculate averages for each backend
        for backend, stats in by_backend.items():
            stats["avg_latency_seconds"] = stats["total_latency"] / stats["requests"]
            stats["avg_tokens_per_second"] = (
                stats["total_tokens_per_sec"] / stats["requests_with_tokens"]
                if stats["requests_with_tokens"] > 0 else 0.0
            )
            # Remove intermediate totals
            del stats["total_latency"]
            del stats["total_tokens_per_sec"]
            del stats["requests_with_tokens"]

        return {
            "total_requests": total_requests,
            "avg_latency_seconds": round(avg_latency, 3),
            "avg_tokens_per_second": round(avg_tokens_per_sec, 2),
            "by_model": by_model,
            "by_backend": by_backend,
            "window_size": self._metrics_window_size
        }

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


# Singleton instance
_router: Optional[LLMRouter] = None


def get_llm_router() -> LLMRouter:
    """Get or create LLM router singleton."""
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router
