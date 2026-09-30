/**
 * Guest Mode Management - Frontend JavaScript
 * Handles CRUD operations for guest entries and display of guest history
 */

// State variables
let guestHistoryData = [];
let guestHistoryOffset = 0;
let guestHistoryLimit = 20;
let guestHistoryTotal = 0;
let filterTimeout = null;

// Test mode state
let guestTestModeEnabled = false;

// Multi-guest display state
let expandedReservations = new Set();
let guestsByEvent = {};

// ============================================================================
// Data Loading Functions
// ============================================================================

async function loadGuestModeData() {
    _gmShowStatusPlaceholder();
    await Promise.all([
        loadCurrentGuests(),
        loadUpcomingGuests(),
        loadGuestHistory(0),
        updateGuestModeStatus()
    ]);
    // Managed interval: cleared on tab switch, skipped while the tab is hidden.
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('guest-mode-status', updateGuestModeStatus, GUEST_MODE_STATUS_REFRESH_MS);
    }
}

async function loadCurrentGuests() {
    const container = document.getElementById('current-guests-container');

    try {
        const data = await apiRequest('/api/guest-mode/events/current');

        if (data.entries.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No current guests</p>
                </div>
            `;
            return;
        }

        // Fetch guest counts for all events
        const eventIds = data.entries.map(e => e.id).join(',');
        let guestCounts = {};
        try {
            const guestsData = await apiRequest(`/api/guests/by-events?event_ids=${eventIds}`);
            guestsByEvent = guestsData;
            for (const [eventId, guests] of Object.entries(guestsData)) {
                guestCounts[eventId] = guests.length;
            }
        } catch (e) {
            console.error('Failed to fetch guest counts', e);
        }

        container.innerHTML = data.entries.map(entry => {
            const guestCount = guestCounts[entry.id] || entry.guest_count || 1;
            const isExpanded = expandedReservations.has(entry.id);
            const guests = guestsByEvent[entry.id] || [];

            return `
            <div class="p-4 bg-dark-bg rounded-lg mb-3 border-l-4 ${entry.is_test ? 'border-yellow-500' : 'border-green-500'}" id="reservation-${entry.id}">
                <div class="flex justify-between items-start">
                    <div class="flex-1">
                        <div class="font-medium text-white flex items-center gap-2">
                            ${entry.is_test ? '<span class="text-yellow-400 text-xs">[TEST]</span>' : ''}
                            ${escapeHtml(entry.guest_name || 'Unknown')}
                            ${guestCount > 1 ? `<span class="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full">+${guestCount - 1} guests</span>` : ''}
                            ${infoIcon('guest-current-name')}
                        </div>
                        <div class="text-sm text-gray-400 mt-1">
                            ${formatDateRange(entry.checkin, entry.checkout)}
                        </div>
                        ${entry.guest_email ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(entry.guest_email)}</div>` : ''}
                    </div>
                    <div class="flex items-center gap-2">
                        <button onclick="toggleReservationExpand(${entry.id})"
                            class="text-blue-400 hover:text-blue-300 text-sm px-2 py-1 rounded border border-blue-400/30 hover:border-blue-400">
                            ${isExpanded ? '▼ Hide' : '▶ Show'} Guests
                        </button>
                        <span class="badge ${entry.is_test ? 'bg-yellow-600/20 text-yellow-400' : 'badge-success'}">
                            ${entry.is_test ? 'Test' : 'Active'}
                        </span>
                    </div>
                </div>

                ${isExpanded ? `
                <div class="mt-4 pt-4 border-t border-dark-border">
                    <div class="flex justify-between items-center mb-2">
                        <span class="text-sm text-gray-400">Guests (${guests.length})</span>
                        <button onclick="showAddGuestToReservationModal(${entry.id})"
                            class="text-xs text-green-400 hover:text-green-300">
                            + Add Guest
                        </button>
                    </div>
                    <div class="space-y-2">
                        ${guests.map(guest => `
                            <div class="flex justify-between items-center p-2 bg-dark-card rounded">
                                <div>
                                    <span class="text-white">${escapeHtml(guest.name)}</span>
                                    ${guest.is_primary ? '<span class="text-xs text-blue-400 ml-2">(Primary)</span>' : ''}
                                    ${guest.is_test ? '<span class="text-xs text-yellow-400 ml-2">[Test]</span>' : ''}
                                </div>
                                <div class="flex gap-2 text-xs">
                                    ${guest.email ? `<span class="text-gray-500">${escapeHtml(guest.email)}</span>` : ''}
                                    ${!guest.is_primary ? `
                                        <button onclick="deleteGuestFromReservation(${guest.id})" class="text-red-400 hover:text-red-300">Remove</button>
                                    ` : ''}
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
                ` : ''}
            </div>
            `;
        }).join('');
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        container.innerHTML = `
            <div class="text-center text-red-400 py-4">
                <p>Failed to load: ${escapeHtml(errorMsg)}</p>
            </div>
        `;
    }
}

async function loadUpcomingGuests() {
    const container = document.getElementById('upcoming-guests-container');
    const daysSelect = document.getElementById('upcoming-days');
    const days = parseInt(daysSelect.value) || 30;

    try {
        const data = await apiRequest(`/api/guest-mode/events/upcoming?days=${days}`);

        if (data.entries.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No upcoming guests in next ${days} days</p>
                </div>
            `;
            return;
        }

        container.innerHTML = data.entries.map(entry => `
            <div class="p-4 bg-dark-bg rounded-lg mb-3 border-l-4 ${entry.is_test ? 'border-yellow-500' : 'border-blue-500'}">
                <div class="flex justify-between items-start">
                    <div>
                        <div class="font-medium text-white flex items-center gap-2">
                            ${entry.is_test ? '<span class="text-yellow-400 text-xs">[TEST]</span>' : ''}
                            ${escapeHtml(entry.guest_name || 'Unknown')}
                            ${entry.guest_count > 1 ? `<span class="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full">+${entry.guest_count - 1} guests</span>` : ''}
                            ${infoIcon('guest-upcoming-name')}
                        </div>
                        <div class="text-sm text-gray-400 mt-1">
                            ${formatDateRange(entry.checkin, entry.checkout)}
                        </div>
                        <div class="text-xs text-gray-500 mt-1">
                            Arriving in ${getDaysUntil(entry.checkin)} days
                        </div>
                    </div>
                    <span class="badge ${entry.is_test ? 'bg-yellow-600/20 text-yellow-400' : (entry.status === 'confirmed' ? 'badge-success' : 'badge-warning')}">
                        ${entry.is_test ? 'Test' : entry.status}
                    </span>
                </div>
            </div>
        `).join('');
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        container.innerHTML = `
            <div class="text-center text-red-400 py-4">
                <p>Failed to load: ${escapeHtml(errorMsg)}</p>
            </div>
        `;
    }
}

async function loadGuestHistory(offset = 0) {
    const container = document.getElementById('guest-history-table-body');
    const includeDeleted = document.getElementById('show-deleted-guests').checked;
    const searchName = document.getElementById('guest-search').value.trim();

    guestHistoryOffset = offset;

    try {
        let url = `/api/guest-mode/history?limit=${guestHistoryLimit}&offset=${offset}&include_deleted=${includeDeleted}&include_test=${guestTestModeEnabled}`;
        if (searchName) {
            url += `&guest_name=${encodeURIComponent(searchName)}`;
        }

        const data = await apiRequest(url);
        guestHistoryData = data.entries;
        guestHistoryTotal = data.total;

        if (data.entries.length === 0) {
            container.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-gray-400 py-8">
                        <div class="text-2xl mb-2">🏠</div>
                        <p>No guest entries found</p>
                    </td>
                </tr>
            `;
            updatePagination();
            return;
        }

        container.innerHTML = data.entries.map(entry => `
            <tr class="${entry.deleted_at ? 'opacity-50' : ''} ${entry.is_test ? 'bg-yellow-900/10' : ''}">
                <td class="text-white font-medium">
                    <span class="flex items-center gap-1">
                        ${entry.is_test ? '<span class="text-yellow-400 text-xs">[TEST]</span>' : ''}
                        ${escapeHtml(entry.guest_name || 'Unknown')}
                        ${entry.created_by === 'manual' ? '<span class="ml-2 text-xs text-purple-400">(manual)</span>' : ''}
                        ${entry.guest_count > 1 ? `<span class="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full ml-1">+${entry.guest_count - 1}</span>` : ''}
                        ${infoIcon('guest-history-name')}
                    </span>
                </td>
                <td>${formatDate(entry.checkin)}</td>
                <td>${formatDate(entry.checkout)}</td>
                <td>
                    ${entry.guest_email ? `<div class="text-xs">${escapeHtml(entry.guest_email)}</div>` : ''}
                    ${entry.guest_phone ? `<div class="text-xs text-gray-500">${escapeHtml(entry.guest_phone)}</div>` : ''}
                    ${!entry.guest_email && !entry.guest_phone ? '<span class="text-gray-500">-</span>' : ''}
                </td>
                <td>
                    <span class="tag ${entry.source === 'manual' ? 'tag-source' : 'tag-backend'}">
                        ${entry.source}
                    </span>
                </td>
                <td>
                    <span class="badge ${entry.is_test ? 'bg-yellow-600/20 text-yellow-400' : getStatusClass(entry.status)}">
                        ${entry.is_test ? 'Test' : entry.status}
                    </span>
                </td>
                <td>
                    <div class="flex gap-2">
                        <button onclick="editGuestEntry(${entry.id})"
                            class="text-blue-400 hover:text-blue-300 text-sm">
                            Edit
                        </button>
                        ${!entry.deleted_at ? `
                            <button onclick="deleteGuestEntry(${entry.id}, '${escapeJsAttr(entry.created_by)}')"
                                class="text-red-400 hover:text-red-300 text-sm">
                                Delete
                            </button>
                        ` : ''}
                    </div>
                </td>
            </tr>
        `).join('');

        updatePagination();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        container.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-red-400 py-8">
                    <p>Failed to load guest history: ${escapeHtml(errorMsg)}</p>
                </td>
            </tr>
        `;
    }
}

// ATHENA-127 D7: the Guest Mode page shows the mode service's OWN decision,
// not just a DB-derived guess -- see docs/CONFIGURATION.md "Guest-mode
// booking source" for the precedence/freshness model this banner reflects.
const GUEST_MODE_STATUS_REFRESH_MS = 30000;

// Literal class strings per mode (no `${color}` fragments), so every class
// name exists verbatim in source.
function _gmTheme(label, icon, color) {
    const themes = {
        green: ['bg-green-900/20 border-green-700/50', 'text-green-200', 'text-green-300/70'],
        blue: ['bg-blue-900/20 border-blue-700/50', 'text-blue-200', 'text-blue-300/70'],
        red: ['bg-red-900/20 border-red-700/50', 'text-red-200', 'text-red-300/70'],
        gray: ['bg-gray-900/20 border-gray-700/50', 'text-gray-200', 'text-gray-300/70'],
    };
    const [box, title, body] = themes[color];
    return {
        label,
        icon,
        box: `p-4 border rounded-lg flex items-center gap-3 ${box}`,
        title: `font-medium ${title}`,
        body: `text-sm ${body}`,
        meta: `text-xs mt-1 ${body}`,
    };
}

const _GM_MODE_THEMES = {
    guest: _gmTheme('Guest', '🏠', 'green'),
    owner: _gmTheme('Owner', '🔑', 'blue'),
    degraded: _gmTheme('Degraded', '⚠️', 'red'),
};

const _GM_SOURCE_LABELS = {
    'admin': 'Admin (required)',
    'ical': 'iCal (required)',
    'admin+ical': 'Admin (required) + iCal (advisory)',
};

const _GM_BOOKINGS_STATUS_WORDS = {
    fresh: 'fresh',
    stale: 'stale (the last fetch failed)',
    expired: 'expired',
    never_loaded: 'not loaded yet',
    not_required: 'not used while guest mode is off',
};

const _GM_TRANSPORT_ERRORS = new Set([
    'ConnectError', 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
    'TimeoutException', 'ReadError', 'RemoteProtocolError',
]);

function _gmHumanAge(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null;
    if (seconds < 60) return 'just now';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(seconds / 3600);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.round(seconds / 86400)} d ago`;
}

function _gmTimezoneText(modeStatus) {
    if (!modeStatus.reachable) return 'unknown (mode service unreachable)';
    const zone = modeStatus.property_timezone || 'UTC';
    return modeStatus.property_timezone_valid === false ? `${zone} (invalid, using UTC)` : zone;
}

// The mode service's reason carries a raw ISO checkout ("(until 2026-...)");
// show it in the property's timezone when that zone is valid (the stay's
// times are the property's), else in the browser's.
function _gmFormatReason(reason, modeStatus) {
    const options = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
    if (modeStatus && modeStatus.property_timezone_valid === true && modeStatus.property_timezone) {
        options.timeZone = modeStatus.property_timezone;
    }
    return String(reason || '').replace(/\(until ([0-9]{4}-[0-9]{2}-[0-9]{2}T[^)]+)\)/, (match, iso) => {
        const when = new Date(iso);
        if (Number.isNaN(when.getTime())) return match;
        let text;
        try {
            text = when.toLocaleString('en-US', options);
        } catch (e) {
            // A zone the browser doesn't know: fall back to its own.
            delete options.timeZone;
            text = when.toLocaleString('en-US', options);
        }
        return `(until ${text})`;
    });
}

function _gmUnreachableMessage(modeStatus) {
    const error = modeStatus.error;
    if (error === 'mode_service_url_invalid') {
        return 'MODE_SERVICE_URL on admin-backend isn\'t a valid http(s) URL (check the host and port, for example http://athena-mode-service:8022) and restart admin-backend.';
    }
    if (error === 'mode_service_url_unset') {
        return 'Admin-backend has no mode service address. Set MODE_SERVICE_URL on admin-backend (for example http://athena-mode-service:8022) and restart it.';
    }
    if (error === 'ssrf_blocked') {
        return 'The mode service URL is blocked by the SSRF allowlist. Add the mode service ClusterIP or CIDR to HEALTH_POLL_ALLOWED_PRIVATE_HOSTS on admin-backend.';
    }
    if (error === 'http_error' && (modeStatus.detail === '401' || modeStatus.detail === '403')) {
        return 'The mode service rejected admin-backend\'s service key. Set the same SERVICE_API_KEY on admin-backend and the mode service.';
    }
    if (error === 'http_error') {
        return `The mode service answered HTTP ${modeStatus.detail || 'error'}. Check the mode service logs.`;
    }
    if (error === 'malformed_response') {
        return 'The mode service sent an unexpected response. Check that MODE_SERVICE_URL points at the mode service.';
    }
    if (error === 'proxy_request_failed') {
        return 'Admin-backend didn\'t answer the mode-status request. Refresh the page, or sign in again.';
    }
    if (_GM_TRANSPORT_ERRORS.has(error)) {
        return 'The mode service isn\'t responding. Check that its pod is running.';
    }
    return 'The mode service couldn\'t be reached. Check that its pod is running and look at its logs.';
}

function _modeStatusWarnings(modeStatus, { guestModeEnabled, hasCurrentGuests, calendarUrlSet, calendarUrlShadowsLodgifyApi }) {
    // Unreachable is the banner's own headline; repeating it here adds nothing.
    if (!modeStatus.reachable) return [];
    const warnings = [];
    if (guestModeEnabled === true && modeStatus.bookings_status !== 'not_required'
        && hasCurrentGuests && modeStatus.mode === 'owner' && !modeStatus.override_active) {
        warnings.push('The database lists a current stay, but the mode service reports owner mode. Check that the stay is confirmed and its calendar source has synced.');
    }
    if (modeStatus.mode === 'degraded') {
        warnings.push('Degraded mode refuses locks, cameras and other restricted devices for everyone. Check that the mode service can reach admin-backend; it recovers on its next successful fetch.');
    } else if (['expired', 'never_loaded'].includes(modeStatus.bookings_status)) {
        warnings.push(`Booking data is ${_GM_BOOKINGS_STATUS_WORDS[modeStatus.bookings_status]}. Check that the mode service can reach admin-backend.`);
    }
    const advisoryUnusable = Object.values(modeStatus.bookings_sources || {})
        .some(src => src && src.required === false && ['never_loaded', 'expired'].includes(src.status));
    if (calendarUrlSet && advisoryUnusable) {
        warnings.push('The legacy iCal URL on this page isn\'t being read, so its bookings are ignored. It must be an https:// URL the mode service can reach; a private host needs SITESCRAPER_ALLOWED_PRIVATE_HOSTS.');
    }
    if (calendarUrlShadowsLodgifyApi === true) {
        warnings.push('The legacy iCal URL points at the Lodgify export while a Lodgify API key is set. Its turnover-day slices add guest time. Clear it; bookings come from Calendar Sources.');
    }
    if (modeStatus.property_timezone_valid === false) {
        warnings.push('The property timezone is invalid, so booking times are read as UTC. Set DEFAULT_TIMEZONE to an IANA zone (for example America/New_York) on admin-backend and the mode service.');
    }
    return warnings;
}

function _gmMetadataLine(modeStatus) {
    const status = modeStatus.bookings_status;
    const statusWords = _GM_BOOKINGS_STATUS_WORDS[status] || status || 'unknown';
    const age = status === 'not_required' ? null : _gmHumanAge(modeStatus.bookings_age_seconds);
    const count = Number.isInteger(Number(modeStatus.events_count)) ? Number(modeStatus.events_count) : 0;
    const source = _GM_SOURCE_LABELS[modeStatus.bookings_source] || modeStatus.bookings_source || 'unknown';
    const parts = [
        `Bookings: ${statusWords}${age ? ` · updated ${age}` : ''}`,
        `${count} in window`,
        `Source: ${source}`,
        `Property timezone: ${_gmTimezoneText(modeStatus)}`,
    ];
    return parts.join(' · ');
}

// Screen readers hear only "Mode: X" plus the warning count, and only when
// that text changes -- not the whole banner on every 30 s refresh.
function _gmAnnounce(text) {
    const live = document.getElementById('guest-mode-status-live');
    if (live && live.textContent !== text) {
        live.textContent = text;
    }
}

function _gmShowStatusPlaceholder() {
    const banner = document.getElementById('guest-mode-status-banner');
    if (!banner) return;
    const placeholder = document.createElement('div');
    placeholder.className = 'p-4 bg-dark-bg border border-dark-border rounded-lg text-gray-400 text-sm';
    placeholder.textContent = 'Checking mode service…';
    banner.replaceChildren(placeholder);
}

function _gmSettled(result, fallback) {
    return result.status === 'fulfilled' ? result.value : fallback;
}

async function updateGuestModeStatus() {
    const banner = document.getElementById('guest-mode-status-banner');
    if (!banner) return;

    const [configResult, currentResult, modeResult] = await Promise.allSettled([
        apiRequest('/api/guest-mode/config'),
        apiRequest('/api/guest-mode/events/current'),
        apiRequest('/api/guest-mode/mode-status'),
    ]);
    const config = _gmSettled(configResult, null);
    const currentData = _gmSettled(currentResult, null);
    const modeStatus = _gmSettled(modeResult, null) || { reachable: false, error: 'proxy_request_failed' };

    const hasCurrentGuests = !!(currentData && currentData.entries && currentData.entries.length > 0);
    const dbLine = currentData
        ? `DB: ${currentData.entries.length} guest(s) currently staying`
        : 'DB: current stays unavailable';

    // bob L10: the manual-entry modal shows the property timezone next
    // to the (browser-local) time inputs.
    const tzNoteEl = document.getElementById('guest-modal-timezone-note');
    if (tzNoteEl) {
        tzNoteEl.textContent = `Times are entered in your browser's timezone; property timezone is ${_gmTimezoneText(modeStatus)}.`;
    }

    // ATHENA-69 Pass H (valerie r1, Medium): a pre-D30 legacy PIN hash
    // can never be verified -- POST verify-pin always answers
    // "not_configured" for it. Fixed string, no interpolation, so no
    // escaping is needed here.
    const pinResetNotice = config && config.owner_pin_needs_reset ? `
        <div class="p-4 bg-yellow-900/20 border border-yellow-700/50 rounded-lg flex items-center gap-3 mt-3">
            <span class="text-2xl" aria-hidden="true">🔑</span>
            <div>
                <div class="text-yellow-200 font-medium">Owner PIN must be set again</div>
                <div class="text-yellow-300/70 text-sm">
                    This PIN was set before a security upgrade and can no longer be verified. Set a new PIN below to restore the voice owner-mode override.
                </div>
            </div>
        </div>
    ` : '';

    const pinStatusEl = document.getElementById('owner-pin-status-text');
    if (pinStatusEl && config) {
        if (config.owner_pin_needs_reset) {
            pinStatusEl.textContent = 'PIN must be reset';
            pinStatusEl.className = 'text-xs text-yellow-400 mb-2';
        } else if (config.owner_pin_configured) {
            pinStatusEl.textContent = 'PIN configured';
            pinStatusEl.className = 'text-xs text-green-400 mb-2';
        } else {
            pinStatusEl.textContent = 'PIN not configured';
            pinStatusEl.className = 'text-xs text-gray-500 mb-2';
        }
    }

    const warnings = _modeStatusWarnings(modeStatus, {
        guestModeEnabled: config ? config.enabled : undefined,
        hasCurrentGuests,
        calendarUrlSet: !!(config && config.calendar_url),
        calendarUrlShadowsLodgifyApi: !!(config && config.calendar_url_shadows_lodgify_api),
    });
    const warningBanner = warnings.length ? `
        <div class="p-3 bg-yellow-900/20 border border-yellow-700/50 rounded-lg text-yellow-200 text-sm mt-3 space-y-1">
            ${warnings.map(w => `<div><span aria-hidden="true">⚠️</span> ${escapeHtml(w)}</div>`).join('')}
        </div>
    ` : '';

    let primaryBanner;
    let announcement;
    if (modeStatus.reachable) {
        const theme = _GM_MODE_THEMES[modeStatus.mode] || _gmTheme(modeStatus.mode || 'Unknown', '❔', 'gray');
        announcement = `Mode: ${theme.label}${warnings.length ? `, ${warnings.length} warning${warnings.length === 1 ? '' : 's'}` : ''}`;
        primaryBanner = `
            <div class="${escapeHtml(theme.box)}">
                <span class="text-2xl" aria-hidden="true">${escapeHtml(theme.icon)}</span>
                <div>
                    <div class="${escapeHtml(theme.title)}">Mode: ${escapeHtml(theme.label)}</div>
                    <div class="${escapeHtml(theme.body)}">${escapeHtml(_gmFormatReason(modeStatus.reason, modeStatus))}</div>
                    <div class="${escapeHtml(theme.meta)}">${escapeHtml(_gmMetadataLine(modeStatus))}</div>
                    <div class="${escapeHtml(theme.meta)}">${escapeHtml(dbLine)}</div>
                </div>
            </div>
        `;
    } else {
        announcement = 'Mode service unreachable';
        primaryBanner = `
            <div class="p-4 bg-yellow-900/20 border border-yellow-700/50 rounded-lg flex items-center gap-3">
                <span class="text-2xl" aria-hidden="true">⚠️</span>
                <div>
                    <div class="text-yellow-200 font-medium">Mode service unreachable</div>
                    <div class="text-yellow-300/70 text-sm">${escapeHtml(_gmUnreachableMessage(modeStatus))}</div>
                    <div class="text-yellow-300/70 text-xs mt-1">${escapeHtml(dbLine)}</div>
                </div>
            </div>
        `;
    }

    banner.innerHTML = `${primaryBanner}${warningBanner}${pinResetNotice}`;
    _gmAnnounce(announcement);
}

