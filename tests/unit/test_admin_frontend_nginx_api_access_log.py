"""admin-frontend's nginx doesn't write /api requests to its access log
(stdlib only). Admin API URLs can carry query strings, and those lines
would otherwise land in the container log."""
from __future__ import annotations

import re
from pathlib import Path

NGINX_CONF = Path(__file__).resolve().parents[2] / "admin" / "frontend" / "nginx.conf"


def location_block(text, path):
    """Body of the first `location <path> { ... }` block (brace-matched,
    comments stripped)."""
    text = re.sub(r"#[^\n]*", "", text)
    m = re.search(r"\blocation\s+" + re.escape(path) + r"\s*\{", text)
    assert m, f"no location {path} block"
    depth, i = 1, m.end()
    while depth and i < len(text):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def _has_access_log_off(block):
    return re.search(r"(^|[;{}\s])access_log\s+off\s*;", block) is not None


def test_api_location_turns_the_access_log_off():
    assert _has_access_log_off(location_block(NGINX_CONF.read_text(), "/api"))


def test_positive_control_root_location_keeps_its_access_log():
    assert not _has_access_log_off(location_block(NGINX_CONF.read_text(), "/"))
