"""The household list editors (admin/frontend/household-settings-lists.js).

Structure is read from the files (delivery path, script order, mounts, no inline
handlers). Behaviour runs household-settings.js and the list editors in Node
over the same fake DOM as the card tests, which throws if anything assigns
innerHTML: adding and removing a feed, masked URLs (never re-sent edited),
allow_private (never sent), focus after Remove, stop names as text, the three
list states, restricted and read-only rendering, 409 recovery, validation, and
the lamp-exclusion rules.
"""
from __future__ import annotations

import re

import pytest

from .frontend_node import FRONTEND, run_scripts
from .test_admin_frontend_household_settings import PRELUDE

CARDS = FRONTEND / "household-settings.js"
LISTS = FRONTEND / "household-settings-lists.js"
INDEX = FRONTEND / "index.html"
DOCKERFILE = FRONTEND / "Dockerfile"

EXTRA = r"""
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
global.__rows = root => root.find(e => e.tagName === 'LI' && e.attrs['data-list-row']);
global.__row = (root, n) => __rows(root)[n];
global.__in = (row, label) => {
  const l = row.find(e => e.tagName === 'LABEL' && e.textContent === label)[0];
  if (!l) throw new Error('no label ' + label);
  return row.find(e => e.attrs.id === l.attrs.for)[0];
};
global.__edit = row => row.find(e => e.tagName === 'BUTTON' && /^(Edit|Done)$/.test(e.textContent))[0];
global.__remove = row => row.find(e => e.tagName === 'BUTTON' && e.textContent === 'Remove')[0];
global.__pick = (select, v) => { select.value = v; select.fire('change'); };
global.__transit = (feeds, services, stored) => __ok('transit', __entry(
  {region_name: null, gtfs_feeds: feeds, static_services: services},
  Object.assign({region_name: false, gtfs_feeds: feeds !== null, static_services: services !== null}, stored || {})));
global.__transitPut = (feeds, services, revision) => __put(__entry(
  {region_name: null, gtfs_feeds: feeds, static_services: services}, {gtfs_feeds: feeds !== null, static_services: services !== null}, {revision: revision || 4}));
global.__lastPut = () => __fetches.filter(f => f.method === 'PUT').pop();
global.__feed = (over) => Object.assign({id: 'mta-bus', name: 'MTA Bus', url: 'https://api.example.org', url_masked: true}, over || {});
"""


def scenario(body: str):
    return run_scripts([CARDS, LISTS], f"(async () => {{ {body} }})()", prelude=PRELUDE + EXTRA)


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def source():
    return LISTS.read_text(encoding="utf-8")


# --- structure ----------------------------------------------------------------------------------

def test_the_script_is_copied_tagged_after_the_renderer_and_before_escape_html(html):
    assert "COPY household-settings-lists.js /usr/share/nginx/html/" in DOCKERFILE.read_text(encoding="utf-8")
    tag = re.search(r'<script src="/household-settings-lists\.js\?v=([^"]+)"></script>', html)
    assert tag, "household-settings-lists.js is not tagged with a ?v="
    assert html.index('<script src="/household-settings.js') < tag.start() < html.index('<script src="/escape-html.js')


@pytest.mark.parametrize("name", ["household-settings.js", "household-settings-lists.js", "directions-settings.js"])
def test_every_edited_script_carries_the_new_buster(html, name):
    assert re.search(rf'/{re.escape(name)}\?v=20261015-hs[0-9]"', html)


def test_the_directions_page_mounts_the_two_list_cards():
    text = (FRONTEND / "directions-settings.js").read_text(encoding="utf-8")
    assert "HouseholdSettings.mount('transit', 'household-card-transit')" in text
    assert "HouseholdSettings.mount('community_events', 'household-card-community-events')" in text
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="household-card-transit"' in html and 'id="household-card-community-events"' in html


def test_the_script_never_assigns_markup_or_writes_handlers(source):
    for needle in (".innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function"):
        assert needle not in source, needle
    assert not re.search(r"\bon(click|input|change|blur|submit)\s*=", source)
    assert not re.search(r"setAttribute\(\s*['\"]on", source)
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", source)


def test_the_private_address_switches_are_never_part_of_a_row(source):
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    assert "allow_private" not in code and "allowed_private_hosts" not in code


