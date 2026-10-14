"""The gateway speaks a closed apology when the orchestrator can't answer.

An orchestrator 5xx, a transport error or a timeout is answered 200 with
SPOKEN_FAILURE_TEXT (HA and OpenAI-SDK clients speak it and stop retrying); a
4xx raises as it always has. No upstream or exception text reaches a response
body or a log record, every failure is counted as an error, and only 502/503/504
and transport errors count toward the circuit breaker.

The orchestrator is a real HTTP peer: an httpx.MockTransport for the seam the
other gateway tests use, plus a real loopback server (asyncio.start_server)
and a closed loopback port, so the status line, the body and the connection
errors are the ones httpx actually produces.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-spoken-failure")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from gateway.circuit_breaker import CircuitBreaker, CircuitState  # noqa: E402
from gateway.conversation_limiter import NewConversationLimiter  # noqa: E402
from shared.output_channel import OutputChannel  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_REAL_ROUTE_TO_OLLAMA = gw.route_to_ollama
SECRET = "SECRET-TOKEN-XYZ"
PHRASE = "Sorry, I couldn't finish that. Please try again in a moment."
STATUSES = (500, 502, 503, 504)
TRANSPORT = ("connect_error", "read_timeout")
FAILURES = tuple(f"status_{s}" for s in STATUSES) + TRANSPORT
CLIENT_TIMEOUT = 0.4


# ---------------------------------------------------------------------------
# The orchestrator peer
# ---------------------------------------------------------------------------


class _Loopback:
    """A real HTTP server on 127.0.0.1 in its own thread: answers every request
    with `status` and a body carrying the planted secret, or reads the request
    and never answers (`status=None`)."""

    def __init__(self, status):
        self.status = status
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            if length:
                await reader.readexactly(length)
            if self.status is None:
                await asyncio.sleep(30)
                return
            body = json.dumps({"detail": f"upstream says {SECRET}"}).encode()
            writer.write(
                f"HTTP/1.1 {self.status} Upstream\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        async def shutdown():
            self.server.close()
            for task in asyncio.all_tasks() - {asyncio.current_task()}:
                task.cancel()
            await asyncio.sleep(0)
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.thread.join(5)
        self.loop.close()


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _mock_client(failure: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "connect_error":
            raise httpx.ConnectError(f"refused {SECRET}", request=request)
        if failure == "read_timeout":
            raise httpx.ReadTimeout(f"timed out {SECRET}", request=request)
        return httpx.Response(int(failure.split("_")[1]), json={"detail": f"upstream says {SECRET}"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://orchestrator.invalid")


@pytest.fixture(params=("mock", "loopback"))
def seam(request, monkeypatch):
    """Returns install(failure): points the gateway's orchestrator client at the peer."""
    servers = []

    def install(failure: str):
        monkeypatch.setattr(gw, "orchestrator_timeout", CLIENT_TIMEOUT)
        if request.param == "mock":
            client = _mock_client(failure)
        elif failure == "connect_error":
            client = httpx.AsyncClient(base_url=_closed_port_url(), timeout=CLIENT_TIMEOUT)
        else:
            status = None if failure == "read_timeout" else int(failure.split("_")[1])
            server = _Loopback(status)
            servers.append(server)
            client = httpx.AsyncClient(base_url=server.base_url, timeout=CLIENT_TIMEOUT)
        monkeypatch.setattr(gw, "orchestrator_client", client)

    yield install
    for server in servers:
        server.stop()


class _Counter:
    def __init__(self):
        self.incs = []

    def labels(self, **labels):
        outer = self

        class _Child:
            def inc(self, amount=1):
                outer.incs.append(tuple(sorted(labels.items())))

        return _Child()

    def errors(self):
        return [i for i in self.incs if ("status", "error") in i]


class _RecordingLogger:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def log(*args, **kwargs):
            self.records.append((level, args, kwargs))
        return log

    def text(self) -> str:
        return repr(self.records)


