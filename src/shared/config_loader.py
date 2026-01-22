"""
Centralized configuration loader for Project Athena.

Loads configuration from config/services.yaml with environment variable overrides.
Provides type-safe access to service URLs, ports, and feature flags.
"""

import os
import yaml
from typing import Dict, Any, Optional
from pathlib import Path

from .logging_config import configure_logging

logger = configure_logging("config-loader")


class ConfigLoader:
    """
    Load and manage application configuration.

    Priority (highest to lowest):
    1. Environment variables
    2. config/services.yaml
    3. Default values
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize configuration loader.

        Args:
            config_path: Path to YAML config file (defaults to config/services.yaml)
        """
        if config_path is None:
            # Find project root by looking for config directory
            current = Path(__file__).resolve()
            for parent in current.parents:
                config_file = parent / "config" / "services.yaml"
                if config_file.exists():
                    config_path = str(config_file)
                    break

            if config_path is None:
                logger.warning("Could not find config/services.yaml, using defaults")
                self.config = {}
            else:
                logger.info(f"Loading configuration from {config_path}")
                with open(config_path, 'r') as f:
                    self.config = yaml.safe_load(f)
        else:
            with open(config_path, 'r') as f:
                self.config = yaml.safe_load(f)

    def get(self, key_path: str, default: Any = None, env_var: Optional[str] = None) -> Any:
        """
        Get configuration value with environment variable override.

        Args:
            key_path: Dot-separated path (e.g., "admin_api.url")
            default: Default value if not found
            env_var: Environment variable name to check first

        Returns:
            Configuration value
        """
        # Check environment variable first
        if env_var and os.getenv(env_var):
            return os.getenv(env_var)

        # Navigate config dictionary
        keys = key_path.split('.')
        value = self.config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    def get_service_url(self, service: str, env_var: Optional[str] = None) -> str:
        """
        Get service URL with environment variable override.

        Args:
            service: Service name (e.g., "admin_api", "orchestrator", "gateway")
            env_var: Environment variable name (defaults to <SERVICE>_URL)

        Returns:
            Service URL
        """
        if env_var is None:
            env_var = f"{service.upper()}_URL"

        return self.get(f"{service}.url", default=None, env_var=env_var)

    def get_service_port(self, service: str, env_var: Optional[str] = None) -> int:
        """
        Get service port with environment variable override.

        Args:
            service: Service name
            env_var: Environment variable name (defaults to <SERVICE>_PORT)

        Returns:
            Service port
        """
        if env_var is None:
            env_var = f"{service.upper()}_PORT"

        port_str = os.getenv(env_var)
        if port_str:
            return int(port_str)

        return self.get(f"{service}.port", default=8000)

    def get_admin_api_url(self) -> str:
        """Get Admin API URL."""
        return self.get_service_url("admin_api", env_var="ADMIN_API_URL")

    def get_orchestrator_url(self) -> str:
        """Get Orchestrator URL."""
        return self.get_service_url("orchestrator", env_var="ORCHESTRATOR_URL")

    def get_gateway_url(self) -> str:
        """Get Gateway URL."""
        return self.get_service_url("gateway", env_var="GATEWAY_URL")

    def get_ollama_url(self) -> str:
        """Get Ollama URL."""
        return self.get_service_url("ollama", env_var="OLLAMA_BASE_URL")

    def get_redis_url(self) -> str:
        """Get Redis URL."""
        return self.get("redis.url", env_var="REDIS_URL", default="redis://localhost:6379/0")

    def get_qdrant_url(self) -> str:
        """Get Qdrant URL."""
        return self.get("qdrant.url", env_var="QDRANT_URL", default="http://localhost:6333")

    def get_rag_service_url(self, service_name: str) -> str:
        """
        Get RAG service URL.

        Args:
            service_name: RAG service name (e.g., "weather", "sports")

        Returns:
            Service URL
        """
        env_var = f"{service_name.upper()}_SERVICE_URL"
        return self.get(f"rag_services.{service_name}.url", env_var=env_var)

    def get_rag_service_port(self, service_name: str) -> int:
        """Get RAG service port."""
        env_var = f"{service_name.upper()}_SERVICE_PORT"
        return self.get_service_port(f"rag_services.{service_name}", env_var=env_var)

    def get_all_rag_services(self) -> Dict[str, Dict[str, Any]]:
        """Get all RAG service configurations."""
        return self.config.get("rag_services", {})

    def get_feature_flag(self, flag_name: str) -> bool:
        """
        Get feature flag value.

        Args:
            flag_name: Flag name (e.g., "llm_based_routing")

        Returns:
            Boolean flag value
        """
        env_var = f"FEATURE_{flag_name.upper()}"
        env_value = os.getenv(env_var)

        if env_value is not None:
            return env_value.lower() in ('true', '1', 'yes', 'on')

        return self.get(f"feature_flags.{flag_name}", default=False)

    def get_retry_config(self) -> Dict[str, Any]:
        """Get retry configuration."""
        return {
            "max_retries": self.get("retry.max_retries", default=3),
            "initial_delay": self.get("retry.initial_delay", default=1.0),
            "max_delay": self.get("retry.max_delay", default=30.0),
            "exponential_backoff": self.get("retry.exponential_backoff", default=True)
        }

    def get_health_check_config(self) -> Dict[str, Any]:
        """Get health check configuration."""
        return {
            "interval": self.get("health_checks.interval", default=30),
            "timeout": self.get("health_checks.timeout", default=5),
            "startup_grace_period": self.get("health_checks.startup_grace_period", default=60)
        }


# Global configuration instance
_config: Optional[ConfigLoader] = None


def get_config() -> ConfigLoader:
    """Get global configuration instance (singleton)."""
    global _config
    if _config is None:
        _config = ConfigLoader()
    return _config
