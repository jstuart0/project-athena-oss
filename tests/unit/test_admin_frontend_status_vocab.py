"""status-vocab.js: the one status vocabulary of the admin panel.

Pins the table of status words (labels and tones), the copy of the Home
Assistant card, the neutral/setup words against the backend's own sets, and a
structural drift scan: no page may carry a second copy of any of it.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from .frontend_node import FRONTEND, REPO, run_scripts

VOCAB = FRONTEND / "status-vocab.js"
ESCAPE = FRONTEND / "escape-html.js"
NOW = "Date.parse('2026-10-11T12:00:00+00:00')"
CHECKED_AT = "2026-10-11T12:00:00+00:00"


def js(expression: str, **kwargs):
    return run_scripts([ESCAPE, VOCAB], expression, **kwargs)


# -- the status table (ruby R11) ------------------------------------------------------------

R11 = {
    **{w: ("Healthy", "good") for w in ("healthy", "running", "healthy (auth required)", "running (auth required)")},
    "unhealthy": ("Unhealthy", "bad"),
    "degraded": ("Degraded", "bad"),
    "unknown": ("Unknown", "bad"),
    "ssrf_blocked": ("Blocked by allowlist", "bad"),
    "unconfigured": ("Needs setup", "setup"),
    "not configured": ("Needs setup", "setup"),
    "pending": ("Checking…", "neutral"),
    "stale": ("No recent check", "neutral"),
    "disabled": ("Not monitored", "neutral"),
    "unreachable": ("Unreachable", "offline"),
    "not deployed": ("Unreachable", "offline"),
    "timeout": ("Not responding", "offline"),
    "error": ("Error", "offline"),
    "error: HTTP 503": ("Error", "offline"),
    "error:anything": ("Error", "offline"),
}


@pytest.mark.parametrize("word,expected", sorted(R11.items()))
def test_every_word_has_its_label_and_tone(word, expected):
    info = js(f"statusInfo({json.dumps(word)})")
    assert (info["label"], info["tone"], info["raw"]) == (*expected, word)


def test_a_missing_entry_is_not_reported_and_neutral():
    for missing in ("null", "undefined"):
        assert js(f"statusInfo({missing})") == {"label": "Not reported", "tone": "neutral", "raw": None}


@pytest.mark.parametrize("word", ["somethingnew", "<img src=x onerror=alert(1)>", "__proto__", "constructor", "toString", "hasOwnProperty", "valueOf", "Error: x", "ERROR"])
def test_an_unknown_word_keeps_its_own_text_as_the_label_and_reads_as_a_problem(word):
    info = js(f"statusInfo({json.dumps(word)})")
    assert (info["label"], info["tone"], info["raw"]) == (word, "bad", word)


def test_the_tables_are_closed_and_only_problems_pulse():
    assert js("Object.keys(TONE_DOT).sort()") == ["bad", "good", "neutral", "offline", "setup"]
    assert js("TONE_DOT.good + ' ' + TONE_TEXT.setup + ' ' + TONE_TILE.setup + ' ' + TONE_TOAST.neutral") == \
        "bg-green-500 text-amber-400 amber info"
    assert js("Object.keys(TONE_PULSE).sort()") == ["bad", "offline"]
    assert set(js("Object.values(TONE_PULSE)")) == {"motion-safe:animate-pulse"}
    assert js("[TONE_TOAST.good, TONE_TOAST.bad, TONE_TOAST.offline, TONE_TOAST.setup, TONE_TOAST.neutral]") == \
        ["success", "error", "error", "warning", "info"]
    assert js("TONE_DOT.constructor === undefined && TONE_TEXT.__proto__ === undefined")


def test_the_registry_helper_answers_null_outside_the_registrys_words():
    assert js("registryHealthInfo('stale').label") == "No recent check"
    assert js("registryHealthInfo(null).label") == "Checking…"
    assert js("registryHealthInfo(undefined).tone") == "neutral"
    for word in ("disabled", "running", "error", "weird", "__proto__"):
        assert js(f"registryHealthInfo({json.dumps(word)})") is None


# -- the neutral pin (ruby R1): against the backend's own sets --------------------------------------

SERVICE_STATE_PY = REPO / "admin" / "backend" / "app" / "utils" / "service_state.py"
HEALTH_POLLER_PY = REPO / "admin" / "backend" / "app" / "services" / "health_poller.py"
HA_STATUS_PY = REPO / "admin" / "backend" / "app" / "services" / "ha_status.py"
HA_LINK_PY = REPO / "src" / "shared" / "ha_link.py"


def _module_assign(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} not found in {path}")


def _strings(node) -> set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_the_neutral_and_setup_words_are_the_backends_not_checked_set_plus_one_fallback():
    not_checked = _strings(_module_assign(SERVICE_STATE_PY, "NOT_CHECKED_STATUSES"))
    assert not_checked == {"disabled", "pending", "stale", "unconfigured"}
    # The registry's display words: the strings service_state.py decides between,
    # and the poller's own "reachable but not configured" word.
    registry_words = _strings(ast.parse(SERVICE_STATE_PY.read_text(encoding="utf-8")))
    assert "'unconfigured'" in HEALTH_POLLER_PY.read_text(encoding="utf-8") or '"unconfigured"' in HEALTH_POLLER_PY.read_text(encoding="utf-8")
    registry_words |= {"unconfigured"}
    quiet = {w for w in R11 if R11[w][1] in ("neutral", "setup")}
    assert quiet == {"unconfigured", "not configured", "pending", "stale", "disabled"}
    assert js("Object.keys(SERVICE_STATUS).filter((w) => ['neutral', 'setup'].includes(SERVICE_STATUS[w].tone)).sort()") == sorted(quiet)
    assert quiet & registry_words == not_checked
    # "not configured" is what /api/status says for Ollama and SearXNG when unset: a fallback word, not a registry word.
    assert quiet - not_checked == {"not configured"}


# -- the Home Assistant card's vocabularies ------------------------------------------------------------

def test_the_reason_copy_keys_are_exactly_the_backends_sixteen():
    reasons = {el.value for el in _module_assign(HA_STATUS_PY, "REASONS").args[0].elts}
    assert len(reasons) == 16 and "ip_banned" in reasons
    assert set(js("Object.keys(HA_REASON_COPY)")) == reasons


def test_the_caller_labels_are_the_link_callers_in_order():
    callers = [el.value for el in _module_assign(HA_LINK_PY, "CALLERS").elts]
    assert js("Object.keys(HA_CALLER_LABELS)") == callers
    assert js("Object.values(HA_CALLER_LABELS)") == [
        "voice commands", "room list", "voice satellites", "speaker room detection", "admin panel", "voice pipeline settings",
    ]


def summarize(**changes):
    base = {"state": "failing", "reason": "token_rejected", "areas": None, "last_outcome_at": "2026-10-11T11:58:00+00:00",
            "last_success_at": "2026-10-11T09:00:00+00:00", "checked_at": CHECKED_AT, "coverage": "cluster", "gateway": "ok"}
    base.update(changes)
    return js(f"summarizeHomeAssistant({json.dumps(base)})")


SUFFIX_FIRST = "Athena has stopped using this token so Home Assistant doesn't block its address."
BASE_DETAIL = "Home Assistant turned down Athena's access token, so Athena can't control your home."


@pytest.mark.parametrize("next_retry,second", [
    (None, "It won't try again until you save a new token."),
    ("not a time", "It will try again later on its own."),
    ("2026-10-11T11:59:00+00:00", "It will try again the next time it needs Home Assistant."),
    ("2026-10-11T12:00:00+00:00", "It will try again the next time it needs Home Assistant."),
    ("2026-10-11T12:30:00+00:00", "It will try again after 12:30 pm."),
    ("2026-10-12T02:15:00+00:00", "It will try again tomorrow after 2:15 am."),
    ("2026-10-13T08:00:00+00:00", "It will try again after 8:00 am on 13 Oct."),
])
def test_the_withheld_suffix_is_the_first_matching_row(next_retry, second):
    out = summarize(token_withheld=True, next_retry_at=next_retry)
    assert out["detail"] == f"{BASE_DETAIL} {SUFFIX_FIRST} {second}"


def test_the_suffix_appears_only_for_a_withheld_token_rejected():
    assert summarize()["detail"] == BASE_DETAIL
    assert summarize(token_withheld=False, next_retry_at=None)["detail"] == BASE_DETAIL
    assert summarize(token_withheld="true")["detail"] == BASE_DETAIL
    other = summarize(reason="ip_banned", token_withheld=True)
    assert SUFFIX_FIRST not in other["detail"]


def _plain(html):
    return re.sub(r"<[^>]+>", "", html)


def test_the_time_part_names_a_block_blocked_and_a_rejection_rejected():
    assert _plain(summarize(reason="ip_banned")["meta"]).startswith("Blocked 2m ago")
    assert _plain(summarize()["meta"]).startswith("Rejected 2m ago")
    assert _plain(summarize(reason="timeout")["meta"]).startswith("Seen 2m ago")


FAILURES = "Failed sign-ins: {} Home Assistant can block Athena's address if these keep growing."


def test_the_failures_line_rules():
    out = summarize(auth_failures=[
        {"caller": "admin_ws", "count": 1}, {"caller": "orchestrator_rest", "count": 2},
        {"caller": "intruder", "count": 5}, {"caller": "gateway_rest", "count": 0},
        {"caller": "gateway_registry", "count": -1}, {"caller": "admin_rest", "count": "3"},
        {"caller": "orchestrator_registry", "count": 1.5}, {"caller": "orchestrator_registry", "count": 4},
    ])
    assert out["failures"] == FAILURES.format("voice commands 2, room list 4, voice pipeline settings 1.")
    for none in ([], None, "x", [{"caller": "x", "count": 1}], [{"caller": "admin_ws", "count": 0}], [None]):
        assert summarize(auth_failures=none)["failures"] == ""
    assert summarize()["failures"] == ""
    assert "5" not in FAILURES, "no threshold number is stated"


def test_the_card_puts_the_action_in_a_div_the_failures_on_their_own_line_and_the_meta_in_grey_400():
    base = {"state": "failing", "reason": "ip_banned", "last_outcome_at": "2026-10-11T11:58:00+00:00",
            "checked_at": CHECKED_AT, "auth_failures": [{"caller": "admin_rest", "count": 2}]}
    html = js(f"renderHomeAssistantCard(summarizeHomeAssistant({json.dumps(base)}))")
    assert '<div class="mt-2 text-sm text-gray-300"><ol class="list-decimal' in html
    assert '<p class="mt-2 text-xs text-gray-400">Failed sign-ins: admin panel 2. ' in html
    assert 'class="text-xs text-gray-400 md:text-right shrink-0"' in html and "text-gray-500" not in html
    assert 'id="ha-status-heading"' in html


def test_the_setup_button_and_the_failure_try_again_are_touch_sized():
    assert "min-h-[44px]" in js("HA_SETUP_BUTTON")
    assert "min-h-[44px]" in js("renderHomeAssistantFailure('dashboard')")


@pytest.mark.parametrize("surface,heading,retry", [
    ("dashboard", "ha-status-heading", "loadStatus()"),
    ("mission", "mc-ha-status-heading", "Athena.pages.MissionControl.refresh()"),
    ("nope", "ha-status-heading", "loadStatus()"),
    ("__proto__", "ha-status-heading", "loadStatus()"),
    ("constructor", "ha-status-heading", "loadStatus()"),
    (None, "ha-status-heading", "loadStatus()"),
])
def test_a_surface_picks_its_heading_and_retry_and_an_unknown_one_is_the_dashboard(surface, heading, retry):
    arg = json.dumps(surface)
    failure = js(f"renderHomeAssistantFailure({arg})")
    card = js(f"renderHomeAssistantCard(summarizeHomeAssistant({{state: 'connected', reason: 'ok'}}), {arg})")
    assert f'id="{heading}"' in failure and f'id="{heading}"' in card
    assert f'onclick="{retry}"' in failure


def test_the_surfaces_are_exactly_the_two_closed_entries():
    assert js("Object.keys(HA_SURFACES)") == ["dashboard", "mission"]


def test_the_loading_skeleton_has_no_live_region_beyond_its_status_role_label():
    html = js("renderHomeAssistantLoading()")
    assert "aria-live" not in html


def test_a_hostile_failure_entry_or_time_is_never_echoed_as_markup():
    out = summarize(auth_failures=[{"caller": "<img src=x>", "count": 3}], last_outcome_at='"><img src=x>', next_retry_at="<img>", token_withheld=True)
    assert "<img" not in json.dumps(out)


# -- ages ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,iso,expected", [
    (False, "2026-10-11T11:48:00+00:00", "12m ago"),
    (True, "2026-10-11T11:48:00+00:00", "12 minutes ago"),
    (True, "2026-10-11T11:59:00+00:00", "1 minute ago"),
    (False, "2026-10-11T08:00:00+00:00", "4h ago"),
    (True, "2026-10-11T08:00:00+00:00", "4 hours ago"),
    (False, "2026-09-27T08:00:00+00:00", "27 Sep"),
    (True, "2026-09-27T08:00:00+00:00", "27 September"),
    (False, "2025-09-27T08:00:00+00:00", "27 Sep 2025"),
    (False, "2026-10-11T11:59:30+00:00", "just now"),
])
def test_age_words(spoken, iso, expected):
    assert js(f"ageWords({json.dumps(iso)}, {NOW}, {json.dumps(spoken)})") == expected


def test_age_words_and_since_words_return_null_for_an_unreadable_time():
    assert js(f"ageWords('yesterday', {NOW}, false)") is None
    assert js(f"sinceWords('yesterday', {NOW})") is None


def test_the_poller_banner_wording():
    html = js(f"stalePollerBanner('2026-10-11T09:14:00+00:00', {NOW}, {{spacing: 'mb-4'}})")
    assert 'role="status"' in html and " mb-4\"" in html
    assert "Health checks haven't run since 09:14. The statuses below may be out of date." in html
    assert "Health checks haven't run yet." in js(f"stalePollerBanner(null, {NOW}, {{}})")
    assert "since 27 Sep." in js(f"stalePollerBanner('2026-09-27T09:02:00+00:00', {NOW}, {{}})")
    assert 'p-4"' in js(f"stalePollerBanner(null, {NOW}, {{}})")


def test_not_monitored_text_is_built_from_the_disabled_label():
    assert js(f"notMonitoredText(null, {NOW})") == "Not monitored · never checked"
    assert js(f"notMonitoredText('2026-10-11T11:48:00+00:00', {NOW})") == "Not monitored · last checked 12m ago"


# -- drift: no page carries a second copy (ruby R12, tessa T7) -----------------------------------------------

MOVED_FUNCTIONS = ("statusLabel", "statusTone", "statusWord", "statusInfo", "notMonitoredText", "sinceWords",
                   "stalePollerBanner", "formatAge", "ageWords", "registryHealthInfo")
TOP_LEVEL_FUNCTION = re.compile(r"^(?:async\s+)?function\s+(%s|renderHomeAssistant\w*|summarizeHomeAssistant)\b" % "|".join(MOVED_FUNCTIONS), re.M)
TOP_LEVEL_CONST = re.compile(r"^(?:const|let|var)\s+(STATUS_\w+|NEUTRAL_STATUSES|SERVICE_STATUS|HA_\w+|TONE_\w+)\b", re.M)
STATUS_WORDS = ("healthy", "unhealthy", "degraded", "unconfigured", "disabled", "stale", "unreachable")
KEY_OR_CASE = re.compile(r"""(?:\bcase\s+)?['"]?\b(%s)\b['"]?\s*:""" % "|".join(STATUS_WORDS))
COPY_PHRASES = re.compile(r"(No recent check|Not monitored|Needs setup|Checking…)")


