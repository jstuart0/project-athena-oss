# Plan: Analytics Mode — Conversation Logging & Evaluation

**Status: SA review complete — all findings resolved or explicitly accepted**

---

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
  on 2026-01-10 only. Captures some query text inconsistently, not in a queryable form.
- Redis sessions: `session_id` per conversation, 1-hour TTL, used for context only.
- `system_settings` table and `SystemSetting` model already exist in the admin backend.
  `/api/settings` routes already exist.
- `ConversationAnalytics` model already exists — low-cardinality event telemetry (session
  creation, follow-up detection), not transcript storage. Separate purpose; no conflict.

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
3. **Best-effort capture, explicitly documented.** Analytics data is not a durable ledger. Process
   crash, pod restart, or OOM kill between response and write completion will lose that turn. This
   is an accepted trade-off (see SA Finding 3).
4. **Source-labeled at the turn level.** Every turn row carries a `source` field indicating why it
   was captured. Source lives on turns only, not on conversation rows (see SA Finding 4).
5. **Explicit RBAC.** Reading conversations requires `read:conversations` scope. Submitting
   evaluations requires `write:conversation_evaluations`. Enabling/disabling analytics requires
   `write:settings` (see SA Finding 6).
6. **Both repos get the full feature.** OSS has admin backend and frontend; both repos receive
   all changes (see SA Finding 5).

---

## SA Review Findings & Resolutions

### Finding 1 (HIGH — resolved): Privacy model under-specified

**Problem:** The initial draft stored raw `query_text` and `response_text` without defining PII
handling, retention, encryption expectations, access control, or consent messaging.

**Resolution:** See the explicit Privacy Policy section below. Summary:
- No automatic PII redaction in v1 (operator responsibility, documented)
- Retention: no automatic expiry in v1; a configurable retention window is future work (documented)
- Encryption-at-rest: relies on database-level encryption; no application-layer encryption added
- Access: `read:conversations` scope required; conversations are not exposed to viewer or guest roles
- Consent: analytics mode toggle in admin UI includes explicit description of what is stored

### Finding 2 (HIGH — resolved): system_settings and /api/settings already exist

**Problem:** The initial draft described creating a `system_settings` table and generic settings
routes that already exist.

**Resolution:** The plan now says:
- Add a single row to the existing `system_settings` table (`key='analytics_mode_enabled'`)
- Add the analytics toggle to the existing settings API — extend routes, not create new ones
- The `SystemSetting` SQLAlchemy model and `/api/settings` router already exist in the admin
  backend at `admin/backend/app/models.py:2420` and `admin/backend/app/routes/settings.py:21`

### Finding 3 (MEDIUM — accepted): Fire-and-forget is best-effort only

**Problem:** `asyncio.create_task()` after response return loses the turn on crash or restart.
Operators may assume completeness.

**Resolution:** Explicitly accepted as the right trade-off for this feature:
- Analytics mode is for trend analysis and quality review, not financial or audit compliance
- Loss rate in practice is very low (only in-flight tasks on shutdown/crash)
- A queue-backed durable capture would add significant infrastructure complexity for marginal gain
- The admin UI and feature description will explicitly state: "Analytics capture is best-effort.
  Turns in-flight at the time of a service restart may not be recorded."

### Finding 4 (MEDIUM — resolved): Source ambiguity at conversation level

**Problem:** If a session starts in debug mode and analytics mode is later enabled (or vice versa),
the `conversations` table could only represent one source cleanly.

**Resolution:** `source` is stored **on turn rows only**, not on conversation rows. This is the
correct model because turns are the atomic unit of capture, and each turn is captured by exactly
one mode. The `conversations` row has no `source` column. If a query is ever needed for
"conversations that have at least one debug-mode turn," it can be answered via a join on turns.

### Finding 5 (MEDIUM — resolved): OSS/private repo boundary was incorrect

**Problem:** The initial draft said "OSS gets orchestrator changes only; admin stays private-repo."
The OSS repo has `admin/backend/` and `admin/frontend/` directories.

**Resolution:** Both repos receive the full feature. All files changed are in both repos.

### Finding 6 (HIGH — resolved): Authorization boundaries not defined

**Problem:** Conversations store raw user text. "Admin auth required" is not sufficient — RBAC
scope must be explicit.

**Resolution:** New scopes required by this feature:
- `read:conversations` — read conversation list, turn detail, evaluation data
- `write:conversation_evaluations` — submit ratings and notes on turns
- `write:settings` — already exists; used to toggle analytics_mode_enabled

