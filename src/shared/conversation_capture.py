"""Conversation capture contract shared by the orchestrator (writer) and admin-backend.

One module owns the pieces both services must agree on, so they can't drift:

* the setting keys and the retention rules,
* the one database-URL builder (``resolve_database_dsn``),
* the text-safety rules (digit redaction and PIN withholding),
* the writer: ``CaptureSink`` (a bounded queue and one background worker) and
  ``AsyncpgCaptureStore`` (the single-statement SQL that writes a turn).

Stdlib only at import time; ``asyncpg`` is imported inside
``AsyncpgCaptureStore.open``. This module holds asyncpg-dialect SQL (``$n``
placeholders) only. The admin service's purge and status queries are
SQLAlchemy ``text()`` in ``app/services/conversation_capture.py``.

Rules that bind every line below:

* No SQL is built by formatting. Every statement is a constant; values travel
  as parameters, intervals through ``make_interval``.
* No captured text, DSN or exception message reaches a log. Logs carry the
  exception class name and counts.
* "Off means off": the analytics flag is checked inside the writing statement
  itself, so a turn queued while the flag was on is dropped if it is off at
  write time.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Setting keys and retention
# ---------------------------------------------------------------------------

ANALYTICS_KEY = "analytics_mode_enabled"
DEBUG_KEY = "conversation_debug_mode_enabled"
RETENTION_KEY = "conversation_retention_days"
WRITER_STATUS_KEY = "conversation_capture.writer_status"
PURGE_LEASE_KEY = "conversation_capture.purge.lock"
WRITER_STATUS_CATEGORY = "conversation_capture"

RETENTION_DEFAULT_DAYS = 30
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 3650

_RETENTION_RE = re.compile(r"[0-9]{1,4}")


def parse_stored_retention(value: Any) -> Optional[int]:
    """Strict reading of the stored retention setting.

    Returns the day count only for ``[0-9]{1,4}`` within 1-3650. Anything else
    is ``None``, and the purge treats ``None`` as "retention invalid": it
    deletes nothing. No code path turns a bad stored value into a number.
    """
    if not isinstance(value, str) or _RETENTION_RE.fullmatch(value) is None:
        return None
    days = int(value)
    if RETENTION_MIN_DAYS <= days <= RETENTION_MAX_DAYS:
        return days
    return None


def writer_status_key(replica: str) -> str:
    """The per-replica status row, so one healthy replica can't mask a failing one."""
    return f"{WRITER_STATUS_KEY}.{replica}"


