"""The household settings cards (admin/frontend/household-settings.js).

Structure is read from the files (delivery path, mounts, no inline handlers).
Behaviour runs the script in Node over a small fake DOM that throws if anything
assigns innerHTML (every value is data): the three list states, Clear, the dirty
marker and leave guard, 409 draft recovery, read-only and owner-restricted
rendering, Retry, validation and focus.
"""
from __future__ import annotations

import json
import re

import pytest

from .frontend_node import FRONTEND, REPO, run_scripts

SCRIPT = FRONTEND / "household-settings.js"
INDEX = FRONTEND / "index.html"
DOCKERFILE = FRONTEND / "Dockerfile"


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def source():
    return SCRIPT.read_text(encoding="utf-8")


# --- structure ----------------------------------------------------------------------------------

def test_the_script_is_copied_tagged_before_escape_html_and_versioned(html):
    assert "COPY household-settings.js /usr/share/nginx/html/" in DOCKERFILE.read_text(encoding="utf-8")
    tag = re.search(r'<script src="/household-settings\.js\?v=([^"]+)"></script>', html)
    assert tag, "household-settings.js is not tagged with a ?v="
    assert tag.start() < html.index('<script src="/escape-html.js')
    assert html.index('<script src="/utils.js') < tag.start() and html.index('<script src="/status-vocab.js') < tag.start()


@pytest.mark.parametrize("name", ["app.js", "gateway-settings.js", "sms.js", "directions-settings.js", "memory-context.js"])
def test_every_edited_page_script_carries_the_new_buster(html, name):
    assert re.search(rf'/{re.escape(name)}\?v=20261015-(hs[0-9]?|lg4)"', html)


def test_the_script_never_assigns_markup_or_writes_handlers(source):
    for needle in (".innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function"):
        assert needle not in source, needle
    assert not re.search(r"\bon(click|input|change|blur|submit)\s*=", source)
    assert not re.search(r"setAttribute\(\s*['\"]on", source)


def test_headings_carry_no_emoji(source):
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", source)


def _between(html, start_marker, end_marker):
    start = html.index(start_marker)
    return html[start: html.index(end_marker, start)]


def test_card_containers_sit_outside_the_containers_the_pages_reload(html):
    gateway = _between(html, 'id="tab-gateway-settings"', "<!-- Conversations Tab")
    assert gateway.index('id="household-card-gateway"') < gateway.index('id="gateway-settings-container"')
    assert 'id="household-card-gateway"' not in _between(gateway, 'id="gateway-settings-container"', "</div>\n            </div>")
    sms = _between(html, 'id="tab-sms"', "<!-- SMS Settings Section -->")
    assert 'id="household-card-sms"' in sms
    assert html.index('id="household-card-sms"') < html.index('id="sms-settings-container"')
    directions = html[html.index('id="tab-directions-settings"'):]
    for card in ("amtrak", "transit", "community-events"):
        assert f'id="household-card-{card}"' in html
    assert re.search(r'<h2[^>]*>Local travel and events</h2>', html)
    assert directions.index('id="household-card-amtrak"') > directions.index("Loading origin placeholder settings...")


def test_devices_page_has_the_household_section_above_the_registry(html):
    devices = _between(html, 'id="tab-devices"', "<!-- Room Groups Tab -->")
    assert re.search(r'<h2[^>]*>Devices</h2>', devices)
    assert "Device Registry" not in devices
    assert re.search(r"<h2[^>]*>Home Assistant devices Athena uses</h2>", devices)
    ids = ["household-card-lights", "household-card-bed-warmer", "household-card-jarvis-web"]
    positions = [devices.index(f'id="{i}"') for i in ids]
    assert positions == sorted(positions) and positions[-1] < devices.index('id="devices-container"')


def test_sidebar_label_and_mount_calls(html):
    assert re.search(r"<span>Directions &amp; Local Area</span>", html)
    for name, section in (("gateway-settings.js", "gateway"), ("sms.js", "sms"), ("directions-settings.js", "amtrak")):
        text = (FRONTEND / name).read_text(encoding="utf-8")
        assert f"HouseholdSettings.mount('{section}'" in text, name
    sms = (FRONTEND / "sms.js").read_text(encoding="utf-8")
    load = sms[sms.index("async function loadSMSData() {"):]
    assert "HouseholdSettings.mount('sms'" in load[: load.index("loadSMSSettings()")]
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    case = app[app.index("case 'devices':"): app.index("case 'users':")]
    for section in ("lights", "bed_warmer", "jarvis_web"):
        assert f"'{section}'" in case


# --- behaviour in Node --------------------------------------------------------------------------