def frontend_scripts():
    files = sorted(FRONTEND.glob("*.js"))
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


def inline_scripts(html: str):
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)]


def drift_a(name, text):
    return [(name, m.group(1)) for m in TOP_LEVEL_FUNCTION.finditer(text)] + [(name, m.group(1)) for m in TOP_LEVEL_CONST.finditer(text)]


def drift_b(name, text, window=500):
    hits = []
    for m in KEY_OR_CASE.finditer(text):
        around = {w.group(1) for w in KEY_OR_CASE.finditer(text[m.start(): m.start() + window])}
        if len(around) >= 3:
            hits.append((name, m.start()))
            break
    return hits


def drift_c(name, text):
    return [(name, m.group(1)) for m in COPY_PHRASES.finditer(text)]


def test_no_page_redeclares_the_moved_functions_or_tables():
    found = [hit for name, text in frontend_scripts() if name != "status-vocab.js" for hit in drift_a(name, text)]
    assert found == []


def test_no_page_has_an_object_literal_or_switch_keyed_by_three_status_words():
    found = [hit for name, text in frontend_scripts() if name != "status-vocab.js" for hit in drift_b(name, text)]
    assert found == []


def test_the_status_phrases_appear_only_in_the_vocabulary():
    found = [hit for name, text in frontend_scripts() if name != "status-vocab.js" for hit in drift_c(name, text)]
    assert found == []