@pytest.fixture
def rig(monkeypatch, caplog):
    counter = _Counter()
    logger = _RecordingLogger()
    monkeypatch.setattr(gw, "request_counter", counter)
    monkeypatch.setattr(gw, "logger", logger)
    monkeypatch.setattr(gw, "new_conversation_limiter", NewConversationLimiter(per_minute=100000))
    monkeypatch.setattr(gw, "global_rate_limiter", None)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": False})
    monkeypatch.setattr(
        gw.speaker_room, "resolve_speaker_room",
        mock.AsyncMock(return_value=gw.speaker_room.SpeakerRoom("kitchen", "map")),
    )
    sessions = SimpleNamespace(
        get_session_for_device=mock.AsyncMock(return_value=None),
        update_session_for_device=mock.AsyncMock(),
    )
    monkeypatch.setattr(gw, "device_session_mgr", sessions)

    async def _flag(name, default=False):
        return False

    monkeypatch.setattr(gw, "get_feature_flag", _flag)
    # The Ollama fallback is down too, and says so with the secret.
    monkeypatch.setattr(gw, "route_to_ollama", mock.AsyncMock(side_effect=RuntimeError(f"ollama down {SECRET}")))
    caplog.set_level("DEBUG")
    return SimpleNamespace(counter=counter, logger=logger, caplog=caplog, sessions=sessions)


def _assert_no_secret(rig, response_text: str) -> None:
    assert SECRET not in response_text
    assert SECRET not in rig.logger.text()
    assert SECRET not in rig.caplog.text


def _chat_body(stream: bool) -> dict:
    return {"model": "gpt-4", "messages": [{"role": "user", "content": "what is the weather"}], "stream": stream}


def _responses_body(stream: bool) -> dict:
    return {"model": "gpt-4", "input": "what is the weather", "stream": stream}


def _client() -> TestClient:
    return TestClient(gw.app)


# ---------------------------------------------------------------------------
# Every failure, every endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", FAILURES)
def test_chat_completions_non_stream_speaks_the_phrase(rig, seam, failure):
    seam(failure)
    response = _client().post("/v1/chat/completions", json=_chat_body(False))

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == PHRASE
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors(), "a failure is counted as an error"
    assert any(r[0] == "error" and r[1] == ("orchestrator_failure",) for r in rig.logger.records)


@pytest.mark.parametrize("failure", FAILURES)
def test_voice_chat_completions_stream_speaks_one_delta_then_done(rig, seam, failure):
    seam(failure)
    response = _client().post("/v1/voice/chat/completions", json=_chat_body(True))

    assert response.status_code == 200
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    deltas = [json.loads(e)["choices"][0]["delta"]["content"] for e in events[:-1]]
    assert deltas == [PHRASE]
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors()


@pytest.mark.parametrize("failure", FAILURES)
def test_text_stream_keeps_the_generic_error_line(rig, seam, failure):
    seam(failure)
    response = _client().post("/v1/chat/completions", json=_chat_body(True))

    assert response.status_code == 200
    assert response.text == gw._stream_error_line()
    assert PHRASE not in response.text
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors()


@pytest.mark.parametrize("failure", FAILURES)
def test_responses_api_non_stream_speaks_the_phrase(rig, seam, failure):
    seam(failure)
    response = _client().post("/v1/responses", json=_responses_body(False))

    assert response.status_code == 200
    assert response.json()["output_text"] == PHRASE
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors()


@pytest.mark.parametrize("failure", FAILURES)
def test_voice_responses_stream_speaks_the_phrase(rig, seam, failure):
    seam(failure)
    response = _client().post("/v1/voice/responses", json=_responses_body(True))

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
    assert deltas == [PHRASE]
    assert events[-1]["type"] == "response.completed"
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors()


