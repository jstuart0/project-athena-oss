-- Migration 028b: Persistent Chat Sessions — athena_admin feature flag seed
-- Run against: athena_admin database.
-- Must run before admin UI is deployed so the feature card renders correctly.

INSERT INTO features (name, display_name, description, category, enabled, priority, config, required, requires_restart)
VALUES (
  'persistent_chat_sessions',
  'Persistent Chat Sessions',
  'Remember users and restore conversation history across browser sessions using anonymous cookies. Users can leave and return to continue where they left off. No login required — identity is tracked via an anonymous browser cookie.',
  'integration',
  FALSE,
  60,
  '{
    "session_ttl_days": 90,
    "max_restored_turns": 20,
    "cookie_name": "jarvis_uid",
    "cookie_domain": null,
    "cookie_secure": true,
    "show_restore_banner": true
  }'::jsonb,
  FALSE,
  FALSE
)
ON CONFLICT (name) DO NOTHING;
