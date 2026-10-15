"""The Rooms tab cards: Room lights and Speaker rooms (admin/frontend/room-settings.js).

Structure is read from the files (delivery path, script order, containers, the
Zone help pointer, no inline handlers, no environment variable names). Behaviour
runs household-settings.js and room-settings.js in Node over the same fake DOM
as the other household card tests, which throws if anything assigns innerHTML:
the three list states, hostile Home Assistant names, 409 recovery, refresh and
tab-leave behaviour, a non-owner, a restricted answer, the pickers up and down,
focus after a remove, the refusals (unmatched light, reserved room, duplicate
room, bad device id) and the room-groups fetch failing while the cards render.
"""
from __future__ import annotations

import re

import pytest

from .frontend_node import FRONTEND, run_scripts
from .test_admin_frontend_household_settings import PRELUDE

CARDS = FRONTEND / "household-settings.js"
ROOMS = FRONTEND / "room-settings.js"
ROOM_GROUPS = FRONTEND / "room-groups.js"
INDEX = FRONTEND / "index.html"
DOCKERFILE = FRONTEND / "Dockerfile"

EVIL = "<img src=x onerror=alert(1)>"

EXTRA = r"""
global.availableRooms = ['living_room', 'kitchen', 'den'];
global.__routes = {};
global.fetch = async (url, opts) => {
  opts = opts || {};
  const method = opts.method || 'GET';
  global.__fetches.push({url: String(url), method, body: opts.body ? JSON.parse(opts.body) : null, headers: opts.headers});
  let r = global.__routes[method + ' ' + String(url)];
  if (Array.isArray(r)) r = r.length > 1 ? r.shift() : r[0];
  if (r === undefined) throw new Error('no route ' + method + ' ' + url);
  if (r instanceof Error) throw r;
  return {ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body};
};
const HH = '/api/settings/household';
const LIGHTS = '/api/settings/room-lights/candidates';
const SATS = '/api/settings/speaker-rooms/satellites';
global.__OWNER = {required_permission: 'manage_infrastructure'};
global.__rl = (rooms, stored, extra) => ({status: 200, body: {room_lights: __entry({rooms}, {rooms: stored !== false && rooms !== null}, Object.assign({}, __OWNER, extra || {}))}});
global.__rlPut = (rooms, revision) => ({status: 200, body: __entry({rooms}, {rooms: rooms !== null}, Object.assign({revision: revision || 4}, __OWNER))});
global.__sr = (entries, extra) => ({status: 200, body: {speaker_rooms: __entry({entries}, {entries: entries !== null}, Object.assign({}, __OWNER, extra || {}))}});
global.__srPut = (entries, revision) => ({status: 200, body: __entry({entries}, {entries: entries !== null}, Object.assign({revision: revision || 4}, __OWNER))});
global.__light = (id, name, group) => ({entity_id: id, friendly_name: name, group: !!group});
global.__setup = (opts) => {
  opts = opts || {};
  __routes['GET ' + HH + '?sections=room_lights'] = opts.rl || __rl(null);
  __routes['GET ' + HH + '?sections=speaker_rooms'] = opts.sr || __sr(null);
  __routes['GET ' + LIGHTS] = opts.lights || {status: 200, body: [__light('light.floor_lamp', 'Floor lamp'), __light('light.sofa', 'Sofa strip'), __light('light.living_group', 'Living room group', true)]};
  __routes['GET ' + SATS] = opts.sats || {status: 200, body: [{entity_id: 'assist_satellite.kitchen', friendly_name: 'Kitchen satellite'}, {entity_id: 'assist_satellite.den', friendly_name: 'Den satellite'}]};
  return {lights: __box('lights'), speakers: __box('speakers')};
};
const _box = global.__box;
global.__box = id => _box(id === 'lights' ? 'household-card-room-lights' : id === 'speakers' ? 'household-card-speaker-rooms' : id);
global.__mount = async () => { await RoomSettings.mountAll(); await __settle(); };
global.__rows = root => root.find(e => e.tagName === 'LI' && e.attrs['data-room-row']);
global.__row = (root, n) => __rows(root)[n];
global.__in = (row, label) => {
  const l = row.find(e => e.tagName === 'LABEL' && e.textContent === label)[0];
  if (!l) throw new Error('no label ' + label);
  return row.find(e => e.attrs.id === l.attrs.for)[0];
};
global.__lastPut = () => __fetches.filter(f => f.method === 'PUT').pop();
global.__chipRemoves = row => row.find(e => e.tagName === 'BUTTON' && e.textContent === 'Remove');
global.__addLight = (row, text) => { const i = __in(row, 'Add light'); i.value = text; i.fire('change'); return i; };
global.__errs = root => root.find(e => e.attrs.role === 'alert' && __visible(e) && e.textContent !== '').map(e => e.textContent);
global.__unsaved = root => root.find(e => e.textContent === 'Unsaved changes').map(__visible).some(Boolean);
"""


