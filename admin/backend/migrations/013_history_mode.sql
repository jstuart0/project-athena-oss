-- Migration 013: Add history_mode to conversation_settings
-- Description: Adds configurable history mode (none, summarized, full)
-- Date: 2026-01-02

-- Add history_mode column with default 'full' (preserves current behavior)
ALTER TABLE conversation_settings
ADD COLUMN IF NOT EXISTS history_mode VARCHAR(20) NOT NULL DEFAULT 'full';

-- Add constraint to ensure valid values
ALTER TABLE conversation_settings
ADD CONSTRAINT chk_history_mode CHECK (history_mode IN ('none', 'summarized', 'full'));

-- Update updated_at trigger if exists
UPDATE conversation_settings SET updated_at = CURRENT_TIMESTAMP WHERE id = 1;
