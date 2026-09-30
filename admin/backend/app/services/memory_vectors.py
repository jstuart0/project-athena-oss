"""The memory vector store: the one module that talks to Qdrant and the
embedder for household memories.

Postgres is the source of truth. This module keeps the ``athena_memories``
collection in a state where it can serve semantic recall for Postgres rows,
and reports honestly when it can't:

- It creates the collection when it's absent (marking live rows ``pending``
  first, so a collection lost at runtime is rebuilt by the pending pass), and
  validates the shape and the recorded embedding model of an existing one. It
  never deletes a collection and never mutates one except to record the model
  on an unrecorded collection.
- Probes run off the request path. A request uses the cached state and only
  probes on first use or after a not-found; the background tick revalidates.
  The state lock never covers network I/O.
- Every embed runs under one process-wide lock, on text truncated to
  ``EMBED_MAX_CHARS``, in batches of ``EMBED_BATCH``, with a single-threaded
  offline model. That bounds the embedder's peak memory.

Callers pass scope names and get ``(point_id, score)`` pairs back; no
qdrant-client type crosses this interface.
"""
from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import structlog

from app.utils.url_validators import redact_url_userinfo

logger = structlog.get_logger()

COLLECTION_NAME = "athena_memories"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
DISTANCE = "Cosine"
PAYLOAD_SCHEMA = 1
EMBED_BATCH = 16
EMBED_MAX_CHARS = 4000
ROW_BATCH = 64
NOT_READY_REPROBE_SECONDS = 30
READY_RECHECK_SECONDS = 300
PRUNE_MIN_AGE_SECONDS = 600
FUTURE_SKEW_SECONDS = 60
LEASE_KEY = "memory_vectors.reindex.lock"
LEASE_TTL_SECONDS = 300
COOLDOWN_SECONDS = 60
PENDING_PASS_MAX_ROWS = 500
PENDING_PASS_INTERVAL_SECONDS = 600
SEARCH_OVERFETCH = 3
SEARCH_FETCH_CAP = 30
FOREIGN_SAMPLE = 10

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = os.getenv("QDRANT_PORT", "6333")
QDRANT_URL = os.getenv("QDRANT_URL", f"http://{QDRANT_HOST}:{QDRANT_PORT}")

READY = "ready"
UNAVAILABLE = "unavailable"
EMBEDDER_UNAVAILABLE = "embedder_unavailable"
SHAPE_MISMATCH = "shape_mismatch"
MODEL_MISMATCH = "model_mismatch"

_TICK_INTERVAL_SECONDS = 10
_DETAIL_MAX_CHARS = 300


@dataclass(frozen=True)
class VectorStoreState:
    status: str
    detail: str = ""
    collection_model: Optional[str] = None
    model_recorded: bool = False
    foreign_points: int = 0
    foreign_point_ids_sample: Tuple[str, ...] = ()


class VectorStoreNotReady(Exception):
    def __init__(self, state: VectorStoreState):
        super().__init__(state.status)
        self.state = state


class EmbeddingUnavailable(Exception):
    pass


class _SystemClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


# Module state. Tests replace these through the set_*_for_tests seams.
_client = None
_client_lock = threading.Lock()
_embedder = None
_embed_override: Optional[Callable[[List[str]], List[List[float]]]] = None
_EMBED_LOCK = threading.Lock()
_embedder_error: Optional[str] = None
_clock = _SystemClock()
_session_factory = None
_background_enabled = True
_task: Optional[asyncio.Task] = None

_state_lock = threading.Lock()
_state: Optional[VectorStoreState] = None
_checked_at: Optional[float] = None
_probe_inflight = False
_adoption_logged = False
_prev_status: Optional[str] = None
_last_pending_pass: Optional[float] = None


def _redact(text: str) -> str:
    text = str(text)
    if QDRANT_URL and QDRANT_URL in text:
        text = text.replace(QDRANT_URL, redact_url_userinfo(QDRANT_URL))
    return text[:_DETAIL_MAX_CHARS]


def _error_text(exc: BaseException) -> str:
    return _redact(str(exc) or type(exc).__name__)


def collection_metadata() -> Dict[str, Any]:
    return {
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
        "distance": DISTANCE,
        "payload_schema": PAYLOAD_SCHEMA,
    }


def _get_session_factory():
    if _session_factory is not None:
        return _session_factory
    from app.database import SessionLocal

    return SessionLocal


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            from qdrant_client import QdrantClient

            _client = QdrantClient(url=QDRANT_URL, timeout=10, check_compatibility=False)
            logger.info("qdrant_client_initialized", url=redact_url_userinfo(QDRANT_URL))
        return _client


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _set_state(state: VectorStoreState) -> None:
    global _state, _checked_at
    with _state_lock:
        previous = _state
        _state = state
        _checked_at = _clock.monotonic()
    if previous is None or previous.status != state.status:
        log = logger.info if state.status == READY else logger.warning
        log("memory_vector_store_state", status=state.status, detail=state.detail,
            model_recorded=state.model_recorded, foreign_points=state.foreign_points)


