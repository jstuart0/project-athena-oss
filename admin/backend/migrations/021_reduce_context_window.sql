-- Migration: 021_reduce_context_window.sql
-- Description: Reduce num_ctx from 4096 to 2048 for faster inference
-- Date: 2026-01-05
-- Rationale: Each doubling of context = ~2x slower inference. Voice assistant rarely needs >2K context.
--            Expected savings: 0.5-1 second per query.

-- Update _default configuration
UPDATE model_configurations
SET ollama_options = jsonb_set(
    COALESCE(ollama_options, '{}'),
    '{num_ctx}',
    '2048'
)
WHERE model_name = '_default';

-- Update all qwen3 models
UPDATE model_configurations
SET ollama_options = jsonb_set(
    COALESCE(ollama_options, '{}'),
    '{num_ctx}',
    '2048'
)
WHERE model_name LIKE 'qwen3:%';

-- Update phi3 models
UPDATE model_configurations
SET ollama_options = jsonb_set(
    COALESCE(ollama_options, '{}'),
    '{num_ctx}',
    '2048'
)
WHERE model_name LIKE 'phi3:%';

-- Update llama3.1 models
UPDATE model_configurations
SET ollama_options = jsonb_set(
    COALESCE(ollama_options, '{}'),
    '{num_ctx}',
    '2048'
)
WHERE model_name LIKE 'llama3%';

-- Log the changes (for verification)
DO $$
BEGIN
    RAISE NOTICE 'Updated num_ctx to 2048 for all voice assistant models';
END $$;
