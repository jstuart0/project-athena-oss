/**
 * The admin panel's one status vocabulary.
 *
 * Owns, for every page: the words and colours of a service's status
 * (statusInfo, SERVICE_STATUS and the TONE_* tables), the wording of ages and of
 * the "health checks have stopped" banner, and the Home Assistant card (its
 * copy, its summary and its three renderers). The pages are adapters: none of
 * them carries a status label, a neutral-status list or a copy of this text
 * (tests/unit/test_admin_frontend_status_vocab.py scans for that).
 *
 * Classic script, loaded after utils.js and before the pages. Nothing here
 * calls escapeHtml at load time (escape-html.js loads last); the functions call
 * it when they run.
 */

// ----------------------------------------------------------------------------
// Closed lookups
// ----------------------------------------------------------------------------

function _closedLookup(table, key, fallback) {
    return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : fallback;
}

// ----------------------------------------------------------------------------
// Service status words -> label and tone
// ----------------------------------------------------------------------------

const SERVICE_STATUS = (() => {
    const table = Object.create(null);
    const add = (words, label, tone) => words.forEach((word) => { table[word] = { label, tone }; });
    add(['healthy', 'running', 'healthy (auth required)', 'running (auth required)'], 'Healthy', 'good');
    add(['unhealthy'], 'Unhealthy', 'bad');
    add(['degraded'], 'Degraded', 'bad');
    add(['unknown'], 'Unknown', 'bad');
    add(['ssrf_blocked'], 'Blocked by allowlist', 'bad');
    add(['unconfigured', 'not configured'], 'Needs setup', 'setup');
    add(['pending'], 'Checking…', 'neutral');
    add(['stale'], 'No recent check', 'neutral');
    add(['disabled'], 'Not monitored', 'neutral');
    add(['unreachable', 'not deployed'], 'Unreachable', 'offline');
    add(['timeout'], 'Not responding', 'offline');
    add(['error'], 'Error', 'offline');
    return table;
})();

const TONE_DOT = Object.assign(Object.create(null), {
    good: 'bg-green-500', bad: 'bg-red-500', offline: 'bg-red-500', setup: 'bg-amber-500', neutral: 'bg-gray-500',
});
const TONE_TEXT = Object.assign(Object.create(null), {
    good: 'text-green-400', bad: 'text-red-400', offline: 'text-red-400', setup: 'text-amber-400', neutral: 'text-gray-400',
});
const TONE_TILE = Object.assign(Object.create(null), {
    good: 'green', bad: 'red', offline: 'red', setup: 'amber', neutral: 'gray',
});
const TONE_TOAST = Object.assign(Object.create(null), {
    good: 'success', bad: 'error', offline: 'error', setup: 'warning', neutral: 'info',
});
// Only a real problem pulses, and never for a viewer who asked for less motion.
const TONE_PULSE = Object.assign(Object.create(null), {
    bad: 'motion-safe:animate-pulse', offline: 'motion-safe:animate-pulse',
});

/**
 * {label, tone, raw} for a status word. null/undefined (no entry at all) reads
 * "Not reported". A word this table has never seen keeps its own text as the
 * label and reads as a problem (tone 'bad'); every caller escapes `label`.
 * Names like __proto__ and constructor are unknown words, not table hits.
 */
function statusInfo(word) {
    if (word === null || word === undefined) return { label: 'Not reported', tone: 'neutral', raw: null };
    const known = _closedLookup(SERVICE_STATUS, word, null);
    if (known) return { label: known.label, tone: known.tone, raw: word };
    if (typeof word === 'string' && word.startsWith('error:')) return { label: 'Error', tone: 'offline', raw: word };
    return { label: String(word), tone: 'bad', raw: String(word) };
}

// The words a service-registry row's health_status can carry. A page that
// shows only registry rows asks registryHealthInfo, which answers null for any
// other word instead of painting it as a problem.
const REGISTRY_HEALTH_WORDS = ['healthy', 'unhealthy', 'degraded', 'unconfigured', 'pending', 'stale'];

/** statusInfo for a registry row's health word (null/undefined read as pending); null outside the registry's words. */
function registryHealthInfo(word) {
    const effective = (word === null || word === undefined) ? 'pending' : word;
    return REGISTRY_HEALTH_WORDS.includes(effective) ? statusInfo(effective) : null;
}

// ----------------------------------------------------------------------------
// Ages and the stopped-checker banner. Ages are measured against a reference
// clock the caller chooses (the server's checked_at where it has one), so a
// skewed laptop can't show a negative or future age.
// ----------------------------------------------------------------------------

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTH_NAMES_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