These scopes must be added to the `UserAPIKey` scope definitions in the admin backend. The
conversations API must enforce `read:conversations` on all GET endpoints and
`write:conversation_evaluations` on the evaluation POST endpoint. Viewer and guest roles do not
receive these scopes.

### Finding 7 (LOW — resolved): ConversationAnalytics reconciliation

**Problem:** `ConversationAnalytics` already exists in the admin backend. Without a decision,
dashboards and analytics APIs will diverge.

**Resolution:** Keep both; they serve different purposes and do not overlap:
- `ConversationAnalytics` (existing): low-cardinality event telemetry — session creation,
  follow-up detection, clarification triggers. Recorded unconditionally regardless of analytics
  mode. Not conversation content.
- `conversations` + `conversation_turns` (new): full transcript storage, captured only when
  analytics or debug mode is active. Used for conversation review and quality evaluation.

No migration or consolidation needed. The admin UI should keep any existing ConversationAnalytics
views separate from the new Conversations transcript view.

### Finding 8 (MEDIUM — resolved): evaluated_by as free-text is weak for auditability

**Problem:** A plain string username is not durable if usernames or auth providers change.

**Resolution:** `turn_evaluations` uses both:
- `evaluated_by_user_id` (INTEGER) — FK to the admin user record; durable across username changes
- `evaluated_by_username` (VARCHAR) — denormalized at write time for display without joins

### Finding 9 (LOW — resolved): Migration should assert query_logs is empty before drop

**Problem:** Assuming 0 rows without a runtime check is brittle in stale environments.

**Resolution:** The Alembic migration (`031_add_analytics_mode_tables.py`) asserts row count
before dropping. If `query_logs` has rows, the migration raises an exception with a clear message
rather than silently dropping data.

---

## Privacy Policy (explicit)

This section must be included in the admin UI feature description and referenced in any
operator-facing documentation.

**What is stored when analytics mode is on:**
- Full query text as typed or spoken by the user
- Full response text generated by Athena
- Metadata: intent, model used, latency, RAG tools, session ID, room, user mode, timestamp

**What is NOT stored:**
- IP addresses
- Browser fingerprints or user agents
- Any data beyond what the orchestrator already processes

**PII handling:**
- Athena does not perform automatic PII redaction. If users may submit personal information
  (names, addresses, health data, etc.), the operator is responsible for either:
  a) not enabling analytics mode for those interaction types, or
  b) implementing redaction at the application layer before enabling
- This is a v1 limitation; prompt-level or post-processing redaction is future work

**Retention:**
- No automatic expiry in v1. Rows persist until manually deleted.
- Future work: configurable retention window (e.g. 90 days) with a scheduled purge job

**Encryption:**
- Relies on database-level encryption. No application-layer encryption is added by this feature.
- Operators are responsible for ensuring their PostgreSQL deployment meets their encryption
  requirements (e.g. encrypted storage, TLS in transit)

**Access control:**
- Only admin users with `read:conversations` scope can read transcript data
- Viewer and guest roles cannot access conversation records

---

## Database Schema

### New Tables (in `athena` database)

#### `conversations`
One row per unique session. Created on the first turn, updated each subsequent turn.
No `source` column — source is on turns only.

```sql
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      VARCHAR(100) NOT NULL UNIQUE,
    room            VARCHAR(100),
    user_mode       VARCHAR(20),
    interface_type  VARCHAR(20),
    started_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMP,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversations_session  ON conversations(session_id);
CREATE INDEX idx_conversations_started  ON conversations(started_at DESC);
CREATE INDEX idx_conversations_room     ON conversations(room);
```

#### `conversation_turns`
One row per Q&A exchange. `source` lives here — not on `conversations`.

```sql
CREATE TABLE conversation_turns (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id       UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_number           INTEGER NOT NULL,
    request_id            VARCHAR(100),
    query_text            TEXT NOT NULL,
    response_text         TEXT,
    intent                VARCHAR(100),
    confidence            NUMERIC(4,3),
    model_used            VARCHAR(100),
    rag_tools_used        JSONB,
    response_time_ms      INTEGER,
    tokens_generated      INTEGER,
    tokens_per_second     NUMERIC(8,2),
    validation_passed     BOOLEAN,
    error_message         TEXT,
    source                VARCHAR(20) NOT NULL,   -- 'analytics' | 'debug'
    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_turns_conversation ON conversation_turns(conversation_id);
CREATE INDEX idx_turns_created      ON conversation_turns(created_at DESC);
CREATE INDEX idx_turns_intent       ON conversation_turns(intent);
CREATE INDEX idx_turns_source       ON conversation_turns(source);
CREATE UNIQUE INDEX idx_turns_conv_number ON conversation_turns(conversation_id, turn_number);
```

