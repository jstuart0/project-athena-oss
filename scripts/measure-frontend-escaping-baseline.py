#!/usr/bin/env python3
"""Measure every ATHENA-66 population against the ACTUAL tree and write
`admin/frontend/.escaping-baseline.json`.

Binding per the plan: "Measure the baseline against the actual tree — do not
copy numbers out of the plan." Every count here is produced by the committed
classifiers in this same directory, not transcribed from `## Context`. Gates
run in baseline-comparison mode from Phase 1 onward; each later phase
tightens specific fields toward zero.

Not itself a gate — it PRODUCES the file the gates compare against. Re-run
and re-commit whenever a classifier's definition changes (never hand-edit
the JSON).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"
OUT_PATH = FRONTEND_DIR / ".escaping-baseline.json"


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
    uniqueness = load("check-escape-html-uniqueness.py")
    handler = load("check-handler-escaping.py")
    callee = load("check-callee-sinks.py")
    html_template = load("check-html-template-escaping.py")
    wiring = load("check-frontend-wiring.py")

    definitions = uniqueness.find_definitions(FRONTEND_DIR)
    entity_map_any_name = uniqueness.find_entity_map_any_name(
        FRONTEND_DIR, uniqueness.resolve_exclude_paths(FRONTEND_DIR, None)
    )
    dom_roundtrip_any_name = uniqueness.find_dom_roundtrip_any_name(
        FRONTEND_DIR, uniqueness.resolve_exclude_paths(FRONTEND_DIR, None)
    )

    interps, builders = handler.all_interpolations(FRONTEND_DIR)
    by_class = {"wrong-primitive": [], "unescaped-quoted": [], "bare-expression": [], "correct": []}
    for i in interps:
        by_class[handler.classify(i)].append(i)

    builder_set = sorted(b.key(REPO_ROOT) for b in builders)

    no_handrolled_rc, no_handrolled_payload = handler.cmd_no_handrolled_escape(
        type("Args", (), {"dir": FRONTEND_DIR})()
    )
    mis_context_census = handler.mis_context_census(FRONTEND_DIR)
    mis_context_counts = {k: (v if k == "style" else len(v)) for k, v in mis_context_census.items()}

    callee_sites = callee.collect_sites(FRONTEND_DIR)
    adjudications = callee.load_adjudications(callee.DEFAULT_ADJUDICATIONS)
    callee_buckets = {"sink-escaped": 0, "sink-unescaped": 0, "unresolved": 0}
    for file, line, expr, raw_value, offset in callee_sites:
        bucket, _detail = callee.resolve_site(file, line, expr, raw_value, offset, adjudications)
        callee_buckets[bucket] = callee_buckets.get(bucket, 0) + 1

    emerging_intents_path = FRONTEND_DIR / "emerging-intents.js"
    emerging_text_node = html_template.collect(FRONTEND_DIR, emerging_intents_path)
    emerging_raw = [
        i for i in emerging_text_node if i["bucket"] == "text-node" and not i["escaped"]
    ]

    innerhtml_matches = []
    text = None
    for path in html_template.scan.iter_frontend_js_files(FRONTEND_DIR):
        t = path.read_text(encoding="utf-8", errors="ignore")
        for m in html_template.INNERHTML_ASSIGN_RE.finditer(t):
            innerhtml_matches.append(html_template.rel(path))
    innerhtml_files = sorted(set(innerhtml_matches))

    data_attr_items = [i for i in html_template.collect(FRONTEND_DIR, None) if i["bucket"] == "quoted-attribute" and not i["escaped"]]
    data_attr_files = sorted({i["file"] for i in data_attr_items})

    insert_adjacent_matches = []
    for path in html_template.scan.iter_frontend_js_files(FRONTEND_DIR):
        t = path.read_text(encoding="utf-8", errors="ignore")
        if ".insertAdjacentHTML(" in t:
            count = t.count(".insertAdjacentHTML(")
            insert_adjacent_matches.append((html_template.rel(path), count))
    insert_adjacent_total = sum(c for _f, c in insert_adjacent_matches)
    insert_adjacent_files = sorted({f for f, _c in insert_adjacent_matches})

    wiring_parity = wiring.compute_parity(FRONTEND_DIR)
    buster_rc, buster_payload = wiring.cmd_buster_presence(type("Args", (), {"dir": FRONTEND_DIR})())

    baseline = {
        "_meta": {
            "measured_by": "scripts/measure-frontend-escaping-baseline.py",
            "note": "Every count here is measured against the actual tree, not copied from the plan.",
        },
        "definitions": definitions,
        "definitions_entity_map_any_name": entity_map_any_name,
        "definitions_dom_roundtrip_any_name": [
            {"file": html_template.rel(Path(d["file"])) if isinstance(d["file"], str) and not d["file"].startswith(str(REPO_ROOT)) else d["file"], **{k: v for k, v in d.items() if k != "file"}}
            for d in dom_roundtrip_any_name
        ],
        "handler_escaping": {
            "wrong_primitive_total": len(by_class["wrong-primitive"]),
            "wrong_primitive_builder": len([i for i in by_class["wrong-primitive"] if i["scope"] == "builder"]),
            "unescaped_quoted_total": len(by_class["unescaped-quoted"]),
            "unescaped_quoted_builder": len([i for i in by_class["unescaped-quoted"] if i["scope"] == "builder"]),
            "bare_expression_total": len(by_class["bare-expression"]),
            "correct_total": len(by_class["correct"]),
            "builder_set": builder_set,
            "handrolled_escape_sites": no_handrolled_payload["violations"],
        },
        "mis_context": {
            **mis_context_counts,
            "style_set": mis_context_census["style"],
        },
        "callee_sinks": callee_buckets,
        "emerging_intents_raw_text_node_sites": len(emerging_raw),
        "insert_adjacent_html": {"total": insert_adjacent_total, "files": insert_adjacent_files},
        "innerhtml_assignments": {"total": len(innerhtml_matches), "files": innerhtml_files},
        "data_bearing_plain_attribute": {"total": len(data_attr_items), "files": data_attr_files},
        "wiring": {
            "dangling": wiring_parity["dangling"],
            "busterless_tags": buster_payload["missing_buster"],
            "busterless_count": len(buster_payload["missing_buster"]),
        },
    }

    OUT_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
