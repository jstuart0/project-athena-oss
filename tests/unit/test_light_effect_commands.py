"""Light effect commands.

Phase 2b: the single-room helper ``_room_light_targets`` returns what the
dispatcher's inline code resolved before the extraction. The reference in this
file is a transcription of that tail as it stood at 89084b9, returning its
values where the original went on to the gate and the writes.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import re
import textwrap
import types

import pytest

from .test_room_light_area_tier import Rig, _isolated, _state, light  # noqa: F401  (autouse fixture; sets sys.path and stubs)
import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from shared import household  # noqa: E402
from orchestrator.smart_home_controller import RoomLightTargets  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402
from orchestrator import access_target, light_effects, write_fanout  # noqa: E402
from shared import fast_path_vocab  # noqa: E402

from . import test_light_effects as fx  # noqa: E402

QUERY = "turn on the lights"
SCOPES = ("all_individual", "individual_lights", "group")
OWNER = ({"mode": "owner"}, "owner")
GUEST_DENIED = (mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\."]}), "guest")
BATHROOMS = {s["entity_id"]: s for s in [
    _state("light.north_bathroom_lamp"), _state("light.south_bathroom_lamp"),
]}
GONE_LIGHTS = {"study": ("light.gone_one", "light.gone_two")}

# (room, who, rig kwargs, service, outcome kind, records a denial). An ambiguous
# name whose every candidate is denied is refused without a recorded denial.
_ROWS = [
    (room, OWNER, {}, "turn_on", kind, False)
    for room, kind in (("galley", "targets"), ("study", "targets"), ("hall", "targets"),
                       ("attic", "all_down"), ("nowhere", "not_found"))
]
_ROWS += [
    ("bathroom", OWNER, {"registry": False, "states": BATHROOMS}, "turn_on", "ask_which", False),
    ("galley", GUEST_DENIED, {}, "turn_on", "denied_all", True),
    ("galley", GUEST_DENIED, {}, "turn_off", "denied_all", True),
    ("bathroom", GUEST_DENIED, {"registry": False, "states": BATHROOMS}, "turn_on", "denied_all", False),
    ("study", OWNER, {"admin_lights": GONE_LIGHTS}, "turn_on", "all_missing", False),
    ("study", OWNER, {"admin_lights": {"study": ("light.study_lamp", "light.gone_one")}}, "turn_on", "targets", False),
]
KINDS_EXPECTED = {"targets", "all_down", "not_found", "ask_which", "denied_all", "all_missing"}


async def _reference_targets(controller, room, intent, scope, per_light, service):
    """The single-room tail of the dispatcher as it read at 89084b9."""
    matches = await controller._resolve_room_lights(room)
    ambiguous = getattr(matches, "ambiguous_rooms", ())
    if ambiguous and not matches:
        resolved = await controller._resolve_ambiguous_room(room, ambiguous, intent, QUERY)
        if isinstance(resolved, str):
            return resolved
        room, matches = resolved
    label = controller._spoken_room(room)
    if not matches:
        if getattr(matches, "missing", 0):
            return f"The {label} lights aren't responding right now."
        return f"I couldn't find any lights in the {label}."
    individual = scope in ("all_individual", "individual_lights")
    ids = []
    if len(matches) > 1:
        for match in matches:
            if individual:
                ids.extend(match.get("members") or [match["entity_id"]])
            else:
                ids.append(match["entity_id"])
    else:
        only = matches[0]
        ids = (only.get("members") or [only["entity_id"]]) if individual else [only["entity_id"]]
    lt = await controller._finalize_light_targets(matches, ids=ids, per_light=per_light)
    if lt.denied_all:
        controller._record_light_denials(lt, service)
        return mp.permission_refusal_message(("light",), mp.current_ha_scope())
    total, down = await controller._light_availability(
        lt.gate_ids, refresh_key=room, missing=getattr(matches, "missing", 0),
    )
    if total and down == total:
        return f"The {label} lights aren't responding right now."
    return (lt.write_ids, lt.gate_ids, room, label, down, await controller._single_light_name(lt.gate_ids))


def _shape(result):
    if isinstance(result, str):
        return result
    return (result.lt.write_ids, result.lt.gate_ids, result.room, result.room_label,
            result.light_down, result.single_name)


def _kind(shape, room):
    if not isinstance(shape, str):
        return "targets"
    if shape.startswith("Which "):
        return "ask_which"
    if "guest mode" in shape.lower():
        return "denied_all"
    if shape.startswith("I couldn't find"):
        return "not_found"
    if shape == "The study lights aren't responding right now." and room == "study":
        return "all_missing"
    if "aren't responding" in shape:
        return "all_down"
    return f"other:{shape}"


def _run(monkeypatch, room, who, rig_kwargs, scope, service, use_helper):
    """(result shape, denials recorded) from a fresh rig and permission scope."""
    perms, mode = who
    rig_kwargs = dict(rig_kwargs)
    admin_lights = rig_kwargs.pop("admin_lights", None)
    rig = Rig(monkeypatch, **rig_kwargs)
    if admin_lights:
        monkeypatch.setattr(shc, "_room_lights_value", lambda: types.SimpleNamespace(
            mapping=admin_lights, source=household.SOURCE_ADMIN))
    intent = light(room, target_scope=scope)
    per_light = scope in ("all_individual", "individual_lights")

    async def go():
        with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(QUERY), trusted_voice=True):
            if use_helper:
                result = await rig.controller._room_light_targets(
                    room, intent, QUERY, target_scope=scope, per_light=per_light, denial_service=service,
                )
            else:
                result = await _reference_targets(rig.controller, room, intent, scope, per_light, service)
            denials = [(d.domain, d.service, tuple(d.targets), d.reason) for d in mp.current_ha_scope().denials]
            return _shape(result) if use_helper else result, denials

    return asyncio.run(go())


_CASES = [(row, scope) for row in _ROWS for scope in SCOPES]


@pytest.mark.parametrize("row,scope", _CASES, ids=[f"{r[0]}-{r[4]}-{r[3]}-{s}" for r, s in _CASES])
def test_room_light_targets_returns_the_same_targets_as_before(monkeypatch, row, scope):
    room, who, rig_kwargs, service, expected_kind, records = row
    before, before_denials = _run(monkeypatch, room, who, rig_kwargs, scope, service, use_helper=False)
    after, after_denials = _run(monkeypatch, room, who, rig_kwargs, scope, service, use_helper=True)
    assert after == before
    assert after_denials == before_denials
    kind = _kind(after, room)
    assert kind == expected_kind
    if records:
        assert after_denials and {d[1] for d in after_denials} == {service}
    else:
        assert after_denials == []


def test_room_light_targets_population_covers_a_named_member_and_each_outcome():
    """Each row asserts its own outcome kind, so the declared kinds are what ran."""
    assert len(_CASES) >= 12
    assert ("galley", "targets", "turn_on") in {(r[0], r[4], r[3]) for r, _ in _CASES}
    assert any(r[0] == "galley" and s == "all_individual" for r, s in _CASES)
    assert {r[4] for r, _ in _CASES} == KINDS_EXPECTED
    assert {r[3] for r, _ in _CASES if r[4] == "denied_all"} == {"turn_on", "turn_off"}


def test_room_light_targets_galley_reaches_the_leaves_and_names_no_single_light(monkeypatch):
    rig = Rig(monkeypatch)

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(QUERY),
                                    trusted_voice=True):
            return await rig.controller._room_light_targets(
                "galley", light("galley"), QUERY,
                target_scope="all_individual", per_light=True, denial_service="turn_on",
            )

    result = asyncio.run(go())
    assert isinstance(result, RoomLightTargets)
    assert len(result.lt.gate_ids) == 3
    assert result.single_name is None
    assert result.room_label == "galley"


def test_the_dispatcher_no_longer_resolves_or_finalizes_the_single_room_tail():
    from orchestrator.smart_home_controller import SmartHomeController
    calls = {
        getattr(node.func, "attr", "")
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(
            SmartHomeController._dispatch_light_or_room_command))))
        if isinstance(node, ast.Call)
    }
    assert {"_resolve_room_lights", "_resolve_ambiguous_room", "_finalize_light_targets",
            "_light_availability", "_single_light_name"}.isdisjoint(calls)
    assert {"_room_light_targets", "call_service"} <= calls


# ---------------------------------------------------------------------------
# Phase 2a: the claimed set, the reply wording, the gate wording
# ---------------------------------------------------------------------------

# One named member per table the claimed set is built from (plan D3).
CLAIMED_TABLE_MEMBERS = {
    "SCENE_PHRASES": ("movie mode", shc.SCENE_PHRASES),
    "AMBIENT_COLOR_COMMANDS": ("ocean", shc.AMBIENT_COLOR_COMMANDS),
    "BASIC_COLORS": ("magenta", shc.BASIC_COLORS),
    "WHOLE_HOUSE_LIGHT_PATTERNS": ("lights off everywhere", [p for p, _ in shc.WHOLE_HOUSE_LIGHT_PATTERNS]),
    "LIGHT_STATUS_PATTERNS": ("lights still on", shc.LIGHT_STATUS_PATTERNS),
    "TURN_ON_PHRASES": ("light me up", shc.TURN_ON_PHRASES),
    "HAS_COLOR_WORDS": ("steelers", shc.HAS_COLOR_WORDS),
    "CLASSIFY_COLOR_NAMES": ("random colors", shc.CLASSIFY_COLOR_NAMES),
    "TURN_OFF_PHRASES": ("lights out", access_target.TURN_OFF_PHRASES),
    "IMPLICIT_BRIGHTER": (access_target.IMPLICIT_BRIGHTER[0], access_target.IMPLICIT_BRIGHTER),
    "IMPLICIT_DIMMER": (access_target.IMPLICIT_DIMMER[0], access_target.IMPLICIT_DIMMER),
    "BRIGHTER_PATTERNS": (access_target.BRIGHTER_PATTERNS[0], access_target.BRIGHTER_PATTERNS),
    "DIMMER_PATTERNS": (access_target.DIMMER_PATTERNS[0], access_target.DIMMER_PATTERNS),
    "MOTION_CONTROL_PATTERNS": (access_target.MOTION_CONTROL_PATTERNS[0], access_target.MOTION_CONTROL_PATTERNS),
    "SCENE_TRIGGER_PHRASES": ("romantic mode", fast_path_vocab.SCENE_TRIGGER_PHRASES),
    "SCENE_OVERRIDE_PHRASES": ("party vibes", fast_path_vocab.SCENE_OVERRIDE_PHRASES),
}


@pytest.mark.parametrize("table", sorted(CLAIMED_TABLE_MEMBERS))
def test_claimed_set_comes_only_from_deterministic_tables(table):
    member, source = CLAIMED_TABLE_MEMBERS[table]
    assert member in source, f"{member!r} is not in {table}"
    assert shc.effect_word_claimed(member) is True
    assert all(shc.effect_word_claimed(phrase) for phrase in source), table


def test_the_claimed_tables_population_and_named_members():
    assert len(CLAIMED_TABLE_MEMBERS) >= 16
    assert CLAIMED_TABLE_MEMBERS["SCENE_TRIGGER_PHRASES"][0] == "romantic mode"


def test_romantic_mode_is_claimed_through_the_shared_scene_vocabulary():
    """F90: the shared fast-path vocabulary is a source, not only the controller's own table."""
    assert "romantic mode" in fast_path_vocab.SCENE_TRIGGER_PHRASES
    assert "romantic mode" in fast_path_vocab.SCENE_OVERRIDE_PHRASES
    assert shc.effect_word_claimed("romantic mode") and shc.effect_word_claimed("romantic")
    assert shc.effect_word_claimed("Romantic  Mode!")


