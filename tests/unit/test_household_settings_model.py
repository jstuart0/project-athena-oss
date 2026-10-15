"""The household settings registry: every validator boundary, masking, the stored
shape and the audit payload.

``src/shared/household_settings.py`` is pure (stdlib and pydantic only) and
jarvis-web runs a copy of it with no ``shared`` package, so this test loads the
file by path. It runs in the orchestrator job (pydantic 2.13) and in the
jarvis-web job (pydantic 2.10).
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
import time
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "shared" / "household_settings.py"


def _load():
    spec = importlib.util.spec_from_file_location("household_settings_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hs = _load()

FEED = {"id": "rtd_bus", "name": "RTD Local Bus", "url": "https://example.org/f.zip"}


def ok(section, value):
    return hs.validate_update(section, value).model_dump(mode="json")


def bad(section, value, kind=None):
    with pytest.raises(hs.SettingsValidationError) as caught:
        hs.validate_update(section, value)
    errors = caught.value.errors
    assert errors and all(set(e) == {"loc", "type"} for e in errors)
    if kind is not None:
        assert kind in {e["type"] for e in errors}, errors
    return errors


# -- the registry ------------------------------------------------------------------------------------

def test_the_registry_has_the_nine_sections_in_order():
    assert list(hs.SECTIONS) == [
        "location", "amtrak", "transit", "community_events", "gateway", "sms", "lights", "bed_warmer", "jarvis_web",
    ]
    for name, spec in hs.SECTIONS.items():
        assert re.fullmatch(r"[a-z_]+", name)
        assert spec.key == f"household.{name}" and spec.name == name
        assert spec.required_permission in ("write", "manage_infrastructure")
    with pytest.raises(TypeError):
        hs.SECTIONS["x"] = None  # read-only


def test_permissions_and_writability_match_the_decisions():
    owner_only = {n for n, s in hs.SECTIONS.items() if s.required_permission == "manage_infrastructure"}
    assert owner_only == {"sms", "lights", "bed_warmer", "jarvis_web"}
    assert [n for n, s in hs.SECTIONS.items() if not s.writable] == ["location"]
    assert hs.SECTIONS["bed_warmer"].units == (("level_left", "level_right", "power_main", "power_side_a", "power_side_b"),)
    assert hs.SECTIONS["jarvis_web"].units == (("min_temp", "max_temp"),)
    assert hs.SECTIONS["jarvis_web"].audit_redact == ("default_room",)
    assert {n: s.audit for n, s in hs.SECTIONS.items() if s.audit != "values"} == {
        "location": "fields", "amtrak": "fields", "transit": "hosts", "community_events": "hosts",
    }


def test_every_section_model_forbids_extras_and_has_only_optional_fields():
    for spec in hs.SECTIONS.values():
        assert spec.model.model_config["extra"] == "forbid"
        assert spec.model() is not None  # every field has a default
        bad(spec.name, {"not_a_field": 1}, "extra_forbidden")


def test_jarvis_web_has_eleven_fields():
    assert len(hs.JarvisWebSection.model_fields) == 11


# -- entity ids (boundary table: Entity, Appliance, Bed warmer) -----------------------------------------------

def test_entity_accepts_a_climate_id():
    assert ok("jarvis_web", {"climate_entity": "climate.living_room"})["climate_entity"] == "climate.living_room"


@pytest.mark.parametrize("value", [
    "switch.living_room", "climate.x/../y", "climate.x\n", "Climate.X", "climate.X", "", "climate.", "climate",
    "climate." + "a" * 250, " climate.x", "climate.x ", 5,
])
def test_entity_rejects(value):
    bad("jarvis_web", {"climate_entity": value})


def test_a_256_character_entity_id_is_rejected_and_255_is_not():
    assert ok("jarvis_web", {"climate_entity": "climate." + "a" * 247})
    bad("jarvis_web", {"climate_entity": "climate." + "a" * 248})


def test_appliances_take_water_heater_and_refuse_the_house_water_heater():
    assert ok("jarvis_web", {"oven_entity": "water_heater.ge_oven"})["oven_entity"] == "water_heater.ge_oven"
    for fname in ("oven_entity", "fridge_entity", "freezer_entity"):
        assert ok("jarvis_web", {fname: "water_heater.ge_unit"})[fname]
        for value in ("water_heater.water_heater", "water_heater.hot_water_tank", "water_heater.boiler_1",
                      "water_heater.geyser", "water_heater.cylinder", "climate.oven"):
            bad("jarvis_web", {fname: value})


def test_sensor_fields_take_their_domains():
    assert ok("jarvis_web", {"stove_timer_sensor": "sensor.stove_timer"})
    bad("jarvis_web", {"stove_timer_sensor": "binary_sensor.stove_timer"})
    assert ok("jarvis_web", {"fridge_door_sensor": "binary_sensor.fridge_door"})
    assert ok("jarvis_web", {"fridge_door_sensor": "sensor.fridge_door"})
    bad("jarvis_web", {"fridge_door_sensor": "switch.fridge_door"})


@pytest.mark.parametrize("fname,value", [
    ("power_main", "switch.bed_main"), ("level_left", "select.bed_left"), ("level_right", "select.bed_right"),
    ("power_side_a", "switch.bed_side_a"), ("power_side_b", "switch.bed_side_b"),
])
def test_bed_warmer_accepts(fname, value):
    assert ok("bed_warmer", {fname: value})[fname] == value


@pytest.mark.parametrize("fname,value", [
    ("power_main", "switch.garage_door"), ("power_main", "switch.front_door_unlock"),
    ("power_main", "switch.bedroom_alarm"), ("power_main", "switch.hallway_siren"),
    ("power_main", "switch.water_valve"), ("power_main", "switch.gate_opener"),
    ("power_main", "switch.water_heater"), ("power_main", "switch.boiler"),
    ("power_main", "light.bed"), ("level_left", "number.bed_left"), ("level_left", "switch.bed_left"),
    ("power_side_a", "select.bed_a"),
])
def test_bed_warmer_rejects(fname, value):
    bad("bed_warmer", {fname: value})


def test_a_partial_bed_warmer_validates_but_is_not_a_unit():
    assert ok("bed_warmer", {"power_main": "switch.bed_main"})["level_left"] is None


# -- country code, station, model, room -------------------------------------------------------------------

@pytest.mark.parametrize("given,stored", [("1", "1"), ("44", "44"), ("+1", "1"), ("358", "358")])
def test_country_code_accepts(given, stored):
    assert ok("sms", {"default_country_code": given}) == {"default_country_code": stored}


@pytest.mark.parametrize("value", ["", "0", "+0", "1234", "1a", "+", "++1", " 1", "00", "٣", 1])
def test_country_code_rejects(value):
    bad("sms", {"default_country_code": value})


@pytest.mark.parametrize("given,stored", [("NYP", "NYP"), ("nyp", "NYP"), (" dEn ", "DEN")])
def test_station_accepts_and_upper_cases(given, stored):
    assert ok("amtrak", {"default_station": given}) == {"default_station": stored}


@pytest.mark.parametrize("value", ["NY", "NYPX", "N1P", "", "ñyp", "ßßß", "ny\n", 5])
def test_station_rejects(value):
    bad("amtrak", {"default_station": value})


@pytest.mark.parametrize("value", ["qwen3:4b-instruct-2507-q4_K_M", "llama3.2", "org/model:tag", "a" * 255])
def test_model_name_accepts(value):
    assert ok("gateway", {"fallback_model": value}) == {"fallback_model": value}


@pytest.mark.parametrize("value", ["", "a b", "a" * 256, "m;rm", "m\n", "m$x", "mé"])
def test_model_name_rejects(value):
    bad("gateway", {"fallback_model": value})


def test_room_accepts_a_name_and_rejects_reserved_words_and_long_names():
    assert ok("jarvis_web", {"default_room": "Living room"})["default_room"] == "Living room"
    assert ok("jarvis_web", {"default_room": "Kid's room & den"})
    for value in ("whole house", "Whole House", "here", "all", "everywhere", "my room", "this room", "a" * 65, "", "x<y"):
        bad("jarvis_web", {"default_room": value})
    assert ok("jarvis_web", {"default_room": "a" * 64})


def test_room_key_and_reserved_words():
    assert hs.room_key("Living  Room!") == "living_room"
    assert hs.room_key("") == "" and hs.room_key(None) == ""
    assert hs.is_reserved_room("whole house") and hs.is_reserved_room("In Here") and not hs.is_reserved_room("kitchen")
    assert hs.RESERVED_ROOM_KEYS >= frozenset({
        "whole_house", "house", "all", "everywhere", "unknown", "multi_room",
        "here", "this_room", "my_room", "the_room", "in_here",
    })
    assert hs.RESERVED_ROOM_KEYS - {
        "whole_house", "house", "all", "everywhere", "unknown", "multi_room",
        "here", "this_room", "my_room", "the_room", "in_here",
    } == {"the_whole_house", "entire_house", "every_room", "all_rooms", "all_the_rooms",
          "throughout_the_house", "all_over_the_house", "my_current_room"}


# -- temperature pair ------------------------------------------------------------------------------------

@pytest.mark.parametrize("low,high", [(65, 75), (70, 70), (40, 95)])
def test_temperature_pair_accepts(low, high):
    assert ok("jarvis_web", {"min_temp": low, "max_temp": high})["max_temp"] == high


@pytest.mark.parametrize("value", [
    {"min_temp": 76, "max_temp": 75}, {"min_temp": 39, "max_temp": 75}, {"min_temp": 65, "max_temp": 96},
    {"min_temp": 65}, {"max_temp": 75}, {"min_temp": "65", "max_temp": 75}, {"min_temp": 65.5, "max_temp": 75},
    {"min_temp": True, "max_temp": 75},
])
def test_temperature_pair_rejects(value):
    bad("jarvis_web", value)


# -- lat/lon and bounds ------------------------------------------------------------------------------------

def _service(**over):
    base = {"id": "ferry", "name": "Ferry", "frequency_minutes": 20, "stops": [{"name": "A", "lat": 39.7, "lon": -105.0}]}
    base.update(over)
    return {"static_services": [base]}


@pytest.mark.parametrize("lat,lon", [(90, 180), (-90, -180), (0, 0), (39.7527, -105.0089)])
def test_lat_lon_accepts(lat, lon):
    assert ok("transit", _service(stops=[{"name": "A", "lat": lat, "lon": lon}]))


@pytest.mark.parametrize("lat,lon", [
    (90.0001, 0), (-90.0001, 0), (0, 180.0001), (0, -180.0001),
    (float("nan"), 0), (0, float("inf")), (float("-inf"), 0), ("39.7", 0),
])
def test_lat_lon_rejects(lat, lon):
    bad("transit", _service(stops=[{"name": "A", "lat": lat, "lon": lon}]))


def test_feed_bounds_must_be_ordered_and_in_range():
    box = {"min_lat": 39.5, "max_lat": 39.9, "min_lon": -105.3, "max_lon": -104.7}
    assert ok("transit", {"gtfs_feeds": [{**FEED, "bounds": box}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "bounds": {**box, "min_lat": 40}}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "bounds": {**box, "max_lon": -106}}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "bounds": {**box, "max_lat": 91}}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "bounds": {"min_lat": 1, "max_lat": 2}}]})


# -- lamp exclusion globs -----------------------------------------------------------------------------------

@pytest.mark.parametrize("glob", ["light.kitchen_led", "light.porch_*", "light.a*b*c*", "light.x_led_ring"])
def test_glob_accepts(glob):
    assert ok("lights", {"room_light_exclude": [glob]}) == {"room_light_exclude": [glob]}


@pytest.mark.parametrize("glob", [
    "*", "light.*", "**x", "light.a*b*c*d*", "light.a+", "light.a.b", "light.a**b", "light.", "switch.porch_*",
    "light." + "a" * 123, "Light.Porch_*", "light.porch_* ", ".*", "light.[a]",
])
def test_glob_rejects(glob):
    bad("lights", {"room_light_exclude": [glob]})


def test_a_128_character_glob_is_the_longest_and_the_fifty_first_item_is_refused():
    assert ok("lights", {"room_light_exclude": ["light." + "a" * 122]})
    assert ok("lights", {"room_light_exclude": [f"light.l{i}" for i in range(50)]})
    bad("lights", {"room_light_exclude": [f"light.l{i}" for i in range(51)]})


def test_an_empty_list_is_a_value_and_null_is_unset():
    assert ok("lights", {"room_light_exclude": []}) == {"room_light_exclude": []}
    assert ok("lights", {"room_light_exclude": None}) == {"room_light_exclude": None}
    assert hs.stored_flags("lights", hs.validate_update("lights", {"room_light_exclude": []})) == {"room_light_exclude": True}
    assert hs.stored_flags("lights", hs.validate_update("lights", {})) == {"room_light_exclude": False}
    assert hs.stored_flags("lights", None) == {"room_light_exclude": False}


def test_glob_to_regex_matches_what_it_says_and_nothing_else():
    rx = re.compile(hs.glob_to_regex("light.porch_*"))
    assert rx.search("light.porch_front") and rx.search("light.porch_")
    assert not rx.search("light.porch") and not rx.search("light.back_porch_x") and not rx.search("xlight.porch_a")
    exact = re.compile(hs.glob_to_regex("light.kitchen_led"))
    assert exact.search("light.kitchen_led") and not exact.search("light.kitchen_led2") and not exact.search("light.kitchenXled")
    assert not exact.search("light.kitchen_led\n")
    with pytest.raises(Exception):
        hs.glob_to_regex("light.*")


@pytest.mark.parametrize("glob", ["light.a*a*a*b", "light.a*a*a*", "light.a*a*a*a"])
def test_the_worst_case_glob_is_fast_on_the_longest_entity_id(glob):
    rx = re.compile(hs.glob_to_regex(glob))
    entity = "light." + "a" * 249
    started = time.perf_counter()
    for _ in range(5):
        rx.search(entity)
    assert time.perf_counter() - started < 1.0


# -- URLs -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://example.org/f.zip", "http://example.org", "https://example.org:8443/a?k=v",
                                 "https://8.8.8.8/f", "https://sub.example.co.uk/x"])
def test_url_accepts(url):
    assert ok("transit", {"gtfs_feeds": [{**FEED, "url": url}]})["gtfs_feeds"][0]["url"] == url


@pytest.mark.parametrize("url,kind", [
    ("https://user:pw@example.org/f", "url_userinfo"), ("ftp://example.org/f", "url_scheme"),
    ("file:///etc/passwd", "url_scheme"), ("javascript:alert(1)", "url_scheme"),
    ("http://localhost/f", "url_private_host"), ("http://LOCALHOST./f", "url_private_host"),
    ("http://a.localhost/f", "url_private_host"), ("http://intranet/f", "url_private_host"),
    ("http://10.0.0.1/f", "url_private_host"), ("http://192.168.1.1/f", "url_private_host"),
    ("http://127.0.0.1/f", "url_private_host"), ("http://0x7f000001/f", "url_private_host"),
    ("http://2130706433/f", "url_private_host"), ("http://0177.0.0.1/f", "url_private_host"),
    ("http://[fd00::1]/f", "url_private_host"), ("http://[::1]/f", "url_private_host"),
    ("http://[::ffff:127.0.0.1]/f", "url_private_host"),
    ("http://169.254.169.254/latest", "url_private_host"), ("http://100.64.0.1/f", "url_private_host"),
    ("http://0.0.0.0/f", "url_private_host"), ("http://224.0.0.1/f", "url_private_host"),
    ("http://svc.ns.svc/f", "url_private_host"), ("http://a.cluster.local/f", "url_private_host"),
    ("https://example.org/f#frag", "url_fragment"), ("https://example.org/f#", "url_fragment"),
    ("https://example.org/•••", "url_mask_token"),
    ("https://example.org/a b", "url_invalid"), ("https://example.org/a\nb", "url_invalid"),
    ("https://example.org\\@evil.org/", "url_invalid"), ("", "url_invalid"), ("https://", "url_invalid"),
    ("https://example.org:99999/", "url_invalid"), ("https://[::1/f", "url_invalid"),
    ("https://example.1/f", "url_invalid"), ("https://-bad.example.org/", "url_invalid"),
    ("https://" + "a" * 64 + ".org/", "url_invalid"), ("https://exämple.org/", "url_invalid"),
    ("https://example.org/" + "a" * 2100, "url_invalid"),
])
def test_url_rejects(url, kind):
    bad("transit", {"gtfs_feeds": [{**FEED, "url": url}]}, kind)


def test_the_private_host_error_names_nothing():
    secret = "http://10.9.8.7/f?api_key=SECRET"
    errors = bad("transit", {"gtfs_feeds": [{**FEED, "url": secret}]})
    assert "SECRET" not in repr(errors) and "10.9.8.7" not in repr(errors)


def test_url_host_and_mask_url_show_scheme_and_host_only():
    assert hs.url_host("https://Feeds.Example.org:8443/a/b?api_key=SECRET#x") == "feeds.example.org"
    assert hs.url_host("not a url") == "unknown" and hs.url_host(None) == "unknown" and hs.url_host("https://[::1") == "unknown"
    assert hs.mask_url("https://example.org/f.zip?api_key=SECRET") == "https://example.org"
    assert hs.mask_url("http://Example.org:8080/x") == "http://example.org:8080"
    assert hs.mask_url("https://[2001:db8::1]/x") == "https://[2001:db8::1]"
    assert hs.mask_url("junk") == "" and hs.mask_url(None) == ""


@pytest.mark.parametrize("path", ["/events/", "/", "/a/b?x=1"])
def test_link_path_accepts(path):
    assert ok("community_events", {"sources": [_source(link_path=path)]})


@pytest.mark.parametrize("path", ["//x", "http://x", "events", "/a\\b", "/a b", "/" + "a" * 200, "", "/a\n"])
def test_link_path_rejects(path):
    bad("community_events", {"sources": [_source(link_path=path)]})


def _source(**over):
    base = {"id": "downtown", "name": "Downtown", "type": "link_scan", "url": "https://example.org/events"}
    base.update(over)
    return base


# -- lists and items -------------------------------------------------------------------------------------

def test_lists_accept_their_caps():
    feeds = [{**FEED, "id": f"f{i}"} for i in range(20)]
    assert len(ok("transit", {"gtfs_feeds": feeds})["gtfs_feeds"]) == 20
    stops = [{"name": f"S{i}", "lat": 1.0, "lon": 2.0} for i in range(500)]
    assert len(ok("transit", _service(stops=stops))["static_services"][0]["stops"]) == 500
    assert len(ok("community_events", {"sources": [_source(id=f"s{i}") for i in range(20)]})["sources"]) == 20


def test_lists_reject_one_over_the_cap_and_duplicates():
    bad("transit", {"gtfs_feeds": [{**FEED, "id": f"f{i}"} for i in range(21)]})
    bad("transit", _service(stops=[{"name": f"S{i}", "lat": 1.0, "lon": 2.0} for i in range(501)]))
    bad("community_events", {"sources": [_source(id=f"s{i}") for i in range(21)]})
    bad("transit", {"gtfs_feeds": [FEED, {**FEED, "name": "Other"}]}, "duplicate_id")
    bad("community_events", {"sources": [_source(), _source(name="Other")]}, "duplicate_id")


def test_a_huge_list_is_refused_before_its_items_are_judged():
    errors = bad("transit", {"gtfs_feeds": [{}] * 5000})
    assert errors == [{"loc": ("gtfs_feeds",), "type": "too_long"}]


@pytest.mark.parametrize("max_bytes", [0, -1, 100 * 2**20 + 1, 1024, "5", 2.5])
def test_max_bytes_rejects(max_bytes):
    bad("transit", {"gtfs_feeds": [{**FEED, "max_bytes": max_bytes}]})
    bad("community_events", {"sources": [_source(max_bytes=max_bytes)]})


@pytest.mark.parametrize("max_bytes", [2**20, 50 * 2**20, 100 * 2**20])
def test_max_bytes_accepts(max_bytes):
    assert ok("transit", {"gtfs_feeds": [{**FEED, "max_bytes": max_bytes}]})


def test_allow_private_is_not_a_field_anywhere():
    bad("transit", {"gtfs_feeds": [{**FEED, "allow_private": True}]}, "extra_forbidden")
    bad("transit", {"gtfs_feeds": [{**FEED, "allow_private": False}]}, "extra_forbidden")
    bad("community_events", {"sources": [_source(allow_private=True)]}, "extra_forbidden")


def test_item_ids_and_names():
    for bad_id in ("", "Feed", "a b", "-a", "a/b", "a" * 65):
        bad("transit", {"gtfs_feeds": [{**FEED, "id": bad_id}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "name": ""}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "name": "a‮b"}]})
    bad("transit", {"gtfs_feeds": [{**FEED, "name": "x" * 101}]})


def test_static_service_hours_and_frequency():
    hours = {"weekday": {"start": "07:00", "end": "19:00"}, "weekend": None}
    assert ok("transit", _service(hours=hours, type="ferry", free=True))["static_services"][0]["hours"]["weekend"] is None
    for hhmm in ("7:00", "24:00", "07:60", "0700", "07:00:00"):
        bad("transit", _service(hours={"weekday": {"start": hhmm, "end": "19:00"}}))
    bad("transit", _service(frequency_minutes=0))
    bad("transit", _service(frequency_minutes=1441))
    bad("transit", {"static_services": [{"id": "x", "name": "X", "stops": []}]})


def test_community_source_types_and_free_text():
    for kind in hs.SOURCE_TYPES:
        assert ok("community_events", {"sources": [_source(type=kind)]})
    bad("community_events", {"sources": [_source(type="rss")]})
    assert ok("community_events", {"sources": [_source(regions=["Downtown", "LoDo"], location_match=["Denver"])]})
    bad("community_events", {"sources": [_source(regions=["x"] * 21)]})
    bad("community_events", {"sources": [_source(regions=["x" * 101])]})
    bad("community_events", {"sources": [_source(regions=["a\x00b"])]})
    bad("community_events", {"sources": [_source(location_match=[""])]})
    bad("community_events", {"sources": [_source(default_city="a​b")]})
    bad("community_events", {"sources": [_source(base_url="http://10.0.0.1")]})
    assert ok("community_events", {"sources": [_source(base_url="https://example.org")]})["sources"][0]["base_url"] == "https://example.org"


def test_validate_update_shape_errors():
    assert hs.validate_update.__name__ == "validate_update"
    with pytest.raises(hs.SettingsValidationError) as caught:
        hs.validate_update("nope", {})
    assert caught.value.errors == [{"loc": ("section",), "type": "unknown_section"}]
    for notdict in (None, [], "x", 1):
        with pytest.raises(hs.SettingsValidationError) as caught:
            hs.validate_update("amtrak", notdict)
        assert caught.value.errors == [{"loc": (), "type": "dict_type"}]


def test_errors_never_carry_input_or_context():
    secret = "SECRET-INPUT-VALUE"
    for section, value in (
        ("sms", {"default_country_code": secret}), ("amtrak", {"default_station": secret}),
        ("gateway", {"fallback_model": secret + " "}), ("jarvis_web", {"climate_entity": secret}),
        ("transit", {"gtfs_feeds": [{**FEED, "url": "https://example.org/?k=" + secret + "\n"}]}),
        ("lights", {"room_light_exclude": [secret]}), ("bed_warmer", {secret: 1}),
    ):
        errors = bad(section, value)
        assert secret not in repr(errors) or section == "bed_warmer"  # an unknown key's own name is the loc
    assert all(len(p) <= 64 for e in bad("bed_warmer", {"k" * 500: 1}) for p in e["loc"])


# -- the stored shape and tolerant parsing ------------------------------------------------------------------

def test_stored_value_round_trips_with_the_documented_shape():
    model = hs.validate_update("transit", {"region_name": "Denver", "gtfs_feeds": [FEED]})
    text = hs.stored_value("transit", model, 4)
    import json

    assert json.loads(text) == {"v": 1, "revision": 4, "value": model.model_dump(mode="json")}
    parsed, revision = hs.parse_stored("transit", text)
    assert revision == 4 and parsed == model


def test_parse_stored_missing_unreadable_and_odd_revisions():
    assert hs.parse_stored("sms", None) == (None, 0)
    assert hs.parse_stored("sms", "not json") == (None, 0)
    assert hs.parse_stored("sms", "[1]") == (None, 0)
    assert hs.parse_stored("sms", '{"v":1,"revision":-3,"value":{}}')[1] == 0
    assert hs.parse_stored("sms", '{"v":1,"revision":true,"value":{}}')[1] == 0
    assert hs.parse_stored("sms", '{"v":1,"revision":2,"value":[]}') == (None, 2)


def test_an_unknown_version_gives_none_and_an_error_log(caplog):
    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        parsed, revision = hs.parse_stored("sms", '{"v":2,"revision":7,"value":{"default_country_code":"44"}}')
    assert (parsed, revision) == (None, 7)
    assert any("household_setting_unknown_version" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    assert "44" not in caplog.text


def test_an_invalid_field_becomes_unset_and_is_logged_by_name_only(caplog):
    raw = '{"v":1,"revision":1,"value":{"climate_entity":"switch.SECRETVALUE","oven_entity":"water_heater.ge_oven"}}'
    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        model, _ = hs.parse_stored("jarvis_web", raw)
    assert model.climate_entity is None and model.oven_entity == "water_heater.ge_oven"
    assert "field=climate_entity" in caplog.text and "SECRETVALUE" not in caplog.text


def test_a_dropped_item_keeps_its_siblings(caplog):
    feeds = [FEED, {**FEED, "id": "bad", "url": "http://10.0.0.1/f"}, {**FEED, "id": "c", "name": "C"}]
    import json

    raw = json.dumps({"v": 1, "revision": 1, "value": {"gtfs_feeds": feeds, "region_name": "Denver"}})
    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        model, _ = hs.parse_stored("transit", raw)
    assert [f.id for f in model.gtfs_feeds] == ["rtd_bus", "c"] and model.region_name == "Denver"
    assert "index=1" in caplog.text and "10.0.0.1" not in caplog.text


def test_duplicate_ids_in_a_stored_list_keep_the_first():
    import json

    raw = json.dumps({"v": 1, "revision": 1, "value": {"gtfs_feeds": [FEED, {**FEED, "name": "Second"}]}})
    model, _ = hs.parse_stored("transit", raw)
    assert [f.name for f in model.gtfs_feeds] == ["RTD Local Bus"]


def test_an_invalid_unit_becomes_unset_as_a_whole():
    import json

    raw = json.dumps({"v": 1, "revision": 1, "value": {"min_temp": 80, "max_temp": 70, "climate_entity": "climate.t"}})
    model, _ = hs.parse_stored("jarvis_web", raw)
    assert (model.min_temp, model.max_temp, model.climate_entity) == (None, None, "climate.t")
    half = json.dumps({"v": 1, "revision": 1, "value": {"min_temp": 66}})
    model, _ = hs.parse_stored("jarvis_web", half)
    assert (model.min_temp, model.max_temp) == (None, None)


def test_unknown_stored_fields_are_ignored():
    model = hs.parse_value("sms", {"default_country_code": "44", "from_the_future": 1})
    assert model.default_country_code == "44"


# -- masking ------------------------------------------------------------------------------------------------

SECRET_URL = "https://example.org/gtfs.zip?api_key=SECRET"


def _transit(url=SECRET_URL):
    return hs.validate_update("transit", {"gtfs_feeds": [{**FEED, "url": url}]})


def test_mask_for_user_shows_scheme_and_host_with_the_flag():
    masked = hs.mask_for_user("transit", _transit())
    assert masked["gtfs_feeds"][0]["url"] == "https://example.org" and masked["gtfs_feeds"][0]["url_masked"] is True
    assert "SECRET" not in repr(masked)
    plain = hs.dump("transit", _transit())
    assert plain["gtfs_feeds"][0]["url"] == SECRET_URL and "url_masked" not in plain["gtfs_feeds"][0]
    assert hs.mask_for_user("transit", None) is None and hs.dump("transit", None) is None
    assert hs.mask_for_user("sms", hs.validate_update("sms", {"default_country_code": "1"})) == {"default_country_code": "1"}


def test_community_base_url_is_masked_too():
    model = hs.validate_update("community_events", {"sources": [_source(base_url="https://example.org/base?token=T")]})
    item = hs.mask_for_user("community_events", model)["sources"][0]
    assert item["url"] == "https://example.org" and item["url_masked"] is True
    assert item["base_url"] == "https://example.org" and item["base_url_masked"] is True
    assert "T" not in repr(item).replace("True", "")


def test_a_masked_url_for_the_same_item_keeps_the_stored_url():
    stored = _transit()
    submitted = hs.mask_for_user("transit", stored)
    merged = hs.merge_masked("transit", submitted, stored)
    assert merged["gtfs_feeds"][0]["url"] == SECRET_URL and "url_masked" not in merged["gtfs_feeds"][0]
    assert hs.validate_update("transit", merged) == stored


def test_a_masked_url_edited_in_place_is_refused():
    stored = _transit()
    edited = hs.mask_for_user("transit", stored)
    edited["gtfs_feeds"][0]["url"] = "https://example.org/other"
    with pytest.raises(hs.SettingsValidationError) as caught:
        hs.merge_masked("transit", edited, stored)
    assert caught.value.errors == [{"loc": ("gtfs_feeds", 0, "url"), "type": "url_masked_edit"}]


def test_a_masked_flag_on_a_different_or_new_item_is_refused():
    stored = _transit()
    for ident in ("other", None):
        submitted = hs.mask_for_user("transit", stored)
        submitted["gtfs_feeds"][0]["id"] = ident
        with pytest.raises(hs.SettingsValidationError):
            hs.merge_masked("transit", submitted, stored)
    with pytest.raises(hs.SettingsValidationError):
        hs.merge_masked("transit", hs.mask_for_user("transit", stored), None)


def test_the_mask_token_in_a_url_is_refused_and_a_new_full_url_passes():
    with pytest.raises(hs.SettingsValidationError) as caught:
        hs.merge_masked("transit", {"gtfs_feeds": [{**FEED, "url": "https://example.org/•••"}]}, None)
    assert caught.value.errors[0]["type"] == "url_masked_edit"
    full = {"gtfs_feeds": [{**FEED, "url": "https://new.example.org/f?k=1"}]}
    assert hs.merge_masked("transit", full, _transit()) == full
    with pytest.raises(hs.SettingsValidationError):
        hs.merge_masked("transit", {"gtfs_feeds": [{**FEED, "url_masked": "yes"}]}, None)


def test_merge_masked_leaves_other_sections_and_odd_shapes_alone():
    assert hs.merge_masked("sms", {"default_country_code": "1"}, None) == {"default_country_code": "1"}
    assert hs.merge_masked("transit", "junk", None) == "junk"
    assert hs.merge_masked("transit", {"gtfs_feeds": "junk"}, None) == {"gtfs_feeds": "junk"}
    assert hs.merge_masked("transit", {"gtfs_feeds": ["junk"]}, None) == {"gtfs_feeds": ["junk"]}


# -- audit payloads -----------------------------------------------------------------------------------------

def test_values_sections_audit_the_values():
    old = hs.validate_update("sms", {"default_country_code": "1"})
    new = hs.validate_update("sms", {"default_country_code": "44"})
    old_value, new_value = hs.audit_payload("sms", old, new)
    assert old_value == {"section": "sms", "default_country_code": "1"}
    assert new_value == {"section": "sms", "default_country_code": "44", "changed_fields": ["default_country_code"]}
    assert hs.audit_payload("sms", None, new)[0] == {"section": "sms", "default_country_code": None}


def test_jarvis_web_audits_entities_but_the_room_only_as_changed():
    old = hs.validate_update("jarvis_web", {"default_room": "Kitchen", "climate_entity": "climate.a"})
    new = hs.validate_update("jarvis_web", {"default_room": "Den", "climate_entity": "climate.b"})
    old_value, new_value = hs.audit_payload("jarvis_web", old, new)
    assert "default_room" not in old_value and "default_room" not in new_value
    assert new_value["climate_entity"] == "climate.b" and old_value["climate_entity"] == "climate.a"
    assert new_value["changed_fields"] == ["climate_entity", "default_room"]
    assert "Den" not in repr(new_value) and "Kitchen" not in repr(old_value)


def test_amtrak_audits_changed_fields_only():
    new = hs.validate_update("amtrak", {"default_station": "NYP"})
    old_value, new_value = hs.audit_payload("amtrak", None, new)
    assert new_value == {"section": "amtrak", "changed_fields": ["default_station"]} and "NYP" not in repr(new_value)


def test_hosts_sections_audit_hosts_and_counts_never_urls_or_names():
    old = hs.validate_update("transit", {"gtfs_feeds": [{**FEED, "url": "https://old.example.org/p?key=SECRET"}]})
    new = hs.validate_update("transit", {"gtfs_feeds": [
        {**FEED, "url": "https://new.example.org/path?key=SECRET2"}, {**FEED, "id": "b", "name": "Private Name",
                                                                       "url": "https://old.example.org/q"},
    ]})
    old_value, new_value = hs.audit_payload("transit", old, new)
    assert new_value["hosts_added"] == ["new.example.org"] and new_value["hosts_removed"] == []
    assert new_value["counts"] == {"gtfs_feeds": 2, "static_services": 0} and old_value["counts"]["gtfs_feeds"] == 1
    blob = repr((old_value, new_value))
    for leaked in ("SECRET", "/path", "/p", "key=", "Private Name", "RTD"):
        assert leaked not in blob
    source = hs.validate_update("community_events", {"sources": [_source(url="https://events.example.org/x?t=1",
                                                                       base_url="https://base.example.org")]})
    _old, new_value = hs.audit_payload("community_events", None, source)
    assert new_value["hosts_added"] == ["base.example.org", "events.example.org"]


# -- SourcedStr and the jarvis-web resolver ---------------------------------------------------------------------

def test_sourced_str_is_a_string_that_remembers_its_source():
    value = hs.SourcedStr("NYP", "env")
    assert value == "NYP" and value.source == "env" and f"{value}!" == "NYP!" and value.lower() == "nyp"
    with pytest.raises(AttributeError):
        value.source = "admin"


def _jarvis(section=None, **env):
    model = hs.validate_update("jarvis_web", section) if section is not None else None
    return hs.resolve_jarvis_web(model, env)


def test_jarvis_resolver_defaults_and_env():
    j = _jarvis()
    assert (j.default_room, j.climate_entity, j.min_temp, j.max_temp, j.oven_entity) == ("guest", "climate.thermostat", 65, 75, "")
    assert j.source == "default"
    j = _jarvis(CLIMATE_ENTITY="climate.up", MIN_TEMP="60", OVEN_ENTITY_ID="water_heater.o")
    assert (j.climate_entity, j.min_temp, j.max_temp, j.oven_entity) == ("climate.up", 60, 75, "water_heater.o")
    assert j.sources["climate_entity"] == "env" and j.sources["default_room"] == "default" and j.source == "env"


def test_jarvis_resolver_admin_wins_and_the_bounds_are_a_unit():
    j = _jarvis({"climate_entity": "climate.admin", "min_temp": 62, "max_temp": 80}, CLIMATE_ENTITY="climate.env", MIN_TEMP="50", MAX_TEMP="90")
    assert (j.climate_entity, j.min_temp, j.max_temp) == ("climate.admin", 62, 80) and j.source == "admin"
    partial = hs.parse_value("jarvis_web", {"min_temp": 62})
    j = hs.resolve_jarvis_web(partial, {"MIN_TEMP": "50", "MAX_TEMP": "90"})
    assert (j.min_temp, j.max_temp) == (50, 90) and j.sources["min_temp"] == "env"
    j = hs.resolve_jarvis_web(partial, {})
    assert (j.min_temp, j.max_temp) == (65, 75) and j.sources["max_temp"] == "default"
    assert hs.resolve_jarvis_web(None, {"MIN_TEMP": "x"}).min_temp == 65


# -- the cache's pure helpers -------------------------------------------------------------------------------------

def test_snapshot_from_body_keeps_stored_sections_and_ignores_unknown_ones():
    body = {
        "sms": {"value": {"default_country_code": "44"}, "revision": 3},
        "amtrak": {"value": None, "revision": 0},
        "from_the_future": {"value": {"x": 1}, "revision": 9},
        "gateway": "junk",
    }
    snap = hs.snapshot_from_body(body, 12.5)
    assert set(snap.sections) == {"sms"} and snap.sections["sms"].default_country_code == "44"
    assert dict(snap.revisions) == {"sms": 3, "amtrak": 0} and snap.loaded and snap.loaded_at == 12.5
    assert hs.Snapshot().loaded is False and hs.Snapshot().loaded_at == 0.0
    with pytest.raises(Exception):
        snap.loaded = False  # frozen
