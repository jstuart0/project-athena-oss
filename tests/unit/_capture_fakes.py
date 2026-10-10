"""Test doubles for conversation capture.

``InMemoryCaptureStore`` follows the writer statement's rules (analytics gate,
debug gate, partition guard, whole-turn withholding with the sticky window) so
route tests can assert on stored rows without a database. The real statement
is exercised on Postgres in admin/backend/tests/integration/.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional

from shared import conversation_capture as cc


class InMemoryCaptureStore:
    def __init__(self, *, analytics: bool = True, debug: bool = False, block: Optional[asyncio.Event] = None,
                 fail: Optional[BaseException] = None):
        self.analytics = analytics
        self.debug = debug
        self.block = block
        self.fail = fail
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.turns: List[Dict[str, Any]] = []
        self.reports: List[Dict[str, Any]] = []
        self.written: List[cc.CaptureRecord] = []
        self.closed = False

    async def read_flags(self) -> cc.CaptureFlags:
        return cc.CaptureFlags(analytics=self.analytics, debug=self.debug)

    async def write(self, record: cc.CaptureRecord) -> str:
        if self.block is not None:
            await self.block.wait()
        if self.fail is not None:
            raise self.fail
        if not self.analytics:
            return cc.RESULT_DISABLED
        conv = self.conversations.get(record.session_id)
        if conv is not None and conv["session_class"] not in (None, record.session_class):
            return cc.RESULT_PARTITION_MISMATCH
        if conv is None:
            conv = self.conversations[record.session_id] = {"session_class": record.session_class, "turns": []}
        conv["session_class"] = conv["session_class"] or record.session_class
        prior = conv["turns"][-2:]
        sticky = any(t["outcome"] == cc.OUTCOME_WITHHELD for t in prior)
        withheld = bool(record.text_withheld) or sticky
        row = {
            "session_id": record.session_id,
            "turn_number": len(conv["turns"]) + 1,
            "query_text": "" if withheld else record.query_text,
            "response_text": None if withheld else record.response_text,
            "text_withheld": withheld,
            "outcome": cc.OUTCOME_WITHHELD if withheld else record.outcome,
            "route": record.route,
            "channel": record.channel,
            "audience_class": record.audience_class,
            "session_class": record.session_class,
            "user_mode": record.user_mode,
            "source": "debug" if self.debug else "analytics",
            "intent": record.intent,
            "debug_detail": record.debug_detail if self.debug and not withheld else None,
            "model_used": record.model_used if self.debug and not withheld else None,
        }
        conv["turns"].append(row)
        self.turns.append(row)
        self.written.append(record)
        return cc.RESULT_WRITTEN

    async def report(self, status: Mapping[str, Any]) -> None:
        self.reports.append(dict(status))

    async def close(self) -> None:
        self.closed = True


class RecordingSink:
    """Stands in for ``CaptureSink`` and keeps every enqueued record."""

    def __init__(self):
        self.records: List[cc.CaptureRecord] = []

    def enqueue(self, record: cc.CaptureRecord) -> str:
        self.records.append(record)
        return cc.RESULT_QUEUED