@pytest.mark.parametrize("phrase", ["candlelight", "home", "glow", "twinkle", "snowflake", "fire 2012", "zebra drift", "", "x" * 400])
def test_phrases_no_table_owns_are_not_claimed(phrase):
    assert shc.effect_word_claimed(phrase) is False


def _table_phrases():
    return [p for _, src in CLAIMED_TABLE_MEMBERS.values() for p in src]


def test_every_table_phrase_ending_in_a_mode_word_has_its_head_claimed():
    """D3(b), per phrase: "night mode" claims "night" (D15), "party vibes" claims "party"."""
    suffixed = [light_effects.normalize(p) for p in _table_phrases()
                if light_effects.normalize(p).rpartition(" ")[2] in shc._MODE_SUFFIXES and " " in light_effects.normalize(p)]
    assert len(suffixed) >= 15 and {"night mode", "morning mode", "movie mode", "party vibes"} <= set(suffixed)
    for phrase in suffixed:
        assert shc.effect_word_claimed(phrase.rpartition(" ")[0]), phrase


@pytest.mark.parametrize("phrase", ["good", "date", "wake", "heading", "time", "mode", "vibes"])
def test_a_word_that_is_only_part_of_a_table_phrase_is_not_claimed(phrase):
    assert shc.effect_word_claimed(phrase) is False


