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
import os
import threading
import time
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
    return {"points_count": getattr(info, "points_count", 0), "vectors_count": vectors_count}


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


# ---------------------------------------------------------------------------
# Transitional (Phase 1 only): the write paths not yet moved to store_vector
# ---------------------------------------------------------------------------

def _client_or_none():
    return _get_client() if _collection_state().status == READY else None


def _embed_one_or_empty(text: str) -> List[float]:
    try:
        return embed([text])[0]
    except EmbeddingUnavailable:
        return []


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
    global _prev_status
    if _revalidation_due():
        await asyncio.to_thread(refresh_state)
    state = get_state()
    _prev_status = state.status


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
    global _state, _checked_at, _probe_inflight, _prev_status, _adoption_logged
    with _state_lock:
        _state = None
        _checked_at = None
        _probe_inflight = False
    _prev_status = None
    _adoption_logged = False


def reset_for_tests() -> None:
    global _client
    with _client_lock:
        _client = None
    set_embedder_for_tests(None)
    set_clock_for_tests(None)
    set_session_factory_for_tests(None)
    reset_state_for_tests()