def replica_id(hostname: str) -> str:
    """A short stable id for a replica; the hostname itself is never stored."""
    return hashlib.sha256(hostname.encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------

SOURCE_DATABASE_URL = "database_url"
SOURCE_ATHENA_DB = "athena_db"
SOURCE_NONE = "none"
SOURCE_UNSUPPORTED = "unsupported"

_PG_URL_RE = re.compile(r"^postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://", re.IGNORECASE)
_DEFAULT_PG_PORT = 5432


@dataclass(frozen=True)
class CaptureDsn:
    """A resolved Postgres DSN. ``repr`` never shows it."""

    dsn: Optional[str] = field(default=None, repr=False)
    sqlalchemy_url: Optional[str] = field(default=None, repr=False)
    source: str = SOURCE_NONE
    legacy_disagrees: bool = False

    @property
    def configured(self) -> bool:
        return self.dsn is not None

    def __reduce__(self):
        raise TypeError("CaptureDsn holds credentials and can't be pickled or copied")


def _splice_db_password(url: str, password: str) -> str:
    """Insert ``DB_PASSWORD`` into ``url`` (admin-backend's long-standing rule).

    ``postgresql://user@host`` becomes ``postgresql://user:password@host``. The
    password is URL-quoted, which leaves a plain alphanumeric password
    byte-for-byte as the inline code it replaced wrote it. The scheme,
    SQLAlchemy driver suffix included, is kept.
    """
    if not password or "@" not in url or "//" not in url:
        return url
    parts = url.split("@")
    scheme, _, rest = parts[0].partition("//")
    return f"{scheme}//{rest}:{quote(password, safe='')}@{'@'.join(parts[1:])}"


def _legacy_dsn(environ: Mapping[str, str]) -> Optional[str]:
    host = environ.get("ATHENA_DB_HOST", "")
    name = environ.get("ATHENA_DB_NAME", "")
    user = environ.get("ATHENA_DB_USER", "")
    if not (host and name and user):
        return None
    port = environ.get("ATHENA_DB_PORT", "") or str(_DEFAULT_PG_PORT)
    password = environ.get("ATHENA_DB_PASSWORD", "")
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    credentials = quote(user, safe="")
    if password:
        credentials += ":" + quote(password, safe="")
    return f"postgresql://{credentials}@{host_part}:{port}/{name}"


def _location(dsn: str) -> Optional[tuple]:
    try:
        parts = urlsplit(dsn)
        return ((parts.hostname or "").lower(), parts.port or _DEFAULT_PG_PORT, parts.path.lstrip("/"))
    except ValueError:
        return None


def resolve_database_dsn(database_url: Optional[str], environ: Mapping[str, str]) -> CaptureDsn:
    """The one place a Postgres DSN is built, for the writer and the reader alike.

    Precedence: ``database_url`` (``AthenaConfig.database_url``) first, with its
    scheme normalised to ``postgresql://``. ``ATHENA_DB_*`` is a fallback only
    when ``database_url`` is empty. A non-Postgres URL (sqlite included) is
    unsupported: no DSN. When both are set and point at different
    host/port/database, ``legacy_disagrees`` is true so the caller can log one
    warning (booleans only).
    """
    url = (database_url or "").strip()
    legacy = _legacy_dsn(environ)
    if not url:
        if legacy is None:
            return CaptureDsn(None, None, SOURCE_NONE)
        return CaptureDsn(legacy, legacy, SOURCE_ATHENA_DB)
    if not _PG_URL_RE.match(url):
        return CaptureDsn(None, None, SOURCE_UNSUPPORTED)

    url = _splice_db_password(url, environ.get("DB_PASSWORD", ""))
    dsn = _PG_URL_RE.sub("postgresql://", url, count=1)
    # SQLAlchemy keeps its driver suffix (postgresql+psycopg, ...); asyncpg's
    # DSN never has one.
    sqlalchemy_url = re.sub(r"^postgres(?:ql)?", "postgresql", url, count=1, flags=re.IGNORECASE)
    disagrees = False
    if legacy is not None:
        disagrees = _location(dsn) != _location(legacy)
    return CaptureDsn(dsn, sqlalchemy_url, SOURCE_DATABASE_URL, disagrees)


# ---------------------------------------------------------------------------
# Field limits
# ---------------------------------------------------------------------------

QUERY_MAX_CHARS = 8000
ANSWER_MAX_CHARS = 32000
ERROR_MAX_CHARS = 300
DETAIL_STRING_MAX_CHARS = 200
_MAX_TOOL_NAMES = 20

# VARCHAR widths of every column a client-influenced label can reach, so one
# long label can't turn a turn into a DataError drop.
FIELD_WIDTHS = {
    "session_id": 100,
    "request_id": 100,
    "room": 100,
    "user_mode": 20,
    "interface_type": 20,
    "intent": 100,
    "model_used": 100,
    "complexity": 50,
    "route": 32,
    "channel": 8,
    "audience_class": 16,
    "session_class": 16,
    "outcome": 16,
}

# Closed key set of the debug_detail JSONB. Anything else is dropped.
DEBUG_DETAIL_KEYS = frozenset(
    {
        "cache_hit",
        "validation_reason",
        "stream_completed",
        "answer_truncated",
        "was_truncated",
        "is_multi_intent",
        "is_novel_intent",
        "caller_trust_tier",
        "degraded",
        "stay_read_only",
        "owner_proven",
        "guest_verified",
        "public",
        "prompt_tokens",
    }
)

OUTCOME_WITHHELD = "withheld"

# ---------------------------------------------------------------------------
# Digit redaction and PIN withholding
# ---------------------------------------------------------------------------

REDACTION_TOKEN = "[number]"
_DIGIT_RUN_RE = re.compile(r"\d(?:[\s,.\-/_]*\d){3,}")


def redact_digits(text: Optional[str]) -> Optional[str]:
    """Replace any run of 4 or more digits (one separator allowed between digits)."""
    if not text:
        return text
    return _DIGIT_RUN_RE.sub(REDACTION_TOKEN, text)


# Mirrors the word table inside mode_permission.extract_pin_from_query
# (homophones included). tests/unit/test_conversation_capture_contract.py
# fails if the two drift.
SPOKEN_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "won": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3",
    "four": "4", "for": "4", "fore": "4",
    "five": "5",
    "six": "6", "sicks": "6", "sex": "6",
    "seven": "7",
    "eight": "8", "ate": "8",
    "nine": "9", "niner": "9",
}

WITHHOLD_OWNER_MODE = "owner_mode"
WITHHOLD_PIN_CONTEXT = "pin_context"
WITHHOLD_DIGIT_RUN = "digit_run"
WITHHOLD_SPOKEN_DIGITS = "spoken_digits"

_NUMBER_WORD = r"(?:\d|\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|niner)\b)"
_PIN_WORD = r"\b(?:pin|passcode|pass\s*code|password|code)\b"
_PIN_CONTEXT_RE = re.compile(
    rf"(?:{_PIN_WORD}.{{0,30}}?{_NUMBER_WORD}|{_NUMBER_WORD}.{{0,30}}?{_PIN_WORD})",
    re.IGNORECASE | re.DOTALL,
)
_TOKEN_RE = re.compile(r"\d+|[a-z]+")
_SEPARATOR_RE = re.compile(r"[\s,.\-/_]*")
_WITHHOLD_DIGITS = 6
_WITHHOLD_SPOKEN_WORDS = 4


# Homophones that are ordinary words far more often than digits. They count as a
# spoken digit only when next to another spoken-digit word ("one two three
# four", "two to six seven"), never alone or next to numerals ("weather for
# 2026", "set it to 72", "at 5 to 6").
WEAK_DIGIT_WORDS = frozenset({"to", "for", "too", "won", "ate"})