// ============================================================================
// Owner PIN
// ============================================================================

async function setOwnerPin() {
    const pinInput = document.getElementById('owner-pin-input');
    const confirmInput = document.getElementById('owner-pin-confirm');
    const errorEl = document.getElementById('owner-pin-error');
    const setBtn = document.getElementById('owner-pin-set-btn');

    const pin = pinInput.value;
    const confirmPin = confirmInput.value;

    const clearInputs = () => {
        pinInput.value = '';
        confirmInput.value = '';
    };

    // Clears both inputs on every validation-error exit path -- codex review
    // on ee7e02a (Medium): the 6-digit-format branch previously returned
    // without clearing, leaving a rejected PIN sitting in the field.
    const showFieldError = (message) => {
        clearInputs();
        errorEl.textContent = message;
        errorEl.classList.remove('hidden');
    };

    errorEl.textContent = '';
    errorEl.classList.add('hidden');

    if (!/^[0-9]{6}$/.test(pin)) {
        showFieldError('PIN must be exactly 6 digits.');
        return;
    }
    if (pin !== confirmPin) {
        showFieldError('PINs do not match.');
        return;
    }

    setBtn.disabled = true;
    try {
        await apiRequest('/api/guest-mode/config', {
            method: 'PATCH',
            body: JSON.stringify({ owner_pin: pin })
        });
        clearInputs();
        safeShowToast('Owner PIN set', 'success');
        updateGuestModeStatus();
    } catch (error) {
        clearInputs();
        safeShowToast(error?.message || 'Failed to set owner PIN', 'error');
    } finally {
        setBtn.disabled = false;
    }
}

