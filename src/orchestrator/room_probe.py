"""Read-only probe of the Home Assistant area registry, for a roll.

    cd /app && python -m orchestrator.room_probe --runs 10 [--shape] [--areas] [--utterances FILE]

Reads ``HA_URL`` and ``HA_TOKEN`` from the environment (through the one
credential resolver), fetches the three registry lists ``--runs`` times over the
same read-only WebSocket session the orchestrator uses, and prints:

* fetch time (p50, p95), payload size, area / device / entity counts, and the
  area-key collision and unmatchable-room counts;
* per area: the lamp targets a "turn on the lights" would write, and how many
  of the area's lights are unavailable;
* ``--shape``: the key names present in each registry list (never a value);
* ``--utterances FILE`` (one utterance per line): what ``decide_room`` would
  do for each, from each area that has a voice satellite.

Nothing is written to Home Assistant. Output carries no entity ids, tokens or
URLs; area names are printed only with ``--areas`` (otherwise ``area 1``...).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO

from orchestrator import room_context
from orchestrator.room_directory import RoomDirectoryProvider
from orchestrator.ha_entity_manager import _room_light_exclude_patterns, area_lamp_targets
from shared.ha_auth import HAAuthLatch
from shared.ha_credentials import KeyLookup, StaticResolver, resolve
from shared.ha_http import HAChannel
from shared.ha_registry import RegistrySnapshot, snapshot_fetcher

UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


class _Snapshot:
    """A registry that always has this one snapshot, for the directory provider
    (which then reads the live HA_LIGHT_GROUPS and HA_SATELLITE_ROOM_MAP)."""

    def __init__(self, snapshot: RegistrySnapshot):
        self._snapshot = snapshot

    def current(self) -> RegistrySnapshot:
        return self._snapshot

    def read(self) -> RegistrySnapshot:
        return self._snapshot


def shape_of(raw: Mapping[str, Any]) -> Dict[str, List[str]]:
    """The union of key names per list. Values are never read."""
    return {
        name: sorted({key for item in items if isinstance(item, dict) for key in item})
        for name, items in raw.items() if isinstance(items, list)
    }


def describe_outcome(decision: Any) -> str:
    """The decision's kind, and how many rooms it names. No names."""
    if isinstance(decision, room_context.Room):
        return "room"
    if isinstance(decision, room_context.Rooms):
        return f"rooms({len(decision.keys)})"
    if isinstance(decision, room_context.WholeHouse):
        return f"whole_house(excluding {len(decision.excluded)})"
    if isinstance(decision, room_context.AskWhich):
        return f"ask_which({len(decision.candidates)})"
    if isinstance(decision, room_context.AskRoom):
        return "ask_room"
    return type(decision).__name__


def decide_for(utterance: str, speaker_room: Optional[str], directory: room_context.RoomDirectory) -> str:
    mention = room_context.find_room_mention(utterance, directory)
    return describe_outcome(room_context.decide_room(
        mention, query=utterance, speaker_room=speaker_room, directory=directory))


def satellite_area_keys(snapshot: RegistrySnapshot) -> List[str]:
    return [a.key for a in snapshot.areas if snapshot.entities_in(a.area_id, "assist_satellite")]


def per_area_lines(
    snapshot: RegistrySnapshot, states: Mapping[str, Any], exclude: Sequence, *, show_names: bool,
) -> List[str]:
    lines = []
    for index, area in enumerate(sorted(snapshot.areas, key=lambda a: a.key), start=1):
        targets = area_lamp_targets(snapshot, area.area_id, states, exclude)
        lights = snapshot.entities_in(area.area_id, "light")
        down = sum(1 for eid in lights if (states.get(eid) or {}).get("state") in UNAVAILABLE_STATES)
        label = area.key if show_names else f"area {index}"
        lines.append(f"  {label}: lamp targets {len(targets)}, lights {len(lights)}, unavailable {down}")
    return lines


async def run(
    fetch: Callable[[], Any],
    read_states: Callable[[], Any],
    *,
    runs: int,
    shape: bool,
    show_names: bool,
    utterances: Sequence[str],
    exclude: Sequence = (),
    out: TextIO = sys.stdout,
) -> int:
    timings, raw = [], None
    for _ in range(runs):
        started = time.perf_counter()
        raw = await fetch()
        timings.append((time.perf_counter() - started) * 1000)
    snapshot = RegistrySnapshot.from_raw(raw, fetched_at=time.monotonic())
    size = len(json.dumps(raw, separators=(",", ":")))
    counts = snapshot.counts()
    directory = RoomDirectoryProvider(_Snapshot(snapshot), None).current()
    print(f"fetch: runs {runs}, p50 {statistics.median(timings):.0f} ms, p95 {_percentile(timings, 0.95):.0f} ms, "
          f"payload {size} bytes", file=out)
    print(f"registry: areas {counts['areas']}, devices {counts['devices']}, entities {counts['entities']}, "
          f"collisions {counts['collisions']}, unmatchable {directory.unmatchable_count}", file=out)

    states = await read_states()
    print("per area:", file=out)
    for line in per_area_lines(snapshot, states, exclude, show_names=show_names):
        print(line, file=out)

    if shape:
        print("shape:", file=out)
        for name, keys in shape_of(raw).items():
            print(f"  {name}: {', '.join(keys)}", file=out)

    if utterances:
        keys = satellite_area_keys(snapshot)
        print(f"utterances: {len(utterances)} from {len(keys)} satellite areas (no writes)", file=out)
        for text_index, utterance in enumerate(utterances, start=1):
            outcomes: Dict[str, int] = {}
            for key in keys:
                kind = decide_for(utterance, key, directory)
                outcomes[kind] = outcomes.get(kind, 0) + 1
            print(f"  utterance {text_index}: " + ", ".join(f"{k} x{n}" for k, n in sorted(outcomes.items())), file=out)
    return 0


def _read_utterances(path: Optional[str]) -> List[str]:
    if not path:
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


async def _live_states(resolver: StaticResolver, latch: HAAuthLatch) -> Mapping[str, Any]:
    channel = HAChannel(resolver, latch, caller="orchestrator_rest", timeout=5.0)
    try:
        response = await channel.request("GET", "/api/states")
        response.raise_for_status()
        return {item["entity_id"]: item for item in response.json()}
    finally:
        await channel.aclose()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.room_probe", description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--shape", action="store_true")
    parser.add_argument("--areas", action="store_true", help="print area keys instead of numbering them")
    parser.add_argument("--utterances", metavar="FILE")
    args = parser.parse_args(argv)
    service_key = os.environ.get("SERVICE_API_KEY")
    credentials = resolve(KeyLookup.absent(), os.environ, previous=None, service_api_key=service_key)
    if not credentials.configured:
        print("HA_URL and HA_TOKEN must be set (and valid)", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2
    resolver = StaticResolver(credentials)
    latch = HAAuthLatch(None, instance="room-probe", service_api_key=service_key)
    try:
        return asyncio.run(run(
            snapshot_fetcher(resolver, latch, caller="orchestrator_registry"),
            lambda: _live_states(resolver, latch),
            runs=args.runs, shape=args.shape, show_names=args.areas,
            utterances=_read_utterances(args.utterances), exclude=_room_light_exclude_patterns(),
        ))
    except Exception as exc:
        print(f"probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
