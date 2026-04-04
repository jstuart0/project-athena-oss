# Plan: Analytics Mode — Conversation Logging & Evaluation

## Context

Athena is privacy-first by design. By default, conversations live only in Redis with a 1-hour TTL
and no query content is persisted anywhere. This is the correct default and must not change.

This plan adds an **opt-in analytics mode** — a global toggle in the admin UI that, when enabled,
additionally writes all conversations to PostgreSQL for review and quality evaluation. The feature
is designed for deployments (e.g. business use cases) where operators want to understand what users
are asking, review responses, and systematically improve the system over time.

**Nothing about the existing request flow changes.** The analytics write is a fire-and-forget side
effect that happens after the response is returned.

### What exists today

- `query_logs` table in the `athena` PostgreSQL database — schema exists, **0 rows**, nothing
  writes to it, superseded by this plan.
- `ATHENA_DEBUG_MODE=true` env var — writes structured logs to disk files. Was active on Mac Studio
  on 2026-01-10 only. Captures some query text but inconsistently, not in a queryable form.
- Redis sessions: `session_id` per conversation, 1-hour TTL, used for context only.

### Motivation

- Operators cannot currently answer: "How many conversations happened today?", "What are users
  asking about?", "Which responses were bad?"
- The `query_logs` table was the intended solution but was never wired up.
- A proper schema + admin UI turns this into a platform for continuous quality improvement:
  evaluate responses, build golden query sets, track regressions, understand usage patterns.

---

## Design Principles

1. **Default is privacy-first.** Analytics mode is off by default. Zero behavioral change when off.
2. **Additive only.** The analytics write is a non-blocking, non-fatal side effect. If it fails,
   the request still succeeds and a warning is logged.
3. **Source-labeled.** Every recorded entry carries a `source` field indicating why it was
   captured, enabling operators to distinguish data from different recording modes.
4. **Unified storage.** Both the new analytics mode and the existing debug mode write to the same
   schema and are visible in the same UI — just labeled differently.

---

## Source Labels

| Value | Trigger | Meaning |
|-------|---------|---------|
| `analytics` | Admin UI toggle is on | Analytics mode explicitly enabled by operator |
| `debug` | `ATHENA_DEBUG_MODE=true` on the service | Debug mode enabled at service level |

Both sources write identical data. The label exists so operators know which mode produced each
record. In a production deployment with analytics mode on and debug mode off, all rows will have
`source=analytics`. In a dev/testing environment, rows may have `source=debug`.

---

## Database Schema

### New Tables (in `athena` database)

#### `conversations`
One row per unique session. Created on the first turn of a new session, updated on each
subsequent turn.

```sql
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      VARCHAR(100) NOT NULL UNIQUE,
    room            VARCHAR(100),
    user_mode       VARCHAR(20),
    interface_type  VARCHAR(20),
    source          VARCHAR(20) NOT NULL,          -- 'analytics' | 'debug'
    started_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMP,                     -- updated on each turn; approximates last activity
    turn_count      INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversations_session   ON conversations(session_id);
CREATE INDEX idx_conversations_started   ON conversations(started_at DESC);
CREATE INDEX idx_conversations_room      ON conversations(room);
CREATE INDEX idx_conversations_source    ON conversations(source);
```

#### `conversation_turns`
One row per Q&A exchange. `turn_number` is the 1-based sequence within the conversation.

```sql
CREATE TABLE conversation_turns (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_number       INTEGER NOT NULL,
    request_id        VARCHAR(100),               -- orchestrator request_id for log correlation
    query_text        TEXT NOT NULL,
    response_text     TEXT,
    intent            VARCHAR(100),
    confidence        NUMERIC(4,3),
    model_used        VARCHAR(100),
    rag_tools_used    JSONB,                       -- list of tool names that fired, e.g. ["weather","sports"]
    response_time_ms  INTEGER,
    tokens_generated  INTEGER,
    tokens_per_second NUMERIC(8,2),
    validation_passed BOOLEAN,
    error_message     TEXT,
    source            VARCHAR(20) NOT NULL,        -- 'analytics' | 'debug'
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_turns_conversation  ON conversation_turns(conversation_id);
CREATE INDEX idx_turns_created       ON conversation_turns(created_at DESC);
CREATE INDEX idx_turns_intent        ON conversation_turns(intent);
CREATE INDEX idx_turns_source        ON conversation_turns(source);
CREATE UNIQUE INDEX idx_turns_conv_number ON conversation_turns(conversation_id, turn_number);
```

#### `turn_evaluations`
One row per human evaluation of a turn. A turn may be evaluated multiple times (e.g. by
different reviewers), but typically one evaluation per turn is expected.