// ============================================================================
// Pagination
// ============================================================================

function updatePagination() {
    const countEl = document.getElementById('guest-history-count');
    const prevBtn = document.getElementById('prev-page-btn');
    const nextBtn = document.getElementById('next-page-btn');

    countEl.textContent = guestHistoryTotal;

    prevBtn.disabled = guestHistoryOffset === 0;
    nextBtn.disabled = guestHistoryOffset + guestHistoryLimit >= guestHistoryTotal;
}

// ============================================================================
// Filter Functions
// ============================================================================

function filterGuestHistory() {
    // Debounce the search
    if (filterTimeout) {
        clearTimeout(filterTimeout);
    }
    filterTimeout = setTimeout(() => {
        loadGuestHistory(0);
    }, 300);
}

// ============================================================================
// Modal Functions
// ============================================================================

function showAddGuestModal() {
    const modal = document.getElementById('guest-modal');
    const title = document.getElementById('guest-modal-title');
    const form = document.getElementById('guest-form');
    const warning = document.getElementById('guest-form-ical-warning');

    // Reset form
    form.reset();
    document.getElementById('guest-entry-id').value = '';
    document.getElementById('guest-entry-source').value = 'manual';

    // Set default dates
    const now = new Date();
    const tomorrow = new Date(now);
    tomorrow.setDate(tomorrow.getDate() + 1);
    const nextWeek = new Date(now);
    nextWeek.setDate(nextWeek.getDate() + 7);

    document.getElementById('guest-checkin').value = formatDateTimeLocal(tomorrow);
    document.getElementById('guest-checkout').value = formatDateTimeLocal(nextWeek);

    // Set test mode checkbox based on current toggle state
    const testCheckbox = document.getElementById('guest-is-test');
    if (testCheckbox) {
        testCheckbox.checked = guestTestModeEnabled;
    }

    // Enable all fields for new entry
    setFormFieldsEnabled(true);
    warning.classList.add('hidden');

    title.textContent = 'Add Guest Entry';
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

async function editGuestEntry(id) {
    const entry = guestHistoryData.find(e => e.id === id);
    if (!entry) {
        console.error('Entry not found:', id);
        return;
    }

    const modal = document.getElementById('guest-modal');
    const title = document.getElementById('guest-modal-title');
    const warning = document.getElementById('guest-form-ical-warning');

    // Populate form
    document.getElementById('guest-entry-id').value = entry.id;
    document.getElementById('guest-entry-source').value = entry.created_by;
    document.getElementById('guest-name').value = entry.guest_name || '';
    document.getElementById('guest-status').value = entry.status || 'confirmed';
    document.getElementById('guest-checkin').value = formatDateTimeLocal(new Date(entry.checkin));
    document.getElementById('guest-checkout').value = formatDateTimeLocal(new Date(entry.checkout));
    document.getElementById('guest-email').value = entry.guest_email || '';
    document.getElementById('guest-phone').value = entry.guest_phone || '';
    document.getElementById('guest-notes').value = entry.notes || '';

    // Set field editability based on source
    const isManual = entry.created_by === 'manual';
    setFormFieldsEnabled(isManual);

    if (isManual) {
        warning.classList.add('hidden');
    } else {
        warning.classList.remove('hidden');
    }

    title.textContent = 'Edit Guest Entry';
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function setFormFieldsEnabled(isManual) {
    const fields = ['guest-name', 'guest-checkin', 'guest-checkout', 'guest-email', 'guest-phone'];
    fields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.disabled = !isManual;
            field.classList.toggle('opacity-50', !isManual);
            field.classList.toggle('cursor-not-allowed', !isManual);
        }
    });
}

