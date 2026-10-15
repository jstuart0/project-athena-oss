"""The Favourite teams card: its markup, how conversation.js builds and saves the
list, focus and announcements, and the escaping of the neighbouring card.

Structure is read from the files; behaviour runs conversation.js in Node over a
small fake DOM that throws if anything assigns innerHTML (team names are data).
"""
from __future__ import annotations

import ast
import json
import re

import pytest

from .frontend_node import FRONTEND, REPO, run_scripts

CONVERSATION = FRONTEND / "conversation.js"
INDEX = FRONTEND / "index.html"
MEMORY = FRONTEND / "memory-context.js"
ESCAPE = FRONTEND / "escape-html.js"

def league_catalog():
    """The league list the GET route serves, read from the model's own literals
    (this job installs no pydantic, so the module itself can't be imported)."""
    tree = ast.parse((REPO / "src" / "shared" / "favorite_teams.py").read_text(encoding="utf-8"))
    literals = {}
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id in {"LEAGUE_CODES", "LEAGUE_LABELS", "LEAGUE_SPORT"}:
            literals[target.id] = ast.literal_eval(node.value)
    codes, labels, sports = literals["LEAGUE_CODES"], literals["LEAGUE_LABELS"], literals["LEAGUE_SPORT"]
    assert len(codes) == 17
    return [{"code": c, "label": labels[c], "sport": sports[c]} for c in codes]


STALE_SENTENCE = (
    "Someone else changed this list while you were editing. Your changes weren't saved. "
    "The latest list is shown below; make your changes again."
)


def _skip_string(source: str, i: int) -> int:
    quote = source[i]
    i += 1
    while source[i] != quote:
        i += 2 if source[i] == "\\" else 1
    return i + 1


def _skip_template(source: str, i: int) -> int:
    i += 1
    while source[i] != "`":
        if source[i] == "\\":
            i += 2
        elif source.startswith("${", i):
            i = _skip_code(source, i + 2)
        else:
            i += 1
    return i + 1


def _skip_code(source: str, i: int) -> int:
    """Index just past the ``}`` that closes the block whose body starts at ``i``,
    skipping strings, template literals (and their ``${}``) and comments."""
    depth = 1
    while depth:
        two = source[i: i + 2]
        ch = source[i]
        if two == "//":
            i = source.index("\n", i)
        elif two == "/*":
            i = source.index("*/", i) + 2
        elif ch in "'\"":
            i = _skip_string(source, i)
        elif ch == "`":
            i = _skip_template(source, i)
        else:
            depth += (ch == "{") - (ch == "}")
            i += 1
    return i