```sql
CREATE TABLE turn_evaluations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id       UUID NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    rating        SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    notes         TEXT,
    evaluated_by  VARCHAR(100),                    -- admin username
    created_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_evaluations_turn     ON turn_evaluations(turn_id);
CREATE INDEX idx_evaluations_rating   ON turn_evaluations(rating);
CREATE INDEX idx_evaluations_created  ON turn_evaluations(created_at DESC);
```

### Migration

1. Apply the three `CREATE TABLE` statements above via a new Alembic migration
   (`031_add_analytics_mode_tables.py`).
2. Drop `query_logs` in the same migration — the table has 0 rows and is superseded.
3. The migration is safe to run on a live system (no existing data affected).

---

## Feature Flag

The analytics mode toggle is stored as a system-wide feature flag in the admin backend.

**Option A (recommended):** Add a `system_settings` table to the admin backend database
(`athena_admin`) if it doesn't already exist, with a row for `analytics_mode_enabled`.

```sql
-- In athena_admin database
CREATE TABLE IF NOT EXISTS system_settings (
    key         VARCHAR(100) PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

INSERT INTO system_settings (key, value, description)
VALUES ('analytics_mode_enabled', 'false', 'When true, all conversations are persisted to the database for review and quality evaluation.')
ON CONFLICT (key) DO NOTHING;
```

**Option B:** Reuse the existing component config mechanism (`get_component_config("analytics")`).
This requires less new infrastructure but mixes operational config with feature flags.

Recommendation: Option A. The `system_settings` table is a clean, general-purpose home for
admin-controlled toggles and will be reused by future features (e.g. the privacy mode discussed
in a prior session).

**Admin backend API:**
- `GET /api/settings` — returns all system settings
- `PATCH /api/settings/{key}` — updates a setting (admin auth required)

**Orchestrator reads the flag:**
- Fetched at startup and cached with a 60-second TTL (same pattern as component config)
- If the fetch fails, default to `false` (safe — no data loss, just no analytics)
- Re-checked periodically so toggling the admin UI takes effect within ~60 seconds without restart

---

## Orchestrator Changes

### Where writes happen

Both the `/query` endpoint (non-streaming) and `/query/stream` endpoint need a write hook.
The hook fires **after the response has been returned to the client**, as a non-blocking
`asyncio.create_task()`.

### Should-record logic

```python
async def _should_record_analytics() -> bool:
    """Returns True if this request should be written to the analytics DB."""
    # Debug mode: always record (controlled at service level)
    if os.getenv("ATHENA_DEBUG_MODE", "false").lower() == "true":
        return True
    # Analytics mode: check admin flag (cached, 60s TTL)
    config = await get_analytics_config()
    return config.get("enabled", False)

def _get_analytics_source() -> str:
    if os.getenv("ATHENA_DEBUG_MODE", "false").lower() == "true":
        return "debug"
    return "analytics"
```

### The write function

```python
async def _record_conversation_turn(
    session_id: str,
    request_id: str,
    query: str,
    response: str,
    intent: Optional[str],
    confidence: Optional[float],
    model_used: Optional[str],
    rag_tools_used: Optional[list],
    response_time_ms: Optional[int],
    tokens_generated: Optional[int],
    tokens_per_second: Optional[float],
    validation_passed: Optional[bool],
    error_message: Optional[str],
    room: Optional[str],
    user_mode: Optional[str],
    interface_type: Optional[str],
    source: str,
) -> None:
    """
    Upsert the conversation row and insert a new turn row.
    Non-fatal: logs warning on failure, never raises.
    """
    try:
        async with get_analytics_db_connection() as conn:
            # Upsert conversation (create if new session, update turn_count + ended_at if existing)
            conv_row = await conn.fetchrow("""
                INSERT INTO conversations (session_id, room, user_mode, interface_type, source, started_at, ended_at, turn_count)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), 1)
                ON CONFLICT (session_id) DO UPDATE
                    SET ended_at = NOW(),
                        turn_count = conversations.turn_count + 1,
                        total_tokens = COALESCE(conversations.total_tokens, 0) + COALESCE($6, 0)
                RETURNING id, turn_count
            """, session_id, room, user_mode, interface_type, source, tokens_generated)

            conversation_id = conv_row["id"]
            turn_number = conv_row["turn_count"]

            # Insert turn
            await conn.execute("""
                INSERT INTO conversation_turns (
                    conversation_id, turn_number, request_id,
                    query_text, response_text, intent, confidence,
                    model_used, rag_tools_used, response_time_ms,
                    tokens_generated, tokens_per_second,
                    validation_passed, error_message, source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """, conversation_id, turn_number, request_id,
                query, response, intent, confidence,
                model_used, json.dumps(rag_tools_used) if rag_tools_used else None,
                response_time_ms, tokens_generated, tokens_per_second,
                validation_passed, error_message, source)

    except Exception as e:
        logger.warning("analytics_write_failed", error=str(e), session_id=session_id, request_id=request_id)
```

