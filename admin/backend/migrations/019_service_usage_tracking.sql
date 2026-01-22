-- Migration: 019_service_usage_tracking
-- Description: Add service usage tracking table for budget management (Bright Data, etc.)
-- Created: 2026-01-05

-- Service usage tracking table
-- Tracks monthly request counts per service for budget management
CREATE TABLE IF NOT EXISTS service_usage (
    id SERIAL PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    month VARCHAR(7) NOT NULL,  -- YYYY-MM format
    request_count INTEGER DEFAULT 0,
    monthly_limit INTEGER,  -- Optional limit (NULL = unlimited)
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(service_name, month)
);

-- Index for efficient lookups
CREATE INDEX IF NOT EXISTS ix_service_usage_service_month ON service_usage(service_name, month);

-- Function to auto-update last_updated timestamp
CREATE OR REPLACE FUNCTION update_service_usage_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to update timestamp on changes
DROP TRIGGER IF EXISTS trigger_service_usage_timestamp ON service_usage;
CREATE TRIGGER trigger_service_usage_timestamp
    BEFORE UPDATE ON service_usage
    FOR EACH ROW
    EXECUTE FUNCTION update_service_usage_timestamp();

-- Insert initial record for bright-data with 5000 monthly limit
INSERT INTO service_usage (service_name, month, request_count, monthly_limit)
VALUES ('bright-data', TO_CHAR(CURRENT_DATE, 'YYYY-MM'), 0, 5000)
ON CONFLICT (service_name, month) DO NOTHING;
