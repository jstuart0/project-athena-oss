"""Local-dev shim: re-exports ``src/shared/household_settings.py``.

In the production container, this file is overwritten by the Dockerfile's
``COPY src/shared/household_settings.py /app/backend/household_settings.py``
directive, so the container always runs the canonical module directly.

For local-source runs (``python -m uvicorn main:app`` outside Docker), this
shim makes ``import household_settings`` work without PYTHONPATH changes.

DO NOT add logic here — this must be a transparent re-export only.
The single source of truth is ``src/shared/household_settings.py``.
"""
import sys
from pathlib import Path

# Walk from apps/jarvis-web/backend/ → apps/jarvis-web/ → apps/ → repo root,
# then step into src/ so ``shared.household_settings`` is importable.
_repo_root = Path(__file__).resolve().parents[3]
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

import shared.household_settings as _canonical  # noqa: E402

# Every name the canonical module defines, so this file is indistinguishable
# from the one the image copies in.
globals().update({k: v for k, v in vars(_canonical).items() if not k.startswith("__")})
