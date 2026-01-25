-- Seed OSS Default Configuration Data
-- Run after initial schema setup to populate essential configuration
--
-- This includes:
-- 1. Hallucination checks for response validation
-- 2. Intent routing configuration
-- 3. Tool calling triggers
-- 4. Model escalation presets and rules
-- 5. Cross-validation models

-- ============================================================================
-- 1. Hallucination Checks
-- ============================================================================
-- These checks validate LLM responses to prevent hallucinations and errors

INSERT INTO hallucination_checks (name, display_name, description, check_type,
                                 applies_to_categories, enabled, severity, configuration,
                                 error_message_template, auto_fix_enabled,
                                 require_cross_model_validation, confidence_threshold,
                                 priority, created_at, updated_at)
VALUES
    ('weather_location_required', 'Weather Location Check',
     'Ensures weather queries include a valid location', 'required_elements',
     ARRAY['weather']::text[], true, 'error',
     '{"required_fields": ["location"]}'::jsonb,
     'Weather query must include a location. Please specify where.',
     false, false, 0.7, 100, NOW(), NOW()),

    ('home_device_validation', 'Home Device Validation',
     'Validates that referenced devices exist in Home Assistant', 'fact_checking',
     ARRAY['home_control']::text[], true, 'warning',
     '{"check_ha_entities": true, "allow_fuzzy_match": true}'::jsonb,
     'Device not found in your home. Did you mean: {suggestions}?',
     true, false, 0.8, 90, NOW(), NOW()),

    ('confidence_threshold', 'Confidence Threshold Check',
     'Requires minimum confidence score for all responses', 'confidence_threshold',
     ARRAY[]::text[], true, 'warning',
     '{"min_confidence": 0.6, "reject_low_confidence": false}'::jsonb,
     'Response confidence is low. Consider asking for clarification.',
     false, false, 0.6, 80, NOW(), NOW()),

    ('cross_model_agreement', 'Cross-Model Agreement',
     'Validates responses using multiple models for critical actions', 'cross_validation',
     ARRAY['home_control']::text[], true, 'error',
     '{"require_agreement": true, "min_models": 2, "agreement_threshold": 0.8}'::jsonb,
     'Multiple models disagree on this action. Validation failed.',
     false, true, 0.8, 95, NOW(), NOW()),

    ('factual_grounding', 'Factual Grounding Check',
     'Ensures responses are grounded in provided RAG context', 'fact_checking',
     ARRAY['news', 'sports', 'stocks']::text[], true, 'warning',
     '{"require_source_citation": false, "check_date_relevance": true}'::jsonb,
     'Could not verify this information from available sources.',
     false, false, 0.7, 85, NOW(), NOW()),

    ('numeric_sanity', 'Numeric Sanity Check',
     'Validates that numeric values are within reasonable ranges', 'sanity_check',
     ARRAY['weather', 'stocks', 'sports']::text[], true, 'error',
     '{"check_temperature_range": [-100, 150], "check_percentage_range": [0, 100]}'::jsonb,
     'The numeric value seems incorrect. Please verify.',
     true, false, 0.9, 75, NOW(), NOW())
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- 2. Intent Routing Configuration
-- ============================================================================
-- Defines how different intents are routed to RAG services

INSERT INTO intent_routing_config (intent_name, display_name, routing_strategy, enabled, priority, config)
VALUES
    ('weather', 'Weather', 'cascading', true, 10, '{"primary_service": "weather", "fallback_services": ["onecall"]}'::jsonb),
    ('dining', 'Dining & Restaurants', 'cascading', true, 10, '{"primary_service": "dining"}'::jsonb),
    ('sports', 'Sports Scores', 'cascading', true, 10, '{"primary_service": "sports"}'::jsonb),
    ('stocks', 'Stock Prices', 'cascading', true, 10, '{"primary_service": "stocks"}'::jsonb),
    ('news', 'News', 'cascading', true, 10, '{"primary_service": "news"}'::jsonb),
    ('events', 'Events', 'cascading', true, 10, '{"primary_service": "events", "fallback_services": ["seatgeek"]}'::jsonb),
    ('flights', 'Flight Status', 'cascading', true, 10, '{"primary_service": "flights"}'::jsonb),
    ('airports', 'Airport Info', 'cascading', true, 10, '{"primary_service": "airports"}'::jsonb),
    ('recipes', 'Recipes', 'cascading', true, 10, '{"primary_service": "recipes"}'::jsonb),
    ('streaming', 'Streaming Services', 'cascading', true, 10, '{"primary_service": "streaming"}'::jsonb),
    ('websearch', 'Web Search', 'always_tool_calling', true, 5, '{"primary_service": "websearch"}'::jsonb),
    ('directions', 'Directions', 'cascading', true, 10, '{"primary_service": "directions"}'::jsonb),
    ('home_control', 'Smart Home Control', 'direct_only', true, 20, '{"use_ha_api": true}'::jsonb),
    ('general', 'General Knowledge', 'always_tool_calling', true, 1, '{"use_llm_knowledge": true}'::jsonb)