function closeGuestModal(event) {
    if (event && event.target !== event.currentTarget) return;
    const modal = document.getElementById('guest-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

// ============================================================================
// CRUD Operations
// ============================================================================

async function saveGuestEntry(event) {
    event.preventDefault();

    const id = document.getElementById('guest-entry-id').value;
    const source = document.getElementById('guest-entry-source').value;
    const isNew = !id;

    const data = {
        guest_name: document.getElementById('guest-name').value,
        checkin: new Date(document.getElementById('guest-checkin').value).toISOString(),
        checkout: new Date(document.getElementById('guest-checkout').value).toISOString(),
        guest_email: document.getElementById('guest-email').value || null,
        guest_phone: document.getElementById('guest-phone').value || null,
        notes: document.getElementById('guest-notes').value || null,
        status: document.getElementById('guest-status').value,
        is_test: document.getElementById('guest-is-test')?.checked || false
    };

    // For non-manual entries, only send allowed fields
    if (!isNew && source !== 'manual') {
        const allowedData = {
            notes: data.notes,
            status: data.status
        };
        Object.assign(data, {});
        Object.assign(data, allowedData);
    }

    try {
        if (isNew) {
            await apiRequest('/api/guest-mode/events', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        } else {
            await apiRequest(`/api/guest-mode/events/${id}`, {
                method: 'PATCH',
                body: JSON.stringify(data)
            });
        }

        closeGuestModal();
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to save guest entry: ${errorMsg}`);
    }
}

async function deleteGuestEntry(id, createdBy) {
    // ATHENA-127 step 6b: synced (iCal/Lodgify) rows are now deletable too --
    // a fixed, non-interpolated string, so no escaping is needed for confirm().
    const confirmMessage = createdBy === 'manual'
        ? 'Are you sure you want to delete this guest entry? This action cannot be undone.'
        : 'This booking came from a calendar sync. Deleting hides it from guest mode permanently; re-syncing will not bring it back. Continue?';
    if (!confirm(confirmMessage)) {
        return;
    }

    try {
        await apiRequest(`/api/guest-mode/events/${id}`, {
            method: 'DELETE'
        });
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to delete guest entry: ${errorMsg}`);
    }
}

// ============================================================================
// Helper Functions
// ============================================================================

function formatDateRange(checkin, checkout) {
    const checkInDate = new Date(checkin);
    const checkOutDate = new Date(checkout);

    const options = { month: 'short', day: 'numeric' };
    const checkInStr = checkInDate.toLocaleDateString('en-US', options);
    const checkOutStr = checkOutDate.toLocaleDateString('en-US', options);

    return `${checkInStr} - ${checkOutStr}`;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric'
    });
}