def scenario(body: str, scripts=None):
    return run_scripts(scripts or [CARDS, ROOMS], f"(async () => {{ {body} }})()", prelude=PRELUDE + EXTRA)


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def source():
    return ROOMS.read_text(encoding="utf-8")


# --- structure ----------------------------------------------------------------------------------

def test_the_script_is_copied_and_tagged_after_the_renderer_and_before_escape_html(html):
    assert "COPY room-settings.js /usr/share/nginx/html/" in DOCKERFILE.read_text(encoding="utf-8")
    tag = re.search(r'<script src="/room-settings\.js\?v=([^"]+)"></script>', html)
    assert tag, "room-settings.js is not tagged with a ?v="
    assert html.index('<script src="/status-vocab.js') < html.index('<script src="/household-settings-lists.js') < tag.start() < html.index('<script src="/escape-html.js')
    assert html.index('<script src="/room-groups.js') < tag.start()


def test_the_edited_scripts_carry_the_new_buster(html):
    for name, tag in (("room-settings.js", "lg6"), ("room-groups.js", "lg4"), ("app.js", "lg4")):
        assert re.search(rf'/{re.escape(name)}\?v=20261015-{tag}"', html), name
    assert re.search(r'/household-settings\.js\?v=20261015-hs5"', html), "edited for the signed-in token"


def test_the_rooms_tab_holds_both_containers_above_the_groups_and_is_named_rooms(html):
    tab = html[html.index('<div id="tab-room-groups"'):html.index('<!-- Users Tab -->')]
    lights, speakers, groups = (tab.index(f'id="{i}"') for i in ("household-card-room-lights", "household-card-speaker-rooms", "room-groups-container"))
    assert lights < speakers < groups
    assert '<h2 class="text-2xl font-semibold text-white">Rooms</h2>' in tab
    nav = re.search(r"showTab\('room-groups'\)[^>]*>\s*<i[^>]*></i>\s*<span>([^<]+)</span>", html)
    assert nav and nav.group(1) == "Rooms"


def test_loading_the_tab_mounts_the_cards_before_the_room_groups_fetch():
    text = ROOM_GROUPS.read_text(encoding="utf-8")
    body = text[text.index("async function loadRoomGroups() {"):]
    assert body.index("RoomSettings.mountAll()") < body.index("fetch(ROOM_GROUPS_API")
    assert "await RoomSettings" not in body


def test_the_zone_help_points_to_speaker_rooms():
    text = (FRONTEND / "app.js").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if "'devices-zone'" in l)
    assert "Rooms" in line and "Speaker rooms" in line


def test_the_script_never_assigns_markup_writes_handlers_or_names_environment_variables(source):
    for needle in (".innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function"):
        assert needle not in source, needle
    assert not re.search(r"\bon(click|input|change|blur|submit)\s*=", source)
    assert not re.search(r"setAttribute\(\s*['\"]on", source)
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", source)
    for name in ("HA_", "LIGHT_GROUPS", "SATELLITE_ROOM_MAP", "process.env", "environment"):
        assert name not in source, name


# --- the three states ---------------------------------------------------------------------------