def function_body(source: str, name: str) -> str:
    """The text of ``function name(...) { ... }``, brace-matched."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, f"{name} not found"
    start = source.index("{", match.end())
    return source[match.start(): _skip_code(source, start + 1)]


@pytest.fixture(scope="module")
def js_source():
    return CONVERSATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


# --- structure ----------------------------------------------------------------------------------

def tab_html(html):
    start = html.index('id="tab-conversation-context"')
    return html[start: html.index("<!-- Service Bypass Tab -->", start) if "<!-- Service Bypass Tab -->" in html[start:] else len(html)]


def test_the_favourite_teams_card_is_the_first_card_of_the_tab(html):
    tab = tab_html(html)
    first_card = tab.index('class="bg-dark-card')
    assert 'id="favorite-teams-card"' in tab[first_card - 40: first_card + 120]
    assert tab.index('id="favorite-teams-container"') < tab.index("General Settings")
    assert 'id="favorite-teams-container"' in tab
    status = re.search(r'<span id="favorite-teams-status"[^>]*>', tab).group(0)
    assert 'aria-live="polite"' in status and "sr-only" in status
    message = re.search(r'<p id="favorite-teams-message"[^>]*>', tab).group(0)
    assert 'role="alert"' in message
    assert 'id="favorite-teams-save"' in tab and "disabled" in re.search(r'<button[^>]*id="favorite-teams-save"[^>]*>', tab).group(0)
    assert "Unsaved changes" in tab
    name_input = re.search(r'<input[^>]*id="favorite-team-name"[^>]*>', tab).group(0)
    assert 'maxlength="64"' in name_input
    assert '<label for="favorite-team-name"' in tab and '<label for="favorite-team-league"' in tab
    assert "Pick a league if the name is shared, e.g. Giants" in tab
    assert "Any league" in tab
    assert "The teams Athena means by" in tab
    assert "Changes reach the assistant within a minute." in tab


def test_the_card_has_no_inline_handlers(html):
    start = html.index('id="favorite-teams-card"')
    end = html.index("<!-- Main Settings Card -->")
    assert not re.search(r"\son[a-z]+\s*=", html[start:end], re.IGNORECASE)


def test_the_old_card_is_renamed(html):
    tab = tab_html(html)
    assert "Ambiguous team names" in tab and "Sports Team Disambiguation" not in tab
    assert "Which team a shared name like" in tab


def test_the_new_card_precedes_the_ambiguous_names_card(html):
    tab = tab_html(html)
    assert tab.index("Favourite teams") < tab.index("Ambiguous team names")


def test_render_favorite_teams_builds_the_dom_with_no_markup_strings(js_source):
    body = function_body(js_source, "renderFavoriteTeams")
    assert "innerHTML" not in body and "insertAdjacentHTML" not in body and "outerHTML" not in body
    assert not re.search(r"""['"`][^'"`\n]*\bon[a-z]+\s*=""", body)
    for needed in ("textContent", "addEventListener", "createElement"):
        assert needed in body or needed in js_source
    assert "aria-label" in function_body(js_source, "favoriteButton")
    section = js_source[js_source.index("// FAVOURITE TEAMS"): js_source.index("// SPORTS TEAMS")]
    assert "innerHTML" not in section and "insertAdjacentHTML" not in section
    assert not re.search(r"""['"`][^'"`\n]*\bon[a-z]+\s*=""", section)


def test_the_page_saves_with_put_and_guards_unsaved_changes(js_source):
    assert re.search(r"/api/settings/favorite-teams", js_source)
    assert "method: 'PUT'" in function_body(js_source, "saveFavoriteTeams")
    assert STALE_SENTENCE in js_source
    assert "addEventListener('beforeunload'" in js_source
    assert "Authorization': `Bearer ${authToken}`" in function_body(js_source, "loadFavoriteTeams")
    assert "Authorization': `Bearer ${authToken}`" in function_body(js_source, "saveFavoriteTeams")


def test_load_favorite_teams_runs_outside_the_settings_try(js_source):
    body = function_body(js_source, "loadConversationSettings")
    call = body.index("loadFavoriteTeams()")
    assert call < body.index("try {")
    assert call < body.index("/api/conversation/settings")


REQUIRED_WRAPS = ("escapeHtml(team.team_name", "escapeHtml(opt.label || opt.id)", "escapeHtml(opt.sport)")


def sports_card_escaping_problems(body: str) -> list:
    problems = [f"missing {needle}" for needle in REQUIRED_WRAPS if needle not in body]
    # No field reaches the markup bare (the ternary's `${opt.sport ? ...}` only tests it).
    if re.search(r"\$\{\s*(?:team\.team_name|opt\.label|opt\.id|opt\.sport)\s*(?:\|\||\})", body):
        problems.append("bare interpolation")
    if re.search(r"\(\$\{\s*opt\.sport\s*\}\)", body):
        problems.append("bare sport")
    return problems


PLANTED_BASE_SPORTS_CARD = """function renderSportsTeams(teams) {
    container.innerHTML = teams.map(team => `<h4>${team.team_name || 'Unknown Team'}</h4>
        ${team.options.map(opt => `<span>${opt.label || opt.id} ${opt.sport ? `(${opt.sport})` : ''}</span>`).join('')}`);
}"""


def test_render_sports_teams_escapes_every_interpolated_field(js_source):
    assert sports_card_escaping_problems(function_body(js_source, "renderSportsTeams")) == []


def test_the_escaping_check_fails_on_the_unwrapped_base_text():
    problems = sports_card_escaping_problems(function_body(PLANTED_BASE_SPORTS_CARD, "renderSportsTeams"))
    assert len(problems) >= 4 and "bare interpolation" in problems and "bare sport" in problems


