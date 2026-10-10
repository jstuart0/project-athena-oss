/**
 * Conversations — Transcript review, capture state and purge
 *
 * Reads from the admin backend /api/conversations endpoints, which read the
 * rows the orchestrator writes to this install's database.
 *
 * Who sees what: query, answer and error text need the owner-only
 * `view_conversation_text` permission; everyone else gets metadata and the
 * response says `text_visible: false`. Every inserted value goes through
 * escapeHtml; messages are fixed text with only numbers and dates substituted.
 *
 * Capture is best-effort — turns in-flight at service restart may not be recorded.
 */

// ============================================================================
// State
// ============================================================================

let convState = {
    view: 'list',              // 'list' | 'detail'
    currentId: null,
    filters: {
        source: '',
        room: '',
        audience: '',
        from: '',
        to: '',
    },
    page: 1,
    perPage: 25,
    stats: null,
    capture: null,
    pendingRatings: {},        // { turnId: { rating, notes } }
};

// ============================================================================
// Display tables. Unknown values fall through as escaped raw text.
// ============================================================================

const CONV_CLASS_LABELS = { owner: 'Owner', guest: 'Guest', other: 'Household or unverified', public: 'Website visitor' };
const CONV_CHANNEL_LABELS = { speech: 'Spoken', text: 'Text' };
const CONV_OUTCOME_LABELS = {
    answered: 'Answered', no_answer: 'No answer', error: 'Error', incomplete: 'Cut off',
    refused: 'Refused', withheld: 'Withheld',
};
const CONV_ROUTE_LABELS = {
    query: 'Query', query_stream: 'Stream', query_stream_v2: 'Stream (sentences)', v1_chat_completions: 'OpenAI-compatible',
};
const CONV_SOURCE_LABELS = { analytics: 'Standard', debug: 'Debug detail' };

const CONV_MODE_LABELS = { owner: 'Owner mode', guest: 'Guest mode' };

const CONV_TEXT_HIDDEN = 'Text hidden: owner only';
const CONV_TEXT_WITHHELD = '(text withheld)';
const CONV_PURGE_PHRASE = 'purge all conversations';

function convLabel(table, value) {
    if (value === null || value === undefined || value === '') return '';
    return Object.prototype.hasOwnProperty.call(table, value) ? table[value] : String(value);
}

function convNum(value) {
    const n = Number(value);
    return escapeHtml(String(Number.isFinite(n) ? n : 0));
}

function convDate(value) {
    return escapeHtml(new Date(value).toLocaleDateString());
}

// ============================================================================
// Capture state banner (one fixed message per state)
// ============================================================================

const CONV_BANNER_STYLES = {
    off: 'bg-gray-800 border-gray-600 text-gray-200',
    on_no_writer: 'bg-yellow-900/20 border-yellow-700/40 text-yellow-200',
    on_stale: 'bg-yellow-900/20 border-yellow-700/40 text-yellow-200',
    on_failing: 'bg-red-900/20 border-red-700/40 text-red-200',
    on_ok: 'bg-green-900/20 border-green-700/40 text-green-200',
};

function renderCaptureBanner(cap) {
    if (!cap) return '';
    const stored = Number.isFinite(Number(cap.turn_count)) && cap.turn_count !== null ? Number(cap.turn_count) : null;
    let message;
    if (cap.state === 'off') {
        message = 'Capture is off. Nothing new is being recorded.';
        if (stored) {
            message += ` ${convNum(stored)} turns are stored`;
            message += cap.next_expiry_at ? `; the oldest are deleted on ${convDate(cap.next_expiry_at)}.` : '.';
        }
    } else if (cap.state === 'on_no_writer') {
        message = 'Capture is on, but no orchestrator has reported in. Check that the orchestrator’s DATABASE_URL points at this install’s database.';
    } else if (cap.state === 'on_stale') {
        message = 'Capture is on, but the orchestrator hasn’t reported for a few minutes.';
    } else if (cap.state === 'on_failing') {
        message = `Capture is on, but the orchestrator is failing to write conversations. ${convNum(cap.dropped)} turns have been dropped.`;
    } else if (cap.state === 'on_ok') {
        message = 'Capture is on and working.';
    } else {
        message = 'Capture state is unknown.';
    }
    const parts = [`<p>${message}</p>`];
    parts.push('<p class="mt-1 text-xs opacity-80">Capture is best-effort: turns in flight when a service restarts may not be recorded.</p>');
    if (cap.debug_enabled && cap.state !== 'off') {
        parts.push(`<p class="mt-1 font-medium">Debug detail ON${cap.debug_enabled_since ? ` since ${convDate(cap.debug_enabled_since)}` : ''}.</p>`);
    }
    if (cap.retention_state === 'retention_invalid') {
        parts.push('<p class="mt-1">The saved retention period isn’t valid, so nothing is being deleted. Set a retention period in Settings → Privacy.</p>');
    }
    const style = CONV_BANNER_STYLES[cap.state] || CONV_BANNER_STYLES.off;
    return `<div role="status" class="mb-4 p-3 border rounded-lg text-sm ${style}">${parts.join('')}</div>`;
}