FRESH = "zq fresh phrase"


def _with_fresh(monkeypatch, owner, name):
    value = getattr(owner, name)
    if isinstance(value, dict):
        patched = {**value, FRESH: value[next(iter(value))]}
    elif value and isinstance(value[0], tuple):
        patched = type(value)([*value, (FRESH, "turn_on")])
    else:
        patched = type(value)([*value, FRESH])
    monkeypatch.setattr(owner, name, patched)


SOURCES = [(shc, n) for n in ("SCENE_PHRASES", "AMBIENT_COLOR_COMMANDS", "BASIC_COLORS", "WHOLE_HOUSE_LIGHT_PATTERNS",
                                "LIGHT_STATUS_PATTERNS", "TURN_ON_PHRASES", "HAS_COLOR_WORDS", "CLASSIFY_COLOR_NAMES",
                                "SCENE_TRIGGER_PHRASES", "SCENE_OVERRIDE_PHRASES")] + [
    (access_target, n) for n in ("TURN_OFF_PHRASES", "IMPLICIT_BRIGHTER", "IMPLICIT_DIMMER", "BRIGHTER_PATTERNS",
                                 "DIMMER_PATTERNS", "MOTION_CONTROL_PATTERNS")]


@pytest.mark.parametrize("owner,name", SOURCES, ids=[n for _, n in SOURCES])
def test_each_claimed_source_is_read_when_the_set_is_built(monkeypatch, owner, name):
    """F116: a phrase added to ANY source table is claimed after a rebuild; dropping a source would fail one of these."""
    assert FRESH not in shc._build_effect_claimed_phrases()
    _with_fresh(monkeypatch, owner, name)
    assert FRESH in shc._build_effect_claimed_phrases()


