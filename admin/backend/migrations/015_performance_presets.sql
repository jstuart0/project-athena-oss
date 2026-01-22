-- Performance Presets table
-- Stores named configurations that bundle all performance-related settings
-- Migration: 015_performance_presets.sql

CREATE TABLE IF NOT EXISTS performance_presets (
    id SERIAL PRIMARY KEY,

    -- Identification
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,

    -- Ownership
    is_system BOOLEAN NOT NULL DEFAULT FALSE,  -- Built-in presets (read-only)
    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,

    -- Active state (only one preset can be active)
    is_active BOOLEAN NOT NULL DEFAULT FALSE,

    -- Settings snapshot (JSONB for flexibility)
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Metadata
    estimated_latency_ms INTEGER,  -- Calculated/estimated total latency
    icon VARCHAR(10),  -- Emoji icon for UI display

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for quick active preset lookup
CREATE INDEX IF NOT EXISTS idx_performance_presets_is_active ON performance_presets(is_active) WHERE is_active = TRUE;

-- Index for user's presets
CREATE INDEX IF NOT EXISTS idx_performance_presets_created_by ON performance_presets(created_by_id);

-- Trigger to update updated_at
CREATE OR REPLACE FUNCTION update_performance_presets_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS performance_presets_updated ON performance_presets;
CREATE TRIGGER performance_presets_updated
    BEFORE UPDATE ON performance_presets
    FOR EACH ROW
    EXECUTE FUNCTION update_performance_presets_timestamp();

-- Seed system presets
INSERT INTO performance_presets (name, description, is_system, is_active, icon, estimated_latency_ms, settings)
VALUES
(
    'Super Fast',
    'Optimized for minimal latency. Uses smallest models across all components, no conversation history, all optimizations enabled. Best for simple commands.',
    TRUE,
    FALSE,
    '⚡',
    1500,
    '{
        "gateway_intent_model": "phi3:mini",
        "gateway_intent_temperature": 0.1,
        "gateway_intent_max_tokens": 10,
        "intent_classifier_model": "qwen2.5:1.5b",
        "tool_calling_simple_model": "phi3:mini",
        "tool_calling_complex_model": "phi3:mini",
        "tool_calling_super_complex_model": "qwen2.5:7b",
        "response_synthesis_model": "phi3:mini",
        "llm_temperature": 0.3,
        "llm_max_tokens": 256,
        "llm_keep_alive_seconds": -1,
        "history_mode": "none",
        "max_llm_history_messages": 0,
        "feature_flags": {
            "ha_room_detection_cache": true,
            "ha_simple_command_fastpath": true,
            "ha_parallel_init": true,
            "ha_precomputed_summaries": true,
            "ha_session_warmup": true,
            "ha_intent_prerouting": true
        }
    }'::jsonb
),
(
    'Balanced',
    'Good balance of speed and context. Uses 7B models for tool calling, summarized history. Recommended for most use cases.',
    TRUE,
    TRUE,  -- Default active preset
    '⚖️',
    2500,
    '{
        "gateway_intent_model": "phi3:mini",
        "gateway_intent_temperature": 0.1,
        "gateway_intent_max_tokens": 10,
        "intent_classifier_model": "qwen2.5:1.5b",
        "tool_calling_simple_model": "qwen2.5:7b",
        "tool_calling_complex_model": "qwen2.5:7b",
        "tool_calling_super_complex_model": "qwen2.5:14b-instruct-q4_K_M",
        "response_synthesis_model": "qwen2.5:7b",
        "llm_temperature": 0.5,
        "llm_max_tokens": 512,
        "llm_keep_alive_seconds": -1,
        "history_mode": "summarized",
        "max_llm_history_messages": 5,
        "feature_flags": {
            "ha_room_detection_cache": true,
            "ha_simple_command_fastpath": true,
            "ha_parallel_init": true,
            "ha_precomputed_summaries": true,
            "ha_session_warmup": true,
            "ha_intent_prerouting": true
        }
    }'::jsonb
),
(
    'Conversational',
    'Maintains full conversation context for natural multi-turn dialogues. Uses larger models for better reasoning.',
    TRUE,
    FALSE,
    '💬',
    4000,
    '{
        "gateway_intent_model": "phi3:mini",
        "gateway_intent_temperature": 0.1,
        "gateway_intent_max_tokens": 10,
        "intent_classifier_model": "qwen2.5:1.5b",
        "tool_calling_simple_model": "qwen2.5:7b",
        "tool_calling_complex_model": "qwen2.5:14b-instruct-q4_K_M",
        "tool_calling_super_complex_model": "qwen2.5:14b-instruct-q4_K_M",
        "response_synthesis_model": "qwen2.5:7b",
        "llm_temperature": 0.7,
        "llm_max_tokens": 1024,
        "llm_keep_alive_seconds": -1,
        "history_mode": "full",
        "max_llm_history_messages": 10,
        "feature_flags": {
            "ha_room_detection_cache": true,
            "ha_simple_command_fastpath": false,
            "ha_parallel_init": true,
            "ha_precomputed_summaries": false,
            "ha_session_warmup": true,
            "ha_intent_prerouting": false
        }
    }'::jsonb
),
(
    'Maximum Accuracy',
    'Prioritizes response quality over speed. Uses largest models for all components. Best for complex questions requiring detailed answers.',
    TRUE,
    FALSE,
    '🎯',
    6000,
    '{
        "gateway_intent_model": "qwen2.5:7b",
        "gateway_intent_temperature": 0.3,
        "gateway_intent_max_tokens": 20,
        "intent_classifier_model": "qwen2.5:7b",
        "tool_calling_simple_model": "qwen2.5:14b-instruct-q4_K_M",
        "tool_calling_complex_model": "qwen2.5:14b-instruct-q4_K_M",
        "tool_calling_super_complex_model": "qwen2.5:14b-instruct-q4_K_M",
        "response_synthesis_model": "qwen2.5:14b-instruct-q4_K_M",
        "llm_temperature": 0.7,
        "llm_max_tokens": 2048,
        "llm_keep_alive_seconds": -1,
        "history_mode": "full",
        "max_llm_history_messages": 15,
        "feature_flags": {
            "ha_room_detection_cache": false,
            "ha_simple_command_fastpath": false,
            "ha_parallel_init": false,
            "ha_precomputed_summaries": false,
            "ha_session_warmup": false,
            "ha_intent_prerouting": false
        }
    }'::jsonb
)
ON CONFLICT (name) DO NOTHING;
