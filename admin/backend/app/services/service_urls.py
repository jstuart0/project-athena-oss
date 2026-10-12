"""Where admin-backend finds the gateway and the orchestrator.

The one reader of ``GATEWAY_URL`` and ``ORCHESTRATOR_URL``. The value is read
when the accessor is called, with the trailing slash removed, and falls back to
the local-development address when the variable is unset or empty. Nothing else
in admin-backend names these variables (``tests/test_service_urls_accessor.py``).
"""
import os

DEFAULT_GATEWAY_URL = "http://localhost:8000"
DEFAULT_ORCHESTRATOR_URL = "http://localhost:8001"


def _read(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


def gateway_url() -> str:
    return _read("GATEWAY_URL", DEFAULT_GATEWAY_URL)


def orchestrator_url() -> str:
    return _read("ORCHESTRATOR_URL", DEFAULT_ORCHESTRATOR_URL)
