"""Gateway channel classification, /v1/voice parity and the gateway-side TTS sinks.

Black-box through the real FastAPI app (`TestClient(gw.app)`) with a fake
orchestrator. The fake emulates the orchestrator's contract: it returns
speech-normalized text when the forwarded `extra_body.interface_type` is
`voice` and the raw text otherwise. Addresses come from RFC 5737 / RFC 3849.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-output-channel")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from gateway.conversation_limiter import NewConversationLimiter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from shared.client_throttle import parse_networks  # noqa: E402
from shared.output_channel import OutputChannel, SPEECH_SINK_MAX_CHARS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

RAW = "Winds 25mph."
SPOKEN = "Winds 25 miles per hour."
RAW_TOKENS = ["Winds ", "25mph."]
SPOKEN_CHUNKS = ["Winds 25 miles ", "per hour."]

PROXY_PEER = "192.0.2.10"
PROXY_NET = "192.0.2.0/24"
SPEECH_NET = "198.51.100.7/32"


def _answer(payload) -> str:
    return SPOKEN if payload["extra_body"].get("interface_type") == "voice" else RAW


class _StreamCtx:
    def __init__(self, tokens):
        self.tokens = tokens

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for token in self.tokens:
            chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
                     "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]}
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"


class _FakeOrchestrator:
    def __init__(self):
        self.payloads = []

    async def post(self, path, json=None, **kwargs):
        self.payloads.append(json)
        response = mock.MagicMock()
        response.raise_for_status = mock.MagicMock()
        response.json.return_value = {"choices": [{"message": {"role": "assistant", "content": _answer(json)}}]}
        return response

    def stream(self, method, path, json=None, timeout=None):
        self.payloads.append(json)
        voice = json["extra_body"].get("interface_type") == "voice"
        return _StreamCtx(SPOKEN_CHUNKS if voice else RAW_TOKENS)

    @property
    def interface_types(self):
        return [p["extra_body"].get("interface_type") for p in self.payloads]


@pytest.fixture
def orchestrator(monkeypatch):
    fake = _FakeOrchestrator()
    monkeypatch.setattr(gw, "orchestrator_client", fake)
    monkeypatch.setattr(gw, "orchestrator_timeout", 5)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "gateway_config", None)
    monkeypatch.setattr(gw, "global_rate_limiter", None)
    monkeypatch.setattr(gw, "new_conversation_limiter", NewConversationLimiter(per_minute=100000))
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", mock.AsyncMock(return_value="kitchen"))
    monkeypatch.setattr(gw, "_SPEECH_CLIENT_NETWORKS", ())
    monkeypatch.setattr(gw, "_TRUSTED_PROXY_NETWORKS", ())
    return fake


@pytest.fixture
def client(orchestrator):
    return TestClient(gw.app)


def _chat_body(stream):
    return {"model": "m", "stream": stream, "messages": [{"role": "user", "content": "weather"}]}


def _responses_body(stream):
    return {"model": "m", "stream": stream, "input": [{"role": "user", "content": "weather"}]}


ROUTES = [
    pytest.param("chat", False, id="chat-nonstream"),
    pytest.param("chat", True, id="chat-stream"),
    pytest.param("responses", False, id="responses-nonstream"),
    pytest.param("responses", True, id="responses-stream"),
]
PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses"}


def _post(client, kind, stream, *, voice=False, **kwargs):
    path = PATHS[kind]
    if voice:
        path = path.replace("/v1/", "/v1/voice/", 1)
    body = (_chat_body if kind == "chat" else _responses_body)(stream)
    return client.post(path, json=body, **kwargs)


def _events(response):
    return [line[6:] for line in response.text.split("\n\n") if line.startswith("data: ")]


def _stream_texts(kind, response):
    """(joined deltas, done text or None) of a streamed answer."""
    events = _events(response)
    if kind == "chat":
        chunks = [json.loads(e) for e in events if e != "[DONE]"]
        return "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks), None
    parsed = [json.loads(e) for e in events]
    deltas = "".join(e["delta"] for e in parsed if e["type"] == "response.output_text.delta")
    done = next(e["text"] for e in parsed if e["type"] == "response.output_text.done")
    return deltas, done


# --- (a) / (b) the server decides ---------------------------------------------------


@pytest.mark.parametrize("kind,stream", ROUTES)
def test_v1_routes_with_no_rules_forward_text(client, orchestrator, kind, stream):
    assert _post(client, kind, stream).status_code == 200
    assert orchestrator.interface_types == ["text"]


@pytest.mark.parametrize("kind,stream", ROUTES)
def test_v1_voice_routes_forward_voice(client, orchestrator, kind, stream):
    assert _post(client, kind, stream, voice=True).status_code == 200
    assert orchestrator.interface_types == ["voice"]


# --- (c) the route template decides, nothing else -----------------------------------------


def test_voice_path_in_the_query_string_is_not_speech(client, orchestrator):
    resp = client.post("/v1/chat/completions?x=/v1/voice/", json=_chat_body(False))
    assert resp.status_code == 200
    assert orchestrator.interface_types == ["text"]


def test_trailing_slash_voice_route_never_classifies_as_speech(client, orchestrator):
    resp = client.post("/v1/voice/responses/", json=_responses_body(False), follow_redirects=False)
    assert resp.status_code in (307, 404)
    assert orchestrator.payloads == []


def test_a_request_without_a_matched_route_is_text():
    request = SimpleNamespace(scope={}, client=SimpleNamespace(host="198.51.100.7"), headers={})
    assert gw._classify_openai_channel(request) is OutputChannel.TEXT


def test_a_route_with_a_non_string_path_is_text():
    request = SimpleNamespace(scope={"route": SimpleNamespace(path=None)}, client=None, headers={})
    assert gw._classify_openai_channel(request) is OutputChannel.TEXT


# --- (d) the client network rule --------------------------------------------------------


def _network_client(monkeypatch, orchestrator, peer, *, speech=SPEECH_NET, trusted=PROXY_NET):
    monkeypatch.setattr(gw, "_SPEECH_CLIENT_NETWORKS", parse_networks(speech))
    monkeypatch.setattr(gw, "_TRUSTED_PROXY_NETWORKS", parse_networks(trusted))
    return TestClient(gw.app, client=(peer, 50000))


def test_trusted_peer_with_a_matching_forwarded_client_is_speech(monkeypatch, orchestrator):
    client = _network_client(monkeypatch, orchestrator, PROXY_PEER)
    _post(client, "chat", False, headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.7"})
    assert orchestrator.interface_types == ["voice"]


def test_untrusted_peer_cannot_borrow_a_matching_forwarded_address(monkeypatch, orchestrator):
    client = _network_client(monkeypatch, orchestrator, "203.0.113.50")
    _post(client, "chat", False, headers={"X-Forwarded-For": "198.51.100.7"})
    assert orchestrator.interface_types == ["text"]


def test_a_spoofed_left_hand_forwarded_address_does_not_count(monkeypatch, orchestrator):
    client = _network_client(monkeypatch, orchestrator, PROXY_PEER)
    _post(client, "chat", False, headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})
    assert orchestrator.interface_types == ["text"]


def test_a_direct_client_in_the_speech_network_is_speech(monkeypatch, orchestrator):
    client = _network_client(monkeypatch, orchestrator, "198.51.100.7", trusted="")
    _post(client, "responses", False)
    assert orchestrator.interface_types == ["voice"]


def test_speech_network_overlapping_the_trusted_proxies_is_dropped_at_startup():
    code = (
        "import sys; sys.path.insert(0, 'src'); from unittest import mock; "
        "sys.modules['prometheus_client'] = mock.MagicMock(); import gateway.main as gw; "
        "print([str(n) for n in gw._SPEECH_CLIENT_NETWORKS], gw._SPEECH_NETWORKS_DROPPED)"
    )
    env = {**os.environ, "OPENAI_SPEECH_CLIENT_NETWORKS": "192.0.2.0/25,198.51.100.0/24",
           "TRUSTED_PROXY_CIDRS": PROXY_NET, "PYTHONPATH": "src"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, env=env)
    assert out.stdout.strip().splitlines()[-1] == "['198.51.100.0/24'] 1", out.stderr[-500:]


def test_startup_logs_report_counts_never_addresses(monkeypatch):
    monkeypatch.setattr(gw, "OPENAI_SPEECH_CLIENT_NETWORKS", "198.51.100.0/24, not-a-network")
    monkeypatch.setattr(gw, "_SPEECH_NETWORKS_DROPPED", 2)
    monkeypatch.setattr(gw, "_SPEECH_CLIENT_NETWORKS", parse_networks("198.51.100.0/24"))
    monkeypatch.setattr(gw, "_TRUSTED_PROXY_NETWORKS", ())
    log = mock.MagicMock()
    monkeypatch.setattr(gw, "logger", log)
    gw._log_speech_network_config()
    assert [c.args[0] for c in log.error.call_args_list] == [
        "openai_speech_client_networks_invalid", "openai_speech_networks_overlap_trusted_proxies",
    ]
    assert all(set(c.kwargs) == {"count"} for c in log.error.call_args_list)
    assert [c.args[0] for c in log.warning.call_args_list] == ["openai_speech_networks_without_trusted_proxy"]


def test_the_channel_log_names_the_route_template_and_no_address(monkeypatch, orchestrator):
    client = _network_client(monkeypatch, orchestrator, "198.51.100.7", trusted="")
    log = mock.MagicMock()
    monkeypatch.setattr(gw, "logger", log)
    _post(client, "chat", False)
    call = next(c for c in log.info.call_args_list if c.args and c.args[0] == "openai_output_channel")
    assert call.kwargs == {"channel": "speech", "rule": "client_network", "route": "/v1/chat/completions"}


# --- (e) a client cannot choose ----------------------------------------------------------


def test_client_sent_interface_type_is_ignored(client, orchestrator):
    body = _chat_body(False) | {"extra_body": {"interface_type": "voice"}, "interface_type": "voice"}
    assert client.post("/v1/chat/completions", json=body).status_code == 200
    assert orchestrator.interface_types == ["text"]


# --- (f) the breaker-open fallback -----------------------------------------------------


class _Ollama:
    async def chat(self, **kwargs):
        yield {"done": True, "message": {"content": RAW}, "eval_count": 3}


@pytest.fixture
def open_breaker(monkeypatch, orchestrator):
    breaker = mock.MagicMock()
    breaker.can_execute = mock.AsyncMock(return_value=False)
    breaker.state = mock.MagicMock(value="open")
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})
    monkeypatch.setattr(gw, "ollama_client", _Ollama())
    monkeypatch.setattr(gw, "_log_metric_to_db", mock.AsyncMock())


@pytest.mark.parametrize("voice,expected", [(True, SPOKEN), (False, RAW)], ids=["speech", "text"])
def test_breaker_open_fallback_follows_the_channel(client, open_breaker, orchestrator, voice, expected):
    resp = _post(client, "chat", False, voice=voice)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == expected
    assert orchestrator.payloads == [], "the orchestrator was never called"


# --- (g) required keyword -----------------------------------------------------------------


def test_payload_builder_requires_a_channel():
    request = gw.ChatCompletionRequest(model="m", messages=[gw.ChatMessage(role="user", content="hi")])
    with pytest.raises(TypeError):
        gw._orchestrator_openai_payload(request, "kitchen", stream=False)


# --- (h) /v1/voice is the same route in every way except the channel ---------------------


@pytest.mark.parametrize("kind,stream", ROUTES)
def test_voice_alias_has_the_same_status_headers_and_shape(client, orchestrator, kind, stream):
    plain = _post(client, kind, stream)
    voice = _post(client, kind, stream, voice=True)
    assert plain.status_code == voice.status_code == 200
    assert plain.headers["content-type"] == voice.headers["content-type"]
    if stream:
        plain_kinds = [json.loads(e).get("type", json.loads(e).get("object")) if e != "[DONE]" else e for e in _events(plain)]
        voice_kinds = [json.loads(e).get("type", json.loads(e).get("object")) if e != "[DONE]" else e for e in _events(voice)]
        assert plain_kinds == voice_kinds
        if kind == "responses":
            assert voice_kinds[0] == "response.created" and voice_kinds[-1] == "response.completed"
        else:
            assert voice_kinds[-1] == "[DONE]"
    else:
        assert plain.json().keys() == voice.json().keys()


# --- (i) the Responses stream round trip --------------------------------------------------


def test_voice_responses_stream_deltas_and_done_text_are_the_spoken_form(client, orchestrator):
    deltas, done = _stream_texts("responses", _post(client, "responses", True, voice=True))
    assert deltas == done == SPOKEN


def test_plain_responses_stream_keeps_the_written_form(client, orchestrator):
    deltas, done = _stream_texts("responses", _post(client, "responses", True))
    assert deltas == done == RAW


# --- the Responses stream parses with the OpenAI client's typed event models -------------------

CANONICAL_RESPONSES_EVENTS = [
    "response.created", "response.in_progress", "response.output_item.added",
    "response.content_part.added", "response.output_text.delta", "response.output_text.delta",
    "response.output_text.done", "response.content_part.done", "response.output_item.done",
    "response.completed",
]


@pytest.mark.parametrize("voice", [False, True], ids=["v1", "v1_voice"])
def test_responses_stream_parses_with_the_openai_typed_event_models(client, orchestrator, voice):
    from openai.types.responses import ResponseCompletedEvent, ResponseStreamEvent, ResponseTextDeltaEvent
    from pydantic import TypeAdapter

    adapter = TypeAdapter(ResponseStreamEvent)
    raw = [json.loads(e) for e in _events(_post(client, "responses", True, voice=voice))]
    typed = [adapter.validate_python(e) for e in raw]  # raises on a missing or mistyped field
    assert [e.type for e in typed] == CANONICAL_RESPONSES_EVENTS
    assert [e.sequence_number for e in typed] == list(range(len(typed)))
    assert isinstance(typed[-1], ResponseCompletedEvent)
    parts = [e.part for e in typed if e.type in ("response.content_part.added", "response.content_part.done")]
    parts.append(typed[-1].response.output[0].content[0])
    assert all(p.logprobs == [] and p.annotations == [] for p in parts)
    assert typed[-1].response.status == "completed"
    assert typed[-1].response.output[0].status == "completed"
    assert typed[-1].response.output[0].content[0].text == (SPOKEN if voice else RAW)
    deltas = [e for e in typed if isinstance(e, ResponseTextDeltaEvent)]
    assert "".join(d.delta for d in deltas) == (SPOKEN if voice else RAW)


# --- a failed stream never returns the exception's text ------------------------------------


class _BrokenStream:
    async def __aenter__(self):
        raise RuntimeError("secret-internal-detail")

    async def __aexit__(self, *exc):
        return False


def test_a_failed_orchestrator_stream_returns_a_generic_error(client, orchestrator, monkeypatch):
    monkeypatch.setattr(orchestrator, "stream", lambda *a, **k: _BrokenStream(), raising=False)
    log = mock.MagicMock()
    monkeypatch.setattr(gw, "logger", log)
    resp = _post(client, "chat", True)
    assert resp.status_code == 200
    assert "secret-internal-detail" not in resp.text
    assert gw.STREAM_ERROR_MESSAGE in resp.text
    assert [c for c in log.error.call_args_list if c.kwargs.get("error") == "RuntimeError"], "class logged server-side"


def test_the_responses_stream_error_event_is_generic_and_well_formed(client, orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(gw.asyncio, "create_task", boom)
    log = mock.MagicMock()
    monkeypatch.setattr(gw, "logger", log)
    resp = _post(client, "responses", True)
    assert "secret-internal-detail" not in resp.text
    last = json.loads(_events(resp)[-1])
    assert last == {"type": "error", "code": "server_error", "message": gw.STREAM_ERROR_MESSAGE,
                    "param": None, "sequence_number": last["sequence_number"]}
    assert [c for c in log.error.call_args_list if c.kwargs.get("error") == "RuntimeError"]


def test_the_ollama_stream_error_line_is_generic():
    assert "secret" not in gw._stream_error_line()
    assert json.loads(gw._stream_error_line()[6:]) == {"error": gw.STREAM_ERROR_MESSAGE}


# --- (j) models ---------------------------------------------------------------------------


class _AdminClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        response = mock.MagicMock()
        response.status_code = 200
        response.raise_for_status = mock.MagicMock()
        response.json.return_value = []
        return response


def test_models_alias_is_ungated_and_identical(monkeypatch, client):
    monkeypatch.setattr(gw, "API_KEY", "a-configured-gateway-key")
    monkeypatch.setattr(gw.httpx, "AsyncClient", _AdminClient)
    plain, voice = client.get("/v1/models"), client.get("/v1/voice/models")
    assert plain.status_code == voice.status_code == 200
    assert plain.json() == voice.json()


def test_the_completion_routes_stay_gated_when_a_key_is_set(monkeypatch, client, orchestrator):
    monkeypatch.setattr(gw, "API_KEY", "a-configured-gateway-key")
    for kind in ("chat", "responses"):
        assert _post(client, kind, False).status_code == 401
        assert _post(client, kind, False, voice=True).status_code == 401
    assert orchestrator.payloads == []


# --- (l) route registration parity ----------------------------------------------------------


def _route(path):
    return next(r for r in gw.app.routes if getattr(r, "path", None) == path)


@pytest.mark.parametrize("plain", ["/v1/chat/completions", "/v1/responses", "/v1/models"])
def test_voice_alias_routes_match_their_twins(plain):
    twin, alias = _route(plain), _route(plain.replace("/v1/", "/v1/voice/", 1))
    assert alias.response_model == twin.response_model
    assert alias.methods == twin.methods
    assert alias.endpoint is twin.endpoint
    assert [d.call for d in alias.dependant.dependencies] == [d.call for d in twin.dependant.dependencies]


# --- the HA conversation route is always speech ---------------------------------------------------


class _QueryClient:
    def __init__(self):
        self.payload = None

    async def post(self, path, json=None, **kwargs):
        self.payload = json
        response = mock.MagicMock()
        response.raise_for_status = mock.MagicMock()
        response.json.return_value = {"answer": SPOKEN, "session_id": "s"}
        return response


def _chat_request():
    return gw.ChatCompletionRequest(model="m", messages=[gw.ChatMessage(role="user", content="weather")])


def test_route_to_orchestrator_names_the_voice_channel_with_a_literal(monkeypatch, orchestrator):
    query_client = _QueryClient()
    monkeypatch.setattr(gw, "orchestrator_client", query_client)
    asyncio.run(gw.route_to_orchestrator(_chat_request(), device_id="kitchen"))
    assert query_client.payload["interface_type"] == "voice"


def test_route_to_orchestrator_fallback_to_ollama_is_rendered_for_speech(monkeypatch, open_breaker):
    result = asyncio.run(gw.route_to_orchestrator(_chat_request(), device_id="kitchen"))
    assert result.choices[0].message.content == SPOKEN


# --- (k) the sinks --------------------------------------------------------------------------


@pytest.fixture
def ha(monkeypatch):
    flags = {}
    monkeypatch.setattr(gw, "get_feature_flag", mock.AsyncMock(side_effect=lambda name, default=False: flags.get(name, default)))
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", mock.AsyncMock(return_value="kitchen"))
    monkeypatch.setattr(gw, "device_session_mgr", SimpleNamespace(
        get_session_for_device=mock.AsyncMock(return_value=None),
        update_session_for_device=mock.AsyncMock(),
    ))
    monkeypatch.setattr(gw, "detect_simple_command", mock.AsyncMock(return_value=("lights", {})))
    monkeypatch.setattr(gw, "execute_simple_command", mock.AsyncMock(return_value=RAW))
    return flags


def _ha_speech(monkeypatch, ha, case):
    if case == "fastpath":
        ha["ha_simple_command_fastpath"] = True
    elif case == "prerouted_simple":
        ha["ha_intent_prerouting"] = True
        monkeypatch.setattr(gw, "classify_intent", mock.AsyncMock(return_value="SIMPLE"))
        monkeypatch.setattr(gw, "handle_simple_intent", mock.AsyncMock(return_value=RAW))
    elif case == "prerouted_home":
        ha["ha_intent_prerouting"] = True
        monkeypatch.setattr(gw, "classify_intent", mock.AsyncMock(return_value="HOME"))
    else:
        message = SimpleNamespace(content=SPOKEN)
        reply = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        monkeypatch.setattr(gw, "route_to_orchestrator", mock.AsyncMock(return_value=(reply, "session-1")))
    resp = TestClient(gw.app).post("/ha/conversation", json={"text": "weather", "device_id": "d1"})
    assert resp.status_code == 200
    return resp.json()["response"]["speech"]["plain"]["speech"]


HA_RETURNS = ["fastpath", "prerouted_simple", "prerouted_home", "orchestrator"]


@pytest.mark.parametrize("case", HA_RETURNS)
def test_ha_conversation_speaks_the_rendered_text_on_every_return(monkeypatch, ha, case):
    assert _ha_speech(monkeypatch, ha, case) == SPOKEN


def _real_wyoming_bridge() -> ModuleType:
    """gateway.wyoming_bridge imported against the real `wyoming` package.

    The package is optional (not in the production gateway image) but is in the
    behaviour tests' locked requirements, so CI exercises the real import."""
    pytest.importorskip("wyoming")
    import gateway.wyoming_bridge as module

    return module