def test_room_lights_shows_unset_none_and_rows_distinctly_and_sends_each():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        const states = [];
        states.push({unset: __text(lights).includes("Not set here. Athena uses the server's setting if there is one, otherwise your Home Assistant areas."), add: __buttons(lights, 'Add a room').length, useServer: __buttons(lights, "Use the server's setting").length});
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Living room');
        __addLight(row, 'Floor lamp');
        __routes['PUT ' + HH + '/room_lights'] = __rlPut([{room: 'Living room', lights: ['light.floor_lamp']}]);
        __saveButton(lights).click();
        await __settle();
        const sentRows = __lastPut().body;
        __buttons(__row(lights, 0), 'Remove room')[0].click();
        const noneText = __text(lights).includes("None. Athena ignores the server's setting and uses only your Home Assistant areas.");
        __routes['PUT ' + HH + '/room_lights'] = __rlPut([]);
        __saveButton(lights).click();
        await __settle();
        const sentNone = __lastPut().body.value;
        __buttons(lights, "Use the server's setting")[0].click();
        __routes['PUT ' + HH + '/room_lights'] = __rlPut(null, 6);
        __saveButton(lights).click();
        await __settle();
        return {states, sentRows, noneText, sentNone, sentNull: __lastPut().body.value, saved: __text(lights).includes('Saved. Changes reach the assistant within a minute.'),
                auths: Array.from(new Set(__fetches.map(f => f.headers && f.headers.Authorization))), kinds: Array.from(new Set(__fetches.map(f => f.method + (f.url.includes('/candidates') ? ' picker' : ''))))};
        """
    )
    # The token exists only in localStorage (app.js's `let authToken` is not on window): every card and picker request must still carry it.
    assert result["auths"] == ["Bearer test-token"] and {"GET", "GET picker", "PUT"} <= set(result["kinds"])
    assert result["states"][0] == {"unset": True, "add": 1, "useServer": 0}
    assert result["sentRows"] == {"value": {"rooms": [{"room": "Living room", "lights": ["light.floor_lamp"]}]}, "revision": 3}
    assert result["noneText"] is True
    assert result["sentNone"] == {"rooms": []}
    assert result["sentNull"] == {"rooms": None}
    assert result["saved"] is True


def test_a_stored_empty_list_loads_as_none_with_the_way_back_to_the_servers_setting():
    result = scenario(
        r"""
        const {lights, speakers} = __setup({rl: __rl([]), sr: __sr([])});
        await __mount();
        return {lights: __text(lights).includes('None. Athena ignores'), back: __buttons(lights, "Use the server's setting").length, add: __buttons(lights, 'Add a room').length,
                speakers: __text(speakers).includes('None. Athena ignores the server\'s setting and uses only the Home Assistant area of each voice satellite.'), addSat: __buttons(speakers, 'Add a satellite').length};
        """
    )
    assert result == {"lights": True, "back": 1, "add": 1, "speakers": True, "addSat": 1}


def test_the_cards_carry_the_intro_and_the_speaker_picker_help_and_no_environment_names():
    result = scenario(
        r"""
        const {lights, speakers} = __setup({sr: __sr([{key: 'assist_satellite.kitchen', room: 'Kitchen'}])});
        await __mount();
        return {lights: __text(lights), speakers: __text(speakers)};
        """
    )
    assert "Usually not needed. Athena finds each room's lights from your Home Assistant areas." in result["lights"]
    assert "A room listed here uses only these lights." in result["lights"]
    assert "Usually not needed. Athena uses the Home Assistant area of each voice satellite." in result["speakers"]
    assert "If it can't, use the device id instead." in result["speakers"]
    for text in result.values():
        assert not re.search(r"HA_[A-Z_]+|[A-Z]{3,}_[A-Z_]{3,}", text)


# --- hostile text -------------------------------------------------------------------------------

def test_hostile_room_names_and_friendly_names_stay_text_everywhere():
    result = scenario(
        r"""
        const evil = '<img src=x onerror=alert(1)>';
        const {lights, speakers} = __setup({
          rl: __rl([{room: evil, lights: ['light.evil', 'light.group_evil']}]),
          lights: {status: 200, body: [__light('light.evil', evil), __light('light.group_evil', evil + 'g', true)]},
          sr: __sr([{key: 'assist_satellite.kitchen', room: evil}]),
          sats: {status: 200, body: [{entity_id: 'assist_satellite.kitchen', friendly_name: evil}]},
        });
        await __mount();
        const tags = n => n.find(e => e.tagName === 'IMG' || e.tagName === 'SCRIPT').length;
        const row = __row(lights, 0);
        const options = lights.find(e => e.tagName === 'OPTION').map(o => o.attrs.label);
        const labels = __chipRemoves(row).map(b => b.attrs['aria-label']);
        __in(row, 'Room name').fire('focus');
        const roomOptions = lights.find(e => e.tagName === 'OPTION' && e.attrs.value === evil).length;
        return {tags: tags(lights) + tags(speakers), nameValue: __in(row, 'Room name').value, options, labels, roomOptions,
                chipText: row.textContent.includes(evil), captionText: __text(speakers).includes('Home Assistant name: ' + evil)};
        """
    )
    assert result["tags"] == 0
    assert result["nameValue"] == EVIL
    assert EVIL in result["options"]
    assert result["labels"][0] == f"Remove {EVIL} from {EVIL}"
    assert result["roomOptions"] >= 1
    assert result["chipText"] is True and result["captionText"] is True


# --- dirty state, refresh, tab leave ----------------------------------------------------------

def test_refresh_keeps_the_draft_one_binding_and_the_pickers_are_not_refetched_while_fresh():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const input = __in(__row(lights, 0), 'Room name');
        __type(input, 'Den');
        const save = __saveButton(lights);
        const before = __fetches.length;
        await __mount();
        await __mount();
        const refetched = __fetches.slice(before).map(f => f.url);
        return {marker: __unsaved(lights), value: __in(__row(lights, 0), 'Room name').value, sameInput: __in(__row(lights, 0), 'Room name') === input,
                saveListeners: save.listeners.click.length, refetched, pickerFetches: __fetches.filter(f => f.url.includes('/candidates')).length};
        """
    )
    assert result["marker"] is True and result["value"] == "Den" and result["sameInput"] is True
    assert result["saveListeners"] == 1
    assert result["refetched"] == ["/api/settings/household?sections=speaker_rooms"] * 2
    assert result["pickerFetches"] == 1


