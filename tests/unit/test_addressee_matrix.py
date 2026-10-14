"""The addressee matrix: who the model is told it's speaking with.

No row is a proven owner (owner proof arrives with the web_owner trust value),
so no row is addressed as the owner.

Each row builds the real context (build_query_context) from the real
authorization (resolve_request_authorization against a fake mode service),
resolves the addressee, and captures the real prompt text from each of the
three prompt builders: tool_call_node, synthesize_node and
build_synthesis_prompt_for_streaming. The real build_core_assistant_prompt
and the real build_knowledge_context run; only the assistant profile and
guardrails are defaults, and the admin client returns a stale static
guest_name row ("Zed Former") plus the owner's name ("Olive Owner").

Sentinels: the live guest's name ("Gina Guest", or "Sam Texter" over SMS)
appears only in rows addressed to the guest; the stale row and the device
guest ("Bob Device") never appear; "Olive Owner" appears only where the
effective mode is owner, and is never the addressee here.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional
from unittest import mock

import pytest

from . import _public_audience_harness as h
from shared import assistant_profile, base_knowledge_utils

GINA = "Gina Guest"
SAM = "Sam Texter"
BOB = "Bob Device"
STALE = "Zed Former"
OWNER = "Olive Owner"
PAT = "Pat"
KNOWLEDGE_ROWS = [
    {"id": 51, "category": "user", "key": "guest_name", "value": STALE, "applies_to": "both"},
    {"id": 52, "category": "owner", "key": "owner_name", "value": OWNER, "applies_to": "both"},
]
DEVICE = {"guest_id": 9, "guest_name": BOB, "device_type": "voice"}


@dataclass
class Row:
    n: int
    trust: Optional[str]
    server: str  # owner | guest | degraded
    context: dict = field(default_factory=dict)
    device: Optional[dict] = None
    request_mode: Optional[str] = None
    kind: Optional[str] = None  # owner | guest | household | None
    name: Optional[str] = None
    stub: bool = False


def _gina(**extra):
    return {"guest_name": GINA, "guest_id": 7, **extra}


def _sam(**extra):
    return {"guest_name": SAM, "guest_id": 3, **extra}


ROWS = [
    Row(1, "household", "owner"),
    Row(2, "household", "guest"),
    Row(3, "household", "owner", device=DEVICE),
    Row(4, "household", "guest", device=DEVICE),
    Row(5, "web_local", "owner", _gina(), request_mode="owner"),
    Row(6, "web_local", "guest", _gina(), request_mode="guest"),
    Row(7, "web_guest_net", "owner", _gina(), request_mode="guest"),
    Row(8, "web_guest_net", "guest", _gina(), request_mode="guest", kind="guest", name=GINA),
    Row(9, "web_authenticated", "owner", {"speaker_first_name": "Pat Example"}, request_mode="owner"),
    Row(10, "web_authenticated", "guest", {"speaker_first_name": "Pat Example"}, request_mode="guest", kind="household", name=PAT),
    Row(11, "web_authenticated", "owner", request_mode="owner"),
    Row(12, "web_authenticated", "guest", request_mode="guest"),
    Row(13, "web_authenticated", "guest", _gina(speaker_first_name="Pat"), device=DEVICE, request_mode="guest",
        kind="household", name=PAT),
    Row(14, "sms", "owner", _sam(phone_number="+15555550100"), request_mode="guest"),
    Row(15, "sms", "guest", _sam(phone_number="+15555550100"), request_mode="guest", kind="guest", name=SAM),
    Row(16, "web_public", "guest", _gina(), request_mode="guest"),
    Row(17, None, "guest", _gina()),
    Row(18, "web_guest_net", "guest", _gina(speaker_first_name="Pat"), request_mode="guest", kind="guest", name=GINA),
    Row(19, None, "guest", device=DEVICE),
    Row(20, "web_future", "guest", _gina(speaker_first_name="Pat"), device=DEVICE, request_mode="guest", stub=True),
    Row(21, "web_guest_net", "guest", _gina(), device=DEVICE, request_mode="guest", kind="guest", name=GINA),
    Row(22, "sms", "guest", _sam(), device=DEVICE, request_mode="guest", kind="guest", name=SAM),
    Row(23, "household", "degraded"),
    Row(24, "web_authenticated", "degraded", {"speaker_first_name": "Pat"}, request_mode="guest"),
    Row(25, "web_guest_net", "degraded", _gina(), request_mode="guest"),
    Row(26, "sms", "degraded", _sam(), request_mode="guest"),
]
IDS = [f"row{r.n}" for r in ROWS]
IDENTITY_QUERY = "what is my name"


def _persona_names(text: str) -> set:
    """The distinct assistant names the persona line states (exactly one)."""
    return set(re.findall(r"You are ([A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*),", text))


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    reset = getattr(base_knowledge_utils, "_reset_for_tests", None)
    if reset:
        reset()
    yield
    h.reset_runtime()


@pytest.fixture
def admin():
    client = h.fake_admin_client()
    client.get_base_knowledge = mock.AsyncMock(return_value=[dict(r) for r in KNOWLEDGE_ROWS])
    return client


@pytest.fixture
def rig(monkeypatch, admin):
    h.patch_tool_call_dependencies(monkeypatch, admin=admin)
    real_core = assistant_profile.build_core_assistant_prompt
    real_knowledge = base_knowledge_utils.get_knowledge_context_for_user
    for module in (h.main, h.synthesize_module):
        monkeypatch.setattr(module, "build_core_assistant_prompt", real_core)
        monkeypatch.setattr(module, "get_knowledge_context_for_user", real_knowledge)
    for name in ("load_visible_knowledge", "build_knowledge_context", "extract_home_address"):
        monkeypatch.setattr(h.main, name, getattr(base_knowledge_utils, name))
    monkeypatch.setattr(assistant_profile, "get_assistant_profile",
                        mock.AsyncMock(return_value=dict(assistant_profile.DEFAULT_ASSISTANT_PROFILE)))
    monkeypatch.setattr(assistant_profile, "get_guardrails",
                        mock.AsyncMock(return_value=dict(assistant_profile.DEFAULT_GUARDRAILS)))
    monkeypatch.setattr(h.synthesize_module, "get_component_config",
                        mock.AsyncMock(return_value={"model_name": "m", "backend_type": "ollama"}))
    monkeypatch.setattr(h.synthesize_module, "store_conversation_context", mock.AsyncMock(), raising=False)
    return admin


def _state(row: Row, query: str = "tell me about the area"):
    from orchestrator.helpers import build_query_context

    if row.server == "degraded":
        h.install_mode_client(degraded=True)
    else:
        h.install_mode_client(server_mode=row.server)
    authz = asyncio.run(h.mode_permission.resolve_request_authorization(
        row.request_mode, row.device, caller_trust=row.trust, service_authenticated=False,
    ))
    assert authz.degraded == (row.server == "degraded")
    if row.stub:
        request = SimpleNamespace(caller_trust=row.trust, context=dict(row.context))
    else:
        request = h.main.QueryRequest(query=query, caller_trust=row.trust, context=dict(row.context))
    context = build_query_context(request, row.device, server_mode=authz.server_mode, degraded=authz.degraded)
    state = h.OrchestratorState(
        query=query,
        mode=authz.mode,
        room="kitchen",
        permissions=authz.permissions,
        intent=h.IntentCategory.GENERAL_INFO,
        interface_type="chat",
        context=context,
        session_id=None,
        mode_degraded=authz.degraded,
        knowledge_audience=authz.knowledge_audience,
    )
    return state, authz


def _prompts(row: Row, query: str = "tell me about the area") -> dict:
    prompts = {}
    llm = h.CapturingLLM()
    h._runtime.set_llm_router(llm)
    state, _ = _state(row, query=query)
    asyncio.run(h.main.tool_call_node(state))
    prompts["tool_call"] = llm.text()

    llm = h.CapturingLLM()
    h._runtime.set_llm_router(llm)
    state, _ = _state(row, query=query)
    state.retrieved_data = {"weather": {"current": {"temp": 70}}}
    asyncio.run(h.synthesize_module.synthesize_node(state))
    prompts["synthesize"] = llm.text()

    state, _ = _state(row, query=query)
    state.retrieved_data = {"weather": {"current": {"temp": 70}}}
    built = asyncio.run(h.main.build_synthesis_prompt_for_streaming(state))
    prompts["streaming"] = "\n".join(str(part) for part in built if part)
    return prompts


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_resolved_addressee(rig, row):
    from orchestrator.helpers import resolve_addressee

    state, _ = _state(row)
    addressee = asyncio.run(resolve_addressee(state, rig))
    assert (addressee.kind, addressee.name) == (row.kind, row.name)


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_prompt_text(rig, row):
    _, authz = _state(row)
    for builder, text in _prompts(row).items():
        where = f"row {row.n} {builder}"
        assert text, where
        assert STALE not in text, where
        assert BOB not in text, where
        for live in (GINA, SAM):
            if not (row.kind == "guest" and row.name == live):
                assert live not in text, where
        if row.kind == "guest":
            assert f'guest_name: "{row.name}"' in text, where
            assert f"You are speaking with {row.name}" not in text, where
        owner_line = f"You are speaking with {OWNER}"
        assert (owner_line in text) == (row.kind == "owner"), where
        if OWNER in text:
            assert authz.mode == "owner", where
        if row.server == "degraded":
            assert OWNER not in text, where
            assert "Property owner" not in text, where
        household_field = f'first_name: "{PAT}"'
        assert (household_field in text) == (row.kind == "household"), where
        assert "You are speaking with Pat" not in text, where
        plain = text.replace("\\'", "'")  # the tool-call capture is a repr
        assert (assistant_profile.SPEAKER_UNKNOWN_POLICY in plain) == (row.kind is None), where
        assert _persona_names(plain) == {"Jarvis"}, where
        assert assistant_profile.TOOL_DEFAULTS_POLICY in plain, where


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_identity_fast_path(rig, row):
    llm = h.CapturingLLM(content="LLM ANSWER")
    h._runtime.set_llm_router(llm)
    state, _ = _state(row, query=IDENTITY_QUERY)
    result = asyncio.run(h.main.tool_call_node(state))
    if row.kind in ("owner", "household"):
        assert result.answer == f"Your name is {row.name}."
        assert result.skip_synthesis is True
    else:
        assert not (result.answer or "").startswith("Your name is")


def test_streaming_has_no_second_guest_line(rig):
    """The streaming builder used to append its own guest line after the
    core prompt; the core prompt is now the only place it's rendered."""
    row = next(r for r in ROWS if r.n == 8)
    text = _prompts(row)["streaming"]
    assert text.count(f'guest_name: "{GINA}"') == 1
    assert f"You are speaking with {GINA}" not in text