def test_memory_context_links_to_the_card_with_a_static_handler():
    source = MEMORY.read_text(encoding="utf-8")
    link = re.search(r'<button[^>]*id="knowledge-favorite-teams-link"[^>]*>[^<]*</button>', source)
    assert link and "Favourite teams" in link.group(0)
    assert "${" not in link.group(0) and "showTab('conversation-context')" in link.group(0)


def test_both_changed_scripts_have_a_new_cache_buster(html):
    assert "/conversation.js?v=20260913" not in html and "/memory-context.js?v=20260927b" not in html
    assert re.search(r'/conversation\.js\?v=\S+"', html) and re.search(r'/memory-context\.js\?v=\S+"', html)


# --- behaviour in Node ------------------------------------------------------------------------------

PRELUDE = r"""
'use strict';
class El {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {}; this._text = ''; this.className = ''; this.disabled = false; this.value = ''; this.title = ''; this.type = ''; this.label = ''; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { throw new Error('innerHTML assigned: ' + String(v).slice(0, 40)); }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { this.fire('click'); }
  fire(t, ev) { (this.listeners[t] || []).forEach(f => f(Object.assign({ key: '', preventDefault() { this.prevented = true; } }, ev || {}))); }
  focus() { global.__focused = this; }
  find(pred, out) { out = out || []; if (pred(this)) out.push(this); this.children.forEach(c => c.find(pred, out)); return out; }
}
const ids = ['favorite-teams-container', 'favorite-teams-message', 'favorite-teams-status', 'favorite-teams-save',
  'favorite-teams-dirty', 'favorite-team-name', 'favorite-team-league', 'favorite-team-add', 'favorite-team-name-error'];
global.__els = {};
ids.forEach(id => { global.__els[id] = new El('div'); });
global.__els['favorite-team-league'].tagName = 'SELECT';
global.document = { getElementById: id => global.__els[id] || null, createElement: tag => new El(tag) };
global.__fetches = [];
global.__script = [];
global.fetch = async (url, opts) => {
  opts = opts || {};
  global.__fetches.push({ url: String(url), method: opts.method || 'GET', body: opts.body, headers: opts.headers });
  if (String(url).includes('/api/settings/favorite-teams')) {
    const r = global.__script.length > 1 ? global.__script.shift() : global.__script[0];
    if (r instanceof Error) throw r;
    return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
global.__listeners = {};
global.addEventListener = (t, f) => { (global.__listeners[t] = global.__listeners[t] || []).push(f); };
global.__confirms = [];
global.__confirmAnswer = false;
global.confirm = msg => { global.__confirms.push(msg); return global.__confirmAnswer; };
global.__shown = [];
global.showTab = function (name) { global.__shown.push(name); return 'shown'; };
global.showError = () => {};
global.API_BASE = '';
global.authToken = 'test-token';
global.console.error = () => {};
const LEAGUES = __LEAGUES__;
global.__ok = (teams, revision) => ({ status: 200, body: { teams: teams, revision: revision, leagues: LEAGUES, max: 8 } });
"""


def scenario(body: str):
    prelude = PRELUDE.replace("__LEAGUES__", json.dumps(league_catalog()))
    return run_scripts([ESCAPE, CONVERSATION], f"(async () => {{ {body} }})()", prelude=prelude)


SNAPSHOT = r"""
const list = () => (__els['favorite-teams-container'].find(e => e.tagName === 'LI'));
const rows = () => list().map(li => ({
  position: li.children[0].textContent,
  name: li.children[1].children[0].textContent,
  league: li.children[1].children[1].textContent,
  buttons: li.children.slice(2).map(b => ({ text: b.textContent, label: b.getAttribute('aria-label'), disabled: b.disabled, title: b.title })),
}));
const btn = (row, kind) => list()[row].children[{ up: 2, down: 3, remove: 4 }[kind]];
const focusedLabel = () => (__focused ? (__focused.getAttribute ? __focused.getAttribute('aria-label') : null) : null);
"""