PRELUDE = r"""
'use strict';
class El {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {}; this._text = ''; this.className = ''; this.value = ''; this.parentElement = null; }
  get disabled() { return 'disabled' in this.attrs; }
  set disabled(v) { if (v) this.attrs.disabled = ''; else delete this.attrs.disabled; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { throw new Error('innerHTML assigned: ' + String(v).slice(0, 40)); }
  appendChild(c) { if (c && typeof c === 'object' && 'parentElement' in c) c.parentElement = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { this.fire('click'); }
  fire(t, ev) { (this.listeners[t] || []).slice().forEach(f => f(Object.assign({ key: '', preventDefault() {} }, ev || {}))); }
  focus() { global.__focused = this; }
  find(pred, out) { out = out || []; if (pred(this)) out.push(this); this.children.forEach(c => c.find && c.find(pred, out)); return out; }
}
class Text { constructor(t) { this.tagName = '#text'; this.data = String(t); this.children = []; } get textContent() { return this.data; } find() { return []; } }
global.__els = {};
global.document = {
  getElementById: id => global.__els[id] || null,
  createElement: tag => new El(tag),
  createTextNode: t => new Text(t),
};
global.__tab = new El('div'); __tab.className = 'tab-content'; __tab.attrs.id = 'tab-devices';
global.__box = id => { const c = new El('div'); c.attrs.id = id; __tab.appendChild(c); __els[id] = c; return c; };
global.__fetches = [];
global.__script = [];
global.fetch = async (url, opts) => {
  opts = opts || {};
  global.__fetches.push({ url: String(url), method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null, headers: opts.headers });
  const r = global.__script.length > 1 ? global.__script.shift() : global.__script[0];
  if (r instanceof Error) throw r;
  return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body };
};
global.__listeners = {};
global.addEventListener = (t, f) => { (global.__listeners[t] = global.__listeners[t] || []).push(f); };
global.__confirms = [];
global.__confirmAnswer = false;
global.confirm = msg => { global.__confirms.push(msg); return global.__confirmAnswer; };
global.__shown = [];
global.showTab = function (name) { global.__shown.push(name); return 'shown'; };
global.__perms = ['read', 'write', 'manage_infrastructure'];
global.hasPermission = p => global.__perms.includes(p);
global.API_BASE = '';
global.authToken = 'test-token';
global.console.error = () => {};

global.__entry = (value, stored, extra) => Object.assign({ value, stored: stored || {}, revision: 3, required_permission: 'write', writable: true }, extra || {});
global.__ok = (section, entry) => ({ status: 200, body: { [section]: entry } });
global.__put = entry => ({ status: 200, body: entry });
global.__settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0)); };
global.__visible = e => { for (let n = e; n; n = n.parentElement) { if (/(^|\s)hidden(\s|$)/.test(n.className)) return false; } return true; };
global.__inputs = root => root.find(e => e.tagName === 'INPUT');
global.__buttons = (root, text) => root.find(e => e.tagName === 'BUTTON' && (text === undefined || e.textContent === text));
global.__labelled = (root, text) => {
  const label = root.find(e => e.tagName === 'LABEL' && e.textContent === text)[0];
  if (!label) throw new Error('no label ' + text);
  return root.find(e => e.attrs.id === label.attrs.for)[0];
};
global.__type = (input, v) => { input.value = v; input.fire('input'); };
global.__saveButton = root => root.find(e => e.tagName === 'BUTTON' && (e.textContent === 'Save' || e.textContent === 'Saving…'))[0];
global.__text = root => root.textContent;
global.__alerts = root => root.find(e => e.attrs.role === 'alert' && __visible(e) && e.textContent !== '').map(e => e.textContent);
"""


def scenario(body: str):
    return run_scripts([SCRIPT], f"(async () => {{ {body} }})()", prelude=PRELUDE)


def test_a_value_that_looks_like_markup_stays_text_and_a_422_path_is_never_echoed():
    result = scenario(
        r"""
        const evil = '<img src=x onerror=alert(1)>';
        const box = __box('c1');
        __script = [__ok('jarvis_web', __entry({default_room: evil, climate_entity: 'climate.<script>', min_temp: null, max_temp: null}, {default_room: true, climate_entity: true}))];
        await HouseholdSettings.mount('jarvis_web', 'c1');
        const room = __labelled(box, 'Default room');
        const climate = __labelled(box, 'Thermostat');
        const tags = box.find(e => e.tagName === 'IMG' || e.tagName === 'SCRIPT').length;
        const roomValue = room.value, climateValue = climate.value;
        __type(room, 'Den');
        __type(climate, 'climate.den');
        __script = [{status: 422, body: {detail: [{loc: [evil, 0], type: 'bad_room'}, {loc: ['oven_entity'], type: 'bad_entity_id'}]}}];
        __saveButton(box).click();
        await __settle();
        return {roomValue, climateValue, tags, alerts: __alerts(box), echoed: __text(box).includes('onerror')};
        """
    )
    assert result["roomValue"] == "<img src=x onerror=alert(1)>"
    assert result["climateValue"] == "climate.<script>"
    assert result["tags"] == 0
    assert any("entity id like water_heater.oven" in a for a in result["alerts"])
    assert result["echoed"] is False