@pytest.mark.parametrize("failure", FAILURES)
def test_ha_conversation_speaks_the_phrase_through_the_tuple_path(rig, seam, failure):
    seam(failure)
    response = _client().post("/ha/conversation", json={"text": "what is the weather", "device_id": "kitchen"})

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["speech"]["plain"]["speech"] == PHRASE
    assert body["continue_conversation"] is False
    assert body["conversation_id"].startswith("session-")
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors()


@pytest.mark.parametrize("failure", FAILURES)
def test_route_to_orchestrator_returns_a_tuple_and_keeps_the_device_session(rig, seam, failure):
    seam(failure)
    request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])

    completion, session_id = asyncio.run(
        gw.route_to_orchestrator(request, device_id="kitchen", session_id="own-session", return_session_id=True)
    )

    assert completion.choices[0].message.content == PHRASE
    assert session_id == "own-session"


def test_a_working_ollama_fallback_answers_a_transport_error(rig, seam, monkeypatch):
    seam("connect_error")
    answer = gw._failure_completion(
        gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])
    )
    answer.choices[0].message.content = "From Ollama."
    monkeypatch.setattr(gw, "route_to_ollama", mock.AsyncMock(return_value=answer))

    response = _client().post("/v1/chat/completions", json=_chat_body(False))

    assert response.json()["choices"][0]["message"]["content"] == "From Ollama."


def test_the_real_ollama_fallback_failing_speaks_the_phrase_and_logs_no_text(rig, seam, monkeypatch):
    class _DownOllama:
        def chat(self, **kwargs):
            raise RuntimeError(f"ollama down {SECRET}")

    seam("connect_error")
    monkeypatch.setattr(gw, "route_to_ollama", _REAL_ROUTE_TO_OLLAMA)
    monkeypatch.setattr(gw, "ollama_client", _DownOllama())
    monkeypatch.setattr(gw, "get_voice_response_limits", mock.AsyncMock(return_value={"max_tokens": 100}))

    response = _client().post("/v1/chat/completions", json=_chat_body(False))

    assert response.json()["choices"][0]["message"]["content"] == PHRASE
    _assert_no_secret(rig, response.text)


def test_a_5xx_does_not_try_ollama(rig, seam):
    seam("status_503")
    _client().post("/v1/chat/completions", json=_chat_body(False))

    gw.route_to_ollama.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4xx is unchanged
# ---------------------------------------------------------------------------


@pytest.fixture(params=(400, 401, 404, 422))
def client_error(request):
    return request.param


def test_a_4xx_still_raises_and_leaks_nothing(rig, monkeypatch, client_error):
    server = _Loopback(client_error)
    try:
        monkeypatch.setattr(gw, "orchestrator_client", httpx.AsyncClient(base_url=server.base_url, timeout=CLIENT_TIMEOUT))
        for path, body in (("/v1/chat/completions", _chat_body(False)), ("/v1/responses", _responses_body(False))):
            response = _client().post(path, json=body)
            assert response.status_code == 502
            assert response.json() == {"detail": gw.ORCHESTRATOR_ERROR_DETAIL}
            _assert_no_secret(rig, response.text)
        ha = _client().post("/ha/conversation", json={"text": "hi", "device_id": "kitchen"})
        assert ha.status_code == 502
        _assert_no_secret(rig, ha.text)
    finally:
        server.stop()


def test_a_4xx_never_counts_toward_the_breaker(rig, monkeypatch):
    breaker = CircuitBreaker(failure_threshold=1)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})
    server = _Loopback(422)
    try:
        monkeypatch.setattr(gw, "orchestrator_client", httpx.AsyncClient(base_url=server.base_url, timeout=CLIENT_TIMEOUT))
        assert _client().post("/v1/chat/completions", json=_chat_body(False)).status_code == 502
    finally:
        server.stop()

    assert breaker.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# The catch-alls
# ---------------------------------------------------------------------------