def test_index_html_inline_scripts_are_scanned_too():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    blocks = inline_scripts(html)
    assert blocks, "the scan needs at least one inline script to be meaningful"
    found = [hit for block in blocks for hit in drift_a("index.html", block) + drift_b("index.html", block) + drift_c("index.html", block)]
    assert found == []


def test_the_vocabulary_has_everything_the_scans_look_for():
    text = VOCAB.read_text(encoding="utf-8")
    declared = {m.group(1) for m in TOP_LEVEL_FUNCTION.finditer(text)} | {m.group(1) for m in TOP_LEVEL_CONST.finditer(text)}
    assert {"statusInfo", "ageWords", "formatAge", "sinceWords", "notMonitoredText", "stalePollerBanner", "summarizeHomeAssistant",
            "renderHomeAssistantCard", "renderHomeAssistantLoading", "renderHomeAssistantFailure",
            "SERVICE_STATUS", "TONE_DOT", "TONE_TEXT", "TONE_TILE", "TONE_TOAST", "HA_REASON_COPY", "HA_SURFACES"} <= declared
    assert {m.group(1) for m in COPY_PHRASES.finditer(text)} == {"No recent check", "Not monitored", "Needs setup", "Checking…"}


@pytest.mark.parametrize("planted,scan", [
    ("function statusLabel(s) { return s; }\n", drift_a),
    ("async function renderHomeAssistantCard(s) { return ''; }\n", drift_a),
    ("const STATUS_LABELS = { a: 1 };\n", drift_a),
    ("const NEUTRAL_STATUSES = ['stale'];\n", drift_a),
    ("const HA_COPY_TWO = {};\n", drift_a),
    ("const x = { healthy: 'a', stale: 'b', disabled: 'c' };\n", drift_b),
    ("switch (s) { case 'healthy': break; case 'unreachable': break; case 'degraded': break; }\n", drift_b),
    ("const label = 'Not monitored · never checked';\n", drift_c),
    ("const t = `No recent check`;\n", drift_c),
])
def test_each_detector_finds_its_planted_copy(planted, scan):
    assert scan("planted.js", planted)