/**
 * '12m ago' style age of iso at nowMs. Under 60 s (or in the future): 'just
 * now'. 24 h or more: an absolute '27 Sep' (with the year when it isn't the
 * current year). spoken=true spells it out for screen readers ('12 minutes
 * ago', '27 September'). null for an unparseable time.
 */
function ageWords(iso, nowMs, spoken) {
    const at = Date.parse(iso);
    if (Number.isNaN(at)) return null;
    const ageMs = nowMs - at;
    if (ageMs < 60 * 1000) return 'just now';
    const minutes = Math.floor(ageMs / 60000);
    if (minutes < 60) return spoken ? `${minutes} ${minutes === 1 ? 'minute' : 'minutes'} ago` : `${minutes}m ago`;
    const hours = Math.floor(ageMs / 3600000);
    if (hours < 24) return spoken ? `${hours} ${hours === 1 ? 'hour' : 'hours'} ago` : `${hours}h ago`;
    const when = new Date(at);
    const year = when.getFullYear() === new Date(nowMs).getFullYear() ? '' : ` ${when.getFullYear()}`;
    return `${when.getDate()} ${(spoken ? MONTH_NAMES_LONG : MONTH_NAMES)[when.getMonth()]}${year}`;
}

function _refMs(refIso) {
    const ms = Date.parse(refIso);
    return Number.isNaN(ms) ? Date.now() : ms;
}

/** ageWords against a reference ISO time (the payload's checked_at). */
function formatAge(iso, refIso) {
    return ageWords(iso, _refMs(refIso), false);
}

/** '3:12 pm' in the viewer's local time, or null for an unparseable time. */
function formatClock(iso) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return null;
    const hours = at.getHours();
    const minutes = String(at.getMinutes()).padStart(2, '0');
    return `${hours % 12 === 0 ? 12 : hours % 12}:${minutes} ${hours < 12 ? 'am' : 'pm'}`;
}

/** A <time> element carrying the exact time on hover; '' when the time can't be read. */
function timeTag(iso, refIso) {
    const rel = formatAge(iso, refIso);
    if (rel === null) return '';
    const exact = new Date(iso).toLocaleString();
    return `<time datetime="${escapeHtml(iso)}" title="${escapeHtml(exact)}">${escapeHtml(rel)}</time>`;
}

/** '14:02' within the last 24 h (local), else the absolute date; null when unreadable. */
function sinceWords(iso, nowMs) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return null;
    if (nowMs - at.getTime() < 24 * 60 * 60 * 1000) {
        return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
    }
    return ageWords(iso, nowMs, false);
}

/** "Not monitored · last checked 27 Sep" / "Not monitored · never checked" for a switched-off row. */
function notMonitoredText(lastCheckIso, nowMs) {
    const age = lastCheckIso ? ageWords(lastCheckIso, nowMs, false) : null;
    const label = statusInfo('disabled').label;
    return age ? `${label} · last checked ${age}` : `${label} · never checked`;
}

/**
 * The page-level warning that the health checker has stopped, as HTML. Each
 * page decides WHEN to show it and which timestamp is the newest; this is how
 * it reads. `spacing` is an optional extra class for the container.
 */
function stalePollerBanner(newestIso, nowMs, options) {
    const since = newestIso ? sinceWords(newestIso, nowMs) : null;
    const sentence = since
        ? `Health checks haven't run since ${escapeHtml(since)}.`
        : "Health checks haven't run yet.";
    const spacing = options && options.spacing ? ` ${options.spacing}` : '';
    return `<div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4${escapeHtml(spacing)}" role="status"><p class="text-yellow-200">${sentence} The statuses below may be out of date.</p></div>`;
}

// ----------------------------------------------------------------------------
// The Home Assistant card. All text comes from the closed maps below; the only
// dynamic values (times, the area count, failure counts) go through escapeHtml
// or are integers.
// ----------------------------------------------------------------------------

const HA_STATE_CLASSES = {
    connected: { dot: 'bg-green-500', text: 'text-green-400' },
    attention: { dot: 'bg-amber-500', text: 'text-amber-400' },
    failing: { dot: 'bg-red-500', text: 'text-red-400' },
    not_configured: { dot: 'bg-gray-500', text: 'text-gray-400' },
    unknown: { dot: 'bg-gray-500', text: 'text-gray-400' },
};
const HA_UNKNOWN_STATE_CLASSES = { dot: 'bg-gray-500', text: 'text-gray-400' };