def test_the_three_list_states_and_what_each_sends():
    result = scenario(
        r"""
        const box = __box('c1');
        const lights = (list, stored) => __ok('lights', __entry({room_light_exclude: list}, {room_light_exclude: stored}, {required_permission: 'manage_infrastructure'}));
        const lightsPut = (list, stored) => __put(__entry({room_light_exclude: list}, {room_light_exclude: stored}, {required_permission: 'manage_infrastructure'}));
        const states = [];
        __script = [lights(null, false)];
        await HouseholdSettings.mount('lights', 'c1');
        states.push({unset: __text(box).includes("Not set here. Athena uses the server's setting."), setButton: __buttons(box, 'Set a list here').length});
        __buttons(box, 'Set a list here')[0].click();
        states.push({rows: __inputs(box).length});
        const input = __inputs(box)[0];
        __type(input, 'light.garage_strip');
        __script = [lightsPut(['light.garage_strip'], true)];
        __saveButton(box).click();
        await __settle();
        const sent1 = __fetches[__fetches.length - 1].body.value.room_light_exclude;
        __buttons(box, 'Remove')[0].click();
        states.push({none: __text(box).includes('None. Only the built-in rule applies.'), useServer: __buttons(box, "Use the server's setting").length});
        __script = [lightsPut([], true)];
        __saveButton(box).click();
        await __settle();
        const sent2 = __fetches[__fetches.length - 1].body.value.room_light_exclude;
        __buttons(box, "Use the server's setting")[0].click();
        __script = [lightsPut(null, false)];
        __saveButton(box).click();
        await __settle();
        const sent3 = __fetches[__fetches.length - 1].body.value.room_light_exclude;
        return {states, sent1, sent2, sent3, help: __text(box).includes('led_ring or status_led')};
        """
    )
    assert result["states"][0] == {"unset": True, "setButton": 1}
    assert result["states"][1]["rows"] == 1
    assert result["states"][2] == {"none": True, "useServer": 1}
    assert result["sent1"] == ["light.garage_strip"]
    assert result["sent2"] == []
    assert result["sent3"] is None
    assert result["help"] is True


def test_clear_stores_null_and_only_shows_for_a_value():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        const input = __labelled(box, 'Station code');
        const clear = __buttons(box, 'Clear')[0];
        const visibleBefore = __visible(clear);
        const label = clear.attrs['aria-label'];
        clear.click();
        const cleared = {value: input.value, visible: __visible(clear), dirty: __visible(box.find(e => e.textContent === 'Unsaved changes')[0])};
        __script = [__put(__entry({default_station: null}, {default_station: false}))];
        __saveButton(box).click();
        await __settle();
        return {visibleBefore, label, cleared, sent: __fetches[__fetches.length - 1].body, text: __text(box)};
        """
    )
    assert result["visibleBefore"] is True
    assert result["label"] == "Clear Station code, use the server setting"
    assert result["cleared"] == {"value": "", "visible": False, "dirty": True}
    assert result["sent"] == {"value": {"default_station": None}, "revision": 3}
    assert "Saved. Changes reach the assistant within a minute." in result["text"]


def test_dirty_marker_refresh_keeps_edits_one_binding_and_the_tab_guard():
    result = scenario(
        r"""
        const box = __box('c1');
        const other = __box('c2');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        const marker = () => box.find(e => e.textContent === 'Unsaved changes')[0];
        const save = __saveButton(box);
        const clean = {marker: __visible(marker()), saveDisabled: save.disabled};
        const input = __labelled(box, 'Station code');
        __type(input, 'bos');
        const dirty = {marker: __visible(marker()), saveDisabled: save.disabled};
        const fetchesBefore = __fetches.length;
        await HouseholdSettings.mount('amtrak', 'c1');
        await HouseholdSettings.mount('amtrak', 'c1');
        const refresh = {fetched: __fetches.length - fetchesBefore, value: input.value, sameInput: __labelled(box, 'Station code') === input, saveListeners: save.listeners.click.length};
        // a second card on the same page, dirty too
        __script = [__ok('gateway', __entry({fallback_model: null}, {fallback_model: false}))];
        await HouseholdSettings.mount('gateway', 'c2');
        __type(__labelled(other, 'Fallback model'), 'gpt-x');
        __confirmAnswer = false;
        const stayed = showTab('dashboard');
        const stayedState = {result: stayed === undefined, shown: __shown.slice(), message: __confirms[0], value: input.value};
        __confirmAnswer = true;
        const left = showTab('dashboard');
        const leftState = {result: left, shown: __shown.slice(), value: input.value, marker: __visible(marker())};
        const sameTab = (() => { __type(input, 'bos'); __confirms.length = 0; showTab('devices'); return __confirms.length; })();
        const beforeunload = {prevented: false};
        __listeners.beforeunload.forEach(f => f({preventDefault() { beforeunload.prevented = true; }}));
        return {clean, dirty, refresh, stayedState, leftState, sameTab, beforeunload};
        """
    )
    assert result["clean"] == {"marker": False, "saveDisabled": True}
    assert result["dirty"] == {"marker": True, "saveDisabled": False}
    assert result["refresh"] == {"fetched": 0, "value": "bos", "sameInput": True, "saveListeners": 1}
    assert result["stayedState"]["result"] is True and result["stayedState"]["shown"] == []
    assert result["stayedState"]["message"] == (
        "You have unsaved changes to Amtrak station and Gateway fallback model. Leave without saving?"
    )
    assert result["stayedState"]["value"] == "bos"
    assert result["leftState"]["result"] == "shown" and result["leftState"]["shown"] == ["dashboard"]
    assert result["leftState"]["value"] == "NYP" and result["leftState"]["marker"] is False
    assert result["sameTab"] == 0
    assert result["beforeunload"]["prevented"] is True


def test_a_clean_card_refetches_when_mounted_again():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        __script = [__ok('amtrak', __entry({default_station: 'BOS'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        return {value: __labelled(box, 'Station code').value, gets: __fetches.filter(f => f.method === 'GET').length, url: __fetches[0].url, auth: __fetches[0].headers.Authorization};
        """
    )
    assert result["value"] == "BOS" and result["gets"] == 2
    assert result["url"] == "/api/settings/household?sections=amtrak"
    assert result["auth"] == "Bearer test-token"


