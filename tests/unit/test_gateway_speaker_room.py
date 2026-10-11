"""gateway.speaker_room: which room the speaker is in, who is believed, and the
prompt marker. Fictional house (tests/fixtures/ha_registry/house_a.json)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest.mock as mock
from pathlib import Path

import pytest
from starlette.requests import Request

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-speaker-room")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import gateway.main as gw  # noqa: E402
from gateway import speaker_room as sr  # noqa: E402
from shared import ha_registry as hr  # noqa: E402
from shared.client_throttle import parse_networks  # noqa: E402

RAW = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json").read_text(encoding="utf-8")
)
DEV = RAW["devices_by_name"]
STUDY = DEV["study_sat"]              # in area_study, has assist_satellite.study_voice
GALLEY_HUB = DEV["galley_hub"]        # in area_galley
UNKNOWN_ID = "f" * 32
SECRET = "gateway-secret-for-tests"


class Fetcher:
    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return RAW


class Spy:
    """The real registry with the calls the gateway makes recorded."""

    def __init__(self, registry):
        self.registry = registry
        self.ensure_calls = []

    def current(self):
        return self.registry.current()

    @property
    def stale(self):
        return self.registry.stale

    def request_refresh(self, **kwargs):
        return self.registry.request_refresh(**kwargs)

    async def ensure(self, timeout, **kwargs):
        self.ensure_calls.append((timeout, kwargs))
        return await self.registry.ensure(timeout, **kwargs)


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh():
    sr.reset_for_test()
    yield
    sr.reset_for_test()


def warm(clock=None):
    fetcher = Fetcher()
    registry = hr.HARegistry(fetcher, monotonic=clock or Clock())
    asyncio.run(registry.ensure(1))
    return Spy(registry), fetcher


def request(headers=None, client="192.0.2.50"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (client, 40000)})


def resolve(registry, *, device_id, trusted, satellite_map=hr.SatelliteRoomMap(), states=None):
    async def read_states():
        return states

    return asyncio.run(sr.resolve_speaker_room(
        device_id=device_id, trusted=trusted, registry=registry, satellite_map=satellite_map,
        read_states=read_states if states is not None else None,
    ))


# ---------------------------------------------------------------------------
# Speaker room
# ---------------------------------------------------------------------------

def test_a_trusted_device_id_resolves_to_its_area_key():
    registry, _ = warm()
    got = resolve(registry, device_id=STUDY, trusted=True)
    assert got == sr.SpeakerRoom("study", "area")


def test_the_satellite_map_beats_the_area_by_device_id():
    registry, _ = warm()
    mapped = hr.SatelliteRoomMap(device_rooms={STUDY: "Galley"})
    assert resolve(registry, device_id=STUDY, trusted=True, satellite_map=mapped) == sr.SpeakerRoom("galley", "map")


def test_the_satellite_map_beats_the_area_by_the_satellite_entity_on_the_device():
    registry, _ = warm()
    mapped = hr.SatelliteRoomMap(entity_rooms={"assist_satellite.study_voice": "galley"})
    assert resolve(registry, device_id=STUDY, trusted=True, satellite_map=mapped) == sr.SpeakerRoom("galley", "map")


def test_an_untrusted_device_id_is_ignored_with_no_room_and_no_registry_wait():
    registry, _ = warm()
    got = resolve(registry, device_id=STUDY, trusted=False)
    assert got == sr.UNKNOWN_ROOM and registry.ensure_calls == []


def test_a_malformed_device_id_is_ignored_even_from_a_trusted_caller():
    registry, _ = warm()
    for bad in ("office", "A" * 32, STUDY[:-1], STUDY + "0", None, ""):
        assert resolve(registry, device_id=bad, trusted=True) == sr.UNKNOWN_ROOM, bad


def test_an_unknown_trusted_id_waits_once_for_a_forced_refresh_and_is_then_remembered_as_unknown():
    registry, fetcher = warm()
    first = resolve(registry, device_id=UNKNOWN_ID, trusted=True)
    assert first == sr.UNKNOWN_ROOM
    assert registry.ensure_calls == [(sr.UNKNOWN_DEVICE_ENSURE_SECONDS, {"refresh": True})]
    second = resolve(registry, device_id=UNKNOWN_ID, trusted=True)
    assert second == sr.UNKNOWN_ROOM and len(registry.ensure_calls) == 1, "negative-cached: no second wait"


def test_the_negative_cache_expires_after_sixty_seconds():
    registry, _ = warm()
    clock = Clock()

    async def go():
        for _ in range(2):
            await sr.resolve_speaker_room(
                device_id=UNKNOWN_ID, trusted=True, registry=registry, monotonic=clock,
            )
            clock.now += sr.NEGATIVE_CACHE_SECONDS + 1

    asyncio.run(go())
    assert len(registry.ensure_calls) == 2


def test_a_warm_registry_makes_no_fetcher_calls_over_a_hundred_turns():
    registry, fetcher = warm()
    before = fetcher.calls
    for _ in range(100):
        assert resolve(registry, device_id=STUDY, trusted=True).key == "study"
    assert fetcher.calls == before and registry.ensure_calls == []


def test_a_warm_turn_is_fast():
    registry, _ = warm()
    started = time.perf_counter()
    for _ in range(100):
        resolve(registry, device_id=STUDY, trusted=True)
    assert (time.perf_counter() - started) / 100 < 0.05


def test_the_first_turn_before_any_snapshot_waits_for_the_cold_ensure():
    fetcher = Fetcher()
    spy = Spy(hr.HARegistry(fetcher, monotonic=Clock()))
    got = resolve(spy, device_id=STUDY, trusted=True)
    assert got.key == "study" and spy.ensure_calls[0][0] == sr.COLD_ENSURE_SECONDS


ACTIVE = {"entity_id": "assist_satellite.study_voice", "state": "listening"}
IDLE = {"entity_id": "assist_satellite.galley_voice", "state": "idle"}
OTHER = {"entity_id": "assist_satellite.lounge_voice", "state": "responding"}


def test_one_non_idle_satellite_from_a_trusted_caller_names_its_area():
    registry, _ = warm()
    got = resolve(registry, device_id=None, trusted=True, states=[ACTIVE, IDLE])
    assert got == sr.SpeakerRoom("study", "guess")


def test_one_non_idle_satellite_from_an_untrusted_caller_is_ignored():
    registry, _ = warm()
    assert resolve(registry, device_id=None, trusted=False, states=[ACTIVE, IDLE]) == sr.UNKNOWN_ROOM


def test_two_non_idle_satellites_name_no_room():
    registry, _ = warm()
    assert resolve(registry, device_id=None, trusted=True, states=[ACTIVE, OTHER]) == sr.UNKNOWN_ROOM


def test_a_satellite_that_went_idle_names_no_room():
    registry, _ = warm()
    idle_study = {"entity_id": "assist_satellite.study_voice", "state": "idle"}
    assert resolve(registry, device_id=None, trusted=True, states=[idle_study, IDLE]) == sr.UNKNOWN_ROOM


def test_the_guess_uses_the_satellite_map_before_the_area():
    registry, _ = warm()
    mapped = hr.SatelliteRoomMap(entity_rooms={"assist_satellite.study_voice": "Galley"})
    assert resolve(registry, device_id=None, trusted=True, satellite_map=mapped, states=[ACTIVE]).key == "galley"


def test_a_failing_state_read_names_no_room_and_does_not_raise():
    registry, _ = warm()

    async def broken():
        raise ConnectionError("down")

    got = asyncio.run(sr.resolve_speaker_room(device_id=None, trusted=True, registry=registry, read_states=broken))
    assert got == sr.UNKNOWN_ROOM


def test_resolution_logs_no_device_id_and_no_room_name_at_info(monkeypatch):
    registry, _ = warm()
    logs = []

    class Recorder:
        def __getattr__(self, level):
            return lambda event, **kw: logs.append({"event": event, "level": level, **kw})

    monkeypatch.setattr(sr, "logger", Recorder())
    resolve(registry, device_id=STUDY, trusted=True)
    blob = json.dumps(logs, default=str)
    assert STUDY not in blob and "study" not in blob
    assert any(e["event"] == "speaker_room_resolved" and e["source"] == "area" for e in logs)


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------

NETS = parse_networks("192.0.2.50/32")


def trusted(req, *, api_key=None, nets=(), proxies=()):
    return sr.trusted_voice_caller(req, api_key=api_key, device_networks=nets, trusted_proxies=proxies)


def test_a_request_presenting_the_gateway_key_is_trusted():
    req = request({"Authorization": f"Bearer {SECRET}"})
    assert trusted(req, api_key=SECRET) is True


def test_a_wrong_key_is_not_trusted():
    assert trusted(request({"Authorization": "Bearer nope"}), api_key=SECRET) is False
    assert trusted(request(), api_key=SECRET) is False


def test_with_no_key_set_or_the_dummy_key_presenting_it_proves_nothing():
    for key in (None, "", "dummy-key"):
        assert trusted(request({"Authorization": f"Bearer {key or 'x'}"}), api_key=key) is False


def test_a_client_in_the_device_networks_is_trusted_and_others_are_not():
    assert trusted(request(client="192.0.2.50"), nets=NETS) is True
    assert trusted(request(client="192.0.2.51"), nets=NETS) is False


def test_the_client_is_the_nearest_forwarded_hop_outside_the_trusted_proxies():
    proxies = parse_networks("10.0.0.0/8")
    via_proxy = request({"X-Forwarded-For": "192.0.2.50"}, client="10.1.2.3")
    assert trusted(via_proxy, nets=NETS, proxies=proxies) is True
    forged = request({"X-Forwarded-For": "192.0.2.50"}, client="198.51.100.9")
    assert trusted(forged, nets=NETS, proxies=proxies) is False, "an untrusted peer's forwarded header is not read"


def test_device_networks_overlapping_the_trusted_proxies_are_dropped_with_one_error(monkeypatch):
    from shared.output_channel import without_networks_overlapping

    proxies = parse_networks("10.0.0.0/8")
    kept, dropped = without_networks_overlapping(parse_networks("10.1.0.0/16,192.0.2.50/32"), proxies, log_error=False)
    assert [str(n) for n in kept] == ["192.0.2.50/32"] and dropped == 1

    monkeypatch.setattr(gw, "HA_DEVICE_CLIENT_NETWORKS", "10.1.0.0/16")
    monkeypatch.setattr(gw, "_DEVICE_NETWORKS_DROPPED", 1)
    errors = []
    monkeypatch.setattr(gw.logger, "error", lambda event, **kw: errors.append({"event": event, **kw}))
    gw._log_speech_network_config()
    overlap = [e for e in errors if e["event"] == "ha_device_networks_overlap_trusted_proxies"]
    assert len(overlap) == 1 and overlap[0]["count"] == 1


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------

def msg(role, content):
    return {"role": role, "content": content}


def marker(device=STUDY):
    return f"athena_device_id: {device}"


def test_a_marker_in_the_system_message_is_read_and_stripped():
    device, cleaned, _, _ = sr.extract_device_marker([
        msg("system", f"You are a helper.\n{marker()}\nBe brief."), msg("user", "turn on the lights"),
    ])
    assert device == STUDY
    assert cleaned[0]["content"] == "You are a helper.\nBe brief." and cleaned[1]["content"] == "turn on the lights"


def test_the_developer_role_and_the_instructions_text_are_read():
    assert sr.extract_device_marker([msg("developer", marker())])[0] == STUDY
    device, _, instructions, _ = sr.extract_device_marker([msg("user", "hi")], f"Rules.\n{marker()}")
    assert device == STUDY and instructions == "Rules."


@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
def test_a_marker_in_any_other_role_is_never_read_or_changed(role):
    messages = [msg("system", "plain"), msg(role, marker())]
    device, cleaned, _, _ = sr.extract_device_marker(messages)
    assert device is None and cleaned[1]["content"] == marker()


def test_a_marker_in_a_later_system_message_counts_only_if_it_is_the_only_one():
    device, cleaned, _, _ = sr.extract_device_marker([msg("system", "a"), msg("user", "b"), msg("system", marker())])
    assert device == STUDY and cleaned[2]["content"] == ""


def test_two_markers_give_no_device_and_both_are_stripped():
    device, cleaned, _, _ = sr.extract_device_marker([
        msg("system", marker()), msg("system", marker(GALLEY_HUB)),
    ])
    assert device is None and [m["content"] for m in cleaned] == ["", ""]


@pytest.mark.parametrize("value", ["A" * 32, "ab" * 8, "ab" * 17, "", "not-hex-" + "0" * 24])
def test_a_malformed_marker_value_gives_no_device_and_is_still_stripped(value):
    device, cleaned, _, _ = sr.extract_device_marker([msg("system", f"keep\nathena_device_id: {value}")])
    assert device is None and cleaned[0]["content"] == "keep"


def test_a_marker_with_surrounding_spaces_and_tabs_is_read():
    assert sr.extract_device_marker([msg("system", f"\t  athena_device_id:\t {STUDY}  \t")])[0] == STUDY


def test_a_long_whitespace_line_within_the_cap_parses_quickly():
    line = "athena_device_id:" + " " * 60_000 + STUDY
    started = time.perf_counter()
    device, _, _, _ = sr.extract_device_marker([msg("system", line)])
    assert time.perf_counter() - started < 0.05
    assert device == STUDY


def test_a_whitespace_line_past_the_cap_is_stripped_but_not_honoured():
    line = "athena_device_id:" + " " * 200_000 + STUDY
    started = time.perf_counter()
    device, cleaned, _, _ = sr.extract_device_marker([msg("system", line)])
    assert time.perf_counter() - started < 0.2
    assert device is None and cleaned[0]["content"] == ""


def test_a_one_mebibyte_message_is_scanned_only_to_the_cap():
    big = ("x" * 99 + "\n") * 10_000 + marker()
    started = time.perf_counter()
    device, cleaned, _, _ = sr.extract_device_marker([msg("system", big)])
    assert time.perf_counter() - started < 1.0
    assert device is None and marker() not in cleaned[0]["content"], "stripped, but not honoured"


def test_a_pydantic_message_is_copied_not_mutated():
    original = gw.ChatMessage(role="system", content=f"hi\n{marker()}")
    device, cleaned, _, _ = sr.extract_device_marker([original])
    assert device == STUDY and cleaned[0].content == "hi" and original.content.endswith(STUDY)


# ---------------------------------------------------------------------------
# The /v1 payload
# ---------------------------------------------------------------------------

def chat(messages=None):
    return gw.ChatCompletionRequest(model="m", messages=messages or [gw.ChatMessage(role="user", content="hi")])


def payload(**kwargs):
    from shared.output_channel import OutputChannel

    return gw._orchestrator_openai_payload(chat(), "study", stream=False, channel=OutputChannel.TEXT, **kwargs)


def test_the_payload_carries_voice_device_id_and_the_follow_up_flag_only_for_a_believed_device():
    with_device = payload(voice_device_id=STUDY)["extra_body"]
    assert with_device["voice_device_id"] == STUDY and with_device["supports_followup"] is True
    without = payload()["extra_body"]
    assert "voice_device_id" not in without and "supports_followup" not in without


def test_the_route_helper_believes_a_device_only_from_a_trusted_caller(monkeypatch):
    registry, _ = warm()
    monkeypatch.setattr(gw, "ha_registry", registry)
    monkeypatch.setattr(gw, "API_KEY", SECRET)
    monkeypatch.setattr(gw, "_DEVICE_CLIENT_NETWORKS", ())
    monkeypatch.setattr(gw, "_get_satellite_room_map", lambda: hr.SatelliteRoomMap())
    sr.reset_for_test()
    trusted_req = request({"Authorization": f"Bearer {SECRET}"})
    got = asyncio.run(gw._speaker_room_for_request(trusted_req, device_id=STUDY))
    assert got == ("study", STUDY)
    got = asyncio.run(gw._speaker_room_for_request(request(), device_id=STUDY))
    assert got == ("unknown", None)


# ---------------------------------------------------------------------------
# HA_SATELLITE_ROOM_MAP: entity keys and device keys
# ---------------------------------------------------------------------------

MAP_JSON = json.dumps({"assist_satellite.study_voice": "study", STUDY: "galley"})


def test_satellite_room_map_inverts_entity_keys_only(monkeypatch):
    monkeypatch.setattr(gw, "_ha_satellite_room_map_cache", hr.parse_satellite_room_map(MAP_JSON))
    assert gw._get_room_to_satellite_map() == {"study": "assist_satellite.study_voice"}


def test_satellite_room_map_never_sends_an_announcement_to_a_device_id(monkeypatch):
    posts = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            posts.append((url, kwargs.get("json")))
            return mock.MagicMock(status_code=200)

    monkeypatch.setattr(gw, "_ha_satellite_room_map_cache", hr.parse_satellite_room_map(MAP_JSON))
    monkeypatch.setattr(gw.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("HA_TOKEN", "t")
    assert asyncio.run(gw.send_satellite_announcement("galley", "hello")) is False
    assert posts == []
    assert asyncio.run(gw.send_satellite_announcement("study", "hello")) is True
    assert posts[0][1]["entity_id"] == "assist_satellite.study_voice"


def test_satellite_room_map_invalid_keys_log_one_error_with_a_count(monkeypatch):
    errors = []
    monkeypatch.setattr(hr.logger, "error", lambda event, **kw: errors.append({"event": event, **kw}))
    parsed = hr.parse_satellite_room_map(json.dumps({"light.kitchen": "x", "assist_satellite.ok": "study"}))
    assert dict(parsed.entity_rooms) == {"assist_satellite.ok": "study"}
    assert errors == [{"event": "ha_satellite_room_map_invalid_keys", "count": 1}]


def test_satellite_room_map_both_key_kinds_resolve_the_speaker_room():
    registry, _ = warm()
    mapped = hr.parse_satellite_room_map(json.dumps({STUDY: "Galley"}))
    assert resolve(registry, device_id=STUDY, trusted=True, satellite_map=mapped).key == "galley"
    entity_mapped = hr.parse_satellite_room_map(json.dumps({"assist_satellite.study_voice": "Galley"}))
    assert resolve(registry, device_id=STUDY, trusted=True, satellite_map=entity_mapped).key == "galley"


# ---------------------------------------------------------------------------
# Security review: rejected marker, state-read cache, bounded negative cache,
# wide device networks
# ---------------------------------------------------------------------------

def test_a_rejected_marker_is_flagged_and_skips_the_satellite_guess():
    for lines in (f"athena_device_id: {STUDY}\nathena_device_id: {GALLEY_HUB}", "athena_device_id: NOT-A-DEVICE"):
        device, _, _, rejected = sr.extract_device_marker([msg("system", lines)])
        assert device is None and rejected is True
    registry, _ = warm()

    async def states():
        return [ACTIVE]

    guessed = asyncio.run(sr.resolve_speaker_room(
        device_id=None, trusted=True, registry=registry, read_states=states, marker_rejected=False))
    assert guessed.source == "guess"
    refused = asyncio.run(sr.resolve_speaker_room(
        device_id=None, trusted=True, registry=registry, read_states=states, marker_rejected=True))
    assert refused == sr.UNKNOWN_ROOM


def test_no_marker_and_a_good_marker_are_not_rejected():
    assert sr.extract_device_marker([msg("system", "plain")])[3] is False
    assert sr.extract_device_marker([msg("system", f"athena_device_id: {STUDY}")])[3] is False


def test_the_negative_cache_is_pruned_and_capped():
    sr.reset_for_test()
    for i in range(sr.UNKNOWN_CACHE_MAX + 50):
        sr._remember_unknown(f"{i:032x}", now=1000.0 + i)
    assert len(sr._unknown_until) <= sr.UNKNOWN_CACHE_MAX
    far = 1000.0 + sr.NEGATIVE_CACHE_SECONDS * 10
    sr._remember_unknown("e" * 32, now=far)
    assert len(sr._unknown_until) < sr.UNKNOWN_CACHE_MAX / 2, "expired entries are dropped once the cap is reached"
    assert "e" * 32 in sr._unknown_until


def test_the_satellite_state_read_is_reused_for_two_seconds(monkeypatch):
    calls = []

    async def fetch():
        calls.append(1)
        return [ACTIVE]

    clock = Clock()
    monkeypatch.setattr(gw, "_fetch_assist_satellite_states", fetch)
    monkeypatch.setattr(gw.time, "monotonic", clock)
    monkeypatch.setattr(gw, "_satellite_states_cache", (float("-inf"), []))

    async def go():
        await gw._read_assist_satellite_states()
        await gw._read_assist_satellite_states()
        clock.now += gw.SATELLITE_STATES_TTL_SECONDS + 0.1
        await gw._read_assist_satellite_states()

    asyncio.run(go())
    assert len(calls) == 2


def test_a_device_network_wider_than_a_slash_24_is_counted():
    count = gw._wide_device_networks
    assert count(parse_networks("192.0.2.0/24,198.51.100.7")) == 0
    assert count(parse_networks("192.0.2.0/23")) == 1
    assert count(parse_networks("10.0.0.0/8,192.0.2.0/24")) == 1
    assert count(parse_networks("2001:db8::/48")) == 1
    assert count(parse_networks("2001:db8::/64")) == 0


def test_startup_warns_about_a_wide_device_network(monkeypatch):
    events = []

    class Recorder:
        def __getattr__(self, level):
            return lambda event, **kw: events.append((level, event, kw))

    monkeypatch.setattr(gw, "logger", Recorder())
    monkeypatch.setattr(gw, "_DEVICE_CLIENT_NETWORKS", parse_networks("10.0.0.0/8"))
    gw._log_speech_network_config()
    wide = [e for e in events if e[1] == "ha_device_client_networks_wide"]
    assert wide and wide[0][0] == "warning" and wide[0][2]["count"] == 1
    assert "10.0.0.0" not in repr(wide)
    events.clear()
    monkeypatch.setattr(gw, "_DEVICE_CLIENT_NETWORKS", parse_networks("192.0.2.0/24"))
    gw._log_speech_network_config()
    assert not [e for e in events if e[1] == "ha_device_client_networks_wide"]
