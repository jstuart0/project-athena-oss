"""Voice Config's Home Assistant card header and pipeline body.

Loads status-vocab.js and voice-config.js into one Node context. The header is
the same dot and headline the Dashboard shows; the body when the pipelines
can't be loaded depends on what the link status says; the mode options are
buttons.
"""
from __future__ import annotations

import json
import re

import pytest

from .frontend_node import DOM_STUB, FRONTEND, requires_node, run_scripts

pytestmark = requires_node

ESCAPE = FRONTEND / "escape-html.js"
VOCAB = FRONTEND / "status-vocab.js"
VOICE_CONFIG = FRONTEND / "voice-config.js"

PRELUDE = DOM_STUB + """
const __section = { innerHTML: '' };
global.document = Object.assign(Object.create(null), {
    getElementById: (id) => (id === 'pipeline-mode-section' ? __section : null),
    querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, createElement: () => ({ style: {} }),
    head: { appendChild() {} }, body: { appendChild() {} },
});
global.getAuthHeaders = () => ({});
global.__section = __section;
"""

LEAD = "Couldn't load voice pipelines from Home Assistant."
TRY_AGAIN = '<button type="button" onclick="loadHAPipelineConfig()" class="mt-3 min-h-[44px] px-3 py-2 text-sm bg-gray-700 hover:bg-gray-600 text-white rounded">Try again</button>'
ASSISTANTS = ("Home Assistant is connected, so the problem is with its voice assistants. Check that at least one assistant is set up in "
              "Home Assistant under Settings → Voice assistants, then try again.")


def render(status, pipelines_ok=True, mode="full"):
    """The section's HTML after loadHAPipelineConfig, with the three requests stubbed."""
    script = f"""
        const status = {json.dumps(status)};
        global.fetch = async (url) => {{
            const answer = (ok, body) => ({{ ok, status: ok ? 200 : 502, json: async () => body }});
            if (url === '/api/service-registry/home-assistant') return status === null ? answer(false, {{detail: 'boom <img src=x>'}}) : answer(true, status);
            if (url === '/api/ha-pipelines/pipelines') return {json.dumps(pipelines_ok)} ? answer(true, {{pipelines: [{{id: 'p1', name: 'Ollama <b>', conversation_engine: 'conversation.ollama_conversation', is_preferred: true}}]}}) : answer(false, {{detail: 'secret detail <img src=x> HA_TOKEN'}});
            if (url === '/api/ha-pipelines/pipelines/preferred') return answer(true, {{current_mode: {json.dumps(mode)}}});
            throw new Error('unexpected ' + url);
        }};
        await loadHAPipelineConfig();
        return __section.innerHTML;
    """
    return run_scripts([ESCAPE, VOCAB, VOICE_CONFIG], "(async () => {" + script + "})()", prelude=PRELUDE)


def plain(html):
    return re.sub(r"<[^>]+>", "", html)


CONNECTED = {"state": "connected", "reason": "ok", "areas": 9, "checked_at": "2026-10-11T12:00:00+00:00",
             "last_outcome_at": "2026-10-11T11:58:00+00:00", "last_success_at": "2026-10-11T11:58:00+00:00", "coverage": "cluster", "gateway": "ok"}
REJECTED = {"state": "failing", "reason": "token_rejected", "checked_at": "2026-10-11T12:00:00+00:00", "token_withheld": True, "next_retry_at": None}
NOT_SET_UP = {"state": "not_configured", "reason": "not_configured", "checked_at": "2026-10-11T12:00:00+00:00"}


def test_the_header_is_the_dashboards_dot_and_headline():
    html = render(CONNECTED)
    assert '<span class="w-2 h-2 rounded-full bg-green-500" aria-hidden="true"></span><span class="text-sm font-medium text-green-400">Connected</span>' in html
    assert "health-indicator" not in html and "status-inactive" not in html and "service-status" not in html


def test_the_header_follows_the_link_state_not_the_pipeline_request():
    html = render(REJECTED, pipelines_ok=True)
    assert 'text-red-400">Token rejected</span>' in html
    assert "Choose your voice pipeline mode:" in html, "the pipelines still show when only the link is unhealthy"