# --- add and remove -----------------------------------------------------------------------------

def test_a_feed_is_added_with_a_generated_id_saved_and_then_comes_back_masked():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit(null, null)];
        await HouseholdSettings.mount('transit', 'c1');
        const unsetText = __text(box).includes("Not set here. Athena uses the server's setting.");
        __buttons(box, 'Set a list here')[0].click();
        const row = __row(box, 0);
        const open = row.find(e => e.attrs['aria-expanded'])[0].attrs['aria-expanded'];
        __type(__in(row, 'Name'), 'MTA Bus');
        __type(__in(row, 'Feed URL'), 'https://example.org/gtfs.zip');
        const idShown = __in(row, 'Id').value;
        const summary = row.textContent.includes('MTA Bus') && row.textContent.includes('example.org');
        __script = [__transitPut([__feed({url: 'https://example.org'})], null)];
        __saveButton(box).click();
        await __settle();
        const sent = __lastPut().body;
        const after = __row(box, 0);
        return {unsetText, open, idShown, summary, sent, savedCopy: __text(box).includes('Saved. The transit service reloads within about a minute.'),
                masked: after.textContent.includes('https://example.org'), replace: __buttons(after, 'Replace URL').length, edit: __edit(after).attrs['aria-expanded']};
        """
    )
    assert result["unsetText"] is True
    assert result["open"] == "true"
    assert result["idShown"] == "mta-bus" and result["summary"] is True
    assert result["sent"] == {
        "value": {
            "region_name": None, "static_services": None,
            "gtfs_feeds": [{"id": "mta-bus", "name": "MTA Bus", "url": "https://example.org/gtfs.zip"}],
        },
        "revision": 3,
    }
    assert result["savedCopy"] is True
    assert result["masked"] is True and result["replace"] == 1 and result["edit"] == "false"


def test_removing_every_feed_stores_an_explicit_empty_list_and_use_server_stores_null():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const removeLabel = __remove(__row(box, 0)).attrs['aria-label'];
        __remove(__row(box, 0)).click();
        const none = {text: __text(box).includes('None. No transit feeds are loaded.'), hint: __text(box).includes("Add your agency's GTFS feed URL"), live: box.find(e => e.attrs.role === 'status').map(e => e.textContent).join('|')};
        __script = [__transitPut([], null)];
        __saveButton(box).click();
        await __settle();
        const sentEmpty = __lastPut().body.value.gtfs_feeds;
        __buttons(box, "Use the server's setting")[0].click();
        __script = [__transitPut(null, null, 5)];
        __saveButton(box).click();
        await __settle();
        return {removeLabel, none, sentEmpty, sentNull: __lastPut().body.value.gtfs_feeds};
        """
    )
    assert result["removeLabel"] == "Remove feed MTA Bus"
    assert result["none"]["text"] is True and result["none"]["hint"] is True
    assert "Removed feed MTA Bus" in result["none"]["live"]
    assert result["sentEmpty"] == []
    assert result["sentNull"] is None


def test_focus_after_remove_goes_to_the_next_remove_then_the_previous_then_add():
    result = scenario(
        r"""
        const box = __box('c1');
        const feeds = ['a', 'b', 'c'].map(n => __feed({id: n, name: 'Feed ' + n}));
        __script = [__transit(feeds, null)];
        await HouseholdSettings.mount('transit', 'c1');
        const removes = () => __rows(box).map(__remove);
        const names = () => __rows(box).map(r => r.textContent.slice(0, 6));
        const nameOf = b => b.attrs['aria-label'];
        __remove(__row(box, 1)).click();
        const middle = nameOf(__focused);
        __remove(__row(box, 1)).click();
        const last = nameOf(__focused);
        __remove(__row(box, 0)).click();
        const only = __focused.textContent;
        return {middle, last, only};
        """
    )
    assert result["middle"] == "Remove feed Feed c"
    assert result["last"] == "Remove feed Feed a"
    assert result["only"] == "Add feed"


