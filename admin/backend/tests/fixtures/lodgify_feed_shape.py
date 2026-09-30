"""The shape of a real Lodgify iCal export: nine date-only windows (masked
booking spans, 1-night masked slices on the span boundaries, and a long
Closed Period), with a fresh UID on every fetch unless UIDs are given.
Summaries are synthetic masks, not real guest names."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional, Sequence

LODGIFY_SHAPE_WINDOWS = (
    (date(2026, 3, 1), date(2026, 5, 31), "J*** D**"),
    (date(2026, 5, 31), date(2026, 6, 1), "A**** B****"),
    (date(2026, 6, 1), date(2026, 7, 5), "M***** K***"),
    (date(2026, 7, 5), date(2026, 7, 6), "A**** B****"),
    (date(2026, 7, 6), date(2026, 8, 31), "R**** T*****"),
    (date(2026, 8, 31), date(2026, 9, 1), "A**** B****"),
    (date(2026, 9, 1), date(2026, 9, 30), "S**** L***"),
    (date(2026, 9, 30), date(2026, 10, 1), "A**** B****"),
    (date(2026, 10, 1), date(2027, 1, 1), "Closed Period"),
)

SLICE_0930 = (date(2026, 9, 30), date(2026, 10, 1))
CLOSED_PERIOD = (date(2026, 10, 1), date(2027, 1, 1))


def ical(events: Sequence[dict]) -> str:
    """events: dicts with `start`/`end` (date or an iCal DATE-TIME string),
    `summary`, and an optional `uid` (None omits the UID line)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//lodgify-shape//EN"]
    for e in events:
        lines.append("BEGIN:VEVENT")
        if e.get("uid") is not None:
            lines.append(f"UID:{e['uid']}")
        for prop, value in (("DTSTART", e["start"]), ("DTEND", e["end"])):
            if isinstance(value, date):
                lines.append(f"{prop};VALUE=DATE:{value:%Y%m%d}")
            else:
                lines.append(f"{prop}:{value}")
        lines.append(f"SUMMARY:{e['summary']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def build_lodgify_shape_feed(uids: Optional[Sequence[str]] = None) -> tuple[str, list[str]]:
    """(feed text, the UIDs used, in window order)."""
    if uids is None:
        uids = [f"{uuid.uuid4()}@lodgify.example" for _ in LODGIFY_SHAPE_WINDOWS]
    uids = list(uids)
    events = [
        {"uid": uid, "start": start, "end": end, "summary": summary}
        for uid, (start, end, summary) in zip(uids, LODGIFY_SHAPE_WINDOWS)
    ]
    return ical(events), uids
