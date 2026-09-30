/**
 * Install telemetry panel (System Configuration tab).
 *
 * Shows whether the pseudonymous install heartbeat is on, why, where it goes,
 * and the exact last payload sent. Owners can switch it off, send now, or
 * start a new identity. Every server-provided string is rendered with
 * textContent; nothing from the API is ever parsed as HTML.
 */

const TELEMETRY_POLL_COUNT = 3;
const TELEMETRY_POLL_INTERVAL_MS = 5000;

const TELEMETRY_REASONS = {
    enabled: 'On',
    admin_setting: 'Off (turned off here)',
    env_athena_telemetry: 'Off (ATHENA_TELEMETRY)',
    env_athena_telemetry_unrecognized: 'Off (ATHENA_TELEMETRY has an unrecognized value)',
    env_do_not_track: 'Off (DO_NOT_TRACK)',
    env_unreadable: 'Off (.env could not be read)',
    endpoint_unset: 'Off (ATHENA_TELEMETRY_ENDPOINT is empty)',
    endpoint_invalid: 'Off (ATHENA_TELEMETRY_ENDPOINT is not a valid https URL)',
    install_class_ci: 'Off (CI install)',
    install_class_test: 'Off (test install)',
    ephemeral_database: 'Off (in-memory database)',
};

let telemetryPollTimers = [];
let telemetryRequestSeq = 0;

function telemetryEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function telemetryRow(label, value) {
    const row = telemetryEl('div', 'flex flex-col sm:flex-row sm:gap-4 py-1');
    row.appendChild(telemetryEl('dt', 'text-xs text-gray-500 uppercase tracking-wide sm:w-40 shrink-0', label));
    row.appendChild(telemetryEl('dd', 'text-sm text-gray-300 break-all', value === null || value === undefined || value === '' ? '—' : value));
    return row;
}

function telemetryButton(label, className, onClick, disabled, title) {
    const button = telemetryEl('button', `px-4 py-2 min-h-[44px] rounded-lg text-sm font-medium ${className} disabled:opacity-50 disabled:cursor-not-allowed`, label);
    button.type = 'button';
    button.disabled = Boolean(disabled);
    if (title) button.title = title;
    button.addEventListener('click', onClick);
    return button;
}

function telemetryErrorText(detail, fallback) {
    if (detail && typeof detail === 'object' && detail.error) {
        const messages = {
            env_locked: 'Telemetry is switched off by the environment, so it can\'t be changed here.',
            disabled: 'Telemetry is off, so there is nothing to send or reset.',
            too_soon: 'A send was requested in the last 10 minutes. Try again later.',
            send_in_progress: 'A send is in progress. Try again in a minute.',
        };
        return messages[detail.error] || fallback;
    }
    return fallback;
}

async function telemetryRequest(method, path, body) {
    const options = { method, headers: getAuthHeaders({ 'Content-Type': 'application/json' }) };
    if (body !== undefined) options.body = JSON.stringify(body);
    const response = await fetch(path, options);
    let data = null;
    try {
        data = await response.json();
    } catch (err) {
        data = null;
    }
    return { ok: response.ok, status: response.status, data };
}

function renderTelemetryPanel(container, status, notice) {
    container.replaceChildren();
    const card = telemetryEl('div', 'bg-dark-card border border-dark-border rounded-xl p-6');

    const header = telemetryEl('div', 'flex flex-wrap items-center justify-between gap-3 mb-2');
    header.appendChild(telemetryEl('h3', 'text-lg font-semibold text-white', 'Install telemetry'));
    const label = TELEMETRY_REASONS[status.reason] || `Off (${status.reason})`;
    const pillColor = status.enabled ? 'bg-green-900/40 text-green-300 border-green-700' : 'bg-gray-800 text-gray-300 border-dark-border';
    const pill = telemetryEl('span', `text-xs px-3 py-1 rounded-full border ${pillColor}`, label);
    pill.setAttribute('role', 'status');
    header.appendChild(pill);
    card.appendChild(header);

    card.appendChild(telemetryEl('p', 'text-sm text-gray-400 mb-4',
        'A pseudonymous heartbeat, about once a day: version, install class, deployment shape, each LLM ' +
        'component\'s model family and whether it runs locally, and coarse feature and usage counts. ' +
        'No names, hosts, URLs, keys, queries or guest data. This is separate from the Privacy analytics mode, ' +
        'which records conversations locally.'));

    if (notice) {
        card.appendChild(telemetryEl('p', `text-sm mb-4 ${notice.error ? 'text-red-400' : 'text-green-400'}`, notice.text));
    }

    const facts = telemetryEl('dl', 'mb-4');
    facts.appendChild(telemetryRow('Endpoint', status.endpoint));
    facts.appendChild(telemetryRow('Installation ID', status.installation_id));
    facts.appendChild(telemetryRow('Install class', status.install_class));
    facts.appendChild(telemetryRow('Provenance', status.provenance));
    facts.appendChild(telemetryRow('Release channel', status.release_channel));
    facts.appendChild(telemetryRow('Last sent', status.last_success_at));
    facts.appendChild(telemetryRow('Last attempt', status.last_attempt_at));
    facts.appendChild(telemetryRow('Next due', status.next_due_at));
    if (status.last_error) {
        const error = status.last_error.status ? `${status.last_error.type} ${status.last_error.status}` : status.last_error.type;
        facts.appendChild(telemetryRow('Last error', error));
    }
    card.appendChild(facts);

    if (status.env_locked && !status.enabled) {
        card.appendChild(telemetryEl('p', 'text-sm text-yellow-300 mb-4',
            'Switched off by the environment or this install\'s setup. Change the environment variable ' +
            '(see docs/CONFIGURATION.md, "Telemetry") to turn it back on.'));
    }

    if (status.can_manage) {
        const controls = telemetryEl('div', 'flex flex-wrap gap-3 mb-4');
        const locked = status.env_locked;
        const lockedTitle = locked ? 'Locked by the environment' : '';
        controls.appendChild(telemetryButton(
            status.enabled ? 'Turn off' : 'Turn on',
            status.enabled ? 'bg-gray-700 hover:bg-gray-600 text-white' : 'bg-blue-600 hover:bg-blue-500 text-white',
            () => setTelemetryEnabled(!status.enabled), locked, lockedTitle));
        controls.appendChild(telemetryButton('Send now', 'bg-dark-bg border border-dark-border hover:border-blue-500 text-gray-200',
            sendTelemetryNow, !status.enabled, status.enabled ? 'Send one heartbeat now' : 'Telemetry is off'));
        controls.appendChild(telemetryButton('Reset telemetry identity', 'bg-dark-bg border border-red-800 hover:border-red-500 text-red-300',
            resetTelemetryIdentity, !status.enabled, 'Start a new installation ID'));
        card.appendChild(controls);
    }

    const details = telemetryEl('details', 'mt-2');
    details.appendChild(telemetryEl('summary', 'cursor-pointer text-sm text-blue-400 hover:text-blue-300',
        status.last_payload ? 'Show the last payload sent' : 'No payload has been sent yet'));
    if (status.last_payload) {
        details.appendChild(telemetryEl('pre', 'mt-2 p-3 bg-dark-bg rounded-lg text-xs text-gray-300 overflow-x-auto',
            JSON.stringify(status.last_payload, null, 2)));
    }
    card.appendChild(details);
    container.appendChild(card);
}

