"""Access and client request lines (full URLs with query strings, which can
carry a location or an API key) aren't logged at INFO.

uvicorn configures its own loggers before it imports the app, and the app's
import runs configure_logging, so the order below is the one a pod runs.
"""
from __future__ import annotations

import logging
import logging.config
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

QUIET = ("uvicorn.access", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_levels():
    saved = {name: logging.getLogger(name).level for name in QUIET}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_request_url_loggers_are_warning_after_uvicorn_config():
    uvicorn_config = pytest.importorskip("uvicorn.config")
    from shared.logging_config import configure_logging

    logging.config.dictConfig(uvicorn_config.LOGGING_CONFIG)
    assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.INFO  # uvicorn's own default
    configure_logging("test-quiet-urls")
    for name in QUIET:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
    assert not logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)


def test_uvicorn_error_logger_stays_at_info():
    """Positive control: startup/shutdown lines still log."""
    uvicorn_config = pytest.importorskip("uvicorn.config")
    from shared.logging_config import configure_logging

    logging.config.dictConfig(uvicorn_config.LOGGING_CONFIG)
    configure_logging("test-quiet-urls")
    assert logging.getLogger("uvicorn.error").isEnabledFor(logging.INFO)


def test_payload_keys_logs_names_only():
    from shared.logging_config import payload_keys

    assert payload_keys({"location": "12 Example St", "api_key": "secret", 3: "x"}) == ["3", "api_key", "location"]
    assert payload_keys(["12 Example St"]) == []
    assert payload_keys(None) == []
