"""
Voice Configuration Manager for Project Athena.

Provides a factory-pattern based manager for voice configuration including:
- STT (Speech-to-Text) engine configuration
- TTS (Text-to-Speech) engine configuration
- Per-interface voice routing
- Engine health checks
- Pipeline latency metrics

Usage:
    from shared.voice_config import get_voice_config_manager

    # Get the voice config manager
    manager = await get_voice_config_manager()

    # Get STT config for an interface
    stt_config = await manager.get_stt_config("web_jarvis")

    # Get TTS config for an interface
    tts_config = await manager.get_tts_config("home_assistant")

    # Check engine health
    health = await manager.check_engine_health()
"""
import time
import asyncio
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
import structlog

logger = structlog.get_logger()


class EngineType(Enum):
    """Voice engine types."""
    STT = "stt"
    TTS = "tts"


@dataclass
class EngineConfig:
    """Configuration for a voice engine (STT or TTS)."""
    engine_id: str
    engine_type: EngineType
    display_name: str
    host: str
    port: int
    wyoming_url: str
    enabled: bool = True
    priority: int = 100
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint_url(self) -> str:
        """Get the endpoint URL for this engine."""
        return f"tcp://{self.host}:{self.port}"


@dataclass
class InterfaceVoiceConfig:
    """Voice configuration for a specific interface."""
    interface_name: str
    display_name: str
    enabled: bool
    stt_engine: Optional[EngineConfig]
    tts_engine: Optional[EngineConfig]
    continued_conversation: bool = False
    wake_word_enabled: bool = False
    default_voice_id: Optional[str] = None
    timeout_seconds: int = 30
    metadata: Dict[str, Any] = field(default_factory=dict)