def test_name_problems():
    result = scenario(
        r"""
        const t = [{name: 'Anytown Owls', league: null}];
        return {
          ok: favoriteTeamProblem('Hawks', t, 8),
          empty: favoriteTeamProblem('   ', t, 8),
          rule: favoriteTeamProblem('<b>x</b>', t, 8),
          newline: favoriteTeamProblem('a\nb', t, 8),
          long: favoriteTeamProblem('x'.repeat(65), t, 8),
          max64: favoriteTeamProblem('x'.repeat(64), t, 8),
          dup: favoriteTeamProblem('anytown owls', t, 8),
          cap: favoriteTeamProblem('Crows', t, 1),
          accents: favoriteTeamProblem('Bayern München', t, 8),
          apostrophe: favoriteTeamProblem("St. Louis-Blues O'Neil", t, 8),
          normalized: normalizeFavoriteName('  München '),
        };
        """
    )
    rule = "Use letters, numbers, spaces and . - ' only"
    assert result == {
        "ok": "", "empty": "Enter a team name", "rule": rule, "newline": rule, "long": rule, "max64": "",
        "dup": "That team is already in the list", "cap": "Up to 1 teams", "accents": "", "apostrophe": "",
        "normalized": "München",
    }


def test_the_cap_message_reads_up_to_eight_teams():
    assert scenario("return favoriteTeamProblem('Crows', Array.from({length: 8}, (_, i) => ({name: 'T' + i})), 8);") == "Up to 8 teams"


def test_moving_teams():
    result = scenario(
        r"""
        const t = [{name: 'A'}, {name: 'B'}, {name: 'C'}];
        const names = (x) => x.map(e => e.name).join('');
        return [names(moveFavoriteTeam(t, 1, -1)), names(moveFavoriteTeam(t, 0, -1)), names(moveFavoriteTeam(t, 2, 1)),
                names(moveFavoriteTeam(t, 0, 1)), names(t)];
        """
    )
    assert result == ["BAC", "ABC", "ABC", "BAC", "ABC"]


def test_loading_renders_rows_with_labels_and_disabled_edges():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'Anytown Owls', league: 'nfl'}, {name: 'Hawks', league: null}, {name: 'Crows', league: 'mlb'}], 4));
        await loadFavoriteTeams();
        const select = __els['favorite-team-league'];
        const groups = select.children.filter(c => c.tagName === 'OPTGROUP');
        return {
          rows: rows(),
          view: favoriteTeamsState.view, revision: favoriteTeamsState.revision,
          firstOption: select.children[0].textContent,
          groupLabels: groups.map(g => g.label),
          optionCount: select.find(e => e.tagName === 'OPTION').length,
          international: select.find(e => e.tagName === 'OPTION' && e.value === 'international')[0].textContent,
          saveDisabled: __els['favorite-teams-save'].disabled,
          addDisabled: __els['favorite-team-add'].disabled,
          fetch: __fetches[0],
        };
        """
    )
    names = [(r["position"], r["name"], r["league"]) for r in result["rows"]]
    assert names == [("1", "Anytown Owls", "NFL"), ("2", "Hawks", "Any league"), ("3", "Crows", "MLB")]
    first, middle, last = (r["buttons"] for r in result["rows"])
    assert first[0]["disabled"] and not first[1]["disabled"]
    assert not middle[0]["disabled"] and not middle[1]["disabled"]
    assert last[1]["disabled"] and not last[0]["disabled"]
    assert first[0]["label"] == "Move Anytown Owls up" and first[1]["label"] == "Move Anytown Owls down"
    assert first[2]["label"] == "Remove Anytown Owls" and first[2]["title"] == "Remove Anytown Owls"
    assert result["view"] == "ready" and result["revision"] == 4
    assert result["firstOption"] == "Any league"
    assert result["groupLabels"] == ["Football", "Basketball", "Baseball", "Hockey", "Soccer"]
    assert result["optionCount"] == 18  # any league + the 17 leagues
    assert result["international"] == "International (national teams)"
    assert result["saveDisabled"] is True and result["addDisabled"] is False
    assert result["fetch"]["url"].endswith("/api/settings/favorite-teams")
    assert result["fetch"]["headers"]["Authorization"] == "Bearer test-token"


def test_a_name_is_text_never_markup():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: '<img src=x onerror=alert(1)>', league: null}], 1));
        await loadFavoriteTeams();
        const li = list()[0];
        return { name: rows()[0].name, nested: li.children[1].children[0].children.length, label: btn(0, 'remove').getAttribute('aria-label') };
        """
    )
    assert result == {"name": "<img src=x onerror=alert(1)>", "nested": 0, "label": "Remove <img src=x onerror=alert(1)>"}