def _invalidate() -> None:
    global _state, _checked_at
    with _state_lock:
        _state = None
        _checked_at = None


def _mark_unavailable(exc: BaseException) -> None:
    _set_state(VectorStoreState(UNAVAILABLE, detail=_error_text(exc)))


def _collection_state() -> VectorStoreState:
    """The cached collection state; probes (single-flight) only when there
    is none. A caller that finds a probe in flight gets ``unavailable``
    immediately rather than waiting on the network."""
    global _probe_inflight
    with _state_lock:
        if _state is not None:
            return _state
        if _probe_inflight:
            return VectorStoreState(UNAVAILABLE, detail="vector store check in progress")
        _probe_inflight = True
    try:
        return refresh_state()
    finally:
        with _state_lock:
            _probe_inflight = False


def get_state() -> VectorStoreState:
    """Never raises. The embedder's health overlays a ready collection."""
    try:
        state = _collection_state()
    except Exception as exc:  # pragma: no cover - refresh_state already catches
        state = VectorStoreState(UNAVAILABLE, detail=_error_text(exc))
    error = _embedder_error
    if state.status == READY and error is not None:
        return replace(state, status=EMBEDDER_UNAVAILABLE, detail=error)
    return state


def refresh_state() -> VectorStoreState:
    """The probe: ensure the collection exists, then validate it."""
    try:
        state = _ensure(_get_client())
    except Exception as exc:
        state = VectorStoreState(UNAVAILABLE, detail=_error_text(exc))
    _set_state(state)
    return state


def _vector_params():
    from qdrant_client.models import Distance, VectorParams

    return VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE)


def _mark_live_rows_pending() -> int:
    """Before creating a collection: every live row that claimed a stored
    vector no longer has one."""
    from app.models import Memory

    session = _get_session_factory()()
    try:
        count = (
            session.query(Memory)
            .filter(Memory.is_deleted == False, Memory.vector_status == "stored")  # noqa: E712
            .update({"vector_status": "pending"}, synchronize_session=False)
        )
        session.commit()
        return count
    finally:
        session.close()


def _create_collection(client) -> None:
    try:
        client.create_collection(COLLECTION_NAME, vectors_config=_vector_params(), metadata=collection_metadata())
        logger.info("memory_vector_collection_created", collection=COLLECTION_NAME)
        return
    except Exception as exc:
        if client.collection_exists(COLLECTION_NAME):
            logger.info("memory_vector_collection_created_elsewhere", collection=COLLECTION_NAME)
            return
        first_error = exc
    try:
        client.create_collection(COLLECTION_NAME, vectors_config=_vector_params())
        logger.warning("memory_vector_collection_created_without_metadata", collection=COLLECTION_NAME,
                       error=_error_text(first_error))
    except Exception:
        if client.collection_exists(COLLECTION_NAME):
            return
        raise


def _foreign_filter():
    from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue, PayloadField

    return Filter(must_not=[
        FieldCondition(key="embedding_model", match=MatchValue(value=EMBEDDING_MODEL)),
        IsEmptyCondition(is_empty=PayloadField(key="embedding_model")),
    ])


def _foreign_points(client) -> Tuple[int, Tuple[str, ...]]:
    flt = _foreign_filter()
    count = client.count(COLLECTION_NAME, count_filter=flt, exact=True).count
    if not count:
        return 0, ()
    points, _ = client.scroll(COLLECTION_NAME, scroll_filter=flt, limit=FOREIGN_SAMPLE,
                              with_payload=False, with_vectors=False)
    return count, tuple(str(p.id) for p in points)


def _recorded_model(info) -> Optional[str]:
    metadata = getattr(info.config, "metadata", None) or {}
    return metadata.get("embedding_model") if isinstance(metadata, dict) else None