ON CONFLICT (intent_name) DO NOTHING;

-- ============================================================================
-- 3. Tool Calling Triggers
-- ============================================================================
-- Defines when to invoke tool calling vs direct RAG

INSERT INTO tool_calling_triggers (trigger_name, trigger_type, enabled, priority, config, description)
VALUES
    ('low_confidence', 'confidence', true, 100,
     '{"threshold": 0.5, "action": "escalate_to_tool_calling"}'::jsonb,
     'Trigger tool calling when intent confidence is below threshold'),

    ('empty_rag_results', 'empty_rag', true, 90,
     '{"check_empty": true, "check_null": true, "min_results": 1}'::jsonb,
     'Trigger tool calling when RAG returns no results'),

    ('multi_intent_query', 'intent', true, 85,
     '{"min_intents": 2, "action": "parallel_tool_calling"}'::jsonb,
     'Use tool calling for queries with multiple intents'),

    ('complex_query', 'keywords', true, 80,
     '{"patterns": ["compare", "versus", "difference between", "which is better", "pros and cons"]}'::jsonb,
     'Trigger tool calling for complex comparative queries'),

    ('validation_required', 'validation', true, 95,
     '{"categories": ["home_control"], "require_confirmation": true}'::jsonb,
     'Require validation before executing control commands'),

    ('time_sensitive', 'keywords', true, 75,
     '{"patterns": ["right now", "currently", "at this moment", "live", "real-time"]}'::jsonb,
     'Use tool calling for time-sensitive queries'),

    ('aggregation_query', 'keywords', true, 70,
     '{"patterns": ["all", "every", "list all", "show me all", "summarize"]}'::jsonb,
     'Use tool calling for queries requiring data aggregation')
ON CONFLICT (trigger_name) DO NOTHING;

-- ============================================================================
-- 4. Escalation Presets
-- ============================================================================
-- Model escalation presets for different use cases

INSERT INTO escalation_presets (name, description, is_active, auto_activate_conditions) VALUES
    ('Balanced', 'Default everyday use - reasonable escalation on clear signals', true, NULL),
    ('Conservative', 'Quality first - escalate early, stay high longer', false, NULL),
    ('Efficient', 'Cost/speed conscious - only escalate on clear failures', false, NULL),
    ('Demo Mode', 'Always use best models - for presentations/demos', false, NULL),
    ('Late Night', 'After 11pm - assume tired/terse user, be more forgiving', false, '{"time_range": {"start": "23:00", "end": "06:00"}}'::jsonb),
    ('Guest Mode', 'For guests - more patient, assume unfamiliar phrasing', false, '{"user_mode": "guest"}'::jsonb)
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- 5. Escalation Rules for Balanced Preset (default active)
-- ============================================================================

-- Get the Balanced preset ID (should be 1)
DO $$
DECLARE
    balanced_id INTEGER;
    conservative_id INTEGER;
    efficient_id INTEGER;
    demo_id INTEGER;
    late_night_id INTEGER;
    guest_id INTEGER;