def test_the_row_edit_disclosure_opens_one_row_at_a_time():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed({id: 'a', name: 'A'}), __feed({id: 'b', name: 'B'})], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const state = () => __rows(box).map(r => __edit(r).attrs['aria-expanded']);
        const start = state();
        __edit(__row(box, 0)).click();
        const first = state();
        __edit(__row(box, 1)).click();
        const second = state();
        __edit(__row(box, 1)).click();
        return {start, first, second, closed: state(), label: __edit(__row(box, 0)).attrs['aria-label'], controls: __edit(__row(box, 0)).attrs['aria-controls']};
        """
    )
    assert result["start"] == ["false", "false"]
    assert result["first"] == ["true", "false"]
    assert result["second"] == ["false", "true"]
    assert result["closed"] == ["false", "false"]
    assert result["label"] == "Edit feed A" and result["controls"].endswith("-panel")


# --- masked URLs --------------------------------------------------------------------------------

def test_an_unchanged_masked_url_is_sent_back_as_it_came_and_a_replacement_drops_the_flag():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const row = () => __row(box, 0);
        const shown = {text: row().textContent.includes('https://api.example.org'), replace: __buttons(row(), 'Replace URL').length};
        __type(__in(row(), 'Name'), 'MTA Buses');
        __script = [__transitPut([__feed({name: 'MTA Buses'})], null)];
        __saveButton(box).click();
        await __settle();
        const keptBody = __lastPut().body.value.gtfs_feeds[0];
        __buttons(row(), 'Replace URL')[0].click();
        __type(__in(row(), 'Feed URL'), 'https://new.example.org/gtfs.zip?key=SECRET');
        __script = [__transitPut([__feed({url: 'https://new.example.org'})], null, 5)];
        __saveButton(box).click();
        await __settle();
        return {shown, keptBody, replacedBody: __lastPut().body.value.gtfs_feeds[0]};
        """
    )
    assert result["shown"] == {"text": True, "replace": 1}
    assert result["keptBody"] == {"id": "mta-bus", "name": "MTA Buses", "url": "https://api.example.org", "url_masked": True}
    assert result["replacedBody"] == {"id": "mta-bus", "name": "MTA Buses", "url": "https://new.example.org/gtfs.zip?key=SECRET"}


def test_pasting_a_masked_url_is_refused_in_the_page_and_nothing_is_sent():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const row = () => __row(box, 0);
        __buttons(row(), 'Replace URL')[0].click();
        const input = __in(row(), 'Feed URL');
        __type(input, 'https://api.example.org/feed?api_key=•••');
        const puts = () => __fetches.filter(f => f.method === 'PUT').length;
        __saveButton(box).click();
        await __settle();
        return {puts: puts(), alerts: __alerts(box), invalid: input.attrs['aria-invalid'], focusedIsUrl: __focused === input};
        """
    )
    assert result["puts"] == 0
    assert any(
        "Paste the full URL, including its key. The saved key is hidden and can't be edited in place." in a
        for a in result["alerts"]
    )
    assert result["invalid"] == "true" and result["focusedIsUrl"] is True


def test_replace_with_nothing_typed_keeps_the_saved_url_and_keep_returns_to_the_saved_view():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const row = () => __row(box, 0);
        __edit(row()).click();
        __buttons(row(), 'Replace URL')[0].click();
        const dirtyAfterReplace = __visible(box.find(e => e.textContent === 'Unsaved changes')[0]);
        const keep = __buttons(row(), 'Keep the saved URL')[0];
        const keepVisible = __visible(keep);
        keep.click();
        return {dirtyAfterReplace, keepVisible, keepHidden: !__visible(keep), replaceVisible: __visible(__buttons(row(), 'Replace URL')[0])};
        """
    )
    assert result == {"dirtyAfterReplace": False, "keepVisible": True, "keepHidden": True, "replaceVisible": True}


def test_a_server_url_masked_edit_refusal_opens_the_replace_field_with_the_explanation():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        __type(__in(__row(box, 0), 'Name'), 'Renamed');
        __script = [{status: 422, body: {detail: [{loc: ['gtfs_feeds', 0, 'url'], type: 'url_masked_edit'}]}}];
        __saveButton(box).click();
        await __settle();
        const row = __row(box, 0);
        return {alerts: __alerts(box), open: __edit(row).attrs['aria-expanded'], input: __visible(__in(row, 'Feed URL'))};
        """
    )
    assert any("The saved key is hidden" in a for a in result["alerts"])
    assert result["open"] == "true" and result["input"] is True


def test_a_server_port_refusal_explains_the_standard_ports():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        __type(__in(__row(box, 0), 'Name'), 'Renamed');
        __script = [{status: 422, body: {detail: [{loc: ['gtfs_feeds', 0, 'url'], type: 'url_port'}]}}];
        __saveButton(box).click();
        await __settle();
        return {alerts: __alerts(box)};
        """
    )
    assert any("standard port" in a for a in result["alerts"]), result["alerts"]
    assert not any("This value wasn't accepted" in a for a in result["alerts"])


