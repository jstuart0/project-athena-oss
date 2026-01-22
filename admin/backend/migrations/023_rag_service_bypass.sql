-- RAG Service Bypass Configuration
-- Allows specific services to be routed to cloud LLMs instead of local RAG services

CREATE TABLE IF NOT EXISTS rag_service_bypass (
    id SERIAL PRIMARY KEY,
    service_name VARCHAR(50) UNIQUE NOT NULL,    -- e.g., 'recipes', 'websearch'
    display_name VARCHAR(100),                    -- Human-readable name
    bypass_enabled BOOLEAN DEFAULT false,

    -- Cloud LLM Configuration
    cloud_provider VARCHAR(32),                   -- 'openai', 'anthropic', 'google', or NULL for any
    cloud_model VARCHAR(100),                     -- Specific model or NULL for default

    -- Custom Instructions
    system_prompt TEXT,                           -- Instructions for the cloud LLM

    -- Conditions (when to bypass)
    bypass_conditions JSONB DEFAULT '{}',         -- Query patterns, complexity thresholds, etc.

    -- Performance Settings
    temperature DECIMAL(3,2) DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 1024,

    -- Metadata
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_by_id INTEGER REFERENCES users(id)
);

-- Index for quick lookups
CREATE INDEX IF NOT EXISTS idx_rag_service_bypass_enabled ON rag_service_bypass(service_name) WHERE bypass_enabled = true;

-- Seed initial bypass configurations (disabled by default)
INSERT INTO rag_service_bypass (service_name, display_name, description, system_prompt) VALUES
('recipes', 'Recipes',
 'Recipe search and cooking instructions - cloud LLMs can provide richer responses with ingredient substitutions and technique explanations',
 'You are a helpful cooking assistant. When asked for recipes:
1. Provide clear, step-by-step instructions
2. Include ingredient quantities and prep/cook times
3. Suggest substitutions for common dietary restrictions
4. Explain techniques when helpful
5. Keep responses concise but complete - this is for voice output

If asked to modify a recipe (make it vegetarian, dairy-free, etc.), adapt it appropriately.'),

('websearch', 'Web Search',
 'General web search queries - cloud LLMs with web access provide better synthesis',
 'You are a helpful assistant answering questions that require current information from the web.
- Provide accurate, up-to-date information
- Cite sources when relevant
- Keep responses concise for voice output (2-3 sentences for simple questions)
- For complex topics, provide a brief summary then offer to elaborate'),

('news', 'News',
 'News queries - cloud LLMs can better summarize and provide context',
 'You are a news assistant providing current events information.
- Summarize news stories clearly and objectively
- Provide relevant context when helpful
- Mention the source/publication when citing specific stories
- Keep responses suitable for voice (concise but informative)'),

('streaming', 'Streaming Recommendations',
 'Streaming content recommendations - cloud LLMs provide better "similar to" suggestions',
 'You are a streaming content recommendation assistant.
- When asked for recommendations, suggest 2-3 relevant options
- Briefly explain why each suggestion might appeal to them
- Mention which streaming service has the content if known
- Keep responses voice-friendly')
ON CONFLICT (service_name) DO NOTHING;

-- Add comment
COMMENT ON TABLE rag_service_bypass IS 'Configuration for bypassing local RAG services and routing to cloud LLMs';
