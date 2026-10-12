// Frozen copy of the Home Assistant card block of admin/frontend/app.js at 9e80241 (before the shared status-vocab.js).
// Status vocabulary shared with Service Control (service-control.js keeps an
// equal copy; a Node test pins the two maps equal on these six codes).
// ----------------------------------------------------------------------------

const STATUS_LABELS = {
    healthy: 'Healthy',
    unhealthy: 'Unhealthy',
    unconfigured: 'Needs setup',
    pending: 'Checking…',
    stale: 'No recent check',
    disabled: 'Not monitored',
};

/** The human word for a registry row's display status; an unknown code is passed through for the caller to escape. */
function statusLabel(status) {
    return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) ? STATUS_LABELS[status] : String(status);
}

const STATUS_DOT_CLASSES = {
    healthy: 'bg-green-500',
    offline: 'bg-gray-500',
    disabled: 'bg-gray-600',
    pending: 'bg-blue-500',
    stale: 'bg-gray-500',
    unconfigured: 'bg-amber-500',
    unhealthy: 'bg-red-500',
    error: 'bg-red-500',
};

const STATUS_TEXT_CLASSES = {
    healthy: 'text-green-400',
    offline: 'text-gray-400',
    disabled: 'text-gray-500',
    pending: 'text-blue-400',
    stale: 'text-gray-400',
    unconfigured: 'text-amber-400',
    unhealthy: 'text-red-400',
    error: 'text-red-400',
};

function _closedLookup(table, key, fallback) {
    return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : fallback;
}

// ----------------------------------------------------------------------------
// Relative times. Ages are measured against the server's own clock (the
// payload's checked_at), never the browser's, so a skewed laptop can't show a
// negative or future age.
// ----------------------------------------------------------------------------

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function _refMs(refIso) {
    const ms = Date.parse(refIso);
    return Number.isNaN(ms) ? Date.now() : ms;
}

/**
 * '12m ago' style age of iso against refIso. Under 60 s (or in the future):
 * 'just now'. 24 h or more: an absolute '27 Sep' (with the year when it isn't
 * the reference year). Returns null for an unparseable time.
 */