BEGIN
    SELECT id INTO balanced_id FROM escalation_presets WHERE name = 'Balanced';
    SELECT id INTO conservative_id FROM escalation_presets WHERE name = 'Conservative';
    SELECT id INTO efficient_id FROM escalation_presets WHERE name = 'Efficient';
    SELECT id INTO demo_id FROM escalation_presets WHERE name = 'Demo Mode';
    SELECT id INTO late_night_id FROM escalation_presets WHERE name = 'Late Night';
    SELECT id INTO guest_id FROM escalation_presets WHERE name = 'Guest Mode';

    -- Balanced preset rules
    IF balanced_id IS NOT NULL THEN
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description, enabled) VALUES
            (balanced_id, 'Clarification Request', 'clarification', '{"patterns": ["could you clarify", "what do you mean", "can you specify", "i''m not sure what", "could you be more specific"]}'::jsonb, 'complex', 5, 100, 'LLM asked for clarification', true),
            (balanced_id, 'User Correction', 'user_correction', '{"patterns": ["no,", "no ", "that''s wrong", "that''s not what", "not what I asked", "I meant", "I said"]}'::jsonb, 'complex', 5, 90, 'User corrected the assistant', true),
            (balanced_id, 'User Frustration', 'user_frustration', '{"patterns": ["you''re confused", "that doesn''t make sense", "try again", "not helpful", "this is wrong"]}'::jsonb, 'super_complex', 5, 80, 'User expressed frustration', true),
            (balanced_id, 'Empty Tool Results', 'empty_results', '{"check_empty": true, "check_null": true}'::jsonb, 'complex', 3, 70, 'Tool returned no results', true),
            (balanced_id, 'Tool Failure', 'tool_failure', '{"on_error": true}'::jsonb, 'complex', 3, 60, 'Tool returned an error', true),
            (balanced_id, 'Explicit Upgrade Request', 'explicit_request', '{"patterns": ["think harder", "be more careful", "think about it", "try a better model"]}'::jsonb, 'super_complex', 3, 110, 'User explicitly asked for better response', true)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Conservative preset rules
    IF conservative_id IS NOT NULL THEN
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description, enabled) VALUES
            (conservative_id, 'Any Clarification', 'clarification', '{"patterns": ["could you", "what do you", "can you", "?"], "match_in_response": true}'::jsonb, 'complex', 8, 100, 'Any clarification question in response', true),
            (conservative_id, 'Short Response', 'short_response', '{"max_length": 50}'::jsonb, 'complex', 5, 90, 'Response was very short', true),
            (conservative_id, 'User Says No', 'user_correction', '{"patterns": ["no", "nope", "wrong", "incorrect"]}'::jsonb, 'super_complex', 8, 85, 'User said no or wrong', true),
            (conservative_id, 'Any Frustration Signal', 'user_frustration', '{"patterns": ["confused", "doesn''t", "didn''t", "can''t", "won''t", "not working"]}'::jsonb, 'super_complex', 8, 80, 'Any frustration signal', true),
            (conservative_id, 'Empty Results', 'empty_results', '{"check_empty": true, "check_null": true}'::jsonb, 'super_complex', 5, 70, 'Empty results - escalate to super_complex', true),
            (conservative_id, 'Repeated Query', 'repeated_query', '{"similarity_threshold": 0.8}'::jsonb, 'super_complex', 5, 95, 'User repeated similar query', true)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Efficient preset rules
    IF efficient_id IS NOT NULL THEN
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description, enabled) VALUES
            (efficient_id, 'Strong Frustration Only', 'user_frustration', '{"patterns": ["completely wrong", "this is broken", "useless", "terrible"]}'::jsonb, 'complex', 3, 100, 'Only escalate on strong frustration', true),
            (efficient_id, 'Explicit Request', 'explicit_request', '{"patterns": ["use a better model", "think harder", "try harder"]}'::jsonb, 'super_complex', 2, 110, 'User explicitly requested upgrade', true),
            (efficient_id, 'Multiple Tool Failures', 'tool_failure', '{"consecutive_failures": 2}'::jsonb, 'complex', 2, 80, 'Only after 2 consecutive failures', true)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Demo Mode preset rules
    IF demo_id IS NOT NULL THEN
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description, enabled) VALUES
            (demo_id, 'Always Escalate', 'always', '{"always": true}'::jsonb, 'super_complex', 999, 1000, 'Always use best model in demo mode', true)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Late Night preset rules
    IF late_night_id IS NOT NULL THEN
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description, enabled) VALUES
            (late_night_id, 'Very Short Query', 'short_query', '{"max_words": 3}'::jsonb, 'complex', 5, 100, 'Short queries at night need more help', true),
            (late_night_id, 'Any Clarification', 'clarification', '{"patterns": ["what", "huh", "?", "clarify"]}'::jsonb, 'complex', 8, 90, 'Be more helpful with clarifications', true),
            (late_night_id, 'Terse Correction', 'user_correction', '{"patterns": ["no", "wrong", "nope", "not that"]}'::jsonb, 'super_complex', 8, 85, 'Terse corrections need best model', true),
            (late_night_id, 'Night Frustration', 'user_frustration', '{"patterns": ["ugh", "come on", "seriously", "whatever"]}'::jsonb, 'super_complex', 10, 80, 'Tired user frustration', true)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Guest Mode preset rules
    IF guest_id IS NOT NULL THEN
        INSERT INTO escalation_rules (preset_id, rule_name, trigger_type, trigger_patterns, escalation_target, escalation_duration, priority, description, enabled) VALUES
            (guest_id, 'Any Question in Response', 'clarification', '{"patterns": ["?"]}'::jsonb, 'complex', 5, 100, 'Any question mark in response', true),
            (guest_id, 'Polite Correction', 'user_correction', '{"patterns": ["actually", "I meant", "sorry, I wanted", "I was asking"]}'::jsonb, 'complex', 5, 90, 'Polite guest corrections', true),
            (guest_id, 'Any Frustration', 'user_frustration', '{"patterns": ["not working", "doesn''t understand", "wrong", "can''t"]}'::jsonb, 'super_complex', 5, 80, 'Guest frustration', true),
            (guest_id, 'Unrecognized Entity', 'entity_unknown', '{"check_location": true, "check_names": true}'::jsonb, 'complex', 3, 85, 'Failed to recognize location or name', true)
        ON CONFLICT DO NOTHING;
    END IF;