def test_a_409_keeps_the_draft_and_reapplying_it_sends_the_new_revision():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        const input = __labelled(box, 'Station code');
        __type(input, 'bos');
        const latest = __ok('amtrak', __entry({default_station: 'PHL'}, {default_station: true}, {revision: 4}));
        __script = [{status: 409, body: {detail: 'stale_revision'}}, latest];
        __saveButton(box).click();
        await __settle();
        const afterConflict = {value: __labelled(box, 'Station code').value, alerts: __alerts(box), reapply: __buttons(box, 'Re-apply my changes').length, dirty: __visible(box.find(e => e.textContent === 'Unsaved changes')[0])};
        __buttons(box, 'Re-apply my changes')[0].click();
        const afterReapply = {value: __labelled(box, 'Station code').value, dirty: __visible(box.find(e => e.textContent === 'Unsaved changes')[0]), banner: __alerts(box)};
        __script = [__put(__entry({default_station: 'BOS'}, {default_station: true}, {revision: 5}))];
        __saveButton(box).click();
        await __settle();
        const sent = __fetches[__fetches.length - 1];
        return {afterConflict, afterReapply, sent: sent.body, method: sent.method, url: sent.url, text: __text(box)};
        """
    )
    conflict = result["afterConflict"]
    assert conflict["value"] == "PHL" and conflict["dirty"] is False and conflict["reapply"] == 1
    assert conflict["alerts"] == [
        "Someone else saved Amtrak station while you were editing. Your changes weren't saved. "
        "The latest version is shown below."
    ]
    assert result["afterReapply"]["value"] == "BOS" and result["afterReapply"]["dirty"] is True
    assert result["afterReapply"]["banner"] == []
    assert result["sent"] == {"value": {"default_station": "BOS"}, "revision": 4}
    assert result["method"] == "PUT" and result["url"] == "/api/settings/household/amtrak"
    assert "Saved." in result["text"]


def test_owner_only_section_is_read_only_for_a_non_owner_and_editable_for_the_owner():
    result = scenario(
        r"""
        const bed = __entry({level_left: 'select.l', level_right: null, power_main: null, power_side_a: null, power_side_b: null}, {level_left: true}, {required_permission: 'manage_infrastructure'});
        __perms = ['read', 'write'];
        let box = __box('c1');
        __script = [__ok('bed_warmer', bed)];
        await HouseholdSettings.mount('bed_warmer', 'c1');
        const operator = {inputsDisabled: __inputs(box).every(i => i.disabled), note: __text(box).includes('Only the house owner can change these settings.'), saveHidden: !__visible(__saveButton(box)), saveDisabled: __saveButton(box).disabled, clears: __buttons(box, 'Clear').every(b => !__visible(b) || b.disabled)};
        __perms = ['read', 'write', 'manage_infrastructure'];
        box = __box('c2');
        __script = [__ok('bed_warmer', bed)];
        await HouseholdSettings.mount('bed_warmer', 'c2');
        const owner = {inputsDisabled: __inputs(box).some(i => i.disabled), note: __text(box).includes('Only the house owner'), saveHidden: !__visible(__saveButton(box)), legends: box.find(e => e.tagName === 'LEGEND').map(e => e.textContent)};
        __perms = ['read'];
        box = __box('c3');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c3');
        const viewer = {disabled: __inputs(box).every(i => i.disabled), note: __text(box).includes('You can view these settings. Changing them needs write access.'), saveHidden: !__visible(__saveButton(box))};
        return {operator, owner, viewer};
        """
    )
    assert result["operator"] == {
        "inputsDisabled": True, "note": True, "saveHidden": True, "saveDisabled": True, "clears": True,
    }
    assert result["owner"]["inputsDisabled"] is False and result["owner"]["note"] is False
    assert result["owner"]["saveHidden"] is False
    assert result["owner"]["legends"] == ["Left side", "Right side", "Power"]
    assert result["viewer"] == {"disabled": True, "note": True, "saveHidden": True}


def test_a_restricted_response_shows_set_by_the_owner_or_not_set_and_no_values_or_inputs():
    result = scenario(
        r"""
        __perms = ['read', 'write'];
        const box = __box('c1');
        const restricted = {value: null, stored: {level_left: true, level_right: false, power_main: true, power_side_a: false, power_side_b: false}, revision: 0, required_permission: 'manage_infrastructure', writable: true, restricted: true};
        __script = [__ok('bed_warmer', restricted)];
        await HouseholdSettings.mount('bed_warmer', 'c1');
        const items = box.find(e => e.tagName === 'LI').map(e => e.textContent);
        const jarvis = __box('c2');
        __script = [__ok('jarvis_web', {value: null, stored: {min_temp: true, max_temp: true, default_room: false}, revision: 0, required_permission: 'manage_infrastructure', writable: true, restricted: true})];
        await HouseholdSettings.mount('jarvis_web', 'c2');
        const jarvisItems = jarvis.find(e => e.tagName === 'LI').map(e => e.textContent);
        return {items, inputs: __inputs(box).length + __inputs(jarvis).length, buttons: __buttons(box).filter(__visible).map(b => b.textContent), saveHidden: !__visible(__saveButton(box)), note: __text(box).includes('Only the house owner can see and change these settings.'), jarvisItems};
        """
    )
    assert result["items"] == [
        "Left heat level: Set by the owner",
        "Right heat level: Not set here",
        "Main power: Set by the owner",
        "Side A power: Not set here",
        "Side B power: Not set here",
    ]
    assert "Default room: Not set here" in result["jarvisItems"]
    assert "Thermostat range: Set by the owner" in result["jarvisItems"]
    assert result["inputs"] == 0
    assert result["buttons"] == [] and result["saveHidden"] is True, "Save is not shown when it cannot be used"
    assert result["note"] is True


def test_load_failure_shows_retry_and_a_forbidden_read_says_so():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [{status: 500, body: {}}];
        await HouseholdSettings.mount('amtrak', 'c1');
        const failed = {text: __text(box), alerts: __alerts(box), retry: __buttons(box, 'Retry').length, save: __saveButton(box).disabled};
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        __buttons(box, 'Retry')[0].click();
        await __settle();
        const recovered = {value: __labelled(box, 'Station code').value, retry: __buttons(box, 'Retry').length};
        const other = __box('c2');
        __script = [{status: 403, body: {}}];
        await HouseholdSettings.mount('gateway', 'c2');
        return {failed, recovered, forbidden: __text(other)};
        """
    )
    assert result["failed"]["alerts"] == ["Couldn't load Amtrak station."]
    assert result["failed"]["retry"] == 1 and result["failed"]["save"] is True
    assert result["recovered"] == {"value": "NYP", "retry": 0}
    assert "You don't have permission to view this setting" in result["forbidden"]


