/**
 * Conversations — Transcript review and turn evaluation
 *
 * Reads from the admin backend /api/conversations endpoints which in turn
 * read from the analytics database written by the orchestrator.
 *
 * Analytics capture is best-effort — turns in-flight at service restart
 * may not be recorded.
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
        from: '',
        to: '',
    },
    page: 1,
    perPage: 25,
    stats: null,
    pendingRatings: {},        // { turnId: { rating, notes } }
};

// ============================================================================
// Entry point (called by showTab / router)
// ============================================================================

async function initConversationsPage() {
    const container = document.getElementById('tab-conversations');
    if (!container) return;

    // Check analytics mode — conversations only work when analytics is enabled
    const convContainer = document.getElementById('conversations-container');
    try {
        const modeResp = await fetch('/api/settings/analytics-mode/public');
        if (modeResp.ok) {
            const modeData = await modeResp.json();
            if (!modeData.analytics_mode_enabled) {
                if (convContainer) {
                    convContainer.innerHTML = `
                        <div class="text-center py-16 text-gray-400">
                            <i data-lucide="bar-chart-2" class="w-12 h-12 mx-auto mb-4 opacity-30"></i>
                            <p class="text-lg font-medium text-gray-300">Analytics Mode is Off</p>
                            <p class="text-sm mt-2 max-w-sm mx-auto">
                                Conversation capture is disabled. Enable analytics mode in
                                <button onclick="showTab('settings')" class="text-blue-400 hover:text-blue-300 underline">Settings → Privacy</button>
                                to start recording conversations.
                            </p>
                        </div>
                    `;
                    if (typeof lucide !== 'undefined') lucide.createIcons();
                }
                return;
            }
        }
    } catch (_) {
        // If we can't check, proceed normally — let the list load and show empty state if needed
    }

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
        // Load stats and list in parallel
        const params = new URLSearchParams({
            page: convState.page,
            per_page: convState.perPage,
        });
        if (convState.filters.source) params.set('source', convState.filters.source);
        if (convState.filters.room) params.set('room', convState.filters.room);
        if (convState.filters.from) params.set('from', convState.filters.from);
        if (convState.filters.to) params.set('to', convState.filters.to);

        const [listResp, statsResp] = await Promise.allSettled([
            fetch(`/api/conversations?${params}`, { headers: getAuthHeaders() }),
            fetch('/api/conversations/stats', { headers: getAuthHeaders() }),
        ]);

        let listData = { items: [], total: 0, pages: 0 };
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
        container.innerHTML = renderListView(listData, statsData);

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

function renderListView(listData, statsData) {
    const { items, total, page, per_page, pages } = listData;

    const statsHtml = statsData ? renderStatsBar(statsData) : '';

    const filtersHtml = `
        <div class="flex flex-wrap items-center gap-3 mb-4">
            <select id="conv-filter-source" onchange="convApplyFilter()"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white">
                <option value="">All sources</option>
                <option value="chatbot" ${convState.filters.source === 'chatbot' ? 'selected' : ''}>Chatbot</option>
                <option value="jarvis" ${convState.filters.source === 'jarvis' ? 'selected' : ''}>Jarvis</option>
                <option value="voice" ${convState.filters.source === 'voice' ? 'selected' : ''}>Voice</option>
                <option value="analytics" ${convState.filters.source === 'analytics' ? 'selected' : ''}>Analytics</option>
                <option value="debug" ${convState.filters.source === 'debug' ? 'selected' : ''}>Debug</option>
            </select>
            <input type="text" id="conv-filter-room" placeholder="Filter by room..."
                value="${escapeHtml(convState.filters.room)}"
                onkeydown="if(event.key==='Enter')convApplyFilter()"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white placeholder-gray-500 w-40">
            <input type="date" id="conv-filter-from" value="${convState.filters.from}"
                onchange="convApplyFilter()"
                class="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white">
            <input type="date" id="conv-filter-to" value="${convState.filters.to}"
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
               <p>No conversations recorded yet.</p>
               <p class="text-xs mt-2">Enable analytics mode in Settings → Privacy to start capturing.</p>
           </div>`
        : `<div class="space-y-2">${items.map(renderConversationRow).join('')}</div>`;

    const paginationHtml = pages > 1 ? `
        <div class="flex items-center justify-between mt-4 pt-4 border-t border-gray-800">
            <span class="text-sm text-gray-400">${total} total · Page ${page} of ${pages}</span>
            <div class="flex gap-2">
                ${page > 1 ? `<button onclick="convChangePage(${page - 1})" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm">← Prev</button>` : ''}
                ${page < pages ? `<button onclick="convChangePage(${page + 1})" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm">Next →</button>` : ''}
            </div>
        </div>
    ` : '';

    return `
        <div class="mb-4 p-3 bg-yellow-900/20 border border-yellow-700/40 rounded-lg text-xs text-yellow-200">
            <i data-lucide="info" class="w-3 h-3 inline mr-1"></i>
            Best-effort capture — turns in-flight at service restart may not be recorded.
        </div>
        ${statsHtml}
        ${filtersHtml}
        ${listHtml}
        ${paginationHtml}
    `;
}

function renderStatsBar(stats) {
    return `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Conversations</div>
                <div class="text-xl font-bold text-white">${stats.total_conversations}</div>
            </div>
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Total Turns</div>
                <div class="text-xl font-bold text-white">${stats.total_turns}</div>
            </div>
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Avg Turns</div>
                <div class="text-xl font-bold text-white">${stats.avg_turns_per_conversation}</div>
            </div>
            <div class="bg-dark-card rounded-lg p-3">
                <div class="text-xs text-gray-400 mb-1">Sources</div>
                <div class="text-sm font-medium text-white">
                    ${Object.entries(stats.by_source || {}).map(([k,v]) => `${k}: ${v}`).join(', ') || '—'}
                </div>
            </div>
        </div>
    `;
}

function renderConversationRow(conv) {
    const dt = conv.started_at ? new Date(conv.started_at) : null;
    const dtStr = dt ? dt.toLocaleString() : '—';
    const duration = conv.started_at && conv.ended_at
        ? formatDuration(new Date(conv.ended_at) - new Date(conv.started_at))
        : null;

    return `
        <div class="bg-dark-card border border-dark-border hover:border-blue-600 rounded-lg p-4 cursor-pointer transition-colors"
             onclick="showConversationDetail('${escapeHtml(conv.id)}')">
            <div class="flex items-start justify-between">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 text-xs text-gray-400 mb-2 flex-wrap">
                        <span>${dtStr}</span>
                        ${conv.room ? `<span class="bg-gray-700 px-2 py-0.5 rounded">${escapeHtml(conv.room)}</span>` : ''}
                        ${conv.user_mode ? `<span class="bg-gray-700 px-2 py-0.5 rounded">${escapeHtml(conv.user_mode)}</span>` : ''}
                        <span class="${conv.source === 'debug' ? 'bg-orange-900/40 text-orange-300' : 'bg-blue-900/40 text-blue-300'} px-2 py-0.5 rounded">
                            ${conv.source || 'analytics'}
                        </span>
                        <span>${conv.turn_count} turn${conv.turn_count !== 1 ? 's' : ''}</span>
                        ${duration ? `<span>${duration}</span>` : ''}
                    </div>
                    <div class="text-sm text-gray-200 truncate">
                        ${conv.first_query ? `"${escapeHtml(conv.first_query)}${conv.first_query.length >= 120 ? '...' : ''}"` : '<span class="text-gray-500 italic">No query text</span>'}
                    </div>
                </div>
                <i data-lucide="chevron-right" class="w-4 h-4 text-gray-500 ml-3 flex-shrink-0 mt-1"></i>
            </div>
        </div>
    `;
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
    convState.filters.room = document.getElementById('conv-filter-room')?.value || '';
    convState.filters.from = document.getElementById('conv-filter-from')?.value || '';
    convState.filters.to = document.getElementById('conv-filter-to')?.value || '';
    convState.page = 1;
    loadConversationsList();
}

function convClearFilters() {
    convState.filters = { source: '', room: '', from: '', to: '' };
    convState.page = 1;
    loadConversationsList();
}

function convChangePage(newPage) {
    convState.page = newPage;
    loadConversationsList();
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

    const metaBadges = [
        conv.room && `<span class="bg-gray-700 px-2 py-1 rounded text-xs">${escapeHtml(conv.room)}</span>`,
        conv.user_mode && `<span class="bg-gray-700 px-2 py-1 rounded text-xs">${escapeHtml(conv.user_mode)}</span>`,
        conv.interface_type && `<span class="bg-gray-700 px-2 py-1 rounded text-xs">${escapeHtml(conv.interface_type)}</span>`,
    ].filter(Boolean).join('');

    const turnsHtml = (conv.turns || []).map(renderTurnCard).join('');

    return `
        <div>
            <div class="flex items-center gap-3 mb-6">
                <button onclick="backToList()"
                    class="text-gray-400 hover:text-white transition-colors flex items-center gap-1 text-sm">
                    <i data-lucide="arrow-left" class="w-4 h-4"></i> Conversations
                </button>
                <span class="text-gray-600">/</span>
                <span class="text-gray-300 text-sm">${dtStr}</span>
            </div>

            <div class="bg-dark-card rounded-lg p-4 mb-6">
                <div class="flex flex-wrap items-center gap-2 mb-2">
                    ${metaBadges}
                    <span class="text-xs text-gray-400">${conv.turn_count} turn${conv.turn_count !== 1 ? 's' : ''}</span>
                    ${conv.total_tokens ? `<span class="text-xs text-gray-400">${conv.total_tokens} tokens</span>` : ''}
                </div>
                <div class="text-xs text-gray-500 font-mono">${escapeHtml(conv.session_id)}</div>
            </div>

            <div class="space-y-4">
                ${turnsHtml || '<div class="text-gray-500 text-center py-8">No turns recorded</div>'}
            </div>
        </div>
    `;
}

function renderTurnCard(turn) {
    const existingEval = (turn.evaluations || [])[0];
    const sourceClass = turn.source === 'debug'
        ? 'bg-orange-900/30 text-orange-300 border-orange-800'
        : 'bg-blue-900/30 text-blue-300 border-blue-800';

    const metaLine = [
        turn.response_time_ms && `⏱ ${(turn.response_time_ms / 1000).toFixed(1)}s`,
        turn.tokens_generated && `${turn.tokens_generated} tok`,
        turn.tokens_per_second && `${Math.round(turn.tokens_per_second)} tok/s`,
        turn.model_used && turn.model_used,
        Array.isArray(turn.rag_tools_used) && turn.rag_tools_used.length && `[${turn.rag_tools_used.join(', ')}]`,
    ].filter(Boolean).join(' · ');

    const evalSection = `
        <div class="mt-4 pt-4 border-t border-gray-800">
            ${existingEval ? `
                <div class="flex items-center gap-2 mb-3">
                    <span class="text-yellow-400">${'★'.repeat(existingEval.rating)}${'☆'.repeat(5 - existingEval.rating)}</span>
                    <span class="text-xs text-gray-400">by ${escapeHtml(existingEval.evaluated_by_username || 'unknown')}</span>
                    ${existingEval.notes ? `<span class="text-xs text-gray-300">"${escapeHtml(existingEval.notes)}"</span>` : ''}
                </div>
            ` : ''}
            <div class="flex items-center gap-2 flex-wrap">
                <span class="text-xs text-gray-400 mr-1">Rate:</span>
                ${[1,2,3,4,5].map(r => `
                    <button onclick="setRating('${turn.id}', ${r})"
                        id="rating-${turn.id}-${r}"
                        class="px-2 py-1 rounded text-sm transition-colors ${existingEval?.rating === r ? 'bg-yellow-600 text-white' : 'bg-gray-700 hover:bg-gray-600 text-gray-300'}"
                        data-turn="${turn.id}" data-rating="${r}">
                        ${'★'.repeat(r)}
                    </button>
                `).join('')}
                <input type="text" id="notes-${turn.id}"
                    placeholder="Notes (optional)"
                    value="${escapeHtml(existingEval?.notes || '')}"
                    class="px-2 py-1 bg-gray-800 border border-gray-700 rounded text-xs text-white placeholder-gray-500 flex-1 min-w-0">
                <button onclick="submitEvaluation('${turn.id}')"
                    class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs font-medium">
                    Save
                </button>
            </div>
            <div id="eval-status-${turn.id}" class="text-xs mt-2"></div>
        </div>
    `;

    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-4" id="turn-card-${turn.id}">
            <div class="flex items-center justify-between mb-3">
                <span class="text-xs font-semibold text-gray-400">Turn ${turn.turn_number}</span>
                <div class="flex items-center gap-2">
                    ${turn.intent ? `<span class="text-xs bg-purple-900/40 text-purple-300 px-2 py-0.5 rounded">${escapeHtml(turn.intent)}</span>` : ''}
                    ${turn.confidence !== null && turn.confidence !== undefined ? `<span class="text-xs text-gray-500">${parseFloat(turn.confidence).toFixed(2)}</span>` : ''}
                    <span class="text-xs border px-2 py-0.5 rounded ${sourceClass}">${turn.source}</span>
                </div>
            </div>

            <div class="mb-3">
                <div class="text-xs text-gray-500 mb-1">Query</div>
                <div class="text-sm text-white bg-gray-800 rounded p-3 font-mono whitespace-pre-wrap break-words">${escapeHtml(turn.query_text)}</div>
            </div>

            ${turn.response_text ? `
                <div class="mb-3">
                    <div class="text-xs text-gray-500 mb-1">Response</div>
                    <div class="text-sm text-gray-200 bg-gray-800/50 rounded p-3 whitespace-pre-wrap break-words">${escapeHtml(turn.response_text)}</div>
                </div>
            ` : ''}

            ${metaLine ? `<div class="text-xs text-gray-500 font-mono">${metaLine}</div>` : ''}

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

async function submitEvaluation(turnId) {
    const pending = convState.pendingRatings[turnId] || {};
    const notes = document.getElementById(`notes-${turnId}`)?.value?.trim() || null;
    const rating = pending.rating;

    if (!rating) {
        const statusEl = document.getElementById(`eval-status-${turnId}`);
        if (statusEl) statusEl.innerHTML = '<span class="text-red-400">Select a rating first</span>';
        return;
    }

    try {
        const resp = await fetch(`/api/conversations/turns/${encodeURIComponent(turnId)}/evaluate`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ rating, notes }),
        });

        const statusEl = document.getElementById(`eval-status-${turnId}`);

        if (resp.ok) {
            if (statusEl) statusEl.innerHTML = '<span class="text-green-400">Evaluation saved</span>';
            setTimeout(() => {
                if (statusEl) statusEl.innerHTML = '';
            }, 3000);
        } else {
            const err = await resp.json().catch(() => ({}));
            if (statusEl) statusEl.innerHTML = `<span class="text-red-400">${escapeHtml(err.detail || 'Failed to save')}</span>`;
        }
    } catch (err) {
        const statusEl = document.getElementById(`eval-status-${turnId}`);
        if (statusEl) statusEl.innerHTML = `<span class="text-red-400">${escapeHtml(err.message)}</span>`;
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

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
