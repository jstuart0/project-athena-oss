"""The Admin Jarvis WebSocket client connects with a single-use ws-ticket
only: no fallback puts the session JWT in the WebSocket URL (stdlib only).
admin-backend closes any non-ticket token with 4001."""
from __future__ import annotations

import re
from pathlib import Path

ADMIN_JARVIS = Path(__file__).resolve().parents[2] / "admin" / "frontend" / "admin-jarvis.js"


def _text():
    return ADMIN_JARVIS.read_text()


def test_no_session_jwt_fallback():
    text = _text()
    hits = []
    if re.search(r"wsToken\s*=\s*sessionToken", text):
        hits.append("wsToken = sessionToken")
    if "?token=${sessionToken}" in text:
        hits.append("?token=${sessionToken}")
    if re.search(r"\blegacyUrl\b", text):
        hits.append("legacyUrl")
    assert not hits, hits


def test_the_ticket_mint_is_the_only_token_source():
    assert _text().count("fetch('/api/auth/ws-ticket'") == 1


_ALLOWED_SESSION_TOKEN_USES = (
    re.compile(r"const\s+sessionToken\s*=\s*localStorage\.getItem\('auth_token'\)"),
    re.compile(r"if\s*\(\s*sessionToken\s*\)"),
    re.compile(r"'Bearer '\s*\+\s*sessionToken\b"),
)


def session_token_misuses(text):
    """Every use of `sessionToken` other than its declaration, the `if`
    guarding the mint, and the mint's Bearer header. Anything else (a
    renamed fallback variable, a URL) is how the JWT would reach a
    WebSocket URL."""
    allowed_spans = [m.span() for rx in _ALLOWED_SESSION_TOKEN_USES for m in rx.finditer(text)]
    misuses = []
    for m in re.finditer(r"\bsessionToken\b", text):
        if not any(a <= m.start() < b for a, b in allowed_spans):
            misuses.append(text.count("\n", 0, m.start()) + 1)
    return misuses


def test_session_token_is_only_the_mint_credential():
    text = _text()
    assert len(re.findall(r"\bsessionToken\b", text)) >= 3
    assert session_token_misuses(text) == []


def test_misuse_detector_self_test():
    good = "const sessionToken = localStorage.getItem('auth_token') || '';\nif (sessionToken) {\n  f({ Authorization: 'Bearer ' + sessionToken });\n}"
    assert session_token_misuses(good) == []
    for bad in ("x = sessionToken;", "const u = `${base}?token=${sessionToken}`;", "wsTicket = sessionToken"):
        assert session_token_misuses(good + "\n" + bad) == [5], bad

