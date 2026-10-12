"""The service-host rule lives in ``shared.url_safety`` and admin's functions delegate to it.

``validate_service_host`` and ``is_local_host`` were moved, not rewritten: the
table below is the behaviour admin-backend's write boundary has always had. The
admin functions of the same name keep their signatures (and the RFC1918
warning); the parity test loads them by path, so this file runs both in the
orchestrator image's environment and under admin-backend's own lock.
"""
import importlib.util
import pathlib

import pytest

from shared import url_safety

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADMIN_VALIDATORS = ROOT / "admin" / "backend" / "app" / "utils" / "url_validators.py"

ACCEPTED = [
    ("ha.example.invalid", "ha.example.invalid"),
    ("10.20.30.10", "10.20.30.10"),
    ("10.0.0.5", "10.0.0.5"),
    ("172.16.4.2", "172.16.4.2"),
    ("  ha.example.invalid  ", "ha.example.invalid"),
    ("2001:db8::1", "2001:db8::1"),
]

REJECTED = [
    "",
    "   ",
    "homeassistant.svc",
    "ha.default.svc.cluster.local",
    "x.cluster.local",
    "homeassistant.svc.",
    "x.cluster.local.",
    "HomeAssistant.SVC",
    "169.254.169.254",
    "::ffff:169.254.169.254",
    "fe80::1",
    "169.254.10.10",
    "127.0.0.1",
    "::1",
    "224.0.0.1",
    "ff02::1",
    "0.0.0.0",
    "::",
]


def _admin_validators():
    spec = importlib.util.spec_from_file_location("admin_url_validators_under_test", ADMIN_VALIDATORS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("raw,expected", ACCEPTED)
def test_service_host_accepts_and_strips(raw, expected):
    assert url_safety.validate_service_host(raw) == expected


@pytest.mark.parametrize("raw", REJECTED)
def test_service_host_rejects(raw):
    with pytest.raises(ValueError):
        url_safety.validate_service_host(raw)


@pytest.mark.parametrize("raw,expected", ACCEPTED)
def test_admin_validate_host_delegates_accepts(raw, expected):
    assert _admin_validators().validate_host(raw) == expected


@pytest.mark.parametrize("raw", REJECTED)
def test_admin_validate_host_delegates_rejects(raw):
    with pytest.raises(ValueError):
        _admin_validators().validate_host(raw)


def test_admin_validate_host_keeps_its_rfc1918_warning():
    module = _admin_validators()
    seen = []

    class _Log:
        def warning(self, event, **fields):
            seen.append((event, fields))

    module.logger = _Log()
    module.validate_host("10.20.30.4")
    module.validate_host("ha.example.invalid")
    assert [event for event, _ in seen] == ["service_host_is_rfc1918"]


LOCAL_CASES = [
    ("localhost", True),
    ("::1", True),
    ("127.0.0.1", True),
    ("10.1.2.3", True),
    ("10.20.30.19", True),
    ("169.254.1.1", False),
    ("fe80::1", False),
    ("example.com", False),
    ("8.8.8.8", False),
    ("", False),
    (None, False),
]


@pytest.mark.parametrize("host,expected", LOCAL_CASES)
def test_is_local_host_outside_a_pod(monkeypatch, host, expected):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("IN_CLUSTER", raising=False)
    assert url_safety.is_local_host(host) is expected
    assert _admin_validators().is_local_host(host) is expected


@pytest.mark.parametrize("marker", [("KUBERNETES_SERVICE_HOST", "10.96.0.1"), ("IN_CLUSTER", "true")])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.1.2.3"])
def test_is_local_host_is_never_true_inside_a_pod(monkeypatch, marker, host):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("IN_CLUSTER", raising=False)
    monkeypatch.setenv(*marker)
    assert url_safety.is_local_host(host) is False
    assert _admin_validators().is_local_host(host) is False