#### `turn_evaluations`
`evaluated_by_user_id` is durable; `evaluated_by_username` is denormalized at write time.

```sql
CREATE TABLE turn_evaluations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id                 UUID NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    rating                  SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    notes                   TEXT,
    evaluated_by_user_id    INTEGER,              -- FK to admin user; durable
    evaluated_by_username   VARCHAR(100),         -- denormalized at write time for display
    created_at              TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_evaluations_turn    ON turn_evaluations(turn_id);
CREATE INDEX idx_evaluations_rating  ON turn_evaluations(rating);
CREATE INDEX idx_evaluations_created ON turn_evaluations(created_at DESC);
```

### Migration (`031_add_analytics_mode_tables.py`)

```python
def upgrade():
    # Assert query_logs is empty before dropping
    result = op.get_bind().execute(text("SELECT COUNT(*) FROM query_logs"))
    count = result.scalar()
    if count > 0:
        raise Exception(
            f"query_logs has {count} rows — cannot drop. Migrate data manually first."
        )

    # Create new tables
    op.create_table('conversations', ...)
    op.create_table('conversation_turns', ...)
    op.create_table('turn_evaluations', ...)

    # Drop superseded table
    op.drop_table('query_logs')

    # Add analytics_mode_enabled to existing system_settings
    op.execute("""
        INSERT INTO system_settings (key, value, description, category)
        VALUES (
            'analytics_mode_enabled',
            'false',
            'When enabled, all conversations are persisted to the database for review and quality evaluation. Best-effort capture: turns in-flight at service restart may not be recorded.',
            'privacy'
        )
        ON CONFLICT (key) DO NOTHING
    """)
```

---

## Feature Flag

**Use the existing `system_settings` table and `SystemSetting` model.**

- Key: `analytics_mode_enabled`, value: `'true'` | `'false'`, category: `'privacy'`
- Inserted by migration (see above); no schema changes needed to admin backend
- Existing `/api/settings` routes extended to expose this key in the `privacy` category
- `write:settings` scope required to update (already enforced by existing route guards)

**Orchestrator reads the flag:**
- Fetched at startup and cached with 60-second TTL (same pattern as component config)
- On fetch failure: default to `false` (safe — no data loss, just no analytics)
- No restart required; toggle takes effect within ~60 seconds

---

## Orchestrator Changes

### Source determination

```python
def _get_analytics_source() -> Optional[str]:
    """Returns the capture source label, or None if no capture should happen."""
    if os.getenv("ATHENA_DEBUG_MODE", "false").lower() == "true":
        return "debug"
    # analytics_mode_enabled checked via cached config
    return None  # caller checks analytics config separately

async def _should_record_analytics(source: Optional[str]) -> bool:
    if source == "debug":
        return True
    config = await get_cached_system_setting("analytics_mode_enabled")
    return config == "true"
```

### The write function

Non-blocking, non-fatal, fire-and-forget via `asyncio.create_task()`. Loss on crash is accepted.

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
    try:
        async with get_analytics_db_connection() as conn:
            conv_row = await conn.fetchrow("""
                INSERT INTO conversations
                    (session_id, room, user_mode, interface_type, started_at, ended_at, turn_count)
                VALUES ($1, $2, $3, $4, NOW(), NOW(), 1)
                ON CONFLICT (session_id) DO UPDATE
                    SET ended_at   = NOW(),
                        turn_count = conversations.turn_count + 1,
                        total_tokens = COALESCE(conversations.total_tokens, 0) + COALESCE($5, 0)
                RETURNING id, turn_count
            """, session_id, room, user_mode, interface_type, tokens_generated)

            await conn.execute("""
                INSERT INTO conversation_turns (
                    conversation_id, turn_number, request_id,
                    query_text, response_text, intent, confidence,
                    model_used, rag_tools_used, response_time_ms,
                    tokens_generated, tokens_per_second,
                    validation_passed, error_message, source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """, conv_row["id"], conv_row["turn_count"], request_id,
                query, response, intent, confidence,
                model_used, json.dumps(rag_tools_used) if rag_tools_used else None,
                response_time_ms, tokens_generated, tokens_per_second,
                validation_passed, error_message, source)

    except Exception as e:
        logger.warning("analytics_write_failed",
                       error=str(e), session_id=session_id, request_id=request_id)