def test_the_claimed_source_list_is_the_whole_deterministic_table_set():
    assert len(SOURCES) == 16 == len(CLAIMED_TABLE_MEMBERS)


def test_a_cue_evidence_colour_word_no_table_owns_is_not_claimed():
    only_cue = sorted(w for w in access_target.COLOR_WORDS if not any(
        w in {light_effects.normalize(p) for p in src} for _, src in CLAIMED_TABLE_MEMBERS.values()))
    assert only_cue, "every cue colour word is also in a table; the exclusion is untested"
    assert not any(shc.effect_word_claimed(w) for w in only_cue[:20])


def test_the_colour_routes_use_the_hoisted_tables():
    import orchestrator.main as main_module
    assert main_module.CLASSIFY_COLOR_NAMES is shc.CLASSIFY_COLOR_NAMES
    source = inspect.getsource(main_module.classify_node)
    assert "color_names = CLASSIFY_COLOR_NAMES" in source and "'random colors'" not in source


# -- reply wording (D5) -----------------------------------------------------

@pytest.mark.parametrize("kwargs,expected", [
    (dict(single_name="Study Prism", effect="candlelight"), "Set the Study Prism to candlelight."),
    (dict(single_name="Study Prism", effect="candlelight", unsupported=1), "Set the Study Prism to candlelight. 1 other light doesn't have that effect."),
    (dict(single_name="Study Prism", effect="candlelight", unsupported=3), "Set the Study Prism to candlelight. 3 other lights don't have that effect."),
    (dict(rooms=["study"], effect="candlelight", unsupported=2), "Set the study lights to candlelight. 2 lights don't have that effect."),
    (dict(rooms=["study"], effect="candlelight", unsupported=1), "Set the study lights to candlelight. 1 light doesn't have that effect."),
    (dict(rooms=["study"], effect="twinkle", failed=1), "Set the study lights to twinkle. 1 didn't respond."),
    (dict(rooms=["study"], effect="twinkle", unsupported=2, unavailable=1, failed=2),
     "Set the study lights to twinkle. 2 lights don't have that effect. 1 isn't responding. 2 didn't respond."),
])
def test_set_effect_reply_wording(kwargs, expected):
    assert shc.light_reply("set_effect", **kwargs) == expected


