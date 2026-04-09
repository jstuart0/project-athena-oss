-- Migration 028a: Persistent Chat Sessions — athena DB tables
-- Three-table model: identity → thread → exchange
-- Run against: athena database BEFORE deploying the new Jarvis backend.

CREATE TABLE IF NOT EXISTS web_browser_identities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cookie_id       VARCHAR(36) NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMP NOT NULL   -- renewed on each activity
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wbi_cookie
    ON web_browser_identities(cookie_id);
CREATE INDEX IF NOT EXISTS idx_wbi_expires
    ON web_browser_identities(expires_at);

CREATE TABLE IF NOT EXISTS web_chat_threads (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id             UUID NOT NULL REFERENCES web_browser_identities(id) ON DELETE CASCADE,
    started_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    last_active_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at                TIMESTAMP,           -- NULL = active thread; set by "Start fresh"
    turn_count              INTEGER NOT NULL DEFAULT 0,
    current_orch_session_id VARCHAR(100)          -- NULL when no live orchestrator session
);
CREATE INDEX IF NOT EXISTS idx_wct_identity_started
    ON web_chat_threads(identity_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_wct_active
    ON web_chat_threads(identity_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS web_chat_exchanges (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id         UUID NOT NULL REFERENCES web_chat_threads(id) ON DELETE CASCADE,
    turn_number       INTEGER NOT NULL,
    user_content      TEXT NOT NULL,
    assistant_content TEXT,           -- NULL if stream was interrupted before completion
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wce_thread_turn
    ON web_chat_exchanges(thread_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_wce_thread_desc
    ON web_chat_exchanges(thread_id, turn_number DESC);