def test_empty_list_and_error_and_forbidden_states():
    result = scenario(
        SNAPSHOT
        + r"""
        const text = () => __els['favorite-teams-container'].textContent;
        __script.push(__ok([], 0));
        await loadFavoriteTeams();
        const empty = text();
        __script.length = 0; __script.push({status: 500, body: {}});
        await loadFavoriteTeams(true);
        const error = text();
        const retry = __els['favorite-teams-container'].find(e => e.tagName === 'BUTTON')[0];
        __script.length = 0; __script.push(__ok([{name: 'Hawks', league: null}], 1));
        retry.click();
        await new Promise(r => setTimeout(r, 0));
        const afterRetry = rows().map(r => r.name);
        __script.length = 0; __script.push({status: 403, body: {}});
        await loadFavoriteTeams(true);
        return { empty, error, retryLabel: retry.textContent, afterRetry, forbidden: text(), addDisabled: __els['favorite-team-add'].disabled };
        """
    )
    assert result["empty"] == "No favourite teams yet. Add one below."
    assert result["error"].startswith("Couldn't load your favourite teams.") and result["retryLabel"] == "Retry"
    assert result["afterRetry"] == ["Hawks"]
    assert result["forbidden"] == "You don't have permission to view this setting"
    assert result["addDisabled"] is True


def test_adding_with_enter_validates_and_reports_the_broken_rule():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'Anytown Owls', league: 'nfl'}], 2));
        await loadFavoriteTeams();
        const input = __els['favorite-team-name'], select = __els['favorite-team-league'];
        const err = () => __els['favorite-team-name-error'].textContent;
        const enter = (v, league) => { input.value = v; select.value = league || ''; input.fire('keydown', { key: 'Enter' }); };
        enter('<b>x</b>'); const rule = err();
        enter('anytown owls'); const dup = err();
        input.fire('input'); const cleared = err();
        enter('Hawks', 'mlb');
        const after = { names: rows().map(r => r.name + '|' + r.league), input: input.value, err: err(), dirty: favoriteTeamsDirty(),
                        saveDisabled: __els['favorite-teams-save'].disabled, dirtyClass: __els['favorite-teams-dirty'].className,
                        status: __els['favorite-teams-status'].textContent, focus: __focused === input, invalid: input.getAttribute('aria-invalid') };
        return { rule, dup, cleared, after };
        """
    )
    assert result["rule"] == "Use letters, numbers, spaces and . - ' only"
    assert result["dup"] == "That team is already in the list" and result["cleared"] == ""
    after = result["after"]
    assert after["names"] == ["Anytown Owls|NFL", "Hawks|MLB"] and after["input"] == "" and after["err"] == ""
    assert after["dirty"] is True and after["saveDisabled"] is False and "hidden" not in after["dirtyClass"]
    assert after["status"] == "Hawks added at position 2" and after["focus"] is True and after["invalid"] == "false"


def test_the_ninth_team_is_refused():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok(Array.from({length: 8}, (_, i) => ({name: 'Team ' + i, league: null})), 1));
        await loadFavoriteTeams();
        __els['favorite-team-name'].value = 'Ninth';
        __els['favorite-team-add'].click();
        return { n: rows().length, err: __els['favorite-team-name-error'].textContent };
        """
    )
    assert result == {"n": 8, "err": "Up to 8 teams"}