@pytest.mark.parametrize("kwargs,expected", [
    (dict(single_name="Study Prism"), "Turned off the effect on the Study Prism."),
    (dict(rooms=["study"]), "Turned off the effect on the study lights."),
    (dict(single_name="Study Prism", unsupported=1), "Turned off the effect on the Study Prism. 1 other light had no effect running."),
    (dict(rooms=["study"], unsupported=2), "Turned off the effect on the study lights. 2 lights had no effect running."),
])
def test_clear_effect_reply_wording(kwargs, expected):
    assert shc.light_reply("clear_effect", **kwargs) == expected


def test_the_existing_reply_wording_is_unchanged_by_the_new_arguments():
    assert shc.light_reply("turn_on", rooms=["study"]) == "Turned on the study lights."
    assert shc.light_reply("set_color", single_name="Lamp", colour="blue", unavailable=1) == "Set the Lamp to blue. 1 isn't responding."


@pytest.mark.parametrize("action,kwargs", [("set_effect", dict(effect="candlelight", unsupported=2, failed=1)),
                                           ("clear_effect", dict(unsupported=2))])
def test_effect_replies_name_at_most_one_light_and_never_an_entity_id(action, kwargs):
    for extra in (dict(rooms=["study"]), dict(single_name="Study Prism"), dict(whole_house=True)):
        text = shc.light_reply(action, **extra, **kwargs)
        assert not re.search(r"\blight\.[a-z0-9_]+", text) and text.count("Study Prism") <= 1


SAMPLES = {
    "EFFECT_NONE_RUNNING": dict(target="the Study Prism"),
    "EFFECT_NOT_FOUND": dict(target="the study lights"),
    "EFFECT_NOT_FOUND_TRY": dict(target="the study lights", options="twinkle, sparkle or fire 2012"),
    "EFFECT_UNSUPPORTED_DEVICE": dict(target="The Study Prism"),
    "EFFECT_NAME_NOT_IN_ROOM": dict(name="hexagon", room="study"),
    "EFFECT_CANT_CLEAR": dict(target="the study lights"),
}


def test_fixed_effect_answers_that_name_nothing_are_registered():
    for text in (shc.EFFECT_NEEDS_WORDS, shc.EFFECT_WRITE_FAILED):
        assert shc.is_refusal_text(text)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_formatted_effect_answer_is_a_refusal(name):
    """D17: each template is matched as a whole string, so the formatted answer is not remembered as a done command."""
    text = getattr(shc, name).format(**SAMPLES[name])
    assert shc.is_refusal_text(text), text
    assert not shc.is_refusal_text("Sure. " + text) and not shc.is_refusal_text(text + " Anything else?")
    assert not shc.is_refusal_text(text.replace(" ", "", 1)[: len(text) // 2])


def test_the_effect_answer_samples_cover_every_template_and_a_missing_field_is_no_refusal():
    templates = {n for n in dir(shc) if n.startswith("EFFECT_") and "{" in getattr(shc, n)}
    assert templates == set(SAMPLES)
    assert not shc.is_refusal_text("There's no effect running on .")
    assert not shc.is_refusal_text("There's no effect running on " + "x" * 400 + ".")
    assert not shc.is_refusal_text("Turned off the effect on the study lights.")


def test_light_reply_with_no_effect_never_says_to_the_effect():
    text = shc.light_reply("set_effect", rooms=["study"], effect=None)
    assert "to the effect" not in text and text == "Set the effect on the study lights."
    assert shc.light_reply("set_effect", single_name="Lamp", effect="") == "Set the effect on the Lamp."


def test_the_ambient_colour_fast_path_hands_out_copies():
    rig = Rig(pytest.MonkeyPatch())
    query = "set the study to sunset"
    first = asyncio.run(rig.controller._fast_path_intent(query, query, None, False, False, True, None))
    first["parameters"]["hs_colors"][0][0] = 999
    again = asyncio.run(rig.controller._fast_path_intent(query, query, None, False, False, True, None))
    assert again["parameters"]["hs_colors"][0][0] == 20 == shc.AMBIENT_COLOR_COMMANDS["sunset"]["hs_colors"][0][0]


# -- gate wording (D12) -----------------------------------------------------

def _config(threshold=6, hard_limit=18):
    return types.SimpleNamespace(ha_write_fanout_confirm_threshold=threshold, ha_write_fanout_hard_limit=hard_limit)


def _gate_prompt(monkeypatch, service, detail, query, count=8, carry=True):
    monkeypatch.setattr(write_fanout, "get_config", lambda: _config())
    with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)) as scope:
        scope.can_carry_pending = carry
        answer = write_fanout.gate("light", service, tuple(f"light.l{i}" for i in range(count)), query,
                                   room="study", detail=detail)
        return answer, scope.fanout_block


