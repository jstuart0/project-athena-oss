-- Migration: Add disable_thinking support and seed no-thinking defaults
-- Date: 2026-04-02
-- Purpose: Ensure Qwen-based website Athena components disable reasoning/thinking

ALTER TABLE component_model_assignments
ADD COLUMN IF NOT EXISTS disable_thinking BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN component_model_assignments.disable_thinking IS
'When True, adds /no_think to component prompts for Qwen-based models to skip reasoning/thinking mode';

-- Default all currently assigned Qwen-based components to no-thinking mode.
UPDATE component_model_assignments
SET disable_thinking = TRUE
WHERE model_name IN (
    '/Users/jstuart/models/mlx/Qwen3-4B-Instruct-2507-4bit',
    '/Users/jstuart/models/mlx/Qwen3-8B-4bit',
    'qwen3:4b-instruct-2507-q4_K_M',
    'qwen3:4b',
    'qwen3:8b'
)
OR model_name ILIKE 'qwen3:%'
OR model_name ILIKE '%Qwen3%';

-- For MLX-backed Qwen models, disable thinking at the template layer too.
UPDATE model_configurations
SET mlx_options = '{"chat_template_kwargs":{"enable_thinking":false}}'::jsonb
WHERE model_name IN (
    '/Users/jstuart/models/mlx/Qwen3-4B-Instruct-2507-4bit',
    '/Users/jstuart/models/mlx/Qwen3-8B-4bit'
);