def test_a_failed_save_names_the_problem_and_moves_focus_to_the_first_invalid_field():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('bed_warmer', __entry({level_left: null, level_right: null, power_main: null, power_side_a: null, power_side_b: null}, {}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('bed_warmer', 'c1');
        __type(__labelled(box, 'Left heat level'), 'select.bed_left');
        __type(__labelled(box, 'Main power'), 'switch.front_door_lock');
        __type(__labelled(box, 'Side A power'), 'light.bed');
        const warned = __text(box).includes('The bed warmer needs all five.');
        const fetchesBefore = __fetches.length;
        __saveButton(box).click();
        await __settle();
        const main = __labelled(box, 'Main power');
        const sideA = __labelled(box, 'Side A power');
        const describedBy = main.attrs['aria-describedby'].split(' ');
        const errorEl = box.find(e => describedBy.includes(e.attrs.id) && e.attrs.role === 'alert')[0];
        return {
          sent: __fetches.length - fetchesBefore,
          focused: __focused === main,
          invalid: [main.attrs['aria-invalid'], sideA.attrs['aria-invalid'], __labelled(box, 'Left heat level').attrs['aria-invalid']],
          errorText: errorEl.textContent,
          sideAText: box.find(e => e.attrs.id === sideA.attrs['aria-describedby'].split(' ')[1])[0].textContent,
          warned,
        };
        """
    )
    assert result["sent"] == 0
    assert result["focused"] is True
    assert result["invalid"] == ["true", "true", "false"]
    assert "looks like a door, lock, valve" in result["errorText"]
    assert result["sideAText"] == "Use a switch. entity id, like switch.bed_power."
    assert result["warned"] is True


def test_a_server_422_is_mapped_to_the_field_and_focus_follows():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: null}, {}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        const input = __labelled(box, 'Station code');
        __type(input, 'xyz');
        __script = [{status: 422, body: {detail: [{loc: ['default_station'], type: 'bad_station'}]}}];
        __saveButton(box).click();
        await __settle();
        return {focused: __focused === input, invalid: input.attrs['aria-invalid'], alerts: __alerts(box), stillDirty: !__saveButton(box).disabled};
        """
    )
    assert result["focused"] is True and result["invalid"] == "true"
    assert "Use the 3-letter Amtrak code." in result["alerts"]
    assert result["stillDirty"] is True


def test_temperature_pair_must_be_complete_and_ordered_and_clamped():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('jarvis_web', __entry({default_room: null, climate_entity: null, min_temp: null, max_temp: null, oven_entity: null, fridge_entity: null, freezer_entity: null, stove_cook_mode_sensor: null, stove_display_temp_sensor: null, stove_timer_sensor: null, fridge_door_sensor: null}, {}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('jarvis_web', 'c1');
        const low = __labelled(box, 'Lowest temperature (°F)');
        const high = __labelled(box, 'Highest temperature (°F)');
        const trial = async (a, b) => { __type(low, a); __type(high, b); __saveButton(box).click(); await __settle(); return __alerts(box); };
        const one = await trial('70', '');
        const order = await trial('80', '70');
        const range = await trial('30', '70');
        __script = [__ok('jarvis_web', __entry({min_temp: 68, max_temp: 74}, {min_temp: true, max_temp: true}, {required_permission: 'manage_infrastructure'}))];
        const before = __fetches.length;
        await trial('68', '74');
        const sent = __fetches.length > before ? __fetches[__fetches.length - 1].body.value : null;
        return {one, order, range, sentTemps: sent && [sent.min_temp, sent.max_temp], sentRoom: sent && sent.default_room, keys: sent && Object.keys(sent).length, hint: __text(box).includes('65–75°F'), sensors: box.find(e => e.tagName === 'SUMMARY').map(e => e.textContent)};
        """
    )
    assert any("Fill in both temperatures" in a for a in result["one"])
    assert any("Lowest must be 40–95°F" in a for a in result["order"] + result["range"])
    assert result["sentTemps"] == [68, 74] and result["sentRoom"] is None and result["keys"] == 11
    assert result["hint"] is True and result["sensors"] == ["Appliance sensors"]


def test_lamp_patterns_are_checked_and_remove_moves_focus():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('lights', __entry({room_light_exclude: ['light.a', 'light.b', 'light.c']}, {room_light_exclude: true}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('lights', 'c1');
        const removes = () => __buttons(box, 'Remove');
        const labels = removes().map(b => b.attrs['aria-label']);
        removes()[1].click();
        const afterMiddle = __focused === removes()[1];
        removes()[1].click();
        const afterLast = __focused === removes()[0];
        const live = box.find(e => e.attrs.role === 'status' && e.attrs['aria-live'] === 'polite')[0] || null;
        removes()[0].click();
        const afterAll = __focused.textContent;
        __buttons(box, 'Add pattern')[0].click();
        const input = __inputs(box)[0];
        const probe = v => { __type(input, v); input.fire('blur'); return box.find(e => e.attrs.role === 'alert' && __visible(e) && e.textContent).map(e => e.textContent)[0] || ''; };
        return {labels, afterMiddle, afterLast, afterAll, wide: probe('light.*'), bad: probe('switch.x'), many: probe('light.a*b*c*d*'), good: probe('light.garage_*'), added: box.textContent.includes('Pattern 1')};
        """
    )
    assert result["labels"] == ["Remove pattern 1: light.a", "Remove pattern 2: light.b", "Remove pattern 3: light.c"]
    assert result["afterMiddle"] is True and result["afterLast"] is True
    assert result["afterAll"] in ("Add pattern", "Use the server's setting")
    assert "can't start with *" in result["wide"]
    assert result["bad"].startswith("Start with light.")
    assert "at most 3 *" in result["many"]
    assert result["good"] == ""


def test_country_code_and_station_are_normalised_as_typed():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('sms', __entry({default_country_code: null}, {}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('sms', 'c1');
        const input = __labelled(box, 'Country code');
        __type(input, '+44');
        input.fire('blur');
        const stripped = input.value;
        __type(input, '0');
        input.fire('blur');
        const zero = __alerts(box);
        return {stripped, zero, mode: input.attrs.inputmode};
        """
    )
    assert result["stripped"] == "44"
    assert result["zero"] == ["Enter 1–3 digits, e.g. 1 or 44."]
    assert result["mode"] == "numeric"


def test_extension_points_accept_a_new_kind_and_card():
    result = scenario(
        r"""
        HouseholdSettings.registerKind('shout', (field, ctx) => {
          const el = document.createElement('div');
          let v = null;
          return { el, fields: [field.name], get: () => ({ [field.name]: v }), set: value => { v = value ? value[field.name] : null; }, validate: () => true, showServerError() {}, hasError: () => false, focus() {}, setDisabled() {}, refresh() {} };
        });
        HouseholdSettings.defineCard('gateway', { title: 'Replaced', blurb: 'b', fields: [{ name: 'fallback_model', kind: 'shout', label: 'x' }] });
        const box = __box('c1');
        __script = [__ok('gateway', __entry({fallback_model: 'm'}, {fallback_model: true}))];
        await HouseholdSettings.mount('gateway', 'c1');
        return {title: box.find(e => e.tagName === 'H3')[0].textContent};
        """
    )
    assert result["title"] == "Replaced"


# --- review round: focus, hidden Save, discard, hash, legend, copy ------------------------------

STATUS = "(box => box.find(e => e.className === 'sr-only')[0].textContent)"


def test_focus_goes_to_the_heading_after_a_save_and_to_reapply_after_a_conflict():
    result = scenario(
        rf"""
        const box = __box('c1');
        const status = {STATUS};
        __script = [__ok('amtrak', __entry({{default_station: 'NYP'}}, {{default_station: true}}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        __type(__labelled(box, 'Station code'), 'bos');
        __script = [__put(__entry({{default_station: 'BOS'}}, {{default_station: true}}, {{revision: 4}}))];
        __saveButton(box).click();
        await __settle();
        const afterSave = {{id: __focused.attrs.id, tag: __focused.tagName}};
        __type(__labelled(box, 'Station code'), 'phl');
        __script = [{{status: 409, body: {{detail: 'stale_revision'}}}}, __ok('amtrak', __entry({{default_station: 'NYP'}}, {{default_station: true}}, {{revision: 9}}))];
        __saveButton(box).click();
        await __settle();
        const afterConflict = {{text: __focused.textContent, visible: __visible(__focused)}};
        return {{afterSave, afterConflict}};
        """
    )
    assert result["afterSave"] == {"id": "hh-amtrak-heading", "tag": "H3"}
    assert result["afterConflict"] == {"text": "Re-apply my changes", "visible": True}


def test_save_is_not_shown_while_loading_failing_forbidden_restricted_or_read_only():
    result = scenario(
        r"""
        const seen = {};
        __perms = ['read'];
        let box = __box('c1');
        __script = [{status: 500, body: {}}];
        await HouseholdSettings.mount('amtrak', 'c1');
        seen.error = __visible(__saveButton(box));
        box = __box('c2');
        __script = [{status: 403, body: {}}];
        await HouseholdSettings.mount('gateway', 'c2');
        seen.forbidden = __visible(__saveButton(box));
        seen.forbiddenCopy = __text(box).includes("You don't have permission to view this setting.");
        box = __box('c3');
        __script = [__ok('sms', __entry({default_country_code: '44'}, {default_country_code: true}))];
        await HouseholdSettings.mount('sms', 'c3');
        seen.readOnly = __visible(__saveButton(box));
        seen.noAriaHidden = !('aria-hidden' in __saveButton(box).attrs);
        __perms = ['read', 'write', 'manage_infrastructure'];
        box = __box('c4');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c4');
        seen.editable = __visible(__saveButton(box));
        return seen;
        """
    )
    assert result == {
        "error": False, "forbidden": False, "forbiddenCopy": True, "readOnly": False, "noAriaHidden": True, "editable": True,
    }


def test_discard_changes_restores_the_saved_values_and_moves_focus_to_the_heading():
    result = scenario(
        rf"""
        const box = __box('c1');
        const status = {STATUS};
        __script = [__ok('amtrak', __entry({{default_station: 'NYP'}}, {{default_station: true}}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        const discard = () => __buttons(box, 'Discard changes')[0];
        const clean = __visible(discard());
        const input = __labelled(box, 'Station code');
        __type(input, 'bos');
        const dirty = __visible(discard());
        const fetchesBefore = __fetches.length;
        discard().click();
        return {{
          clean, dirty, value: input.value, after: __visible(discard()), marker: __visible(box.find(e => e.textContent === 'Unsaved changes')[0]),
          focus: __focused.attrs.id, announced: status(box), fetched: __fetches.length - fetchesBefore,
        }};
        """
    )
    assert result == {
        "clean": False, "dirty": True, "value": "NYP", "after": False, "marker": False,
        "focus": "hh-amtrak-heading", "announced": "Changes discarded.", "fetched": 0,
    }


def test_a_cancelled_leave_puts_the_url_back_on_the_tab_that_is_still_showing():
    result = scenario(
        r"""
        const box = __box('c1');
        const urls = [];
        global.history = { replaceState(a, b, url) { urls.push(url); } };
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        __type(__labelled(box, 'Station code'), 'bos');
        __confirmAnswer = false;
        showTab('dashboard');
        const cancelled = urls.slice();
        __confirmAnswer = true;
        showTab('dashboard');
        return {cancelled, afterLeaving: urls.slice(cancelled.length)};
        """
    )
    assert result == {"cancelled": ["#devices"], "afterLeaving": []}


def test_the_legend_shows_only_where_a_server_setting_placeholder_exists_and_a_card_can_override_it():
    result = scenario(
        r"""
        const legend = box => __text(box).includes('Fields marked "Server setting"');
        const out = {};
        let box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: null}, {default_station: false}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        out.text = legend(box);
        box = __box('c2');
        __script = [__ok('lights', __entry({room_light_exclude: null}, {room_light_exclude: false}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('lights', 'c2');
        out.listOnly = legend(box);
        const field = {name: 'fallback_model', kind: 'text', label: 'Model', maxlength: 20};
        HouseholdSettings.defineCard('gateway', {title: 'Custom', blurb: 'b', legend: 'Our own legend.', fields: [field]});
        box = __box('c3');
        __script = [__ok('gateway', __entry({fallback_model: null}, {fallback_model: false}))];
        await HouseholdSettings.mount('gateway', 'c3');
        out.override = {own: __text(box).includes('Our own legend.'), default: legend(box)};
        HouseholdSettings.defineCard('sms', {title: 'Quiet', blurb: 'b', legend: '', fields: [Object.assign({}, field, {name: 'default_country_code'})]});
        box = __box('c4');
        __script = [__ok('sms', __entry({default_country_code: null}, {default_country_code: false}))];
        await HouseholdSettings.mount('sms', 'c4');
        out.empty = legend(box);
        return out;
        """
    )
    assert result["text"] is True and result["listOnly"] is False
    assert result["override"] == {"own": True, "default": False}
    assert result["empty"] is False


def test_the_saved_line_goes_when_the_user_edits_again():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        const input = __labelled(box, 'Station code');
        __type(input, 'bos');
        __script = [__put(__entry({default_station: 'BOS'}, {default_station: true}, {revision: 4}))];
        __saveButton(box).click();
        await __settle();
        const saved = __text(box).includes('Saved.');
        __type(input, 'phl');
        return {saved, after: __text(box).includes('Saved.')};
        """
    )
    assert result == {"saved": True, "after": False}


def test_a_conflict_whose_reload_failed_offers_retry_not_a_dead_reapply_button():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('amtrak', __entry({default_station: 'NYP'}, {default_station: true}))];
        await HouseholdSettings.mount('amtrak', 'c1');
        __type(__labelled(box, 'Station code'), 'bos');
        __script = [{status: 409, body: {}}, {status: 500, body: {}}];
        __saveButton(box).click();
        await __settle();
        const reapply = () => __buttons(box, 'Re-apply my changes')[0];
        const failed = {reapplyVisible: __visible(reapply()), retry: __buttons(box, 'Retry').filter(__visible).length};
        __script = [__ok('amtrak', __entry({default_station: 'PHL'}, {default_station: true}, {revision: 4}))];
        __buttons(box, 'Retry')[0].click();
        await __settle();
        return {failed, recovered: __visible(reapply())};
        """
    )
    assert result == {"failed": {"reapplyVisible": False, "retry": 1}, "recovered": True}


def test_the_thermostat_range_is_a_labelled_group_and_short_codes_use_short_inputs():
    result = scenario(
        r"""
        let box = __box('c1');
        __script = [__ok('jarvis_web', __entry({min_temp: null, max_temp: null}, {}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('jarvis_web', 'c1');
        const group = box.find(e => e.tagName === 'FIELDSET' && e.find(x => x.tagName === 'LEGEND' && x.textContent === 'Thermostat range').length);
        const thermostat = __labelled(box, 'Thermostat');
        box = __box('c2');
        __script = [__ok('amtrak', __entry({default_station: null}, {}))];
        await HouseholdSettings.mount('amtrak', 'c2');
        const station = __labelled(box, 'Station code');
        return {group: group.length, thermostat: thermostat.className, station: station.className};
        """
    )
    assert result["group"] == 1
    assert "max-w-xl" in result["thermostat"] and "max-w-[8rem]" in result["station"]


def test_a_removed_pattern_label_follows_what_the_user_types():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('lights', __entry({room_light_exclude: ['light.a']}, {room_light_exclude: true}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('lights', 'c1');
        const remove = () => __buttons(box, 'Remove')[0];
        const before = remove().attrs['aria-label'];
        __type(__inputs(box)[0], 'light.garage_*');
        return {before, after: remove().attrs['aria-label'], secondary: remove().className.includes('bg-gray-700') && !remove().className.includes('bg-blue-600')};
        """
    )
    assert result == {"before": "Remove pattern 1: light.a", "after": "Remove pattern 1: light.garage_*", "secondary": True}


def test_changing_the_country_code_warns_about_guest_matching_only_while_dirty():
    result = scenario(
        rf"""
        const box = __box('c1');
        const status = {STATUS};
        __script = [__ok('sms', __entry({{default_country_code: '1'}}, {{default_country_code: true}}, {{required_permission: 'manage_infrastructure'}}))];
        await HouseholdSettings.mount('sms', 'c1');
        const note = () => box.find(e => e.tagName === 'P' && __visible(e) && e.textContent.includes("check that a current guest's texts are still recognised")).length === 1;
        const clean = note();
        const input = __labelled(box, 'Country code');
        __type(input, '44');
        const dirty = {{note: note(), announced: status(box).includes('guest')}};
        __type(input, '1');
        return {{clean, dirty, reverted: note()}};
        """
    )
    assert result == {"clean": False, "dirty": {"note": True, "announced": True}, "reverted": False}


def test_a_warning_present_at_load_is_not_announced_but_one_caused_by_an_edit_is():
    result = scenario(
        rf"""
        const box = __box('c1');
        const status = {STATUS};
        const bed = __entry({{level_left: 'select.l', level_right: null, power_main: null, power_side_a: null, power_side_b: null}}, {{level_left: true}}, {{required_permission: 'manage_infrastructure'}});
        __script = [__ok('bed_warmer', bed)];
        await HouseholdSettings.mount('bed_warmer', 'c1');
        const warning = () => box.find(e => e.textContent.startsWith('The bed warmer needs all five'))[0];
        const atLoad = {{shown: __visible(warning()), role: warning().attrs.role === undefined, announced: status(box)}};
        const empty = __box('c2');
        __script = [__ok('bed_warmer', __entry({{level_left: null, level_right: null, power_main: null, power_side_a: null, power_side_b: null}}, {{}}, {{required_permission: 'manage_infrastructure'}}))];
        await HouseholdSettings.mount('bed_warmer', 'c2');
        const before = status(empty);
        __type(__labelled(empty, 'Left heat level'), 'select.l');
        return {{atLoad, before, afterEdit: status(empty).startsWith('The bed warmer needs all five')}};
        """
    )
    assert result["atLoad"] == {"shown": True, "role": True, "announced": ""}
    assert result["before"] == "" and result["afterEdit"] is True
