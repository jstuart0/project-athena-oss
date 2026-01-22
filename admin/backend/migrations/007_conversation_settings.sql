-- Migration 007: Conversation Context & Clarification Settings
-- Description: Adds tables for conversation context management and clarifying questions
-- Date: 2025-11-15

-- ============================================================================
-- CONVERSATION SETTINGS
-- ============================================================================

-- Core conversation settings table
CREATE TABLE IF NOT EXISTS conversation_settings (
    id SERIAL PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT true,
    use_context BOOLEAN NOT NULL DEFAULT true,
    max_messages INTEGER NOT NULL DEFAULT 20,
    timeout_seconds INTEGER NOT NULL DEFAULT 1800,  -- 30 minutes
    cleanup_interval_seconds INTEGER NOT NULL DEFAULT 60,
    session_ttl_seconds INTEGER NOT NULL DEFAULT 3600,  -- 1 hour
    max_llm_history_messages INTEGER NOT NULL DEFAULT 10,  -- Show 5 exchanges to LLM
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Ensure only one row exists
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_settings_singleton ON conversation_settings ((id IS NOT NULL));

-- ============================================================================
-- CLARIFICATION SETTINGS
-- ============================================================================

-- Global clarification settings
CREATE TABLE IF NOT EXISTS clarification_settings (
    id SERIAL PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT true,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,  -- 5 minutes
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Ensure only one row exists
CREATE UNIQUE INDEX IF NOT EXISTS idx_clarification_settings_singleton ON clarification_settings ((id IS NOT NULL));

-- ============================================================================
-- CLARIFICATION TYPES
-- ============================================================================

-- Individual clarification type configurations
CREATE TABLE IF NOT EXISTS clarification_types (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL UNIQUE,  -- 'device', 'location', 'time', 'sports_team'
    enabled BOOLEAN NOT NULL DEFAULT true,
    timeout_seconds INTEGER,  -- Override global timeout if set
    priority INTEGER NOT NULL DEFAULT 0,  -- Higher priority types checked first
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_clarification_types_enabled ON clarification_types(enabled);
CREATE INDEX IF NOT EXISTS idx_clarification_types_priority ON clarification_types(priority DESC);

-- ============================================================================
-- SPORTS TEAM DISAMBIGUATION
-- ============================================================================

-- Sports team disambiguation rules
CREATE TABLE IF NOT EXISTS sports_team_disambiguation (
    id SERIAL PRIMARY KEY,
    team_name VARCHAR(100) NOT NULL,
    requires_disambiguation BOOLEAN NOT NULL DEFAULT true,
    options JSONB NOT NULL,  -- [{"id": "ny-giants", "label": "NY Giants (NFL)", "sport": "football"}]
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sports_team_name ON sports_team_disambiguation(team_name);
CREATE INDEX IF NOT EXISTS idx_sports_disambiguation_required ON sports_team_disambiguation(requires_disambiguation);

-- ============================================================================
-- DEVICE DISAMBIGUATION RULES
-- ============================================================================

-- Device disambiguation rules (synced from Home Assistant)
CREATE TABLE IF NOT EXISTS device_disambiguation_rules (
    id SERIAL PRIMARY KEY,
    device_type VARCHAR(50) NOT NULL,  -- 'lights', 'switches', 'thermostats'
    requires_disambiguation BOOLEAN NOT NULL DEFAULT true,
    min_entities_for_clarification INTEGER NOT NULL DEFAULT 2,
    include_all_option BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_device_type ON device_disambiguation_rules(device_type);

-- ============================================================================
-- CONVERSATION ANALYTICS
-- ============================================================================

-- Analytics event tracking
CREATE TABLE IF NOT EXISTS conversation_analytics (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(50) NOT NULL,  -- 'session_created', 'followup_detected', 'clarification_triggered'
    metadata JSONB,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analytics_event_type ON conversation_analytics(event_type);
CREATE INDEX IF NOT EXISTS idx_analytics_timestamp ON conversation_analytics(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_session_id ON conversation_analytics(session_id);

-- ============================================================================
-- DEFAULT DATA
-- ============================================================================

-- Insert default conversation settings
INSERT INTO conversation_settings (
    enabled,
    use_context,
    max_messages,
    timeout_seconds,
    cleanup_interval_seconds,
    session_ttl_seconds,
    max_llm_history_messages
) VALUES (
    true,   -- enabled
    true,   -- use_context
    20,     -- max_messages
    1800,   -- timeout_seconds (30 minutes)
    60,     -- cleanup_interval_seconds
    3600,   -- session_ttl_seconds (1 hour)
    10      -- max_llm_history_messages (5 exchanges)
) ON CONFLICT DO NOTHING;

-- Insert default clarification settings
INSERT INTO clarification_settings (
    enabled,
    timeout_seconds
) VALUES (
    true,   -- enabled
    300     -- timeout_seconds (5 minutes)
) ON CONFLICT DO NOTHING;

-- Insert default clarification types
INSERT INTO clarification_types (type, enabled, priority, description) VALUES
    ('sports_team', true, 10, 'Disambiguate ambiguous sports team names (Giants, Cardinals, Panthers, Spurs)'),
    ('device', true, 20, 'Disambiguate Home Assistant devices when multiple entities match'),
    ('location', true, 30, 'Ask for location when not specified or in context'),
    ('time', true, 40, 'Ask for duration/time when not specified')
ON CONFLICT (type) DO NOTHING;

-- Insert default sports team disambiguation rules
INSERT INTO sports_team_disambiguation (team_name, requires_disambiguation, options) VALUES
    ('Giants', true, '[
        {"id": "ny-giants", "label": "NY Giants (NFL)", "sport": "football"},
        {"id": "sf-giants", "label": "SF Giants (MLB)", "sport": "baseball"}
    ]'::jsonb),
    ('Cardinals', true, '[
        {"id": "az-cardinals", "label": "Arizona Cardinals (NFL)", "sport": "football"},
        {"id": "stl-cardinals", "label": "St. Louis Cardinals (MLB)", "sport": "baseball"}
    ]'::jsonb),
    ('Panthers', true, '[
        {"id": "carolina-panthers", "label": "Carolina Panthers (NFL)", "sport": "football"},
        {"id": "florida-panthers", "label": "Florida Panthers (NHL)", "sport": "hockey"}
    ]'::jsonb),
    ('Spurs', true, '[
        {"id": "sa-spurs", "label": "San Antonio Spurs (NBA)", "sport": "basketball"},
        {"id": "tottenham-spurs", "label": "Tottenham Spurs (EPL)", "sport": "soccer"}
    ]'::jsonb)
ON CONFLICT DO NOTHING;

-- Insert default device disambiguation rules
INSERT INTO device_disambiguation_rules (
    device_type,
    requires_disambiguation,
    min_entities_for_clarification,
    include_all_option
) VALUES
    ('lights', true, 2, true),
    ('switches', true, 2, true),
    ('thermostats', true, 2, false),  -- Don't offer "all thermostats"
    ('fans', true, 2, true),
    ('covers', true, 2, true)
ON CONFLICT (device_type) DO NOTHING;

-- ============================================================================
-- UPDATE TRIGGERS
-- ============================================================================

-- Trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to all settings tables
CREATE TRIGGER update_conversation_settings_updated_at
    BEFORE UPDATE ON conversation_settings
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_clarification_settings_updated_at
    BEFORE UPDATE ON clarification_settings
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_clarification_types_updated_at
    BEFORE UPDATE ON clarification_types
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_sports_team_disambiguation_updated_at
    BEFORE UPDATE ON sports_team_disambiguation
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_device_disambiguation_rules_updated_at
    BEFORE UPDATE ON device_disambiguation_rules
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- Verify tables created
DO $$
BEGIN
    RAISE NOTICE 'Migration 007 completed successfully';
    RAISE NOTICE 'Created tables:';
    RAISE NOTICE '  - conversation_settings';
    RAISE NOTICE '  - clarification_settings';
    RAISE NOTICE '  - clarification_types';
    RAISE NOTICE '  - sports_team_disambiguation';
    RAISE NOTICE '  - device_disambiguation_rules';
    RAISE NOTICE '  - conversation_analytics';
END $$;