def test_the_wyoming_package_is_in_the_locked_test_requirements():
    """Guards the real-package tests above from being skipped everywhere."""
    lock = (REPO_ROOT / "src/orchestrator/requirements-test.txt").read_text()
    assert re.search(r"^wyoming==", lock, re.MULTILINE)


def test_the_wyoming_bridge_imports_against_the_real_package():
    module = _real_wyoming_bridge()
    from wyoming.server import AsyncEventHandler

    assert module.WYOMING_AVAILABLE is True
    assert issubclass(module.AthenaWyomingHandler, AsyncEventHandler)


class _TtsClient:
    posted = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        type(self).posted = json
        return SimpleNamespace(status_code=200, content=b"", text="")


def _run_wyoming_synthesize(monkeypatch, text):
    module = _real_wyoming_bridge()
    monkeypatch.setattr(module, "EVENTS_AVAILABLE", False)
    monkeypatch.setattr(module.httpx, "AsyncClient", _TtsClient)
    handler = SimpleNamespace(
        session_id="s", interface_name="home_assistant", state=module.WyomingSessionState.IDLE,
        _get_voice_manager=mock.AsyncMock(return_value=None), _schedule_follow_up=mock.AsyncMock(),
        tts_cancelled=False, pipeline_start_time=None,
    )

    async def _drain():
        async for _ in module.AthenaWyomingHandler._synthesize(handler, text):
            pass

    asyncio.run(_drain())
    return _TtsClient.posted["text"]


