"""
Unit tests for Voice Configuration Manager.

Tests the VoiceConfigManager, VoiceConfigFactory, and related utilities.
"""
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Import test subjects
import sys
sys.path.insert(0, 'src')

from shared.voice_config import (
    EngineType,
    EngineConfig,
    InterfaceVoiceConfig,
    VoiceConfigManager,
    VoiceConfigFactory,
    get_voice_config_manager,
    get_stt_config,
    get_tts_config,
    record_pipeline_stage,
    PROMETHEUS_AVAILABLE,
)


# =============================================================================
# Test EngineType Enum
# =============================================================================

class TestEngineType:
    """Tests for EngineType enum."""

    def test_stt_value(self):
        """STT has correct value."""
        assert EngineType.STT.value == "stt"

    def test_tts_value(self):
        """TTS has correct value."""
        assert EngineType.TTS.value == "tts"


# =============================================================================
# Test EngineConfig Dataclass
# =============================================================================

class TestEngineConfig:
    """Tests for EngineConfig dataclass."""

    def test_engine_config_defaults(self):
        """EngineConfig has correct defaults."""
        config = EngineConfig(
            engine_id="test-stt",
            engine_type=EngineType.STT,
            display_name="Test STT",
            host="localhost",
            port=10300,
            wyoming_url="tcp://localhost:10300"
        )
        assert config.enabled is True
        assert config.priority == 100
        assert config.metadata == {}

    def test_engine_config_custom_values(self):
        """EngineConfig accepts custom values."""
        config = EngineConfig(
            engine_id="custom-tts",
            engine_type=EngineType.TTS,
            display_name="Custom TTS",
            host="localhost",
            port=10200,
            wyoming_url="tcp://localhost:10200",
            enabled=False,
            priority=50,
            metadata={"voice": "lessac"}
        )
        assert config.engine_id == "custom-tts"
        assert config.engine_type == EngineType.TTS
        assert config.enabled is False
        assert config.priority == 50
        assert config.metadata == {"voice": "lessac"}

    def test_endpoint_url_property(self):
        """endpoint_url property returns correct format."""
        config = EngineConfig(
            engine_id="test",
            engine_type=EngineType.STT,
            display_name="Test",
            host="localhost",
            port=10300,
            wyoming_url=""
        )
        assert config.endpoint_url == "tcp://localhost:10300"


# =============================================================================
# Test InterfaceVoiceConfig Dataclass
# =============================================================================

class TestInterfaceVoiceConfig:
    """Tests for InterfaceVoiceConfig dataclass."""

    def test_interface_config_defaults(self):
        """InterfaceVoiceConfig has correct defaults."""
        config = InterfaceVoiceConfig(
            interface_name="test_interface",
            display_name="Test Interface",
            enabled=True,
            stt_engine=None,
            tts_engine=None
        )
        assert config.continued_conversation is False
        assert config.wake_word_enabled is False
        assert config.default_voice_id is None
        assert config.timeout_seconds == 30
        assert config.metadata == {}

    def test_interface_config_with_engines(self):
        """InterfaceVoiceConfig with STT/TTS engines."""
        stt = EngineConfig(
            engine_id="whisper-stt",
            engine_type=EngineType.STT,
            display_name="Whisper",
            host="localhost",
            port=10300,
            wyoming_url="tcp://localhost:10300"
        )
        tts = EngineConfig(
            engine_id="piper-tts",
            engine_type=EngineType.TTS,
            display_name="Piper",
            host="localhost",
            port=10200,
            wyoming_url="tcp://localhost:10200"
        )
        config = InterfaceVoiceConfig(
            interface_name="web_jarvis",
            display_name="Web Jarvis",
            enabled=True,
            stt_engine=stt,
            tts_engine=tts,
            continued_conversation=True,
            default_voice_id="en_US-lessac-medium"
        )
        assert config.stt_engine.engine_id == "whisper-stt"
        assert config.tts_engine.engine_id == "piper-tts"
        assert config.continued_conversation is True
        assert config.default_voice_id == "en_US-lessac-medium"


# =============================================================================
# Test VoiceConfigManager
# =============================================================================