def test_catch_alls_speak_on_the_speech_channel_and_stay_generic_on_text(rig, monkeypatch):
    monkeypatch.setattr(
        gw.speaker_room, "extract_device_marker", mock.Mock(side_effect=RuntimeError(f"boom {SECRET}"))
    )

    for path, body, key in (
        ("/v1/voice/chat/completions", _chat_body(False), lambda j: j["choices"][0]["message"]["content"]),
        ("/v1/voice/responses", _responses_body(False), lambda j: j["output_text"]),
    ):
        response = _client().post(path, json=body)
        assert response.status_code == 200
        assert key(response.json()) == PHRASE
        _assert_no_secret(rig, response.text)

    for path, body in (("/v1/chat/completions", _chat_body(False)), ("/v1/responses", _responses_body(False))):
        response = _client().post(path, json=body)
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}
        _assert_no_secret(rig, response.text)


def test_ha_conversation_catch_all_speaks_the_phrase(rig, monkeypatch):
    monkeypatch.setattr(gw, "_session_for_device", mock.AsyncMock(side_effect=RuntimeError(f"boom {SECRET}")))

    response = _client().post("/ha/conversation", json={"text": "hi", "device_id": "kitchen"})

    assert response.status_code == 200
    assert response.json()["response"]["speech"]["plain"]["speech"] == PHRASE
    _assert_no_secret(rig, response.text)
    assert rig.counter.errors()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def _drive_breaker(monkeypatch, failure: str, calls: int) -> CircuitBreaker:
    async def go():
        breaker = CircuitBreaker(failure_threshold=3)
        monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
        monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})
        monkeypatch.setattr(gw, "orchestrator_client", _mock_client(failure))
        monkeypatch.setattr(gw, "route_to_ollama", mock.AsyncMock(side_effect=RuntimeError("down")))
        request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])
        for _ in range(calls):
            answer = await gw.route_chat_completion_to_orchestrator(request, channel=OutputChannel.SPEECH)
            assert answer.choices[0].message.content == PHRASE
        return breaker

    return asyncio.run(go())


def test_a_deterministic_500_never_opens_the_breaker(rig, monkeypatch):
    assert _drive_breaker(monkeypatch, "status_500", 10).state is CircuitState.CLOSED


@pytest.mark.parametrize("failure", ("status_502", "status_503", "status_504", "connect_error", "read_timeout"))
def test_availability_failures_open_the_breaker(rig, monkeypatch, failure):
    breaker = _drive_breaker(monkeypatch, failure, 3)

    assert breaker.state is CircuitState.OPEN


def test_an_open_breaker_with_ollama_down_still_speaks_the_phrase(rig, monkeypatch):
    async def go():
        breaker = CircuitBreaker(failure_threshold=1)
        await breaker.record_failure()
        monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
        monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})
        request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])
        return await gw.route_chat_completion_to_orchestrator(request, channel=OutputChannel.TEXT)

    answer = asyncio.run(go())

    assert answer.choices[0].message.content == PHRASE
    _assert_no_secret(rig, answer.model_dump_json())


# ---------------------------------------------------------------------------
# Music routes
# ---------------------------------------------------------------------------


class _FakeSocket:
    def __init__(self, reply):
        self.reply = reply
        self.received = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, _):
        return None

    async def recv(self):
        self.received += 1
        return json.dumps({"server_version": "1"} if self.received == 1 else self.reply)


def _install_music(monkeypatch, reply=None, raises=None):
    import websockets

    def connect(*args, **kwargs):
        if raises is not None:
            raise raises
        return _FakeSocket(reply)

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(gw, "_fetch_ma_auth_token", mock.AsyncMock(return_value=None))


def _music(monkeypatch, rig, route, *, reply=None, raises=None):
    _install_music(monkeypatch, reply=reply, raises=raises)
    if route == "search":
        request = gw.MusicSearchRequest(query="abba")
        call = gw.music_search
    else:
        request = gw.MusicPlayRequest(player_id="p1", uri="library://track/1")
        call = gw.music_play
    with pytest.raises(gw.HTTPException) as caught:
        asyncio.run(call(request))
    assert SECRET not in str(caught.value.detail)
    assert SECRET not in rig.logger.text()
    return caught.value