function formatDateTimeLocal(date) {
    const d = new Date(date);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function getDaysUntil(dateStr) {
    const now = new Date();
    const target = new Date(dateStr);
    const diffTime = target - now;
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
    return Math.max(0, diffDays);
}

function getStatusClass(status) {
    switch (status) {
        case 'confirmed':
            return 'badge-success';
        case 'pending':
            return 'badge-warning';
        case 'cancelled':
            return 'bg-red-900/20 text-red-400';
        case 'blocked':
            return 'bg-gray-800 text-gray-400';
        default:
            return '';
    }
}

// Note: escapeHtml is defined in app.js

// ============================================================================
// Test Mode Functions
// ============================================================================

function toggleGuestTestMode() {
    guestTestModeEnabled = document.getElementById('guest-test-mode').checked;

    // Show/hide clear test data button
    const clearBtn = document.getElementById('clear-test-data-btn');
    if (clearBtn) {
        clearBtn.classList.toggle('hidden', !guestTestModeEnabled);
    }

    // Reload data with test filter
    loadGuestModeData();
}

async function clearTestData() {
    if (!confirm('Are you sure you want to delete ALL test reservations and guests? This cannot be undone.')) {
        return;
    }

    try {
        const result = await apiRequest('/api/guest-mode/test-data', {
            method: 'DELETE'
        });

        alert(`Cleared ${result.deleted_events} test reservations and ${result.deleted_guests} test guests.`);
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to clear test data: ${errorMsg}`);
    }
}

// ============================================================================
// Multi-Guest Functions
// ============================================================================

function toggleReservationExpand(eventId) {
    if (expandedReservations.has(eventId)) {
        expandedReservations.delete(eventId);
    } else {
        expandedReservations.add(eventId);
        loadGuestsForEvent(eventId);
    }
    // Re-render current guests to update expand state
    loadCurrentGuests();
}

async function loadGuestsForEvent(eventId) {
    try {
        const guests = await apiRequest(`/api/guests?calendar_event_id=${eventId}`);
        guestsByEvent[eventId] = guests;
    } catch (error) {
        console.error('Failed to load guests for event', eventId, error);
    }
}

function showAddGuestToReservationModal(eventId) {
    // Store the event ID for the form submission
    document.getElementById('add-guest-event-id').value = eventId;

    // Show modal
    const modal = document.getElementById('add-guest-to-reservation-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');

    // Clear form
    document.getElementById('new-guest-name-for-reservation').value = '';
    document.getElementById('new-guest-email-for-reservation').value = '';
    document.getElementById('new-guest-phone-for-reservation').value = '';
}

function closeAddGuestToReservationModal() {
    const modal = document.getElementById('add-guest-to-reservation-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

async function saveGuestToReservation(event) {
    event.preventDefault();

    const eventId = document.getElementById('add-guest-event-id').value;
    const name = document.getElementById('new-guest-name-for-reservation').value;
    const email = document.getElementById('new-guest-email-for-reservation').value || null;
    const phone = document.getElementById('new-guest-phone-for-reservation').value || null;

    try {
        await apiRequest('/api/guests', {
            method: 'POST',
            body: JSON.stringify({
                calendar_event_id: parseInt(eventId),
                name: name,
                email: email,
                phone: phone,
                is_primary: false,
                is_test: guestTestModeEnabled
            })
        });

        closeAddGuestToReservationModal();

        // Refresh the guests for this event
        await loadGuestsForEvent(parseInt(eventId));
        loadCurrentGuests();

    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to add guest: ${errorMsg}`);
    }
}

async function deleteGuestFromReservation(guestId) {
    if (!confirm('Remove this guest from the reservation?')) {
        return;
    }

    try {
        await apiRequest(`/api/guests/${guestId}`, {
            method: 'DELETE'
        });
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to remove guest: ${errorMsg}`);
    }
}