def test_reordering_keeps_focus_on_the_moved_row_and_hands_off_a_disabled_arrow():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}, {name: 'B', league: null}, {name: 'C', league: null}], 1));
        await loadFavoriteTeams();
        btn(1, 'down').click();                       // B to the bottom: down is now disabled
        const afterDown = { order: rows().map(r => r.name).join(''), focus: focusedLabel(), status: __els['favorite-teams-status'].textContent };
        btn(2, 'up').click();                          // B back to the middle: same button keeps focus
        const afterUp = { order: rows().map(r => r.name).join(''), focus: focusedLabel() };
        btn(1, 'up').click(); btn(0, 'up');            // B to the top: up is disabled, focus moves to down
        return { afterDown, afterUp, top: { order: rows().map(r => r.name).join(''), focus: focusedLabel() } };
        """
    )
    assert result["afterDown"] == {"order": "ACB", "focus": "Move B up", "status": "B moved to position 3 of 3"}
    assert result["afterUp"] == {"order": "ABC", "focus": "Move B up"}
    assert result["top"] == {"order": "BAC", "focus": "Move B down"}


def test_removing_moves_focus_to_the_next_remove_then_the_previous_then_the_name_input():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}, {name: 'B', league: null}, {name: 'C', league: null}], 1));
        await loadFavoriteTeams();
        btn(1, 'remove').click(); const afterMiddle = { names: rows().map(r => r.name).join(''), focus: focusedLabel() };
        btn(1, 'remove').click(); const afterLast = { names: rows().map(r => r.name).join(''), focus: focusedLabel() };
        btn(0, 'remove').click();
        return { afterMiddle, afterLast, afterOnly: { n: rows().length, focusIsInput: __focused === __els['favorite-team-name'],
                 status: __els['favorite-teams-status'].textContent,
                 empty: __els['favorite-teams-container'].textContent } };
        """
    )
    assert result["afterMiddle"] == {"names": "AC", "focus": "Remove C"}
    assert result["afterLast"] == {"names": "A", "focus": "Remove A"}
    assert result["afterOnly"] == {"n": 0, "focusIsInput": True, "status": "A removed", "empty": "No favourite teams yet. Add one below."}


def test_saving_sends_the_list_and_revision_then_is_clean():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: 'nfl'}, {name: 'B', league: null}], 3));
        await loadFavoriteTeams();
        btn(1, 'up').click();
        __script.length = 0; __script.push(__ok([{name: 'B', league: null}, {name: 'A', league: 'nfl'}], 4));
        __els['favorite-teams-save'].click();
        await new Promise(r => setTimeout(r, 0));
        const put = __fetches.filter(f => f.method === 'PUT')[0];
        return { body: JSON.parse(put.body), headers: put.headers, names: rows().map(r => r.name).join(''), revision: favoriteTeamsState.revision,
                 dirty: favoriteTeamsDirty(), message: __els['favorite-teams-message'].textContent, saveDisabled: __els['favorite-teams-save'].disabled,
                 saveText: __els['favorite-teams-save'].textContent };
        """
    )
    assert result["body"] == {"teams": [{"name": "B", "league": None}, {"name": "A", "league": "nfl"}], "revision": 3}
    assert result["headers"]["Authorization"] == "Bearer test-token" and result["headers"]["Content-Type"] == "application/json"
    assert result["names"] == "BA" and result["revision"] == 4 and result["dirty"] is False
    assert result["message"] == "Saved. Changes reach the assistant within a minute."
    assert result["saveDisabled"] is True and result["saveText"] == "Save"


def test_a_conflict_reloads_the_latest_list_and_says_why():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}], 3));
        await loadFavoriteTeams();
        __els['favorite-team-name'].value = 'Mine'; __els['favorite-team-add'].click();
        __script.length = 0;
        __script.push({status: 409, body: {detail: 'stale_revision'}}, __ok([{name: 'Theirs', league: null}], 5));
        __els['favorite-teams-save'].click();
        await new Promise(r => setTimeout(r, 5));
        return { names: rows().map(r => r.name), revision: favoriteTeamsState.revision, dirty: favoriteTeamsDirty(),
                 message: __els['favorite-teams-message'].textContent, cls: __els['favorite-teams-message'].className };
        """
    )
    assert result["names"] == ["Theirs"] and result["revision"] == 5 and result["dirty"] is False
    assert result["message"] == STALE_SENTENCE and "text-red-400" in result["cls"]