_NUMERAL, _WORD, _WEAK = "numeral", "word", "weak"


def _contiguous_runs(low: str) -> List[List[tuple]]:
    """Runs of (kind, digit_count) tokens joined only by separators or "and"."""
    runs: List[List[tuple]] = []
    current: List[tuple] = []
    prev_end = 0
    just_and = False
    for match in _TOKEN_RE.finditer(low):
        token = match.group()
        joined = bool(current) and _SEPARATOR_RE.fullmatch(low[prev_end:match.start()]) is not None
        prev_end = match.end()
        if token == "and" and joined and not just_and:
            just_and = True
            continue
        if token.isdigit():
            item = (_NUMERAL, len(token))
        elif token in WEAK_DIGIT_WORDS:
            item = (_WEAK, 1)
        elif token in SPOKEN_DIGIT_WORDS:
            item = (_WORD, 1)
        else:
            if current:
                runs.append(current)
            current, just_and = [], False
            continue
        if not joined and current:
            runs.append(current)
            current = []
        current.append(item)
        just_and = False
    if current:
        runs.append(current)
    return runs


def _split_unqualified_weak(run: List[tuple]) -> List[List[tuple]]:
    """Drop each weak homophone with no spoken-digit word beside it, splitting the run there."""
    pieces: List[List[tuple]] = [[]]
    for i, (kind, count) in enumerate(run):
        if kind == _WEAK:
            beside = [run[j][0] for j in (i - 1, i + 1) if 0 <= j < len(run)]
            if _WORD not in beside and _WEAK not in beside:
                pieces.append([])
                continue
        pieces[-1].append((kind, count))
    return [p for p in pieces if p]


def _digit_run_reason(query: str) -> Optional[str]:
    """One scanner over digit groups and spoken-digit words.

    Separators ``[\\s,.\\-/_]`` (and the word "and") between tokens don't break a
    run. A run is withheld at 6 digits in total, or at 4 in total when at
    least one of them is a spoken word. Weak homophones count only beside
    another spoken-digit word.
    """
    for run in _contiguous_runs(query.lower()):
        for piece in _split_unqualified_weak(run):
            total = sum(count for _, count in piece)
            if total >= _WITHHOLD_DIGITS:
                return WITHHOLD_DIGIT_RUN
            if total >= _WITHHOLD_SPOKEN_WORDS and any(kind != _NUMERAL for kind, _ in piece):
                return WITHHOLD_SPOKEN_DIGITS
    return None


