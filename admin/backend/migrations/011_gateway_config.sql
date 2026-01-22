-- Migration 011: Gateway Configuration Table
-- Stores gateway service configuration as a singleton table (id=1)

-- Gateway configuration table
CREATE TABLE IF NOT EXISTS gateway_config (
    id SERIAL PRIMARY KEY,

    -- Service URLs
    orchestrator_url VARCHAR(500) NOT NULL DEFAULT 'http://localhost:8001',
    ollama_fallback_url VARCHAR(500) NOT NULL DEFAULT 'http://localhost:11434',

    -- Intent Classification
    intent_model VARCHAR(255) NOT NULL DEFAULT 'phi3:mini',
    intent_temperature DECIMAL(3,2) NOT NULL DEFAULT 0.1,
    intent_max_tokens INTEGER NOT NULL DEFAULT 10,
    intent_timeout_seconds INTEGER NOT NULL DEFAULT 5,

    -- Timeouts
    orchestrator_timeout_seconds INTEGER NOT NULL DEFAULT 60,

    -- Session Management
    session_timeout_seconds INTEGER NOT NULL DEFAULT 300,
    session_max_age_seconds INTEGER NOT NULL DEFAULT 86400,
    session_cleanup_interval_seconds INTEGER NOT NULL DEFAULT 60,

    -- Cache
    cache_ttl_seconds INTEGER NOT NULL DEFAULT 60,

    -- Rate Limiting
    rate_limit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    rate_limit_requests_per_minute INTEGER NOT NULL DEFAULT 60,

    -- Circuit Breaker
    circuit_breaker_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    circuit_breaker_failure_threshold INTEGER NOT NULL DEFAULT 5,
    circuit_breaker_recovery_timeout_seconds INTEGER NOT NULL DEFAULT 30,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Insert default config row (singleton)
INSERT INTO gateway_config (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Trigger to update updated_at on row update
CREATE OR REPLACE FUNCTION update_gateway_config_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS gateway_config_updated ON gateway_config;
CREATE TRIGGER gateway_config_updated
    BEFORE UPDATE ON gateway_config
    FOR EACH ROW
    EXECUTE FUNCTION update_gateway_config_timestamp();