function formatAge(iso, refIso) {
    const at = Date.parse(iso);
    if (Number.isNaN(at)) return null;
    const ref = _refMs(refIso);
    const ageMs = ref - at;
    if (ageMs < 60 * 1000) return 'just now';
    if (ageMs < 60 * 60 * 1000) return `${Math.floor(ageMs / 60000)}m ago`;
    if (ageMs < 24 * 60 * 60 * 1000) return `${Math.floor(ageMs / 3600000)}h ago`;
    const when = new Date(at);
    const sameYear = when.getFullYear() === new Date(ref).getFullYear();
    return `${when.getDate()} ${MONTH_NAMES[when.getMonth()]}${sameYear ? '' : ` ${when.getFullYear()}`}`;
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

// ----------------------------------------------------------------------------
// The Home Assistant card. All text comes from the closed maps below; the only
// dynamic values (times, the area count) go through escapeHtml.
// ----------------------------------------------------------------------------

const HA_STATE_CLASSES = {
    connected: { dot: 'bg-green-500', text: 'text-green-400' },
    attention: { dot: 'bg-amber-500', text: 'text-amber-400' },
    failing: { dot: 'bg-red-500', text: 'text-red-400' },
    not_configured: { dot: 'bg-gray-500', text: 'text-gray-400' },
    unknown: { dot: 'bg-gray-500', text: 'text-gray-400' },
};
const HA_UNKNOWN_STATE_CLASSES = { dot: 'bg-gray-500', text: 'text-gray-400' };

const HA_SETUP_BUTTON = '<button type="button" onclick="showTab(\'external-api-keys\')" class="mt-2 px-3 py-2 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded">Set up Home Assistant</button>';

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
        action: 'Create a long-lived access token in Home Assistant (your profile → Security), save it under External API Keys → home-assistant, then restart the orchestrator.',
    },
    forbidden: {
        headline: 'Access blocked',
        detail: 'Home Assistant refused Athena. This usually means it blocked Athena\'s address after failed sign-ins.',
        action: "Remove Athena's address from Home Assistant's ip_bans.yaml and restart Home Assistant, then check the token.",
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
        action: 'Restart the orchestrator to try again.',
    },
    registry_refused: {
        headline: 'Connected, rooms not loaded',
        detail: "Home Assistant turned down Athena's token when it loaded areas earlier. Commands work now.",
        action: 'Restart the orchestrator to load rooms.',
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

const HA_IDLE_AFTER_MS = 15 * 60 * 1000;
const HA_REPLICA_TIP = "Athena couldn't combine what all its orchestrator copies have seen, so this is one copy's view.";
const HA_GATEWAY_TIP = "One copy of the gateway reported a problem reading Home Assistant's areas. Room detection for voice may be affected. This is a sample, not the whole picture.";

function _haTimePart(p, checkedAt) {
    const outcome = p.last_outcome_at ? timeTag(p.last_outcome_at, checkedAt) : '';
    if (!outcome) return null;
    const success = p.last_success_at ? timeTag(p.last_success_at, checkedAt) : '';
    const sameMoment = p.last_outcome_at === p.last_success_at;
    if (p.state === 'connected' || sameMoment) return `Last contact ${outcome}`;
    const verb = p.reason === 'token_rejected' ? 'Rejected' : 'Seen';
    return success ? `${verb} ${outcome} · last worked ${success}` : `${verb} ${outcome}`;
}

/**
 * Pure: the Home Assistant card's pieces from GET /api/service-registry/home-assistant.
 * Returns {dotClass, textClass, headline, detail, action, meta}; every field is
 * HTML-safe (closed-map text, or escaped values). An unknown state is grey and an
 * unknown reason reads "Status unknown"; neither is ever echoed.
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
        meta: parts.join(' · '),
    };
}

function renderHomeAssistantCard(summary) {
    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4 md:p-6">
            <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-2 md:gap-6">
                <div class="min-w-0">
                    <h3 id="ha-status-heading" class="text-lg font-semibold text-white">Home Assistant</h3>
                    <p class="mt-1 flex items-center gap-2 text-sm font-medium ${escapeHtml(summary.textClass)}">
                        <span class="w-2 h-2 rounded-full ${escapeHtml(summary.dotClass)}" aria-hidden="true"></span>${summary.headline}
                    </p>
                    ${summary.detail ? `<p class="mt-1 text-sm text-gray-400">${summary.detail}</p>` : ''}
                    ${summary.action ? `<p class="mt-2 text-xs text-gray-400">${summary.action}</p>` : ''}
                </div>
                <p class="text-xs text-gray-500 md:text-right shrink-0">${summary.meta}</p>
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

function renderHomeAssistantFailure() {
    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4 md:p-6">
            <h3 id="ha-status-heading" class="text-lg font-semibold text-white">Home Assistant</h3>
            <p class="mt-1 flex items-center gap-2 text-sm font-medium text-gray-400">
                <span class="w-2 h-2 rounded-full bg-gray-500" aria-hidden="true"></span>Status unavailable
            </p>
            <p class="mt-1 text-sm text-gray-400">Couldn't load Home Assistant status.</p>
            <button type="button" onclick="loadStatus()" class="mt-2 px-3 py-2 text-xs bg-gray-700 hover:bg-gray-600 text-white rounded">Try again</button>
        </div>
    `;
}

// ----------------------------------------------------------------------------
// Registry rows: the poller banner and the "not monitored" card line.
// ----------------------------------------------------------------------------

/** '14:02' today (local), or '27 Sep' for an older time. */
function _since(iso, nowMs) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return null;
    if (nowMs - at.getTime() < 24 * 60 * 60 * 1000) {
        return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
    }
    return formatAge(iso, new Date(nowMs).toISOString());
}

/**
 * Pure: the page-level warning when the health checker has stopped, as HTML
 * ('' when it hasn't). Said once for the page instead of once per row: every
 * enabled row is stale, or the server's roll-up is "unknown" with rows to
 * report on.
 */
function pollerBanner(payload, nowMs) {
    const enabled = ((payload && payload.services) || []).filter(service => service.enabled);
    if (enabled.length === 0) return '';
    const allStale = enabled.every(service => service.monitoring === 'stale');
    if (!allStale && !(payload && payload.overall_health === 'unknown')) return '';
    const checked = enabled.map(service => Date.parse(service.last_health_check)).filter(ms => !Number.isNaN(ms));
    const newest = checked.length ? new Date(Math.max(...checked)).toISOString() : null;
    const since = newest ? _since(newest, nowMs === undefined ? Date.now() : nowMs) : null;
    const text = since
        ? `Health checks haven't run since ${escapeHtml(since)}. The statuses below may be out of date.`
        : "Health checks haven't run yet. The statuses below may be out of date.";
    return `<div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4 mb-4" role="status"><p class="text-yellow-200">${text}</p></div>`;
}

/** "Not monitored · last checked 27 Sep" / "Not monitored · never checked" for a switched-off row. */
function monitoringLabel(service, refIso) {
    const checked = service && service.last_health_check ? formatAge(service.last_health_check, refIso) : null;
    return checked ? `Not monitored · last checked ${checked}` : 'Not monitored · never checked';
}

