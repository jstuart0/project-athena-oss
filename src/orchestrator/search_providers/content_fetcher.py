# DEPRECATED 2026-05-09 — use shared.content_fetcher; remove after ATHENA-36-followup.
# Both lines below are required: the explicit list pins the public contract,
# the star-import preserves any module-level helpers (e.g. fetch_url_content)
# not in the explicit list. Do not remove either line until the shim is deleted.
from shared.content_fetcher import (
    ContentFetcher,
    HAS_PLAYWRIGHT,
    HAS_TRAFILATURA,
    HAS_EXTRUCT,
    HAS_PANDAS,
    fetch_url_content,
)
from shared.content_fetcher import *  # noqa: F401,F403 — see comment above
