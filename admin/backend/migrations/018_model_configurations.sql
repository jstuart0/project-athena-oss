-- Migration: 018_model_configurations.sql
-- Description: Add model_configurations table for dynamic LLM model settings
-- Date: 2026-01-05

-- Create model_configurations table
CREATE TABLE IF NOT EXISTS model_configurations (
    id SERIAL PRIMARY KEY,
    model_name VARCHAR(100) NOT NULL UNIQUE,  -- e.g., "qwen3:8b", "phi3:mini", "_default"
    display_name VARCHAR(200),                 -- e.g., "Qwen3 8B"
    backend_type VARCHAR(20) NOT NULL DEFAULT 'ollama',  -- ollama, mlx, auto
    enabled BOOLEAN NOT NULL DEFAULT true,

    -- Core settings (direct columns for common options)
    temperature DECIMAL(3,2) DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 2048,
    timeout_seconds INTEGER DEFAULT 60,
    keep_alive_seconds INTEGER DEFAULT -1,

    -- Extended options (JSONB for flexibility)
    ollama_options JSONB DEFAULT '{}',  -- num_ctx, num_batch, top_k, top_p, mirostat, etc.
    mlx_options JSONB DEFAULT '{}',     -- max_kv_size, quantization, etc.

    -- Metadata
    description TEXT,
    priority INTEGER DEFAULT 0,          -- For ordering/fallback
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create index on model_name for fast lookups
CREATE INDEX IF NOT EXISTS idx_model_configurations_model_name ON model_configurations(model_name);
CREATE INDEX IF NOT EXISTS idx_model_configurations_enabled ON model_configurations(enabled);

-- Create updated_at trigger
CREATE OR REPLACE FUNCTION update_model_configurations_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS model_configurations_updated ON model_configurations;
CREATE TRIGGER model_configurations_updated
    BEFORE UPDATE ON model_configurations
    FOR EACH ROW
    EXECUTE FUNCTION update_model_configurations_timestamp();

-- Seed data: Default configuration for unconfigured models
INSERT INTO model_configurations (model_name, display_name, description, ollama_options, priority)
VALUES ('_default', 'Default Configuration', 'Applied to models without explicit configuration',
        '{"num_ctx": 4096, "num_batch": 256}', -1)
ON CONFLICT (model_name) DO NOTHING;

-- Seed data: Pre-configured models with baseline settings
INSERT INTO model_configurations (model_name, display_name, backend_type, ollama_options, priority) VALUES
('qwen3:4b', 'Qwen3 4B', 'ollama', '{"num_ctx": 4096, "num_batch": 256}', 50),
('qwen3:8b', 'Qwen3 8B', 'ollama', '{"num_ctx": 4096, "num_batch": 256}', 50),
('qwen3:14b', 'Qwen3 14B', 'ollama', '{"num_ctx": 4096, "num_batch": 256}', 50),
('phi3:mini', 'Phi-3 Mini', 'ollama', '{"num_ctx": 4096, "num_batch": 256}', 50),
('llama3.1:8b', 'Llama 3.1 8B', 'ollama', '{"num_ctx": 4096, "num_batch": 256}', 50)
ON CONFLICT (model_name) DO NOTHING;

-- Comment on table
COMMENT ON TABLE model_configurations IS 'Dynamic LLM model configurations with Ollama/MLX options';
COMMENT ON COLUMN model_configurations.model_name IS 'Model identifier (e.g., qwen3:8b). Use _default for fallback config.';
COMMENT ON COLUMN model_configurations.ollama_options IS 'JSONB with num_ctx, num_batch, top_k, top_p, mirostat, mirostat_tau, mirostat_eta, repeat_penalty, etc.';
COMMENT ON COLUMN model_configurations.mlx_options IS 'JSONB with max_kv_size, quantization, etc.';
