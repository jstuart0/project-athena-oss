#!/usr/bin/env python3
"""Generate `admin/frontend/.callee-sink-phase-scopes.json` — the pinned
site populations `check-callee-sinks.py --scope <name>` filters against.

Not a gate. Run once per phase that needs a new named scope, BEFORE that
phase's call-site edits land, and commit the result. `check-callee-sinks.py`
reads it at gate time; see `load_phase_scope`'s docstring there for why the
scope must be a static manifest rather than a live re-classification.

"phase3" (ATHENA-66) = every `file:line` call site Phase 3 converts:
  - the wrong-primitive population (46 direct + 6 builder-routed), per
    `check-handler-escaping.py --class wrong-primitive`.
  - the 5 sink-associated call sites fixed alongside their callee sinks in
    this phase, per the committed `.callee-sink-table.json`'s
    `sink-unescaped` bucket at generation time (all 5 are fixed in Phase 3;
    none is deferred to Phase 6).

Never hand-edit the output — re-run this script.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"
OUT_PATH = FRONTEND_DIR / ".callee-sink-phase-scopes.json"


def load(module_file: str):
    name = module_file.replace("-", "_").rstrip(".py")
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPTS_DIR / module_file
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    handler = load("check-handler-escaping.py")
    callee = load("check-callee-sinks.py")

    interps, builders = handler.all_interpolations(FRONTEND_DIR)
    wrong_primitive_keys = {
        f"{i['file']}:{i['line']}" for i in interps if handler.classify(i) == "wrong-primitive"
    }

    adjudications = callee.load_adjudications(callee.DEFAULT_ADJUDICATIONS)
    sites = callee.collect_sites(FRONTEND_DIR)
    sink_unescaped_keys = set()
    for file, line, expr, raw_value, offset in sites:
        bucket, _detail = callee.resolve_site(file, line, expr, raw_value, offset, adjudications)
        if bucket == "sink-unescaped":
            sink_unescaped_keys.add(f"{callee.rel(file)}:{line}")

    phase3 = sorted(wrong_primitive_keys | sink_unescaped_keys)

    manifest = {
        "_meta": {
            "generated_by": "scripts/generate-callee-sink-phase-scope.py",
            "note": "Pinned before Phase 3's call-site edits landed. Do not hand-edit.",
        },
        "phase3": phase3,
    }
    OUT_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH} ({len(phase3)} phase3 sites: "
          f"{len(wrong_primitive_keys)} wrong-primitive + {len(sink_unescaped_keys)} sink-unescaped, "
          f"{len(wrong_primitive_keys & sink_unescaped_keys)} overlapping)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
