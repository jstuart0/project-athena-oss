-- Migration: Add stage column to llm_performance_metrics
-- Date: 2025-01-07
-- Description: Adds a stage column to track the purpose of each LLM call
--              (classify, summarize, tool_selection, validation, synthesize, etc.)

-- Add stage column
ALTER TABLE llm_performance_metrics
ADD COLUMN IF NOT EXISTS stage VARCHAR(50);

-- Create index for stage filtering
CREATE INDEX IF NOT EXISTS idx_llm_metrics_stage ON llm_performance_metrics (stage);

-- Optionally update existing records based on source patterns
-- (This is a best-effort migration to populate existing data)
UPDATE llm_performance_metrics
SET stage = CASE
    WHEN source LIKE '%intent%' OR source LIKE '%classif%' THEN 'classify'
    WHEN source LIKE '%synth%' THEN 'synthesize'
    WHEN source LIKE '%tool%' THEN 'tool_selection'
    WHEN source LIKE '%valid%' THEN 'validation'
    ELSE NULL
END
WHERE stage IS NULL AND source IS NOT NULL;