def test_a_rejected_name_names_the_rule_and_keeps_the_edits():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}], 1));
        await loadFavoriteTeams();
        __els['favorite-team-name'].value = 'Mine'; __els['favorite-team-add'].click();
        __script.length = 0; __script.push({status: 422, body: {}});
        __els['favorite-teams-save'].click();
        await new Promise(r => setTimeout(r, 0));
        return { names: rows().map(r => r.name), dirty: favoriteTeamsDirty(), message: __els['favorite-teams-message'].textContent,
                 saveDisabled: __els['favorite-teams-save'].disabled };
        """
    )
    assert result["names"] == ["A", "Mine"] and result["dirty"] is True and result["saveDisabled"] is False
    assert "letters, numbers, spaces and . - ' only" in result["message"] and "up to 8 teams" in result["message"]


def test_a_network_failure_on_save_keeps_the_edits():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}], 1));
        await loadFavoriteTeams();
        btn(0, 'remove').click();
        __script.length = 0; __script.push(new Error('offline'));
        __els['favorite-teams-save'].click();
        await new Promise(r => setTimeout(r, 0));
        return { dirty: favoriteTeamsDirty(), message: __els['favorite-teams-message'].textContent, saveDisabled: __els['favorite-teams-save'].disabled };
        """
    )
    assert result["dirty"] is True and result["saveDisabled"] is False
    assert result["message"].startswith("Couldn't save your favourite teams.")


def test_a_refresh_keeps_unsaved_edits_but_retry_after_a_conflict_replaces_them():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}], 1));
        await loadFavoriteTeams();
        btn(0, 'remove').click();
        const before = __fetches.length;
        await loadFavoriteTeams();                    // the page's Refresh
        const refreshFetched = __fetches.length - before;
        await loadFavoriteTeams(true);
        return { refreshFetched, names: rows().map(r => r.name) };
        """
    )
    assert result == {"refreshFetched": 0, "names": ["A"]}


def test_leaving_with_unsaved_changes_asks_first_and_clean_leaving_does_not():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}], 1));
        await loadFavoriteTeams();
        const unload = () => { const ev = { prevented: false, preventDefault() { this.prevented = true; } }; __listeners.beforeunload.forEach(f => f(ev)); return ev.prevented; };
        const cleanUnload = unload();
        const cleanSwitch = showTab('dashboard');
        btn(0, 'remove').click();
        const dirtyUnload = unload();
        __confirmAnswer = false;
        const declined = showTab('dashboard');
        const sameTab = showTab('conversation-context');
        __confirmAnswer = true;
        const accepted = showTab('guests');
        return { cleanUnload, cleanSwitch, dirtyUnload, declined: declined === undefined, sameTab, accepted,
                 shown: __shown, confirms: __confirms };
        """
    )
    assert result["cleanUnload"] is False and result["cleanSwitch"] == "shown"
    assert result["dirtyUnload"] is True
    assert result["declined"] is True and result["sameTab"] == "shown" and result["accepted"] == "shown"
    assert result["shown"] == ["dashboard", "conversation-context", "guests"]
    assert result["confirms"] == ["You have unsaved changes to Favourite teams. Leave without saving?"] * 2


def test_the_settings_loader_starts_favourites_first_and_neither_failure_hides_the_other():
    result = scenario(
        SNAPSHOT
        + r"""
        __script.push(__ok([{name: 'A', league: null}], 1));
        await loadConversationSettings();             // the settings request is a 404 here
        await new Promise(r => setTimeout(r, 5));
        return { first: __fetches[0].url, second: __fetches[1].url, view: favoriteTeamsState.view, names: rows().map(r => r.name) };
        """
    )
    assert result["first"].endswith("/api/settings/favorite-teams")
    assert result["second"].endswith("/api/conversation/settings")
    assert result["view"] == "ready" and result["names"] == ["A"]

    forbidden = scenario(
        r"""
        __script.push({status: 403, body: {}});
        await loadConversationSettings();
        await new Promise(r => setTimeout(r, 5));
        return { view: favoriteTeamsState.view, settingsRequested: __fetches.some(f => f.url.endsWith('/api/conversation/settings')) };
        """
    )
    assert forbidden == {"view": "forbidden", "settingsRequested": True}