def test_gate_prompt_names_the_effect(monkeypatch):
    answer, block = _gate_prompt(monkeypatch, "set_effect", "candlelight", "light up the study with something nice")
    assert answer == "That would set the candlelight effect on 8 lights in the study. Should I go ahead?"
    assert block.writes[0].detail == "candlelight"


def test_gate_prompt_for_a_clear_and_for_an_effect_without_a_detail(monkeypatch):
    answer, _ = _gate_prompt(monkeypatch, "clear_effect", None, "light up the study with something nice")
    assert answer == "That would turn off the effect on 8 lights in the study. Should I go ahead?"
    answer, _ = _gate_prompt(monkeypatch, "set_effect", None, "light up the study with something nice")
    assert answer == "That would change the effect on 8 lights in the study. Should I go ahead?"


def test_gate_detail_does_not_change_replay_matching(monkeypatch):
    """A replay matches by entity ids, so what the change is called plays no part."""
    answer, block = _gate_prompt(monkeypatch, "set_effect", "candlelight", "light up the study with something nice")
    ids = {e for w in block.writes for e in w.entity_ids}
    monkeypatch.setattr(write_fanout, "get_config", lambda: _config())
    with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("yes")) as scope:
        scope.confirmed_entity_ids = ids
        assert write_fanout.gate("light", "set_effect", tuple(sorted(ids)), "yes", room="study", detail="a different name") is None


def test_two_writes_that_differ_only_in_a_non_empty_detail_are_equal():
    first = write_fanout.PlannedWrite("light", "set_effect", ("light.a",), "candlelight")
    second = write_fanout.PlannedWrite("light", "set_effect", ("light.a",), "twinkle")
    assert first.detail != second.detail and first == second and hash(first) == hash(second)
    assert first != write_fanout.PlannedWrite("light", "set_effect", ("light.b",), "candlelight")


@pytest.mark.parametrize("detail", ["<b>", "a{b}", "x" * 65, "line\nbreak", "tick`", 5, ["a"]])
def test_a_planned_write_refuses_an_unclean_detail(detail):
    with pytest.raises(ValueError):
        write_fanout.PlannedWrite("light", "set_effect", ("light.a",), detail)


@pytest.mark.parametrize("detail", ["", "candlelight", "fire 2012", "x" * 64, "sparkle+"])
def test_a_planned_write_accepts_a_clean_detail(detail):
    assert write_fanout.PlannedWrite("light", "set_effect", ("light.a",), detail).detail == detail


def test_the_list_form_of_gate_refuses_a_detail_argument_and_differing_details_are_not_merged(monkeypatch):
    monkeypatch.setattr(write_fanout, "get_config", lambda: _config())
    a = write_fanout.PlannedWrite("light", "set_effect", ("light.a",), "candlelight")
    b = write_fanout.PlannedWrite("light", "set_effect", ("light.b",), "twinkle")
    with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("set it")):
        with pytest.raises(TypeError):
            write_fanout.gate([a], "set it", detail="candlelight")
        with pytest.raises(ValueError):
            write_fanout.gate_many([a, b], "set it")
        same = write_fanout.PlannedWrite("light", "set_effect", ("light.b",), "candlelight")
        assert write_fanout.gate_many([a, same], "set it") is None


def test_a_stored_pending_write_without_a_detail_still_builds_a_planned_write():
    stored = ["light", "set_effect", ["light.a", "light.b"]]
    assert write_fanout.PlannedWrite(stored[0], stored[1], tuple(stored[2])).detail == ""


REWORDING_EFFECTS = ["candlelight", "fire 2012", "zebra drift", "sunset", "romantic", "movie", "night", "morning"]


