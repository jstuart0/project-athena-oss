-- Migration 009: Component Model Assignments and Service Control
-- Description: Adds tables for configurable LLM model assignments per component
--              and service control/monitoring capabilities
-- Date: 2025-11-28

-- ============================================================================
-- COMPONENT MODEL ASSIGNMENTS
-- ============================================================================

CREATE TABLE IF NOT EXISTS component_model_assignments (
    id SERIAL PRIMARY KEY,
    component_name VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    description TEXT,
    category VARCHAR(50) NOT NULL DEFAULT 'orchestrator',
    model_name VARCHAR(255) NOT NULL,
    backend_type VARCHAR(32) NOT NULL DEFAULT 'ollama',
    temperature FLOAT,
    max_tokens INTEGER,
    timeout_seconds INTEGER,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_component_model_category ON component_model_assignments(category);
CREATE INDEX IF NOT EXISTS idx_component_model_enabled ON component_model_assignments(enabled);

-- ============================================================================
-- ATHENA SERVICES
-- ============================================================================

CREATE TABLE IF NOT EXISTS athena_services (
    id SERIAL PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    description TEXT,
    service_type VARCHAR(50) NOT NULL,
    host VARCHAR(255) NOT NULL,
    port INTEGER NOT NULL,
    health_endpoint VARCHAR(255) DEFAULT '/health',
    control_method VARCHAR(50) NOT NULL DEFAULT 'docker',
    container_name VARCHAR(255),
    is_running BOOLEAN DEFAULT false,
    last_health_check TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    auto_start BOOLEAN DEFAULT true,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_athena_services_type ON athena_services(service_type);
CREATE INDEX IF NOT EXISTS idx_athena_services_running ON athena_services(is_running);

-- ============================================================================
-- SEED DATA: Component Model Assignments
-- ============================================================================

INSERT INTO component_model_assignments
    (component_name, display_name, description, category, model_name, backend_type, temperature, enabled)
VALUES
    ('intent_classifier', 'Intent Classification', 'Classifies user queries into intent categories (weather, sports, smart_home, etc.)', 'orchestrator', 'qwen2.5:1.5b', 'ollama', 0.3, true),
    ('tool_calling_simple', 'Tool Calling (Simple)', 'Selects RAG tools for simple queries', 'orchestrator', 'qwen2.5:7b', 'ollama', 0.7, true),
    ('tool_calling_complex', 'Tool Calling (Complex)', 'Selects RAG tools for complex multi-step queries', 'orchestrator', 'qwen2.5:7b', 'ollama', 0.7, true),
    ('tool_calling_super_complex', 'Tool Calling (Super Complex)', 'Selects RAG tools for highly complex queries', 'orchestrator', 'qwen2.5:14b-instruct-q4_K_M', 'ollama', 0.7, true),
    ('response_synthesis', 'Response Synthesis', 'Generates natural language responses from RAG tool results', 'orchestrator', 'qwen2.5:7b', 'ollama', 0.7, true),
    ('fact_check_validation', 'Fact-Check Validation', 'Validates responses for accuracy and hallucinations', 'validation', 'qwen2.5:7b', 'ollama', 0.1, true),
    ('smart_home_control', 'Smart Home Control', 'Extracts device commands from natural language for Home Assistant', 'control', 'llama3.1:8b', 'ollama', 0.1, true),
    ('response_validator_primary', 'Response Validator (Primary)', 'Primary model for cross-validation', 'validation', 'phi3:mini', 'ollama', 0.1, true),
    ('response_validator_secondary', 'Response Validator (Secondary)', 'Secondary model for cross-validation comparison', 'validation', 'phi3:mini', 'ollama', 0.1, true)
ON CONFLICT (component_name) DO NOTHING;

-- ============================================================================
-- SEED DATA: Athena Services
-- ============================================================================

INSERT INTO athena_services
    (service_name, display_name, description, service_type, host, port, health_endpoint, control_method, container_name)
VALUES
    ('gateway', 'Gateway', 'Main API gateway for voice assistant', 'core', 'localhost', 8000, '/health', 'docker', 'athena-gateway'),
    ('orchestrator', 'Orchestrator', 'Query orchestration and LLM coordination', 'core', 'localhost', 8001, '/health', 'docker', 'athena-orchestrator'),
    ('ollama', 'Ollama', 'LLM inference server', 'llm', 'localhost', 11434, '/api/tags', 'launchd', NULL),
    ('weather-rag', 'Weather RAG', 'Weather information service', 'rag', 'localhost', 8010, '/health', 'docker', 'athena-weather'),
    ('airports-rag', 'Airports RAG', 'Airport information service', 'rag', 'localhost', 8011, '/health', 'docker', 'athena-airports'),
    ('flights-rag', 'Flights RAG', 'Flight tracking service', 'rag', 'localhost', 8012, '/health', 'docker', 'athena-flights'),
    ('news-rag', 'News RAG', 'News retrieval service', 'rag', 'localhost', 8013, '/health', 'docker', 'athena-news'),
    ('stocks-rag', 'Stocks RAG', 'Stock market data service', 'rag', 'localhost', 8014, '/health', 'docker', 'athena-stocks'),
    ('recipes-rag', 'Recipes RAG', 'Recipe search service', 'rag', 'localhost', 8015, '/health', 'docker', 'athena-recipes'),
    ('events-rag', 'Events RAG', 'Event information service', 'rag', 'localhost', 8016, '/health', 'docker', 'athena-events'),
    ('sports-rag', 'Sports RAG', 'Sports information service', 'rag', 'localhost', 8017, '/health', 'docker', 'athena-sports'),
    ('streaming-rag', 'Streaming RAG', 'Streaming service info', 'rag', 'localhost', 8018, '/health', 'docker', 'athena-streaming'),
    ('dining-rag', 'Dining RAG', 'Restaurant search service', 'rag', 'localhost', 8019, '/health', 'docker', 'athena-dining'),
    ('websearch-rag', 'Web Search RAG', 'Web search service', 'rag', 'localhost', 8020, '/health', 'docker', 'athena-websearch'),
    ('qdrant', 'Qdrant', 'Vector database', 'infrastructure', 'localhost', 6333, '/healthz', 'docker', 'qdrant'),
    ('redis', 'Redis', 'Cache server', 'infrastructure', 'localhost', 6379, '/', 'docker', 'redis'),
    ('control-agent', 'Control Agent', 'Service control agent for Docker/system service management', 'infrastructure', 'localhost', 8099, '/health', 'none', NULL)
ON CONFLICT (service_name) DO NOTHING;

-- ============================================================================
-- UPDATE TRIGGERS
-- ============================================================================

-- Apply update trigger to new tables
CREATE TRIGGER update_component_model_assignments_updated_at
    BEFORE UPDATE ON component_model_assignments
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_athena_services_updated_at
    BEFORE UPDATE ON athena_services
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- VERIFICATION
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE 'Migration 009 completed successfully';
    RAISE NOTICE 'Created tables:';
    RAISE NOTICE '  - component_model_assignments (9 default components)';
    RAISE NOTICE '  - athena_services (17 default services)';
END $$;
