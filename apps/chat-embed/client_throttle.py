"""Local-dev shim: re-exports ``src/shared/client_throttle.py``.

In the production container, this file is overwritten by the Dockerfile's
``COPY src/shared/client_throttle.py`` directive (once the image builds from the
repository root), so the container always runs the canonical module directly.

For local-source runs (``python -m uvicorn main:app`` outside Docker), this
shim makes ``import client_throttle`` work without PYTHONPATH changes.

DO NOT add logic here — this must be a transparent re-export only.
The single source of truth is ``src/shared/client_throttle.py``.
"""
import sys
from pathlib import Path

# Walk from apps/chat-embed/ → apps/ → repo root,
# then step into src/ so ``shared.client_throttle`` is importable.
_repo_root = Path(__file__).resolve().parents[2]
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

import shared.client_throttle as _canonical  # noqa: E402

# Every name the canonical module defines, so this file is indistinguishable
# from the one the image copies in.
globals().update({k: v for k, v in vars(_canonical).items() if not k.startswith("__")})