@pytest.mark.parametrize("effect", REWORDING_EFFECTS)
def test_the_gate_rewording_reads_as_a_set_request_the_recogniser_takes(effect):
    """F84/F115: "set all the {room} lights to {effect} effect" is T1 with the word "effect", so it
    is taken even for a name an existing path owns."""
    block = write_fanout.FanoutBlock(
        writes=(write_fanout.PlannedWrite("light", "set_effect", tuple(f"light.l{i}" for i in range(8)), effect),),
        unbounded=False, room="study")
    command = write_fanout.rewording(block).split("To do it, say: ", 1)[1].rstrip(".")
    assert command == f"set all the study lights to {effect} effect"
    state = {"entity_id": "light.example_zebra", "state": "on",
             "attributes": {"supported_features": 4, "effect_list": [effect, "Other"]}}
    index = light_effects.EffectIndex({state["entity_id"]: state})
    request = light_effects.recognise(command, index, [state["entity_id"]], claimed=shc.effect_word_claimed,
                                      names_target=fx.names_target, names_light=fx.names_light)
    assert request is not None and request.phrase == light_effects.normalize(effect) and not request.clear and request.explicit


def test_the_claimed_names_are_refused_without_the_word_effect():
    """The negative control for the above: the old wording is dropped by the recogniser."""
    for effect in ("sunset", "romantic", "movie", "night", "morning"):
        state = {"entity_id": "light.example_zebra", "state": "on",
                 "attributes": {"supported_features": 4, "effect_list": [effect, "Other"]}}
        index = light_effects.EffectIndex({state["entity_id"]: state})
        assert light_effects.recognise(f"set all the study lights to {effect}", index, [state["entity_id"]],
                                       claimed=shc.effect_word_claimed, names_target=fx.names_target,
                                       names_light=fx.names_light) is None, effect


def test_the_full_rewording_text_for_an_effect_and_for_a_clear():
    effect = write_fanout.FanoutBlock(
        writes=(write_fanout.PlannedWrite("light", "set_effect", tuple(f"light.l{i}" for i in range(8)), "candlelight"),),
        unbounded=False, room="study")
    assert write_fanout.rewording(effect) == (
        "That would set the candlelight effect on 8 lights in the study. "
        "To do it, say: set all the study lights to candlelight effect.")
    clear = write_fanout.FanoutBlock(
        writes=(write_fanout.PlannedWrite("light", "clear_effect", ("light.a", "light.b")),), unbounded=False, room="study")
    assert write_fanout.rewording(clear) == (
        "That would turn off the effect on 2 lights in the study. "
        "To do it, say: turn off the effect on all the study lights.")
    house = write_fanout.FanoutBlock(
        writes=(write_fanout.PlannedWrite("light", "set_effect", ("all",), "candlelight"),), unbounded=True)
    assert write_fanout.rewording(house) == (
        "That would affect all the lights. To do it, say: set all the lights to candlelight effect.")


def test_the_single_light_rewording_and_the_clear_rewording():
    one = write_fanout.FanoutBlock(writes=(write_fanout.PlannedWrite("light", "set_effect", ("light.a",), "twinkle"),),
                                   unbounded=False, room="study")
    assert write_fanout.rewording(one) == (
        "That would set the twinkle effect on 1 light in the study. To do it, say: set the study light to twinkle effect.")
    clear = write_fanout.FanoutBlock(writes=(write_fanout.PlannedWrite("light", "clear_effect", ("light.a", "light.b")),),
                                     unbounded=False, room="study")
    command = write_fanout.rewording(clear).split("To do it, say: ", 1)[1].rstrip(".")
    assert command == "turn off the effect on all the study lights"
    state = {"entity_id": "light.example_one", "state": "on", "attributes": {"supported_features": 4, "effect_list": ["Fire"]}}
    request = light_effects.recognise(command, light_effects.EffectIndex({state["entity_id"]: state}), [state["entity_id"]],
                                      claimed=shc.effect_word_claimed, names_target=fx.names_target, names_light=fx.names_light)
    assert request is not None and request.clear and request.target_text == "all the study lights"


def test_light_effects_is_a_first_import_in_the_cycle_check():
    from . import test_orchestrator_entrypoint_import as entry
    assert "orchestrator.light_effects" in entry.FIRST_IMPORTS