def _ensure(client) -> VectorStoreState:
    global _adoption_logged
    if not client.collection_exists(COLLECTION_NAME):
        try:
            marked = _mark_live_rows_pending()
        except Exception as exc:
            logger.error("memory_vector_mark_pending_failed", error=_error_text(exc))
            return VectorStoreState(UNAVAILABLE, detail="could not mark memories pending before creating the collection")
        if marked:
            logger.warning("memory_vectors_marked_pending", rows=marked, reason="collection_absent")
        _create_collection(client)

    info = client.get_collection(COLLECTION_NAME)
    vectors = info.config.params.vectors
    size = getattr(vectors, "size", None)
    distance = getattr(getattr(vectors, "distance", None), "value", getattr(vectors, "distance", None))
    if size != EMBEDDING_DIM or distance != DISTANCE:
        detail = f"collection has size={size} distance={distance}; expected size={EMBEDDING_DIM} distance={DISTANCE}"
        logger.error("memory_vector_collection_shape_mismatch", collection=COLLECTION_NAME, detail=detail)
        return VectorStoreState(SHAPE_MISMATCH, detail=detail)

    recorded = _recorded_model(info)
    foreign, sample = _foreign_points(client)
    if recorded is not None and recorded != EMBEDDING_MODEL:
        return VectorStoreState(MODEL_MISMATCH, detail=f"collection records embedding model {recorded}",
                                collection_model=recorded, model_recorded=True,
                                foreign_points=foreign, foreign_point_ids_sample=sample)
    if foreign:
        return VectorStoreState(MODEL_MISMATCH, detail=f"{foreign} points were embedded with another model",
                                collection_model=recorded, model_recorded=recorded is not None,
                                foreign_points=foreign, foreign_point_ids_sample=sample)
    if recorded is not None:
        return VectorStoreState(READY, collection_model=recorded, model_recorded=True)

    try:
        client.update_collection(COLLECTION_NAME, metadata=collection_metadata())
    except Exception as exc:
        logger.info("memory_vector_metadata_update_rejected", error=_error_text(exc))
    recorded = _recorded_model(client.get_collection(COLLECTION_NAME))
    if recorded == EMBEDDING_MODEL:
        logger.warning("memory_vector_collection_adopted", collection=COLLECTION_NAME, embedding_model=recorded)
        return VectorStoreState(READY, collection_model=recorded, model_recorded=True)
    if not _adoption_logged:
        _adoption_logged = True
        logger.warning("memory_vector_model_unrecorded", collection=COLLECTION_NAME,
                       reason="server does not store collection metadata; per-point model stamps are checked instead")
    return VectorStoreState(READY, collection_model=None, model_recorded=False)


def _is_not_found(exc: BaseException) -> bool:
    from qdrant_client.http.exceptions import UnexpectedResponse

    if isinstance(exc, UnexpectedResponse):
        return exc.status_code == 404
    return isinstance(exc, ValueError) and "not found" in str(exc).lower()


def collection_info() -> Dict[str, Any]:
    """Point counts of the collection. Raises when it can't be read."""
    info = _get_client().get_collection(COLLECTION_NAME)
    vectors_count = getattr(info, "vectors_count", None)
    if vectors_count is None:
        vectors_count = getattr(info, "indexed_vectors_count", 0)
    return {"collection": COLLECTION_NAME, "points_count": getattr(info, "points_count", 0),
            "vectors_count": vectors_count}


def describe() -> Dict[str, Any]:
    """The vector store's side of the status report (URL redacted)."""
    state = get_state()
    report: Dict[str, Any] = {
        "url": redact_url_userinfo(QDRANT_URL),
        "collection": COLLECTION_NAME,
        "state": state.status,
        "detail": state.detail,
    }
    if state.status in (UNAVAILABLE, EMBEDDER_UNAVAILABLE):
        report["status"] = "unavailable"
        return report
    try:
        report.update(collection_info())
    except Exception as exc:
        report.update(status="error", error=_error_text(exc))
        return report
    report["status"] = "healthy" if state.status == READY else "error"
    return report


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(model_name=EMBEDDING_MODEL, local_files_only=True, threads=1)
        logger.info("embedder_initialized", model=EMBEDDING_MODEL)
    return _embedder


def embed(texts: Sequence[str]) -> List[List[float]]:
    """Embed under the process-wide lock. Raises EmbeddingUnavailable (and
    reports ``embedder_unavailable``) when the model can't run."""
    global _embedder_error
    clipped = [str(t)[:EMBED_MAX_CHARS] for t in texts]
    with _EMBED_LOCK:
        vectors: List[List[float]] = []
        try:
            for start in range(0, len(clipped), EMBED_BATCH):
                chunk = clipped[start:start + EMBED_BATCH]
                if _embed_override is not None:
                    vectors.extend([float(x) for x in v] for v in _embed_override(chunk))
                else:
                    model = _get_embedder()
                    vectors.extend(v.tolist() for v in model.embed(chunk, batch_size=EMBED_BATCH))
        except Exception as exc:
            _embedder_error = _error_text(exc)
            logger.error("memory_embedder_failed", error=_embedder_error)
            raise EmbeddingUnavailable(_embedder_error) from exc
        _embedder_error = None
    return vectors