def test_a_failed_status_request_reads_grey_status_unavailable_and_the_pipelines_still_show():
    html = render(None, pipelines_ok=True)
    assert '<span class="w-2 h-2 rounded-full bg-gray-500" aria-hidden="true"></span><span class="text-sm font-medium text-gray-400">Status unavailable</span>' in html
    assert "Choose your voice pipeline mode:" in html


def test_pipelines_failed_while_the_token_is_rejected_shows_the_detail_and_the_action():
    html = render(REJECTED, pipelines_ok=False)
    text = plain(html)
    assert LEAD in text
    assert "Home Assistant turned down Athena's access token" in text and "It won't try again until you save a new token." in text
    assert 'Open your user profile in Home Assistant' in text
    assert TRY_AGAIN in html
    assert '<p class="text-sm text-gray-200">' in html and '<p class="mt-1 text-sm text-gray-400">' in html and '<div class="mt-2 text-sm text-gray-300">' in html


def test_pipelines_failed_while_not_set_up_shows_the_setup_button():
    html = render(NOT_SET_UP, pipelines_ok=False)
    assert ">Set up Home Assistant</button>" in html and "Add your Home Assistant address and token" in plain(html)
    assert TRY_AGAIN in html


@pytest.mark.parametrize("state", ["connected", "attention"])
def test_pipelines_failed_while_connected_blames_the_voice_assistants_not_the_link(state):
    html = render({**CONNECTED, "state": state, "reason": "ok" if state == "connected" else "registry_unavailable"}, pipelines_ok=False)
    assert LEAD in plain(html) and ASSISTANTS in plain(html)
    assert TRY_AGAIN in html
    assert "turned down" not in html


@pytest.mark.parametrize("status", [{"state": "unknown", "reason": "orchestrator_unreachable", "checked_at": "2026-10-11T12:00:00+00:00"}, None])
def test_pipelines_failed_with_an_unknown_link_says_only_that_and_offers_try_again(status):
    html = render(status, pipelines_ok=False)
    body = html[html.index('<div class="config-card-body">'):]
    assert plain(body).replace("Try again", "").strip() == LEAD
    assert TRY_AGAIN in html


def test_a_failed_pipelines_request_never_shows_the_routes_detail_or_the_token_name_or_a_red_box():
    for status in (CONNECTED, REJECTED, NOT_SET_UP, None):
        html = render(status, pipelines_ok=False)
        assert "secret detail" not in html and "<img" not in html and "HA_TOKEN" not in html
        assert "error-message" not in html and "Unable to connect" not in html


def test_the_mode_options_are_buttons_with_aria_pressed_and_literal_handlers():
    html = render(CONNECTED, mode="full")
    options = re.findall(r'<button type="button" class="mode-option[^"]*"[^>]*>', html)
    assert len(options) == 3
    assert '<div class="mode-option' not in html
    assert [re.search(r'aria-pressed="(\w+)"', o).group(1) for o in options] == ["false", "true", "false"]
    assert [re.search(r"onclick=\"setVoiceMode\('(\w+)'\)\"", o).group(1) for o in options] == ["streaming_rag", "full", "simple"]
    assert html.count("</button>") == 3


def test_the_pipeline_names_are_escaped():
    html = render(CONNECTED)
    assert "Ollama &lt;b&gt;" in html and "Ollama <b>" not in html


def test_the_header_wraps_and_the_mode_option_resets_button_styles():
    source = VOICE_CONFIG.read_text(encoding="utf-8")
    header = source[source.index(".config-card-header {"):]
    header = header[: header.index("}")]
    assert "flex-wrap: wrap" in header and "gap: 8px 16px" in header
    option = source[source.index(".mode-option {"):]
    option = option[: option.index("}")]
    for declaration in ("text-align: left", "width: 100%", "font: inherit", "color: inherit"):
        assert declaration in option


def test_the_file_has_no_token_name_and_no_connected_flag():
    source = VOICE_CONFIG.read_text(encoding="utf-8")
    assert "HA_TOKEN" not in source and "haConnected" not in source
    assert "Promise.allSettled" in source
