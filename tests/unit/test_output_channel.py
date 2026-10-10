"""shared.output_channel: the one seam that says whether an answer is spoken.

Addresses come from the documentation ranges (RFC 5737 / RFC 3849) only.
"""
from __future__ import annotations

import ast
import inspect
import logging
import sys
from pathlib import Path
from typing import get_args

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.datastructures import Headers

from shared import output_channel as oc
from shared.client_throttle import parse_networks, read_forwarded_for
from shared.output_channel import (
    OutputChannel,
    channel_for_interface_type,
    classify_openai_caller,
    interface_type_for_channel,
    render_answer,
    render_for_channel,
    renders_spoken_answer,
    without_networks_overlapping,
)

from .test_tts_normalizer import CORPUS

REPO = Path(__file__).resolve().parents[2]
# `orchestrator` is not an installed package (only `shared` is): reach it the way the sibling tests do.
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
SPEECH, TEXT = OutputChannel.SPEECH, OutputChannel.TEXT


# --- Mapping and state parity -------------------------------------------------


def _query_request():
    from orchestrator.main import QueryRequest

    return QueryRequest


def test_mapping_covers_exactly_the_interface_types_the_request_accepts():
    accepted = set(get_args(_query_request().model_fields["interface_type"].annotation))
    assert accepted == {"voice", "text", "chat"}
    assert {t: channel_for_interface_type(t) for t in accepted} == {
        "voice": SPEECH, "text": TEXT, "chat": TEXT,
    }


@pytest.mark.parametrize("value", [None, "", "VOICE", "speech", "sms", "x" * 5000])
def test_unknown_interface_type_is_text(value):
    assert channel_for_interface_type(value) is TEXT


def test_interface_type_for_channel_round_trips():
    assert interface_type_for_channel(SPEECH) == "voice"
    assert interface_type_for_channel(TEXT) == "text"
    assert channel_for_interface_type(interface_type_for_channel(SPEECH)) is SPEECH


def test_orchestrator_state_and_query_request_interface_type_agree():
    from orchestrator.state import OrchestratorState

    request_field = _query_request().model_fields["interface_type"]
    state_field = OrchestratorState.model_fields["interface_type"]
    assert get_args(state_field.annotation) == get_args(request_field.annotation)
    assert state_field.default == request_field.default == "voice"


# --- Config field ---------------------------------------------------------------


def test_speech_networks_config_defaults_empty_and_reads_env(monkeypatch):
    from .test_config import _TestConfig

    monkeypatch.delenv("OPENAI_SPEECH_CLIENT_NETWORKS", raising=False)
    assert _TestConfig().openai_speech_client_networks == ""
    monkeypatch.setenv("OPENAI_SPEECH_CLIENT_NETWORKS", "198.51.100.0/24")
    assert _TestConfig().openai_speech_client_networks == "198.51.100.0/24"


# --- Rendering --------------------------------------------------------------------


def test_text_channel_is_identity():
    assert render_for_channel("Winds 12 mph", TEXT) == "Winds 12 mph"
    assert render_answer("Winds 12 mph", "chat") == "Winds 12 mph"
    assert render_answer("Winds 12 mph", "text") == "Winds 12 mph"


def test_speech_channel_expands_units():
    assert render_for_channel("12 mph", SPEECH) == "12 miles per hour"
    assert render_answer("12 mph", "voice") == "12 miles per hour"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_input_passes_through(value):
    assert render_for_channel(value, SPEECH) == value
    assert render_answer(value, "voice") == value


def test_speech_input_is_capped_before_the_normalizer(monkeypatch):
    seen = []
    monkeypatch.setattr(oc, "normalize_for_tts", lambda text: seen.append(len(text)) or text)
    render_for_channel("a" * 100_000, SPEECH)
    assert seen == [oc.SPEECH_SINK_MAX_CHARS] == [5000]
    render_for_channel("a" * 100_000, TEXT)
    assert seen == [5000]


def test_the_fallback_after_a_normalizer_error_is_still_capped(monkeypatch):
    def boom(text):
        raise ValueError(text)

    monkeypatch.setattr(oc, "normalize_for_tts", boom)
    long_text = "a" * (oc.SPEECH_SINK_MAX_CHARS + 500)
    assert render_for_channel(long_text, SPEECH) == "a" * oc.SPEECH_SINK_MAX_CHARS
    assert render_for_channel(long_text, TEXT) == long_text, "text is never capped"