@pytest.mark.parametrize("route,phrase", (("search", "Music search failed."), ("play", "Music playback failed.")))
def test_music_upstream_error_is_a_closed_phrase(rig, monkeypatch, route, phrase):
    error = _music(monkeypatch, rig, route, reply={"error": True, "details": f"MA says {SECRET}"})

    assert (error.status_code, error.detail) == (500, phrase)


@pytest.mark.parametrize("route", ("search", "play"))
def test_music_websocket_error_is_service_unavailable(rig, monkeypatch, route):
    import websockets

    error = _music(monkeypatch, rig, route, raises=websockets.exceptions.WebSocketException(f"ws {SECRET}"))

    assert (error.status_code, error.detail) == (502, "Music service unavailable.")


@pytest.mark.parametrize("route", ("search", "play"))
def test_music_unexpected_error_is_a_closed_phrase(rig, monkeypatch, route):
    error = _music(monkeypatch, rig, route, raises=RuntimeError(f"boom {SECRET}"))

    assert (error.status_code, error.detail) == (500, "Music request failed.")
    assert any("error_type" in kwargs for _, _, kwargs in rig.logger.records), "logged with its class"


# ---------------------------------------------------------------------------
# Models (ATHENA-258)
# ---------------------------------------------------------------------------


def _config(monkeypatch, *, fallback: str, default: str):
    monkeypatch.setattr(
        gw, "_get_athena_config",
        lambda: SimpleNamespace(gateway_fallback_model=fallback, athena_default_model=default),
    )


def test_fallback_model_prefers_the_gateway_setting_then_the_default(monkeypatch):
    _config(monkeypatch, fallback="mine:1b", default="default:4b")
    assert gw._fallback_model() == "mine:1b"

    _config(monkeypatch, fallback="", default="default:4b")
    assert gw._fallback_model() == "default:4b"


def test_openai_names_resolve_to_an_installed_model_and_others_pass_through(monkeypatch):
    _config(monkeypatch, fallback="", default="default:4b")

    assert gw._resolve_ollama_model("gpt-4") == "default:4b"
    assert gw._resolve_ollama_model("gpt-4-32k") == "default:4b"
    assert gw._resolve_ollama_model("gpt-4o-mini") == "default:4b"
    assert gw._resolve_ollama_model("") == "default:4b"
    assert gw._resolve_ollama_model(None) == "default:4b"
    assert gw._resolve_ollama_model("gpt-3.5-turbo") == "phi3:mini"
    for installed in ("llama3.2:3b", "qwen3:4b", "mistral"):
        assert gw._resolve_ollama_model(installed) == installed


class _FakeOllama:
    def __init__(self):
        self.models = []

    async def chat(self, *, model, messages, temperature, stream, **kwargs):
        self.models.append(model)
        yield {"message": {"content": "ok"}, "done": not stream}
        if stream:
            yield {"done": True}


@pytest.mark.parametrize("requested,expected", (("gpt-4", "default:4b"), ("llama3.2:3b", "llama3.2:3b")))
def test_route_to_ollama_and_stream_response_use_the_resolved_model(monkeypatch, requested, expected):
    _config(monkeypatch, fallback="", default="default:4b")
    fake = _FakeOllama()
    monkeypatch.setattr(gw, "ollama_client", fake)
    monkeypatch.setattr(gw, "_log_metric_to_db", mock.AsyncMock())
    monkeypatch.setattr(
        gw, "get_voice_response_limits", mock.AsyncMock(return_value={"max_tokens": 100})
    )
    request = gw.ChatCompletionRequest(model=requested, messages=[gw.ChatMessage(role="user", content="hi")])

    async def go():
        await gw.route_to_ollama(request, channel=OutputChannel.TEXT)
        return [chunk async for chunk in gw.stream_response(request)]

    chunks = asyncio.run(go())

    assert fake.models == [expected, expected]
    assert chunks[-1] == "data: [DONE]\n\n"


