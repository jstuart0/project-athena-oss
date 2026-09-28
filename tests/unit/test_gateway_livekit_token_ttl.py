"""ATHENA-69 D27/P5.4 -- browser-facing LiveKit tokens expire in 30 minutes
by default; server-side Athena tokens keep their prior 24h TTL explicitly.

The `livekit`/`livekit-api` SDK isn't installed in this environment (nor
required to be, for a unit-test run -- see test_gateway_livekit_deps.py's
same subprocess-isolation approach), so each test runs LiveKitService in a
fresh subprocess with a minimal fake `livekit.api` module injected into
sys.modules before `gateway.livekit_service` is imported. The fake
AccessToken reproduces just enough of the real SDK's builder pattern
(.with_identity/.with_name/.with_ttl/.with_grants/.to_jwt) to encode a real,
decodable JWT with nbf/exp claims -- the same claims the real SDK's
`with_ttl(timedelta(...))` produces -- so the test can assert on
`exp - nbf` without a network call or the real SDK installed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
SUBPROCESS_TIMEOUT_S = 30

_FAKE_LIVEKIT_SDK = '''
import types, time as _time

class _FakeVideoGrants:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

class _FakeAccessToken:
    def __init__(self, api_key, api_secret):
        self._api_key = api_key
        self._api_secret = api_secret or "test-secret"
        self._identity = None
        self._name = None
        self._ttl = None
        self._grants = None

    def with_identity(self, identity):
        self._identity = identity
        return self

    def with_name(self, name):
        self._name = name
        return self

    def with_ttl(self, ttl):
        self._ttl = ttl
        return self

    def with_grants(self, grants):
        self._grants = grants
        return self

    def to_jwt(self):
        import jwt as _jwt
        now = int(_time.time())
        exp = now + int(self._ttl.total_seconds())
        payload = {
            "iss": self._api_key,
            "sub": self._identity,
            "nbf": now,
            "exp": exp,
            "name": self._name,
        }
        return _jwt.encode(payload, self._api_secret, algorithm="HS256")

_lk = types.ModuleType("livekit")
_lk_api = types.ModuleType("livekit.api")
_lk_rtc = types.ModuleType("livekit.rtc")
_lk_api.AccessToken = _FakeAccessToken
_lk_api.VideoGrants = _FakeVideoGrants
_lk.api = _lk_api
_lk.rtc = _lk_rtc
sys.modules["livekit"] = _lk
sys.modules["livekit.api"] = _lk_api
sys.modules["livekit.rtc"] = _lk_rtc
'''


def _run(body: str, env_extra: dict | None = None) -> dict:
    """Run `body` in a fresh subprocess with the fake livekit SDK injected
    and prometheus_client stubbed. Returns the parsed RESULT: line."""
    preamble = (
        "import sys, json, types, unittest.mock as mock\n"
        'sys.modules["prometheus_client"] = mock.MagicMock()\n'
        + _FAKE_LIVEKIT_SDK
    )
    code = preamble + "\n" + body

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    env.pop("ATHENA_DEBUG_MODE", None)
    env.pop("LIVEKIT_USER_TOKEN_TTL_MINUTES", None)
    if env_extra:
        env.update(env_extra)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )

    result = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
            break

    assert result is not None, (
        f"subprocess produced no RESULT: line (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return result


_TOKEN_SCRIPT = """
import gateway.livekit_service as lks
service = lks.LiveKitService(livekit_url="wss://lk.example", api_key="k", api_secret="s")
{call}
import jwt as _jwt
claims = _jwt.decode(token, options={{"verify_signature": False}})
print("RESULT:" + json.dumps({{"ttl_seconds": claims["exp"] - claims["nbf"]}}))
"""


def test_browser_token_default_ttl_30_minutes():
    """No ttl_minutes argument (as every browser-facing call site passes) --
    the default comes from AthenaConfig.livekit_user_token_ttl_minutes,
    which defaults to 30 minutes when LIVEKIT_USER_TOKEN_TTL_MINUTES is
    unset."""
    body = _TOKEN_SCRIPT.format(
        call='token = service.generate_room_token(room_name="r", participant_name="User")'
    )
    result = _run(body)
    assert abs(result["ttl_seconds"] - 1800) <= 5


@pytest.mark.parametrize(
    "env_value,expected_seconds",
    [
        pytest.param("5", 300, id="5-minutes"),
        pytest.param("0", 60, id="clamped-low-to-1-minute"),
        pytest.param("99999", 86400, id="clamped-high-to-1440-minutes"),
    ],
)
def test_browser_token_ttl_configurable(env_value, expected_seconds):
    body = _TOKEN_SCRIPT.format(
        call='token = service.generate_room_token(room_name="r", participant_name="User")'
    )
    result = _run(body, env_extra={"LIVEKIT_USER_TOKEN_TTL_MINUTES": env_value})
    assert abs(result["ttl_seconds"] - expected_seconds) <= 5


def test_athena_token_keeps_24h():
    """The two server-side Athena call sites pass ttl_minutes=24 * 60
    explicitly and must be unaffected by LIVEKIT_USER_TOKEN_TTL_MINUTES."""
    body = _TOKEN_SCRIPT.format(
        call=(
            'token = service.generate_room_token('
            'room_name="r", participant_name="Athena", ttl_minutes=24 * 60)'
        )
    )
    result = _run(body, env_extra={"LIVEKIT_USER_TOKEN_TTL_MINUTES": "5"})
    assert abs(result["ttl_seconds"] - 86400) <= 5