### Integration points in main.py

At the end of the `/query` handler (after `session_manager.add_message`):
```python
if await _should_record_analytics():
    asyncio.create_task(_record_conversation_turn(
        session_id=session.session_id,
        request_id=request_id,
        query=request.query,
        response=state.answer,
        intent=state.intent.value if state.intent else None,
        confidence=state.confidence,
        model_used=...,          # from state or llm_router
        rag_tools_used=list(state.retrieved_data.keys()) if state.retrieved_data else [],
        response_time_ms=int(processing_time * 1000),
        tokens_generated=...,
        tokens_per_second=...,
        validation_passed=state.validation_passed,
        error_message=state.error,
        room=request.room,
        user_mode=request.mode,
        interface_type=request.interface_type,
        source=_get_analytics_source(),
    ))
```

Same pattern at the end of the `/query/stream` handler, using the values already computed
(`full_answer`, `token_count`, `tokens_per_second`, `llm_time`, etc.).

### Database connection

Use `asyncpg` (already available in the Python environment). Connection pool initialized at
startup alongside the existing Redis/Qdrant connections. Target the `athena` database on
`postgres-01.xmojo.net`.

---

## Admin Backend Changes

### New API endpoints (in `admin/backend/app/routes/`)

**Settings:**
- `GET /api/settings` — list all system settings (key, value, description, updated_at)
- `PATCH /api/settings/{key}` — update a setting value (requires `write:settings` scope)

**Conversations:**
- `GET /api/conversations` — paginated list with filters:
  - `?page=1&per_page=25`
  - `?source=analytics|debug`
  - `?room=office`
  - `?user_mode=owner|guest`
  - `?from=2026-04-01&to=2026-04-04`
  - Returns: id, session_id, room, user_mode, interface_type, source, started_at, ended_at,
    turn_count, first_query (preview)
- `GET /api/conversations/{id}` — full conversation with all turns and any evaluations
- `GET /api/conversations/stats` — summary stats: total conversations, total turns, avg turns
  per conversation, breakdown by source/room/intent

**Evaluations:**
- `POST /api/conversations/turns/{turn_id}/evaluate` — create or update evaluation
  - Body: `{ "rating": 1-5, "notes": "optional" }`
- `GET /api/conversations/turns/{turn_id}/evaluations` — list evaluations for a turn

---

## Admin Frontend Changes

### New "Conversations" page (`/conversations`)

**List view:**
```
┌────────────────────────────────────────────────────────────────────┐
│ Conversations                          [Filter ▼]  [Source: All ▼] │
├────────────────────────────────────────────────────────────────────┤
│ Apr 4 2026 10:32 AM  │ office  │ owner  │ analytics │ 4 turns │ 3m │
│ "What's the weather like tomo..."                                   │
├────────────────────────────────────────────────────────────────────┤
│ Apr 4 2026 9:15 AM   │ web     │ guest  │ analytics │ 2 turns │ 1m │
│ "Tell me about Jay's background..."                                 │
└────────────────────────────────────────────────────────────────────┘
```

**Detail view (click a conversation):**
```
┌─────────────────────────────────────────────────────────────┐
│ Conversation — office — Apr 4, 2026 10:32 AM               │
│ 4 turns · 3m 12s · analytics                               │
├─────────────────────────────────────────────────────────────┤
│ Turn 1                                      WEATHER · 0.94  │
│ Q: What's the weather like tomorrow?                        │
│ A: Tomorrow in Baltimore looks mostly cloudy with a high... │
│ ⏱ 1.2s · 87 tokens · llama3.1:8b · [weather]             │
│ [★★★★★] [Add note...]                      [Evaluate]      │
├─────────────────────────────────────────────────────────────┤
│ Turn 2                                      GENERAL · 0.81  │
│ Q: What about the weekend?                                  │
│ A: This weekend is looking better — Saturday should be...  │
│ ⏱ 0.9s · 64 tokens · llama3.1:8b · [weather]             │
│ [★★★☆☆] Note: "Follow-up handled correctly"   ✓ Evaluated  │
└─────────────────────────────────────────────────────────────┘
```

### Settings page addition

Add to the existing Settings/Features section:

