"""jarvis-web frontend contract (V7.1, ruby H1/H2, xander N-L1, A8 M5).

Static checks over the page and scripts, plus a behavioural run of
jarvis-fetch.js under Node (skipped only when Node isn't installed): a
same-origin 401 or opaque redirect ends the session exactly once without
reading the body; a network error probes once.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap

import pytest

from . import _jarvis_web_harness as h

INDEX = (h.FRONTEND / "index.html").read_text(encoding="utf-8")
FETCH_JS = h.FRONTEND / "jarvis-fetch.js"
NODE = shutil.which("node") or ("/opt/homebrew/bin/node" if shutil.os.path.exists("/opt/homebrew/bin/node") else None)


def _function_body(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace:i + 1]
    raise AssertionError(name)


def test_jarvis_fetch_header_and_manual_redirect():
    source = FETCH_JS.read_text(encoding="utf-8")
    assert "options.redirect = 'manual'" in source
    assert "headers.set('X-Jarvis-Request', '1')" in source


def test_session_ended_wired_to_redirect_and_401_without_reading_body():
    source = FETCH_JS.read_text(encoding="utf-8")
    ends = _function_body(source, "endsSession")
    assert "opaqueredirect" in ends and "response.status === 401" in ends
    fetch_body = _function_body(source, "jarvisFetch")
    before_body_read = fetch_body.split("response.clone().json()", 1)[0]
    assert "endsSession(response)" in before_body_read and "sessionEnded()" in before_body_read


def test_reload_required_branch_pinned():
    source = FETCH_JS.read_text(encoding="utf-8")
    assert "body.detail === 'reload_required'" in source
    assert "Jarvis was updated. Reload the page." in source


def test_polls_stop_after_session_end():
    """Named: both pollers return early once signed out, and the
    session-ended handler clears both timers and every reconnect path."""
    for poller in ("checkHealth", "loadModeState"):
        body = _function_body(INDEX, poller)
        first = body.split("\n", 2)[1]
        assert "window.jarvisSession.isSignedOut()" in first and "return" in first, poller
    assert "healthTimer = setInterval(checkHealth" in INDEX and "modeTimer = setInterval(loadModeState" in INDEX
    handler = INDEX.split("window.jarvisSession.onSessionEnded(() => {", 1)[1].split("\n        });", 1)[0]
    for needle in ("clearInterval(healthTimer)", "clearInterval(modeTimer)", "maClient.disconnect()",
                   "sendspinClient.disconnect()", "livekitClient.disconnect()", "voiceStatus.textContent = 'Signed out'",
                   "setStatus('offline', 'Signed out')", "saveDraft(messageInput.value)"):
        assert needle in handler, needle


@pytest.mark.parametrize("script", ["music-assistant-client.js", "sendspin-client.js"])
def test_ws_reconnect_loops_check_signed_out(script):
    source = (h.FRONTEND / script).read_text(encoding="utf-8")
    onclose = source.split("this.ws.onclose", 1)[1].split("_scheduleReconnect(", 1)[0]
    assert "isSignedOut()" in onclose
    scheduled = _function_body(source, "_scheduleReconnect") if "function _scheduleReconnect" in source else \
        source.split("_scheduleReconnect(", 2)[2].split("}, delay)", 1)[0]
    assert "isSignedOut()" in scheduled


def test_banner_markup():
    banner = re.search(r'<div class="session-banner" id="session-banner"[^>]*>', INDEX).group(0)
    assert 'role="alert"' in banner and "hidden" in banner
    assert INDEX.index('id="session-banner"') < INDEX.index('<header class="header">')
    show = _function_body(INDEX, "showSessionBanner")
    assert "Your sign-in has expired." in show and "Sign in again" in show
    assert "doesn't look like it's on the home network anymore." in show and "Try again" in show
    assert ".focus(" not in show


def test_mode_indicator_starts_hidden():
    tag = re.search(r'<div[^>]*id="mode-indicator"[^>]*>', INDEX).group(0)
    assert "hidden" in tag
    assert "document.getElementById('mode-indicator').hidden = false" in _function_body(INDEX, "loadModeState")


def test_display_name_only_via_text_content():
    assert "capabilities.display_name" in INDEX
    for line in INDEX.splitlines():
        if "display_name" in line:
            assert "innerHTML" not in line, line
    assert "nameEl.textContent = capabilities.display_name" in INDEX


def test_guest_stay_copy_and_signin_link_rule():
    assert "Controls are view-only during a guest stay." in INDEX
    assert "Controls are view-only on the guest network." in INDEX
    apply = _function_body(INDEX, "applyCapabilities")
    assert "capabilities.control_reason === 'guest_stay' ? capabilities.signin_url : null" in apply
    assert "Household sign-in" in INDEX
    assert "aria-describedby" in apply


def test_show_error_is_an_alert():
    assert "toast.setAttribute('role', 'alert')" in _function_body(INDEX, "showError")


# ---------------------------------------------------------------------------
# Behaviour, under Node
# ---------------------------------------------------------------------------

NODE_HARNESS = textwrap.dedent("""
    const fs = require('fs');
    const vm = require('vm');
    const [sourcePath, scenarioJson] = process.argv.slice(-2);
    const source = fs.readFileSync(sourcePath, 'utf8');
    const scenario = JSON.parse(scenarioJson);
    const results = { ended: 0, bodyReads: 0, fetches: [], notices: 0 };
    const notice = { prepend() { results.notices += 1; } };
    const document = {
        getElementById() { return null; },
        querySelector() { return notice; },
        body: notice,
        createElement() { return { setAttribute() {}, appendChild() {}, addEventListener() {} }; },
    };
    const window = { location: { href: 'https://jarvis.example/', origin: 'https://jarvis.example', reload() {} } };
    let calls = 0;
    function response(spec) {
        return {
            type: spec.type || 'basic', status: spec.status || 200,
            clone() { return this; },
            json() { results.bodyReads += 1; return Promise.resolve(spec.body || {}); },
        };
    }
    async function fetch(url, options) {
        results.fetches.push({ url, redirect: options.redirect,
                               header: options.headers && options.headers.get ? options.headers.get('X-Jarvis-Request') : null });
        const spec = scenario.responses[Math.min(calls, scenario.responses.length - 1)];
        calls += 1;
        if (spec.throw) throw new TypeError('Failed to fetch');
        return response(spec);
    }
    const context = { window, document, fetch, Headers, URL, console, Promise, TypeError, setTimeout };
    vm.createContext(context);
    vm.runInContext(source, context);
    window.jarvisSession.onSessionEnded(() => { results.ended += 1; });
    (async () => {
        for (const url of scenario.urls) {
            try { await window.jarvisFetch(url, {}); } catch (e) { results.error = e.name; }
        }
        results.signedOut = window.jarvisSession.isSignedOut();
        console.log(JSON.stringify(results));
    })();
