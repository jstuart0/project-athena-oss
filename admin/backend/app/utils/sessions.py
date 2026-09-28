"""Session-rotation helper shared by every authentication entry point.

ATHENA-80 (session fixation, codex diff review 2026-09-28): starsessions'
regenerate_session_id() only repoints the in-request SessionHandler to a
freshly generated id -- it never removes the pre-rotation entry from the
backing store (Redis in production, InMemoryStore in DEV_MODE). If that old
id already held data before this request -- an attacker-seeded fixation
cookie that was itself a live (if unauthenticated) session, or any other
stale pre-existing entry -- it stays valid under its own TTL even after this
request rotates to a new id. rotate_session_id() closes that gap: capture
the old id, rotate, then explicitly purge the old entry from the store so
it cannot be replayed.

Do NOT use this on auth_logout. See the comment above auth_logout in
main.py: for an already-cleared (empty) session, starsessions'
SessionMiddleware already removes the store entry and expires the cookie
on its own, and calling regenerate_session_id() there actively defeats
that cleanup (it swaps session_id to a fresh, unused id before the
middleware's own destroy() call runs, so destroy() deletes the wrong
entry).
"""
from fastapi import Request
from starsessions import get_session_handler, regenerate_session_id


async def rotate_session_id(request: Request) -> str:
    """Rotate the session id post-authentication and purge the old entry.

    Call immediately after request.session has been populated with the
    authenticated user's data (access_token, user_id, ...).
    """
    handler = get_session_handler(request)
    old_session_id = handler.session_id
    new_session_id = regenerate_session_id(request)
    if old_session_id:
        await handler.store.remove(old_session_id)
    return new_session_id