// Each surface that shows the card names its heading id and the retry call.
// Closed on purpose: a free-form handler string would be a new on*= sink.
const HA_SURFACES = {
    dashboard: { headingId: 'ha-status-heading', retry: 'loadStatus()' },
    mission: { headingId: 'mc-ha-status-heading', retry: 'Athena.pages.MissionControl.refresh()' },
};

function _haSurface(surface) {
    return _closedLookup(HA_SURFACES, surface, HA_SURFACES.dashboard);
}

const HA_SETUP_BUTTON = '<button type="button" onclick="showTab(\'external-api-keys\')" class="mt-2 min-h-[44px] px-3 py-2 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded">Set up Home Assistant</button>';

const HA_IP_BANNED_ACTION = '<ol class="list-decimal pl-5 space-y-1"><li>If the token might be wrong, create a new one in Home Assistant (your user profile, Security tab) and save it under External API Keys → home-assistant. Otherwise Home Assistant will block the address again.</li><li>Open <code>ip_bans.yaml</code> in Home Assistant\'s config folder and delete the entry for Athena\'s address. Home Assistant\'s "Login attempt failed" notification names it.</li><li>Restart Home Assistant. Athena reconnects on its own.</li></ol>';

// Keys are exactly admin-backend's ha_status.REASONS (pinned by a Node test).
const HA_REASON_COPY = {
    ok: { headline: 'Connected', detail: null, action: null },
    waiting: {
        headline: 'No contact yet',
        detail: "Athena hasn't talked to Home Assistant since it started. This updates after the first voice command.",
        action: null,
    },
    token_rejected: {
        headline: 'Token rejected',
        detail: "Home Assistant turned down Athena's access token, so Athena can't control your home.",
        action: 'Open your user profile in Home Assistant, go to the Security tab, and create a long-lived access token. Save it in Athena under External API Keys → home-assistant. Athena starts using it within a minute. No restart needed.',
    },
    ip_banned: {
        headline: 'Address blocked',
        detail: 'Home Assistant has blocked the address Athena connects from after too many failed sign-ins. Nothing from Athena gets through until the block is removed.',
        action: HA_IP_BANNED_ACTION,
    },
    forbidden: {
        headline: 'Request refused',
        detail: 'Home Assistant, or a proxy in front of it, refused a request from Athena.',
        action: "If it keeps happening, check Home Assistant's log. If Athena's address is listed as banned, remove it from ip_bans.yaml and restart Home Assistant.",
    },
    unreachable: {
        headline: "Can't reach Home Assistant",
        detail: 'Athena couldn\'t connect. Home Assistant may be off, restarting, or at a different address.',
        action: 'Check that Home Assistant is running and that the address under External API Keys → home-assistant is right.',
    },
    timeout: {
        headline: 'Not responding',
        detail: "Athena connected, but Home Assistant didn't answer in time. It may be busy or starting up.",
        action: null,
    },
    tls_error: {
        headline: 'Secure connection failed',
        detail: "Athena reached Home Assistant but couldn't set up the HTTPS connection. Check the address uses the right scheme (http or https) and port.",
        action: null,
    },
    ha_error: {
        headline: 'Home Assistant error',
        detail: 'Home Assistant is running but returned an error to Athena. Its own log will say why.',
        action: null,
    },
    registry_unavailable: {
        headline: 'Connected, rooms not loaded',
        detail: "Commands reach Home Assistant, but Athena couldn't load its areas, so it can't tell which room you're in.",
        action: 'Athena tries again on its own the next time it needs rooms.',
    },
    registry_refused: {
        headline: 'Connected, rooms not loaded',
        detail: "Home Assistant turned down Athena's token when it loaded areas earlier. Commands work now.",
        action: "Athena tries again on its own the next time it needs rooms. If this doesn't clear, create a new token and save it under External API Keys → home-assistant.",
    },
    not_configured: {
        headline: 'Not set up',
        detail: 'Add your Home Assistant address and token so Athena can control your home.',
        action: HA_SETUP_BUTTON,
    },
    orchestrator_unreachable: {
        headline: 'Status unavailable',
        detail: "The admin panel couldn't reach Athena's orchestrator to ask about Home Assistant. Home Assistant itself may be fine.",
        action: null,
    },
    orchestrator_refused: {
        headline: 'Status unavailable',
        detail: "The orchestrator refused the admin panel's service key. Home Assistant itself may be fine.",
        action: 'Check SERVICE_API_KEY matches on admin-backend and the orchestrator.',
    },
    orchestrator_blocked: {
        headline: 'Status unavailable',
        detail: "The orchestrator's address isn't on the admin panel's allowed list. Home Assistant itself may be fine.",
        action: "Allow the orchestrator's address in HEALTH_POLL_ALLOWED_PRIVATE_HOSTS.",
    },
    orchestrator_outdated: {
        headline: 'Status unavailable',
        detail: "The orchestrator is an older version that doesn't report this yet.",
        action: 'Update the orchestrator.',
    },
};
const HA_UNKNOWN_REASON_COPY = { headline: 'Status unknown', detail: null, action: null };