```
Analytics Mode
─────────────────────────────────────────────────────
When enabled, all conversations are persisted to the
database. Use the Conversations screen to review and
evaluate responses.

Default: Off. Turning this on does not change how
Athena responds — it only adds logging.

[  OFF  ●────────]   Saved automatically
```

---

## Files Changed

| Repo | File | Change |
|------|------|--------|
| Both | `src/orchestrator/main.py` | Add `_should_record_analytics()`, `_get_analytics_source()`, `_record_conversation_turn()`, asyncpg pool init, hook into `/query` and `/query/stream` |
| Both | `src/orchestrator/requirements.txt` | Ensure `asyncpg` is listed |
| Private | `admin/backend/app/routes/conversations.py` | New — conversations + evaluations endpoints |
| Private | `admin/backend/app/routes/settings.py` | New (or extend existing) — system settings endpoints |
| Private | `admin/backend/app/models.py` | Add Conversation, ConversationTurn, TurnEvaluation models |
| Private | `admin/backend/alembic/versions/031_add_analytics_mode_tables.py` | Migration: create 3 new tables, drop query_logs |
| Private | `admin/frontend/conversations.js` | New — conversations list + detail page |
| Private | `admin/frontend/settings.js` | Add analytics mode toggle |

OSS repo receives the orchestrator changes only. Admin backend/frontend are private-repo-only.

---

## Rollout Order

1. **Run migration** (`031_add_analytics_mode_tables.py`) — creates tables, drops `query_logs`
2. **Deploy admin backend** — settings API + conversations API, analytics mode defaults to `false`
3. **Deploy orchestrator** — write hook present but no-ops while flag is `false`
4. **Deploy admin frontend** — Conversations page + toggle visible
5. **Enable analytics mode** via admin UI → rows start appearing in Conversations page

Steps 1–4 are zero-risk (nothing new fires while flag is off). Step 5 is the activation.

---

## Known Limitations & Future Work

### Not in scope for this change
- **Per-room or per-user-mode analytics override** — global toggle only. Room-level control is
  future work.
- **Data export** — CSV/JSON export of conversations for external analysis. Future work.
- **Conversation search** — full-text search across query/response content. Future work (requires
  pg_trgm or a search index).
- **Streaming turn content in debug mode** — ATHENA_DEBUG_MODE currently writes logs to disk
  files. This plan adds DB writes alongside that, but does not remove the file logging behavior.
- **Retention policy** — no automatic cleanup of old analytics rows. Future work: a configurable
  retention window (e.g. keep 90 days) with a scheduled purge job.
- **Evaluation aggregation** — the Conversations UI shows per-turn ratings but no rollup views
  (e.g. "average rating by intent" dashboard). Future work after sufficient data is collected.

### Known gap: partial streams
The streaming path already has a `stream_completed` flag. When `stream_completed=False`
(client disconnected mid-stream), the user message is persisted to Redis but the assistant
response is not (see streaming plan GAP 7). The analytics write should follow the same policy:
only write a turn row when `stream_completed=True`. Partial turns are not recorded.

### Known gap: voice path
The `/ha/conversation` (Home Assistant voice) path and `/v1/chat/completions` (gateway) path
do not go through `/query` or `/query/stream` directly — they proxy through. Analytics writes
for those paths require the same hook to be added wherever those endpoints finalize their
response. Deferred to a follow-up.

---

## Verification

1. **Analytics off (default):** Send 10 queries. Confirm `SELECT COUNT(*) FROM conversations` = 0.
2. **Toggle on:** Enable in admin UI. Send 5 queries. Confirm 1 conversation row and 5 turn rows.
3. **Multi-session:** Open two browser tabs simultaneously. Send queries from each. Confirm two
   separate conversation rows in the DB, each with correct turn sequences.
4. **Debug mode:** Restart orchestrator with `ATHENA_DEBUG_MODE=true`. Send queries. Confirm
   rows appear with `source=debug` regardless of admin toggle state.
5. **Write failure resilience:** Temporarily break DB connectivity. Send a query. Confirm response
   still returned to client within normal latency. Confirm `analytics_write_failed` log warning.
6. **Admin UI list:** Conversations appear in reverse-chronological order. Filters work.
7. **Admin UI detail:** Full Q&A thread renders correctly. Metadata (intent, model, latency,
   RAG tools) is populated for each turn.
8. **Evaluation:** Submit a rating on a turn. Confirm row in `turn_evaluations`. Reload page —
   evaluation persists and renders correctly.
9. **Partial stream:** Disconnect browser mid-stream. Confirm no turn row written for that exchange.
10. **Toggle off:** Disable analytics mode. Send queries. Confirm no new rows written. Existing
    rows remain.