def test_config_fields_read_the_environment(monkeypatch):
    from shared.config import AthenaConfig

    monkeypatch.delenv("GATEWAY_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("ATHENA_DEFAULT_MODEL", raising=False)
    assert AthenaConfig(_env_file=None).gateway_fallback_model == ""
    assert AthenaConfig(_env_file=None).athena_default_model

    monkeypatch.setenv("GATEWAY_FALLBACK_MODEL", "fb:1b")
    monkeypatch.setenv("ATHENA_DEFAULT_MODEL", "def:2b")
    config = AthenaConfig(_env_file=None)
    assert (config.gateway_fallback_model, config.athena_default_model) == ("fb:1b", "def:2b")


# ---------------------------------------------------------------------------
# Error accounting and config validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ("status_500", "status_503", "connect_error"))
def test_a_turn_answered_with_the_phrase_is_counted_as_an_error_not_a_success(rig, seam, failure):
    seam(failure)
    _client().post("/v1/chat/completions", json=_chat_body(False))
    _client().post("/v1/responses", json=_responses_body(False))
    _client().post("/ha/conversation", json={"text": "hi", "device_id": "kitchen"})

    for endpoint in ("chat_completions", "responses_api", "ha_conversation"):
        assert (("endpoint", endpoint), ("status", "error")) in rig.counter.incs
        assert (("endpoint", endpoint), ("status", "success")) not in rig.counter.incs


def test_a_real_answer_is_still_counted_as_a_success(rig, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

    monkeypatch.setattr(
        gw, "orchestrator_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://orchestrator.invalid"),
    )
    _client().post("/v1/chat/completions", json=_chat_body(False))

    assert (("endpoint", "chat_completions"), ("status", "success")) in rig.counter.incs


def test_a_4xx_increments_the_orchestrator_error_counter_but_not_the_breaker(rig, monkeypatch):
    breaker = CircuitBreaker(failure_threshold=1)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})
    monkeypatch.setattr(gw, "orchestrator_client", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(422, json={})), base_url="http://orchestrator.invalid"))

    assert _client().post("/v1/chat/completions", json=_chat_body(False)).status_code == 502

    assert (("endpoint", "orchestrator"), ("status", "error")) in rig.counter.incs
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("value", ("bad model", "x" * 129, "m;rm -rf", "über:1b", "a\nb"))
@pytest.mark.parametrize("field,env,default", (
    ("athena_default_model", "ATHENA_DEFAULT_MODEL", None),
    ("gateway_fallback_model", "GATEWAY_FALLBACK_MODEL", ""),
))
def test_an_invalid_model_tag_warns_once_and_falls_back(monkeypatch, caplog, value, field, env, default):
    from shared.config import AthenaConfig

    monkeypatch.delenv("ATHENA_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("GATEWAY_FALLBACK_MODEL", raising=False)
    expected = default if default is not None else AthenaConfig(_env_file=None).athena_default_model
    monkeypatch.setenv(env, value)
    with caplog.at_level("WARNING", logger="shared.config"):
        config = AthenaConfig(_env_file=None)

    assert getattr(config, field) == expected
    warnings = [r for r in caplog.records if "model_tag_invalid" in r.getMessage()]
    assert len(warnings) == 1
    assert value not in warnings[0].getMessage()


def test_a_valid_model_tag_is_stripped_and_kept(monkeypatch):
    from shared.config import AthenaConfig

    monkeypatch.setenv("GATEWAY_FALLBACK_MODEL", "  hf.co/org/model-GGUF:Q4_K_M  ")
    assert AthenaConfig(_env_file=None).gateway_fallback_model == "hf.co/org/model-GGUF:Q4_K_M"
