"""The automation agent fails closed on light effects, immediate and scheduled.

An effect reaches Home Assistant only as the literal ``data.effect`` of a
``light.turn_on`` the user's words call for, aimed at lights the caller may
control, spelled as each light's own list spells it. Every other carrier, an
unspoken effect, a scheduled clear, an unverifiable light or a denied one
refuses the whole call. Lights and names here are fictional.
"""
from __future__ import annotations

import asyncio
import copy
import io
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import automation_agent as aa  # noqa: E402
from orchestrator import light_effects as le  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_probe  # noqa: E402
from orchestrator.automation_agent import AUTOMATION_TOOLS, AutomationAgent  # noqa: E402

EFFECTS = ("none", "candlelight", "twinkle", "Fire 2012")
OWNER = {"mode": "owner"}
SPOKEN = "set the prism to candlelight"
SCHEDULED = "at 8pm set the prism to candlelight"
PRISM, STRIP, LAMP, GROUP = "light.example_prism", "light.example_strip", "light.example_lamp", "light.example_group"
TRIGGER = {"type": "time", "time": "20:00"}


def light(entity_id, effects=EFFECTS, *, state="on", effect=None, features=36, **extra):
    attrs = {
        "friendly_name": f"Secret {entity_id.split('.')[1]}", "supported_features": features,
        "effect_list": list(effects), "effect": effect, "brightness": 128,
    }
    attrs.update(extra)
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def house(**override):
    states = {s["entity_id"]: s for s in (light(PRISM), light(STRIP), light(LAMP, effects=("none", "twinkle")))}
    states[GROUP] = {"entity_id": GROUP, "state": "on", "attributes": {"entity_id": [PRISM, STRIP], "friendly_name": "Secret group"}}
    states.update(override)
    return states


class Manager:
    def __init__(self, states, fail=False):
        self.states, self.fail, self.reads = states, fail, 0

    async def get_entities(self, force_refresh=False, max_age=None):
        self.reads += 1
        if self.fail:
            raise RuntimeError("secret backend detail")
        return self.states


class Admin:
    def __init__(self):
        self.stored = []

    async def create_voice_automation(self, record, **_):
        self.stored.append(record)
        return {"id": 1}


class Rig:
    def __init__(self, states=None, manager=True, admin=True, fail=False):
        self.ha = MagicMock()
        self.ha.create_automation = AsyncMock(return_value=True)
        self.ha.call_service = AsyncMock(return_value=True)
        self.manager = Manager(states if states is not None else house(), fail=fail) if manager else None
        self.admin = Admin() if admin else None
        self.agent = AutomationAgent(self.ha, MagicMock(), admin_client=self.admin, entity_manager=self.manager)
        self.agent.ha_client = self.ha  # the raw mock stands in for the permission wrapper

    def context(self, query):
        return {"query": query, "mode": "owner", "room": "study", "session_id": "s1"}

    def say(self, query, domain="light", service="turn_on", entity_id=PRISM, data=None, perms=OWNER):
        args = {"service": service, "entity_id": entity_id}
        if domain is not None:
            args["domain"] = domain
        if data is not None:
            args["data"] = data
        with mp.ha_permission_scope(perms, mode="owner" if perms.get("mode") == "owner" else "guest"):
            return asyncio.run(self.agent._exec_ha_service(args, self.context(query)))

    def schedule(self, actions=None, query=SCHEDULED, perms=OWNER, **extra):
        args = {"name": "evening", "trigger": TRIGGER, "actions": actions}
        args.update(extra)
        if actions is None:
            del args["actions"]
        with mp.ha_permission_scope(perms, mode="owner" if perms.get("mode") == "owner" else "guest"):
            return asyncio.run(self.agent._create_automation(args, self.context(query)))

    @property
    def writes(self):
        return [c.args for c in self.ha.call_service.await_args_list]

    @property
    def registered(self):
        return [c.args[1] for c in self.ha.create_automation.await_args_list]

    @property
    def stored(self):
        return self.admin.stored if self.admin else []


ROOMS_OF = {}  # leaf -> room keys; a leaf not listed is in the study, where the speaker is


@pytest.fixture(autouse=True)
def _rooms(monkeypatch):
    ROOMS_OF.clear()
    monkeypatch.setattr(AutomationAgent, "_leaf_room_keys", lambda self, leaf: set(ROOMS_OF.get(leaf, {"study"})))


@pytest.fixture(autouse=True)
def _no_assistant_names(monkeypatch):
    async def none():
        return ()

    monkeypatch.setattr(aa, "_assistant_names", none)


def effect_step(effect="candlelight", entity=PRISM, **extra):
    return {"service": "light.turn_on", "entity_id": entity, "data": {"effect": effect, **extra}}


def nothing_happened(rig):
    return not rig.writes and not rig.registered and not rig.stored


GUEST_WITHOUT_STRIP = {**mp.apply_guest_baseline({"mode": "guest"}), "restricted_entities": [r".*strip.*"]}
GUEST_WITHOUT_PRISM = {**mp.apply_guest_baseline({"mode": "guest"}), "restricted_entities": [r".*prism.*"]}


# ----- the schema and the read tool ------------------------------------------------------------

def test_the_ha_service_schema_lists_effect():
    tool = next(t for t in AUTOMATION_TOOLS if t["function"]["name"] == "ha_service")
    assert tool["function"]["parameters"]["properties"]["data"]["properties"]["effect"]["type"] == "string"


