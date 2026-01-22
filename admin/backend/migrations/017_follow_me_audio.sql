-- Migration: 017_follow_me_audio.sql
-- Description: Add follow-me audio configuration table
-- Date: 2026-01-05

-- Follow-me audio configuration
CREATE TABLE IF NOT EXISTS follow_me_config (
    id SERIAL PRIMARY KEY,
    enabled BOOLEAN DEFAULT TRUE,
    mode VARCHAR(20) DEFAULT 'single' CHECK (mode IN ('off', 'single', 'party')),
    debounce_seconds FLOAT DEFAULT 5.0,
    grace_period_seconds FLOAT DEFAULT 30.0,
    min_motion_duration_seconds FLOAT DEFAULT 2.0,
    quiet_hours_start INTEGER DEFAULT 23, -- Hour in 24h format
    quiet_hours_end INTEGER DEFAULT 7,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Room motion sensor mapping
CREATE TABLE IF NOT EXISTS room_motion_sensors (
    id SERIAL PRIMARY KEY,
    room_name VARCHAR(100) NOT NULL UNIQUE,
    motion_entity_id VARCHAR(255) NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    priority INTEGER DEFAULT 0, -- Higher priority rooms preferred
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Rooms excluded from follow-me
CREATE TABLE IF NOT EXISTS follow_me_excluded_rooms (
    id SERIAL PRIMARY KEY,
    room_name VARCHAR(100) NOT NULL UNIQUE,
    reason VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Insert default config
INSERT INTO follow_me_config (enabled, mode, debounce_seconds, grace_period_seconds)
VALUES (TRUE, 'single', 5.0, 30.0)
ON CONFLICT DO NOTHING;

-- Insert default room motion mappings (based on common HA motion sensors)
INSERT INTO room_motion_sensors (room_name, motion_entity_id, priority) VALUES
    ('office', 'binary_sensor.office_motion', 10),
    ('living_room', 'binary_sensor.living_room_motion', 8),
    ('kitchen', 'binary_sensor.kitchen_motion', 7),
    ('master_bedroom', 'binary_sensor.master_bedroom_motion', 9),
    ('master_bathroom', 'binary_sensor.master_bathroom_motion', 5),
    ('dining_room', 'binary_sensor.dining_room_motion', 6)
ON CONFLICT (room_name) DO NOTHING;

-- Create trigger to update updated_at
CREATE OR REPLACE FUNCTION update_follow_me_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_follow_me_config_timestamp
    BEFORE UPDATE ON follow_me_config
    FOR EACH ROW
    EXECUTE FUNCTION update_follow_me_timestamp();

CREATE TRIGGER update_room_motion_sensors_timestamp
    BEFORE UPDATE ON room_motion_sensors
    FOR EACH ROW
    EXECUTE FUNCTION update_follow_me_timestamp();

-- Add comments
COMMENT ON TABLE follow_me_config IS 'Configuration for follow-me audio feature';
COMMENT ON TABLE room_motion_sensors IS 'Maps rooms to their motion sensor entities';
COMMENT ON TABLE follow_me_excluded_rooms IS 'Rooms excluded from follow-me audio transfers';
COMMENT ON COLUMN follow_me_config.mode IS 'off=disabled, single=follow one user, party=expand to all motion rooms';
COMMENT ON COLUMN room_motion_sensors.priority IS 'Higher priority rooms preferred when multiple have motion';