class TestVoiceConfigManager:
    """Tests for VoiceConfigManager class."""

    def test_manager_initial_state(self):
        """Manager starts uninitialized."""
        manager = VoiceConfigManager()
        assert manager._initialized is False
        assert manager._stt_engines == {}
        assert manager._tts_engines == {}
        assert manager._interface_configs == {}

    def test_cache_validity_uninitialized(self):
        """Cache is invalid when not initialized."""
        manager = VoiceConfigManager()
        assert manager.is_cache_valid() is False

    def test_cache_validity_fresh(self):
        """Cache is valid when freshly initialized."""
        manager = VoiceConfigManager()
        manager._initialized = True
        manager._cache_time = time.time()
        assert manager.is_cache_valid() is True

    def test_cache_validity_expired(self):
        """Cache is invalid when expired."""
        manager = VoiceConfigManager()
        manager._initialized = True
        manager._cache_time = time.time() - 120  # 2 minutes ago
        manager._cache_ttl = 60  # 1 minute TTL
        assert manager.is_cache_valid() is False

    def test_get_stats(self):
        """get_stats returns correct structure."""
        manager = VoiceConfigManager()
        manager._initialized = True
        manager._cache_time = time.time()
        manager._stt_engines = {"engine1": MagicMock()}
        manager._tts_engines = {"engine1": MagicMock(), "engine2": MagicMock()}

        stats = manager.get_stats()

        assert stats["initialized"] is True
        assert stats["stt_engines_count"] == 1
        assert stats["tts_engines_count"] == 2
        assert stats["cached_interfaces_count"] == 0
        assert "cache_age_seconds" in stats
        assert stats["cache_valid"] is True


# =============================================================================
# Test VoiceConfigFactory
# =============================================================================

class TestVoiceConfigFactory:
    """Tests for VoiceConfigFactory class."""

    def setup_method(self):
        """Clear factory before each test."""
        VoiceConfigFactory.clear()

    def test_get_creates_instance(self):
        """get() creates new instance if none exists."""
        manager = VoiceConfigFactory.get("test")
        assert isinstance(manager, VoiceConfigManager)

    def test_get_returns_same_instance(self):
        """get() returns same instance for same name."""
        manager1 = VoiceConfigFactory.get("test")
        manager2 = VoiceConfigFactory.get("test")
        assert manager1 is manager2

    def test_get_different_names_different_instances(self):
        """get() returns different instances for different names."""
        manager1 = VoiceConfigFactory.get("instance1")
        manager2 = VoiceConfigFactory.get("instance2")
        assert manager1 is not manager2

    def test_clear_specific_instance(self):
        """clear() removes specific instance."""
        VoiceConfigFactory.get("keep")
        VoiceConfigFactory.get("remove")
        VoiceConfigFactory.clear("remove")
        assert "keep" in VoiceConfigFactory._instances
        assert "remove" not in VoiceConfigFactory._instances

    def test_clear_all_instances(self):
        """clear() with no args removes all instances."""
        VoiceConfigFactory.get("one")
        VoiceConfigFactory.get("two")
        VoiceConfigFactory.clear()
        assert VoiceConfigFactory._instances == {}

    def test_list_instances(self):
        """list_instances() returns all instance names."""
        VoiceConfigFactory.get("alpha")
        VoiceConfigFactory.get("beta")
        instances = VoiceConfigFactory.list_instances()
        assert sorted(instances) == ["alpha", "beta"]


# =============================================================================
# Test Pipeline Metrics
# =============================================================================

class TestPipelineMetrics:
    """Tests for pipeline latency metrics."""

    def test_record_pipeline_stage_no_error(self):
        """record_pipeline_stage doesn't raise errors."""
        # Should not raise even if prometheus not available
        record_pipeline_stage(
            stage="stt",
            interface="web_jarvis",
            latency_seconds=0.5,
            success=True
        )

    def test_record_pipeline_stage_failure(self):
        """record_pipeline_stage handles failure case."""
        record_pipeline_stage(
            stage="tts",
            interface="home_assistant",
            latency_seconds=1.2,
            success=False
        )

    def test_prometheus_available_is_bool(self):
        """PROMETHEUS_AVAILABLE is a boolean."""
        assert isinstance(PROMETHEUS_AVAILABLE, bool)


# =============================================================================
# Integration Tests with Mocks
# =============================================================================

