-- Migration 026: OSS Default Model Configuration
-- Description: Sets up qwen3:4b as the default model for OSS deployments
-- Date: 2025-01-24
--
-- This migration ensures a working out-of-the-box experience for OSS users by:
-- 1. Adding qwen3:4b to llm_backends (primary default model)
-- 2. Updating all component_model_assignments to use qwen3:4b
--
-- Prerequisites:
-- - Ollama must be running with qwen3:4b model available
-- - The model can be pulled via: ollama pull qwen3:4b

-- ============================================================================
-- SEED LLM BACKEND: qwen3:4b
-- ============================================================================

-- Add qwen3:4b as the primary OSS model
INSERT INTO llm_backends (
    model_name,
    backend_type,
    endpoint_url,
    enabled,
    priority,
    max_tokens,
    temperature_default,
    timeout_seconds,
    keep_alive_seconds,
    description,
    created_at,
    updated_at,
    total_requests,
    total_errors
)
VALUES (
    'qwen3:4b',
    'ollama',
    COALESCE(current_setting('app.ollama_url', true), 'http://localhost:11434'),
    true,
    50,  -- High priority (lower number = higher priority)
    4096,
    0.7,
    90,
    -1,  -- Keep model loaded indefinitely for fast responses
    'Qwen3 4B - Default OSS model for all components. Optimized for speed and quality balance.',
    NOW(),
    NOW(),
    0,
    0
)
ON CONFLICT (model_name) DO UPDATE SET
    enabled = true,
    priority = 50,
    description = 'Qwen3 4B - Default OSS model for all components. Optimized for speed and quality balance.',
    updated_at = NOW();

-- ============================================================================
-- UPDATE COMPONENT MODEL ASSIGNMENTS TO USE qwen3:4b
-- ============================================================================

-- Update all orchestrator components to use qwen3:4b
UPDATE component_model_assignments
SET
    model_name = 'qwen3:4b',
    updated_at = NOW()
WHERE category = 'orchestrator';

-- Update validation components to use qwen3:4b
UPDATE component_model_assignments
SET
    model_name = 'qwen3:4b',
    updated_at = NOW()
WHERE category = 'validation';

-- Update control components to use qwen3:4b
UPDATE component_model_assignments
SET
    model_name = 'qwen3:4b',
    updated_at = NOW()
WHERE category = 'control';

-- ============================================================================
-- ENSURE ALL REQUIRED COMPONENTS EXIST
-- ============================================================================

-- Insert any missing components with qwen3:4b as default
INSERT INTO component_model_assignments
    (component_name, display_name, description, category, model_name, backend_type, temperature, enabled)
VALUES
    ('intent_classifier', 'Intent Classification', 'Classifies user queries into intent categories', 'orchestrator', 'qwen3:4b', 'ollama', 0.3, true),
    ('tool_calling_simple', 'Tool Calling (Simple)', 'Selects RAG tools for simple queries', 'orchestrator', 'qwen3:4b', 'ollama', 0.7, true),
    ('tool_calling_complex', 'Tool Calling (Complex)', 'Selects RAG tools for complex queries', 'orchestrator', 'qwen3:4b', 'ollama', 0.7, true),
    ('tool_calling_super_complex', 'Tool Calling (Super Complex)', 'Selects RAG tools for highly complex queries', 'orchestrator', 'qwen3:4b', 'ollama', 0.7, true),
    ('response_synthesis', 'Response Synthesis', 'Generates natural language responses from RAG results', 'orchestrator', 'qwen3:4b', 'ollama', 0.7, true),
    ('fact_check_validation', 'Fact-Check Validation', 'Validates responses for accuracy', 'validation', 'qwen3:4b', 'ollama', 0.1, true),
    ('smart_home_control', 'Smart Home Control', 'Extracts device commands from natural language', 'control', 'qwen3:4b', 'ollama', 0.1, true),
    ('response_validator_primary', 'Response Validator (Primary)', 'Primary model for cross-validation', 'validation', 'qwen3:4b', 'ollama', 0.1, true),
    ('response_validator_secondary', 'Response Validator (Secondary)', 'Secondary model for cross-validation', 'validation', 'qwen3:4b', 'ollama', 0.1, true),
    ('conversation_summarizer', 'Conversation Summarizer', 'Compresses conversation history into brief context', 'orchestrator', 'qwen3:4b', 'ollama', 0.3, true)
ON CONFLICT (component_name) DO UPDATE SET
    model_name = 'qwen3:4b',
    updated_at = NOW();

-- ============================================================================
-- UPDATE CROSS-VALIDATION MODELS (if they exist)
-- ============================================================================

UPDATE cross_validation_models
SET
    model_id = 'qwen3:4b',
    updated_at = NOW()
WHERE model_id IN ('phi3:mini', 'llama3.1:8b-q4', 'qwen2.5:1.5b', 'qwen2.5:7b');

-- ============================================================================
-- VERIFICATION
-- ============================================================================

DO $$
DECLARE
    backend_count INTEGER;
    component_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO backend_count FROM llm_backends WHERE model_name = 'qwen3:4b' AND enabled = true;
    SELECT COUNT(*) INTO component_count FROM component_model_assignments WHERE model_name = 'qwen3:4b';

    RAISE NOTICE 'Migration 026 (OSS Default Model) completed:';
    RAISE NOTICE '  - qwen3:4b backend enabled: %', backend_count > 0;
    RAISE NOTICE '  - Components using qwen3:4b: %', component_count;
    RAISE NOTICE '';
    RAISE NOTICE 'IMPORTANT: Ensure qwen3:4b is pulled in Ollama:';
    RAISE NOTICE '  ollama pull qwen3:4b';
END $$;