```

### Hook points in main.py

At the end of `/query` and `/query/stream` handlers, after session persistence, before response:

```python
source = _get_analytics_source()
if await _should_record_analytics(source):
    asyncio.create_task(_record_conversation_turn(
        session_id=session.session_id,
        source=source or "analytics",
        # ... all other fields from state / computed values
    ))
```

**Streaming path note:** Only write when `stream_completed=True`. Partial streams (client
disconnect) are not recorded — consistent with session persistence policy from GAP 7 of the
real-streaming plan.

---

## Admin Backend Changes

### RBAC — new scopes (add to existing scope definitions)

```python
# In admin/backend/app/utils/api_keys.py or wherever scopes are defined
"read:conversations"            # read conversation list, turns, evaluations
"write:conversation_evaluations"  # submit ratings and notes
# write:settings already exists
```

### Extend existing settings route

In `admin/backend/app/routes/settings.py` — add a `GET /api/settings/privacy` endpoint (or
expose the `privacy` category through the existing generic settings API) so the analytics mode
toggle is accessible from the frontend without new infrastructure.

### New conversations route (`admin/backend/app/routes/conversations.py`)

**Conversations:**
- `GET /api/conversations` — paginated list; requires `read:conversations`
  - Filters: `source`, `room`, `user_mode`, `from`, `to`, `intent`, `page`, `per_page`
  - Returns: id, session_id, room, user_mode, interface_type, started_at, ended_at,
    turn_count, first_query (first 120 chars of turn 1 query_text)
- `GET /api/conversations/{id}` — full thread with all turns and evaluations; requires
  `read:conversations`
- `GET /api/conversations/stats` — aggregate stats; requires `read:conversations`
  - total conversations, total turns, avg turns/conversation, breakdown by source/intent/room

**Evaluations:**
- `POST /api/conversations/turns/{turn_id}/evaluate` — requires `write:conversation_evaluations`
  - Body: `{ "rating": 1-5, "notes": "optional" }`
  - Upserts (one evaluation per admin user per turn)
  - Records `evaluated_by_user_id` from JWT, denormalizes `evaluated_by_username` at write time

---

## Admin Frontend Changes

### New "Conversations" page

**List view** — sortable, filterable by source label, date range, room, intent:

```
┌──────────────────────────────────────────────────────────────────┐
│ Conversations                     [Source ▼] [Room ▼] [Date ▼]  │
│ Best-effort capture — turns in-flight at restart may be missing  │
├──────────────────────────────────────────────────────────────────┤
│ Apr 4 10:32 AM · office · owner · analytics · 4 turns · 3m 12s  │
│ "What's the weather like tomo..."                                 │
├──────────────────────────────────────────────────────────────────┤
│ Apr 4 09:15 AM · web · guest · analytics · 2 turns · 1m 04s     │
│ "Tell me about Jay's background..."                              │
└──────────────────────────────────────────────────────────────────┘
```

**Detail view:**

```
┌──────────────────────────────────────────────────────────────────┐
│ ← Conversations   office · Apr 4 2026 10:32 AM · analytics      │
│ 4 turns · 3m 12s                                                 │
├──────────────────────────────────────────────────────────────────┤
│ Turn 1                                         WEATHER · 0.94   │
│ Q: What's the weather like tomorrow?                             │
│ A: Tomorrow in Baltimore looks mostly cloudy with a high...      │
│ ⏱ 1.2s · 87 tok · 72 tok/s · llama3.1:8b · [weather]          │
│ ★★★★☆  "Good but didn't mention wind"     ✓ Evaluated           │
├──────────────────────────────────────────────────────────────────┤
│ Turn 2                                         WEATHER · 0.81   │
│ Q: What about the weekend?                                       │
│ A: This weekend is looking better — Saturday should be...        │
│ ⏱ 0.9s · 64 tok · 71 tok/s · llama3.1:8b · [weather]          │
│ [ Rate: ★☆☆☆☆ ★★☆☆☆ ★★★☆☆ ★★★★☆ ★★★★★ ] [Notes...] [Save]   │
└──────────────────────────────────────────────────────────────────┘
```

### Settings page — analytics toggle

Add to the existing Settings page under a "Privacy" section:

```
Analytics Mode
──────────────────────────────────────────────────────────────────
When enabled, all conversations are persisted to the database.
Use the Conversations screen to review responses and submit ratings.