END $$;

-- ============================================================================
-- 6. Cross-Validation Models
-- ============================================================================
-- Models used for cross-validating responses

INSERT INTO cross_validation_models (name, model_id, model_type, endpoint_url, enabled,
                                    use_for_categories, temperature, max_tokens,
                                    timeout_seconds, weight, min_confidence_required, created_at)
VALUES
    ('primary-validator', 'qwen3:4b', 'primary', 'http://ollama:11434', true,
     ARRAY['home_control', 'weather', 'sports']::text[], 0.1, 200, 30, 1.0, 0.7, NOW()),
    ('secondary-validator', 'phi3:mini', 'validation', 'http://ollama:11434', true,
     ARRAY['home_control']::text[], 0.1, 200, 30, 0.8, 0.6, NOW())
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- 7. Multi-Intent Configuration
-- ============================================================================
-- Configuration for handling queries with multiple intents

INSERT INTO multi_intent_config (id, enabled, max_intents_per_query, separators,
                                context_preservation, parallel_processing,
                                combination_strategy, min_words_per_intent,
                                context_words_to_preserve, updated_at)
SELECT 1, true, 3,
       ARRAY[' and ', ' then ', ' also ', ', then ', '; ']::text[],
       true, false, 'concatenate', 2,
       ARRAY['the', 'my', 'in', 'at', 'to']::text[],
       NOW()
WHERE NOT EXISTS (SELECT 1 FROM multi_intent_config WHERE id = 1);

-- ============================================================================
-- 8. Intent Chain Rules (Compound Commands)
-- ============================================================================
-- Rules for handling compound/routine commands

INSERT INTO intent_chain_rules (name, trigger_pattern, intent_sequence, enabled,
                               description, examples, require_all, stop_on_error, created_at)
VALUES
    ('goodnight_routine', '(?i)goodnight|good night',
     ARRAY['turn_off_lights', 'lock_doors', 'set_thermostat_night']::text[], true,
     'Automated goodnight routine - turns off lights, locks doors, adjusts thermostat',
     ARRAY['goodnight', 'good night', 'time for bed']::text[],
     false, true, NOW()),

    ('leaving_home', '(?i)(leaving|heading out|going out)',
     ARRAY['lock_doors', 'turn_off_lights', 'set_thermostat_away']::text[], true,
     'Leaving home routine - secures house and saves energy',
     ARRAY['I''m leaving', 'heading out', 'leaving now']::text[],
     false, true, NOW()),

    ('movie_time', '(?i)(movie time|watch a movie|movie night)',
     ARRAY['dim_lights', 'set_tv_mode', 'close_blinds']::text[], true,
     'Movie watching setup - dims lights and prepares entertainment',
     ARRAY['movie time', 'let''s watch a movie']::text[],
     false, false, NOW()),

    ('wake_up', '(?i)(good morning|wake up routine|morning)',
     ARRAY['turn_on_lights', 'start_coffee', 'read_schedule']::text[], true,
     'Morning wake up routine - lights, coffee, and daily briefing',
     ARRAY['good morning', 'wake up', 'start my day']::text[],
     false, false, NOW())
ON CONFLICT (name) DO UPDATE SET
    trigger_pattern = EXCLUDED.trigger_pattern,
    intent_sequence = EXCLUDED.intent_sequence,
    description = EXCLUDED.description,
    examples = EXCLUDED.examples;

-- ============================================================================
-- Verification
-- ============================================================================
DO $$
BEGIN
    RAISE NOTICE 'OSS Seed Data Applied:';
    RAISE NOTICE '  - Hallucination checks: %', (SELECT COUNT(*) FROM hallucination_checks);
    RAISE NOTICE '  - Intent routing configs: %', (SELECT COUNT(*) FROM intent_routing_config);
    RAISE NOTICE '  - Tool calling triggers: %', (SELECT COUNT(*) FROM tool_calling_triggers);
    RAISE NOTICE '  - Escalation presets: %', (SELECT COUNT(*) FROM escalation_presets);
    RAISE NOTICE '  - Escalation rules: %', (SELECT COUNT(*) FROM escalation_rules);
    RAISE NOTICE '  - Cross-validation models: %', (SELECT COUNT(*) FROM cross_validation_models);
    RAISE NOTICE '  - Intent chain rules: %', (SELECT COUNT(*) FROM intent_chain_rules);
END $$;
