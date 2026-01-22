-- Migration: 008_guest_contact_fields.sql
-- Add guest contact fields and soft delete support to calendar_events table
-- Date: 2025-11-28

-- Add guest contact fields and soft delete support
ALTER TABLE calendar_events
ADD COLUMN IF NOT EXISTS guest_email VARCHAR(255),
ADD COLUMN IF NOT EXISTS guest_phone VARCHAR(50),
ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS created_by VARCHAR(50) DEFAULT 'ical_sync';

-- Add index for history queries (filter by soft delete)
CREATE INDEX IF NOT EXISTS idx_calendar_events_deleted_at ON calendar_events(deleted_at);

-- Add index for checkout queries (for history ordering)
CREATE INDEX IF NOT EXISTS idx_calendar_events_checkout ON calendar_events(checkout);

-- Add index for created_by queries (manual vs ical_sync filtering)
CREATE INDEX IF NOT EXISTS idx_calendar_events_created_by ON calendar_events(created_by);

-- Add comments for clarity
COMMENT ON COLUMN calendar_events.guest_email IS 'Guest email address (optional)';
COMMENT ON COLUMN calendar_events.guest_phone IS 'Guest phone number (optional)';
COMMENT ON COLUMN calendar_events.created_by IS 'Source: ical_sync or manual';
COMMENT ON COLUMN calendar_events.deleted_at IS 'Soft delete timestamp, NULL if active';

-- Update existing records to have created_by set to ical_sync if NULL
UPDATE calendar_events SET created_by = 'ical_sync' WHERE created_by IS NULL;
