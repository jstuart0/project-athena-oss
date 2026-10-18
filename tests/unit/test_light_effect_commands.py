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
from orchestrator import access_target, light_effects, room_context, write_fanout  # noqa: E402
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
def test_a_typed_effect_answer_is_a_refusal_whatever_the_words_in_it(name):
    """D17: the writer returns the typed outcome, so a name with "!", "?" or ". " in it is still a refusal,
    and a plain string that only reads the same is not."""
    for awkward in ("the Study Prism", "the Wow! lamp", "the Is it? lamp", "St. John's lamp. Next", "x" * 400):
        fields = {k: awkward for k in SAMPLES[name]}
        text = getattr(shc, name).format(**fields)
        assert shc.is_refusal_text(room_context.RefusedText(text)), text
        assert not shc.is_refusal_text(str(text)), "only the typed outcome is a refusal"


def test_the_effect_answer_samples_cover_every_template():
    templates = {n for n in dir(shc) if n.startswith("EFFECT_") and isinstance(getattr(shc, n), str) and "{" in getattr(shc, n)}
    assert templates == set(SAMPLES)
    assert not shc.is_refusal_text("Turned off the effect on the study lights.")
    assert room_context.RefusedText("a") == "a" and isinstance(room_context.RefusedText("a"), str)


def test_light_reply_with_no_effect_never_says_to_the_effect():
    text = shc.light_reply("set_effect", rooms=["study"], effect=None)
    assert "to the effect" not in text and text == "Set the effect on the study lights."
    assert shc.light_reply("set_effect", single_name="Lamp", effect="") == "Set the effect on the Lamp."


def test_the_ambient_colour_fast_path_hands_out_copies(monkeypatch):
    rig = Rig(monkeypatch)
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


# ---------------------------------------------------------------------------
# Phase 2c: the effect handler, through the real controller and guard
# ---------------------------------------------------------------------------

import copy  # noqa: E402
import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from . import test_room_light_area_tier as tat  # noqa: E402
from orchestrator.room_choice import storable_intent  # noqa: E402
from orchestrator.sequence_executor import EFFECT_STEP_REFUSAL, SequenceExecutor  # noqa: E402

GOVEE_LIST = list(fx.GOVEE["attributes"]["effect_list"])
WLED_LIST = list(fx.WLED["attributes"]["effect_list"])
SET = {"device_type": "light", "room": None, "action": "set_effect", "target_scope": "all_individual",
       "parameters": {}, "color_description": None}


def give(states, entity_id, name, effects, *, state="on", effect=None, bit=4):
    """Make one of the house's lights an effect-capable one (fictional name)."""
    entry = states[entity_id]
    entry["state"] = state
    entry["attributes"].update(friendly_name=name, supported_features=bit, effect=effect)
    if effects is None:
        entry["attributes"].pop("effect_list", None)
    else:
        entry["attributes"]["effect_list"] = list(effects)
    return entry


def effect_states(**overrides):
    """The area-tier house with: Study Prism (WLED) in the study, Hexagon (Govee) in the hall,
    two WLED galley bulbs, and a galley relay with no effects."""
    _, states = tat.house()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST)
    give(states, "light.hall_lamp", "Hexagon", GOVEE_LIST)
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST)
    give(states, "light.galley_bulb_2", "Galley Wash", WLED_LIST)
    for eid, spec in overrides.items():
        give(states, eid.replace("__", "."), **spec)
    return states


def effect_rig(monkeypatch, states=None, **kw):
    return Rig(monkeypatch, states=states or effect_states(), **kw)


def intent(**parameters):
    out = copy.deepcopy(SET)
    out["parameters"] = parameters
    return out


def say(rig, query, parameters=None, *, action="set_effect", device_room=None, perms=None, cue_query=None,
        carry=False, confirmed=None, original_query="__same__", room=None):
    """One turn through execute_intent under the guard; the answer."""
    body = intent(**(parameters if parameters is not None else {}))
    body["action"] = action
    body["room"] = room
    if parameters is None:
        body["parameters"] = {}
    original = query if original_query == "__same__" else original_query

    async def go():
        scope_perms = perms or {"mode": "owner"}
        with mp.ha_permission_scope(scope_perms, mode="owner", utterance=classify_utterance(query), trusted_voice=True) as scope:
            scope.can_carry_pending = carry
            if confirmed is not None:
                with write_fanout.confirmed(confirmed):
                    return await rig.controller.execute_intent(body, rig.raw, original, device_room, cue_query=cue_query)
            return await rig.controller.execute_intent(body, rig.raw, original, device_room, cue_query=cue_query)

    answer = asyncio.run(go())
    rig.last_intent = body
    return answer


def writes(rig):
    """Every call_service made, exactly as made."""
    return [tuple(c.args) for c in rig.raw.call_service.await_args_list]


def turn_on(entity_id, effect):
    return ("light", "turn_on", {"entity_id": entity_id, "effect": effect})


