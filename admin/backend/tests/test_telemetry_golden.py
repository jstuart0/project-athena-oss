"""valid-v1-full.json is the sender's real output over a seeded database, so
the fixture the collector accepts is what an install actually sends.

Regenerate after an intended payload change:
    UPDATE_GOLDEN=1 pytest tests/test_telemetry_golden.py
then copy the fixtures to the collector (scripts/sync-athena-fixtures.sh).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from app.services.telemetry.collect import collect_facts
from app.services.telemetry.schema import SendState, build_payload, pretty
from tests._telemetry_support import FIXTURES, GOLDEN_ID, canonical_collect_kwargs, seed_canonical

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def test_full_fixture_is_the_collected_payload(db):
    seed_canonical(db, NOW)
    facts = asyncio.run(collect_facts(db, **canonical_collect_kwargs(NOW)))
    state = SendState(GOLDEN_ID, "heartbeat", "upgraded", "0.5.0", "stable", "self_hosted_real")
    text = pretty(build_payload(facts, state))
    path = FIXTURES / "valid-v1-full.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text
