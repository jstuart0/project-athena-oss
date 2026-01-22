-- Cloud LLM Support Migration
-- Phase 1: Database Schema for Cloud Provider Integration
--
-- This migration adds support for tracking cloud LLM usage (OpenAI, Anthropic, Google)
-- including cost tracking, usage analytics, and provider configuration.
--
-- Open Source Compatible: Uses standard PostgreSQL types.

-- Cloud LLM Usage Tracking Table
-- Records every cloud LLM API call with full metadata for cost and usage analytics
CREATE TABLE IF NOT EXISTS cloud_llm_usage (
    id SERIAL PRIMARY KEY,

    -- Provider and model identification
    provider VARCHAR(32) NOT NULL,              -- 'openai', 'anthropic', 'google'
    model VARCHAR(100) NOT NULL,                -- e.g., 'gpt-4o', 'claude-sonnet'

    -- Token usage (from provider metadata, not estimates)
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,

    -- Cost tracking (in USD, calculated from provider pricing)
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0.0,

    -- Performance metrics
    latency_ms INTEGER,                         -- Total request duration
    ttft_ms INTEGER,                            -- Time to first token (streaming)
    streaming BOOLEAN DEFAULT false,            -- Whether streaming was used

    -- Request context
    request_id VARCHAR(64),                     -- Unique request identifier
    session_id VARCHAR(64),                     -- Conversation session
    user_id VARCHAR(64),                        -- User making the request
    zone VARCHAR(50),                           -- Zone/room (e.g., 'office', 'kitchen')
    intent VARCHAR(100),                        -- Classified intent (e.g., 'weather', 'general')

    -- Routing metadata
    was_fallback BOOLEAN DEFAULT false,         -- True if this was a fallback from local
    fallback_reason VARCHAR(255),               -- Why fallback occurred (if applicable)

    -- Timestamp
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Indexes for efficient querying
CREATE INDEX idx_cloud_llm_usage_provider ON cloud_llm_usage(provider);
CREATE INDEX idx_cloud_llm_usage_model ON cloud_llm_usage(model);
CREATE INDEX idx_cloud_llm_usage_timestamp ON cloud_llm_usage(timestamp);
CREATE INDEX idx_cloud_llm_usage_user ON cloud_llm_usage(user_id);
CREATE INDEX idx_cloud_llm_usage_session ON cloud_llm_usage(session_id);
CREATE INDEX idx_cloud_llm_usage_request ON cloud_llm_usage(request_id);

-- Composite index for date-range queries by provider
CREATE INDEX idx_cloud_llm_usage_provider_time ON cloud_llm_usage(provider, timestamp);

-- Cloud Provider Configuration (extends external_api_keys)
-- Note: This table stores provider-level configuration, not API keys
-- API keys are stored in external_api_keys table with encryption
CREATE TABLE IF NOT EXISTS cloud_llm_providers (
    id SERIAL PRIMARY KEY,

    -- Provider identification
    provider VARCHAR(32) UNIQUE NOT NULL,       -- 'openai', 'anthropic', 'google'
    display_name VARCHAR(100) NOT NULL,

    -- Status
    enabled BOOLEAN DEFAULT false NOT NULL,

    -- Default configuration
    default_model VARCHAR(100),                 -- Default model for this provider
    max_tokens_default INTEGER DEFAULT 2048,
    temperature_default DECIMAL(3, 2) DEFAULT 0.7,

    -- Rate limiting (requests per minute)
    rate_limit_rpm INTEGER DEFAULT 60,

    -- Cost configuration (per 1M tokens in USD)
    input_cost_per_1m DECIMAL(10, 4),
    output_cost_per_1m DECIMAL(10, 4),

    -- Health tracking
    last_health_check TIMESTAMP WITH TIME ZONE,
    health_status VARCHAR(32) DEFAULT 'unknown', -- 'healthy', 'degraded', 'unavailable'
    consecutive_failures INTEGER DEFAULT 0,

    -- Metadata
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Insert default provider configurations (disabled by default for security)
INSERT INTO cloud_llm_providers (provider, display_name, default_model, input_cost_per_1m, output_cost_per_1m, description)
VALUES
    ('openai', 'OpenAI', 'gpt-4o-mini', 0.15, 0.60, 'OpenAI API (GPT-4o, GPT-4o-mini)'),
    ('anthropic', 'Anthropic', 'claude-sonnet-4-20250514', 3.00, 15.00, 'Anthropic Claude API (Sonnet, Opus, Haiku)'),
    ('google', 'Google', 'gemini-2.0-flash', 0.075, 0.30, 'Google Gemini API (Pro, Flash)')
ON CONFLICT (provider) DO NOTHING;

-- Cloud LLM Model Pricing Table
-- Stores per-model pricing for accurate cost calculation
CREATE TABLE IF NOT EXISTS cloud_llm_model_pricing (
    id SERIAL PRIMARY KEY,

    -- Model identification
    provider VARCHAR(32) NOT NULL,
    model_id VARCHAR(100) NOT NULL,             -- Exact model ID (e.g., 'gpt-4o-2024-08-06')
    model_name VARCHAR(100),                    -- Friendly name (e.g., 'GPT-4o')

    -- Pricing (per 1M tokens in USD)
    input_cost_per_1m DECIMAL(10, 4) NOT NULL,
    output_cost_per_1m DECIMAL(10, 4) NOT NULL,

    -- Capabilities
    max_context_length INTEGER,                 -- Max context window
    supports_vision BOOLEAN DEFAULT false,
    supports_tools BOOLEAN DEFAULT true,
    supports_streaming BOOLEAN DEFAULT true,

    -- Metadata
    effective_date DATE DEFAULT CURRENT_DATE,   -- When this pricing became effective
    deprecated BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,

    UNIQUE(provider, model_id)
);

-- Insert current model pricing (as of January 2026)
INSERT INTO cloud_llm_model_pricing (provider, model_id, model_name, input_cost_per_1m, output_cost_per_1m, max_context_length, supports_vision, supports_tools)
VALUES
    -- OpenAI models
    ('openai', 'gpt-4o', 'GPT-4o', 2.50, 10.00, 128000, true, true),
    ('openai', 'gpt-4o-mini', 'GPT-4o Mini', 0.15, 0.60, 128000, true, true),
    ('openai', 'gpt-4-turbo', 'GPT-4 Turbo', 10.00, 30.00, 128000, true, true),
    ('openai', 'gpt-3.5-turbo', 'GPT-3.5 Turbo', 0.50, 1.50, 16385, false, true),

    -- Anthropic models
    ('anthropic', 'claude-sonnet-4-20250514', 'Claude Sonnet 4', 3.00, 15.00, 200000, true, true),
    ('anthropic', 'claude-opus-4-20250514', 'Claude Opus 4', 15.00, 75.00, 200000, true, true),
    ('anthropic', 'claude-3-5-sonnet-20241022', 'Claude 3.5 Sonnet', 3.00, 15.00, 200000, true, true),
    ('anthropic', 'claude-3-5-haiku-20241022', 'Claude 3.5 Haiku', 0.80, 4.00, 200000, true, true),

    -- Google models
    ('google', 'gemini-2.0-flash', 'Gemini 2.0 Flash', 0.075, 0.30, 1000000, true, true),
    ('google', 'gemini-1.5-pro', 'Gemini 1.5 Pro', 1.25, 5.00, 2000000, true, true),
    ('google', 'gemini-1.5-flash', 'Gemini 1.5 Flash', 0.075, 0.30, 1000000, true, true)
ON CONFLICT (provider, model_id) DO UPDATE SET
    model_name = EXCLUDED.model_name,
    input_cost_per_1m = EXCLUDED.input_cost_per_1m,
    output_cost_per_1m = EXCLUDED.output_cost_per_1m,
    max_context_length = EXCLUDED.max_context_length,
    supports_vision = EXCLUDED.supports_vision,
    supports_tools = EXCLUDED.supports_tools;

-- Cloud LLM Feature Flags
INSERT INTO features (name, enabled, description, category) VALUES
('cloud_llm_enabled', false, 'Enable cloud LLM providers (OpenAI, Anthropic, Google)', 'llm'),
('cloud_llm_for_complex', false, 'Automatically route complex queries to cloud LLMs', 'llm'),
('cloud_llm_fallback', false, 'Fall back to cloud LLM when local models fail', 'llm'),
('cloud_llm_privacy_filter', true, 'Filter sensitive data before sending to cloud providers', 'llm')
ON CONFLICT (name) DO NOTHING;

-- Add comments for documentation
COMMENT ON TABLE cloud_llm_usage IS 'Tracks all cloud LLM API calls with cost and usage analytics';
COMMENT ON TABLE cloud_llm_providers IS 'Configuration for cloud LLM providers (OpenAI, Anthropic, Google)';
COMMENT ON TABLE cloud_llm_model_pricing IS 'Per-model pricing for accurate cost calculation';

COMMENT ON COLUMN cloud_llm_usage.cost_usd IS 'Cost in USD calculated from provider pricing at time of request';
COMMENT ON COLUMN cloud_llm_usage.ttft_ms IS 'Time to first token in milliseconds (streaming requests only)';
COMMENT ON COLUMN cloud_llm_usage.was_fallback IS 'True if request fell back from local LLM due to failure';