def test_allow_private_and_origin_are_never_sent_even_if_a_row_carries_them():
    result = scenario(
        r"""
        const box = __box('c1');
        const feed = __feed({allow_private: true, allowed_private_hosts: ['10.0.0.0/8'], origin: 'env'});
        __script = [__transit([feed], [{id: 's', name: 'Shuttle', frequency_minutes: 20, allow_private: true, stops: [{name: 'Dock', lat: 40.1, lon: -73.9}]}])];
        await HouseholdSettings.mount('transit', 'c1');
        __type(__in(__row(box, 0), 'Name'), 'Edited');
        __script = [__transitPut([__feed()], null)];
        __saveButton(box).click();
        await __settle();
        return JSON.stringify(__lastPut().body);
        """
    )
    assert "allow_private" not in result and "allowed_private_hosts" not in result and '"origin"' not in result


# --- stops, hours, ids, escaping ----------------------------------------------------------------

def test_stop_names_and_row_values_stay_text_and_are_never_markup():
    result = scenario(
        r"""
        const box = __box('c1');
        const evil = '<img src=x onerror=alert(1)>';
        __script = [__transit([__feed({name: evil})], [{id: 's', name: evil, frequency_minutes: 15, stops: [{name: evil, lat: 40.5, lon: -73.5}]}])];
        await HouseholdSettings.mount('transit', 'c1');
        const stop = __row(box, 1);
        __edit(stop).click();
        const nameInput = __in(stop, 'Stop 1 name');
        return {
          tags: box.find(e => e.tagName === 'IMG' || e.tagName === 'SCRIPT').length,
          value: nameInput.value, title: __row(box, 0).textContent.includes(evil),
          removeLabel: __remove(__row(box, 0)).attrs['aria-label'],
          stopRemove: stop.find(e => e.attrs['aria-label'] && e.attrs['aria-label'].startsWith('Remove stop'))[0].attrs['aria-label'],
        };
        """
    )
    assert result["tags"] == 0
    assert result["value"] == "<img src=x onerror=alert(1)>" and result["title"] is True
    assert result["removeLabel"] == "Remove feed <img src=x onerror=alert(1)>"
    assert result["stopRemove"] == "Remove stop 1: <img src=x onerror=alert(1)>"


def test_a_fixed_route_service_sends_hours_and_stops_and_validates_them():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit(null, null)];
        await HouseholdSettings.mount('transit', 'c1');
        __buttons(box, 'Set a list here')[1].click();
        const row = __row(box, 0);
        __type(__in(row, 'Name'), 'Harbor Shuttle');
        __type(__in(row, 'Runs every (minutes)'), '20');
        __buttons(row, 'Add stop')[0].click();
        __type(__in(row, 'Stop 1 name'), 'Dock');
        __type(__in(row, 'Stop 1 latitude'), '95');
        __type(__in(row, 'Stop 1 longitude'), '-73.9');
        __type(__in(row, 'Weekdays start'), '06:30');
        const blocked = {puts: 0};
        __saveButton(box).click();
        await __settle();
        blocked.puts = __fetches.filter(f => f.method === 'PUT').length;
        blocked.alerts = __alerts(box);
        __type(__in(row, 'Stop 1 latitude'), '40.7');
        __type(__in(row, 'Weekdays end'), '22:00');
        __script = [__transitPut(null, [{id: 'harbor-shuttle', name: 'Harbor Shuttle', frequency_minutes: 20, stops: [{name: 'Dock', lat: 40.7, lon: -73.9}]}])];
        __saveButton(box).click();
        await __settle();
        return {blocked, sent: __lastPut().body.value.static_services};
        """
    )
    assert result["blocked"]["puts"] == 0
    assert "Latitude is between -90 and 90." in result["blocked"]["alerts"]
    assert result["sent"] == [{
        "id": "harbor-shuttle", "name": "Harbor Shuttle", "frequency_minutes": 20,
        "hours": {"weekday": {"start": "06:30", "end": "22:00"}},
        "stops": [{"name": "Dock", "lat": 40.7, "lon": -73.9}],
    }]


def test_a_half_filled_hours_range_and_a_service_with_no_stops_are_refused():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit(null, null)];
        await HouseholdSettings.mount('transit', 'c1');
        __buttons(box, 'Set a list here')[1].click();
        const row = __row(box, 0);
        __type(__in(row, 'Name'), 'Shuttle');
        __type(__in(row, 'Runs every (minutes)'), '0');
        __type(__in(row, 'Weekends start'), '09:00');
        __saveButton(box).click();
        await __settle();
        return {alerts: __alerts(box), puts: __fetches.filter(f => f.method === 'PUT').length};
        """
    )
    assert result["puts"] == 0
    assert "Enter whole minutes from 1 to 1440." in result["alerts"]
    assert "Enter both a start and an end time, or clear both." in result["alerts"]
    assert "Add at least one stop." in result["alerts"]


