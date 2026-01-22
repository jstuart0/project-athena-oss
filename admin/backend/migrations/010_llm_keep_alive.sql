-- Migration 010: Add keep_alive_seconds to llm_backends table
-- This enables control over how long models stay loaded in memory
-- -1 = forever (never unload), 0 = unload immediately, >0 = seconds before unload

-- Add keep_alive_seconds column with default of -1 (keep forever)
ALTER TABLE llm_backends
ADD COLUMN IF NOT EXISTS keep_alive_seconds INTEGER DEFAULT -1;

-- Add comment for documentation
COMMENT ON COLUMN llm_backends.keep_alive_seconds IS 'How long to keep model loaded in memory. -1 = forever, 0 = unload immediately, >0 = seconds';

-- Create system_settings table if it doesn't exist
CREATE TABLE IF NOT EXISTS system_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(255) NOT NULL UNIQUE,
    value TEXT NOT NULL,
    description TEXT,
    category VARCHAR(100) DEFAULT 'general',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create index on category
CREATE INDEX IF NOT EXISTS idx_system_settings_category ON system_settings(category);

-- Add global LLM memory management settings
INSERT INTO system_settings (key, value, description, category, updated_at)
VALUES (
    'llm_keep_models_loaded',
    'true',
    'When enabled, keeps LLM models loaded in memory to avoid cold start delays. When disabled, models unload after 5 minutes of inactivity.',
    'performance',
    NOW()
) ON CONFLICT (key) DO NOTHING;

INSERT INTO system_settings (key, value, description, category, updated_at)
VALUES (
    'llm_default_keep_alive_seconds',
    '-1',
    'Default keep_alive duration for models. -1 = forever, 0 = unload immediately, >0 = seconds. Individual models can override this.',
    'performance',
    NOW()
) ON CONFLICT (key) DO NOTHING;
