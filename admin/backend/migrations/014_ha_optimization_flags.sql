-- Migration 014: Add HA voice optimization feature flags
-- Description: Feature flags for incremental HA voice latency optimizations
-- Date: 2026-01-03

-- Insert new feature flags (all default to false for safe rollout)
INSERT INTO features (name, display_name, description, category, enabled, required, priority, created_at, updated_at)
VALUES
    ('ha_room_detection_cache', 'Room Detection Cache', 'Cache active satellite room detection for 3 seconds to eliminate repeated HA API calls', 'performance', false, false, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('ha_simple_command_fastpath', 'Simple Command Fast-Path', 'Bypass orchestrator for simple home control commands (on/off/set) - direct to HA API', 'performance', false, false, 20, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('ha_parallel_init', 'Parallel Initialization', 'Run room detection and session lookup concurrently using asyncio.gather', 'performance', false, false, 30, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('ha_precomputed_summaries', 'Precomputed Summaries', 'Store conversation summaries in Redis and update incrementally instead of recomputing', 'performance', false, false, 40, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('ha_session_warmup', 'Session Warmup', 'Pre-fetch session data on wake word detection before STT completes', 'performance', false, false, 50, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('ha_intent_prerouting', 'Intent Pre-routing', 'Lightweight intent classification at Gateway to route simple queries directly to LLM', 'performance', false, false, 60, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (name) DO NOTHING;