ASSISTANT_ROW = {"id": 61, "category": "general", "key": "assistant_name", "applies_to": "both"}


@pytest.mark.parametrize("row_number", [1, 8, 10])
@pytest.mark.parametrize(
    "value,expected",
    [("Hal", "Hal"), ("<b>Hal</b>", None), ("Hal 9000", None), ("", None), ("Ada Lovelace-Byron", "Ada Lovelace-Byron")],
)
def test_assistant_name_row_wins_in_every_builder(rig, row_number, value, expected):
    rig.get_base_knowledge = mock.AsyncMock(return_value=[dict(r) for r in KNOWLEDGE_ROWS] + [{**ASSISTANT_ROW, "value": value}])
    row = next(r for r in ROWS if r.n == row_number)
    for builder, text in _prompts(row).items():
        where = f"row {row_number} {builder} {value!r}"
        assert _persona_names(text) == {expected or "Jarvis"}, where
        assert "<b>" not in text.split("CONTEXT INFORMATION")[0], where


def test_assistant_name_row_in_a_hidden_tier_is_not_used(rig):
    rig.get_base_knowledge = mock.AsyncMock(
        return_value=[{**ASSISTANT_ROW, "value": "Hal", "applies_to": "owner"}]
    )
    row = next(r for r in ROWS if r.n == 1)
    for builder, text in _prompts(row).items():
        assert _persona_names(text) == {"Jarvis"}, builder