# ---------------------------------------------------------------------------
# Scope filter, payload, query, delete
# ---------------------------------------------------------------------------

def _scope_qdrant_filter(scope_names: Tuple[str, ...], guest_session_id: Optional[int]):
    """Qdrant rendering of a scope set; None when the set is empty (the
    caller must then not search at all)."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    conditions = []
    for name in scope_names:
        if name == "guest":
            conditions.append(Filter(must=[
                FieldCondition(key="scope", match=MatchValue(value="guest")),
                FieldCondition(key="guest_session_id", match=MatchValue(value=guest_session_id)),
            ]))
        else:
            conditions.append(FieldCondition(key="scope", match=MatchValue(value=name)))
    return Filter(should=conditions) if conditions else None


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def build_payload(memory) -> Dict[str, Any]:
    return {
        "content": memory.content,
        "summary": memory.summary or (memory.content or "")[:100],
        "scope": memory.scope,
        "guest_session_id": memory.guest_session_id,
        "category": memory.category,
        "importance": memory.importance,
        "source_type": memory.source_type,
        "created_at": _iso(memory.created_at),
        "expires_at": _iso(memory.expires_at),
        "memory_id": memory.id,
        "embedding_model": EMBEDDING_MODEL,
        "vector_written_at": _clock.utcnow().isoformat(),
    }


def query(
    vector: Sequence[float],
    *,
    readable_scopes: Tuple[str, ...],
    guest_session_id: Optional[int],
    limit: int,
    score_threshold: Optional[float],
) -> List[Tuple[str, float]]:
    """Nearest points in the given scopes as ``(point_id, score)``. Raises
    VectorStoreNotReady when the collection can't answer."""
    state = _collection_state()
    if state.status != READY:
        raise VectorStoreNotReady(state)
    if not readable_scopes:
        return []
    flt = _scope_qdrant_filter(tuple(readable_scopes), guest_session_id)
    for attempt in (0, 1):
        try:
            response = _get_client().query_points(
                collection_name=COLLECTION_NAME, query=list(vector), query_filter=flt,
                limit=limit, score_threshold=score_threshold, with_payload=False,
            )
            return [(str(p.id), float(p.score)) for p in response.points]
        except Exception as exc:
            if attempt == 0 and _is_not_found(exc):
                logger.warning("memory_vector_collection_not_found", operation="query")
                _invalidate()
                state = _collection_state()
                if state.status != READY:
                    raise VectorStoreNotReady(state) from exc
                continue
            logger.error("memory_vector_query_failed", error=_error_text(exc))
            _mark_unavailable(exc)
            raise VectorStoreNotReady(get_state()) from exc
    raise VectorStoreNotReady(get_state())  # pragma: no cover


def delete_points(ids: Iterable[str]) -> bool:
    """Best-effort point removal; never raises. Orphans left behind are
    never served (results come from rows) and the owner prune removes them."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return True
    try:
        from qdrant_client.models import PointIdsList

        _get_client().delete(collection_name=COLLECTION_NAME, points_selector=PointIdsList(points=ids))
        return True
    except Exception as exc:
        logger.warning("memory_vector_delete_failed", error=_error_text(exc), points=len(ids))
        return False


def _upsert(vector_id: str, vector: Sequence[float], payload: Dict[str, Any]) -> None:
    from qdrant_client.models import PointStruct

    _get_client().upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(id=vector_id, vector=list(vector), payload=payload)],
        wait=True,
    )


def store_vector(memory) -> bool:
    """Embed ``memory`` and upsert its point under its own ``vector_id``;
    the only path that sets ``vector_status='stored'``. On any failure the
    row is left ``pending`` (the pending pass retries it). Never raises and
    never commits: the caller owns the transaction."""
    state = get_state()
    reason = state.status
    error = state.detail
    if state.status in (READY, EMBEDDER_UNAVAILABLE):
        try:
            vector = embed([memory.content])[0]
        except EmbeddingUnavailable as exc:
            reason, error = EMBEDDER_UNAVAILABLE, str(exc)
        else:
            for attempt in (0, 1):
                try:
                    _upsert(memory.vector_id, vector, build_payload(memory))
                    memory.vector_status = "stored"
                    return True
                except Exception as exc:
                    if attempt == 0 and _is_not_found(exc):
                        logger.warning("memory_vector_collection_not_found", operation="upsert")
                        _invalidate()
                        retry_state = _collection_state()
                        if retry_state.status == READY:
                            continue
                        reason, error = retry_state.status, retry_state.detail
                        break
                    reason, error = "upsert_error", _error_text(exc)
                    _mark_unavailable(exc)
                    break
    memory.vector_status = "pending"
    logger.error("memory_vector_store_failed", memory_id=memory.id, reason=reason, error=error)
    return False


# ---------------------------------------------------------------------------
# Rebuild from Postgres
# ---------------------------------------------------------------------------

_SCROLL_PAGE = 256
_LEASE_DEPLOYMENT = "memory_vectors"


class ReindexBusy(Exception):
    """Another rebuild holds the lease, or the cooldown after one is running."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(f"memory vector reindex busy; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass
class ReindexReport:
    mode: str
    dry_run: bool = False
    pending_only: bool = False
    live_rows: int = 0
    selected: int = 0
    already_present: int = 0
    embedded: int = 0
    failed: int = 0
    content_changed_retry: int = 0
    orphans_pruned: int = 0
    foreign_orphans_pruned: int = 0
    prune_deferred_recent: int = 0
    prune_skipped: bool = False
    would_prune: int = 0
    would_recreate: bool = False
    points_after: Optional[int] = None
    foreign_point_ids_sample: Tuple[str, ...] = ()
    refused: Optional[str] = None
    aborted: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.__dict__)
        out["foreign_point_ids_sample"] = list(self.foreign_point_ids_sample)
        return out


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_old(payload: Dict[str, Any], now: datetime) -> bool:
    """Old enough to prune: written at least PRUNE_MIN_AGE_SECONDS ago. An
    unparseable timestamp, or one more than FUTURE_SKEW_SECONDS ahead,
    counts as old (it can't be vouched for as recent)."""
    written = _parse_ts(payload.get("vector_written_at")) or _parse_ts(payload.get("created_at"))
    if written is None:
        return True
    age = (now - written).total_seconds()
    if age < -FUTURE_SKEW_SECONDS:
        return True
    return age >= PRUNE_MIN_AGE_SECONDS


def _retry_after(expires_at: Optional[datetime]) -> int:
    if expires_at is None:
        return COOLDOWN_SECONDS
    return max(1, math.ceil((expires_at - _clock.utcnow()).total_seconds()))


def _acquire(mode: str):
    from app.services import service_control_settings as scs

    try:
        return scs.acquire_lease(_get_session_factory(), _LEASE_DEPLOYMENT, mode, 0,
                                 ttl=LEASE_TTL_SECONDS, now=_clock.utcnow, key=LEASE_KEY)
    except scs.LeaseBusy as busy:
        raise ReindexBusy(_retry_after(busy.expires_at)) from busy


def _live_count(session) -> int:
    from app.models import Memory

    return session.query(Memory).filter(Memory.is_deleted == False).count()  # noqa: E712


def pending_count() -> int:
    from app.models import Memory

    session = _get_session_factory()()
    try:
        return session.query(Memory).filter(
            Memory.is_deleted == False, Memory.vector_status == "pending",  # noqa: E712
        ).count()
    finally:
        session.close()


def _batches(session, pending_only: bool, max_rows: Optional[int]):
    """Row batches in id order: all live rows, or (pending_only) the first
    ``max_rows`` live pending rows."""
    from app.models import Memory

    live = session.query(Memory).filter(Memory.is_deleted == False)  # noqa: E712
    if pending_only:
        rows = live.filter(Memory.vector_status == "pending").order_by(Memory.id).limit(max_rows).all()
        for start in range(0, len(rows), ROW_BATCH):
            yield rows[start:start + ROW_BATCH]
        return
    last_id = 0
    while True:
        rows = live.filter(Memory.id > last_id).order_by(Memory.id).limit(ROW_BATCH).all()
        if not rows:
            return
        last_id = rows[-1].id
        yield rows


def _present_ids(vector_ids: List[str]) -> set:
    points = _get_client().retrieve(COLLECTION_NAME, ids=vector_ids, with_payload=False, with_vectors=False)
    return {str(p.id) for p in points}


def _write_batch(session, rows, report: ReindexReport) -> None:
    """Embed and upsert ``rows`` under their existing ids, re-check their
    content against Postgres, and mark stored only rows whose content is
    what was embedded."""
    from app.models import Memory

    embedded_content = {row.id: row.content for row in rows}
    vectors = embed([row.content for row in rows])
    for row, vector in zip(rows, vectors):
        _upsert(row.vector_id, vector, build_payload(row))
    fresh = dict(session.query(Memory.id, Memory.content).filter(Memory.id.in_(list(embedded_content))).all())
    changed = [row for row in rows if row.id in fresh and fresh[row.id] != embedded_content[row.id]]
    if changed:
        for row in changed:
            session.expire(row)
        changed_vectors = embed([fresh[row.id] for row in changed])
        for row, vector in zip(changed, changed_vectors):
            _upsert(row.vector_id, vector, build_payload(row))
            embedded_content[row.id] = fresh[row.id]
    for row_id, content in embedded_content.items():
        updated = session.query(Memory).filter(Memory.id == row_id, Memory.content == content).update(
            {"vector_status": "stored"}, synchronize_session=False)
        if updated:
            report.embedded += 1
        else:
            report.content_changed_retry += 1