def test_a_wled_light_gets_the_effect_in_its_own_spelling(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the study prism to fire 2012", {"effect": "fire 2012"})
    assert writes(rig) == [turn_on("light.study_lamp", "Fire 2012")]
    assert answer == "Set the Study Prism to fire 2012."


def test_a_govee_light_gets_the_exact_call_shape(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the hexagon to candlelight", {"effect": "candlelight"})
    assert writes(rig) == [turn_on("light.hall_lamp", "candlelight")]
    assert list(rig.raw.call_service.await_args_list[0].args[2]) == ["entity_id", "effect"]
    assert answer == "Set the Hexagon to candlelight."


def test_partial_support_counts_the_lights_that_lack_it(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert sorted(writes(rig), key=lambda w: w[2]['entity_id']) == [turn_on("light.galley_bulb_1", "Twinkle"), turn_on("light.galley_bulb_2", "Twinkle")]
    assert answer == "Set the galley lights to twinkle. 1 light doesn't have that effect."


def test_one_capable_light_among_several_says_other(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_2", "Galley Wash", None)
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert writes(rig) == [turn_on("light.galley_bulb_1", "Twinkle")]
    assert answer == "Set the Galley Spot to twinkle. 2 other lights don't have that effect."


@pytest.mark.parametrize("shape", ["no_effects", "effect_list_without_bit", "null_effect_list", "unavailable"])
def test_a_room_whose_lights_take_no_effects_is_told_so_and_nothing_is_written(monkeypatch, shape):
    states = effect_states()
    fixture = fx.load(shape)["attributes"]
    give(states, "light.galley_bulb_1", "Galley Spot", fixture.get("effect_list"), bit=fixture["supported_features"])
    give(states, "light.galley_bulb_2", "Galley Wash", fixture.get("effect_list"), bit=fixture["supported_features"])
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert writes(rig) == []
    assert answer == "I can't set an effect on the galley lights."
    assert shc.is_refusal_text(answer)


def test_an_effect_only_another_room_has_is_not_found_with_suggestions_from_this_rooms_lights(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert writes(rig) == []
    assert answer.startswith("I couldn't find that effect on the Study Prism. Try ")
    offered = answer.split("Try ", 1)[1].rstrip(".")
    assert all(light_effects.match(o, WLED_LIST) for o in offered.replace(" or ", ", ").split(", "))
    assert not any(g in offered for g in ("sunrise", "dating", "energetic")), "no suggestion from another room's light"
    assert shc.is_refusal_text(answer)


def test_fade_asked_of_a_room_without_it_is_an_honest_not_found_with_suggestions(monkeypatch):
    """F85: a permitted light in another room has Fade and no study light does."""
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", GOVEE_LIST)
    give(states, "light.hall_lamp", "Hexagon", WLED_LIST)
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "put the study on fade", {"effect": "fade"})
    assert writes(rig) == []
    assert answer.startswith("I couldn't find that effect on the Study Prism. Try ") and "fade" not in answer.split("Try ")[1]


def test_the_named_light_down_while_others_are_up_writes_nothing(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, state="unavailable")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "set the galley spot to twinkle", {"effect": "twinkle"})
    assert writes(rig) == []
    assert answer == "The Galley Spot isn't responding right now."


def test_every_light_down_says_so(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, state="unavailable")
    rig = effect_rig(monkeypatch, states)
    assert say(rig, "set the study prism to twinkle", {"effect": "twinkle"}) == "The study lights aren't responding right now."
    assert writes(rig) == []


# -- clearing ---------------------------------------------------------------

def test_a_clear_writes_the_lists_own_off_value_to_a_light_running_an_effect(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Fire 2012")
    give(states, "light.hall_lamp", "Hexagon", GOVEE_LIST, effect="candlelight")
    rig = effect_rig(monkeypatch, states)
    assert say(rig, "turn off the effect in the study", {}, action="clear_effect") == "Turned off the effect on the Study Prism."
    assert writes(rig) == [turn_on("light.study_lamp", "Solid")]
    rig.raw.call_service.reset_mock()
    assert say(rig, "turn off the effect in the hall", {}, action="clear_effect") == "Turned off the effect on the Hexagon."
    assert writes(rig) == [turn_on("light.hall_lamp", "none")]


def test_an_off_light_is_never_written_by_a_clear(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, state="off", effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == []
    assert answer == "There's no effect running on the Study Prism." and shc.is_refusal_text(answer)


def test_a_light_on_with_no_effect_is_not_written(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == [] and answer == "There's no effect running on the Study Prism."


def test_a_running_effect_with_no_off_value_cannot_be_cleared(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", ["Fire", "Wave"], effect="Fire")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == [] and answer == "I can't turn off the effect on the Study Prism." and shc.is_refusal_text(answer)


def test_a_named_clear_matches_only_the_light_running_that_effect(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, effect="Twinkle")
    give(states, "light.galley_bulb_2", "Galley Wash", WLED_LIST, effect="Sparkle")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "turn off the twinkle effect in the galley", {"effect": "twinkle"}, action="clear_effect")
    assert writes(rig) == [turn_on("light.galley_bulb_1", "Solid")]
    assert answer == "Turned off the effect on the Galley Spot. 2 other lights had no effect running."


def test_a_named_clear_for_an_effect_not_running_clears_nothing(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "turn off the sparkle effect in the study", {"effect": "sparkle"}, action="clear_effect")
    assert writes(rig) == [] and answer == "There's no effect running on the Study Prism."


def test_a_mixed_group_clear_skips_ineligible_leaves_and_writes_the_eligible_ones(monkeypatch):
    """F81."""
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, effect="Fire 2012")
    give(states, "light.galley_bulb_2", "Galley Wash", WLED_LIST, state="off", effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "turn off the effect in the galley", {}, action="clear_effect")
    assert writes(rig) == [turn_on("light.galley_bulb_1", "Solid")]
    assert answer == "Turned off the effect on the Galley Spot. 2 other lights had no effect running."


def test_a_running_effect_that_is_not_in_the_sanitised_list_is_still_cleared(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Sparkle+")
    rig = effect_rig(monkeypatch, states)
    say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == [turn_on("light.study_lamp", "Solid")]


# -- the stale cache --------------------------------------------------------

def _stale_study_states():
    """The cached study light lacks Candlelight; a refresh gives it a Govee list."""
    return effect_states()


def test_a_miss_refreshes_once_and_then_hits(monkeypatch):
    rig = effect_rig(monkeypatch)
    fresh = effect_states()
    give(fresh, "light.study_lamp", "Study Prism", GOVEE_LIST)
    rig.refresh_to = fresh
    answer = say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert rig.refreshes == 1
    assert writes(rig) == [turn_on("light.study_lamp", "candlelight")] and answer == "Set the Study Prism to candlelight."


def test_a_miss_after_the_refresh_is_not_found_and_reads_once(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert rig.refreshes == 1 and writes(rig) == [] and answer.startswith("I couldn't find that effect")


def test_a_second_miss_inside_a_minute_does_not_read_again_and_one_after_does(monkeypatch):
    rig = effect_rig(monkeypatch)
    say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert rig.refreshes == 1
    rig.clock.now += 61
    say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert rig.refreshes == 2


def test_a_refresh_error_counts_as_no_match(monkeypatch):
    rig = effect_rig(monkeypatch)
    rig.refresh_error = RuntimeError("ha down")
    answer = say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert writes(rig) == [] and answer.startswith("I couldn't find that effect")


def test_no_refresh_when_no_light_advertises_effects(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", None, bit=0)
    rig = effect_rig(monkeypatch, states)
    assert say(rig, "set the study prism to candlelight", {"effect": "candlelight"}) == "I can't set an effect on the Study Prism."
    assert rig.refreshes == 0


def test_ten_concurrent_misses_refresh_once(monkeypatch):
    rig = effect_rig(monkeypatch)

    real_refresh = rig.manager.refresh_entities

    async def slow_refresh():
        await asyncio.sleep(0)  # lets the other misses run while this one reads
        return await real_refresh()

    rig.manager.refresh_entities = slow_refresh

    async def one():
        body = intent(effect="candlelight")
        query = "set the study prism to candlelight"
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query), trusted_voice=True):
            return await rig.controller.execute_intent(body, rig.raw, query)

    async def burst():
        return await asyncio.gather(*[one() for _ in range(10)])

    answers = asyncio.run(burst())
    assert rig.refreshes == 1 and writes(rig) == []
    assert all(a.startswith("I couldn't find that effect") for a in answers)


# -- failures, the gate, the scope ------------------------------------------

def test_one_write_failing_is_counted_and_the_others_are_reported(monkeypatch):
    rig = effect_rig(monkeypatch)

    async def flaky(domain, service, data, *a, **kw):
        if data["entity_id"] == "light.galley_bulb_2":
            raise RuntimeError("no answer")
        return {"ok": True}

    rig.raw.call_service = AsyncMock(side_effect=flaky)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert answer == "Set the galley lights to twinkle. 1 light doesn't have that effect. 1 didn't respond."


def test_every_write_failing_says_so_without_a_success_text(monkeypatch):
    rig = effect_rig(monkeypatch)
    rig.raw.call_service = AsyncMock(side_effect=RuntimeError("no answer"))
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert answer == "I couldn't change the effect right now." and shc.is_refusal_text(answer)


def test_a_denied_light_surfaces_the_guards_refusal_and_writes_nothing(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the study prism to fire 2012", {"effect": "fire 2012"}, perms=GUEST_DENIED[0])
    assert writes(rig) == []
    assert answer


def test_over_the_limit_the_gate_rewords_with_the_effect_and_writes_nothing(monkeypatch):
    rig = effect_rig(monkeypatch, hard_limit=1)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert writes(rig) == []
    assert answer == ("That would set the twinkle effect on 2 lights in the galley. "
                      "To do it, say: set all the galley lights to twinkle effect.")


def test_over_the_limit_with_a_pending_carrier_the_prompt_names_the_effect(monkeypatch):
    rig = effect_rig(monkeypatch, hard_limit=1)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"}, carry=True)
    assert writes(rig) == [] and answer == "That would set the twinkle effect on 2 lights in the galley. Should I go ahead?"


def test_the_gate_is_asked_about_exactly_the_lights_that_would_be_written(monkeypatch):
    rig = effect_rig(monkeypatch)
    say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert rig.gate_calls == [("light.galley_bulb_1", "light.galley_bulb_2")]


def test_a_confirmed_replay_writes_once_and_the_stored_intent_keeps_the_effect(monkeypatch):
    """F81/F108: a pending saved for an effect replays, and one saved before detail existed still builds."""
    rig = effect_rig(monkeypatch, hard_limit=1)
    query = "make the galley twinkle"
    prompt = say(rig, query, {"effect": "twinkle"}, carry=True)
    assert prompt.endswith("Should I go ahead?") and writes(rig) == []
    stored = json.loads(json.dumps(storable_intent(rig.last_intent)))
    assert stored["parameters"]["effect"] == "twinkle" and stored["decided_room"] == "galley"
    before = [["light", "set_effect", ["light.galley_bulb_1", "light.galley_bulb_2"]]]
    assert write_fanout.PlannedWrite(*before[0]).detail == ""
    ids = [e for w in before for e in w[2]]

    async def replay():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("yes"), trusted_voice=True):
            with write_fanout.confirmed(ids):
                return await rig.controller.execute_intent(stored, rig.raw, original_query=query)

    answer = asyncio.run(replay())
    assert sorted(writes(rig), key=lambda w: w[2]['entity_id']) == [turn_on("light.galley_bulb_1", "Twinkle"), turn_on("light.galley_bulb_2", "Twinkle")]
    assert answer.startswith("Set the galley lights to twinkle")


def test_several_rooms_a_room_group_and_the_whole_house_are_unsupported(monkeypatch):
    rig = effect_rig(monkeypatch)
    for query in ("make the galley and the study twinkle", "set all the lights to twinkle"):
        assert say(rig, query, {"effect": "twinkle"}) == shc.SEVERAL_ROOMS_UNSUPPORTED
    grouped = effect_rig(monkeypatch, resolve_group={"name": "upper", "members": [{"room_name": "study"}]})
    assert say(grouped, "make the study twinkle", {"effect": "twinkle"}) == shc.SEVERAL_ROOMS_UNSUPPORTED
    assert writes(rig) == [] and writes(grouped) == []


# -- narrowing ---------------------------------------------------------------

def test_the_effect_span_is_not_a_light_name(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Twinkle Lamp", WLED_LIST)
    rig = effect_rig(monkeypatch, states)
    say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert sorted(w[2]["entity_id"] for w in writes(rig)) == ["light.galley_bulb_1", "light.galley_bulb_2"]


def test_the_most_specific_name_wins(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST)
    give(states, "light.galley_bulb_2", "Galley Spot Big", WLED_LIST)
    rig = effect_rig(monkeypatch, states)
    say(rig, "set the galley spot big to twinkle", {"effect": "twinkle"})
    assert writes(rig) == [turn_on("light.galley_bulb_2", "Twinkle")]
    rig.raw.call_service.reset_mock()
    say(rig, "set the galley spot to twinkle", {"effect": "twinkle"})
    assert writes(rig) == [turn_on("light.galley_bulb_1", "Twinkle")]


def test_a_uniquely_named_light_in_another_room_never_writes_the_speakers_room(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the hexagon to candlelight", {"effect": "candlelight"}, device_room="study")
    assert writes(rig) == [turn_on("light.hall_lamp", "candlelight")]
    assert answer == "Set the Hexagon to candlelight."
    assert rig.last_intent["decided_room"] == "hall" and rig.last_intent["room"] == "hall"


def test_the_same_with_the_word_light_after_the_name(monkeypatch):
    """F82."""
    rig = effect_rig(monkeypatch)
    say(rig, "set the hexagon light to candlelight", {"effect": "candlelight"}, device_room="study")
    assert writes(rig) == [turn_on("light.hall_lamp", "candlelight")]


def test_a_name_that_is_not_in_the_named_room_is_a_refusal_not_the_whole_room(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the hexagon in the study to candlelight", {"effect": "candlelight"})
    assert writes(rig) == []
    assert answer == "I couldn't find the hexagon in the study." and shc.is_refusal_text(answer)


def _twin_states():
    states = effect_states()
    give(states, "light.north_lamp", "Hexagon", GOVEE_LIST)
    return states


def test_two_areas_with_the_speaker_in_one_choose_the_speakers(monkeypatch):
    rig = effect_rig(monkeypatch, _twin_states())
    say(rig, "set the hexagon to candlelight", {"effect": "candlelight"}, device_room="hall")
    assert writes(rig) == [turn_on("light.hall_lamp", "candlelight")]


def test_two_areas_otherwise_ask_and_the_reply_replays_to_exactly_the_named_leaf(monkeypatch):
    rig = effect_rig(monkeypatch, _twin_states())
    question = say(rig, "set the hexagon to candlelight", {"effect": "candlelight"})
    assert writes(rig) == [] and "hall" in question.lower() and "north bathroom" in question.lower()
    replay = copy.deepcopy(rig.last_intent)
    shc.SmartHomeController._set_decided(replay, "north_bathroom", "north bathroom")

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("north"), trusted_voice=True):
            return await rig.controller.execute_intent(replay, rig.raw, "set the hexagon to candlelight")

    assert asyncio.run(go()) == "Set the Hexagon to candlelight."
    assert writes(rig) == [turn_on("light.north_lamp", "candlelight")]


# -- the 2c review: room, gate words, fresh clears, typed refusals ------------------------

def test_an_effect_named_like_a_room_does_not_decide_the_room(monkeypatch):
    """F137."""
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST + ["Galley Sparkle"])
    rig = effect_rig(monkeypatch, states)
    say(rig, "galley sparkle effect on the study prism", {"effect": "galley sparkle"})
    assert writes(rig) == [turn_on("light.study_lamp", "Galley Sparkle")]


def test_a_room_the_model_guessed_decides_nothing(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Fire 2012")
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, effect="Fire 2012")
    give(states, "light.galley_bulb_2", "Galley Wash", WLED_LIST, effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    say(rig, "turn off the effect", {}, action="clear_effect", room="galley", device_room="study")
    assert writes(rig) == [turn_on("light.study_lamp", "Solid")], "the speaker's room, not the model's galley"


def test_an_effect_named_like_the_house_does_not_skip_the_confirmation(monkeypatch):
    """F138: the gate reads the words without the effect, so "house party" is no scope cue."""
    states = effect_states()
    for eid in ("light.galley_bulb_1", "light.galley_bulb_2"):
        give(states, eid, eid[-6:], WLED_LIST + ["House Party"])
    rig = effect_rig(monkeypatch, states, hard_limit=1)
    answer = say(rig, "make the galley house party effect", {"effect": "house party"})
    assert writes(rig) == []
    assert answer == ("That would set the house party effect on 2 lights in the galley. "
                      "To do it, say: set all the galley lights to house party effect.")


def test_the_gate_and_the_writer_read_the_same_words(monkeypatch):
    """F140 L1."""
    rig = effect_rig(monkeypatch, hard_limit=1)
    seen = []
    real_gate = write_fanout.gate

    def spy(domain, service=None, ids=None, query=None, **kw):
        seen.append(query)
        return real_gate(domain, service, ids, query, **kw)

    monkeypatch.setattr(write_fanout, "gate", spy)
    say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert seen == ["make the galley"], "the effect words are not the gate's words"


def _stale_cache_rig(monkeypatch, live_state):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    rig.manager._ha.get_state = AsyncMock(return_value=live_state)
    return rig


def test_a_light_turned_off_since_the_cache_was_filled_is_not_turned_back_on(monkeypatch):
    """F139: cached on, live off."""
    live = copy.deepcopy(effect_states()["light.study_lamp"])
    live["state"] = "off"
    live["attributes"]["effect"] = "Fire 2012"
    rig = _stale_cache_rig(monkeypatch, live)
    answer = say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == [] and answer == "There's no effect running on the Study Prism."


def test_a_light_still_running_the_effect_is_cleared_after_the_fresh_read(monkeypatch):
    live = copy.deepcopy(effect_states()["light.study_lamp"])
    live["attributes"]["effect"] = "Fire 2012"
    rig = _stale_cache_rig(monkeypatch, live)
    say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == [turn_on("light.study_lamp", "Solid")]
    assert rig.manager._ha.get_state.await_count == 1


def test_a_light_that_cannot_be_read_fresh_is_not_cleared(monkeypatch):
    rig = _stale_cache_rig(monkeypatch, None)
    rig.manager._ha.get_state = AsyncMock(side_effect=RuntimeError("ha down"))
    say(rig, "turn off the effect in the study", {}, action="clear_effect")
    assert writes(rig) == []


def test_a_light_that_stands_for_unlisted_members_is_never_written(monkeypatch):
    """F140 L4: the target resolution already keeps a group out of the leaves, so it never reaches the writer."""
    states = effect_states()
    states["light.galley_bulb_1"]["attributes"]["entity_id"] = ["light.not_in_this_house"]
    rig = effect_rig(monkeypatch, states)
    say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert all(w[2]["entity_id"] != "light.galley_bulb_1" for w in writes(rig))


def test_a_refusal_from_the_room_decision_is_typed(monkeypatch):
    rig = effect_rig(monkeypatch)
    rig.controller._decide_intent_room = AsyncMock(return_value="I couldn't find a room called the cellar.")
    answer = say(rig, "make the study twinkle", {"effect": "twinkle"})
    assert answer == "I couldn't find a room called the cellar." and shc.is_refusal_text(answer) and writes(rig) == []


def test_two_lists_give_each_light_its_own_spelling_with_one_match_each(monkeypatch):
    """F143."""
    states = effect_states()
    give(states, "light.galley_bulb_2", "Galley Wash", GOVEE_LIST)
    calls = {"n": 0}
    real_match = light_effects.match

    def counting(*a, **kw):
        calls["n"] += 1
        return real_match(*a, **kw)

    monkeypatch.setattr(light_effects, "match", counting)
    rig = effect_rig(monkeypatch, states)
    say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert sorted(writes(rig), key=lambda w: w[2]["entity_id"]) == [
        turn_on("light.galley_bulb_1", "Twinkle"), turn_on("light.galley_bulb_2", "twinkle")]
    assert calls["n"] == 2


def test_a_phrase_exact_in_one_light_and_loosely_matched_to_another_effect_in_a_sibling_is_refused(monkeypatch):
    """F140 L2: "sparkle" is Sparkle in one list and would be Sparkles in the other."""
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", ["Solid", "Sparkle"])
    give(states, "light.galley_bulb_2", "Galley Wash", ["Solid", "Sparkles"])
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "make the galley sparkle", {"effect": "sparkle"})
    assert writes(rig) == [] and shc.is_refusal_text(answer) and "sparkle" in answer and "sparkles" in answer


def test_the_executors_fuzzy_path_is_reached_by_an_effect_phrase_with_a_named_light(monkeypatch):
    """F146: pinned. "<phrase> effect on <named light>" is recognised without the vocabulary and matched loosely."""
    rig = effect_rig(monkeypatch)
    say(rig, "play the candlelite effect on the hexagon", {"effect": "candlelite"})
    assert writes(rig) == [turn_on("light.hall_lamp", "candlelight")]
    rig.raw.call_service.reset_mock()
    say(rig, "set the hexagon to candlelite", {"effect": "candlelite"})
    assert writes(rig) == [], "T1 needs an exact name; only the effect form is loose"


def test_the_needs_words_answer_is_reachable_only_from_the_writer_itself(monkeypatch):
    rig = effect_rig(monkeypatch)

    async def direct():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
            return await rig.controller._handle_effect_intent(intent(effect="twinkle"), rig.raw, None, None)

    assert asyncio.run(direct()) == shc.EFFECT_NEEDS_WORDS and shc.is_refusal_text(asyncio.run(direct()))
    body = intent(effect="twinkle")
    assert asyncio.run(_execute_without_words(rig, body)) == shc.NO_WRITE_CUE_TEXT


async def _execute_without_words(rig, body):
    with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
        return await rig.controller.execute_intent(body, rig.raw)


def test_every_answer_that_changed_nothing_is_a_typed_refusal(monkeypatch):
    """F142, the consumer side: whatever the scenario, route_control sees a refusal."""
    scenarios = {}

    def run(name, query, parameters=None, *, action="set_effect", prep=None, **kw):
        states = effect_states()
        if prep:
            prep(states)
        rig = effect_rig(monkeypatch, states, **kw.pop("rig", {}))
        scenarios[name] = (say(rig, query, parameters, action=action, **kw), writes(rig))

    run("not found", "set the study prism to candlelight", {"effect": "candlelight"})
    run("none running", "turn off the effect in the study", {}, action="clear_effect")
    run("cannot clear", "turn off the effect in the study", {}, action="clear_effect",
        prep=lambda st: give(st, "light.study_lamp", "Study Prism", ["Fire", "Wave"], effect="Fire"))
    run("unsupported", "make the galley twinkle", {"effect": "twinkle"},
        prep=lambda st: [give(st, e, "Plain", None, bit=0) for e in ("light.galley_bulb_1", "light.galley_bulb_2")])
    run("name not in room", "set the hexagon in the study to candlelight", {"effect": "candlelight"})
    run("room down", "set the study prism to twinkle", {"effect": "twinkle"},
        prep=lambda st: give(st, "light.study_lamp", "Study Prism", WLED_LIST, state="unavailable"))
    run("named down", "set the galley spot to twinkle", {"effect": "twinkle"},
        prep=lambda st: give(st, "light.galley_bulb_1", "Galley Spot", WLED_LIST, state="unavailable"))
    run("several rooms", "make the galley and the study twinkle", {"effect": "twinkle"})
    run("unknown room", "set the study prism in the cellar to twinkle", {"effect": "twinkle"})
    assert set(scenarios) == {"not found", "none running", "cannot clear", "unsupported", "name not in room", "room down",
                              "named down", "several rooms", "unknown room"}
    for name, (answer, made) in scenarios.items():
        assert made == [], name
        assert shc.is_refusal_text(answer), (name, answer)
    failing = effect_rig(monkeypatch)
    failing.raw.call_service = AsyncMock(side_effect=RuntimeError("x"))
    assert shc.is_refusal_text(say(failing, "make the galley twinkle", {"effect": "twinkle"}))


def test_partial_down_writes_the_light_that_answers_and_counts_the_one_that_does_not(monkeypatch):
    """F145."""
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, state="unavailable")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert writes(rig) == [turn_on("light.galley_bulb_2", "Twinkle")]
    assert answer == "Set the Galley Wash to twinkle. 1 other light doesn't have that effect. 1 isn't responding."


def test_a_named_light_that_is_up_while_a_sibling_is_down_reports_no_count(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, state="unavailable")
    rig = effect_rig(monkeypatch, states)
    assert say(rig, "set the galley wash to twinkle", {"effect": "twinkle"}) == "Set the Galley Wash to twinkle."


def test_a_clear_on_an_unavailable_light_that_still_reports_an_effect_writes_nothing(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, state="unavailable", effect="Fire 2012")
    give(states, "light.galley_bulb_2", "Galley Wash", WLED_LIST, effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    say(rig, "turn off the effect in the galley", {}, action="clear_effect")
    assert writes(rig) == [turn_on("light.galley_bulb_2", "Solid")]


def test_a_guest_naming_a_restricted_light_in_another_room_gets_the_same_reply_as_for_none(monkeypatch):
    perms = mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\.hall_lamp$"]})
    rig = effect_rig(monkeypatch)
    restricted = say(rig, "set the hexagon to candlelight", {"effect": "candlelight"}, perms=perms, device_room="study")
    nonexistent = say(rig, "set the zebra to candlelight", {"effect": "candlelight"}, perms=perms, device_room="study")
    assert restricted == nonexistent and writes(rig) == [] and rig.refreshes == 0


def test_a_mixed_permitted_and_denied_room_writes_the_permitted_light_and_records_the_denial(monkeypatch):
    """F146: the denial is recorded and the answer says part was refused."""
    perms = mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\.galley_bulb_1$"]})
    rig = effect_rig(monkeypatch)
    captured = {}

    async def go():
        with mp.ha_permission_scope(perms, mode="owner", utterance=classify_utterance("make the galley twinkle"), trusted_voice=True) as scope:
            answer = await rig.controller.execute_intent(intent(effect="twinkle") | {"action": "set_effect"}, rig.raw, "make the galley twinkle")
            captured["denials"] = len(scope.denials)
            return answer

    answer = asyncio.run(go())
    assert [w[2]["entity_id"] for w in writes(rig)] == ["light.galley_bulb_2"]
    assert captured["denials"] >= 1 and answer


def test_a_running_effect_reported_in_compact_form_is_cleared_by_name(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Fire2012")
    rig = effect_rig(monkeypatch, states)
    say(rig, "turn off the fire 2012 effect in the study", {"effect": "fire 2012"}, action="clear_effect")
    assert writes(rig) == [turn_on("light.study_lamp", "Solid")]


def test_a_mixed_clearable_and_unclearable_group_clears_what_it_can(monkeypatch):
    states = effect_states()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST, effect="Fire 2012")
    give(states, "light.galley_bulb_2", "Galley Wash", ["Fire 2012", "Wave"], effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    answer = say(rig, "turn off the effect in the galley", {}, action="clear_effect")
    assert writes(rig) == [turn_on("light.galley_bulb_1", "Solid")]
    assert answer.startswith("Turned off the effect on the Galley Spot.")


# -- the words, not the model -----------------------------------------------

def test_a_model_effect_the_caller_never_said_makes_no_service_call(monkeypatch):
    """tessa: end to end, the model supplies an effect and the words do not."""
    rig = effect_rig(monkeypatch)
    for query in ("set the study prism to blue", "make the galley brighter", "turn on the study lights"):
        answer = say(rig, query, {"effect": "twinkle"})
        assert answer == shc.NO_WRITE_CUE_TEXT, query
    assert writes(rig) == [] and rig.refreshes == 0


def test_model_parameters_effect_is_ignored_in_favour_of_the_words(monkeypatch):
    rig = effect_rig(monkeypatch)
    say(rig, "set the study prism to fire 2012", {"effect": "fire 2012", "clear": True})
    assert writes(rig) == [turn_on("light.study_lamp", "Fire 2012")]


def test_the_words_decide_even_when_the_model_names_another_effect(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = say(rig, "set the study prism to fire 2012", {"effect": "sparkle"})
    assert writes(rig) == [] and answer == shc.NO_WRITE_CUE_TEXT


def test_a_sequence_room_step_with_set_effect_writes_nothing(monkeypatch):
    rig = effect_rig(monkeypatch)
    body = intent(effect="twinkle")
    body["room"] = "galley"

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
            return await rig.controller.execute_intent(body, rig.raw)

    assert asyncio.run(go()) == shc.NO_WRITE_CUE_TEXT
    assert writes(rig) == []


def test_a_sequence_direct_step_with_set_effect_writes_nothing_and_is_not_refused_fanout(monkeypatch):
    rig = effect_rig(monkeypatch)
    executor = SequenceExecutor(rig.controller, rig.raw)
    step = {"action": "set_effect", "target": {"entity_id": "light.study_lamp", "decided_entity": "light.study_lamp",
                                                "device_type": "light"}, "parameters": {"effect": "Fire 2012"}}

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
            result = await executor._execute_step(step, 1, 1)
            return result, write_fanout.take_block()

    result, block = asyncio.run(go())
    assert result == EFFECT_STEP_REFUSAL and block is None and writes(rig) == []


def test_handler_reads_the_cue_words_for_a_carried_follow_up(monkeypatch):
    rig = effect_rig(monkeypatch)
    say(rig, "set the study prism to fire 2012", {"effect": "fire 2012"}, original_query="and the same again",
        cue_query="set the study prism to fire 2012", device_room="study")
    assert writes(rig) == [turn_on("light.study_lamp", "Fire 2012")]


def test_a_guest_asking_for_an_effect_only_a_restricted_light_has_gets_the_same_reply_as_for_none(monkeypatch):
    """The reply and the writes come only from lights the caller may use."""
    perms = mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\.hall_lamp$"]})
    rig = effect_rig(monkeypatch)
    only_restricted = say(rig, "set the study prism to candlelight", {"effect": "candlelight"}, perms=perms)
    nonexistent = say(rig, "set the study prism to zebra drift", {"effect": "zebra drift"}, perms=perms)
    assert only_restricted == nonexistent == shc.NO_WRITE_CUE_TEXT
    assert writes(rig) == [] and rig.refreshes == 0


def test_the_vocabulary_is_the_permitted_lights_only(monkeypatch):
    rig = effect_rig(monkeypatch)
    owner = say(rig, "set the study prism to candlelight", {"effect": "candlelight"})
    assert owner.startswith("I couldn't find that effect"), "the owner's vocabulary holds candlelight"
    perms = mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\.hall_lamp$"]})
    assert say(rig, "set the study prism to candlelight", {"effect": "candlelight"}, perms=perms) == shc.NO_WRITE_CUE_TEXT


def test_a_state_question_about_an_effect_never_reaches_a_write():
    """route_control answers a STATE_QUESTION with a get_status read under a read-only scope."""
    from orchestrator.utterance_kind import UtteranceKind
    for query in ("is the study prism on candlelight", "which effect is the lamp on", "is there an effect on the lamp"):
        assert classify_utterance(query).kind == UtteranceKind.STATE_QUESTION, query


@pytest.mark.parametrize("query", [
    "what's the rain effect on the study prism", "what is twinkle effect on the study prism", "does the study prism have a fire 2012 effect",
    "is the study prism on fire 2012", "how do I set the study prism to twinkle",
])
def test_a_question_that_reaches_the_handler_writes_nothing(monkeypatch, query):
    """Classified a question or not, the words of a question are never a request to set an effect
    (the phrase would have to read "what is twinkle", which no light lists)."""
    rig = effect_rig(monkeypatch)
    answer = say(rig, query, {"effect": "rain"})
    assert writes(rig) == [] and answer == shc.NO_WRITE_CUE_TEXT


def test_a_device_word_inside_an_effects_name_is_accepted_when_a_permitted_light_lists_it(monkeypatch):
    """F120: "turn on the tv simulator" with no word "effect"."""
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST + ["TV Simulator"])
    rig = effect_rig(monkeypatch, states)
    say(rig, "turn the study prism to tv simulator", {"effect": "TV Simulator"})
    assert writes(rig) == [turn_on("light.study_lamp", "TV Simulator")]
    unlisted = effect_rig(monkeypatch)
    assert say(unlisted, "turn on the tv", {"effect": "TV Simulator"}) in (shc.NO_DEVICE_TEXT, shc.NO_WRITE_CUE_TEXT)
    assert writes(unlisted) == []


def test_sixty_leaves_sharing_one_list_cost_one_match(monkeypatch):
    """F96."""
    real_house = tat.house

    def big(**kw):
        raw, states = real_house(**kw)
        for i in range(60):
            eid = f"light.galley_strip_{i}"
            raw["entities"].append(tat._reg(eid, area="area_galley"))
            states[eid] = tat._state(eid, name=f"Galley Strip {i}")
            give(states, eid, f"Galley Strip {i}", WLED_LIST)
        return raw, states

    monkeypatch.setattr(tat, "house", big)
    _, states = big()
    give(states, "light.galley_bulb_1", "Galley Spot", WLED_LIST)
    give(states, "light.galley_bulb_2", "Galley Wash", WLED_LIST)
    calls = {"n": 0}
    real_match = light_effects.match

    def counting(*a, **kw):
        calls["n"] += 1
        return real_match(*a, **kw)

    monkeypatch.setattr(light_effects, "match", counting)
    rig = Rig(monkeypatch, states=states, hard_limit=500)
    say(rig, "make the galley twinkle", {"effect": "twinkle"})
    assert len(writes(rig)) == 62 and calls["n"] == 1


def test_the_writer_itself_checks_the_effect_feature_before_every_write(monkeypatch):
    """F120: a light that stops reporting the EFFECT feature between the plan and the write is not written."""
    rig = effect_rig(monkeypatch)
    monkeypatch.setattr(light_effects, "supports_effects", lambda state: False)
    answer = say(rig, "set the study prism to fire 2012", {"effect": "fire 2012"})
    assert writes(rig) == [] and answer == "I couldn't change the effect right now."


def test_a_write_the_guard_denies_is_raised_not_swallowed_as_a_failure(monkeypatch):
    rig = effect_rig(monkeypatch)
    rig.raw.call_service = AsyncMock(side_effect=mp.HAWritePermissionDenied("denied", reason="entity_or_domain_denied"))
    # the guard wraps the raw client, so a denial from below it still reaches the caller as a denial
    with pytest.raises(mp.HAWritePermissionDenied):
        say(rig, "set the study prism to fire 2012", {"effect": "fire 2012"})


def test_a_name_that_already_ends_in_effect_is_not_said_twice():
    block = write_fanout.FanoutBlock(
        writes=(write_fanout.PlannedWrite("light", "set_effect", ("light.a", "light.b"), "rain effect"),), unbounded=False, room="study")
    text = write_fanout.rewording(block)
    assert "effect effect" not in text and "set the rain effect on 2 lights" in text and text.endswith("set all the study lights to rain effect.")


def _direct(rig, query, parameters=None, action="set_effect", device_room=None):
    """The handler itself, with the cue check skipped: its own refusals are what is tested."""
    body = intent(**(parameters or {}))
    body["action"] = action

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query), trusted_voice=True):
            return await rig.controller._handle_effect_intent(body, rig.raw, query, device_room)

    return asyncio.run(go())


@pytest.mark.parametrize("spaced", ["o f f", "n o n e", "s o l i d"])
def test_a_spaced_clear_alias_said_as_a_set_is_never_written_by_the_cue_or_the_handler(monkeypatch, spaced):
    """F125: a set that resolves to a clear member would skip the clear rules and could turn an off light on."""
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST + ["off", "none"], state="off", effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    query = f"set the study prism to {spaced}"
    assert say(rig, query, {"effect": spaced}) == shc.NO_WRITE_CUE_TEXT
    assert _direct(rig, query, {"effect": spaced}) == shc.NO_WRITE_CUE_TEXT
    assert writes(rig) == []


def test_a_fuzzy_match_that_lands_on_a_clear_member_is_never_written_as_a_set(monkeypatch):
    rig = effect_rig(monkeypatch)
    answer = _direct(rig, "sollid effect on the study prism", {"effect": "sollid"})
    assert writes(rig) == [] and answer.startswith("I couldn't find that effect on the Study Prism")
    assert light_effects.match("sollid", WLED_LIST, fuzzy=True) == "Solid", "the fuzzy step does reach the clear member"


HOUSE_MODE_EFFECTS = ["Guest", "Away", "Home", "Sleep", "Vacation", "Holiday", "Eco", "Game", "Night", "Morning", "Party", "Quiet",
                      "Do Not Disturb", "Sound"]


def _house_mode_index():
    state = {"entity_id": "light.example_modes", "state": "on",
             "attributes": {"supported_features": 4, "effect_list": HOUSE_MODE_EFFECTS + ["Fire"]}}
    return light_effects.EffectIndex({state["entity_id"]: state}), [state["entity_id"]]


def _recognise_real(text):
    index, ids = _house_mode_index()
    return light_effects.recognise(text, index, ids, claimed=shc.effect_word_claimed,
                                   names_target=fx.names_target, names_light=fx.names_light)


D19_WORDS = ("guest", "away", "home", "sleep", "vacation", "holiday", "eco", "game", "night", "morning", "party", "quiet",
             "do not disturb")


def test_the_house_mode_words_are_the_thirteen_the_decision_names():
    assert len(D19_WORDS) == 13 and set(shc._HOUSE_MODE_WORDS) == set(D19_WORDS)


@pytest.mark.parametrize("word", D19_WORDS)
def test_a_house_mode_never_takes_the_effect_route_even_when_a_light_lists_an_effect_of_that_name(word):
    """D19."""
    for text in (f"turn off {word} mode", f"{word} mode off", f"set the study prism to {word} mode", f"{word} mode on the study prism",
                 f"stop {word} mode", f"turn off the {word} mode"):
        assert _recognise_real(text) is None, text


@pytest.mark.parametrize("word", ["guest", "away", "sleep", "game", "do not disturb"])
def test_the_effect_of_that_name_is_still_asked_for_with_the_word_effect(word):
    request = _recognise_real(f"set the study prism to the {word} effect")
    assert request is not None and request.phrase == word and request.explicit
    cleared = _recognise_real(f"turn off the {word} effect")
    assert cleared is not None and cleared.clear and cleared.phrase == word


def test_the_house_mode_words_are_claimed_only_with_mode():
    for word in shc._HOUSE_MODE_WORDS:
        assert shc.effect_word_claimed(f"{word} mode")
    for word in ("guest", "away", "sleep", "game", "vacation", "holiday", "eco", "quiet", "do not disturb"):
        assert not shc.effect_word_claimed(word), word


def test_turn_off_the_sound_effects_stays_off_the_route():
    """With the cold index and with the full WLED list (which lists no sound effect)."""
    assert light_effects.recognise("turn off the sound effects", None, None, claimed=shc.effect_word_claimed,
                                   names_target=fx.names_target, names_light=fx.names_light) is None
    wled = light_effects.EffectIndex({fx.WLED_ID: fx.WLED})
    assert light_effects.recognise("turn off the sound effects", wled, [fx.WLED_ID], claimed=shc.effect_word_claimed,
                                   names_target=fx.names_target, names_light=fx.names_light) is None


@pytest.mark.parametrize("text", ["turn off guest mode", "away mode off", "sleep mode off", "game mode off"])
def test_a_house_mode_phrase_writes_nothing_through_the_controller(monkeypatch, text):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", HOUSE_MODE_EFFECTS + ["Fire"], effect="Guest")
    rig = effect_rig(monkeypatch, states)
    word = text.replace("turn off ", "").replace(" off", "").replace(" mode", "")
    assert say(rig, text, {"effect": word}, action="clear_effect") in (shc.NO_WRITE_CUE_TEXT, shc.NO_DEVICE_TEXT)
    assert writes(rig) == []


# -- the fast path feeding the writer (Phase 3) ------------------------------------------

def _through_the_fast_path(rig, query, device_room="study"):
    """extract_intent (no model: it fails if called) then execute_intent, under one scope."""
    rig.controller.llm_router = MagicMock()
    rig.controller.llm_router.generate = AsyncMock(side_effect=AssertionError("the model was called"))

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query), trusted_voice=True):
            body = await rig.controller.extract_intent(query, device_room=device_room)
            return body, await rig.controller.execute_intent(body, rig.raw, query, device_room)

    return asyncio.run(go())


def test_the_reported_shape_sets_the_effect_with_no_model_call_and_one_write(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", GOVEE_LIST)
    rig = effect_rig(monkeypatch, states)
    body, answer = _through_the_fast_path(rig, "set the study prism to candlelight")
    assert body["action"] == "set_effect" and writes(rig) == [turn_on("light.study_lamp", "candlelight")]
    assert answer == "Set the Study Prism to candlelight."


def test_turn_off_the_effect_through_the_fast_path_clears_a_running_effect_and_leaves_lights_on(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    body, answer = _through_the_fast_path(rig, "turn off the effect")
    assert body["action"] == "clear_effect"
    assert writes(rig) == [turn_on("light.study_lamp", "Solid")] and all(w[1] == "turn_on" for w in writes(rig))
    assert answer == "Turned off the effect on the Study Prism."


def test_turn_off_the_effect_through_the_fast_path_does_not_touch_an_off_light(monkeypatch):
    states = effect_states()
    give(states, "light.study_lamp", "Study Prism", WLED_LIST, state="off", effect="Fire 2012")
    rig = effect_rig(monkeypatch, states)
    _, answer = _through_the_fast_path(rig, "turn off the effect")
    assert writes(rig) == [] and answer == "There's no effect running on the Study Prism."


def test_a_repeated_effect_word_names_no_light_in_the_controller_either(monkeypatch):
    """F151: "twinkle twinkle" must not name a bedroom light called Twinkle, or send the write there."""
    states = effect_states()
    give(states, "light.north_lamp", "Twinkle", GOVEE_LIST)
    rig = effect_rig(monkeypatch, states)
    say(rig, "make the galley twinkle twinkle", {"effect": "twinkle"})
    assert sorted(w[2]["entity_id"] for w in writes(rig)) == ["light.galley_bulb_1", "light.galley_bulb_2"]
    assert all(w[2]["entity_id"] != "light.north_lamp" for w in writes(rig))


def test_no_light_effect_test_builds_a_monkeypatch_of_its_own():
    """A MonkeyPatch made by hand is never undone: its patches (a mock admin client, the config) stay
    for every later test in the process, which broke 26 tests in test_write_fanout_confirmation.py
    whenever this file was collected before it."""
    from pathlib import Path
    hand_made = re.compile("Monkey" + r"Patch\(\)")
    offenders = [p.name for p in sorted(Path(__file__).resolve().parent.glob("test_light_effect*.py"))
                 if hand_made.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