def test_render_never_raises_and_logs_the_class_name_only(monkeypatch, caplog):
    secret = "the secret answer 12 mph"

    def boom(text):
        raise ValueError(text)

    monkeypatch.setattr(oc, "normalize_for_tts", boom)
    with caplog.at_level(logging.ERROR, logger=oc.logger.name):
        assert render_for_channel(secret, SPEECH) == secret
    (record,) = [r for r in caplog.records if r.getMessage() == "tts_normalization_failed"]
    baseline = set(vars(logging.makeLogRecord({})))
    assert set(vars(record)) - baseline - {"message", "asctime"} == {"error"}
    assert record.error == "ValueError"
    assert secret not in caplog.text


def test_double_render_equals_single_render_over_the_phase_1_corpus():
    assert len(CORPUS) >= 300
    failures = [
        (text, once, twice)
        for text in CORPUS
        for once in [render_for_channel(text, SPEECH)]
        for twice in [render_for_channel(once, SPEECH)]
        if twice != once
    ]
    assert not failures, failures[:5]


# --- Decorator ----------------------------------------------------------------------


class _Req(BaseModel):
    query: str = "q"
    interface_type: str = "voice"


class _Resp(BaseModel):
    answer: str
    note: str = "n"


def _handler():
    async def handle(request: _Req) -> _Resp:
        return _Resp(answer="Winds 25mph.")

    return handle


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_decorator_keeps_the_signature_and_name():
    original = _handler()
    wrapped = renders_spoken_answer(original)
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert wrapped.__name__ == original.__name__
    assert inspect.iscoroutinefunction(wrapped)


def test_decorator_renders_for_voice_and_leaves_chat_raw():
    wrapped = renders_spoken_answer(_handler())
    assert _run(wrapped(request=_Req(interface_type="voice"))).answer == "Winds 25 miles per hour."
    assert _run(wrapped(request=_Req(interface_type="chat"))).answer == "Winds 25mph."


def test_decorator_reads_the_request_positionally_too():
    wrapped = renders_spoken_answer(_handler())
    assert _run(wrapped(_Req(interface_type="voice"))).answer == "Winds 25 miles per hour."


def test_decorator_lets_http_exceptions_through_unchanged():
    async def refuse(request: _Req):
        raise HTTPException(status_code=403, detail="nope")

    with pytest.raises(HTTPException) as caught:
        _run(renders_spoken_answer(refuse)(request=_Req()))
    assert (caught.value.status_code, caught.value.detail) == (403, "nope")


@pytest.mark.parametrize("make", [lambda: Response("12 mph"), lambda: StreamingResponse(iter(["12 mph"]))])
def test_decorator_passes_responses_through_as_the_same_object(make):
    result = make()

    async def handle(request: _Req):
        return result

    assert _run(renders_spoken_answer(handle)(request=_Req())) is result


def test_decorator_under_a_fastapi_route_renders_and_keeps_the_response_model():
    app = FastAPI()

    @app.post("/q", response_model=_Resp)
    @renders_spoken_answer
    async def handle(request: _Req):
        return _Resp(answer="Winds 25mph.")

    route = next(r for r in app.routes if getattr(r, "path", None) == "/q")
    assert route.response_model is _Resp
    client = TestClient(app)
    assert client.post("/q", json={"interface_type": "voice"}).json()["answer"] == "Winds 25 miles per hour."
    assert client.post("/q", json={"interface_type": "chat"}).json()["answer"] == "Winds 25mph."


# --- Classifier ------------------------------------------------------------------------

PROXY = "192.0.2.0/24"
PROXY_PEER = "192.0.2.10"
SPEECH_32 = "198.51.100.7/32"


def _decide(*, voice_path=False, peer=None, xff=None, speech=SPEECH_32, trusted=PROXY):
    decision = classify_openai_caller(
        voice_path=voice_path,
        peer=peer,
        forwarded_for=xff,
        speech_networks=parse_networks(speech),
        trusted_proxies=parse_networks(trusted),
    )
    return decision.channel, decision.rule