class TestVoiceConfigManagerWithMocks:
    """Integration tests using mocked admin client."""

    @pytest.fixture
    def mock_admin_client(self):
        """Create mock admin client."""
        client = MagicMock()
        client.admin_url = "http://localhost:8080"
        client.client = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_initialize_success(self, mock_admin_client):
        """Manager initializes successfully with valid API response."""
        # Setup mocks
        mock_admin_client.get_voice_config_all = AsyncMock(return_value={
            "stt": {"model_id": "whisper-small", "wyoming_url": "tcp://localhost:10300"},
            "tts": {"voice_id": "lessac", "wyoming_url": "tcp://localhost:10200"}
        })

        mock_stt_response = MagicMock()
        mock_stt_response.status_code = 200
        mock_stt_response.json.return_value = [
            {"engine_id": "default-stt", "display_name": "Default", "host": "localhost", "port": 10300}
        ]

        mock_tts_response = MagicMock()
        mock_tts_response.status_code = 200
        mock_tts_response.json.return_value = [
            {"engine_id": "default-tts", "display_name": "Default", "host": "localhost", "port": 10200}
        ]

        mock_admin_client.client.get = AsyncMock(side_effect=[mock_stt_response, mock_tts_response])

        with patch('shared.admin_config.get_admin_client', return_value=mock_admin_client):
            manager = VoiceConfigManager()
            result = await manager.initialize()

            assert result is True
            assert manager._initialized is True
            assert manager._default_stt_config is not None
            assert manager._default_tts_config is not None

    @pytest.mark.asyncio
    async def test_get_stt_config_with_interface(self, mock_admin_client):
        """get_stt_config returns correct config for interface."""
        mock_admin_client.get_voice_interface_config = AsyncMock(return_value={
            "interface_name": "web_jarvis",
            "enabled": True,
            "stt_engine": {
                "engine_id": "whisper-stt",
                "host": "localhost",
                "port": 10300,
                "wyoming_url": "tcp://localhost:10300"
            },
            "tts_engine": None,
            "timeout_seconds": 30
        })

        with patch('shared.admin_config.get_admin_client', return_value=mock_admin_client):
            manager = VoiceConfigManager()
            manager._initialized = True

            stt_config = await manager.get_stt_config("web_jarvis")

            assert stt_config is not None
            assert stt_config["engine_id"] == "whisper-stt"
            assert stt_config["host"] == "localhost"
            assert stt_config["port"] == 10300

    @pytest.mark.asyncio
    async def test_check_engine_health(self, mock_admin_client):
        """check_engine_health returns health status."""
        mock_admin_client.check_voice_services_health = AsyncMock(return_value={
            "stt": {"healthy": True, "message": "OK"},
            "tts": {"healthy": True, "message": "OK"},
            "overall_healthy": True
        })

        with patch('shared.admin_config.get_admin_client', return_value=mock_admin_client):
            manager = VoiceConfigManager()
            health = await manager.check_engine_health()

            assert health["overall_healthy"] is True
            assert health["stt"]["healthy"] is True
            assert health["tts"]["healthy"] is True


# =============================================================================
# Test Convenience Functions
# =============================================================================

class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def setup_method(self):
        """Clear factory before each test."""
        VoiceConfigFactory.clear()

    @pytest.mark.asyncio
    async def test_get_voice_config_manager_initializes(self):
        """get_voice_config_manager returns initialized manager."""
        with patch('shared.voice_config.VoiceConfigManager.initialize', new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True

            manager = await get_voice_config_manager()

            assert isinstance(manager, VoiceConfigManager)
            mock_init.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_stt_config_uses_manager(self):
        """get_stt_config uses default manager."""
        mock_manager = MagicMock()
        mock_manager.get_stt_config = AsyncMock(return_value={"engine_id": "test"})
        mock_manager._initialized = True

        with patch('shared.voice_config.get_voice_config_manager', new_callable=AsyncMock, return_value=mock_manager):
            config = await get_stt_config("test_interface")

            assert config == {"engine_id": "test"}
            mock_manager.get_stt_config.assert_called_once_with("test_interface")

    @pytest.mark.asyncio
    async def test_get_tts_config_uses_manager(self):
        """get_tts_config uses default manager."""
        mock_manager = MagicMock()
        mock_manager.get_tts_config = AsyncMock(return_value={"voice_id": "lessac"})
        mock_manager._initialized = True

        with patch('shared.voice_config.get_voice_config_manager', new_callable=AsyncMock, return_value=mock_manager):
            config = await get_tts_config("test_interface")

            assert config == {"voice_id": "lessac"}
            mock_manager.get_tts_config.assert_called_once_with("test_interface")