def test_leaving_the_tab_asks_naming_the_card_and_staying_keeps_the_draft():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        __type(__in(__row(lights, 0), 'Room name'), 'Den');
        __confirmAnswer = false;
        const stayed = showTab('dashboard');
        const message = __confirms[0];
        const kept = __in(__row(lights, 0), 'Room name').value;
        const beforeunload = {prevented: false};
        __listeners.beforeunload.forEach(f => f({preventDefault() { beforeunload.prevented = true; }}));
        __confirmAnswer = true;
        showTab('dashboard');
        return {stayed: stayed === undefined, message, kept, beforeunload, shown: __shown.slice()};
        """
    )
    assert result["stayed"] is True
    assert result["message"] == "You have unsaved changes to Room lights. Leave without saving?"
    assert result["kept"] == "Den" and result["beforeunload"]["prevented"] is True
    assert result["shown"] == ["dashboard"]


def test_a_409_keeps_the_draft_reloads_the_latest_and_reapply_sends_the_new_revision():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Den');
        __addLight(row, 'light.sofa');
        const latest = __rl([{room: 'Garage', lights: ['light.floor_lamp']}], true, {revision: 9});
        __routes['GET ' + HH + '?sections=room_lights'] = latest;
        __routes['PUT ' + HH + '/room_lights'] = {status: 409, body: {detail: 'revision_conflict'}};
        __saveButton(lights).click();
        await __settle();
        const text = __text(lights);
        const shown = __in(__row(lights, 0), 'Room name').value;
        __buttons(lights, 'Re-apply my changes')[0].click();
        const reapplied = __in(__row(lights, 0), 'Room name').value;
        const focusOnRestored = __focused === __in(__row(lights, 0), 'Room name');
        const dirty = __unsaved(lights);
        __routes['PUT ' + HH + '/room_lights'] = __rlPut([{room: 'Den', lights: ['light.sofa']}], 10);
        __saveButton(lights).click();
        await __settle();
        return {conflict: text.includes("Someone else saved Room lights while you were editing. Your changes weren't saved. The latest version is shown below."), shown, reapplied, focusOnRestored, dirty, sent: __lastPut().body};
        """
    )
    assert result["conflict"] is True
    assert result["shown"] == "Garage"
    assert result["reapplied"] == "Den" and result["dirty"] is True
    assert result["focusOnRestored"] is True, "focus lands on the first restored field, not Add a room"
    assert result["sent"] == {"value": {"rooms": [{"room": "Den", "lights": ["light.sofa"]}]}, "revision": 9}


# --- who may change it -------------------------------------------------------------------------

def test_a_non_owner_sees_the_values_read_only_and_cannot_save():
    result = scenario(
        r"""
        __perms = ['read', 'write'];
        const {lights, speakers} = __setup({rl: __rl([{room: 'Den', lights: ['light.sofa']}]), sr: __sr([{key: 'assist_satellite.den', room: 'Den'}])});
        await __mount();
        const inputs = __inputs(lights).concat(__inputs(speakers));
        const buttons = lights.find(e => e.tagName === 'BUTTON').concat(speakers.find(e => e.tagName === 'BUTTON'));
        return {note: __text(lights).includes('Only the house owner can change these settings.'), allDisabled: inputs.length > 0 && inputs.every(i => i.disabled),
                activeButtons: buttons.filter(b => !b.disabled && __visible(b)).map(b => b.textContent), value: __in(__row(lights, 0), 'Room name').value,
                pickerFetches: __fetches.filter(f => f.url === LIGHTS || f.url === SATS).length, text: __text(lights) + __text(speakers)};
        """
    )
    assert result["note"] is True and result["allDisabled"] is True
    assert result["pickerFetches"] == 0, "the pickers are owner-only: a non-owner never asks"
    assert "isn't answering" not in result["text"]
    assert result["activeButtons"] == []
    assert result["value"] == "Den"


def test_a_restricted_answer_shows_no_values_and_no_controls():
    result = scenario(
        r"""
        __perms = ['read', 'write'];
        const restricted = {status: 200, body: {room_lights: {value: null, stored: {rooms: true}, revision: 3, required_permission: 'manage_infrastructure', writable: true, restricted: true}}};
        const {lights} = __setup({rl: restricted});
        await __mount();
        return {text: __text(lights), inputs: __inputs(lights).length, save: __visible(__saveButton(lights)) && !__saveButton(lights).disabled};
        """
    )
    assert "Only the house owner can see and change these settings." in result["text"]
    assert "Set by the owner" in result["text"]
    assert result["inputs"] == 0 and result["save"] is False


