"""Local-dev shim: re-exports ``src/shared/service_key.py``.

In the production container, this file is overwritten by the Dockerfile's
``COPY src/shared/service_key.py /app/backend/service_key.py`` directive, so
the container always runs the canonical helper directly.

For local-source runs (e.g. ``python main.py`` or
``python -m uvicorn main:app`` outside Docker), this shim makes
``from service_key import note_admin_refusal`` work without any PYTHONPATH
gymnastics.

DO NOT add logic here — this must be a transparent re-export only.
The single source of truth is ``src/shared/service_key.py``.
"""
import sys
from pathlib import Path

# Walk from apps/jarvis-web/backend/ → apps/jarvis-web/ → apps/ → repo root,
# then step into src/ so ``shared.service_key`` is importable.
_repo_root = Path(__file__).resolve().parents[3]
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from shared.service_key import is_header_safe, note_admin_refusal, service_key_headers  # noqa: E402

__all__ = ["is_header_safe", "note_admin_refusal", "service_key_headers"]
