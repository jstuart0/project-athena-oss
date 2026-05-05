"""Local-dev shim: re-exports ``get_admin_url`` from ``src/shared/admin_url.py``.

In the production container, this file is overwritten by the Dockerfile's
``COPY src/shared/admin_url.py /app/backend/admin_url.py`` directive, so the
container always runs the canonical helper directly.

For local-source runs (e.g. ``python main.py`` or
``python -m uvicorn main:app`` outside Docker), this shim makes
``from admin_url import get_admin_url`` work without any PYTHONPATH
gymnastics.

DO NOT add resolution logic here — this must be a transparent re-export only.
The single source of truth is ``src/shared/admin_url.py``.
"""
import sys
from pathlib import Path

# Walk from apps/jarvis-web/backend/ → apps/jarvis-web/ → apps/ → repo root,
# then step into src/ so ``shared.admin_url`` is importable.
_repo_root = Path(__file__).resolve().parents[3]
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from shared.admin_url import get_admin_url, _clear_cache_for_tests  # noqa: E402

__all__ = ["get_admin_url", "_clear_cache_for_tests"]
