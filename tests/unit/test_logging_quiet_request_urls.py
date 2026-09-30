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
    names = QUIET + ("uvicorn", "uvicorn.error", "athena.pre")
    saved = {
        name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers),
               logging.getLogger(name).disabled, logging.getLogger(name).propagate)
        for name in names
    }
    yield
    for name, (level, handlers, disabled, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.disabled = disabled
        logger.propagate = propagate


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


# ---------------------------------------------------------------------------
# quiet_uvicorn_log_config / StripQueryStringFilter (admin-backend's
# python main.py launch)
# ---------------------------------------------------------------------------

def test_quiet_uvicorn_log_config_levels():
    pytest.importorskip("uvicorn.config")
    from shared.logging_config import quiet_uvicorn_log_config

    logging.config.dictConfig(quiet_uvicorn_log_config())
    assert not logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
    assert logging.getLogger("uvicorn.error").isEnabledFor(logging.INFO)


def test_quiet_uvicorn_log_config_keeps_existing_loggers():
    """disable_existing_loggers stays False: a logger set up before the
    dictConfig (e.g. by an import) still emits afterwards."""
    pytest.importorskip("uvicorn.config")
    from shared.logging_config import quiet_uvicorn_log_config

    records = []

    class _Keep(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    pre = logging.getLogger("athena.pre")
    pre.addHandler(_Keep())
    pre.setLevel(logging.INFO)
    logging.config.dictConfig(quiet_uvicorn_log_config())
    pre.info("still here")
    assert records == ["still here"]


def test_quiet_uvicorn_log_config_filters_both_handlers():
    pytest.importorskip("uvicorn.config")
    from shared.logging_config import quiet_uvicorn_log_config

    config = quiet_uvicorn_log_config()
    assert config["disable_existing_loggers"] is False
    assert config["filters"]["strip_query"]["()"] == "shared.logging_config.StripQueryStringFilter"
    for handler in ("default", "access"):
        assert "strip_query" in config["handlers"][handler]["filters"], handler


def _record(msg, args):
    return logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)


def test_filter_strips_query_from_tuple_args():
    from shared.logging_config import StripQueryStringFilter

    record = _record('%s - "WebSocket %s" 403', (("127.0.0.1", 5), "/ws/admin-jarvis?token=x"))
    assert StripQueryStringFilter().filter(record) is True
    assert record.args == (("127.0.0.1", 5), "/ws/admin-jarvis")
    assert isinstance(record.args, tuple)
    assert "token" not in record.getMessage()


def test_filter_strips_query_from_the_message():
    from shared.logging_config import StripQueryStringFilter

    record = _record("GET /a?b=1 done", None)
    StripQueryStringFilter().filter(record)
    assert record.getMessage() == "GET /a done"


def test_filter_leaves_mapping_args_alone():
    from shared.logging_config import StripQueryStringFilter

    record = _record("%(path)s", ({"path": "/a?b=1"},))
    assert StripQueryStringFilter().filter(record) is True
    assert record.args == {"path": "/a?b=1"}


def test_request_url_loggers_is_public():
    from shared import logging_config

    assert logging_config.REQUEST_URL_LOGGERS == ("uvicorn.access", "httpx", "httpcore")
    assert logging_config._REQUEST_URL_LOGGERS is logging_config.REQUEST_URL_LOGGERS
    logging_config.quiet_request_url_loggers()
    for name in logging_config.REQUEST_URL_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