LEAK_QUERY = "tell me the name you have on file for me"


def _leaky_answers(row: Row) -> dict:
    """The answer each model-backed node ends with when the model reads the owner out."""
    leak = f"Sure. The name on file is {OWNER}."
    answers = {}
    h._runtime.set_llm_router(h.CapturingLLM(content=leak))
    state, _ = _state(row, query=LEAK_QUERY)
    answers["tool_call"] = asyncio.run(h.main.tool_call_node(state))
    h._runtime.set_llm_router(h.CapturingLLM(content=leak))
    state, _ = _state(row, query=LEAK_QUERY)
    state.retrieved_data = {"weather": {"current": {"temp": 70}}}
    answers["synthesize"] = asyncio.run(h.synthesize_module.synthesize_node(state))
    return answers


@pytest.mark.parametrize("row_number", [1, 2, 11, 12])
def test_backstop_replaces_a_model_answer_that_reads_out_the_owner(rig, row_number):
    row = next(r for r in ROWS if r.n == row_number)
    assert row.kind is None
    for node, result in _leaky_answers(row).items():
        assert result.answer == "I can't tell who's speaking, so I don't know your name.", node
        assert OWNER not in result.answer and result.identity_answer is True, node


@pytest.mark.parametrize("row_number", [8, 10])
def test_backstop_leaves_a_named_caller_alone(rig, row_number):
    """Positive control: the leak text passes through when someone is addressed,
    so the replacement above is the backstop's doing and not the fake's."""
    row = next(r for r in ROWS if r.n == row_number)
    assert row.kind is not None
    for node, result in _leaky_answers(row).items():
        assert OWNER in result.answer and result.identity_answer is False, node