function renderTelemetryError(container, text) {
    container.replaceChildren();
    const card = telemetryEl('div', 'bg-dark-card border border-dark-border rounded-xl p-6');
    card.appendChild(telemetryEl('h3', 'text-lg font-semibold text-white mb-2', 'Install telemetry'));
    card.appendChild(telemetryEl('p', 'text-sm text-red-400', text));
    container.appendChild(card);
}

async function loadTelemetryPanel(notice) {
    const container = document.getElementById('telemetry-panel');
    if (!container) return;
    const seq = ++telemetryRequestSeq;
    try {
        const result = await telemetryRequest('GET', '/api/telemetry/status');
        if (seq !== telemetryRequestSeq) return;
        if (!result.ok || !result.data) {
            renderTelemetryError(container, 'Couldn\'t load the telemetry status. Refresh the page to try again.');
            return;
        }
        renderTelemetryPanel(container, result.data, notice);
    } catch (err) {
        if (seq === telemetryRequestSeq) {
            renderTelemetryError(container, 'Couldn\'t reach admin-backend for the telemetry status.');
        }
    }
}

async function setTelemetryEnabled(enabled) {
    const result = await telemetryRequest('PUT', '/api/telemetry/settings', { enabled });
    const notice = result.ok
        ? { text: enabled ? 'Telemetry is on.' : 'Telemetry is off. Nothing more will be sent.' }
        : { error: true, text: telemetryErrorText(result.data && result.data.detail, 'The setting could not be changed.') };
    await loadTelemetryPanel(notice);
}

function clearTelemetryPolls() {
    telemetryPollTimers.forEach((timer) => clearTimeout(timer));
    telemetryPollTimers = [];
}

async function sendTelemetryNow() {
    const result = await telemetryRequest('POST', '/api/telemetry/send');
    if (result.status !== 202) {
        await loadTelemetryPanel({ error: true, text: telemetryErrorText(result.data && result.data.detail, 'The send could not be queued.') });
        return;
    }
    await loadTelemetryPanel({ text: 'Send queued. This panel refreshes for the next 15 seconds.' });
    clearTelemetryPolls();
    for (let i = 1; i <= TELEMETRY_POLL_COUNT; i += 1) {
        telemetryPollTimers.push(setTimeout(() => loadTelemetryPanel(), i * TELEMETRY_POLL_INTERVAL_MS));
    }
}

async function resetTelemetryIdentity() {
    const confirmed = confirm(
        'Reset the telemetry identity?\n\n' +
        'The next heartbeat is sent under a new installation ID. The old ID\'s data stays at the collector ' +
        'until its retention period ends, and the two can still be linked by timing, so this is not an ' +
        'anonymity guarantee.');
    if (!confirmed) return;
    const result = await telemetryRequest('POST', '/api/telemetry/reset-identity');
    const notice = result.ok
        ? { text: 'Identity reset. A new ID is created on the next heartbeat.' }
        : { error: true, text: telemetryErrorText(result.data && result.data.detail, 'The identity could not be reset.') };
    await loadTelemetryPanel(notice);
}

function initTelemetryPanel() {
    clearTelemetryPolls();
    loadTelemetryPanel();
}

function destroyTelemetryPanel() {
    clearTelemetryPolls();
    telemetryRequestSeq += 1;
}

if (typeof window !== 'undefined') {
    window.initTelemetryPanel = initTelemetryPanel;
    window.destroyTelemetryPanel = destroyTelemetryPanel;
}