def test_wyoming_synthesize_posts_rendered_text(monkeypatch):
    assert _run_wyoming_synthesize(monkeypatch, RAW) == SPOKEN


def test_livekit_synthesize_passes_rendered_text_to_curl(monkeypatch):
    from gateway.livekit_integration import TTSClient

    seen = {}

    def _fake_run(args, **kwargs):
        seen["payload"] = json.loads(args[args.index("-d") + 1])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    asyncio.run(TTSClient("http://tts.invalid").synthesize(RAW))
    assert seen["payload"] == {"text": SPOKEN}


def test_a_huge_input_is_capped_before_rendering_and_logged_by_length(monkeypatch, caplog):
    from gateway.livekit_integration import TTSClient

    seen = {}

    def _fake_run(args, **kwargs):
        seen["payload"] = json.loads(args[args.index("-d") + 1])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    huge = "secretword " * 10_000
    with caplog.at_level(logging.INFO, logger="shared.output_channel"):
        asyncio.run(TTSClient("http://tts.invalid").synthesize(huge))
    assert len(seen["payload"]["text"]) <= SPEECH_SINK_MAX_CHARS
    record = next(r for r in caplog.records if r.getMessage() == "speech_sink_rendered")
    assert (record.sink, record.text_length, record.truncated) == ("livekit", len(huge), True)
    assert "secretword" not in caplog.text


def test_wyoming_and_ha_sinks_cap_and_log_lengths_only(monkeypatch, caplog, ha):
    huge = "secretword " * 10_000
    with caplog.at_level(logging.INFO, logger="shared.output_channel"):
        posted = _run_wyoming_synthesize(monkeypatch, huge)
        spoken = gw._ha_response_payload(huge, "en")["speech"]["plain"]["speech"]
    assert len(posted) <= SPEECH_SINK_MAX_CHARS and len(spoken) <= SPEECH_SINK_MAX_CHARS
    sinks = {r.sink for r in caplog.records if r.getMessage() == "speech_sink_rendered"}
    assert sinks == {"wyoming", "ha_conversation"}
    assert "secretword" not in caplog.text


def test_this_file_has_at_least_twelve_ids(request):
    ids = [i for i in request.session.items if i.module is request.module]
    assert len(ids) >= 12
