"""The admin UI's automation detail escapes stored values it renders as
HTML (stdlib only). owner_type and status come from the database; a row
written by an old anonymous caller could hold markup."""
from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "admin" / "frontend" / "voice-automations.js"


def raw_interpolations(text, fields):
    """`${auto.<field>}` interpolations not wrapped in escapeHtml(...)."""
    return [m.group(0) for m in re.finditer(r"\$\{auto\.(" + "|".join(fields) + r")\}", text)]


def test_owner_type_and_status_are_escaped():
    text = JS.read_text()
    assert raw_interpolations(text, ["owner_type", "status"]) == []
    assert "${escapeHtml(auto.owner_type)}" in text
    assert "${escapeHtml(auto.status)}" in text


def test_detector_self_test():
    assert raw_interpolations("<b>${auto.owner_type}</b>", ["owner_type"]) == ["${auto.owner_type}"]
    assert raw_interpolations("<b>${escapeHtml(auto.owner_type)}</b>", ["owner_type"]) == []
    assert raw_interpolations("${auto.owner_type === 'guest' ? 1 : 2}", ["owner_type"]) == []
