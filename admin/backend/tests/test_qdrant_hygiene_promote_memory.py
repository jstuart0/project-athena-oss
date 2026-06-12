"""
qdrant-hygiene reconcile tests — promote_memory vector safety.

Covers two ian fixes from the qdrant-hygiene campaign:

  ian High  — promote_memory must call qdrant.retrieve with with_vectors=True;
              omitting it returns vector=None (qdrant-client 1.10+ default) and
              silently upserts a null vector (data corruption).

  ian High  — promote_memory must raise/abort when vector is None instead of
              upserting a null vector into Qdrant.
"""
import os
import sys
import unittest.mock as mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-qdrant-hygiene")

import pytest

# ---------------------------------------------------------------------------
# Import the module under test after env is set.
# We import the function directly so we can patch its collaborators without
# needing to stand up a full FastAPI app.
# ---------------------------------------------------------------------------
import app.routes.memories as memories_module


def _make_mock_point(vector=None, payload=None):
    """Return a mock Qdrant ScoredPoint/Record with the given vector and payload."""
    pt = mock.MagicMock()
    pt.vector = vector
    pt.payload = payload or {"scope": "guest", "guest_session_id": "sess-abc"}
    return pt


# ---------------------------------------------------------------------------
# Tests: retrieve is called with with_vectors=True
# ---------------------------------------------------------------------------

class TestPromoteMemoryRetrieveCallsWithVectors:
    """promote_memory must pass with_vectors=True to qdrant.retrieve."""

    def _run_promote(self, mock_qdrant, mock_memory, mock_db, mock_user):
        """Drive the core Qdrant branch of promote_memory in isolation."""
        # Patch collaborators used inside the function
        with mock.patch.object(memories_module, "get_qdrant", return_value=mock_qdrant), \
             mock.patch.object(memories_module, "COLLECTION_NAME", "test-collection"):

            # Simulate the DB query returning our mock memory
            mock_db.query.return_value.filter.return_value.first.return_value = mock_memory

            # We only need to exercise the Qdrant block; stop before DB write
            mock_db.add = mock.MagicMock()
            mock_db.commit = mock.MagicMock()
            mock_db.refresh = mock.MagicMock()

            import asyncio
            from app.routes.memories import promote_memory, PromoteRequest

            request = PromoteRequest(target_scope="owner")
            coro = promote_memory(
                memory_id=mock_memory.id,
                request=request,
                db=mock_db,
                current_user=mock_user,
            )
            return asyncio.run(coro)

    def _make_fixtures(self, vector_value=None, missing=False):
        """Return (qdrant_mock, memory_mock, db_mock, user_mock)."""
        # Memory fixture
        memory = mock.MagicMock()
        memory.id = 42
        memory.vector_id = "vec-uuid-001"
        memory.scope = "guest"
        memory.content = "some content"
        memory.summary = "summary"
        memory.category = "general"
        memory.importance = 1
        memory.is_deleted = False

        # Qdrant mock
        qdrant = mock.MagicMock()
        if missing:
            qdrant.retrieve.return_value = []
        else:
            pt = _make_mock_point(vector=vector_value, payload={"scope": "guest"})
            qdrant.retrieve.return_value = [pt]

        # DB mock
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.first.return_value = memory

        # User mock — must have write permission
        user = mock.MagicMock()
        user.has_permission.return_value = True
        user.username = "testuser"

        return qdrant, memory, db, user

    # ------------------------------------------------------------------
    def test_retrieve_called_with_with_vectors_true(self):
        """qdrant.retrieve must be called with with_vectors=True."""
        vec = [0.1, 0.2, 0.3]
        qdrant, memory, db, user = self._make_fixtures(vector_value=vec)

        self._run_promote(qdrant, memory, db, user)

        qdrant.retrieve.assert_called_once()
        call_kwargs = qdrant.retrieve.call_args
        assert call_kwargs.kwargs.get("with_vectors") is True or \
               (call_kwargs.args and False), \
            "retrieve must be called with with_vectors=True keyword argument"
        assert qdrant.retrieve.call_args.kwargs["with_vectors"] is True

    def test_retrieve_collection_and_ids(self):
        """qdrant.retrieve must target the correct collection and vector_id."""
        vec = [0.1, 0.2, 0.3]
        qdrant, memory, db, user = self._make_fixtures(vector_value=vec)

        self._run_promote(qdrant, memory, db, user)

        qdrant.retrieve.assert_called_once()
        kwargs = qdrant.retrieve.call_args.kwargs
        assert kwargs["collection_name"] == "test-collection"
        assert kwargs["ids"] == [memory.vector_id]


# ---------------------------------------------------------------------------
# Tests: abort when vector is None
# ---------------------------------------------------------------------------

class TestPromoteMemoryAbortOnNullVector:
    """promote_memory must not upsert when the retrieved vector is None."""

    def _run_promote_expect_error(self, qdrant, memory, db, user):
        """Run promote_memory and expect the Qdrant block to raise inside the try/except."""
        with mock.patch.object(memories_module, "get_qdrant", return_value=qdrant), \
             mock.patch.object(memories_module, "COLLECTION_NAME", "test-collection"):

            db.query.return_value.filter.return_value.first.return_value = memory
            db.add = mock.MagicMock()
            db.commit = mock.MagicMock()
            db.refresh = mock.MagicMock()

            import asyncio
            from app.routes.memories import promote_memory, PromoteRequest

            request = PromoteRequest(target_scope="owner")
            coro = promote_memory(
                memory_id=memory.id,
                request=request,
                db=db,
                current_user=user,
            )
            # promote_memory catches the ValueError internally and logs; the
            # outer function still returns (PostgreSQL record is still created).
            # What we care about: qdrant.upsert must NOT have been called.
            asyncio.run(coro)

    def _make_fixtures(self, vector_value=None, missing=False):
        memory = mock.MagicMock()
        memory.id = 99
        memory.vector_id = "vec-null-001"
        memory.scope = "guest"
        memory.content = "content"
        memory.summary = "summary"
        memory.category = "general"
        memory.importance = 1
        memory.is_deleted = False

        qdrant = mock.MagicMock()
        if missing:
            qdrant.retrieve.return_value = []
        else:
            pt = _make_mock_point(vector=vector_value)
            qdrant.retrieve.return_value = [pt]

        db = mock.MagicMock()
        db.query.return_value.filter.return_value.first.return_value = memory

        user = mock.MagicMock()
        user.has_permission.return_value = True
        user.username = "testuser"

        return qdrant, memory, db, user

    # ------------------------------------------------------------------
    def test_upsert_not_called_when_vector_is_none(self):
        """qdrant.upsert must not be called when retrieve returns vector=None."""
        qdrant, memory, db, user = self._make_fixtures(vector_value=None)

        self._run_promote_expect_error(qdrant, memory, db, user)

        qdrant.upsert.assert_not_called()

    def test_upsert_not_called_when_point_missing(self):
        """qdrant.upsert must not be called when retrieve returns empty list."""
        qdrant, memory, db, user = self._make_fixtures(missing=True)

        self._run_promote_expect_error(qdrant, memory, db, user)

        qdrant.upsert.assert_not_called()

    def test_upsert_called_when_vector_present(self):
        """qdrant.upsert must be called when a valid vector is present."""
        vec = [0.1, 0.2, 0.3]
        qdrant, memory, db, user = self._make_fixtures(vector_value=vec)

        self._run_promote_expect_error(qdrant, memory, db, user)

        qdrant.upsert.assert_called_once()