@pytest.mark.parametrize("fine,scan", [
    ("  function statusTone(service) { return 1; }\n", drift_a),   # inside an IIFE: indented
    ("const ANALYTICS_STATUS_ON = 'x';\n", drift_a),
    ("const x = { healthy: 'a', stale: 'b' };\n", drift_b),
    ("const label = 'Offline';\n", drift_c),
])
def test_the_detectors_do_not_flag_what_is_not_a_copy(fine, scan):
    assert scan("fine.js", fine) == []


# -- the pages are adapters over the vocabulary ---------------------------------------------------------------

@pytest.mark.parametrize("page,needles", [
    ("app.js", ["statusInfo(serviceStatus(service))", "TONE_DOT[info.tone]", "TONE_TEXT[info.tone]", "notMonitoredText(", "stalePollerBanner(",
                "renderHomeAssistantCard(summarizeHomeAssistant(haResult.value))"]),
    ("service-control.js", ["registryHealthInfo(row.health_status)", "TONE_TEXT[info.tone]", "TONE_TOAST[to.tone]",
                            "${f.name}: ${statusInfo(f.from).label} → ${to.label}", "notMonitoredText(", "stalePollerBanner("]),
    ("mission-control.js", ["TONE_TILE[info.tone]", "TONE_PULSE[info.tone]", "renderHomeAssistantCard(summarizeHomeAssistant(haResult.value), 'mission')",
                            "renderHomeAssistantFailure('mission')"]),
    ("system-config.js", ["statusInfo(word).tone", "serviceCardLabel(", "${escapeHtml(svc.name)}"]),
    ("voice-pipelines.js", ["statusInfo(service.status || 'not configured').tone", "TONE_TILE[tone]", "escapeHtml(statusText)"]),
    ("voice-config.js", ["summarizeHomeAssistant(haStatusPayload)", "Promise.allSettled"]),
])
def test_each_page_reads_the_shared_vocabulary(page, needles):
    text = (FRONTEND / page).read_text(encoding="utf-8")
    missing = [needle for needle in needles if needle not in text]
    assert missing == []


def test_the_vocabulary_is_loaded_before_every_page_that_uses_it_and_is_copied_into_the_image():
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r'<script src="/([^"?]+)', index)
    for page in ("mission-control.js", "voice-pipelines.js", "app.js", "service-control.js", "system-config.js", "voice-config.js"):
        assert tags.index("status-vocab.js") < tags.index(page)
    assert tags.index("utils.js") < tags.index("status-vocab.js")
    assert "COPY status-vocab.js /usr/share/nginx/html/" in (FRONTEND / "Dockerfile").read_text(encoding="utf-8")
