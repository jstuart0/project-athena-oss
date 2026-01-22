-- Migration 012: Add process control method support
-- Description: Updates services on Mac Studio to use 'process' control method
--              instead of 'docker' since they run as bare Python processes
-- Date: 2025-12-02

-- ============================================================================
-- UPDATE CONTROL METHOD FOR MAC STUDIO SERVICES
-- ============================================================================
-- These services run as Python/uvicorn processes, not Docker containers

UPDATE athena_services
SET control_method = 'process',
    updated_at = CURRENT_TIMESTAMP
WHERE host = 'localhost'
  AND service_name IN (
      'gateway',
      'orchestrator',
      'weather-rag',
      'airports-rag',
      'flights-rag',
      'news-rag',
      'stocks-rag',
      'recipes-rag',
      'events-rag',
      'sports-rag',
      'streaming-rag',
      'dining-rag',
      'websearch-rag'
  );

-- Keep Ollama as 'launchd' since it's managed by brew services
-- Keep infrastructure services (qdrant, redis on Mac mini) as 'docker'
-- Keep control-agent as 'none' since it doesn't need to control itself

-- ============================================================================
-- VERIFICATION
-- ============================================================================

DO $$
DECLARE
    updated_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO updated_count
    FROM athena_services
    WHERE control_method = 'process';

    RAISE NOTICE 'Migration 012 completed successfully';
    RAISE NOTICE '  - Updated % services to process control method', updated_count;
END $$;