def test_a_saved_rows_id_survives_a_rename_and_a_new_rows_id_follows_the_name_until_edited():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed({id: 'mta-bus'})], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const saved = __row(box, 0);
        __type(__in(saved, 'Name'), 'Totally Different');
        const savedId = {value: __in(saved, 'Id').value, readonly: __in(saved, 'Id').attrs.readonly !== undefined};
        __buttons(box, 'Add feed')[0].click();
        const fresh = __row(box, 1);
        __type(__in(fresh, 'Name'), 'MTA Bus');
        const first = __in(fresh, 'Id').value;
        __type(__in(fresh, 'Name'), 'Metro Rail');
        const renamed = __in(fresh, 'Id').value;
        __type(__in(fresh, 'Id'), 'my-id');
        __type(__in(fresh, 'Name'), 'Other');
        return {savedId, first, renamed, manual: __in(fresh, 'Id').value};
        """
    )
    assert result["savedId"] == {"value": "mta-bus", "readonly": True}
    assert result["first"] == "mta-bus-2"
    assert result["renamed"] == "metro-rail"
    assert result["manual"] == "my-id"


# --- community events ---------------------------------------------------------------------------

def test_community_sources_use_human_type_labels_and_help_that_follows_the_type():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('community_events', __entry({sources: null}, {sources: false}))];
        await HouseholdSettings.mount('community_events', 'c1');
        __buttons(box, 'Set a list here')[0].click();
        const row = __row(box, 0);
        const select = __in(row, 'How the page is read');
        const labels = select.children.map(o => o.textContent);
        const help = () => row.find(e => e.attrs.id === select.attrs.id + '-help')[0].textContent;
        const first = help();
        __pick(select, 'tribe_events_api');
        const second = help();
        __type(__in(row, 'Name'), 'Town Calendar');
        __type(__in(row, 'Page address'), 'https://town.example.org/events');
        __type(__in(row, 'Link path'), '/events/');
        __type(__in(row, 'Regions'), 'Downtown\nUptown\n');
        __script = [__ok('community_events', __entry({sources: [{id: 'town-calendar', name: 'Town Calendar', type: 'tribe_events_api', url: 'https://town.example.org', url_masked: true}]}, {sources: true}, {revision: 4}))];
        __saveButton(box).click();
        await __settle();
        return {labels, first, second, sent: __lastPut().body.value.sources, saved: __text(box).includes('Saved. The events service reloads within about a minute.')};
        """
    )
    assert result["labels"] == [
        "Links on a page", "Event cards on a page", "WordPress Events Calendar", "Squarespace event list",
    ]
    assert "link path" in result["first"] and "Events Calendar" in result["second"]
    assert result["sent"] == [{
        "id": "town-calendar", "name": "Town Calendar", "type": "tribe_events_api",
        "url": "https://town.example.org/events", "link_path": "/events/", "regions": ["Downtown", "Uptown"],
    }]
    assert result["saved"] is True