CLASSIFIER_ROWS = [
    pytest.param(dict(voice_path=True, peer="203.0.113.50"), (SPEECH, "voice_path"), id="voice_path_beats_non_matching_network"),
    pytest.param(dict(voice_path=True, peer=None, speech=""), (SPEECH, "voice_path"), id="voice_path_with_no_peer_or_rules"),
    pytest.param(dict(peer=PROXY_PEER, xff="203.0.113.9, 198.51.100.7"), (SPEECH, "client_network"), id="trusted_peer_rightmost_untrusted_hop_matches"),
    pytest.param(dict(peer=PROXY_PEER, xff="198.51.100.7, 203.0.113.9"), (TEXT, "default"), id="trusted_peer_spoofed_left_hop_ignored"),
    pytest.param(dict(peer="203.0.113.9", xff="198.51.100.7"), (TEXT, "default"), id="untrusted_peer_with_matching_xff"),
    pytest.param(dict(peer="198.51.100.7", trusted=""), (SPEECH, "client_network"), id="direct_untrusted_peer_matches"),
    pytest.param(dict(peer="2001:db8:1::5", speech="2001:db8:1::/48", trusted=""), (SPEECH, "client_network"), id="ipv6_peer_in_ipv6_network"),
    pytest.param(dict(peer="::ffff:198.51.100.7", trusted=""), (SPEECH, "client_network"), id="ipv4_mapped_peer"),
    pytest.param(dict(peer="198.51.100.7", speech=""), (TEXT, "default"), id="empty_speech_list"),
    pytest.param(dict(peer=PROXY_PEER, xff=""), (TEXT, "default"), id="empty_xff_trusted_peer"),
    pytest.param(dict(peer=PROXY_PEER, xff="not-an-address, ???"), (TEXT, "default"), id="garbage_xff"),
    pytest.param(dict(peer=None), (TEXT, "default"), id="peer_none"),
    pytest.param(dict(peer="garbage", trusted=""), (TEXT, "default"), id="unparseable_peer"),
    pytest.param(dict(peer="198.51.100.200", speech="198.51.100.0/24", trusted=""), (SPEECH, "client_network"), id="slash_24_hit"),
    pytest.param(dict(peer="198.51.100.8", trusted=""), (TEXT, "default"), id="slash_32_miss"),
    pytest.param(dict(peer="2001:db8:2::5", speech="2001:db8:1::/48", trusted=""), (TEXT, "default"), id="ipv6_peer_outside_network"),
]


@pytest.mark.parametrize("kwargs,expected", CLASSIFIER_ROWS)
def test_classifier_table(kwargs, expected):
    assert _decide(**kwargs) == expected


def test_classifier_floor():
    assert len(CLASSIFIER_ROWS) >= 14


def test_two_forwarded_for_lines_are_joined_before_classifying():
    headers = Headers(raw=[
        (b"x-forwarded-for", b"198.51.100.99"),
        (b"x-forwarded-for", b"203.0.113.9, 198.51.100.7"),
    ])
    joined = read_forwarded_for(headers)
    assert joined == "198.51.100.99, 203.0.113.9, 198.51.100.7"
    assert _decide(peer=PROXY_PEER, xff=joined) == (SPEECH, "client_network")
    only_first_line = read_forwarded_for(Headers(raw=[(b"x-forwarded-for", b"198.51.100.99")]))
    assert _decide(peer=PROXY_PEER, xff=only_first_line) == (TEXT, "default")


def test_classifier_composes_client_throttle_with_a_literal_trust_cf_false():
    tree = ast.parse(Path(oc.__file__).read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "classify_openai_caller")
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "resolve_rate_client"]
    assert len(calls) == 1
    (trust_cf,) = [k for k in calls[0].keywords if k.arg == "trust_cf"]
    assert isinstance(trust_cf.value, ast.Constant) and trust_cf.value.value is False
    assert not any(isinstance(n, ast.Import) and n.names[0].name == "ipaddress" for n in ast.walk(tree))


# --- Overlap with trusted proxies ----------------------------------------------------------


def test_overlapping_speech_networks_are_dropped_with_a_count_only(caplog):
    speech = parse_networks("192.0.2.0/25, 198.51.100.0/24, 2001:db8::/32")
    trusted = parse_networks("192.0.2.0/24")
    with caplog.at_level(logging.ERROR, logger=oc.logger.name):
        kept, dropped = without_networks_overlapping(speech, trusted)
    assert dropped == 1
    assert [str(n) for n in kept] == ["198.51.100.0/24", "2001:db8::/32"]
    (record,) = caplog.records
    assert record.getMessage() == "openai_speech_networks_overlap_trusted_proxies"
    assert record.count == 1
    assert "192.0.2" not in caplog.text and "198.51.100" not in caplog.text


def test_no_overlap_keeps_everything_and_logs_nothing(caplog):
    speech = parse_networks("198.51.100.0/24")
    with caplog.at_level(logging.ERROR, logger=oc.logger.name):
        kept, dropped = without_networks_overlapping(speech, parse_networks("192.0.2.0/24"))
    assert (kept, dropped) == (speech, 0)
    assert not caplog.records


# --- Drift guards (expected sets move per phase; end state in Phase 5) -----------------------

