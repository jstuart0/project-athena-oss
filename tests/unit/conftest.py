"""Shared fixtures for tests/unit."""
from __future__ import annotations

import sys

import pytest
import structlog

_LAZY_PROXY_TYPE = type(structlog.get_logger())


def _uncache_lazy_loggers() -> None:
    """Drop every module-level structlog proxy's cached bound logger.

    shared.logging_config.configure_logging() sets
    cache_logger_on_first_use=True, so a module's `logger` freezes the
    processors list that was current at its first use. If one test module
    uses mode_service.main's logger and then restores the structlog config,
    the next module's capture_logs() edits the restored list while that
    logger keeps writing to the old one, and captures nothing. Uncached, the
    proxy rebinds to whatever config is current at its next use.
    """
    for module in list(sys.modules.values()):
        candidate = getattr(module, "logger", None)
        if isinstance(candidate, _LAZY_PROXY_TYPE):
            candidate.__dict__.pop("bind", None)


@pytest.fixture(scope="module")
def isolated_structlog():
    """Module-scoped structlog isolation for modules that import a service's
    main (which calls configure_logging() at import time): snapshot the
    config before the module's first test, restore it after its last, and
    uncache lazy loggers at both edges."""
    snapshot = structlog.get_config()
    _uncache_lazy_loggers()
    yield
    structlog.configure(**snapshot)
    _uncache_lazy_loggers()
