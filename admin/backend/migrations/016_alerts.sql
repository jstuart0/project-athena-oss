-- Migration 016: System Alerts
-- Creates the alerts table for monitoring stuck sensors, service health, and system warnings

-- Create alerts table
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    alert_type VARCHAR(50) NOT NULL,  -- 'stuck_sensor', 'service_down', 'system_warning', etc.
    severity VARCHAR(20) NOT NULL DEFAULT 'warning',  -- 'info', 'warning', 'error', 'critical'
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    entity_id VARCHAR(255),  -- The Home Assistant entity_id if applicable
    entity_type VARCHAR(50),  -- 'sensor', 'light', 'switch', etc.
    alert_data JSONB DEFAULT '{}',  -- Additional metadata
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- 'active', 'acknowledged', 'resolved', 'dismissed'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    acknowledged_by_id INTEGER REFERENCES users(id),
    resolved_by_id INTEGER REFERENCES users(id),
    resolution_notes TEXT,
    dedup_key VARCHAR(255) UNIQUE  -- Prevents duplicate alerts for same issue
);

-- Create indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_alert_type ON alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_entity_id ON alerts(entity_id);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup_key ON alerts(dedup_key);

-- Add updated_at trigger
CREATE OR REPLACE FUNCTION update_alerts_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_alerts_updated_at ON alerts;
CREATE TRIGGER trigger_alerts_updated_at
    BEFORE UPDATE ON alerts
    FOR EACH ROW
    EXECUTE FUNCTION update_alerts_updated_at();

-- Insert migration record
INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('016', 'System alerts for monitoring stuck sensors and service health', NOW())
ON CONFLICT (version) DO NOTHING;
