-- Migration: Add conversation_summarizer component model
-- Date: 2025-01-07
-- Description: Adds the conversation_summarizer as a configurable component

-- Add conversation_summarizer to component_model_assignments
INSERT INTO component_model_assignments (
    component_name,
    display_name,
    description,
    category,
    model_name,
    backend_type,
    temperature,
    max_tokens,
    timeout_seconds,
    enabled,
    created_at,
    updated_at
) VALUES (
    'conversation_summarizer',
    'Conversation Summarizer',
    'Compresses conversation history into brief context for follow-up queries',
    'orchestrator',
    'qwen3:4b',
    'ollama',
    0.3,
    150,
    10,
    true,
    NOW(),
    NOW()
) ON CONFLICT (component_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    updated_at = NOW();