def test_community_sources_show_the_three_list_states():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('community_events', __entry({sources: []}, {sources: true}))];
        await HouseholdSettings.mount('community_events', 'c1');
        return {none: __text(box).includes('None. Community events are off.'), add: __buttons(box, 'Add source').length, useServer: __buttons(box, "Use the server's setting").length};
        """
    )
    assert result == {"none": True, "add": 1, "useServer": 1}


def test_a_server_422_lands_on_the_row_and_field_and_never_echoes_the_path():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed({id: 'a', name: 'A'}), __feed({id: 'b', name: 'B'})], null)];
        await HouseholdSettings.mount('transit', 'c1');
        __type(__in(__row(box, 0), 'Name'), 'A2');
        __script = [{status: 422, body: {detail: [{loc: ['gtfs_feeds', 1, 'bounds', 'min_lat'], type: 'bounds_order'}, {loc: ['gtfs_feeds'], type: 'duplicate_id'}]}}];
        __saveButton(box).click();
        await __settle();
        const second = __row(box, 1);
        return {open: __rows(box).map(r => __edit(r).attrs['aria-expanded']), alerts: __alerts(box), echoed: __text(box).includes('min_lat') || __text(box).includes('gtfs_feeds')};
        """
    )
    assert result["open"] == ["false", "true"]
    assert "South must be below North, and West below East." in result["alerts"]
    assert "Another row already uses this id. Pick a different one." in result["alerts"]
    assert result["echoed"] is False


def test_a_client_error_inside_a_collapsed_row_opens_it_and_moves_focus_to_the_field():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed({id: 'a', name: 'A'}), __feed({id: 'b', name: 'B'})], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const bounds = __in(__row(box, 1), 'South (latitude)');
        __type(bounds, '40');
        __saveButton(box).click();
        await __settle();
        return {open: __rows(box).map(r => __edit(r).attrs['aria-expanded']), alerts: __alerts(box), focus: global.__focused === bounds, puts: __fetches.filter(f => f.method === 'PUT').length};
        """
    )
    assert result["open"] == ["false", "true"]
    assert "Fill in all four, or clear all four." in result["alerts"]
    assert result["focus"] is True and result["puts"] == 0


# --- states shared with the cards ---------------------------------------------------------------

def test_a_409_keeps_the_list_draft_and_reapplying_sends_the_new_revision():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        __type(__in(__row(box, 0), 'Name'), 'My edit');
        __script = [{status: 409, body: {detail: 'stale_revision'}}, __ok('transit', __entry({region_name: null, gtfs_feeds: [__feed({name: 'Theirs'})], static_services: null}, {gtfs_feeds: true}, {revision: 4}))];
        __saveButton(box).click();
        await __settle();
        const after = {name: __in(__row(box, 0), 'Name').value, reapply: __buttons(box, 'Re-apply my changes').length};
        __buttons(box, 'Re-apply my changes')[0].click();
        const reapplied = __in(__row(box, 0), 'Name').value;
        __script = [__transitPut([__feed({name: 'My edit'})], null, 5)];
        __saveButton(box).click();
        await __settle();
        const sent = __lastPut().body;
        return {after, reapplied, name: sent.value.gtfs_feeds[0], revision: sent.revision};
        """
    )
    assert result["after"] == {"name": "Theirs", "reapply": 1}
    assert result["reapplied"] == "My edit"
    assert result["revision"] == 4
    assert result["name"] == {"id": "mta-bus", "name": "My edit", "url": "https://api.example.org", "url_masked": True}


def test_a_read_only_user_can_open_rows_but_not_change_or_add_anything():
    result = scenario(
        r"""
        __perms = ['read'];
        const box = __box('c1');
        __script = [__transit([__feed()], null)];
        await HouseholdSettings.mount('transit', 'c1');
        const row = __row(box, 0);
        __edit(row).click();
        return {
          inputsDisabled: __inputs(row).every(i => i.disabled), editEnabled: !__edit(row).disabled, open: __edit(row).attrs['aria-expanded'],
          removeDisabled: __remove(row).disabled, addDisabled: __buttons(box, 'Add feed').every(b => b.disabled),
          note: __text(box).includes('You can view these settings. Changing them needs write access.'), saveHidden: !__visible(__saveButton(box)),
          replaceDisabled: __buttons(row, 'Replace URL')[0].disabled,
        };
        """
    )
    assert result == {
        "inputsDisabled": True, "editEnabled": True, "open": "true", "removeDisabled": True, "addDisabled": True,
        "note": True, "saveHidden": True, "replaceDisabled": True,
    }