def _prune_orphans(report: ReindexReport, dry_run: bool) -> None:
    """Delete points no live row owns. Scroll first, check each page against
    a fresh Postgres read, spare anything written in the last
    PRUNE_MIN_AGE_SECONDS (unless it's stamped with another model), and
    refuse to delete anything if Postgres reports no live rows at all."""
    from app.models import Memory

    client = _get_client()
    factory = _get_session_factory()
    now = _clock.utcnow()
    candidates: List[str] = []
    foreign: set = set()
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION_NAME, limit=_SCROLL_PAGE, offset=offset, with_vectors=False,
            with_payload=["vector_written_at", "created_at", "embedding_model"],
        )
        ids = [str(p.id) for p in points]
        session = factory()
        try:
            live = {vid for (vid,) in session.query(Memory.vector_id).filter(
                Memory.vector_id.in_(ids), Memory.is_deleted == False).all()}  # noqa: E712
        finally:
            session.close()
        for point in points:
            point_id = str(point.id)
            if point_id in live:
                continue
            payload = point.payload or {}
            model = payload.get("embedding_model")
            if model and model != EMBEDDING_MODEL:
                candidates.append(point_id)
                foreign.add(point_id)
            elif _is_old(payload, now):
                candidates.append(point_id)
            else:
                report.prune_deferred_recent += 1
        if offset is None:
            break

    session = factory()
    try:
        if candidates and _live_count(session) == 0:
            report.prune_skipped = True
            logger.warning("memory_vector_prune_skipped", reason="no_live_rows", candidates=len(candidates))
            return
        if dry_run:
            report.would_prune = len(candidates)
            return
        for start in range(0, len(candidates), _SCROLL_PAGE):
            chunk = candidates[start:start + _SCROLL_PAGE]
            still_live = {vid for (vid,) in session.query(Memory.vector_id).filter(
                Memory.vector_id.in_(chunk), Memory.is_deleted == False).all()}  # noqa: E712
            doomed = [pid for pid in chunk if pid not in still_live]
            if not doomed:
                continue
            from qdrant_client.models import PointIdsList

            client.delete(collection_name=COLLECTION_NAME, points_selector=PointIdsList(points=doomed))
            report.orphans_pruned += len(doomed)
            report.foreign_orphans_pruned += len([pid for pid in doomed if pid in foreign])
    finally:
        session.close()


def _preconditions(mode: str, recreate: bool, pending_only: bool) -> Optional[str]:
    status = _collection_state().status
    if status == READY:
        return None
    if pending_only:
        return status
    if status == MODEL_MISMATCH and mode == "all":
        return None
    if status == SHAPE_MISMATCH and recreate:
        return None
    return status