class VoiceConfigManager:
    """
    Manager for voice configuration with factory pattern.

    Provides centralized access to voice configuration including:
    - STT/TTS engine configurations
    - Per-interface voice routing
    - Engine health monitoring
    - Configuration caching
    """

    def __init__(self):
        """Initialize the voice config manager."""
        self._initialized = False
        self._stt_engines: Dict[str, EngineConfig] = {}
        self._tts_engines: Dict[str, EngineConfig] = {}
        self._interface_configs: Dict[str, InterfaceVoiceConfig] = {}
        self._default_stt_config: Optional[Dict[str, Any]] = None
        self._default_tts_config: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 60.0  # 60 second cache

    async def initialize(self) -> bool:
        """
        Initialize the voice config manager by loading configs from Admin API.

        Returns:
            True if initialization succeeded, False otherwise.
        """
        if self._initialized:
            return True

        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()

            # Load default STT/TTS configs
            voice_config = await client.get_voice_config_all()
            self._default_stt_config = voice_config.get("stt")
            self._default_tts_config = voice_config.get("tts")

            # Load available engines
            await self._load_engines()

            self._initialized = True
            self._cache_time = time.time()

            logger.info(
                "voice_config_manager_initialized",
                stt_engines=len(self._stt_engines),
                tts_engines=len(self._tts_engines),
                interfaces=len(self._interface_configs)
            )
            return True

        except Exception as e:
            logger.error(
                "voice_config_manager_init_failed",
                error=str(e)
            )
            return False

    async def _load_engines(self):
        """Load available STT and TTS engines from Admin API."""
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()

            # Fetch engines from public endpoints
            stt_url = f"{client.admin_url}/api/voice-interfaces/engines/public/stt"
            tts_url = f"{client.admin_url}/api/voice-interfaces/engines/public/tts"

            stt_response = await client.client.get(stt_url)
            tts_response = await client.client.get(tts_url)

            if stt_response.status_code == 200:
                for engine in stt_response.json():
                    self._stt_engines[engine["engine_id"]] = EngineConfig(
                        engine_id=engine["engine_id"],
                        engine_type=EngineType.STT,
                        display_name=engine.get("display_name", engine["engine_id"]),
                        host=engine.get("host", "localhost"),
                        port=engine.get("port", 10300),
                        wyoming_url=engine.get("wyoming_url", ""),
                        enabled=engine.get("enabled", True),
                        priority=engine.get("priority", 100),
                        metadata=engine.get("metadata", {})
                    )

            if tts_response.status_code == 200:
                for engine in tts_response.json():
                    self._tts_engines[engine["engine_id"]] = EngineConfig(
                        engine_id=engine["engine_id"],
                        engine_type=EngineType.TTS,
                        display_name=engine.get("display_name", engine["engine_id"]),
                        host=engine.get("host", "localhost"),
                        port=engine.get("port", 10200),
                        wyoming_url=engine.get("wyoming_url", ""),
                        enabled=engine.get("enabled", True),
                        priority=engine.get("priority", 100),
                        metadata=engine.get("metadata", {})
                    )

            logger.debug(
                "voice_engines_loaded",
                stt_count=len(self._stt_engines),
                tts_count=len(self._tts_engines)
            )

        except Exception as e:
            logger.warning(
                "voice_engines_load_failed",
                error=str(e)
            )

    async def refresh(self):
        """Force refresh of all voice configurations."""
        self._initialized = False
        self._stt_engines.clear()
        self._tts_engines.clear()
        self._interface_configs.clear()
        await self.initialize()

    def is_cache_valid(self) -> bool:
        """Check if the cached configuration is still valid."""
        if not self._initialized:
            return False
        return (time.time() - self._cache_time) < self._cache_ttl

    async def get_interface_config(self, interface_name: str) -> Optional[InterfaceVoiceConfig]:
        """
        Get voice configuration for a specific interface.

        Args:
            interface_name: Interface identifier (e.g., "web_jarvis", "home_assistant")

        Returns:
            InterfaceVoiceConfig or None if interface not found.
        """
        if not self._initialized:
            await self.initialize()

        # Check cache
        if interface_name in self._interface_configs:
            return self._interface_configs[interface_name]

        # Fetch from admin API
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            config = await client.get_voice_interface_config(interface_name)

            if not config:
                return None

            # Parse STT engine
            stt_engine = None
            if config.get("stt_engine"):
                stt_data = config["stt_engine"]
                stt_engine = EngineConfig(
                    engine_id=stt_data.get("engine_id", "default-stt"),
                    engine_type=EngineType.STT,
                    display_name=stt_data.get("display_name", "Default STT"),
                    host=stt_data.get("host", "localhost"),
                    port=stt_data.get("port", 10300),
                    wyoming_url=stt_data.get("wyoming_url", ""),
                    enabled=stt_data.get("enabled", True),
                    priority=stt_data.get("priority", 100)
                )

            # Parse TTS engine
            tts_engine = None
            if config.get("tts_engine"):
                tts_data = config["tts_engine"]
                tts_engine = EngineConfig(
                    engine_id=tts_data.get("engine_id", "default-tts"),
                    engine_type=EngineType.TTS,
                    display_name=tts_data.get("display_name", "Default TTS"),
                    host=tts_data.get("host", "localhost"),
                    port=tts_data.get("port", 10200),
                    wyoming_url=tts_data.get("wyoming_url", ""),
                    enabled=tts_data.get("enabled", True),
                    priority=tts_data.get("priority", 100)
                )

            interface_config = InterfaceVoiceConfig(
                interface_name=interface_name,
                display_name=config.get("display_name", interface_name),
                enabled=config.get("enabled", True),
                stt_engine=stt_engine,
                tts_engine=tts_engine,
                continued_conversation=config.get("continued_conversation", False),
                wake_word_enabled=config.get("wake_word_enabled", False),
                default_voice_id=config.get("default_voice_id"),
                timeout_seconds=config.get("timeout_seconds", 30)
            )

            # Cache it
            self._interface_configs[interface_name] = interface_config
            return interface_config

        except Exception as e:
            logger.warning(
                "get_interface_config_failed",
                interface_name=interface_name,
                error=str(e)
            )
            return None

    async def get_stt_config(self, interface_name: str) -> Optional[Dict[str, Any]]:
        """
        Get STT configuration for an interface.

        Args:
            interface_name: Interface identifier

        Returns:
            Dict with STT configuration including endpoint URL.
        """
        config = await self.get_interface_config(interface_name)

        if config and config.stt_engine:
            return {
                "engine_id": config.stt_engine.engine_id,
                "host": config.stt_engine.host,
                "port": config.stt_engine.port,
                "wyoming_url": config.stt_engine.wyoming_url or config.stt_engine.endpoint_url,
                "enabled": config.stt_engine.enabled,
                "timeout_seconds": config.timeout_seconds
            }

        # Fall back to default STT config
        if self._default_stt_config:
            return self._default_stt_config

        return None

    async def get_tts_config(self, interface_name: str) -> Optional[Dict[str, Any]]:
        """
        Get TTS configuration for an interface.

        Args:
            interface_name: Interface identifier

        Returns:
            Dict with TTS configuration including endpoint URL.
        """
        config = await self.get_interface_config(interface_name)

        if config and config.tts_engine:
            return {
                "engine_id": config.tts_engine.engine_id,
                "host": config.tts_engine.host,
                "port": config.tts_engine.port,
                "wyoming_url": config.tts_engine.wyoming_url or config.tts_engine.endpoint_url,
                "enabled": config.tts_engine.enabled,
                "voice_id": config.default_voice_id,  # Renamed for consistency with gateway
                "timeout_seconds": config.timeout_seconds
            }

        # Fall back to default TTS config
        if self._default_tts_config:
            return self._default_tts_config

        return None

    async def check_engine_health(self) -> Dict[str, Any]:
        """
        Check health of all voice engines.

        Returns:
            Dict with health status for each engine and overall health.
        """
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            return await client.check_voice_services_health()
        except Exception as e:
            logger.warning(
                "engine_health_check_failed",
                error=str(e)
            )
            return {
                "stt": {"healthy": False, "message": str(e)},
                "tts": {"healthy": False, "message": str(e)},
                "overall_healthy": False
            }

    def get_available_stt_engines(self) -> List[EngineConfig]:
        """Get list of available STT engines."""
        return [e for e in self._stt_engines.values() if e.enabled]

    def get_available_tts_engines(self) -> List[EngineConfig]:
        """Get list of available TTS engines."""
        return [e for e in self._tts_engines.values() if e.enabled]

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about loaded voice configuration."""
        return {
            "initialized": self._initialized,
            "stt_engines_count": len(self._stt_engines),
            "tts_engines_count": len(self._tts_engines),
            "cached_interfaces_count": len(self._interface_configs),
            "cache_age_seconds": time.time() - self._cache_time if self._cache_time else None,
            "cache_valid": self.is_cache_valid()
        }


# =============================================================================
# Factory Pattern - Manager Registry
# =============================================================================

class VoiceConfigFactory:
    """Factory for creating and managing VoiceConfigManager instances."""

    _instances: Dict[str, VoiceConfigManager] = {}

    @classmethod
    def get(cls, name: str = "default") -> VoiceConfigManager:
        """
        Get or create a VoiceConfigManager instance.

        Args:
            name: Instance name for isolation (default: "default")

        Returns:
            VoiceConfigManager instance
        """
        if name not in cls._instances:
            cls._instances[name] = VoiceConfigManager()
        return cls._instances[name]

    @classmethod
    def clear(cls, name: str = None):
        """
        Clear VoiceConfigManager instances.

        Args:
            name: Specific instance to clear, or None to clear all
        """
        if name:
            if name in cls._instances:
                del cls._instances[name]
        else:
            cls._instances.clear()

    @classmethod
    def list_instances(cls) -> List[str]:
        """List all active manager instances."""
        return list(cls._instances.keys())


# =============================================================================
# Convenience Functions
# =============================================================================

async def get_voice_config_manager(name: str = "default") -> VoiceConfigManager:
    """
    Get an initialized VoiceConfigManager instance.

    Args:
        name: Instance name for isolation (default: "default")

    Returns:
        Initialized VoiceConfigManager
    """
    manager = VoiceConfigFactory.get(name)
    if not manager._initialized:
        await manager.initialize()
    return manager


async def get_stt_config(interface_name: str) -> Optional[Dict[str, Any]]:
    """
    Convenience function to get STT config for an interface.

    Args:
        interface_name: Interface identifier

    Returns:
        STT configuration dict or None
    """
    manager = await get_voice_config_manager()
    return await manager.get_stt_config(interface_name)


async def get_tts_config(interface_name: str) -> Optional[Dict[str, Any]]:
    """
    Convenience function to get TTS config for an interface.

    Args:
        interface_name: Interface identifier

    Returns:
        TTS configuration dict or None
    """
    manager = await get_voice_config_manager()
    return await manager.get_tts_config(interface_name)


# =============================================================================
# Pipeline Latency Metrics
# =============================================================================

try:
    from prometheus_client import Histogram, Counter

    PIPELINE_LATENCY = Histogram(
        'athena_voice_pipeline_seconds',
        'Voice pipeline latency in seconds',
        ['stage', 'interface'],
        buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0]
    )

    PIPELINE_STAGE_COUNT = Counter(
        'athena_voice_pipeline_stage_total',
        'Total voice pipeline stage executions',
        ['stage', 'interface', 'success']
    )

    PROMETHEUS_AVAILABLE = True

except ImportError:
    # Fallback stubs when prometheus_client is not available
    class StubMetric:
        def labels(self, *args, **kwargs):
            return self
        def observe(self, *args, **kwargs):
            pass
        def inc(self, *args, **kwargs):
            pass

    PIPELINE_LATENCY = StubMetric()
    PIPELINE_STAGE_COUNT = StubMetric()
    PROMETHEUS_AVAILABLE = False


def record_pipeline_stage(
    stage: str,
    interface: str,
    latency_seconds: float,
    success: bool = True
):
    """
    Record a voice pipeline stage execution.

    Args:
        stage: Pipeline stage (e.g., "stt", "intent", "llm", "tts")
        interface: Interface name
        latency_seconds: Stage latency in seconds
        success: Whether the stage succeeded
    """
    PIPELINE_LATENCY.labels(stage=stage, interface=interface).observe(latency_seconds)
    PIPELINE_STAGE_COUNT.labels(
        stage=stage,
        interface=interface,
        success="true" if success else "false"
    ).inc()

    logger.debug(
        "pipeline_stage_recorded",
        stage=stage,
        interface=interface,
        latency_seconds=round(latency_seconds, 3),
        success=success
    )


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == "__main__":
    async def main():
        print("Testing VoiceConfigManager...")

        manager = await get_voice_config_manager()
        print(f"Manager initialized: {manager._initialized}")
        print(f"Stats: {manager.get_stats()}")

        # Test interface config
        for interface in ["web_jarvis", "home_assistant", "admin_jarvis"]:
            config = await manager.get_interface_config(interface)
            if config:
                print(f"\n{interface}:")
                print(f"  Enabled: {config.enabled}")
                print(f"  STT Engine: {config.stt_engine.engine_id if config.stt_engine else 'None'}")
                print(f"  TTS Engine: {config.tts_engine.engine_id if config.tts_engine else 'None'}")
            else:
                print(f"\n{interface}: Not found")

        # Test health check
        health = await manager.check_engine_health()
        print(f"\nHealth: {health}")

    asyncio.run(main())