def test_a_403_on_the_get_says_so_and_a_failure_offers_retry():
    result = scenario(
        r"""
        const {lights, speakers} = __setup({rl: {status: 403, body: {}}, sr: {status: 500, body: {}}});
        await __mount();
        const forbidden = __text(lights).includes("You don't have permission to view this setting");
        const failed = __text(speakers).includes("Couldn't load Speaker rooms.");
        const retry = __buttons(speakers, 'Retry').length;
        __routes['GET ' + HH + '?sections=speaker_rooms'] = __sr(null);
        __buttons(speakers, 'Retry')[0].click();
        await __settle();
        return {forbidden, failed, retry, recovered: __buttons(speakers, 'Add a satellite').length};
        """
    )
    assert result == {"forbidden": True, "failed": True, "retry": 1, "recovered": 1}


# --- chips, focus and the picker --------------------------------------------------------------

def test_a_chip_has_a_named_remove_and_focus_moves_to_the_next_then_previous_then_add():
    result = scenario(
        r"""
        const {lights} = __setup({rl: __rl([{room: 'Den', lights: ['light.floor_lamp', 'light.sofa', 'light.living_group']}])});
        await __mount();
        const row = () => __row(lights, 0);
        const name = b => b.attrs['aria-label'];
        const first = __chipRemoves(row()).map(name);
        __chipRemoves(row())[1].click();
        const live = lights.find(e => e.attrs.role === 'status').map(e => e.textContent).join('|');
        const middle = name(__focused);
        __chipRemoves(row())[1].click();
        const last = name(__focused);
        __chipRemoves(row())[0].click();
        return {first, middle, last, onlyAdd: __focused === __in(row(), 'Add light'), live};
        """
    )
    assert result["first"] == ["Remove Floor lamp from Den", "Remove Sofa strip from Den", "Remove Living room group from Den"]
    assert result["middle"] == "Remove Living room group from Den"
    assert result["last"] == "Remove Floor lamp from Den"
    assert result["onlyAdd"] is True
    assert "Sofa strip removed from Den" in result["live"]


def test_a_light_group_shows_a_group_marker_and_the_guest_help():
    result = scenario(
        r"""
        const {lights} = __setup({rl: __rl([{room: 'Den', lights: ['light.living_group']}])});
        await __mount();
        const row = __row(lights, 0);
        const help = row.find(e => e.textContent.startsWith('A group answers as its bulbs'))[0];
        return {group: row.find(e => e.tagName === 'SPAN' && e.textContent === 'group').length, helpVisible: __visible(help), id: row.textContent.includes('light.living_group')};
        """
    )
    assert result == {"group": 1, "helpVisible": True, "id": True}


def test_removing_the_last_row_announces_the_empty_state_in_the_live_region():
    result = scenario(
        r"""
        const {lights, speakers} = __setup({rl: __rl([{room: 'Den', lights: ['light.sofa']}, {room: 'Hall', lights: ['light.floor_lamp']}]), sr: __sr([{key: 'assist_satellite.den', room: 'Den'}])});
        await __mount();
        const live = box => box.find(e => e.attrs.role === 'status').map(e => e.textContent).join('|');
        __buttons(__row(lights, 1), 'Remove room')[0].click();
        const afterFirst = live(lights);
        __buttons(__row(lights, 0), 'Remove room')[0].click();
        const afterLast = live(lights);
        __buttons(__row(speakers, 0), 'Remove')[0].click();
        return {afterFirst, afterLast, speakers: live(speakers)};
        """
    )
    tail = "None are listed now. Save to use only Home Assistant areas, or use the server's setting."
    assert "Room removed" in result["afterFirst"] and tail not in result["afterFirst"]
    assert f"Room removed. {tail}" in result["afterLast"]
    assert f"Satellite removed. {tail}" in result["speakers"]


def test_the_two_light_cards_have_distinct_titles_and_the_name_block_wraps_on_phones(source):
    household = CARDS.read_text(encoding="utf-8")
    assert "title: \"Lights that aren't lamps\"" in household and "title: 'Room lights'" not in household
    assert "title: 'Room lights'" in source
    assert "basis-full sm:basis-auto" in source and "sm:mt-6" in source


def test_a_saved_light_home_assistant_no_longer_has_is_marked_in_text_and_in_its_remove_label():
    result = scenario(
        r"""
        const {lights} = __setup({rl: __rl([{room: 'Den', lights: ['light.sofa', 'light.not_in_ha_anymore']}])});
        await __mount();
        const row = __row(lights, 0);
        const tags = row.find(e => e.tagName === 'SPAN' && e.textContent === 'Not in Home Assistant').length;
        const labels = __chipRemoves(row).map(b => b.attrs['aria-label']);
        return {tags, labels};
        """
    )
    assert result["tags"] == 1
    assert result["labels"] == ["Remove Sofa strip from Den", "Remove light.not_in_ha_anymore (not in Home Assistant) from Den"]