def withhold_reason(query: Optional[str], owner_command: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """Why a whole turn must not be stored, or ``None``.

    (a) PIN intent: ``owner_command`` (the orchestrator's
    ``detect_owner_mode_command``, injected so ``shared`` never imports the
    orchestrator), or pin/passcode/code/password near a digit or number word;
    (b) and (c) a digit run of 6 or more, or 4 or more spoken-digit words.
    A predicate that raises withholds (fail closed).
    """
    if not query:
        return None
    if owner_command is not None:
        try:
            if owner_command(query):
                return WITHHOLD_OWNER_MODE
        except Exception:
            return WITHHOLD_OWNER_MODE
    if _PIN_CONTEXT_RE.search(query):
        return WITHHOLD_PIN_CONTEXT
    return _digit_run_reason(query)


# ---------------------------------------------------------------------------
# The record handed to the sink, and its sanitiser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureRecord:
    """One answered turn, in the shape the writer statement takes.

    Base fields are stored whenever Analytics Mode is on. The debug-only
    block is written only if the debug flag is on at write time (the SQL
    nulls it otherwise). No identity is carried: no guest id or name, speaker
    name, device id or fingerprint.
    """

    session_id: str
    query_text: str
    route: str
    channel: str
    audience_class: str
    session_class: str
    response_text: Optional[str] = None
    outcome: str = "answered"
    text_withheld: bool = False
    request_id: Optional[str] = None
    room: Optional[str] = None
    user_mode: Optional[str] = None
    interface_type: Optional[str] = None
    intent: Optional[str] = None
    confidence: Optional[float] = None
    is_fallback: Optional[bool] = None
    response_time_ms: Optional[int] = None
    # debug-only
    model_used: Optional[str] = None
    rag_tools_used: Optional[List[str]] = None
    tokens_generated: Optional[int] = None
    tokens_per_second: Optional[float] = None
    validation_passed: Optional[bool] = None
    error_message: Optional[str] = None
    complexity: Optional[str] = None
    base_knowledge_populated: Optional[bool] = None
    node_timings_ms: Optional[Dict[str, float]] = None
    debug_detail: Optional[Dict[str, Any]] = None


def _strip_nul(value: Optional[str]) -> Optional[str]:
    return value.replace("\x00", "") if isinstance(value, str) else value


def _clamp(value: Any, width: int) -> Optional[str]:
    if value is None:
        return None
    return _strip_nul(str(value))[:width]


def _clean_text(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    # Redaction can lengthen a short digit run, so the cap applies again after it.
    return redact_digits(_strip_nul(str(value))[:limit])[:limit]


def _clean_detail(detail: Optional[Mapping[str, Any]], extra: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    for key, value in list(dict(detail or {}).items()) + list(extra.items()):
        if key not in DEBUG_DETAIL_KEYS:
            continue
        if isinstance(value, str):
            merged[key] = _clean_text(value, DETAIL_STRING_MAX_CHARS)
        elif isinstance(value, (bool, int, float)) or value is None:
            merged[key] = value
    return merged or None


# Closed vocabularies for the fields that are no longer behind the debug flag:
# anything else (a payload key, a free-form data source) is dropped, so these
# columns can never carry text. tests/unit/test_conversation_capture_contract.py
# pins CAPTURE_TOOL_NAMES to rag_tools.py and CAPTURE_NODE_NAMES to the graph.
CAPTURE_RETRIEVE_SERVICES = frozenset({"weather", "onecall", "airports", "events", "sports", "websearch"})
CAPTURE_RAG_TOOL_NAMES = frozenset(
    {
        "get_weather",
        "get_sports_scores",
        "get_sports_standings",
        "get_airport_info",
        "search_flights",
        "search_events",
        "search_streaming",
        "get_news",
        "get_stock_info",
        "search_web",
        "search_restaurants",
        "search_recipes",
        "search_transit",
        "get_directions",
        "get_train_schedule",
        "scrape_website",
        "scrape_webpage_bright",
        "compare_prices",
        "get_tesla_metrics",
        "request_media",
    }
)
CAPTURE_TOOL_NAMES = CAPTURE_RAG_TOOL_NAMES | CAPTURE_RETRIEVE_SERVICES
CAPTURE_GRAPH_NODE_NAMES = frozenset(
    {
        "classify",
        "route_control",
        "route_music",
        "route_tv",
        "route_info",
        "retrieve",
        "synthesize",
        "validate",
        "tool_call",
        "send_sms",
        "notification_pref",
        "intent_refused",
        "finalize",
    }
)
CAPTURE_NODE_NAMES = CAPTURE_GRAPH_NODE_NAMES | {"tool_selection", "post_synthesis_fallback"}
_MODEL_NAME_RE = re.compile(r"^[\w.:/@-]{1,100}$")


def _clean_model(model: Optional[str]) -> Optional[str]:
    if not isinstance(model, str):
        return None
    model = _strip_nul(model).strip()
    return model if _MODEL_NAME_RE.fullmatch(model) else None


def _clean_tools(tools: Optional[List[str]]) -> Optional[List[str]]:
    if not tools:
        return None
    kept = [t for t in list(tools) if isinstance(t, str) and t in CAPTURE_TOOL_NAMES]
    return list(dict.fromkeys(kept))[:_MAX_TOOL_NAMES] or None


def _clean_timings(timings: Optional[Mapping[str, Any]]) -> Optional[Dict[str, float]]:
    if not timings:
        return None
    cleaned = {
        k: float(v)
        for k, v in timings.items()
        if isinstance(k, str)
        and k in CAPTURE_NODE_NAMES
        and isinstance(v, (int, float))
        and not isinstance(v, bool)
    }
    return cleaned or None


def sanitize_record(record: CaptureRecord, *, owner_command: Optional[Callable[[str], bool]] = None) -> CaptureRecord:
    """The last gate before storage. Idempotent; the writer always applies it.

    * A turn that is withheld, or whose query trips ``withhold_reason``, keeps
      no text at all: ``query_text=''``, no answer, no error, no debug detail,
      ``outcome='withheld'``, ``text_withheld=True``.
    * Otherwise query, answer, error and every ``debug_detail`` string are
      length-capped and digit-redacted; ``debug_detail`` keys outside
      ``DEBUG_DETAIL_KEYS`` are dropped.
    * Every VARCHAR-bound label is clamped to its column width; NUL bytes
      (which Postgres text rejects) are removed.
    """
    labels = {name: _clamp(getattr(record, name), width) for name, width in FIELD_WIDTHS.items() if name != "model_used"}
    reason = None if record.text_withheld else withhold_reason(record.query_text, owner_command)
    if record.text_withheld or reason is not None:
        labels.pop("outcome")
        return dataclasses.replace(
            record,
            **labels,
            query_text="",
            response_text=None,
            outcome=OUTCOME_WITHHELD,
            text_withheld=True,
            error_message=None,
            debug_detail=None,
            model_used=None,
            rag_tools_used=None,
            node_timings_ms=None,
        )

    query = _strip_nul(str(record.query_text or ""))
    answer = _strip_nul(record.response_text) if record.response_text is not None else None
    truncated = (answer is not None and len(answer) > ANSWER_MAX_CHARS) or len(query) > QUERY_MAX_CHARS
    return dataclasses.replace(
        record,
        **labels,
        query_text=_clean_text(query, QUERY_MAX_CHARS) or "",
        response_text=_clean_text(answer, ANSWER_MAX_CHARS),
        error_message=_clean_text(record.error_message, ERROR_MAX_CHARS),
        model_used=_clean_model(record.model_used),
        rag_tools_used=_clean_tools(record.rag_tools_used),
        node_timings_ms=_clean_timings(record.node_timings_ms),
        debug_detail=_clean_detail(record.debug_detail, {"answer_truncated": True} if truncated else {}),
    )


# ---------------------------------------------------------------------------
# SQL (asyncpg dialect). Constants only: nothing here is ever formatted.
# ---------------------------------------------------------------------------

FLAGS_READ_SQL = """
SELECT s.key AS key, lower(btrim(s.value)) = 'true' AS enabled
FROM system_settings s
WHERE s.key = ANY($1::text[])
"""

WRITER_STATUS_UPSERT_SQL = """
INSERT INTO system_settings (key, value, description, category)
VALUES ($1::varchar, $2::text,
        'Conversation capture writer status for one orchestrator replica.',
        'conversation_capture')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
"""

# One data-modifying statement per turn: the flag read, the conversation
# upsert and the turn insert share one snapshot and succeed or fail together.
#   f  the two flags, read from system_settings in this same statement
#   c  the conversation upsert, only while Analytics Mode is on; on a
#      session_class mismatch nothing is returned and no turn is written.
#      A legacy row with a NULL class is adopted and stamped. Bumping
#      ended_at takes the row lock the purge's empty-conversation sweep needs.
#   w  whole-turn withholding: the caller said so, or either of the
#      conversation's last two turns is a withheld marker from the last 5
#      minutes (the owner-mode utterance, then the PIN said in parts)
#   d  debug detail is written only while the debug flag is on and the turn
#      is not withheld
#   t  the turn insert; "source" is the capture level, decided here
WRITE_TURN_SQL = """
WITH f AS (
    SELECT
        COALESCE((SELECT lower(btrim(s.value)) = 'true'
                  FROM system_settings s WHERE s.key = $1::text), false) AS analytics,
        COALESCE((SELECT lower(btrim(s.value)) = 'true'
                  FROM system_settings s WHERE s.key = $2::text), false) AS debug
),
c AS (
    INSERT INTO conversations
        (id, session_id, room, user_mode, interface_type, session_class,
         started_at, ended_at, turn_count, total_tokens)
    SELECT $3::varchar, $4::varchar, $5::varchar, $6::varchar, $7::varchar, $8::varchar,
           NOW(), NOW(), 1, CASE WHEN f.debug THEN $9::integer END
    FROM f
    WHERE f.analytics
    ON CONFLICT (session_id) DO UPDATE SET
        ended_at = NOW(),
        turn_count = conversations.turn_count + 1,
        total_tokens = CASE WHEN EXCLUDED.total_tokens IS NULL THEN conversations.total_tokens
                            ELSE COALESCE(conversations.total_tokens, 0) + EXCLUDED.total_tokens END,
        session_class = COALESCE(conversations.session_class, EXCLUDED.session_class)
    WHERE conversations.session_class IS NULL
       OR conversations.session_class IS NOT DISTINCT FROM EXCLUDED.session_class
    RETURNING id, turn_count
),
w AS (
    SELECT ($10::boolean OR EXISTS (
        SELECT 1
        FROM (
            SELECT p.outcome, p.created_at
            FROM conversation_turns p
            WHERE p.conversation_id = c.id
            ORDER BY p.turn_number DESC, p.created_at DESC
            LIMIT 2
        ) AS last_two
        WHERE last_two.outcome = 'withheld'
          AND last_two.created_at > NOW() - make_interval(mins => 5)
    )) AS withheld
    FROM c
),
d AS (
    SELECT (f.debug AND NOT w.withheld) AS on_
    FROM f, w
),
t AS (
    INSERT INTO conversation_turns (
        id, conversation_id, turn_number, request_id,
        query_text, response_text, intent, confidence,
        model_used, rag_tools_used, response_time_ms,
        tokens_generated, tokens_per_second,
        validation_passed, error_message, source,
        complexity, is_fallback, base_knowledge_populated, node_timings_ms,
        route, channel, audience_class, outcome, text_withheld, debug_detail,
        created_at
    )
    SELECT
        $11::varchar, c.id, c.turn_count, $12::varchar,
        CASE WHEN w.withheld THEN '' ELSE $13::text END,
        CASE WHEN w.withheld THEN NULL ELSE $14::text END,
        $15::varchar,
        $16::double precision::numeric,
        CASE WHEN NOT w.withheld THEN $17::varchar END,
        CASE WHEN NOT w.withheld THEN $18::jsonb END,
        $19::integer,
        CASE WHEN d.on_ THEN $9::integer END,
        CASE WHEN d.on_ THEN $20::double precision::numeric END,
        CASE WHEN d.on_ THEN $21::boolean END,
        CASE WHEN d.on_ THEN $22::text END,
        CASE WHEN f.debug THEN 'debug' ELSE 'analytics' END,
        CASE WHEN d.on_ THEN $23::varchar END,
        $24::boolean,
        CASE WHEN d.on_ THEN $25::boolean END,
        CASE WHEN NOT w.withheld THEN $26::jsonb END,
        $27::varchar, $28::varchar, $29::varchar,
        CASE WHEN w.withheld THEN 'withheld' ELSE $30::varchar END,
        w.withheld,
        CASE WHEN d.on_ THEN $31::jsonb END,
        NOW()
    FROM c, f, w, d
    RETURNING id
)
SELECT (SELECT analytics FROM f) AS analytics,
       (SELECT count(*) FROM c) AS conversations,
       (SELECT count(*) FROM t) AS turns
"""

RESULT_WRITTEN = "written"
RESULT_DISABLED = "disabled"
RESULT_PARTITION_MISMATCH = "partition_mismatch"
RESULT_FAILED = "failed"
RESULT_QUEUE_FULL = "dropped_queue_full"
RESULT_UNCONFIGURED = "unconfigured"
RESULT_SHUTTING_DOWN = "shutting_down"
RESULT_QUEUED = "queued"


def _json_or_none(value: Any) -> Optional[str]:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else None


def write_turn_params(record: CaptureRecord, *, conversation_id: str, turn_id: str) -> tuple:
    """Positional parameters for ``WRITE_TURN_SQL`` (the order is the SQL's)."""
    return (
        ANALYTICS_KEY,                                  # $1
        DEBUG_KEY,                                      # $2
        conversation_id,                                # $3
        record.session_id,                              # $4
        record.room,                                    # $5
        record.user_mode,                               # $6
        record.interface_type,                          # $7
        record.session_class,                           # $8
        record.tokens_generated,                        # $9
        bool(record.text_withheld),                     # $10
        turn_id,                                        # $11
        record.request_id,                              # $12
        record.query_text,                              # $13
        record.response_text,                           # $14
        record.intent,                                  # $15
        record.confidence,                              # $16
        record.model_used,                              # $17
        _json_or_none(record.rag_tools_used),           # $18
        record.response_time_ms,                        # $19
        record.tokens_per_second,                       # $20
        record.validation_passed,                       # $21
        record.error_message,                           # $22
        record.complexity,                              # $23
        record.is_fallback,                             # $24
        record.base_knowledge_populated,                # $25
        _json_or_none(record.node_timings_ms),          # $26
        record.route,                                   # $27
        record.channel,                                 # $28
        record.audience_class,                          # $29
        record.outcome,                                 # $30
        _json_or_none(record.debug_detail),             # $31
    )


# ---------------------------------------------------------------------------
# Store port and the asyncpg adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureFlags:
    analytics: bool = False
    debug: bool = False


class CaptureStore(Protocol):
    async def read_flags(self) -> CaptureFlags: ...

    async def write(self, record: CaptureRecord) -> str:
        """Store one sanitised record; returns a ``RESULT_*`` string."""
        ...

    async def report(self, status: Mapping[str, Any]) -> None: ...

    async def close(self) -> None: ...


_POOL_MAX_SIZE = 2
_POOL_COMMAND_TIMEOUT_S = 5
_POOL_CONNECT_TIMEOUT_S = 5
_POOL_CLOSE_TIMEOUT_S = 1.0


class AsyncpgCaptureStore:
    """Writes turns to the install's own database through a small asyncpg pool.

    The pool is created on first use (``min_size=0``), so an unreachable
    database costs the first write, never startup. Errors that escape carry no
    DSN and no row values; callers log the class name only.
    """

    def __init__(self, dsn: str, *, replica: str):
        self._dsn = dsn
        self._replica = replica
        self._pool: Any = None
        self._lock: Optional[asyncio.Lock] = None
        self._closed = False

    def __repr__(self) -> str:
        return f"AsyncpgCaptureStore(replica={self._replica!r})"

    async def open(self) -> Any:
        if self._closed:
            raise RuntimeError("capture store is closed")
        if self._pool is not None:
            return self._pool
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._closed:
                raise RuntimeError("capture store is closed")
            if self._pool is None:
                import asyncpg

                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=0,
                    max_size=_POOL_MAX_SIZE,
                    command_timeout=_POOL_COMMAND_TIMEOUT_S,
                    timeout=_POOL_CONNECT_TIMEOUT_S,
                )
        return self._pool

    async def read_flags(self) -> CaptureFlags:
        pool = await self.open()
        rows = await pool.fetch(FLAGS_READ_SQL, [ANALYTICS_KEY, DEBUG_KEY])
        enabled = {row["key"]: bool(row["enabled"]) for row in rows}
        return CaptureFlags(analytics=enabled.get(ANALYTICS_KEY, False), debug=enabled.get(DEBUG_KEY, False))

    async def write(self, record: CaptureRecord) -> str:
        import uuid

        pool = await self.open()
        params = write_turn_params(record, conversation_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()))
        row = await pool.fetchrow(WRITE_TURN_SQL, *params)
        if not row["analytics"]:
            return RESULT_DISABLED
        if not row["conversations"]:
            return RESULT_PARTITION_MISMATCH
        return RESULT_WRITTEN if row["turns"] else RESULT_FAILED

    async def report(self, status: Mapping[str, Any]) -> None:
        pool = await self.open()
        await pool.execute(
            WRITER_STATUS_UPSERT_SQL,
            writer_status_key(self._replica),
            json.dumps(dict(status), sort_keys=True),
        )

    async def close(self) -> None:
        self._closed = True
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), _POOL_CLOSE_TIMEOUT_S)
        except BaseException:
            pool.terminate()