def reindex(
    mode: str,
    *,
    dry_run: bool = False,
    prune: bool = False,
    recreate: bool = False,
    pending_only: bool = False,
    max_rows: Optional[int] = None,
    caller: str = "cli",
) -> ReindexReport:
    """Rebuild vectors from Postgres under the cross-replica lease.

    ``missing`` embeds every pending row plus stored rows whose point is
    gone; ``all`` re-embeds every live row (and, with no failures, records
    the model on the collection). Ids are always the rows' own vector_ids.
    ``prune`` removes orphan points (owner and CLI only). ``recreate``
    drops and re-creates the collection first (CLI only; implies ``all``).
    ``pending_only`` is the automatic pass: at most ``max_rows`` pending
    rows, no presence check, no prune. Raises ReindexBusy when the lease is
    held or cooling down."""
    if recreate:
        mode = "all"
    if mode not in ("missing", "all"):
        raise ValueError(f"unknown reindex mode {mode!r}")
    report = ReindexReport(mode=mode, dry_run=dry_run, pending_only=pending_only)
    refused = _preconditions(mode, recreate, pending_only)
    if refused is not None:
        report.refused = refused
        logger.warning("memory_vector_reindex_refused", mode=mode, state=refused, caller=caller)
        return report

    from app.services import service_control_settings as scs

    factory = _get_session_factory()
    lease = _acquire(mode)
    arm_cooldown = False
    try:
        logger.info("memory_vector_reindex_started", mode=mode, dry_run=dry_run, prune=prune,
                    recreate=recreate, pending_only=pending_only, caller=caller)
        if recreate:
            client = _get_client()
            if dry_run:
                report.would_recreate = True
            else:
                before = collection_info().get("points_count")
                logger.warning("memory_vector_collection_recreating", collection=COLLECTION_NAME,
                               points_before=before)
                client.delete_collection(COLLECTION_NAME)
                _invalidate()
                state = refresh_state()
                if state.status != READY:
                    report.aborted = f"recreate_failed:{state.status}"
                    return report

        session = factory()
        try:
            report.live_rows = _live_count(session)
            for rows in _batches(session, pending_only, max_rows or PENDING_PASS_MAX_ROWS):
                report.selected += len(rows)
                try:
                    for row in rows:
                        if not row.vector_id:
                            row.vector_id = str(uuid.uuid4())
                    if pending_only:
                        chosen = list(rows)
                    else:
                        present = _present_ids([row.vector_id for row in rows])
                        report.already_present += len(present)
                        if mode == "all":
                            chosen = list(rows)
                        else:
                            chosen = [row for row in rows
                                      if row.vector_status == "pending" or row.vector_id not in present]
                    if chosen and not dry_run:
                        _write_batch(session, chosen, report)
                    if not dry_run:
                        session.commit()
                except Exception as exc:
                    session.rollback()
                    report.failed += len(rows) if not dry_run else 0
                    logger.error("memory_vector_reindex_batch_failed", error=_error_text(exc), rows=len(rows))
                    if not dry_run:
                        _mark_rows_pending([row.id for row in rows])
                if not scs.renew_lease(factory, lease, ttl=LEASE_TTL_SECONDS, now=_clock.utcnow):
                    report.aborted = "lease_lost"
                    logger.error("memory_vector_reindex_lease_lost", mode=mode)
                    return report
        finally:
            session.close()

        if prune and not pending_only:
            _prune_orphans(report, dry_run)

        if mode == "all" and report.failed == 0 and not dry_run:
            try:
                _get_client().update_collection(COLLECTION_NAME, metadata=collection_metadata())
            except Exception as exc:
                logger.info("memory_vector_metadata_update_rejected", error=_error_text(exc))
            refresh_state()

        try:
            report.points_after = _get_client().count(COLLECTION_NAME, exact=True).count
        except Exception:
            report.points_after = None
        report.foreign_point_ids_sample = get_state().foreign_point_ids_sample
        arm_cooldown = not dry_run and not pending_only
        logger.info("memory_vector_reindex_finished", caller=caller, **{
            k: v for k, v in report.to_dict().items() if k not in ("foreign_point_ids_sample",)})
        return report
    finally:
        if arm_cooldown and scs.renew_lease(factory, lease, ttl=COOLDOWN_SECONDS, now=_clock.utcnow):
            pass
        else:
            scs.release_lease(factory, lease)


def _mark_rows_pending(ids: List[int]) -> None:
    from app.models import Memory

    session = _get_session_factory()()
    try:
        session.query(Memory).filter(Memory.id.in_(ids)).update(
            {"vector_status": "pending"}, synchronize_session=False)
        session.commit()
    except Exception as exc:
        logger.error("memory_vector_mark_pending_failed", error=_error_text(exc))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Background maintenance
# ---------------------------------------------------------------------------

def _revalidation_due() -> bool:
    with _state_lock:
        state, checked = _state, _checked_at
    if state is None or checked is None:
        return True
    elapsed = _clock.monotonic() - checked
    if state.status == READY:
        return elapsed >= READY_RECHECK_SECONDS
    return elapsed >= NOT_READY_REPROBE_SECONDS


async def tick() -> None:
    """Revalidate when due, then run the automatic pending-only pass: on the
    transition to ready (the first tick after start counts), and while
    ready whenever rows are pending and PENDING_PASS_INTERVAL_SECONDS have
    passed since the last pass. The pass never prunes."""
    global _prev_status, _last_pending_pass
    if _revalidation_due():
        await asyncio.to_thread(refresh_state)
    state = _collection_state()
    previous, _prev_status = _prev_status, state.status
    if state.status != READY:
        return
    if previous == READY:
        if (_last_pending_pass is not None
                and _clock.monotonic() - _last_pending_pass < PENDING_PASS_INTERVAL_SECONDS):
            return
        if await asyncio.to_thread(pending_count) == 0:
            return
    try:
        report = await asyncio.to_thread(functools.partial(
            reindex, "missing", pending_only=True, max_rows=PENDING_PASS_MAX_ROWS, caller="auto",
        ))
    except ReindexBusy:
        logger.debug("memory_vector_pending_pass_skipped", reason="lease_busy")
        return
    _last_pending_pass = _clock.monotonic()
    if report.selected:
        logger.info("memory_vector_pending_pass", selected=report.selected, embedded=report.embedded,
                    failed=report.failed, refused=report.refused)


