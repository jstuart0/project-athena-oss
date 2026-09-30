"""Shared helpers for the telemetry tests (not a test module itself)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
FIXTURES = BACKEND / "tests" / "fixtures" / "telemetry"
SCHEMA_FILE = REPO / "docs" / "telemetry" / "payload-v1.schema.json"

# Paths whose value is a map (open-vocabulary keys): the map itself is the
# leaf, its keys are data.
MAP_LEAVES = frozenset({"usage.intent_mix_30d"})


def loc_to_path(loc: Tuple[Any, ...]) -> str:
    """A pydantic error location as the shared wire path: dots between keys,
    ``[i]`` for list indexes; the ``[key]`` marker of a map-key error is
    dropped (the path names the offending key)."""
    out = ""
    for part in loc:
        if part == "[key]":
            continue
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def error_paths(exc) -> set:
    return {loc_to_path(tuple(e["loc"])) for e in exc.errors()}


def resolve_ref(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        name = node["$ref"].split("/")[-1]
        node = schema["$defs"][name]
    return node


def _non_null(schema: dict, node: dict) -> dict:
    node = resolve_ref(schema, node)
    if "anyOf" in node:
        options = [resolve_ref(schema, o) for o in node["anyOf"] if o.get("type") != "null"]
        if len(options) == 1:
            return options[0]
    return node


def schema_leaf_paths(schema: dict) -> List[str]:
    """Leaf paths of the JSON Schema: objects with properties recurse, arrays
    of objects recurse as ``[]``, everything else (scalars, scalar lists,
    maps) is a leaf."""
    out: List[str] = []

    def walk(node: dict, prefix: str) -> None:
        node = _non_null(schema, node)
        if "properties" in node:
            for key, sub in node["properties"].items():
                walk(sub, f"{prefix}.{key}" if prefix else key)
            return
        if node.get("type") == "array":
            items = _non_null(schema, node.get("items", {}))
            if "properties" in items:
                walk(items, prefix + "[]")
                return
        out.append(prefix)

    walk(schema, "")
    return sorted(out)


def emitted_leaf_paths(payload: dict) -> List[str]:
    out = set()

    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict) and prefix not in MAP_LEAVES:
            for key, sub in value.items():
                walk(sub, f"{prefix}.{key}" if prefix else key)
            return
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for item in value:
                walk(item, prefix + "[]")
            return
        out.add(prefix)

    walk(payload, "")
    return sorted(out)


def iter_object_nodes(instance: Any, prefix: str = "") -> Iterator[Tuple[str, dict]]:
    """Every object node of a payload instance (maps excluded), as
    (path, dict). List items get ``[i]`` indexes."""
    if isinstance(instance, dict) and prefix not in MAP_LEAVES:
        yield prefix, instance
        for key, sub in instance.items():
            yield from iter_object_nodes(sub, f"{prefix}.{key}" if prefix else key)
    elif isinstance(instance, list):
        for i, sub in enumerate(instance):
            yield from iter_object_nodes(sub, f"{prefix}[{i}]")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