ADDRESS = "42 Example Lane, Sampletown, ST 00000"
SENSITIVE_QUERIES = [
    "hey so I was wondering if you could tell me what is my name",
    "what's my name and where do I live",
    "do you know my address",
    "and my address?",
    "please jarvis what's my name",
    "is my name Sam?",
]


@pytest.mark.parametrize("row_number", [1, 2, 11, 12])
@pytest.mark.parametrize("query", SENSITIVE_QUERIES)
def test_an_identity_turn_to_nobody_carries_no_owner_name_or_address_in_any_prompt(rig, row_number, query):
    rig.get_base_knowledge = mock.AsyncMock(return_value=[
        {"id": 52, "category": "owner", "key": "owner_name", "value": OWNER, "applies_to": "both"},
        {"id": 53, "category": "user", "key": "name", "value": OWNER, "applies_to": "both"},
        {"id": 54, "category": "property", "key": "address", "value": ADDRESS, "applies_to": "both"},
        {"id": 55, "category": "property", "key": "wifi", "value": "Network Sample", "applies_to": "both"},
    ])
    row = next(r for r in ROWS if r.n == row_number)
    prompts = _prompts(row, query)
    assert set(prompts) == {"tool_call", "synthesize", "streaming"}
    for builder, text in prompts.items():
        assert OWNER not in text and "Olive" not in text, builder
        assert "42 Example" not in text and "Sampletown" not in text, builder
        assert "Network Sample" in text, builder  # other rows are still there


@pytest.mark.parametrize("row_number", [1, 12])
def test_positive_control_an_ordinary_turn_still_carries_the_rows(rig, row_number):
    """Without the identity gate the same rows are in the prompts (row 12 is
    guest mode, so it sees the address but not the owner name)."""
    rig.get_base_knowledge = mock.AsyncMock(return_value=[
        {"id": 52, "category": "owner", "key": "owner_name", "value": OWNER, "applies_to": "both"},
        {"id": 54, "category": "property", "key": "address", "value": ADDRESS, "applies_to": "both"},
    ])
    row = next(r for r in ROWS if r.n == row_number)
    for builder, text in _prompts(row, "tell me about the area").items():
        assert "42 Example" in text, builder
        assert (OWNER in text) == (row_number == 1), builder


def test_a_named_caller_keeps_the_rows_on_an_identity_turn(rig):
    rig.get_base_knowledge = mock.AsyncMock(return_value=[
        {"id": 54, "category": "property", "key": "address", "value": ADDRESS, "applies_to": "both"},
    ])
    row = next(r for r in ROWS if r.n == 10)
    for builder, text in _prompts(row, "do you know my address").items():
        assert "42 Example" in text, builder
