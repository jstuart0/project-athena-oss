"""Local-dev shim: re-exports ``src/shared/client_throttle.py``.

In the production container, this file is overwritten by the Dockerfile's
``COPY src/shared/client_throttle.py /app/backend/client_throttle.py``
directive, so the container always runs the canonical module directly.

For local-source runs (``python -m uvicorn main:app`` outside Docker), this
shim makes ``import client_throttle`` work without PYTHONPATH changes.

DO NOT add logic here — this must be a transparent re-export only.
The single source of truth is ``src/shared/client_throttle.py``.
"""
import sys
from pathlib import Path

# Walk from apps/jarvis-web/backend/ → apps/jarvis-web/ → apps/ → repo root,
# then step into src/ so ``shared.client_throttle`` is importable.
_repo_root = Path(__file__).resolve().parents[3]
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from shared.client_throttle import (  # noqa: E402,F401
    ResolvedClient,
    SlidingWindowLimiter,
    in_networks,
    invalid_network_entries,
    local_candidate,
    parse_ip,
    parse_networks,
    rate_limit_key,
    read_forwarded_for,
    resolve_rate_client,
)
