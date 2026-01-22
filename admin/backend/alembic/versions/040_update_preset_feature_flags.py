"""Update preset feature flags to include all features

Revision ID: 040
Revises: 039
Create Date: 2026-01-04
"""
from alembic import op
import sqlalchemy as sa
import json


# revision identifiers, used by Alembic.
revision = '040_preset_feature_flags'
down_revision = '039_performance_presets'
branch_labels = None
depends_on = None


def upgrade():
    """Update system presets to include all feature flags with appropriate values."""

    conn = op.get_bind()

    # Define complete feature flag sets for each preset
    # Maximum Speed: Enable speed optimizations, disable heavy features
    max_speed_flags = {
        # HA optimizations - all enabled for speed
        "ha_room_detection_cache": True,
        "ha_simple_command_fastpath": True,
        "ha_parallel_init": True,
        "ha_precomputed_summaries": True,
        "ha_session_warmup": True,
        "ha_intent_prerouting": True,
        # Core features - minimal set
        "home_assistant": True,
        "intent_classification": True,
        "enable_llm_intent_classification": False,  # Faster without LLM classification
        "llm_based_routing": False,  # Use simpler routing
        "conversation_context": False,  # No context for speed
        "clarifications": False,  # No clarifications for speed
        "multi_intent_detection": False,  # Single intent only
        "self_building_tools": False,  # Disabled for speed
        "response_streaming": True,  # Faster perceived response
        "redis_caching": True,  # Caching for speed
        # RAG services - minimal
        "rag_weather": True,
        "rag_airports": False,
        "rag_sports": False,
        "rag_directions": False,
        # Other features
        "mlx_backend": False,
        "music_playback": False,
        "livekit_webrtc": False,
    }

    # Balanced: Good mix of features and performance
    balanced_flags = {
        # HA optimizations - most enabled
        "ha_room_detection_cache": True,
        "ha_simple_command_fastpath": True,
        "ha_parallel_init": True,
        "ha_precomputed_summaries": True,
        "ha_session_warmup": False,
        "ha_intent_prerouting": False,
        # Core features
        "home_assistant": True,
        "intent_classification": True,
        "enable_llm_intent_classification": True,
        "llm_based_routing": True,
        "conversation_context": True,
        "clarifications": True,
        "multi_intent_detection": True,
        "self_building_tools": False,
        "response_streaming": True,
        "redis_caching": True,
        # RAG services - common ones
        "rag_weather": True,
        "rag_airports": True,
        "rag_sports": True,
        "rag_directions": True,
        # Other features
        "mlx_backend": False,
        "music_playback": False,
        "livekit_webrtc": True,
    }

    # Maximum Accuracy: All features enabled for best quality
    max_accuracy_flags = {
        # HA optimizations - only safe ones
        "ha_room_detection_cache": True,
        "ha_simple_command_fastpath": False,  # Full processing
        "ha_parallel_init": True,
        "ha_precomputed_summaries": False,  # Fresh summaries
        "ha_session_warmup": False,
        "ha_intent_prerouting": False,
        # Core features - all enabled
        "home_assistant": True,
        "intent_classification": True,
        "enable_llm_intent_classification": True,
        "llm_based_routing": True,
        "conversation_context": True,
        "clarifications": True,
        "multi_intent_detection": True,
        "self_building_tools": True,
        "response_streaming": True,
        "redis_caching": True,
        # RAG services - all enabled
        "rag_weather": True,
        "rag_airports": True,
        "rag_sports": True,
        "rag_directions": True,
        # Other features
        "mlx_backend": False,
        "music_playback": True,
        "livekit_webrtc": True,
    }

    # Pre-existing: Match current production settings
    preexisting_flags = {
        # HA optimizations - current state (mostly disabled)
        "ha_room_detection_cache": False,
        "ha_simple_command_fastpath": False,
        "ha_parallel_init": False,
        "ha_precomputed_summaries": False,
        "ha_session_warmup": False,
        "ha_intent_prerouting": False,
        # Core features - current state
        "home_assistant": True,
        "intent_classification": True,
        "enable_llm_intent_classification": True,
        "llm_based_routing": True,
        "conversation_context": True,
        "clarifications": True,
        "multi_intent_detection": True,
        "self_building_tools": True,
        "response_streaming": True,
        "redis_caching": True,
        # RAG services - current state
        "rag_weather": True,
        "rag_airports": True,
        "rag_sports": True,
        "rag_directions": True,
        # Other features - current state
        "mlx_backend": False,
        "music_playback": False,
        "livekit_webrtc": True,
    }

    # Update each preset
    presets = [
        ("Maximum Speed", max_speed_flags),
        ("Balanced", balanced_flags),
        ("Maximum Accuracy", max_accuracy_flags),
        ("Pre-existing Configuration", preexisting_flags),
    ]

    for preset_name, flags in presets:
        # Get current settings
        result = conn.execute(
            sa.text("SELECT id, settings FROM performance_presets WHERE name = :name"),
            {"name": preset_name}
        ).fetchone()

        if result:
            preset_id, settings = result
            if isinstance(settings, str):
                settings = json.loads(settings)

            # Update feature_flags in settings
            settings["feature_flags"] = flags

            # Update the preset
            conn.execute(
                sa.text("UPDATE performance_presets SET settings = :settings WHERE id = :id"),
                {"settings": json.dumps(settings), "id": preset_id}
            )


def downgrade():
    """Revert to original 6-flag presets."""
    conn = op.get_bind()

    original_flags = {
        "Maximum Speed": {
            "ha_room_detection_cache": True,
            "ha_simple_command_fastpath": True,
            "ha_parallel_init": True,
            "ha_precomputed_summaries": True,
            "ha_session_warmup": True,
            "ha_intent_prerouting": True,
        },
        "Balanced": {
            "ha_room_detection_cache": True,
            "ha_simple_command_fastpath": True,
            "ha_parallel_init": True,
            "ha_precomputed_summaries": True,
            "ha_session_warmup": False,
            "ha_intent_prerouting": False,
        },
        "Maximum Accuracy": {
            "ha_room_detection_cache": True,
            "ha_simple_command_fastpath": False,
            "ha_parallel_init": True,
            "ha_precomputed_summaries": False,
            "ha_session_warmup": False,
            "ha_intent_prerouting": False,
        },
        "Pre-existing Configuration": {
            "ha_room_detection_cache": True,
            "ha_simple_command_fastpath": True,
            "ha_parallel_init": True,
            "ha_precomputed_summaries": True,
            "ha_session_warmup": True,
            "ha_intent_prerouting": True,
        },
    }

    for preset_name, flags in original_flags.items():
        result = conn.execute(
            sa.text("SELECT id, settings FROM performance_presets WHERE name = :name"),
            {"name": preset_name}
        ).fetchone()

        if result:
            preset_id, settings = result
            if isinstance(settings, str):
                settings = json.loads(settings)

            settings["feature_flags"] = flags

            conn.execute(
                sa.text("UPDATE performance_presets SET settings = :settings WHERE id = :id"),
                {"settings": json.dumps(settings), "id": preset_id}
            )
