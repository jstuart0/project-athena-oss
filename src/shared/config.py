"""
Centralized configuration for all Athena services.
Secure-by-default: critical settings MUST be explicitly configured.

This module provides a single source of truth for configuration across all
Athena services, with proper validation and fail-fast behavior for required settings.
"""
import os
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""
    pass


@dataclass
class AthenaConfig:
    """
    Configuration container with validation.

    Configuration Precedence (highest to lowest):
    1. Admin Backend (runtime) - checked via get_with_admin_override()
    2. Environment Variables
    3. Config Files (.env)
    4. Code Defaults (only for non-sensitive, optional values)
    """

    # =========================================================================
    # REQUIRED - No defaults, fail fast if missing
    # =========================================================================

    # Database password - NEVER have a default
    db_password: str = field(default_factory=lambda: os.environ.get("ATHENA_DB_PASSWORD", ""))

    # Admin API URL - REQUIRED for service coordination
    admin_api_url: str = field(default_factory=lambda: os.environ.get("ADMIN_API_URL", ""))

    # Security keys - REQUIRED, no weak defaults
    encryption_key: str = field(default_factory=lambda: os.environ.get("ENCRYPTION_KEY", ""))
    encryption_salt: str = field(default_factory=lambda: os.environ.get("ENCRYPTION_SALT", ""))
    session_secret: str = field(default_factory=lambda: os.environ.get("SESSION_SECRET_KEY", ""))
    jwt_secret: str = field(default_factory=lambda: os.environ.get("JWT_SECRET", ""))

    # =========================================================================
    # OPTIONAL - Sensible defaults for development
    # =========================================================================

    # Infrastructure (defaults to localhost for local development)
    ollama_host: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "localhost"))
    ollama_port: int = field(default_factory=lambda: int(os.environ.get("OLLAMA_PORT", "11434")))

    qdrant_host: str = field(default_factory=lambda: os.environ.get("QDRANT_HOST", "localhost"))
    qdrant_port: int = field(default_factory=lambda: int(os.environ.get("QDRANT_PORT", "6333")))

    redis_host: str = field(default_factory=lambda: os.environ.get("REDIS_HOST", "localhost"))
    redis_port: int = field(default_factory=lambda: int(os.environ.get("REDIS_PORT", "6379")))

    # Database (host/name have defaults, password does NOT)
    db_host: str = field(default_factory=lambda: os.environ.get("ATHENA_DB_HOST", "localhost"))
    db_port: int = field(default_factory=lambda: int(os.environ.get("ATHENA_DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: os.environ.get("ATHENA_DB_NAME", "athena"))
    db_user: str = field(default_factory=lambda: os.environ.get("ATHENA_DB_USER", "athena"))

    # Admin database (defaults to main database if not specified)
    admin_db_host: str = field(default_factory=lambda: os.environ.get("ATHENA_ADMIN_DB_HOST", os.environ.get("ATHENA_DB_HOST", "localhost")))
    admin_db_port: int = field(default_factory=lambda: int(os.environ.get("ATHENA_ADMIN_DB_PORT", os.environ.get("ATHENA_DB_PORT", "5432"))))
    admin_db_name: str = field(default_factory=lambda: os.environ.get("ATHENA_ADMIN_DB_NAME", "athena_admin"))
    admin_db_user: str = field(default_factory=lambda: os.environ.get("ATHENA_ADMIN_DB_USER", os.environ.get("ATHENA_DB_USER", "athena")))

    # Personalization (optional, blank = no default)
    default_city: str = field(default_factory=lambda: os.environ.get("DEFAULT_CITY", ""))
    default_state: str = field(default_factory=lambda: os.environ.get("DEFAULT_STATE", ""))
    default_country: str = field(default_factory=lambda: os.environ.get("DEFAULT_COUNTRY", "US"))
    default_timezone: str = field(default_factory=lambda: os.environ.get("DEFAULT_TIMEZONE", "UTC"))

    # Module enables (default True for backwards compatibility)
    home_assistant_enabled: bool = field(default_factory=lambda: os.environ.get("MODULE_HOME_ASSISTANT", "true").lower() == "true")
    guest_mode_enabled: bool = field(default_factory=lambda: os.environ.get("MODULE_GUEST_MODE", "true").lower() == "true")
    notifications_enabled: bool = field(default_factory=lambda: os.environ.get("MODULE_NOTIFICATIONS", "true").lower() == "true")
    monitoring_enabled: bool = field(default_factory=lambda: os.environ.get("MODULE_MONITORING", "false").lower() == "true")

    # Home Assistant configuration
    ha_url: str = field(default_factory=lambda: os.environ.get("HA_URL", ""))
    ha_token: str = field(default_factory=lambda: os.environ.get("HA_TOKEN", ""))

    # Service ports
    gateway_port: int = field(default_factory=lambda: int(os.environ.get("GATEWAY_PORT", "8000")))
    orchestrator_port: int = field(default_factory=lambda: int(os.environ.get("ORCHESTRATOR_PORT", "8001")))
    admin_port: int = field(default_factory=lambda: int(os.environ.get("ADMIN_PORT", "8080")))

    # =========================================================================
    # Computed Properties
    # =========================================================================

    @property
    def ollama_url(self) -> str:
        """Get Ollama URL, preferring explicit URL over host:port."""
        return os.environ.get("OLLAMA_URL") or os.environ.get("LLM_SERVICE_URL") or f"http://{self.ollama_host}:{self.ollama_port}"

    @property
    def qdrant_url(self) -> str:
        """Get Qdrant URL, preferring explicit URL over host:port."""
        return os.environ.get("QDRANT_URL", f"http://{self.qdrant_host}:{self.qdrant_port}")

    @property
    def redis_url(self) -> str:
        """Get Redis URL, preferring explicit URL over host:port."""
        return os.environ.get("REDIS_URL", f"redis://{self.redis_host}:{self.redis_port}")

    @property
    def database_url(self) -> str:
        """Get main database connection URL."""
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def admin_database_url(self) -> str:
        """Get admin database connection URL."""
        return f"postgresql://{self.admin_db_user}:{self.db_password}@{self.admin_db_host}:{self.admin_db_port}/{self.admin_db_name}"

    # =========================================================================
    # Validation
    # =========================================================================

    def validate(self, service_name: str = "athena", require_admin: bool = True, require_db: bool = True) -> List[str]:
        """
        Validate configuration and return list of errors.
        Call at service startup to fail fast with clear errors.

        Args:
            service_name: Name of service for error messages
            require_admin: Whether admin API URL is required (True for most services)
            require_db: Whether database password is required (True for most services)

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        # Required: Database password (for services that need DB)
        if require_db and not self.db_password:
            errors.append(
                f"ATHENA_DB_PASSWORD is required but not set.\n"
                f"  Set via environment variable: export ATHENA_DB_PASSWORD='your-password'\n"
                f"  Or in .env file: ATHENA_DB_PASSWORD=your-password"
            )

        # Required: Admin API URL (for most services)
        if require_admin and not self.admin_api_url:
            errors.append(
                f"ADMIN_API_URL is required but not set.\n"
                f"  Set via environment variable: export ADMIN_API_URL='http://admin-backend:8080'\n"
                f"  Or in .env file: ADMIN_API_URL=http://admin-backend:8080"
            )

        # Required: Security keys (for admin backend)
        if service_name == "admin-backend":
            if not self.encryption_key:
                errors.append(
                    f"ENCRYPTION_KEY is required for admin backend.\n"
                    f"  Generate with: openssl rand -base64 32"
                )
            if not self.encryption_salt:
                errors.append(
                    f"ENCRYPTION_SALT is required for admin backend.\n"
                    f"  Generate with: openssl rand -base64 16"
                )
            if not self.session_secret:
                errors.append(
                    f"SESSION_SECRET_KEY is required for admin backend.\n"
                    f"  Generate with: openssl rand -base64 32"
                )

        # Required: Home Assistant config if HA module enabled
        if self.home_assistant_enabled:
            if not self.ha_url:
                # This is a warning, not an error - HA config can come from admin backend
                logger.warning(
                    "HA_URL not set but MODULE_HOME_ASSISTANT is enabled. "
                    "Home Assistant integration will require configuration via admin backend."
                )
            if not self.ha_token:
                logger.warning(
                    "HA_TOKEN not set but MODULE_HOME_ASSISTANT is enabled. "
                    "Home Assistant integration will require configuration via admin backend."
                )

        return errors

    def validate_or_exit(self, service_name: str = "athena", require_admin: bool = True, require_db: bool = True):
        """Validate configuration and exit with clear error if invalid."""
        errors = self.validate(service_name, require_admin, require_db)
        if errors:
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"CONFIGURATION ERROR - {service_name} cannot start", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)
            for i, error in enumerate(errors, 1):
                print(f"{i}. {error}\n", file=sys.stderr)
            print(f"{'='*60}", file=sys.stderr)
            print(f"Fix the above issues and restart the service.", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)
            sys.exit(1)

    def check_conflicts(self, admin_values: Dict[str, Any]) -> List[str]:
        """
        Check for conflicts between env vars and admin backend values.
        Returns list of warnings (not errors - admin backend wins).
        """
        warnings = []

        # Map of config keys to check
        check_keys = {
            "OLLAMA_URL": ("ollama_url", self.ollama_url),
            "QDRANT_URL": ("qdrant_url", self.qdrant_url),
            "REDIS_URL": ("redis_url", self.redis_url),
        }

        for env_key, (attr_name, env_value) in check_keys.items():
            admin_value = admin_values.get(attr_name)
            if admin_value and admin_value != env_value:
                warnings.append(
                    f"Config conflict for {env_key}: "
                    f"env='{env_value}' vs admin='{admin_value}'. "
                    f"Using admin backend value."
                )

        return warnings

    def get_service_url(self, service_name: str) -> str:
        """
        Get URL for a service by name.

        Args:
            service_name: Service name (e.g., "orchestrator", "gateway", "mode_service")

        Returns:
            Service URL from environment or default
        """
        # Check for explicit URL env var
        env_var = f"{service_name.upper()}_URL"
        explicit_url = os.environ.get(env_var)
        if explicit_url:
            return explicit_url

        # Check for host/port env vars
        host_var = f"{service_name.upper()}_HOST"
        port_var = f"{service_name.upper()}_PORT"
        host = os.environ.get(host_var, "localhost")
        port = os.environ.get(port_var)

        # Default ports by service
        default_ports = {
            "gateway": "8000",
            "orchestrator": "8001",
            "admin_backend": "8080",
            "mode_service": "8022",
            "notifications": "8050",
            "weather": "8010",
            "airports": "8011",
            "sports": "8012",
            "stocks": "8016",
            "flights": "8013",
            "events": "8014",
            "streaming": "8015",
            "news": "8017",
            "websearch": "8018",
            "dining": "8019",
            "recipes": "8020",
            "directions": "8030",
        }

        if not port:
            port = default_ports.get(service_name.lower(), "8000")

        return f"http://{host}:{port}"


# Singleton instance
config = AthenaConfig()


def get_config() -> AthenaConfig:
    """Get the singleton config instance."""
    return config