async def _maintenance_loop() -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("memory_vector_tick_failed", error=_error_text(exc))
        await asyncio.sleep(_TICK_INTERVAL_SECONDS)


def start_vector_store_maintenance() -> None:
    global _task
    if not _background_enabled:
        return
    if _task is not None and not _task.done():
        logger.warning("memory_vector_maintenance_already_running")
        return
    _task = asyncio.create_task(_maintenance_loop())
    logger.info("memory_vector_maintenance_started")


async def stop_vector_store_maintenance() -> None:
    global _task
    task, _task = _task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("memory_vector_maintenance_stopped")


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------

def set_client_for_tests(client, keep_state: bool = False) -> None:
    global _client
    with _client_lock:
        _client = client
    if not keep_state:
        reset_state_for_tests()


def set_embedder_for_tests(fn: Optional[Callable[[List[str]], List[List[float]]]]) -> None:
    global _embed_override, _embedder, _embedder_error
    _embed_override = fn
    _embedder = None
    _embedder_error = None


def set_clock_for_tests(clock) -> None:
    global _clock
    _clock = clock if clock is not None else _SystemClock()


def set_session_factory_for_tests(factory) -> None:
    global _session_factory
    _session_factory = factory


def set_background_enabled(enabled: bool) -> None:
    global _background_enabled
    _background_enabled = enabled


def reset_state_for_tests() -> None:
    global _state, _checked_at, _probe_inflight, _prev_status, _adoption_logged, _last_pending_pass
    with _state_lock:
        _state = None
        _checked_at = None
        _probe_inflight = False
    _prev_status = None
    _adoption_logged = False
    _last_pending_pass = None


def reset_for_tests() -> None:
    global _client
    with _client_lock:
        _client = None
    set_embedder_for_tests(None)
    set_clock_for_tests(None)
    set_session_factory_for_tests(None)
    reset_state_for_tests()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _batch_size(value: str) -> int:
    import argparse

    n = int(value)
    if not 1 <= n <= 64:
        raise argparse.ArgumentTypeError("--batch-size must be between 1 and 64")
    return n


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Rebuild memory vectors from Postgres.

    Run it as a one-off Pod with its own memory limits (it loads the
    embedding model), never as an exec into a serving admin-backend pod:
    a second model in that container can OOM-kill the server. For routine
    recovery prefer the in-process route (POST
    /api/memories/vector-store/reindex). The CLI prunes orphan points and
    is the only way to --recreate the collection, which requires typing the
    collection name.

    Prints one JSON line. Exit 0 on success, 1 when rows failed or the run
    was aborted, 2 when refused, busy or the confirmation is wrong.
    """
    import argparse

    global ROW_BATCH
    parser = argparse.ArgumentParser(prog="python -m app.services.memory_vectors",
                                     description="Memory vector store maintenance.")
    commands = parser.add_subparsers(dest="command", required=True)
    rebuild = commands.add_parser("reindex", help="rebuild vectors from Postgres (prunes orphan points)")
    rebuild.add_argument("--mode", choices=["missing", "all"], default="missing")
    rebuild.add_argument("--dry-run", action="store_true", help="report what would change; change nothing")
    rebuild.add_argument("--batch-size", type=_batch_size, default=None, help="rows per batch (1-64)")
    rebuild.add_argument("--recreate", action="store_true",
                         help="drop and re-create the collection first (implies --mode all)")
    rebuild.add_argument("--confirm-collection", default=None,
                         help=f"required with --recreate: the collection name ({COLLECTION_NAME})")
    args = parser.parse_args(argv)

    if args.recreate and args.confirm_collection != COLLECTION_NAME:
        print(json.dumps({"error": "confirmation_required",
                          "detail": f"--recreate requires --confirm-collection {COLLECTION_NAME}"}))
        return 2

    previous_batch = ROW_BATCH
    if args.batch_size:
        ROW_BATCH = args.batch_size
    try:
        report = reindex(args.mode, dry_run=args.dry_run, prune=True, recreate=args.recreate, caller="cli")
    except ReindexBusy as busy:
        print(json.dumps({"error": "reindex_busy", "retry_after_seconds": busy.retry_after_seconds}))
        return 2
    finally:
        ROW_BATCH = previous_batch
    print(json.dumps(report.to_dict()))
    if report.refused:
        return 2
    if report.failed or report.aborted:
        return 1
    return 0


if __name__ == "__main__":
    # Logs go to stderr so stdout stays the one JSON report line.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    sys.exit(main())