def test_nothing_is_marked_missing_while_the_home_assistant_list_is_unavailable():
    result = scenario(
        r"""
        const {lights} = __setup({rl: __rl([{room: 'Den', lights: ['light.not_in_ha_anymore']}]), lights: {status: 502, body: {}}});
        await __mount();
        return {tags: __row(lights, 0).find(e => e.tagName === 'SPAN' && e.textContent === 'Not in Home Assistant').length};
        """
    )
    assert result["tags"] == 0


def test_a_satellite_home_assistant_does_not_list_is_captioned_not_found():
    result = scenario(
        r"""
        const {speakers} = __setup({sr: __sr([{key: 'assist_satellite.gone', room: 'Den'}, {key: 'assist_satellite.kitchen', room: 'Kitchen'}])});
        await __mount();
        return {gone: __row(speakers, 0).textContent.includes('Not found in Home Assistant'), listed: __row(speakers, 1).textContent.includes('Not found in Home Assistant')};
        """
    )
    assert result == {"gone": True, "listed": False}


def test_a_long_chip_wraps_inside_its_row_and_the_room_row_stays_compact(source):
    chip = source[source.index("chipList.appendChild(el('li'"):]
    chip = chip[: chip.index("remove));")]
    assert "flex-wrap" in chip and "max-w-full" in chip and "min-w-0" in chip
    assert "break-all" in chip and "w-full sm:w-auto" in chip, "the entity id sits on its own line below sm and breaks"
    row = source[source.index("el('li', { class: 'mb-4 border border-dark-border rounded-lg p-3', 'data-room-row': 'lights' }"):]
    row = row[: row.index("roomName()")]
    assert "sm:max-w-sm" in row and "sm:max-w-xl" in row
    assert row.index("removeRoom") < row.index("Add light"), "Remove room sits in the row header"


def test_typed_names_resolve_and_an_unmatched_value_is_refused_while_home_assistant_answers():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Den');
        __addLight(row, 'sofa STRIP');
        const byName = __chipRemoves(row).length;
        const input = __addLight(row, 'light.not_in_the_list');
        const refused = {errors: __errs(row), invalid: input.attrs['aria-invalid'], kept: input.value, chips: __chipRemoves(row).length};
        __type(input, '');
        __addLight(row, 'light.floor_lamp');
        const dup = __addLight(row, 'Floor lamp');
        return {byName, refused, dupErrors: __errs(row), chips: __chipRemoves(row).length};
        """
    )
    assert result["byName"] == 1
    assert result["refused"]["errors"] == ["Pick a light from the list, or type its entity id (light.…)."]
    assert result["refused"]["invalid"] == "true" and result["refused"]["kept"] == "light.not_in_the_list" and result["refused"]["chips"] == 1
    assert result["dupErrors"] == ["That light is already in this room."]
    assert result["chips"] == 2


def test_when_home_assistant_does_not_answer_the_card_still_works_with_typed_ids():
    result = scenario(
        r"""
        const {lights, speakers} = __setup({lights: {status: 502, body: {}}, sats: new Error('network')});
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Den');
        __addLight(row, 'LIGHT.Sofa');
        const accepted = __chipRemoves(row).length;
        const input = __addLight(row, 'Floor lamp');
        __buttons(speakers, 'Add a satellite')[0].click();
        const srow = __row(speakers, 0);
        const key = __in(srow, 'Voice satellite or device id');
        key.value = 'assist_satellite.den';
        key.fire('blur');
        return {notice: __text(lights).includes("Home Assistant isn't answering, so the list of lights can't be shown. You can still type entity ids."),
                satNotice: __text(speakers).includes("so the list of voice satellites can't be shown. You can still enter a device id."),
                accepted, errors: __errs(row), srowErrors: __errs(srow), key: key.value, options: lights.find(e => e.tagName === 'OPTION' && e.attrs.label).length};
        """
    )
    assert result["notice"] is True and result["satNotice"] is True
    assert result["accepted"] == 1
    assert result["errors"] == ["Only light entities can be added here (light.…)."]
    assert result["srowErrors"] == [] and result["key"] == "assist_satellite.den"


def test_enter_adds_the_light_without_saving():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        const input = __in(row, 'Add light');
        input.value = 'Floor lamp';
        input.fire('keydown', {key: 'Enter'});
        return {chips: __chipRemoves(row).length, cleared: input.value, puts: __fetches.filter(f => f.method === 'PUT').length};
        """
    )
    assert result == {"chips": 1, "cleared": "", "puts": 0}