// The verb in front of a failure's time. Anything else reads "Seen".
const HA_TIME_VERBS = { token_rejected: 'Rejected', ip_banned: 'Blocked' };

// Keys are ha_link.CALLERS, in that order (pinned by a Node test).
const HA_CALLER_LABELS = {
    orchestrator_rest: 'voice commands',
    orchestrator_registry: 'room list',
    gateway_rest: 'voice satellites',
    gateway_registry: 'speaker room detection',
    admin_rest: 'admin panel',
    admin_ws: 'voice pipeline settings',
};

const HA_WITHHELD_SENTENCE = "Athena has stopped using this token so Home Assistant doesn't block its address.";
const HA_IDLE_AFTER_MS = 15 * 60 * 1000;
const HA_REPLICA_TIP = "Athena couldn't combine what all its orchestrator copies have seen, so this is one copy's view.";
const HA_GATEWAY_TIP = "One copy of the gateway reported a problem reading Home Assistant's areas. Room detection for voice may be affected. This is a sample, not the whole picture.";

function _haTimePart(p, checkedAt) {
    const outcome = p.last_outcome_at ? timeTag(p.last_outcome_at, checkedAt) : '';
    if (!outcome) return null;
    const success = p.last_success_at ? timeTag(p.last_success_at, checkedAt) : '';
    const sameMoment = p.last_outcome_at === p.last_success_at;
    if (p.state === 'connected' || sameMoment) return `Last contact ${outcome}`;
    const verb = _closedLookup(HA_TIME_VERBS, p.reason, 'Seen');
    return success ? `${verb} ${outcome} · last worked ${success}` : `${verb} ${outcome}`;
}