# ---------------------------------------------------------------------------
# The sink: a bounded queue and one background worker
# ---------------------------------------------------------------------------

FLAG_TTL_S = 30.0
QUEUE_MAX = 256
REPORT_INTERVAL_S = 60.0
DRAIN_S = 2.0
_FLAG_READ_TIMEOUT_S = 5.0
_ERROR_LOG_INTERVAL_S = 60.0
_TASK_STOP_TIMEOUT_S = 1.0
_FINAL_REPORT_TIMEOUT_S = 1.0
_STICKY_TURNS = 2
_STICKY_WINDOW_S = 300.0
_STICKY_MAX_SESSIONS = 1024

STATE_UNCONFIGURED = "unconfigured"
STATE_STARTING = "starting"
STATE_OK = "ok"
STATE_FAILING = "failing"
STATE_STOPPED = "stopped"


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


class CaptureSink:
    """Accepts finished turns without blocking and stores them in the background.

    ``enqueue`` is synchronous and does no I/O: it checks a cached copy of the
    flags (refreshed from the database every ``flag_ttl`` seconds; unknown means
    off) and does ``put_nowait``. A full queue drops the newest turn and counts
    it. The authoritative flag check is in the writing statement itself.
    Must be started and stopped from the event loop that owns it.
    """

    def __init__(
        self,
        store: Optional[CaptureStore],
        *,
        flag_ttl: float = FLAG_TTL_S,
        queue_max: int = QUEUE_MAX,
        report_interval: float = REPORT_INTERVAL_S,
        on_result: Optional[Callable[[str], None]] = None,
        owner_command: Optional[Callable[[str], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._store = store
        self._owner_command = owner_command
        self._sticky: "Dict[str, tuple]" = {}
        self._flag_ttl = flag_ttl
        self._queue_max = queue_max
        self._report_interval = report_interval
        self._on_result = on_result
        self._clock = clock
        self._queue: Optional["asyncio.Queue[CaptureRecord]"] = None
        self._flags: Optional[CaptureFlags] = None
        self._tasks: List["asyncio.Task[None]"] = []
        self._inflight = False
        self._stopping = False
        self._dropped = 0
        self._last_ok_at: Optional[datetime] = None
        self._last_error_at: Optional[datetime] = None
        self._last_error_class: Optional[str] = None
        self._last_error_source: Optional[str] = None
        self._last_logged_class: Optional[str] = None
        self._last_logged_at = float("-inf")
        self._last_report = float("-inf")
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the workers on the running loop. Idempotent."""
        if self._store is None or self._started:
            return
        self._started = True
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._flag_loop()), loop.create_task(self._write_loop())]

    async def stop(self, drain_s: float = DRAIN_S) -> None:
        """Stop accepting, drain for ``drain_s``, count what's left as dropped."""
        if not self._started or self._stopping:
            return
        self._stopping = True
        queue = self._queue
        assert queue is not None
        try:
            await asyncio.wait_for(queue.join(), drain_s)
        except asyncio.TimeoutError:
            pass
        self._dropped += queue.qsize() + (1 if self._inflight else 0)
        for task in self._tasks:
            task.cancel()
        # Bounded, not gather(): on Python 3.11 a cancel that lands as an inner
        # asyncio.wait_for completes can be swallowed, leaving the loop running. The
        # loops also test ``_stopping``, so such a task exits at its next iteration.
        await asyncio.wait(self._tasks, timeout=_TASK_STOP_TIMEOUT_S)
        self._tasks = []
        # One last status report, so the admin page sees the final dropped count rather than
        # the one from up to a minute ago (best effort, time-boxed).
        if self._flags is not None and self._flags.analytics:
            try:
                await asyncio.wait_for(self._store.report(self._report_payload()), _FINAL_REPORT_TIMEOUT_S)  # type: ignore[union-attr]
            except BaseException as exc:
                self._log_failure("conversation_capture_final_report_failed", exc, force=True)
        try:
            await asyncio.wait_for(self._store.close(), _POOL_CLOSE_TIMEOUT_S + 0.5)  # type: ignore[union-attr]
        except BaseException as exc:
            self._log_failure("conversation_capture_close_failed", exc, force=True)

    # -- request path (sync, no I/O) --------------------------------------

    def enqueue(self, record: CaptureRecord) -> str:
        """Hand over one turn; returns the ``RESULT_*`` outcome. Never raises."""
        try:
            if self._stopping:
                # A turn that finishes while the service is stopping is lost, and counted as such.
                self._dropped += 1
                return self._emit(RESULT_SHUTTING_DOWN)
            if self._store is None or not self._started:
                return self._emit(RESULT_UNCONFIGURED)
            flags = self._flags
            if flags is None or not flags.analytics:
                return self._emit(RESULT_DISABLED)
            assert self._queue is not None
            record = self._carry_withholding(record)
            try:
                self._queue.put_nowait(record)
            except asyncio.QueueFull:
                self._dropped += 1
                return self._emit(RESULT_QUEUE_FULL)
            return RESULT_QUEUED
        except Exception:
            return RESULT_FAILED

    def _carry_withholding(self, record: CaptureRecord) -> CaptureRecord:
        """In-process twin of the writer's sticky rule (belt and braces).

        A withheld turn arms its session: the next ``_STICKY_TURNS`` turns of
        that session inside ``_STICKY_WINDOW_S`` are marked withheld before they
        leave the request. The writer's SQL rule still decides across replicas.
        """
        now = self._clock()
        session = record.session_id
        armed = self._sticky.get(session)
        if record.text_withheld or record.outcome == OUTCOME_WITHHELD:
            self._arm(session, now)
            return record
        if armed is not None:
            left, until = armed
            if now <= until and left > 0:
                self._sticky[session] = (left - 1, until)
                return dataclasses.replace(record, text_withheld=True)
            del self._sticky[session]
        return record

    def _arm(self, session: str, now: float) -> None:
        if len(self._sticky) >= _STICKY_MAX_SESSIONS:
            self._sticky.pop(next(iter(self._sticky)))
        self._sticky[session] = (_STICKY_TURNS, now + _STICKY_WINDOW_S)

    def analytics_enabled(self) -> bool:
        """The cached Analytics Mode flag: False when unconfigured, not started, or unknown."""
        flags = self._flags
        return bool(self._store is not None and self._started and not self._stopping and flags is not None and flags.analytics)

    def _emit(self, result: str) -> str:
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:
                pass
        return result

    # -- status ------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Counts and states only: no text, no DSN."""
        if self._store is None:
            state = STATE_UNCONFIGURED
        elif self._stopping:
            state = STATE_STOPPED
        elif self._last_error_at and (not self._last_ok_at or self._last_error_at > self._last_ok_at):
            state = STATE_FAILING
        elif self._last_ok_at or self._flags is not None:
            state = STATE_OK
        else:
            state = STATE_STARTING
        return {
            "configured": self._store is not None,
            "writer_state": state,
            "dropped": self._dropped,
            "last_ok_at": _iso(self._last_ok_at),
        }

    def _report_payload(self) -> Dict[str, Any]:
        return {
            "last_ok_at": _iso(self._last_ok_at),
            "last_error_at": _iso(self._last_error_at),
            "last_error_class": self._last_error_class,
            "dropped": self._dropped,
        }

    # -- workers -----------------------------------------------------------

    def _record_error(self, exc: BaseException, source: str) -> None:
        self._last_error_at = datetime.now(timezone.utc)
        self._last_error_class = type(exc).__name__
        self._last_error_source = source

    def _record_ok(self) -> None:
        self._last_ok_at = datetime.now(timezone.utc)

    def _log_failure(self, event: str, exc: BaseException, *, force: bool = False) -> None:
        name = type(exc).__name__
        now = self._clock()
        if force or name != self._last_logged_class or now - self._last_logged_at >= _ERROR_LOG_INTERVAL_S:
            self._last_logged_class = name
            self._last_logged_at = now
            logger.warning(event, extra={"error_type": name, "dropped": self._dropped})

    async def _flag_loop(self) -> None:
        store = self._store
        assert store is not None
        while not self._stopping:
            try:
                self._flags = await asyncio.wait_for(store.read_flags(), _FLAG_READ_TIMEOUT_S)
                self._clear_error_on_flag_success()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._flags = None
                self._record_error(exc, "flags")
                self._log_failure("conversation_capture_flags_failed", exc)
            await self._maybe_report()
            await asyncio.sleep(self._flag_ttl)

    def _clear_error_on_flag_success(self) -> None:
        """A flag read that works again clears only a flag-read failure.

        A write failure stays until a write succeeds: a reachable database
        with a statement that keeps failing must keep reading as failing.
        """
        if self._last_error_source == "flags":
            self._record_ok()

    async def _maybe_report(self) -> None:
        flags = self._flags
        now = self._clock()
        if flags is None or not flags.analytics or now - self._last_report < self._report_interval:
            return
        self._last_report = now
        try:
            await asyncio.wait_for(self._store.report(self._report_payload()), _FLAG_READ_TIMEOUT_S)  # type: ignore[union-attr]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_failure("conversation_capture_report_failed", exc)

    async def _write_loop(self) -> None:
        store, queue = self._store, self._queue
        assert store is not None and queue is not None
        while not self._stopping or not queue.empty():
            record = await queue.get()
            self._inflight = True
            try:
                result = await store.write(sanitize_record(record, owner_command=self._owner_command))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._dropped += 1
                self._record_error(exc, "write")
                self._log_failure("conversation_capture_write_failed", exc)
                self._emit(RESULT_FAILED)
            else:
                if result == RESULT_WRITTEN:
                    self._record_ok()
                elif result == RESULT_PARTITION_MISMATCH:
                    self._dropped += 1
                self._emit(result)
            finally:
                self._inflight = False
                queue.task_done()