def test_room_name_suggestions_list_rooms_in_use_first_then_the_common_names():
    result = scenario(
        r"""
        const {lights, speakers} = __setup({rl: __rl([{room: 'Library', lights: ['light.sofa']}]), sr: __sr([{key: 'assist_satellite.den', room: 'Porch'}])});
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 1);
        __in(row, 'Room name').fire('focus');
        const list = row.find(e => e.tagName === 'DATALIST')[0] || lights.find(e => e.tagName === 'DATALIST' && e.attrs.id.endsWith('room-names'))[0];
        return list.find(e => e.tagName === 'OPTION').map(o => o.attrs.value + '|' + o.attrs.label);
        """
    )
    assert result == [
        "Library|Already used on this page",
        "Porch|Already used on this page",
        "Living room|Common room name",
        "Kitchen|Common room name",
        "Den|Common room name",
    ]


# --- refusals ----------------------------------------------------------------------------------

def test_a_reserved_a_duplicate_a_blank_and_a_long_room_name_each_get_their_own_message():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        const out = {};
        __buttons(lights, 'Add a room')[0].click();
        const add = () => __buttons(lights, 'Add a room')[0].click();
        add(); add();
        const names = [0, 1, 2].map(i => __in(__row(lights, i), 'Room name'));
        const check = (input, v) => { input.value = v; input.fire('blur'); return __errs(__row(lights, names.indexOf(input)).parentElement || lights).slice(); };
        const one = (i, v) => { names[i].value = v; names[i].fire('blur'); return names[i].attrs['aria-invalid'] + ':' + __errs(__row(lights, i)).join('|'); };
        out.reserved = one(0, 'Whole House');
        out.blank = (() => { names[0].value = ''; names[0].fire('blur'); return __errs(__row(lights, 0)).join('|'); })();
        out.long = one(0, 'x'.repeat(65));
        out.first = one(0, 'Living room');
        out.dup = one(1, ' living   ROOM ');
        out.clean = one(2, 'Den');
        return out;
        """
    )
    assert result["reserved"] == "true:That name is reserved. Choose a room name such as Living room."
    assert result["blank"] == "Enter a room name."
    assert result["long"] == "true:Room names can be up to 64 characters."
    assert result["first"] == "false:"
    assert result["dup"] == "true:Living room is already listed (names are matched ignoring case and spaces)."
    assert result["clean"] == "false:"


def test_save_with_an_empty_room_or_no_lights_stops_before_the_request_and_focuses_the_first_problem():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Den');
        __saveButton(lights).click();
        await __settle();
        return {puts: __fetches.filter(f => f.method === 'PUT').length, errors: __errs(lights), focusedIsAdd: __focused === __in(row, 'Add light')};
        """
    )
    assert result["puts"] == 0
    assert "Add at least one light to this room, or remove the room." in result["errors"]
    assert result["focusedIsAdd"] is True