Privacy note: Full query and response text is stored. No automatic
PII redaction. Analytics capture is best-effort — turns in-flight
at service restart may not be recorded.

Default: Off. Changing this does not affect how Athena responds.

[  OFF  ●────────]   Changes take effect within ~60 seconds.
```

---

## Files Changed

| File | Change | Both repos? |
|------|--------|-------------|
| `src/orchestrator/main.py` | Analytics write hook, `_record_conversation_turn`, `_should_record_analytics`, asyncpg pool | Yes |
| `src/orchestrator/requirements.txt` | Ensure `asyncpg` listed | Yes |
| `admin/backend/app/routes/conversations.py` | New — conversations + evaluations endpoints | Yes |
| `admin/backend/app/routes/settings.py` | Extend to expose `privacy` category settings | Yes |
| `admin/backend/app/models.py` | Add `Conversation`, `ConversationTurn`, `TurnEvaluation` models | Yes |
| `admin/backend/app/utils/api_keys.py` | Add `read:conversations`, `write:conversation_evaluations` scopes | Yes |
| `admin/backend/alembic/versions/031_add_analytics_mode_tables.py` | New migration | Yes |
| `admin/frontend/conversations.js` | New — conversations list + detail + evaluation UI | Yes |
| `admin/frontend/settings.js` | Add analytics mode toggle under Privacy section | Yes |

---

## Rollout Order

1. **Run migration** — creates 3 tables, asserts `query_logs` is empty, drops it, inserts
   `analytics_mode_enabled=false` row in `system_settings`
2. **Deploy admin backend** — conversations API + evaluation API + updated settings API;
   `analytics_mode_enabled` defaults to `false` so no data flows yet
3. **Deploy orchestrator** — write hook present but no-ops while flag is `false`
4. **Deploy admin frontend** — Conversations page + analytics toggle visible
5. **Enable analytics mode** in admin UI → rows begin appearing in Conversations page

Steps 1–4 are zero-risk. Step 5 is the activation decision.

---

## Known Limitations & Future Work

- **Best-effort capture:** Turns in-flight at process crash or restart are lost. Documented
  in the UI. A queue-backed durable capture path is future work.
- **No PII redaction:** Operator responsibility in v1. Application-layer redaction is future work.
- **No retention policy:** Rows persist indefinitely. Configurable retention window is future work.
- **Voice path not covered:** `/ha/conversation` and `/v1/chat/completions` (gateway) paths
  bypass `/query` and `/query/stream`. Analytics writes for those paths are deferred.
- **No full-text search:** Searching across query/response content requires `pg_trgm` or an
  external search index. Future work.
- **No data export:** CSV/JSON export for external analysis is future work.
- **Per-room or per-mode overrides:** Global toggle only in v1. Room-level control is future work.
- **Evaluation aggregation views:** Per-turn ratings are stored but no rollup dashboards exist
  (e.g. "average rating by intent"). Future work once sufficient data is collected.

---

## Verification

1. **Analytics off (default):** Send 10 queries. Confirm `SELECT COUNT(*) FROM conversations = 0`.
2. **Toggle on:** Enable in admin UI. Send 5 queries. Confirm 1 conversation row, 5 turn rows,
   `source='analytics'` on all turns.
3. **Multi-session:** Two browser tabs simultaneously. Send queries from each. Confirm two
   separate `conversations` rows, each with correct sequential `turn_number`.
4. **Debug mode:** Restart orchestrator with `ATHENA_DEBUG_MODE=true`, analytics toggle off.
   Send queries. Confirm rows appear with `source='debug'`.
5. **Both active:** Both debug mode and analytics toggle on. Confirm `source='debug'` wins
   (debug mode takes precedence in source determination).
6. **Write failure resilience:** Break DB connectivity. Send a query. Confirm response returns
   within normal latency. Confirm `analytics_write_failed` warning in logs. No exception to client.
7. **Partial stream:** Disconnect browser mid-stream. Confirm no `conversation_turns` row written.
8. **RBAC:** Request `GET /api/conversations` with a token missing `read:conversations`. Confirm 403.
9. **Evaluation:** Submit rating on a turn. Confirm row in `turn_evaluations` with correct
   `evaluated_by_user_id` and `evaluated_by_username`. Reload — evaluation persists.
10. **Toggle off:** Disable analytics mode. Send queries. Confirm no new rows. Existing rows remain.
11. **Migration guard:** Run migration against a DB where `query_logs` has rows. Confirm migration
    raises an exception rather than silently dropping data.