// ============================================================================
// Entry point (called by showTab / router)
// ============================================================================

async function loadCaptureState() {
    try {
        const resp = await fetch('/api/conversations/capture', { headers: getAuthHeaders() });
        convState.capture = resp.ok ? await resp.json() : null;
    } catch (_) {
        convState.capture = null;
    }
    return convState.capture;
}

async function initConversationsPage() {
    const container = document.getElementById('tab-conversations');
    if (!container) return;

    // Check for hash sub-route: #conversations/UUID
    const hash = window.location.hash;
    const detailMatch = hash.match(/^#conversations\/([^/]+)$/);
    if (detailMatch) {
        await showConversationDetail(detailMatch[1]);
        return;
    }

    await loadConversationsList();
}

// ============================================================================
// List view
// ============================================================================

async function loadConversationsList() {
    const container = document.getElementById('conversations-container');
    if (!container) return;

    container.innerHTML = renderListSkeleton();

    try {
        const params = new URLSearchParams({
            page: convState.page,
            per_page: convState.perPage,
        });
        if (convState.filters.source) params.set('source', convState.filters.source);
        if (convState.filters.room) params.set('room', convState.filters.room);
        if (convState.filters.audience) params.set('audience_class', convState.filters.audience);
        if (convState.filters.from) params.set('from', convState.filters.from);
        if (convState.filters.to) params.set('to', convState.filters.to);

        const [listResp, statsResp] = await Promise.allSettled([
            fetch(`/api/conversations?${params}`, { headers: getAuthHeaders() }),
            fetch('/api/conversations/stats', { headers: getAuthHeaders() }),
            loadCaptureState(),
        ]);

        let listData = { items: [], total: 0, pages: 0, text_visible: false };
        let statsData = null;

        if (listResp.status === 'fulfilled' && listResp.value.ok) {
            listData = await listResp.value.json();
        } else if (listResp.status === 'fulfilled' && listResp.value.status === 403) {
            container.innerHTML = renderAccessDenied();
            return;
        }

        if (statsResp.status === 'fulfilled' && statsResp.value.ok) {
            statsData = await statsResp.value.json();
        }

        convState.stats = statsData;
        container.innerHTML = renderListView(listData, statsData, convState.capture);

        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (err) {
        console.error('Failed to load conversations:', err);
        container.innerHTML = renderError(err.message);
    }
}

function renderAccessDenied() {
    return `
        <div class="text-center py-16 text-gray-400">
            <i data-lucide="lock" class="w-12 h-12 mx-auto mb-4 opacity-40"></i>
            <p class="text-lg font-medium">Access Denied</p>
            <p class="text-sm mt-2">This account requires the <code class="bg-gray-800 px-1 rounded">read:conversations</code> scope.</p>
        </div>
    `;
}

function renderError(msg) {
    return `
        <div class="text-center py-16 text-red-400">
            <i data-lucide="alert-triangle" class="w-12 h-12 mx-auto mb-4 opacity-60"></i>
            <p class="text-lg font-medium">Failed to load conversations</p>
            <p class="text-sm mt-2 text-gray-400">${escapeHtml(msg)}</p>
            <button onclick="loadConversationsList()" class="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                Retry
            </button>
        </div>
    `;
}

function renderListSkeleton() {
    return `
        <div class="animate-pulse space-y-3 p-4">
            ${Array(5).fill(0).map(() => `
                <div class="h-16 bg-gray-800 rounded-lg"></div>
            `).join('')}
        </div>
    `;
}

function convCanPurge() {
    return typeof hasPermission === 'function' && hasPermission('delete') && hasPermission('view_conversation_text');
}

function renderPurgeControls(cap) {
    if (!convCanPurge()) return '';
    const retentionOk = !!cap && cap.retention_state !== 'retention_invalid' && cap.retention_days;
    const expiredLabel = retentionOk
        ? `Delete conversations older than ${convNum(cap.retention_days)} days now`
        : 'Delete expired conversations now';
    const disabledReason = retentionOk ? '' : 'Set a retention period in Settings → Privacy first.';
    return `
        <details class="mt-8 border-t border-gray-800 pt-4">
            <summary class="text-sm text-gray-400 cursor-pointer hover:text-gray-200">Manage storage</summary>
            <div class="flex flex-wrap items-center gap-3 mt-3">
                <button onclick="convPurgeExpired()" ${retentionOk ? '' : 'disabled'}
                    class="px-3 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-gray-200 rounded-lg text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500">
                    ${expiredLabel}
                </button>
                <button onclick="convPurgeAll()"
                    class="px-3 py-2 bg-red-900/40 hover:bg-red-900/60 text-red-200 rounded-lg text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500">
                    Delete all conversations…
                </button>
                ${disabledReason ? `<span class="text-xs text-gray-500">${escapeHtml(disabledReason)}</span>` : ''}
            </div>
        </details>
    `;
}

function convFiltersActive() {
    return Object.values(convState.filters).some(Boolean);
}

/** One fixed sentence that is true for the situation: filtered, capture on, or capture off. */
function emptyStateMessage(capture) {
    if (convFiltersActive()) return 'No conversations match these filters.';
    if (capture && capture.state !== 'off') return 'Nothing has been captured yet.';
    return 'Capture is off, so nothing has been recorded. Turn on Analytics Mode in Settings → Privacy to start.';
}

function renderFilterOption(value, label, current) {
    return `<option value="${escapeHtml(value)}" ${current === value ? 'selected' : ''}>${escapeHtml(label)}</option>`;
}

function renderListView(listData, statsData, capture) {
    const { items, total, page, per_page, pages } = listData;
    const textVisible = listData.text_visible === true;

    const statsHtml = statsData ? renderStatsBar(statsData) : '';

    const filtersHtml = `
        <div class="flex flex-wrap items-center gap-3 mb-4">
            <select id="conv-filter-source" onchange="convApplyFilter()" aria-label="Capture level"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white">
                ${renderFilterOption('', 'All capture levels', convState.filters.source)}
                ${renderFilterOption('analytics', CONV_SOURCE_LABELS.analytics, convState.filters.source)}
                ${renderFilterOption('debug', CONV_SOURCE_LABELS.debug, convState.filters.source)}
            </select>
            <select id="conv-filter-audience" onchange="convApplyFilter()" aria-label="Who was talking"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white">
                ${renderFilterOption('', 'Everyone', convState.filters.audience)}
                ${Object.keys(CONV_CLASS_LABELS).map(k => renderFilterOption(k, CONV_CLASS_LABELS[k], convState.filters.audience)).join('')}
            </select>
            <input type="text" id="conv-filter-room" placeholder="Filter by room..." aria-label="Room"
                value="${escapeHtml(convState.filters.room)}"
                onkeydown="if(event.key==='Enter')convApplyFilter()"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white placeholder-gray-500 w-40">
            <input type="date" id="conv-filter-from" value="${escapeHtml(convState.filters.from)}" aria-label="From date"
                onchange="convApplyFilter()"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white">
            <input type="date" id="conv-filter-to" value="${escapeHtml(convState.filters.to)}" aria-label="To date"
                onchange="convApplyFilter()"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white">
            <button onclick="convClearFilters()"
                class="px-3 py-2 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg text-sm">
                Clear
            </button>
        </div>
    `;

    const listHtml = items.length === 0
        ? `<div class="text-center py-12 text-gray-500">
               <i data-lucide="message-square" class="w-10 h-10 mx-auto mb-3 opacity-30"></i>
               <p>${escapeHtml(emptyStateMessage(capture))}</p>
           </div>`
        : `<div class="space-y-2">${items.map(item => renderConversationRow(item, textVisible)).join('')}</div>`;

    const paginationHtml = pages > 1 ? `
        <div class="flex items-center justify-between mt-4 pt-4 border-t border-gray-800">
            <span class="text-sm text-gray-400">${convNum(total)} total · Page ${convNum(page)} of ${convNum(pages)}</span>
            <div class="flex gap-2">
                ${page > 1 ? `<button onclick="convChangePage(${Number(page) - 1})" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm">← Prev</button>` : ''}
                ${page < pages ? `<button onclick="convChangePage(${Number(page) + 1})" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm">Next →</button>` : ''}
            </div>
        </div>
    ` : '';

    return `
        ${renderCaptureBanner(capture)}
        ${statsHtml}
        ${filtersHtml}
        ${listHtml}
        ${paginationHtml}
        ${renderPurgeControls(capture)}
    `;
}

function renderStatsBar(stats) {
    const sources = Object.entries(stats.by_source || {})
        .map(([k, v]) => `${escapeHtml(convLabel(CONV_SOURCE_LABELS, k))}: ${convNum(v)}`).join(', ') || '—';
    return `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Conversations</div>
                <div class="text-xl font-bold text-white">${convNum(stats.total_conversations)}</div>
            </div>
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Total Turns</div>
                <div class="text-xl font-bold text-white">${convNum(stats.total_turns)}</div>
            </div>
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Avg Turns</div>
                <div class="text-xl font-bold text-white">${convNum(stats.avg_turns_per_conversation)}</div>
            </div>
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Capture levels</div>
                <div class="text-sm font-medium text-white">${sources}</div>
            </div>
        </div>
    `;
}

function renderBadge(text, classes) {
    return text ? `<span class="${classes} px-2 py-0.5 rounded">${escapeHtml(text)}</span>` : '';
}

/** The first-query preview: text, a hidden-text label, or a withheld label. */
function renderPreview(conv, textVisible) {
    if (!textVisible) return `<span class="text-gray-500 italic">${escapeHtml(CONV_TEXT_HIDDEN)}</span>`;
    if (conv.first_query === '') return `<span class="text-gray-500 italic">${escapeHtml(CONV_TEXT_WITHHELD)}</span>`;
    if (!conv.first_query) return '<span class="text-gray-500 italic">No query text</span>';
    return `"${escapeHtml(conv.first_query)}${conv.first_query.length >= 120 ? '...' : ''}"`;
}

function renderConversationRow(conv, textVisible) {
    const dt = conv.started_at ? new Date(conv.started_at) : null;
    const dtStr = dt ? dt.toLocaleString() : '—';
    const duration = conv.started_at && conv.ended_at
        ? formatDuration(new Date(conv.ended_at) - new Date(conv.started_at))
        : null;
    const sourceClass = conv.source === 'debug' ? 'bg-orange-900/40 text-orange-300' : 'bg-blue-900/40 text-blue-300';
    const turns = Number(conv.turn_count) || 0;

    return `
        <div class="bg-dark-card border border-dark-border hover:border-blue-600 rounded-lg p-4 cursor-pointer transition-colors"
             onclick="showConversationDetail('${escapeJsAttr(conv.id)}')">
            <div class="flex items-start justify-between">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 text-xs text-gray-400 mb-2 flex-wrap">
                        <span>${escapeHtml(dtStr)}</span>
                        ${renderBadge(convLabel(CONV_CLASS_LABELS, conv.session_class) || 'Unclassified', 'bg-gray-700')}
                        ${renderBadge(conv.room, 'bg-gray-700')}
                        ${renderBadge(convLabel(CONV_MODE_LABELS, conv.user_mode), 'bg-gray-700')}
                        ${renderBadge(convLabel(CONV_SOURCE_LABELS, conv.source || 'analytics'), sourceClass)}
                        <span>${convNum(turns)} turn${turns !== 1 ? 's' : ''}</span>
                        ${duration ? `<span>${escapeHtml(duration)}</span>` : ''}
                    </div>
                    <div class="text-sm text-gray-200 truncate">${renderPreview(conv, textVisible)}</div>
                </div>
                <i data-lucide="chevron-right" class="w-4 h-4 text-gray-500 ml-3 flex-shrink-0 mt-1"></i>
            </div>
        </div>
    `;
}

function convStars(rating) {
    return Math.max(0, Math.min(5, Math.floor(Number(rating)) || 0));
}

function formatDuration(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}m ${rem}s`;
}

function convApplyFilter() {
    convState.filters.source = document.getElementById('conv-filter-source')?.value || '';
    convState.filters.audience = document.getElementById('conv-filter-audience')?.value || '';
    convState.filters.room = document.getElementById('conv-filter-room')?.value || '';
    convState.filters.from = document.getElementById('conv-filter-from')?.value || '';
    convState.filters.to = document.getElementById('conv-filter-to')?.value || '';
    convState.page = 1;
    loadConversationsList();
}

function convClearFilters() {
    convState.filters = { source: '', room: '', audience: '', from: '', to: '' };
    convState.page = 1;
    loadConversationsList();
}

function convChangePage(newPage) {
    convState.page = newPage;
    loadConversationsList();
}

// ============================================================================
// Purge
// ============================================================================

async function convPostPurge(body) {
    const resp = await fetch('/api/conversations/purge', {
        method: 'POST',
        headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (resp.ok) return { ok: true, result: await resp.json() };
    const err = await resp.json().catch(() => ({}));
    return { ok: false, status: resp.status, detail: typeof err.detail === 'string' ? err.detail : '' };
}

/** Fixed messages only; nothing from the server is shown. */
function convPurgeFailureMessage(outcome) {
    if (outcome.status === 409 && /retention/i.test(outcome.detail)) {
        return 'The saved retention period isn’t valid, so nothing was deleted. Set one in Settings → Privacy.';
    }
    if (outcome.status === 409) return 'A purge is already running; try again in a minute.';
    if (outcome.status === 429) return 'Too many purge requests. Try again in a minute.';
    if (outcome.status === 403) return 'Only an owner can delete conversations.';
    return 'The purge failed.';
}

function convPurgeSuccessMessage(result) {
    const base = `Deleted ${Number(result.turns) || 0} turns in ${Number(result.conversations) || 0} conversations.`;
    return result.aborted ? `${base} The purge stopped early; run it again to finish.` : base;
}

async function convRunPurge(body) {
    try {
        const outcome = await convPostPurge(body);
        if (outcome.ok) {
            showCaptureToast(convPurgeSuccessMessage(outcome.result), 'success');
        } else {
            showCaptureToast(convPurgeFailureMessage(outcome), 'error');
        }
    } catch (err) {
        showCaptureToast('The purge failed.', 'error');
    }
    await loadConversationsList();
}

async function convPurgeExpired() {
    const cap = convState.capture;
    if (!convCanPurge() || !cap || !cap.retention_days) return;
    let count = null;
    try {
        const resp = await fetch(`/api/conversations/capture?preview_retention=${encodeURIComponent(cap.retention_days)}`, { headers: getAuthHeaders() });
        if (resp.ok) count = (await resp.json()).preview_delete_turns;
    } catch (_) { /* the confirm below says the count is unknown */ }
    const message = Number.isFinite(count)
        ? `This deletes ${count} turns older than ${cap.retention_days} days. It can’t be undone.`
        : `This deletes every turn older than ${cap.retention_days} days. It can’t be undone.`;
    const confirmed = await showServiceConfirmModal({ title: 'Delete expired conversations', message, requireTyped: null });
    if (confirmed === false) return;
    await convRunPurge({ scope: 'expired' });
}

async function convPurgeAll() {
    if (!convCanPurge()) return;
    const cap = convState.capture;
    const stored = cap && Number.isFinite(cap.turn_count) ? cap.turn_count : null;
    const message = stored !== null
        ? `This deletes all ${stored} stored turns and every conversation. It can’t be undone.`
        : 'This deletes every stored turn and conversation. It can’t be undone.';
    const confirmed = await showServiceConfirmModal({ title: 'Delete all conversations', message, requireTyped: CONV_PURGE_PHRASE });
    if (confirmed === false) return;
    await convRunPurge({ scope: 'all', confirm: confirmed });
}

// ============================================================================
// Detail view
// ============================================================================

async function showConversationDetail(id) {
    convState.view = 'detail';
    convState.currentId = id;
    convState.pendingRatings = {};

    const container = document.getElementById('conversations-container');
    if (!container) return;

    container.innerHTML = `<div class="animate-pulse p-4 space-y-4">
        <div class="h-8 bg-gray-800 rounded w-48"></div>
        ${Array(3).fill(0).map(() => `<div class="h-32 bg-gray-800 rounded"></div>`).join('')}
    </div>`;

    try {
        const resp = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
            headers: getAuthHeaders()
        });

        if (!resp.ok) {
            if (resp.status === 404) {
                container.innerHTML = `<div class="text-center py-12 text-gray-400">Conversation not found.</div>`;
            } else {
                throw new Error(`HTTP ${resp.status}`);
            }
            return;
        }

        const conv = await resp.json();
        container.innerHTML = renderDetailView(conv);
        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (err) {
        console.error('Failed to load conversation detail:', err);
        container.innerHTML = renderError(err.message);
    }
}

function renderDetailView(conv) {
    const dt = conv.started_at ? new Date(conv.started_at) : null;
    const dtStr = dt ? dt.toLocaleString() : '—';
    const textVisible = conv.text_visible === true;
    const turns = Number(conv.turn_count) || 0;

    const metaBadges = [
        renderBadge(convLabel(CONV_CLASS_LABELS, conv.session_class) || 'Unclassified', 'bg-gray-700 py-1 text-xs'),
        renderBadge(conv.room, 'bg-gray-700 py-1 text-xs'),
        renderBadge(convLabel(CONV_MODE_LABELS, conv.user_mode), 'bg-gray-700 py-1 text-xs'),
        renderBadge(conv.interface_type, 'bg-gray-700 py-1 text-xs'),
    ].filter(Boolean).join('');

    const turnsHtml = (conv.turns || []).map(turn => renderTurnCard(turn, textVisible)).join('');

    return `
        <div>
            <div class="flex items-center gap-3 mb-6">
                <button onclick="backToList()"
                    class="text-gray-400 hover:text-white transition-colors flex items-center gap-1 text-sm">
                    <i data-lucide="arrow-left" class="w-4 h-4"></i> Conversations
                </button>
                <span class="text-gray-600">/</span>
                <span class="text-gray-300 text-sm">${escapeHtml(dtStr)}</span>
            </div>

            <div class="bg-dark-card rounded-lg p-4 mb-6">
                <div class="flex flex-wrap items-center gap-2 mb-2">
                    ${metaBadges}
                    <span class="text-xs text-gray-400">${convNum(turns)} turn${turns !== 1 ? 's' : ''}</span>
                    ${conv.total_tokens ? `<span class="text-xs text-gray-400">${convNum(conv.total_tokens)} tokens</span>` : ''}
                </div>
                <div class="text-xs text-gray-500 font-mono" title="First 12 characters of the session id">${escapeHtml(conv.session_id)}</div>
            </div>

            <div class="space-y-4">
                ${turnsHtml || '<div class="text-gray-500 text-center py-8">No turns recorded</div>'}
            </div>
        </div>
    `;
}

/** A text cell: the text, "(text withheld)", or "Text hidden: owner only". */
function renderTextBlock(label, value, textVisible, withheld, classes) {
    let body;
    if (!textVisible) {
        body = `<span class="text-gray-500 italic">${escapeHtml(CONV_TEXT_HIDDEN)}</span>`;
    } else if (withheld) {
        body = `<span class="text-gray-500 italic">${escapeHtml(CONV_TEXT_WITHHELD)}</span>`;
    } else if (value === null || value === undefined || value === '') {
        return '';
    } else {
        body = escapeHtml(value);
    }
    return `
        <div class="mb-3">
            <div class="text-xs text-gray-500 mb-1">${escapeHtml(label)}</div>
            <div class="text-sm ${classes} rounded p-3 whitespace-pre-wrap break-words">${body}</div>
        </div>
    `;
}

function renderDebugValue(value, textVisible) {
    // Strings and nested values can hold text; only numbers and booleans are shown without the permission.
    if (!textVisible && (typeof value === 'string' || (typeof value === 'object' && value !== null))) {
        return `<span class="italic">${escapeHtml(CONV_TEXT_HIDDEN)}</span>`;
    }
    if (typeof value === 'string') return escapeHtml(value);
    if (typeof value === 'boolean') return value ? 'yes' : 'no';
    if (typeof value === 'number') return convNum(value);
    if (value === null || value === undefined) return '—';
    return escapeHtml(JSON.stringify(value));
}

function renderDebugBlock(turn, textVisible) {
    const rows = [];
    const add = (name, valueHtml) => rows.push(`<div><span class="text-gray-500">${escapeHtml(name)}:</span> ${valueHtml}</div>`);
    if (turn.model_used) add('model', escapeHtml(turn.model_used));
    if (turn.complexity) add('complexity', escapeHtml(turn.complexity));
    if (turn.tokens_generated) add('tokens', convNum(turn.tokens_generated));
    if (turn.tokens_per_second) add('tokens/s', convNum(Math.round(turn.tokens_per_second)));
    if (turn.validation_passed !== null && turn.validation_passed !== undefined) add('validation passed', turn.validation_passed ? 'yes' : 'no');
    if (turn.is_fallback) add('fallback', 'yes');
    if (turn.base_knowledge_populated) add('base knowledge', 'yes');
    if (Array.isArray(turn.rag_tools_used) && turn.rag_tools_used.length) {
        add('tools', turn.rag_tools_used.map(t => escapeHtml(String(t))).join(', '));
    }
    if (turn.node_timings_ms && Object.keys(turn.node_timings_ms).length) {
        add('node timings', Object.entries(turn.node_timings_ms).map(([k, v]) => `${escapeHtml(k)} ${convNum(v)}ms`).join(', '));
    }
    if (turn.error_message || (!textVisible && turn.outcome === 'error')) {
        add('error', renderDebugValue(turn.error_message || '', textVisible));
    }
    const detail = turn.debug_detail && typeof turn.debug_detail === 'object' ? turn.debug_detail : {};
    Object.entries(detail).forEach(([k, v]) => add(k, renderDebugValue(v, textVisible)));
    if (!rows.length) return '';
    return `
        <details class="mt-2">
            <summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-400">Debug detail</summary>
            <div class="mt-1 text-xs font-mono text-gray-400 pl-2 space-y-0.5">${rows.join('')}</div>
        </details>
    `;
}

function renderTurnCard(turn, textVisible) {
    const existingEval = (turn.evaluations || [])[0];
    const sourceClass = turn.source === 'debug'
        ? 'bg-orange-900/30 text-orange-300 border-orange-800'
        : 'bg-blue-900/30 text-blue-300 border-blue-800';
    const legacy = !turn.route;
    const sourceText = legacy
        ? (turn.source === 'debug' ? 'legacy debug' : 'legacy')
        : convLabel(CONV_SOURCE_LABELS, turn.source);
    const turnId = escapeHtml(turn.id);

    const metaLine = [
        turn.response_time_ms && `⏱ ${(turn.response_time_ms / 1000).toFixed(1)}s`,
        turn.is_fallback && '⚠ fallback',
    ].filter(Boolean).map(escapeHtml).join(' · ');

    const labels = [
        convLabel(CONV_ROUTE_LABELS, turn.route),
        convLabel(CONV_CHANNEL_LABELS, turn.channel),
        convLabel(CONV_CLASS_LABELS, turn.audience_class),
        convLabel(CONV_OUTCOME_LABELS, turn.outcome),
    ].filter(Boolean).map(l => `<span class="text-xs bg-gray-700 text-gray-300 px-2 py-0.5 rounded">${escapeHtml(l)}</span>`).join('');

    const evalSection = `
        <div class="mt-4 pt-4 border-t border-gray-800">
            ${existingEval ? `
                <div class="flex items-center gap-2 mb-3">
                    <span class="text-yellow-400">${'★'.repeat(convStars(existingEval.rating))}${'☆'.repeat(5 - convStars(existingEval.rating))}</span>
                    <span class="text-xs text-gray-400">by ${escapeHtml(existingEval.evaluated_by_username || 'unknown')}</span>
                    ${existingEval.notes ? `<span class="text-xs text-gray-300">"${escapeHtml(existingEval.notes)}"</span>` : ''}
                </div>
            ` : ''}
            <div class="flex items-center gap-2 flex-wrap">
                <span class="text-xs text-gray-400 mr-1">Rate:</span>
                ${[1,2,3,4,5].map(r => `
                    <button onclick="setRating('${escapeJsAttr(turn.id)}', ${r})"
                        id="rating-${turnId}-${r}"
                        class="px-2 py-1 rounded text-sm transition-colors ${existingEval?.rating === r ? 'bg-yellow-600 text-white' : 'bg-gray-700 hover:bg-gray-600 text-gray-300'}"
                        data-turn="${turnId}" data-rating="${r}">
                        ${'★'.repeat(r)}
                    </button>
                `).join('')}
                <input type="text" id="notes-${turnId}" aria-label="Evaluation notes"
                    placeholder="Notes (optional)"
                    value="${escapeHtml(existingEval?.notes || '')}"
                    class="px-2 py-1 bg-gray-800 border border-gray-700 rounded text-xs text-white placeholder-gray-500 flex-1 min-w-0">
                <button onclick="submitEvaluation('${escapeJsAttr(turn.id)}')"
                    class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs font-medium">
                    Save
                </button>
            </div>
            <div id="eval-status-${turnId}" class="text-xs mt-2"></div>
        </div>
    `;

    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4" id="turn-card-${turnId}">
            <div class="flex items-center justify-between mb-3">
                <span class="text-xs font-semibold text-gray-400">Turn ${convNum(turn.turn_number)}</span>
                <div class="flex items-center gap-2 flex-wrap justify-end">
                    ${labels}
                    ${turn.intent ? `<span class="text-xs bg-purple-900/40 text-purple-300 px-2 py-0.5 rounded">${escapeHtml(turn.intent)}</span>` : ''}
                    ${turn.confidence !== null && turn.confidence !== undefined ? `<span class="text-xs text-gray-500">${escapeHtml(parseFloat(turn.confidence).toFixed(2))}</span>` : ''}
                    <span class="text-xs border px-2 py-0.5 rounded ${sourceClass}">${escapeHtml(sourceText)}</span>
                </div>
            </div>

            ${renderTextBlock('Query', turn.query_text, textVisible, turn.text_withheld, 'text-white bg-gray-800 font-mono')}
            ${renderTextBlock('Response', turn.response_text, textVisible, turn.text_withheld, 'text-gray-200 bg-gray-800/50')}

            ${metaLine ? `<div class="text-xs text-gray-500 font-mono">${metaLine}</div>` : ''}
            ${renderDebugBlock(turn, textVisible)}

            ${evalSection}
        </div>
    `;
}

function setRating(turnId, rating) {
    if (!convState.pendingRatings[turnId]) convState.pendingRatings[turnId] = {};
    convState.pendingRatings[turnId].rating = rating;

    // Update button visual states
    for (let r = 1; r <= 5; r++) {
        const btn = document.getElementById(`rating-${turnId}-${r}`);
        if (btn) {
            btn.className = `px-2 py-1 rounded text-sm transition-colors ${
                r === rating
                    ? 'bg-yellow-600 text-white'
                    : 'bg-gray-700 hover:bg-gray-600 text-gray-300'
            }`;
        }
    }
}

function convSetStatus(element, message, colorClass) {
    if (!element) return;
    element.textContent = message;
    element.className = `text-xs mt-2 ${colorClass}`;
}

async function submitEvaluation(turnId) {
    const pending = convState.pendingRatings[turnId] || {};
    const notes = document.getElementById(`notes-${turnId}`)?.value?.trim() || null;
    const rating = pending.rating;
    const statusEl = document.getElementById(`eval-status-${turnId}`);

    if (!rating) {
        convSetStatus(statusEl, 'Select a rating first', 'text-red-400');
        return;
    }

    try {
        const resp = await fetch(`/api/conversations/turns/${encodeURIComponent(turnId)}/evaluate`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ rating, notes }),
        });

        if (resp.ok) {
            convSetStatus(statusEl, 'Evaluation saved', 'text-green-400');
            setTimeout(() => convSetStatus(statusEl, '', ''), 3000);
        } else {
            convSetStatus(statusEl, 'Failed to save', 'text-red-400');
        }
    } catch (err) {
        convSetStatus(statusEl, 'Failed to save', 'text-red-400');
    }
}

function backToList() {
    convState.view = 'list';
    convState.currentId = null;
    history.replaceState(null, '', '#conversations');
    loadConversationsList();
}

// ============================================================================
// Helper: get auth headers (re-use the pattern from the rest of the frontend)
// ============================================================================

function getAuthHeaders() {
    const token = typeof getToken === 'function' ? getToken() : localStorage.getItem('auth_token');
    return token ? { 'Authorization': `Bearer ${token}` } : {};
}