@pytest.mark.parametrize("count,shown,more", [(0, 0, ""), (25, 25, ""), (26, 25, " and 1 more")])
def test_get_entity_state_shows_at_most_25_effects_and_a_count(count, shown, more):
    names = [f"effect {chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(count)]
    rig = Rig({PRISM: light(PRISM, effects=names)})
    rig.ha.get_state = AsyncMock(return_value=rig.manager.states[PRISM])
    text = asyncio.run(rig.agent._get_state({"entity_id": PRISM}))
    listed = text.split("effects: ")[1].split(";")[0] if count else ""
    assert (len([n for n in names if n in text]) == shown) and (listed.endswith(more) if more else " more" not in text)
    if not count:
        assert "effects:" not in text


def test_get_entity_state_hides_effects_of_a_denied_light_and_of_one_that_takes_none():
    rig = Rig()
    rig.ha.get_state = AsyncMock(return_value=rig.manager.states[PRISM])
    with mp.ha_permission_scope(GUEST_WITHOUT_PRISM, mode="guest"):
        denied = asyncio.run(rig.agent._get_state({"entity_id": PRISM}))
    permitted = asyncio.run(rig.agent._get_state({"entity_id": PRISM}))
    assert "candlelight" not in denied and "effects:" not in denied
    assert "candlelight" in permitted
    rig.ha.get_state = AsyncMock(return_value=light(PRISM, features=0))
    assert "effects:" not in asyncio.run(rig.agent._get_state({"entity_id": PRISM}))


# ----- immediate -------------------------------------------------------------------------------

def test_a_spoken_effect_passes_spelled_as_the_light_spells_it():
    rig = Rig()
    reply = rig.say("set the prism to Fire 2012", data={"effect": "fire2012"})
    assert rig.writes == [("light", "turn_on", {"entity_id": [PRISM], "effect": "Fire 2012"})], reply


def test_an_invented_effect_is_refused_even_when_spoken():
    rig = Rig()
    assert rig.say("set the prism to zebra", data={"effect": "zebra"}) == aa.EFFECT_NOT_FOUND_TEXT
    assert not rig.writes


def test_an_effect_the_model_picked_but_the_user_never_said_is_refused_now():
    rig = Rig()
    reply = rig.say("turn on the prism", data={"effect": "twinkle"})
    assert not rig.writes and "twinkle" not in reply


def test_a_spoken_effect_is_accepted_now_and_an_unspoken_one_is_refused_in_the_same_words():
    rig = Rig()
    rig.say(SPOKEN, data={"effect": "twinkle"})
    assert not rig.writes
    rig.say(SPOKEN, data={"effect": "candlelight"})
    assert len(rig.writes) == 1


def test_a_clear_word_beside_the_effect_does_not_set_it():
    rig = Rig()
    rig.say("turn off the candlelight on the prism", data={"effect": "candlelight"})
    assert not rig.writes


IMMEDIATE_CARRIERS = [
    ("light", "turn_on", {"data_template": {"effect": "candlelight"}}),
    ("light", "turn_on", {"service_data": {"effect": "candlelight"}}),
    ("light", "turn_on", {"effect": "candlelight", "target": {"entity_id": PRISM}}),
    ("light", "turn_on", {"effect": "candlelight", "area_id": "study"}),
    ("light", "turn_on", {"effect": "candlelight", "device_id": "abc"}),
    ("light", "turn_on", {"effect": "{{ 'candlelight' }}"}),
    ("light", "turn_on", {"effect": ["candlelight"]}),
    ("light", "turn_on", {"effect": 7}),
    ("light", "turn_on", {"effect": "<b>candlelight</b>"}),
    ("light", "turn_on", {"effect": 'candle"light'}),
    ("light", "turn_on", {"effect": "candle`light"}),
    ("light", "turn_off", {"effect": "candlelight"}),
    ("homeassistant", "turn_on", {"effect": "candlelight"}),
    ("{{ d }}", "turn_on", {"effect": "candlelight"}),
    ("light", "turn_on", {"{{ k }}": 1, "effect": "candlelight"}),
]


@pytest.mark.parametrize("domain,service,data", IMMEDIATE_CARRIERS)
def test_every_carrier_but_a_literal_data_effect_is_refused_now(domain, service, data):
    rig = Rig()
    rig.say(SPOKEN, domain=domain, service=service, data=copy.deepcopy(data))
    assert not rig.writes


@pytest.mark.parametrize("entity_id", ["all", "light.*", "", "area.study", "light.a,,light.b", "light.example_prism, switch.x"])
def test_target_forms_that_are_not_concrete_lights_are_refused_now(entity_id):
    rig = Rig()
    rig.say(SPOKEN, entity_id=entity_id, data={"effect": "candlelight"})
    assert not rig.writes


def test_a_comma_list_of_lights_writes_each_leaf_with_its_own_spelling():
    states = house()
    states[STRIP] = light(STRIP, effects=("none", "Candlelight"))
    rig = Rig(states)
    rig.say(SPOKEN, entity_id=f"{PRISM}, {STRIP}", data={"effect": "candlelight"})
    assert sorted((w[2]["effect"], tuple(w[2]["entity_id"])) for w in rig.writes) == [
        ("Candlelight", (STRIP,)), ("candlelight", (PRISM,))]


def test_a_group_is_written_as_its_leaves_never_as_the_group():
    rig = Rig()
    rig.say(SPOKEN, entity_id=GROUP, data={"effect": "candlelight"})
    assert rig.writes == [("light", "turn_on", {"entity_id": [PRISM, STRIP], "effect": "candlelight"})]


def test_a_group_with_one_light_lacking_the_effect_writes_nothing():
    states = house()
    states[STRIP] = light(STRIP, effects=("none", "twinkle"))
    rig = Rig(states)
    assert rig.say(SPOKEN, entity_id=GROUP, data={"effect": "candlelight"}) == aa.EFFECT_NOT_FOUND_TEXT
    assert not rig.writes


def test_a_light_whose_list_has_no_feature_bit_takes_no_effect():
    rig = Rig({PRISM: light(PRISM, features=0)})
    rig.say(SPOKEN, data={"effect": "candlelight"})
    assert not rig.writes


def test_light_data_travels_with_the_effect_only_when_the_words_back_it():
    rig = Rig()
    rig.say(SPOKEN, data={"effect": "candlelight", "brightness": 90, "transition": 5, "flash": "long"})
    assert rig.writes[0][2] == {"entity_id": [PRISM], "effect": "candlelight"}, "no word backs any of them"
    rig.ha.call_service.reset_mock()
    rig.say("set the prism to candlelight at 90 percent", data={"effect": "candlelight", "brightness": 90, "transition": 5})
    assert rig.writes[0][2] == {"brightness": 90, "entity_id": [PRISM], "effect": "candlelight"}, "brightness is backed, transition never"


def test_a_call_with_no_effect_is_passed_through_untouched():
    rig = Rig()
    data = {"brightness": 90}
    rig.say("turn on the prism", data=data)
    assert rig.writes == [("light", "turn_on", {"brightness": 90, "entity_id": PRISM})]
    assert rig.manager.reads == 0


def test_no_entity_manager_refuses_an_immediate_effect():
    rig = Rig(manager=False)
    assert rig.say(SPOKEN, data={"effect": "candlelight"}) == aa.EFFECT_CANT_CHECK_TEXT
    assert not rig.writes


def test_a_failed_entity_read_refuses_and_leaks_no_detail():
    rig = Rig(fail=True)
    reply = rig.say(SPOKEN, data={"effect": "candlelight"})
    assert reply == aa.EFFECT_CANT_CHECK_TEXT and "secret" not in reply and not rig.writes


def test_an_immediate_agent_clear_never_writes_an_off_light():
    states = house()
    states[PRISM] = light(PRISM, effect="candlelight")
    states[STRIP] = light(STRIP, effect="candlelight", state="off")
    states[LAMP] = light(LAMP, effects=("none", "twinkle"), effect="none")
    rig = Rig(states)
    rig.say("turn off the effect on the lights", entity_id=f"{PRISM},{STRIP},{LAMP}", data={"effect": "none"})
    assert rig.writes == [("light", "turn_on", {"entity_id": [PRISM], "effect": "none"})]


def test_an_immediate_clear_with_no_running_effect_writes_nothing():
    states = house()
    states[PRISM] = light(PRISM, effect="candlelight", state="off")
    rig = Rig(states)
    assert rig.say("turn off the effect on the prism", data={"effect": "none"}) == aa.EFFECT_NONE_RUNNING_TEXT
    assert not rig.writes


def test_an_immediate_clear_of_a_light_with_no_clear_alias_is_refused():
    states = {PRISM: light(PRISM, effects=("twinkle",), effect="twinkle")}
    rig = Rig(states)
    assert rig.say("turn off the effect on the prism", data={"effect": "off"}) == aa.EFFECT_CANT_CLEAR_TEXT
    assert not rig.writes


def test_a_clear_needs_the_words_to_ask_for_one():
    states = house()
    states[PRISM] = light(PRISM, effect="candlelight")
    rig = Rig(states)
    rig.say("turn on the prism", data={"effect": "none"})
    assert not rig.writes


def test_a_denied_light_gets_the_scopes_refusal_with_no_name_and_no_read():
    rig = Rig()
    reply = rig.say(SPOKEN, data={"effect": "candlelight"}, perms=GUEST_WITHOUT_PRISM)
    assert not rig.writes and "prism" not in reply.lower() and "secret" not in reply.lower()
    assert rig.manager.reads == 0, "the ids are checked before anything is read"


class SpyAttrs(dict):
    log: list = []

    def get(self, key, default=None):
        SpyAttrs.log.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        SpyAttrs.log.append(key)
        return super().__getitem__(key)


def spied_house(denied_member):
    states = house()
    for entity in states.values():
        entity["attributes"] = SpyAttrs(entity["attributes"])
    states[GROUP]["attributes"]["entity_id"] = [PRISM, denied_member]
    return states


def test_a_denied_leaf_refuses_before_names_or_effect_lists_are_read(monkeypatch):
    calls = []
    real = le.sanitize_effect_list
    monkeypatch.setattr(le, "sanitize_effect_list", lambda raw: calls.append(1) or real(raw))
    SpyAttrs.log = []
    rig = Rig(spied_house(STRIP))
    reply = rig.say(SPOKEN, entity_id=GROUP, data={"effect": "candlelight"}, perms=GUEST_WITHOUT_STRIP)
    assert not rig.writes and rig.manager.reads == 1
    assert calls == [], "no effect list was sanitised"
    assert not {"friendly_name", "effect_list", "effect", "supported_features"} & set(SpyAttrs.log)
    assert "strip" not in reply.lower() and "secret" not in reply.lower()


def test_the_spy_does_see_the_reads_when_the_leaf_is_permitted(monkeypatch):
    SpyAttrs.log = []
    rig = Rig(spied_house(STRIP))
    rig.say(SPOKEN, entity_id=GROUP, data={"effect": "candlelight"})
    assert "effect_list" in SpyAttrs.log and len(rig.writes) == 1


def test_a_denied_leaf_is_recorded_on_the_scope_without_halting_it():
    rig = Rig()
    with mp.ha_permission_scope(GUEST_WITHOUT_STRIP, mode="guest") as scope:
        asyncio.run(rig.agent._exec_ha_service(
            {"domain": "light", "service": "turn_on", "entity_id": GROUP, "data": {"effect": "candlelight"}},
            rig.context(SPOKEN)))
        assert [d.reason for d in scope.denials] == ["entity_or_domain_denied"]
    assert not rig.writes


# ----- the cue --------------------------------------------------------------------------------

def test_the_service_cue_judges_set_effect_when_the_data_carries_one():
    refusal = aa._service_cue_refusal("light", "turn_on", {"query": "turn on the prism"}, (), {"effect": "twinkle"})
    assert refusal is not None
    assert aa._service_cue_refusal("light", "turn_on", {"query": "turn on the prism"}, (), {"brightness": 5}) is None
    assert aa._service_cue_refusal("light", "turn_on", {"query": SPOKEN}, (), {"effect": "candlelight"}) is None


# ----- scheduled -------------------------------------------------------------------------------

def test_a_spoken_scheduled_effect_registers_and_is_stored():
    rig = Rig()
    reply = rig.schedule([effect_step()])
    assert reply.startswith("Created automation"), reply
    assert len(rig.registered) == 1 and len(rig.stored) == 1


def test_a_valid_but_unspoken_scheduled_effect_is_neither_registered_nor_stored():
    rig = Rig()
    reply = rig.schedule([effect_step("twinkle")])
    assert not reply.startswith("Created") and nothing_happened(rig)


def test_the_scheduled_words_check_allows_a_time_led_command(monkeypatch):
    seen = []
    real = aa.access_target.write_cue_problem

    def spy(*a, **k):
        seen.append(k.get("allow_scheduled"))
        return real(*a, **k)

    monkeypatch.setattr(aa.access_target, "write_cue_problem", spy)
    Rig().schedule([effect_step()])
    assert seen == [True]


def test_a_scheduled_effect_is_refused_with_no_words_at_all():
    rig = Rig()
    with mp.ha_permission_scope(OWNER, mode="owner"):
        asyncio.run(rig.agent._create_automation(
            {"name": "x", "trigger": TRIGGER, "actions": [effect_step()]},
            {"mode": "owner", "session_id": "s"}))
    assert nothing_happened(rig)


LEAF = {"service": "light.turn_on", "target": {"entity_id": PRISM}, "data": {"effect": "candlelight"}}
SCHEDULED_CARRIERS = {
    "data_template": {"service": "light.turn_on", "entity_id": PRISM, "data_template": {"effect": "candlelight"}},
    "service_data": {"service": "light.turn_on", "entity_id": PRISM, "service_data": {"effect": "candlelight"}},
    "templated service": {"service": "{{ svc }}", "entity_id": PRISM, "data": {"effect": "candlelight"}},
    "templated service_template": {"service_template": "{{ svc }}", "entity_id": PRISM, "data": {"effect": "candlelight"}},
    "homeassistant.turn_on": {"service": "homeassistant.turn_on", "entity_id": PRISM, "data": {"effect": "candlelight"}},
    "light.turn_off": {"service": "light.turn_off", "entity_id": PRISM, "data": {"effect": "candlelight"}},
    "choose": {"choose": [{"conditions": [], "sequence": [LEAF]}]},
    "repeat": {"repeat": {"count": 2, "sequence": [LEAF]}},
    "parallel": {"parallel": [LEAF]},
    "if then": {"if": [{"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}], "then": [LEAF]},
    "if else": {"if": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}], "then": [], "else": [LEAF]},
    "sequence": {"sequence": [LEAF]},
    "default": {"choose": [], "default": [LEAF]},
    "nested choose in sequence": {"sequence": [{"choose": [{"conditions": [], "sequence": [LEAF]}]}]},
    "service and container": {**LEAF, "sequence": [LEAF]},
    "templated data": {"service": "light.turn_on", "entity_id": PRISM, "data": "{{ {'effect': 'candlelight'} }}"},
    "templated key": {"service": "light.turn_on", "entity_id": PRISM, "data": {"{{ k }}": "x", "effect": "candlelight"}},
    "area_id": {"service": "light.turn_on", "data": {"effect": "candlelight", "area_id": "study"}},
    "no target": {"service": "light.turn_on", "data": {"effect": "candlelight"}},
    "all": {"service": "light.turn_on", "entity_id": "all", "data": {"effect": "candlelight"}},
    "less-than": effect_step("<b>candlelight"),
    "double quote": effect_step('candle"light'),
    "backtick": effect_step("candle`light"),
    "non-string": effect_step(["candlelight"]),
    "two effects": {"service": "light.turn_on", "entity_id": PRISM, "data": {"effect": "candlelight"}, "target": {"effect": "x"}},
    "platform variant": {"platform": "light", "entity_id": PRISM, "turn_on": {"effect": "candlelight"}},
    "ha_service variant": {"ha_service": PRISM, "service": "turn_on", "data": {"effect": "candlelight"}},
}


@pytest.mark.parametrize("name", sorted(SCHEDULED_CARRIERS))
def test_every_scheduled_carrier_but_a_literal_data_effect_refuses_the_automation(name):
    rig = Rig()
    reply = rig.schedule([copy.deepcopy(SCHEDULED_CARRIERS[name])])
    assert not reply.startswith("Created") and nothing_happened(rig), reply


def test_a_valid_effect_beside_a_bad_one_refuses_everything():
    rig = Rig()
    rig.schedule([effect_step(), copy.deepcopy(SCHEDULED_CARRIERS["choose"])])
    assert nothing_happened(rig)


@pytest.mark.parametrize("extra", [
    {"action": effect_step()},
    {"action": [effect_step()]},
    {"variables": {"effect": "candlelight"}},
    {"trigger_variables": {"effect": "candlelight"}},
    {"use_blueprint": {"path": "x.yaml", "input": {"effect": "candlelight"}}},
    {"conditions": [{"type": "state", "entity_id": PRISM, "state": "on", "effect": "x"}]},
    {"triggers": [{"type": "time", "time": "20:00", "variables": {"effect": "x"}}]},
])
def test_an_effect_outside_the_action_list_refuses_the_automation(extra):
    rig = Rig()
    rig.schedule([effect_step()], **extra)
    assert nothing_happened(rig)


def test_the_singular_action_form_carrying_an_effect_is_refused_even_with_no_actions_list():
    rig = Rig()
    rig.schedule(None, action=effect_step())
    assert nothing_happened(rig)


@pytest.mark.parametrize("effect", ["none", "off", "Solid", "None"])
def test_a_scheduled_clear_is_refused(effect):
    rig = Rig()
    reply = rig.schedule([effect_step(effect)], query="at 8pm turn off the effect on the prism")
    assert reply == aa.EFFECT_SCHEDULED_CLEAR_TEXT and nothing_happened(rig)


def test_no_entity_manager_refuses_a_scheduled_effect():
    rig = Rig(manager=False)
    assert rig.schedule([effect_step()]) == aa.EFFECT_CANT_CHECK_TEXT
    assert nothing_happened(rig)


def test_a_denied_scheduled_leaf_refuses_the_automation_and_names_nothing():
    rig = Rig()
    reply = rig.schedule([effect_step(entity=GROUP)], perms=GUEST_WITHOUT_STRIP)
    assert nothing_happened(rig) and "strip" not in reply.lower() and "secret" not in reply.lower()


def test_a_scheduled_group_with_one_light_lacking_the_effect_is_all_or_nothing():
    states = house()
    states[STRIP] = light(STRIP, effects=("none", "twinkle"))
    rig = Rig(states)
    assert rig.schedule([effect_step(entity=GROUP)]) == aa.EFFECT_NOT_FOUND_TEXT
    assert nothing_happened(rig)


def registered_effect_steps(rig):
    return [s for s in rig.registered[0]["action"] if "effect" in (s.get("data") or {})]


def test_a_scheduled_group_becomes_its_leaves_one_step_per_spelling():
    states = house()
    states[STRIP] = light(STRIP, effects=("none", "Candlelight"))
    rig = Rig(states)
    rig.schedule([effect_step("candlelight", entity=GROUP)])
    steps = registered_effect_steps(rig)
    assert sorted((s["data"]["effect"], tuple(s["target"]["entity_id"])) for s in steps) == [
        ("Candlelight", (STRIP,)), ("candlelight", (PRISM,))]


STORED_EQUALS_REGISTERED_FORMS = {
    "tool format": lambda: effect_step(),
    "native target": lambda: {"service": "light.turn_on", "target": {"entity_id": PRISM}, "data": {"effect": "candlelight"}},
    "static action leaf": lambda: {"action": "light.turn_on", "target": {"entity_id": PRISM}, "data": {"effect": "candlelight"}},
    "action and service": lambda: {"action": "light.turn_on", "service": "light.turn_on", "entity_id": PRISM,
                                   "data": {"effect": "Candlelight"}},
    "data entity id": lambda: {"service": "light.turn_on", "data": {"effect": "candlelight", "entity_id": [PRISM, STRIP]}},
    "group": lambda: effect_step(entity=GROUP),
    "with delay": lambda: {**effect_step(), "delay": "00:05:00"},
    "with other data": lambda: effect_step(brightness=40),
}


def plain_calls(steps):
    """(service, sorted target ids, full data dict, delay) per step, read with no help from the module under test."""
    out = []
    for step in steps:
        if set(step) == {"delay"}:
            out.append(("delay", (), {}, step["delay"]))
            continue
        target = step.get("target", {}).get("entity_id", step.get("entity_id"))
        ids = [target] if isinstance(target, str) else list(target)
        out.append((step["service"], tuple(sorted(ids)), dict(step.get("data", {})), step.get("delay")))
    return out


@pytest.mark.parametrize("form", sorted(STORED_EQUALS_REGISTERED_FORMS))
def test_stored_actions_equal_registered_actions_for_effect_steps(form):
    """The record is the registered action list, exactly: no normalising either side."""
    rig = Rig()
    reply = rig.schedule([STORED_EQUALS_REGISTERED_FORMS[form]()], query=SCHEDULED + " at 40 percent")
    assert reply.startswith("Created automation"), reply
    stored, registered = rig.stored[0]["actions_config"], rig.registered[0]["action"]
    assert stored == registered and stored
    assert not any("action" in step for step in stored)
    calls = [step for step in registered if "service" in step]
    assert all(step["service"] == "light.turn_on" and "target" in step and "entity_id" not in step for step in calls)
    assert all(step["data"]["effect"] in ("candlelight", "Candlelight") for step in calls)
    assert all(step["data"].get("brightness") == (40 if form == "with other data" else None) for step in calls)
    if form == "with delay":
        assert registered[-1] == {"delay": "00:05:00"} and stored[-1] == registered[-1]


def test_an_automation_with_no_effect_is_stored_as_it_always_was():
    rig = Rig()
    step = {"service": "light.turn_on", "entity_id": PRISM, "data": {"brightness": 50}, "delay": "00:01:00"}
    rig.schedule([step])
    assert rig.stored[0]["actions_config"] == [step], "the pre-image, unchanged"
    assert rig.registered[0]["action"] != [step]


def test_a_delay_is_kept_after_the_last_step_it_belongs_to():
    rig = Rig()
    rig.schedule([{**effect_step(entity=GROUP), "delay": "00:05:00"}])
    kinds = ["delay" if "delay" in s else "call" for s in rig.registered[0]["action"]]
    assert kinds == ["call", "delay"]


def test_an_automation_with_no_effect_is_passed_through_unchanged():
    rig = Rig()
    args = {"name": "x", "trigger": TRIGGER, "actions": [
        {"service": "light.turn_on", "entity_id": PRISM, "data": {"brightness": 50}},
        {"service": "notify.mobile", "entity_id": "notify.me", "data": "{{ msg }}"},
    ]}
    refusal, validated = asyncio.run(rig.agent._effect_actions(args, rig.context("at 8pm turn on the prism")))
    assert refusal is None and validated is args and rig.manager.reads == 0


def test_hidden_content_beside_a_light_is_refused_but_a_plain_notification_template_is_not():
    rig = Rig()
    hidden = {"service": "light.turn_on", "entity_id": PRISM, "data": "{{ x }}"}
    refusal, _ = asyncio.run(rig.agent._effect_actions({"actions": [hidden]}, rig.context("at 8pm turn on the prism")))
    assert refusal == aa.EFFECT_NOT_ALLOWED_TEXT


# ----- the built config ------------------------------------------------------------------------

def built_for(rig, steps):
    args = {"name": "x", "trigger": TRIGGER, "actions": steps}
    return rig.agent._build_ha_automation("voice_x", args), args


@pytest.mark.parametrize("where", ["variables", "trigger_variables", "use_blueprint", "extra step", "other light", "other effect"])
def test_the_built_config_is_walked_whole_not_only_its_action_list(where):
    rig = Rig()
    config, args = built_for(rig, [effect_step()])
    assert rig.agent._built_effect_problem(config, args) is None, "positive control"
    config = copy.deepcopy(config)  # the builder shares the step's data dict with the validated list
    if where in ("variables", "trigger_variables"):
        config[where] = {"effect": "candlelight"}
    elif where == "use_blueprint":
        config[where] = {"path": "x.yaml", "input": {"effect": "candlelight"}}
    elif where == "extra step":
        config["action"].append(copy.deepcopy(config["action"][0]))
    elif where == "other light":
        config["action"][0]["target"]["entity_id"] = [STRIP]
    else:
        config["action"][0]["data"]["effect"] = "twinkle"
    assert rig.agent._built_effect_problem(config, args) == aa.EFFECT_NOT_ALLOWED_TEXT


def test_a_build_that_differs_from_the_validated_list_is_refused_before_registering(monkeypatch):
    rig = Rig()
    real = rig.agent._build_ha_automation

    def tampered(automation_id, args):
        config = real(automation_id, args)
        config["variables"] = {"effect": "twinkle"}
        return config

    monkeypatch.setattr(rig.agent, "_build_ha_automation", tampered)
    rig.schedule([effect_step()])
    assert nothing_happened(rig)


def test_the_validator_runs_once_and_feeds_both_the_build_and_the_store(monkeypatch):
    rig = Rig()
    built, stored = [], []
    real_build = rig.agent._build_ha_automation
    monkeypatch.setattr(rig.agent, "_build_ha_automation", lambda i, a: built.append(a) or real_build(i, a))
    real_store = rig.agent._store_automation_record

    async def store(i, a, c):
        stored.append(a)
        return await real_store(i, a, c)

    monkeypatch.setattr(rig.agent, "_store_automation_record", store)
    rig.schedule([effect_step(entity=GROUP)])
    assert built and stored
    assert built[0]["actions"][0]["entity_id"] == [PRISM, STRIP], "the build gets the validated list"
    assert stored[0]["actions"] == rig.registered[0]["action"], "the store gets what was registered"


# ----- logs ------------------------------------------------------------------------------------

class LogSpy:
    def __init__(self):
        self.calls = []

    def __getattr__(self, level):
        return lambda *a, **k: self.calls.append((level, a, k))


def test_a_failed_service_call_logs_the_error_class_only(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr(aa, "logger", spy)
    rig = Rig()
    rig.ha.call_service = AsyncMock(side_effect=RuntimeError("secret prism candlelight"))
    rig.say(SPOKEN, data={"effect": "candlelight"})
    errors = [c for c in spy.calls if c[0] == "error"]
    assert errors and errors[0][2] == {"error_class": "RuntimeError"} and errors[0][1] == ("ha_service_call_failed",)
    assert "secret" not in repr(spy.calls) and "candlelight" not in repr(spy.calls)


def test_an_unknown_action_shape_logs_its_key_names_only(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr(aa, "logger", spy)
    assert Rig().agent._build_single_action({"mystery": "light.secret_room", "effect": "candlelight"}) is None
    assert spy.calls == [("warning", ("unknown_action_format",), {"action_keys": ["effect", "mystery"]})]


# ----- room_probe --effects --------------------------------------------------------------------

def probe(effects):
    import io as _io
    from . import _room_chat_harness as rch

    raw, states = rch.house_raw_and_states()
    states["light.study_prism"] = light("light.study_prism", effect="candlelight")
    out = _io.StringIO()

    async def fetch():
        return raw

    async def read_states():
        return states

    code = asyncio.run(room_probe.run(
        fetch, read_states, runs=1, shape=False, show_names=False, utterances=[], effects=effects, out=out))
    return code, out.getvalue()


EFFECTS_LINE = re.compile(
    r"effects: lights \d+, with effects \d+, list without feature \d+, with a clear value \d+, running \d+, dropped names \d+"
)


def test_room_probe_run_prints_the_effects_line_only_with_the_flag():
    code, plain = probe(False)
    assert code == 0 and "effects:" not in plain
    code, flagged = probe(True)
    lines = [l for l in flagged.splitlines() if l.startswith("effects:")]
    assert code == 0 and len(lines) == 1 and EFFECTS_LINE.fullmatch(lines[0]), lines
    assert "running 1" in lines[0] and "with effects 1" in lines[0]
    for forbidden in ("light.", "study_prism", "Secret", "candlelight", "twinkle"):
        assert forbidden not in lines[0]
        assert forbidden not in flagged.split("effects:")[1]


@pytest.mark.parametrize("argv,expected", [(["--effects"], True), ([], False)])
def test_room_probe_main_passes_the_effects_flag_to_the_run(monkeypatch, argv, expected):
    seen = {}

    async def fake(*a, **k):
        seen.update(k)
        return 0

    monkeypatch.setenv("HA_URL", "http://ha.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "t")
    monkeypatch.setattr(room_probe, "_run_with_household", fake)
    assert room_probe.main(["--runs", "1", *argv]) == 0
    assert seen["effects"] is expected


# ----- wiring ----------------------------------------------------------------------------------

def test_the_agent_module_is_in_the_pii_room_scope():
    text = (ROOT / "tests" / "unit" / "test_no_pii_in_logs.py").read_text(encoding="utf-8")
    assert '"src/orchestrator/automation_agent.py"' in text


def test_no_agent_effect_path_reads_a_friendly_name():
    source = Path(aa.__file__).read_text(encoding="utf-8")
    start, end = source.index("async def _effect_leaves"), source.index("    async def _exec_ha_service")
    assert not re.search(r"friendly_name", source[start:end])


# ----- defence in depth: each guard holds when the one beside it is gone -------------------------

@pytest.mark.parametrize("name", ["choose", "repeat", "sequence", "default", "two effects"])
def test_the_direct_leaf_check_refuses_on_its_own_when_the_structure_validator_has_a_gap(monkeypatch, name):
    monkeypatch.setattr(le, "effect_step_problem", lambda step: None)
    rig = Rig()
    reply = rig.schedule([copy.deepcopy(SCHEDULED_CARRIERS[name])])
    assert reply == aa.EFFECT_NOT_ALLOWED_TEXT and nothing_happened(rig)


def test_a_group_member_that_is_not_a_light_is_refused_as_a_non_light_not_as_a_missing_effect():
    states = house()
    states[GROUP]["attributes"]["entity_id"] = [PRISM, "switch.example_plug"]
    rig = Rig(states)
    assert rig.say(SPOKEN, entity_id=GROUP, data={"effect": "candlelight"}) == aa.EFFECT_NOT_ALLOWED_TEXT
    assert not rig.writes


def test_a_running_effect_outside_the_charset_is_never_shown_to_the_model():
    rig = Rig({PRISM: light(PRISM, effect="<b>evil</b>")})
    rig.ha.get_state = AsyncMock(return_value=rig.manager.states[PRISM])
    text = asyncio.run(rig.agent._get_state({"entity_id": PRISM}))
    assert "evil" not in text and "running" not in text


# ----- F124: one spelling of domain, service and entity ------------------------------------------

@pytest.mark.parametrize("domain,entity", [
    ("light ", PRISM), (" light", PRISM), ("LIGHT", PRISM), ("light\n", PRISM), (None, PRISM), (None, " LIGHT.Example_Prism "),
])
def test_an_unspoken_effect_is_refused_whatever_whitespace_or_case_the_domain_arrives_in(domain, entity):
    rig = Rig()
    rig.say("turn on the prism", domain=domain, entity_id=entity, data={"effect": "twinkle"})
    assert not rig.writes


@pytest.mark.parametrize("domain,entity", [("light ", PRISM), (" light", PRISM), (None, " LIGHT.Example_Prism ")])
def test_a_spoken_effect_is_written_once_the_domain_is_normalised(domain, entity):
    rig = Rig()
    rig.say(SPOKEN, domain=domain, entity_id=entity, data={"effect": "candlelight"})
    assert rig.writes == [("light", "turn_on", {"entity_id": [PRISM], "effect": "candlelight"})]


@pytest.mark.parametrize("domain,service", [("light.x", "turn_on"), ("li ght", "turn_on"), ("light", "turn_on; x"), ("light", ""), (7, "turn_on")])
def test_a_domain_or_service_that_is_not_one_word_is_refused_before_anything_runs(domain, service):
    rig = Rig()
    assert rig.say(SPOKEN, domain=domain, service=service, data={"effect": "candlelight"}) == aa.BAD_SERVICE_TEXT
    assert not rig.writes and rig.manager.reads == 0


def test_a_service_with_stray_whitespace_is_written_under_its_clean_name():
    rig = Rig()
    rig.say("turn on the prism", service=" Turn_On ", data={"brightness": 3})
    assert rig.writes[0][:2] == ("light", "turn_on")


def test_the_effect_cue_runs_before_the_question_exit():
    asking = {"query": "is the prism on?"}
    assert aa._service_cue_refusal("light", "turn_on", asking, (), {"effect": "twinkle"}) is not None
    assert aa._service_cue_refusal("light", "turn_on", asking, (), None) is None, "control: a question with no effect exits as before"


def test_a_denied_caller_hears_the_same_answer_whatever_domain_the_effect_rode_in_on():
    light_rig, other_rig = Rig(), Rig()
    as_light = light_rig.say(SPOKEN, data={"effect": "candlelight"}, perms=GUEST_WITHOUT_PRISM)
    as_other = other_rig.say("turn on the prism switch", domain="switch", data={"effect": "candlelight"}, perms=GUEST_WITHOUT_PRISM)
    assert as_light == as_other and not light_rig.writes and not other_rig.writes
    assert other_rig.manager.reads == 0


def test_a_non_light_service_carrying_an_effect_is_refused_with_the_not_allowed_answer():
    rig = Rig()
    assert rig.say(SPOKEN, domain="homeassistant", data={"effect": "candlelight"}) == aa.EFFECT_NOT_ALLOWED_TEXT
    assert not rig.writes


# ----- F126: templates a non-owner may not schedule ----------------------------------------------

GUEST = mp.apply_guest_baseline({"mode": "guest"})


def notify(message, **extra):
    return {"service": "notify.mobile", "entity_id": "notify.me", "data": {"message": message, **extra}}


@pytest.mark.parametrize("step", [
    notify("{{ states('light.example_prism') }}"),
    notify("{{ state_attr('light.example_prism', 'effect_list') }}"),
    notify("{{ expand('light.example_group') | map(attribute='state') | list }}"),
    notify("{{ states.light.example_prism.state }}"),
    notify("{{ is_state('light.example_prism', 'on') }}"),
    notify("hello", target="{{ x }}"),
    {"service": "tts.speak", "entity_id": "tts.cloud", "data": {"message": "{{ area_entities('study') }}"}},
    {"service": "light.turn_on", "entity_id": PRISM, "data": {"brightness": "{{ 5 }}"}},
    {"service": "{{ svc }}", "entity_id": PRISM},
    {"service": "script.turn_on", "entity_id": "script.x", "data": {"variables": {"v": "{{ 1 }}"}}},
    {"sequence": [notify("{{ states('light.example_prism') }}")]},
])
def test_a_guest_automation_with_a_state_reading_or_stray_template_is_refused(step):
    rig = Rig()
    reply = rig.schedule([copy.deepcopy(step)], query="at 8pm turn on the prism and notify me", perms=GUEST)
    assert reply == aa.TEMPLATE_REFUSED_TEXT and nothing_happened(rig)


@pytest.mark.parametrize("step", [notify("It is {{ now().hour }} o'clock", title="Hi {{ 1 + 1 }}"), notify("plain words")])
def test_a_guest_notification_text_that_reads_nothing_about_the_house_is_allowed(step):
    rig = Rig()
    assert rig.schedule([copy.deepcopy(step)], query="at 8pm turn on the prism and notify me", perms=GUEST).startswith("Created")


def test_the_owner_keeps_templates_in_a_notification_policy_pinned():
    rig = Rig()
    reply = rig.schedule([notify("{{ states('light.example_prism') }}")], query="at 8pm turn on the prism and notify me")
    assert reply.startswith("Created") and len(rig.registered) == 1


# ----- F127: a light that stands for lights it doesn't list --------------------------------------

@pytest.mark.parametrize("extra", [
    {"is_hue_group": True}, {"group_id": 7}, {"entity_id": []}, {"entity_id": "light.a"}, {"is_deconz_group": True},
])
def test_a_group_light_that_lists_no_members_is_refused_now_and_later(extra):
    states = house()
    states[PRISM]["attributes"].update(extra)
    rig = Rig(states)
    assert rig.say(SPOKEN, data={"effect": "candlelight"}) == aa.EFFECT_GROUP_TEXT
    assert rig.schedule([effect_step()]) == aa.EFFECT_GROUP_TEXT
    assert nothing_happened(rig)


def test_a_group_that_does_list_its_members_is_still_fine():
    rig = Rig()
    rig.say(SPOKEN, entity_id=GROUP, data={"effect": "candlelight"})
    assert len(rig.writes) == 1


# ----- F131: the cap, near misses, read order on the scheduled path -------------------------------

def many_lights(count):
    states = {}
    for i in range(count):
        states[f"light.bulk_{i:02d}"] = light(f"light.bulk_{i:02d}")
    ids = sorted(states)
    for size in (6, 7, 64, 65):
        states[f"light.set_{size}"] = {"entity_id": f"light.set_{size}", "state": "on", "attributes": {"entity_id": ids[:size]}}
    return states


@pytest.mark.parametrize("size,allowed", [(64, True), (65, False)])
def test_the_scheduled_leaf_cap_is_64(size, allowed):
    rig = Rig(many_lights(65))
    reply = rig.schedule([effect_step(entity=f"light.set_{size}")])
    assert reply.startswith("Created") is allowed
    if allowed:
        assert len(rig.registered[0]["action"][0]["target"]["entity_id"]) == 64
    else:
        assert reply == aa.EFFECT_TOO_MANY_TEXT and nothing_happened(rig)


@pytest.mark.parametrize("threshold,size,allowed", [(6, 6, True), (6, 7, False), (0, 64, True), (100, 65, False)])
def test_an_immediate_fan_out_stops_at_the_controllers_confirm_threshold(monkeypatch, threshold, size, allowed):
    from types import SimpleNamespace
    monkeypatch.setattr(aa, "get_athena_config", lambda: SimpleNamespace(ha_write_fanout_confirm_threshold=threshold))
    rig = Rig(many_lights(65))
    reply = rig.say(SPOKEN, entity_id=f"light.set_{size}", data={"effect": "candlelight"})
    assert bool(rig.writes) is allowed
    if not allowed:
        assert reply == aa.EFFECT_TOO_MANY_TEXT


def test_a_near_miss_effect_name_is_refused_because_the_agent_matches_exactly():
    rig = Rig()
    assert rig.say("set the prism to candlelite", data={"effect": "candlelite"}) == aa.EFFECT_NOT_FOUND_TEXT
    assert rig.schedule([effect_step("candlelite")], query="at 8pm set the prism to candlelite") == aa.EFFECT_NOT_FOUND_TEXT
    assert not rig.writes and nothing_happened(rig)
    rig.say("set the prism to candle light", data={"effect": "Candle Light"})
    assert rig.writes[0][2]["effect"] == "candlelight", "control: a spacing difference still matches"


def test_the_scheduled_path_reads_nothing_of_a_denied_group_member(monkeypatch):
    calls = []
    real = le.sanitize_effect_list
    monkeypatch.setattr(le, "sanitize_effect_list", lambda raw: calls.append(1) or real(raw))
    SpyAttrs.log = []
    rig = Rig(spied_house(STRIP))
    reply = rig.schedule([effect_step(entity=GROUP)], perms=GUEST_WITHOUT_STRIP)
    assert nothing_happened(rig) and calls == []
    assert not {"friendly_name", "effect_list", "effect", "supported_features"} & set(SpyAttrs.log)
    assert "strip" not in reply.lower() and "secret" not in reply.lower()


def test_a_denied_group_id_is_refused_before_anything_is_read_on_the_scheduled_path():
    rig = Rig()
    perms = {**GUEST, "restricted_entities": [r".*group.*"]}
    reply = rig.schedule([effect_step(entity=GROUP)], perms=perms)
    assert nothing_happened(rig) and rig.manager.reads == 0 and "group" not in reply.lower()


def test_a_non_light_service_beside_a_denied_light_gives_the_denial_on_the_scheduled_path_too():
    rig = Rig()
    step = {"service": "homeassistant.turn_on", "entity_id": PRISM, "data": {"effect": "candlelight"}}
    plain = Rig().schedule([effect_step()], perms=GUEST_WITHOUT_PRISM)
    assert rig.schedule([step], perms=GUEST_WITHOUT_PRISM) == plain and nothing_happened(rig)


# ----- F125 stopgap: a spaced clear alias is a clear --------------------------------------------

@pytest.mark.parametrize("spelling", ["o f f", "O-F-F", "No ne", "sol id"])
def test_a_spaced_clear_alias_is_a_clear_not_a_set(spelling):
    rig = Rig()
    assert rig.schedule([effect_step(spelling)], query="at 8pm turn off the effect on the prism") == aa.EFFECT_SCHEDULED_CLEAR_TEXT
    states = house()
    states[PRISM] = light(PRISM, effects=("off", "candlelight"), effect="candlelight")
    now = Rig(states)
    now.say("turn off the effect on the prism", data={"effect": spelling})
    if spelling in ("o f f", "O-F-F"):
        assert now.writes == [("light", "turn_on", {"entity_id": [PRISM], "effect": "off"})]


def test_a_set_that_lands_on_a_clear_member_is_refused():
    states = house()
    states[PRISM] = light(PRISM, effects=("O F F", "candlelight"))
    rig = Rig(states)
    rig.say(SPOKEN, data={"effect": "candlelight"})
    assert len(rig.writes) == 1, "control: a real effect still writes"
    rig.ha.call_service.reset_mock()
    assert aa.AutomationAgent._spell_for_leaves("O F F", [PRISM], states) is None


# ----- F128/F132 ------------------------------------------------------------------------------

BAD_DELAYS = [
    "{{ 5 }}", "{% if x %}00:00:05{% endif %}", True, ["1"], None, "5", "00:00", "1:2:3", "00:00:05.1234", "00:00:05 ",
    {"hours": "{{ 1 }}"}, {"hours": True}, {"hours": -1}, {"hours": float("nan")}, {"weeks": 1}, {"{{ k }}": 1}, {"hours": [1]},
    -5, float("inf"), {}, {"seconds": None},
]
GOOD_DELAYS = [
    {"hours": 0, "minutes": 5, "seconds": 0, "milliseconds": 0}, {"days": 1}, {"seconds": 2.5},
    "00:05:00", "01:30:15.250", 90, 2.5, 0,
]


@pytest.mark.parametrize("delay", BAD_DELAYS, ids=repr)
def test_a_templated_or_oddly_shaped_delay_on_any_step_refuses_the_automation(delay):
    rig = Rig()
    rig.schedule([{"service": "light.turn_on", "entity_id": PRISM, "data": {"brightness": 5}, "delay": delay}])
    assert nothing_happened(rig)


@pytest.mark.parametrize("delay", GOOD_DELAYS, ids=repr)
def test_every_delay_home_assistant_takes_registers_on_an_ordinary_step(delay):
    rig = Rig()
    rig.schedule([{"service": "light.turn_on", "entity_id": PRISM, "data": {"brightness": 5}, "delay": delay}])
    assert len(rig.registered) == 1
    assert ({"delay": delay} in rig.registered[0]["action"]) is bool(delay), "the builder drops a zero delay: no wait"


@pytest.mark.parametrize("delay", GOOD_DELAYS, ids=repr)
def test_every_delay_home_assistant_takes_follows_an_effect_step_too(delay):
    rig = Rig()
    rig.schedule([{**effect_step(), "delay": delay}])
    assert len(rig.registered) == 1 and (rig.registered[0]["action"][-1] == {"delay": delay} or not delay)


@pytest.mark.parametrize("delay", BAD_DELAYS, ids=repr)
def test_a_bad_delay_beside_an_effect_refuses_it_too(delay):
    rig = Rig()
    rig.schedule([{**effect_step(), "delay": delay}])
    assert nothing_happened(rig)


def test_a_scheduled_effect_spelled_in_another_case_is_written_as_the_light_spells_it():
    rig = Rig()
    rig.schedule([effect_step("CANDLELIGHT")])
    assert registered_effect_steps(rig)[0]["data"]["effect"] == "candlelight"


def test_a_mixed_group_clear_skips_the_ineligible_lights_and_says_so():
    states = house()
    states[PRISM] = light(PRISM, effect="candlelight")
    states[STRIP] = light(STRIP, effect="candlelight", state="off")
    states[LAMP] = light(LAMP, effects=("none", "twinkle"), effect="none")
    rig = Rig(states)
    reply = rig.say("turn off the effect on the lights", entity_id=f"{PRISM},{STRIP},{LAMP}", data={"effect": "off"})
    assert rig.writes == [("light", "turn_on", {"entity_id": [PRISM], "effect": "none"})]
    assert "1 light" in reply and "2 had no effect running" in reply


def test_an_unavailable_light_with_an_effect_attribute_is_not_cleared():
    states = {PRISM: light(PRISM, state="unavailable", effect="candlelight")}
    rig = Rig(states)
    assert rig.say("turn off the effect on the prism", data={"effect": "none"}) == aa.EFFECT_NONE_RUNNING_TEXT
    assert not rig.writes


def test_a_registration_home_assistant_refuses_stores_nothing():
    rig = Rig()
    rig.ha.create_automation = AsyncMock(return_value=False)
    reply = rig.schedule([effect_step()])
    assert reply.startswith("Failed") and rig.stored == []


def test_automation_logs_carry_the_error_class_only_and_never_the_alias(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr(aa, "logger", spy)
    rig = Rig()
    rig.ha.create_automation = AsyncMock(side_effect=RuntimeError("secret candlelight prism"))
    rig.schedule([effect_step()])
    ok = Rig()

    async def broken(record, **_):
        raise RuntimeError("secret store detail")

    ok.admin.create_voice_automation = broken
    ok.schedule([effect_step()])
    flat = repr(spy.calls)
    assert "secret" not in flat and "candlelight" not in flat and "evening" not in flat
    assert {c[1][0] for c in spy.calls if c[0] in ("error", "warning")} >= {
        "ha_automation_registration_error", "create_automation_failed", "voice_automation_store_failed"}


# ----- fixtures: a Govee list and the WLED list on both paths ---------------------------------

def fixture_rig(name):
    import json
    state = json.loads((ROOT / "tests" / "fixtures" / "light_effects" / f"{name}.json").read_text(encoding="utf-8"))
    return Rig({state["entity_id"]: state}), state["entity_id"]


@pytest.mark.parametrize("fixture,spoken,listed", [("govee_local", "candlelight", "candlelight"), ("wled", "fire 2012", "Fire 2012")])
def test_a_real_effect_list_works_on_the_immediate_and_scheduled_paths(fixture, spoken, listed):
    rig, entity = fixture_rig(fixture)
    rig.say(f"set the strip to {spoken}", entity_id=entity, data={"effect": spoken})
    assert rig.writes == [("light", "turn_on", {"entity_id": [entity], "effect": listed})]
    scheduled = rig.schedule([effect_step(spoken, entity=entity)], query=f"at 8pm set the strip to {spoken}")
    assert scheduled.startswith("Created") and registered_effect_steps(rig)[0]["data"]["effect"] == listed


# ----- the real seam: HAEntityManager and PermissionEnforcingHAClient --------------------------

class RecordingInner:
    def __init__(self, states):
        self.states, self.calls = states, []

    async def get_states(self):
        return list(self.states)

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return {}

    async def create_automation(self, automation_id, config):
        self.calls.append(("create", automation_id, config))
        return True

    async def get_state(self, entity_id):
        return {}


def seam():
    from orchestrator.ha_entity_manager import HAEntityManager
    inner = RecordingInner(list(house().values()))
    wrapped = mp.PermissionEnforcingHAClient(inner)
    manager = HAEntityManager(wrapped)
    return inner, wrapped, AutomationAgent(wrapped, MagicMock(), admin_client=None, entity_manager=manager)


def seam_say(agent, query, entity, data, perms=OWNER):
    args = {"domain": "light", "service": "turn_on", "entity_id": entity, "data": data}
    with mp.ha_permission_scope(perms, mode="owner" if perms.get("mode") == "owner" else "guest"):
        return asyncio.run(agent._exec_ha_service(args, {"query": query, "mode": "owner", "room": "study", "session_id": "s"}))


def test_the_real_manager_and_the_real_permission_client_write_a_spoken_effect_to_leaves_only():
    inner, _wrapped, agent = seam()
    seam_say(agent, SPOKEN, GROUP, {"effect": "candlelight"})
    assert inner.calls == [("light", "turn_on", {"entity_id": [PRISM, STRIP], "effect": "candlelight"})]


def test_the_real_permission_client_is_the_backstop_for_a_denied_leaf():
    inner, wrapped, agent = seam()
    reply = seam_say(agent, SPOKEN, GROUP, {"effect": "candlelight"}, perms=GUEST_WITHOUT_STRIP)
    assert inner.calls == [] and "strip" not in reply.lower()
    with mp.ha_permission_scope(GUEST_WITHOUT_STRIP, mode="guest"):
        with pytest.raises(mp.HAWritePermissionDenied):
            asyncio.run(wrapped.call_service("light", "turn_on", {"entity_id": [PRISM, STRIP], "effect": "candlelight"}))
    assert inner.calls == []


def test_a_scheduled_effect_over_the_real_seam_registers_leaves_and_a_denied_leaf_registers_nothing():
    inner, _wrapped, agent = seam()
    args = {"name": "evening", "trigger": TRIGGER, "actions": [effect_step(entity=GROUP)]}
    context = {"query": SCHEDULED, "mode": "owner", "room": "study", "session_id": "s"}
    with mp.ha_permission_scope(GUEST_WITHOUT_STRIP, mode="guest"):
        denied = asyncio.run(agent._create_automation(copy.deepcopy(args), context))
    assert not denied.startswith("Created") and inner.calls == []
    with mp.ha_permission_scope(OWNER, mode="owner"):
        assert asyncio.run(agent._create_automation(copy.deepcopy(args), context)).startswith("Created")
    config = inner.calls[0][2]
    assert config["action"][0]["target"]["entity_id"] == [PRISM, STRIP]


def test_the_leaf_resolver_checks_the_ids_before_it_reads_anything_even_when_called_alone():
    rig = Rig()
    with mp.ha_permission_scope(GUEST_WITHOUT_PRISM, mode="guest"):
        refusal, leaves, _ = asyncio.run(rig.agent._effect_leaves([PRISM]))
    assert refusal and leaves == [] and rig.manager.reads == 0


# ----- F150: a delay is Home Assistant's, or it is refused -------------------------------------------

def test_an_empty_delay_map_is_refused_not_silently_dropped():
    rig = Rig()
    reply = rig.schedule([{**effect_step(), "delay": {}}])
    assert reply == aa.EFFECT_NOT_ALLOWED_TEXT and nothing_happened(rig)
    assert not aa._plain_delay({}) and not aa._plain_delay({"seconds": None})


@pytest.mark.parametrize("zero", [0, 0.0])
def test_a_zero_delay_means_no_wait_and_registers_without_a_delay_step(zero):
    rig = Rig()
    rig.schedule([{**effect_step(), "delay": zero}])
    assert len(rig.registered) == 1 and not any("delay" in step for step in rig.registered[0]["action"])
    assert aa._plain_delay(zero) and aa._plain_delay({"seconds": 0})


# ----- F147: the lights must be the ones the words call for ---------------------------------------------

BEDROOM_LAMP = "light.example_bedroom_lamp"


def two_room_house():
    states = house()
    states[BEDROOM_LAMP] = light(BEDROOM_LAMP)
    ROOMS_OF.update({PRISM: {"study"}, STRIP: {"study"}, GROUP: {"study"}, LAMP: {"study"}, BEDROOM_LAMP: {"bedroom"}})
    return states


def test_a_model_chosen_light_in_another_room_is_refused_now_and_scheduled():
    rig = Rig(two_room_house())
    assert rig.say("set the prism to candlelight", entity_id=BEDROOM_LAMP, data={"effect": "candlelight"}) == shc_no_device()
    assert rig.schedule([effect_step(entity=BEDROOM_LAMP)]) == shc_no_device()
    assert nothing_happened(rig)


def test_the_speakers_room_grounds_a_light_when_no_room_is_named():
    rig = Rig(two_room_house())
    rig.say("set the lights to candlelight", entity_id=PRISM, data={"effect": "candlelight"})
    assert rig.writes == [("light", "turn_on", {"entity_id": [PRISM], "effect": "candlelight"})]


def test_a_room_the_words_name_grounds_its_lights_and_not_the_speakers():
    rig = Rig(two_room_house())
    rig.say("set the bedroom lights to candlelight", entity_id=BEDROOM_LAMP, data={"effect": "candlelight"})
    assert [w[2]["entity_id"] for w in rig.writes] == [[BEDROOM_LAMP]]
    rig2 = Rig(two_room_house())
    assert rig2.say("set the bedroom lights to candlelight", entity_id=PRISM, data={"effect": "candlelight"}) == shc_no_device()
    assert nothing_happened(rig2)


def test_a_named_light_is_grounded_wherever_it_is():
    states = two_room_house()
    states[BEDROOM_LAMP]["attributes"]["friendly_name"] = "Nightstand Hexagon"
    rig = Rig(states)
    rig.say("set the nightstand hexagon to candlelight", entity_id=BEDROOM_LAMP, data={"effect": "candlelight"})
    assert [w[2]["entity_id"] for w in rig.writes] == [[BEDROOM_LAMP]]
    scheduled = Rig(states)
    scheduled.schedule([effect_step(entity=BEDROOM_LAMP)], query="at 8pm set the nightstand hexagon to candlelight")
    assert len(scheduled.registered) == 1


def test_one_ungrounded_leaf_refuses_the_whole_call():
    states = two_room_house()
    rig = Rig(states)
    assert rig.say("set the prism to candlelight", entity_id=f"{PRISM}, {BEDROOM_LAMP}", data={"effect": "candlelight"}) == shc_no_device()
    assert nothing_happened(rig)


def test_a_group_whose_leaves_are_in_another_room_is_refused():
    states = two_room_house()
    ROOMS_OF.update({PRISM: {"bedroom"}, STRIP: {"bedroom"}})
    rig = Rig(states)
    assert rig.say("set the study lights to candlelight", entity_id=GROUP, data={"effect": "candlelight"}) == shc_no_device()
    assert nothing_happened(rig)


def test_a_clear_is_grounded_too():
    states = two_room_house()
    states[BEDROOM_LAMP] = light(BEDROOM_LAMP, effect="candlelight")
    rig = Rig(states)
    assert rig.say("turn off the effect", entity_id=BEDROOM_LAMP, data={"effect": "off"}) == shc_no_device()
    assert nothing_happened(rig)


def test_a_light_call_without_an_effect_keeps_its_old_exemption():
    """Pre-existing and out of scope: ordinary light calls are not grounded here."""
    rig = Rig(two_room_house())
    rig.say("turn on the prism", entity_id=BEDROOM_LAMP, data={"brightness": 50})
    assert rig.writes and rig.writes[0][2]["entity_id"] == BEDROOM_LAMP


def test_the_registry_and_configured_rooms_decide_which_room_a_light_is_in(monkeypatch):
    monkeypatch.undo()  # the autouse stub off: the real lookup
    from orchestrator import smart_home_controller as shc
    snapshot = MagicMock()
    snapshot.area_for = lambda leaf: type("Area", (), {"name": "Nightstand Room"})() if leaf == PRISM else None
    monkeypatch.setattr(shc.SmartHomeController, "_registry_snapshot", staticmethod(lambda: snapshot))
    monkeypatch.setattr(shc, "_room_lights_value", lambda: type("V", (), {"mapping": {"kitchen": (LAMP,)}})())
    agent = AutomationAgent(MagicMock(), MagicMock())
    assert agent._leaf_room_keys(PRISM) == {"nightstand_room"}
    assert agent._leaf_room_keys(LAMP) == {"kitchen"}
    assert agent._leaf_room_keys(STRIP) == set()


def shc_no_device():
    from orchestrator.smart_home_controller import NO_DEVICE_TEXT
    return NO_DEVICE_TEXT


# ----- F148: no template a non-owner can carry, in any part of the built config -------------------------

TEMPLATE_TRIGGER = {"platform": "template", "value_template": "{{ states('sensor.secret') | float > 5 }}"}


def test_a_guest_template_trigger_beside_a_valid_effect_action_is_refused():
    rig = Rig()
    reply = rig.schedule([effect_step()], perms=GUEST_ALL_LIGHTS, trigger={}, triggers=[TEMPLATE_TRIGGER])
    assert reply == aa.TEMPLATE_REFUSED_TEXT and nothing_happened(rig)


def test_the_same_config_registers_for_the_owner():
    rig = Rig()
    reply = rig.schedule([effect_step()], trigger={}, triggers=[TEMPLATE_TRIGGER])
    assert reply.startswith("Created automation") and rig.registered[0]["trigger"] == [TEMPLATE_TRIGGER]


@pytest.mark.parametrize("extra", [
    {"conditions": [{"type": "state", "entity_id": "sensor.x", "state": "{{ states('sensor.y') }}"}]},
    {"triggers": [{"platform": "state", "entity_id": "sensor.x", "to": "{{ 'on' }}"}], "trigger": {}},
    {"triggers": [{"platform": "numeric_state", "entity_id": "sensor.x", "value_template": "{{ 1 }}"}], "trigger": {}},
], ids=["condition", "state trigger", "numeric trigger"])
def test_a_guest_template_anywhere_in_the_built_config_is_refused(extra):
    rig = Rig()
    assert rig.schedule([effect_step()], perms=GUEST_ALL_LIGHTS, **extra) == aa.TEMPLATE_REFUSED_TEXT
    assert nothing_happened(rig)


def test_a_guest_notification_text_stays_the_one_exception():
    rig = Rig()
    step = {"service": "notify.mobile", "data": {"message": "lights are on"}}
    reply = rig.schedule([effect_step(), step], perms=GUEST_ALL_LIGHTS)
    assert reply.startswith("Created automation")


GUEST_ALL_LIGHTS = {**mp.apply_guest_baseline({"mode": "guest"})}


def test_the_effects_own_words_do_not_name_a_light():
    """A light that happens to be called "Twinkle" is not named by asking for the twinkle effect."""
    states = two_room_house()
    states[BEDROOM_LAMP]["attributes"]["friendly_name"] = "Twinkle"
    rig = Rig(states)
    assert rig.say("set the study lights to twinkle", entity_id=BEDROOM_LAMP, data={"effect": "twinkle"}) == shc_no_device()
    assert nothing_happened(rig)


def test_a_call_with_no_words_is_never_grounded():
    rig = Rig(two_room_house())
    leaves = [PRISM]
    assert asyncio.run(rig.agent._effect_ungrounded(leaves, rig.manager.states, "candlelight", {})) == shc_no_device()
    assert asyncio.run(rig.agent._effect_ungrounded(leaves, rig.manager.states, "candlelight", {"query": 5})) == shc_no_device()
