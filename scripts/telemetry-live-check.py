#!/usr/bin/env python3
"""Send one real telemetry cycle to a collector and check it was accepted.

The live contract check before merging a telemetry change: admin-backend's
real sender (collect -> build -> sign -> POST) runs against a throwaway
SQLite database, so what the collector receives is exactly what an install
would send.

    # a local collector (services/athena-telemetry: npm run db:migrate:local && npm run dev)
    python3 scripts/telemetry-live-check.py --endpoint http://127.0.0.1:8787/v1/ping \\
        --expect-local-row --collector-dir /path/to/services/athena-telemetry

    # a deployed collector: test-class ping; clean it up with the printed ID and date
    python3 scripts/telemetry-live-check.py --endpoint https://<collector>/v1/ping --mode test

Run it with admin-backend's dependencies installed (requirements-test.txt
plus `pip install -e src/shared`). Exit 0 only when the cycle was accepted
(and, with --expect-local-row, the local D1 has exactly one row for the ID).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", required=True, help="the collector's /v1/ping URL")
    parser.add_argument("--mode", default="test", choices=["test", "ci", "dev", "self_hosted_real"],
                        help="ATHENA_TELEMETRY_MODE for the ping (default: test, so the collector flags it)")
    parser.add_argument("--expect-local-row", action="store_true",
                        help="also query the local D1 (wrangler d1 execute --local) for exactly one row")
    parser.add_argument("--collector-dir", type=Path, help="services/athena-telemetry, for --expect-local-row")
    return parser.parse_args(argv)


def _prepare_environment(db_path: Path, args) -> None:
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # DEV_MODE makes the models use SQLite-compatible column types; the
    # sender's database check is pointed at the real file below.
    os.environ["DEV_MODE"] = "true"
    os.environ.setdefault("SERVICE_API_KEY", "telemetry-live-check")
    os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:1")
    os.environ["ATHENA_TELEMETRY"] = ""
    os.environ["ATHENA_TELEMETRY_ENDPOINT"] = args.endpoint
    os.environ["ATHENA_TELEMETRY_MODE"] = args.mode
    for name in ("DO_NOT_TRACK", "CI"):
        os.environ.pop(name, None)
    sys.path[:0] = [str(REPO / "admin" / "backend"), str(REPO / "src")]


def _seed(session_factory) -> None:
    from app.models import ComponentModelAssignment, User

    session = session_factory()
    try:
        session.add(User(username="live-check", email="live-check@example.invalid", role="owner",
                         created_at=datetime.now(timezone.utc) - timedelta(days=2)))
        session.add(ComponentModelAssignment(component_name="intent_classifier", display_name="Intent Classifier",
                                             category="orchestrator", model_name="qwen3:4b-instruct-2507-q4_K_M",
                                             backend_type="ollama", enabled=True))
        session.commit()
    finally:
        session.close()


def _local_rows(collector_dir: Path, installation_id: str) -> int:
    command = ["npx", "wrangler", "d1", "execute", "athena-telemetry", "--local", "--json", "--command",
               f"SELECT count(*) AS n FROM installs WHERE installation_id = '{installation_id}'"]
    result = subprocess.run(command, cwd=collector_dir, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise SystemExit(f"wrangler d1 execute failed ({result.returncode}): {result.stderr.strip()[-500:]}")
    return int(json.loads(result.stdout)[0]["results"][0]["n"])


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.expect_local_row and not args.collector_dir:
        raise SystemExit("--expect-local-row needs --collector-dir")
    with tempfile.TemporaryDirectory(prefix="athena-telemetry-live-") as tmp:
        _prepare_environment(Path(tmp) / "athena.db", args)

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base
        from app.services import memory_vectors
        from app.services.telemetry import classify, sender

        # admin-backend's own engine is in-memory under DEV_MODE, so the
        # throwaway file database gets its own engine, handed to the sender
        # through its session-factory seam; the ephemeral-database rule is
        # evaluated against that file (a persistent database, as on an install).
        url = os.environ["DATABASE_URL"]
        engine = create_engine(url, connect_args={"check_same_thread": False})
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        sender.LEASE_SESSION_FACTORY = SessionLocal
        sender.EPHEMERAL_DB_CHECK = lambda: classify.db_is_ephemeral(url)
        memory_vectors.set_background_enabled(False)
        Base.metadata.create_all(bind=engine)
        _seed(SessionLocal)

        outcome = asyncio.run(sender.run_cycle(force=True))
        session = SessionLocal()
        try:
            status = sender.get_status(session, None)
        finally:
            session.close()
        report = {
            "outcome": outcome,
            "endpoint": status["endpoint"],
            "installation_id": status["installation_id"],
            "date": (status["last_attempt_at"] or "")[:10],
            "last_success_at": status["last_success_at"],
            "last_error": status["last_error"],
            "install_class": status["install_class"],
        }
        print(json.dumps(report, indent=2))
        if outcome != "sent" or not status["last_success_at"]:
            print("FAIL: the collector did not accept the ping", file=sys.stderr)
            return 1
        if args.expect_local_row:
            rows = _local_rows(args.collector_dir, status["installation_id"])
            print(f"local D1 rows for {status['installation_id']}: {rows}")
            if rows != 1:
                print("FAIL: expected exactly one row in the local D1", file=sys.stderr)
                return 1
        print("PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