def test_a_422_is_placed_on_its_row_in_plain_words_and_the_server_text_is_never_shown():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Den');
        __addLight(row, 'Sofa strip');
        __routes['PUT ' + HH + '/room_lights'] = {status: 422, body: {detail: [
          {loc: ['rooms', 0, 'room'], type: 'reserved_room', msg: '<img src=x onerror=alert(1)>', input: 'Den'},
          {loc: ['rooms', 0, 'lights', 0], type: 'not_a_light'},
        ]}};
        __saveButton(lights).click();
        await __settle();
        return {errors: __errs(lights), echoed: __text(lights).includes('onerror'), focusedName: __focused === __in(row, 'Room name'), invalid: __in(row, 'Room name').attrs['aria-invalid']};
        """
    )
    assert "That name is reserved. Choose a room name such as Living room." in result["errors"]
    assert "Only light entities can be added here (light.…)." in result["errors"]
    assert result["echoed"] is False and result["focusedName"] is True and result["invalid"] == "true"


def test_a_list_level_error_and_an_unknown_error_type_get_plain_generic_words():
    result = scenario(
        r"""
        const {lights} = __setup();
        await __mount();
        __buttons(lights, 'Add a room')[0].click();
        const row = __row(lights, 0);
        __type(__in(row, 'Room name'), 'Den');
        __addLight(row, 'Sofa strip');
        const send = async detail => {
          __routes['PUT ' + HH + '/room_lights'] = {status: 422, body: {detail}};
          __saveButton(lights).click();
          await __settle();
          return __errs(lights).join('|');
        };
        const tooMany = await send([{loc: ['rooms'], type: 'too_many'}]);
        const unknown = await send([{loc: ['rooms', 0, 'room'], type: 'surprise_type_from_a_newer_server'}]);
        return {tooMany, unknown};
        """
    )
    assert "Up to 64 rooms." in result["tooMany"]
    assert "This value wasn't accepted." in result["unknown"] and "surprise" not in result["unknown"]


# --- Speaker rooms -----------------------------------------------------------------------------

def test_a_satellite_row_resolves_a_picked_name_a_device_id_and_saves_both():
    result = scenario(
        r"""
        const {speakers} = __setup();
        await __mount();
        __buttons(speakers, 'Add a satellite')[0].click();
        __buttons(speakers, 'Add a satellite')[0].click();
        const [a, b] = [0, 1].map(i => __row(speakers, i));
        const key = r => __in(r, 'Voice satellite or device id');
        key(a).value = 'kitchen SATELLITE'; key(a).fire('blur');
        const resolved = key(a).value;
        const caption = a.textContent.includes('Home Assistant name: Kitchen satellite');
        __type(__in(a, 'Room'), 'Kitchen');
        key(b).value = ' ABCDEF0123456789 abcdef0123456789 '; key(b).fire('blur');
        __type(__in(b, 'Room'), 'Porch');
        __routes['PUT ' + HH + '/speaker_rooms'] = __srPut([]);
        __saveButton(speakers).click();
        await __settle();
        return {resolved, caption, sent: __lastPut().body, label: __buttons(a, 'Remove')[0].attrs['aria-label']};
        """
    )
    assert result["resolved"] == "assist_satellite.kitchen" and result["caption"] is True
    assert result["sent"] == {
        "value": {"entries": [
            {"key": "assist_satellite.kitchen", "room": "Kitchen"},
            {"key": "abcdef0123456789abcdef0123456789", "room": "Porch"},
        ]},
        "revision": 3,
    }
    assert result["label"] == "Remove satellite Kitchen satellite"


def test_a_bad_device_id_an_unlisted_satellite_a_duplicate_key_and_a_shared_room_are_refused():
    result = scenario(
        r"""
        const {speakers} = __setup();
        await __mount();
        for (let i = 0; i < 3; i++) __buttons(speakers, 'Add a satellite')[0].click();
        const rows = [0, 1, 2].map(i => __row(speakers, i));
        const key = r => __in(r, 'Voice satellite or device id');
        const set = (r, v, blur) => { const i = typeof v === 'string' && blur === 'room' ? __in(r, 'Room') : key(r); i.value = v; i.fire('blur'); };
        const err = r => __errs(r).join('|');
        const out = {};
        set(rows[0], 'abc123'); out.short = err(rows[0]);
        set(rows[0], 'the porch thing'); out.unlisted = err(rows[0]);
        set(rows[0], 'assist_satellite.garage'); out.unlistedId = err(rows[0]);
        set(rows[0], 'assist_satellite.kitchen'); set(rows[0], 'Kitchen', 'room'); out.fine = err(rows[0]) + '|' + key(rows[0]).attrs['aria-invalid'];
        set(rows[1], 'Den satellite'); set(rows[1], ' kitchen ', 'room'); out.shared = err(rows[1]);
        set(rows[2], 'assist_satellite.kitchen'); out.dupKey = err(rows[2]);
        return out;
        """
    )
    assert result["short"] == "A device id is 32 letters and numbers (0-9, a-f)."
    assert result["unlisted"] == "Pick a satellite from the list, or enter its device id."
    assert result["unlistedId"] == "Pick a satellite from the list, or enter its device id."
    assert result["fine"] == "|false"
    assert result["shared"] == "Another satellite already uses this room."
    assert result["dupKey"] == "That satellite is already listed."


def test_a_device_id_may_share_a_room_with_a_satellite_entity():
    result = scenario(
        r"""
        const {speakers} = __setup({sr: __sr([{key: 'assist_satellite.kitchen', room: 'Kitchen'}, {key: 'abcdef0123456789abcdef0123456789', room: 'Kitchen'}])});
        await __mount();
        const ok = __rows(speakers).map(r => r.find(e => e.tagName === 'INPUT').length);
        __type(__in(__row(speakers, 0), 'Room'), 'Kitchen');
        __routes['PUT ' + HH + '/speaker_rooms'] = __srPut([]);
        __saveButton(speakers).click();
        await __settle();
        return {rows: ok.length, errors: __errs(speakers), put: __fetches.filter(f => f.method === 'PUT').length};
        """
    )
    assert result == {"rows": 2, "errors": [], "put": 0}


# --- the room-groups fetch failing does not block the cards ----------------------------------

def test_the_cards_render_when_the_room_groups_fetch_fails():
    result = scenario(
        r"""
        const {lights, speakers} = __setup();
        __routes['GET /api/room-groups'] = new Error('network down');
        __routes['GET /api/room-groups/available-rooms'] = new Error('network down');
        global.getToken = () => 'tok';
        await loadRoomGroups();
        await __settle();
        return {lights: __buttons(lights, 'Add a room').length, speakers: __buttons(speakers, 'Add a satellite').length};
        """,
        scripts=["document.addEventListener = () => {}; window.tabChangeCallbacks = {};", CARDS, ROOMS, ROOM_GROUPS],
    )
    assert result == {"lights": 1, "speakers": 1}