""")


def _run(responses, urls=("https://jarvis.example/api/mode",)):
    if NODE is None:
        pytest.skip("node not installed")
    scenario = json.dumps({"responses": responses, "urls": list(urls)})
    proc = subprocess.run([NODE, "-e", NODE_HARNESS, "--", str(FETCH_JS), scenario],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_node_401_ends_session_once_without_reading_body():
    result = _run([{"status": 401}], urls=["/api/mode", "/api/welcome"])
    assert result["signedOut"] is True and result["ended"] == 1
    assert result["bodyReads"] == 0
    assert all(f["redirect"] == "manual" and f["header"] == "1" for f in result["fetches"])


def test_node_opaque_redirect_ends_session():
    result = _run([{"type": "opaqueredirect", "status": 0}])
    assert result["signedOut"] is True and result["ended"] == 1


def test_node_ok_and_cross_origin_401_do_not():
    assert _run([{"status": 200}])["signedOut"] is False
    cross = _run([{"status": 401}], urls=["https://other.example/x"])
    assert cross["signedOut"] is False and cross["fetches"][0]["header"] is None


def test_node_network_error_probes_once():
    offline = _run([{"throw": True}, {"throw": True}])
    assert offline["signedOut"] is False and offline["error"] == "TypeError"
    assert [f["url"] for f in offline["fetches"]] == ["https://jarvis.example/api/mode", "/api/welcome"]
    expired = _run([{"throw": True}, {"status": 401}])
    assert expired["signedOut"] is True and expired["ended"] == 1


def test_node_reload_required_offers_reload():
    result = _run([{"status": 403, "body": {"detail": "reload_required"}}])
    assert result["notices"] == 1 and result["signedOut"] is False


# ---------------------------------------------------------------------------
# ruby r1 (B1-B7)
# ---------------------------------------------------------------------------

def _css_var(name):
    return re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", INDEX).group(1)


def _contrast(fg, bg):
    def lum(hexc):
        r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rule(selector):
    return re.search(re.escape(selector) + r"\s*\{([^}]*)\}", INDEX).group(1)


def test_red_surfaces_and_links_meet_contrast():
    """B1 (named: the banner): white on the red surface and the header
    links both reach 4.5:1."""
    surface = _css_var("error-surface")
    assert _contrast("#ffffff", surface) >= 4.5
    assert "background: var(--error-surface)" in _rule(".session-banner, .jarvis-notice")
    assert "background: var(--error-surface)" in _rule(".error-toast")
    assert "color: var(--link)" in _rule(".signed-in a, .control-note a")
    for bg in ("bg-primary", "bg-secondary"):
        assert _contrast(_css_var("link"), _css_var(bg)) >= 4.5, bg


def test_narrow_header_keeps_sign_out_and_drops_the_name():
    """B2: at 480px and below the name is visually hidden (still read by a
    screen reader) and the Sign out link stays."""
    narrow = INDEX.split("@media (max-width: 480px) {\n            .header-right { gap: 4px; }", 1)[1].split("\n        }\n", 1)[0]
    assert "#signed-in-name, #status-text" in narrow and "clip: rect(0 0 0 0)" in narrow
    assert "signout-link" not in narrow and "display: none" not in narrow


def test_rate_limited_message_marked_not_sent():
    """B3 (named): a 429 marks the bubble "Not sent", adds an aria-live
    notice in the transcript with chat-embed's wording, and restores the
    draft, for typed and spoken messages alike."""
    embed = (h.REPO_ROOT / "apps" / "chat-embed" / "main.py").read_text(encoding="utf-8")
    wording = "You're going a bit fast. Please wait a minute, then try again."
    assert wording in embed
    assert f'const RATE_LIMIT_MESSAGE = "{wording}";' in INDEX
    not_sent = _function_body(INDEX, "markNotSent")
    for needle in ("classList.add('not-sent')", "textContent = 'Not sent'", "showTranscriptNotice(RATE_LIMIT_MESSAGE)",
                   "messageInput.value = message", "saveDraft(message)"):
        assert needle in not_sent, needle
    notice = _function_body(INDEX, "showTranscriptNotice")
    assert "setAttribute('aria-live', 'polite')" in notice and "textContent = text" in notice
    for sender in ("sendMessage", "sendMessageWithVoice"):
        body = _function_body(INDEX, sender)
        assert "const userBubble = addMessage(message, 'user');" in body, sender
        assert "if (error.status === 429) {\n                    markNotSent(userBubble, message);" in body, sender
    assert "sending messages too quickly" not in INDEX


def _emoji_outside_hidden():
    from html.parser import HTMLParser

    emoji = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿←-⇿]")
    markup = INDEX.split("<body>", 1)[1].split('<script src="/static/jarvis-fetch.js">', 1)[0]
    found, stack = [], []

    class _P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in {"br", "img", "input", "path", "circle", "line", "rect", "polyline", "meta"}:
                stack.append(dict(attrs).get("aria-hidden") == "true")

        def handle_endtag(self, tag):
            if tag not in {"br", "img", "input", "path", "circle", "line", "rect", "polyline", "meta"} and stack:
                stack.pop()

        def handle_data(self, data):
            for char in emoji.findall(data):
                found.append((char, any(stack)))

    _P().feed(markup)
    return found


def test_decorative_emoji_are_hidden_from_screen_readers():
    """B4: floor 13 decorative symbols in the page markup; named member the
    TV icon; every one sits inside aria-hidden="true"."""
    found = _emoji_outside_hidden()
    assert len(found) >= 13
    assert ("📺", True) in found
    assert [char for char, hidden in found if not hidden] == []


def test_capabilities_load_before_music_and_voice():
    """B5 (named): welcome (capabilities) loads first; the music socket,
    voice and LiveKit only start when this caller may control."""
    init = INDEX.split("document.addEventListener('DOMContentLoaded', async () => {", 1)[1].split("\n        });", 1)[0]
    welcome = init.index("await loadWelcome();")
    gate = init.index("if (canControl()) {")
    assert welcome < gate
    gated = init[gate:init.index("} else {", gate)]
    for starter in ("await checkVoiceHealth();", "await initializeLiveKit();", "await initMusicPlayer();"):
        assert starter in gated and init.count(starter) == 1, starter
    assert "disableVoice(" in init[gate:]
    assert "Sign-in required for" not in INDEX


def test_view_only_disables_every_control():
    """B6: the Apple TV remote, playback and app buttons join the controls
    that view-only disables and takes out of the tab order, including after
    the app buttons are re-rendered."""
    assert "'.tv-apps button, .tv-remote-btn[data-cmd], .tv-playback-btn[data-cmd]'" in INDEX
    state = _function_body(INDEX, "applyControlState")
    assert "el.disabled = !allowed" in state and "setAttribute('tabindex', '-1')" in state
    assert "applyControlState();" in _function_body(INDEX, "applyCapabilities")
    render = INDEX.split("tvApps.innerHTML = streamingApps", 1)[1].split("async function launchApp", 1)[0]
    assert "applyControlState();" in render


def test_no_dead_sign_in_required_branches():
    """B7: 403 sign_in_required no longer exists; nothing checks for it."""
    for source in (INDEX, (h.FRONTEND / "music-player.js").read_text(encoding="utf-8")):
        assert "detail === 'sign_in_required'" not in source