def test_a_restricted_list_section_says_set_by_the_owner_or_not_set_here():
    result = scenario(
        r"""
        __perms = ['read', 'write'];
        const box = __box('c1');
        __script = [__ok('transit', {value: null, stored: {region_name: false, gtfs_feeds: true, static_services: false}, revision: 0, required_permission: 'write', writable: true, restricted: true})];
        await HouseholdSettings.mount('transit', 'c1');
        return {items: box.find(e => e.tagName === 'LI').map(e => e.textContent), inputs: __inputs(box).length};
        """
    )
    assert result["items"] == [
        "Region name: Not set here",
        "Transit feeds (GTFS): Set by the owner",
        "Fixed-route services: Not set here",
    ]
    assert result["inputs"] == 0


def test_the_dirty_marker_covers_list_edits_and_a_failed_load_offers_retry():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [{status: 500, body: {}}];
        await HouseholdSettings.mount('transit', 'c1');
        const failed = {alert: __alerts(box), retry: __buttons(box, 'Retry').length};
        __script = [__transit([__feed()], null)];
        __buttons(box, 'Retry')[0].click();
        await __settle();
        const marker = () => __visible(box.find(e => e.textContent === 'Unsaved changes')[0]);
        const clean = marker();
        __type(__in(__row(box, 0), 'Name'), 'x');
        const dirty = marker();
        const fetches = __fetches.length;
        await HouseholdSettings.mount('transit', 'c1');
        return {failed, clean, dirty, refetched: __fetches.length - fetches, kept: __in(__row(box, 0), 'Name').value};
        """
    )
    assert result["failed"] == {"alert": ["Couldn't load Transit."], "retry": 1}
    assert result["clean"] is False and result["dirty"] is True
    assert result["refetched"] == 0 and result["kept"] == "x"


# --- the lamp exclusion editor (a text list from the card renderer) ------------------------------

def test_the_lamp_exclusion_card_says_the_built_in_rule_always_applies_and_enforces_the_glob_rules():
    result = scenario(
        r"""
        const box = __box('c1');
        __script = [__ok('lights', __entry({room_light_exclude: ['light.garage_strip']}, {room_light_exclude: true}, {required_permission: 'manage_infrastructure'}))];
        await HouseholdSettings.mount('lights', 'c1');
        const text = __text(box);
        const input = () => __inputs(box)[0];
        const check = value => { __type(input(), value); input().fire('blur'); return __alerts(box); };
        const bad = {noPrefix: check('garage_strip'), leadingStar: check('light.*_ring'), onlyStar: check('*'), badChar: check('light.Garage'), tooMany: check('light.a*b*c*d*e'), ok: check('light.hallway_*')};
        __type(input(), 'light.*x');
        __script = [{status: 422, body: {detail: [{loc: ['room_light_exclude', 0], type: 'glob_too_broad'}]}}];
        __saveButton(box).click();
        await __settle();
        return {builtIn: text.includes('Whenever a list is saved here, Athena also skips voice-satellite LED rings and status LEDs'), replaces: text.includes("A list saved here replaces the server's list."), always: text.includes('always'), legend: text.includes('Server setting'), bad, server: __alerts(box)};
        """
    )
    assert result["builtIn"] is True and result["replaces"] is True
    assert result["always"] is False and result["legend"] is False, "no 'always' claim; no Server-setting legend on a list-only card"
    starts = "Start with light. and use only lowercase letters, numbers, _ and *."
    assert result["bad"]["noPrefix"] == [starts]
    assert result["bad"]["leadingStar"] == ["A pattern can't start with *. Name the start of the light, like light.garage_*."]
    assert result["bad"]["onlyStar"] == [starts]
    assert result["bad"]["badChar"] == [starts]
    assert result["bad"]["tooMany"] == ["Use at most 3 * in a pattern."]
    assert result["bad"]["ok"] == []
    assert "A pattern can't start with *. Name the start of the light, like light.garage_*." in result["server"]