EXPECTED_NORMALIZER_IMPORTERS = {
    "src/shared/output_channel.py",
    "apps/jarvis-web/backend/main.py",
    "apps/jarvis-web/backend/tts_normalizer.py",
}
TTS_SINKS = {
    "src/gateway/wyoming_bridge.py::AthenaWyomingHandler._synthesize",
    "src/gateway/livekit_integration.py::TTSClient.synthesize",
    "apps/jarvis-web/backend/main.py::synthesize_speech",
    "src/orchestrator/automation_agent.py::AutomationAgent._send_notification",
}
# Every sink renders: this is the end state.
RENDERING_SINKS: set = {
    "src/orchestrator/automation_agent.py::AutomationAgent._send_notification",
    "src/gateway/wyoming_bridge.py::AthenaWyomingHandler._synthesize",
    "src/gateway/livekit_integration.py::TTSClient.synthesize",
    "apps/jarvis-web/backend/main.py::synthesize_speech",
}
# admin diagnostics (admin/backend/app/routes/voice_tests.py) are out of scope: not an Athena answer.
MUST_NOT_IMPORT_SEAM = [
    "src/orchestrator/mode_permission.py",
    "apps/jarvis-web/backend/caller_auth.py",
    "src/orchestrator/semantic_cache.py",
]


# Not valid Python today; the scans skip it and the first test below proves it names none of the guarded things.
UNPARSEABLE = {"src/jetson/athena_lite_llm.py"}


def _python_files():
    for root in ("src", "apps"):
        for path in (REPO / root).rglob("*.py"):
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            if _relative(path) in UNPARSEABLE:
                continue
            yield path


def _relative(path):
    return path.relative_to(REPO).as_posix()


def _imports_name(tree, name):
    return any(
        isinstance(n, ast.ImportFrom) and any(a.name == name for a in n.names)
        for n in ast.walk(tree)
    )


def _imports_module(tree, module):
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith(module):
            return True
        if isinstance(n, ast.ImportFrom) and any(a.name == module for a in n.names):
            return True
        if isinstance(n, ast.Import) and any(a.name.endswith(module) for a in n.names):
            return True
    return False


def test_files_the_scans_skip_are_the_known_unparseable_ones_and_name_nothing_guarded():
    unparseable = set()
    for root in ("src", "apps"):
        for path in (REPO / root).rglob("*.py"):
            try:
                ast.parse(path.read_text())
            except SyntaxError:
                unparseable.add(_relative(path))
    assert unparseable == UNPARSEABLE
    for relative in UNPARSEABLE:
        text = (REPO / relative).read_text()
        assert not any(name in text for name in ("normalize_for_tts", "output_channel", "/tts/synthesize", "call_service"))


def test_normalizer_importers_are_exactly_the_expected_set():
    importers = {
        _relative(p) for p in _python_files()
        if _relative(p) != "src/shared/tts_normalizer.py"
        and (
            _imports_name(ast.parse(p.read_text()), "normalize_for_tts")
            or _imports_module(ast.parse(p.read_text()), "tts_normalizer")
        )
    }
    assert importers == EXPECTED_NORMALIZER_IMPORTERS


def _functions_with_qualname(tree):
    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, scope + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield ".".join(scope + [child.name]), child
                yield from walk(child, scope + [child.name])
            else:
                yield from walk(child, scope)

    yield from walk(tree, [])


def _is_sink(function):
    for n in ast.walk(function):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "/tts/synthesize" in n.value:
            return True
        if (
            isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "call_service"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "tts"
        ):
            return True
    return False


def _calls_a_renderer(function):
    """A renderer is called, or handed to a thread (``asyncio.to_thread(normalize_for_tts, ...)``)."""
    return any(
        isinstance(n, ast.Name) and n.id in {"render_for_channel", "render_sink_text", "normalize_for_tts"}
        for n in ast.walk(function)
    )


def test_tts_sink_population_is_exactly_the_known_set_and_rendering_matches_the_phase():
    sinks, rendering = set(), set()
    for path in _python_files():
        source = path.read_text()
        if "/tts/synthesize" not in source and "call_service" not in source:
            continue
        for qualname, function in _functions_with_qualname(ast.parse(source)):
            if _is_sink(function):
                key = f"{_relative(path)}::{qualname}"
                sinks.add(key)
                if _calls_a_renderer(function):
                    rendering.add(key)
    assert sinks == TTS_SINKS
    assert rendering == RENDERING_SINKS


@pytest.mark.parametrize("relative", MUST_NOT_IMPORT_SEAM)
def test_authorization_and_cache_modules_never_import_the_seam(relative):
    path = REPO / relative
    assert path.exists(), relative
    assert not _imports_module(ast.parse(path.read_text()), "output_channel")


def test_the_import_guard_can_see_an_import():
    assert _imports_module(ast.parse("from shared.output_channel import render_answer"), "output_channel")
    assert _imports_module(ast.parse("from shared import output_channel"), "output_channel")
    assert _imports_module(ast.parse("import shared.output_channel"), "output_channel")
    assert not _imports_module(ast.parse("from shared import config"), "output_channel")