function _localDay(ms) {
    const d = new Date(ms);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** The second sentence of the withheld suffix: when Athena will try again. */
function _haRetrySentence(nextRetryAt, checkedAt) {
    if (nextRetryAt === null || nextRetryAt === undefined) return "It won't try again until you save a new token.";
    const next = Date.parse(nextRetryAt);
    if (Number.isNaN(next)) return 'It will try again later on its own.';
    const now = _refMs(checkedAt);
    if (next <= now) return 'It will try again the next time it needs Home Assistant.';
    const clock = escapeHtml(formatClock(nextRetryAt));
    const dayDelta = Math.round((_localDay(next) - _localDay(now)) / (24 * 60 * 60 * 1000));
    if (dayDelta === 0) return `It will try again after ${clock}.`;
    if (dayDelta === 1) return `It will try again tomorrow after ${clock}.`;
    const when = new Date(next);
    return `It will try again after ${clock} on ${when.getDate()} ${MONTH_NAMES[when.getMonth()]}.`;
}

/** "Failed sign-ins: voice commands 2, room list 1. ..." or '' when there is nothing to say. */
function _haFailuresLine(authFailures) {
    if (!Array.isArray(authFailures)) return '';
    const counts = Object.create(null);
    authFailures.forEach((entry) => {
        if (entry && typeof entry === 'object' && Number.isInteger(entry.count) && entry.count > 0
            && typeof entry.caller === 'string' && !(entry.caller in counts)) {
            counts[entry.caller] = entry.count;
        }
    });
    const parts = Object.keys(HA_CALLER_LABELS)
        .filter((caller) => caller in counts)
        .map((caller) => `${HA_CALLER_LABELS[caller]} ${counts[caller]}`);
    if (parts.length === 0) return '';
    return `Failed sign-ins: ${parts.join(', ')}. Home Assistant can block Athena's address if these keep growing.`;
}

/**
 * Pure: the Home Assistant card's pieces from GET /api/service-registry/home-assistant.
 * Returns {dotClass, textClass, headline, detail, action, failures, meta}; every
 * field is HTML-safe (closed-map text, or escaped values). An unknown state is
 * grey and an unknown reason reads "Status unknown"; neither is ever echoed.
 */
function summarizeHomeAssistant(payload) {
    const p = (payload && typeof payload === 'object') ? payload : {};
    const classes = _closedLookup(HA_STATE_CLASSES, p.state, HA_UNKNOWN_STATE_CLASSES);
    const copy = _closedLookup(HA_REASON_COPY, p.reason, HA_UNKNOWN_REASON_COPY);

    let detail = copy.detail;
    if (p.reason === 'ok' && p.last_success_at && p.checked_at) {
        const idleMs = Date.parse(p.checked_at) - Date.parse(p.last_success_at);
        const since = formatClock(p.last_success_at);
        if (idleMs >= HA_IDLE_AFTER_MS && since) {
            detail = `Athena checks Home Assistant whenever it uses it. Nothing has needed it since ${escapeHtml(since)}, so this is the latest it knows.`;
        }
    }
    if (p.reason === 'token_rejected' && p.token_withheld === true) {
        detail = `${detail} ${HA_WITHHELD_SENTENCE} ${_haRetrySentence(p.next_retry_at, p.checked_at)}`;
    }

    const parts = [];
    const timePart = _haTimePart(p, p.checked_at);
    if (timePart) parts.push(timePart);
    const hasAreas = Number.isFinite(p.areas) && p.areas >= 0;
    if (hasAreas) {
        parts.push(p.areas === 1 ? '1 area' : `${escapeHtml(String(p.areas))} areas`);
    } else if (p.state === 'connected' || p.state === 'attention') {
        parts.push('rooms not loaded');
    }
    if (p.coverage === 'replica') {
        parts.push(`<span title="${escapeHtml(HA_REPLICA_TIP)}">this replica only</span>`);
    }
    if (p.gateway === 'not_checked') {
        parts.push('gateway not checked');
    } else if (p.gateway === 'refused' || p.gateway === 'unavailable') {
        parts.push(`<span title="${escapeHtml(HA_GATEWAY_TIP)}">gateway check failed</span>`);
    }

    return {
        dotClass: classes.dot,
        textClass: classes.text,
        headline: copy.headline,
        detail,
        action: copy.action,
        failures: _haFailuresLine(p.auth_failures),
        meta: parts.join(' · '),
    };
}

function renderHomeAssistantCard(summary, surface) {
    const target = _haSurface(surface);
    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4 md:p-6">
            <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-2 md:gap-6">
                <div class="min-w-0">
                    <h3 id="${escapeHtml(target.headingId)}" class="text-lg font-semibold text-white">Home Assistant</h3>
                    <p class="mt-1 flex items-center gap-2 text-sm font-medium ${escapeHtml(summary.textClass)}">
                        <span class="w-2 h-2 rounded-full ${escapeHtml(summary.dotClass)}" aria-hidden="true"></span>${summary.headline}
                    </p>
                    ${summary.detail ? `<p class="mt-1 text-sm text-gray-400">${summary.detail}</p>` : ''}
                    ${summary.action ? `<div class="mt-2 text-sm text-gray-300">${summary.action}</div>` : ''}
                    ${summary.failures ? `<p class="mt-2 text-xs text-gray-400">${summary.failures}</p>` : ''}
                </div>
                <p class="text-xs text-gray-400 md:text-right shrink-0">${summary.meta}</p>
            </div>
        </div>
    `;
}

function renderHomeAssistantLoading() {
    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4 motion-safe:animate-pulse" role="status" aria-label="Loading Home Assistant status">
            <div class="h-4 w-40 bg-gray-700 rounded"></div>
            <div class="h-3 w-64 bg-gray-700/60 rounded mt-3"></div>
        </div>
    `;
}

function renderHomeAssistantFailure(surface) {
    const target = _haSurface(surface);
    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4 md:p-6">
            <h3 id="${escapeHtml(target.headingId)}" class="text-lg font-semibold text-white">Home Assistant</h3>
            <p class="mt-1 flex items-center gap-2 text-sm font-medium text-gray-400">
                <span class="w-2 h-2 rounded-full bg-gray-500" aria-hidden="true"></span>Status unavailable
            </p>
            <p class="mt-1 text-sm text-gray-400">Couldn't load Home Assistant status.</p>
            <button type="button" onclick="${target.retry}" class="mt-2 min-h-[44px] px-3 py-2 text-xs bg-gray-700 hover:bg-gray-600 text-white rounded">Try again</button>
        </div>
    `;
}
