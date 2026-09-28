"""Session-rotation helper shared by every authentication entry point.

ATHENA-80 (session fixation, codex diff review 2026-09-28): starsessions'
regenerate_session_id() only repoints the in-request SessionHandler to a
freshly generated id -- it never removes the pre-rotation entry from the
backing store (Redis in production, InMemoryStore in DEV_MODE). Confirmed
against the INSTALLED starsessions 2.2.1 source
(site-packages/starsessions/session.py:153-155):

    def regenerate_id(self) -> str:
        self.session_id = generate_session_id()
        return self.session_id

That's the entire method -- no store write, no removal. `save()`
(session.py:133-142) only ever writes under `self.session_id`, which by
then is the NEW id; it never touches the old one. There is no
`_remove_data_for_session` (or any equivalent) anywhere in the installed
package -- confirmed by grepping every file under
site-packages/starsessions/. So if the old id already held data before
this request -- an attacker-seeded fixation cookie that was itself a live
(if unauthenticated) session, or any other stale pre-existing entry -- it
stays valid under its own TTL even after regenerate_session_id() runs.
rotate_session_id() below closes that gap itself: capture the old id,
rotate, then explicitly purge the old entry from the store. This is a real
fix for a real gap in the installed version, not defense-in-depth against
something the library already does.

Do NOT use this on auth_logout. See the comment above auth_logout in
main.py: for an already-cleared (empty) session, starsessions'
SessionMiddleware already removes the store entry and expires the cookie
on its own, and calling regenerate_session_id() there actively defeats
that cleanup (it swaps session_id to a fresh, unused id before the
middleware's own destroy() call runs, so destroy() deletes the wrong
entry).
"""
import structlog
from fastapi import Request
from starsessions import get_session_handler, regenerate_session_id

logger = structlog.get_logger()


async def rotate_session_id(request: Request) -> str:
    """Rotate the session id post-authentication and purge the old entry.

    Call immediately after request.session has been populated with the
    authenticated user's data (access_token, user_id, ...).

    xander (residual Medium, 2026-09-28): rotating a caller-supplied cookie
    purges whatever session that id held. If an attacker plants a victim's
    live, authenticated cookie and then logs in as themselves under that
    same cookie, the victim's session gets purged as a side effect -- a
    force-logout of an identity that isn't the one authenticating right
    now. That's the fixation defense doing its job (the old id must not
    survive), but it's silent: nobody can tell a cross-identity purge from
    an ordinary same-user re-login. Before removing the old entry, read it
    and compare its user_id against the one that just authenticated; if
    they differ, log a WARNING (identities and client IP only -- never
    tokens) so this is visible in the audit trail. The purge itself still
    happens either way -- the fixation defense always wins.
    """
    handler = get_session_handler(request)
    old_session_id = handler.session_id
    new_user_id = request.session.get("user_id")

    if old_session_id:
        old_raw = await handler.store.read(old_session_id, handler.lifetime)
        if old_raw:
            try:
                old_data = handler.serializer.deserialize(old_raw)
            except Exception:
                old_data = {}
            old_user_id = old_data.get("user_id")
            if old_user_id is not None and old_user_id != new_user_id:
                logger.warning(
                    "session_rotation_purged_foreign_identity",
                    old_user_id=old_user_id,
                    new_user_id=new_user_id,
                    client_ip=request.client.host if request.client else None,
                )

    new_session_id = regenerate_session_id(request)
    if old_session_id:
        await handler.store.remove(old_session_id)
    return new_session_id
