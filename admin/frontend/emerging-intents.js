/**
 * Emerging Intents Management UI
 *
 * Displays discovered novel intents for admin review:
 * - Table of emerging intents sorted by occurrence count
 * - Sample queries per intent
 * - Actions: Promote, Reject, Merge, Mark Reviewed
 * - Statistics and analytics
 *
 * This helps identify user needs that don't match existing services,
 * informing service development priorities.
 */

let emergingIntentsData = [];
let intentStats = {};
let selectedIntents = new Set();

// Use shared utilities from utils.js (getAuthHeaders, safeShowToast available via window)

/**
 * Load all emerging intents from backend
 */
async function loadEmergingIntents() {
    try {
        const response = await fetch('/api/emerging-intents?sort_by=occurrence_count&sort_order=desc&limit=100', {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load emerging intents: ${response.statusText}`);
        }

        emergingIntentsData = await response.json();
        console.log('Emerging intents loaded:', emergingIntentsData.length);

        // Load stats in parallel
        await loadIntentStats();

        renderEmergingIntents();
    } catch (error) {
        console.error('Failed to load emerging intents:', error);
        safeShowToast('Failed to load emerging intents', 'error');
        showEmergingIntentsError(error.message);
    }
}

/**
 * Load emerging intent statistics
 */
async function loadIntentStats() {
    try {
        const response = await fetch('/api/emerging-intents/stats', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            intentStats = await response.json();
        }
    } catch (error) {
        console.error('Failed to load intent stats:', error);
    }
}

/**
 * Render the emerging intents page
 */
function renderEmergingIntents() {
    const container = document.getElementById('emerging-intents-container');
    if (!container) return;

    let html = '';

    // Stats summary
    html += renderStatsCards();

    // Filter controls
    html += renderFilterControls();

    // Main table
    html += renderIntentsTable();

    container.innerHTML = html;
}

/**
 * Render statistics cards
 */
function renderStatsCards() {
    const total = intentStats.total || 0;
    const byStatus = intentStats.by_status || {};
    const discovered = byStatus.discovered || 0;
    const reviewed = byStatus.reviewed || 0;
    const promoted = byStatus.promoted || 0;
    const rejected = byStatus.rejected || 0;

    return `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div class="text-2xl font-bold text-white">${total}</div>
                <div class="text-sm text-gray-400">Total Discovered</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 border border-yellow-700">
                <div class="text-2xl font-bold text-yellow-400">${discovered}</div>
                <div class="text-sm text-gray-400">Pending Review</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 border border-green-700">
                <div class="text-2xl font-bold text-green-400">${promoted}</div>
                <div class="text-sm text-gray-400">Promoted</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 border border-red-700">
                <div class="text-2xl font-bold text-red-400">${rejected}</div>
                <div class="text-sm text-gray-400">Rejected</div>
            </div>
        </div>
    `;
}

/**
 * Render filter controls
 */
function renderFilterControls() {
    return `
        <div class="flex flex-wrap gap-4 mb-4 items-center">
            <div class="flex items-center gap-2">
                <label class="text-sm text-gray-400">Status:</label>
                <select id="intent-status-filter" onchange="filterIntents()"
                        class="bg-gray-700 text-white rounded px-3 py-1 text-sm border border-gray-600">
                    <option value="">All</option>
                    <option value="discovered" selected>Discovered</option>
                    <option value="reviewed">Reviewed</option>
                    <option value="promoted">Promoted</option>
                    <option value="rejected">Rejected</option>
                </select>
            </div>
            <div class="flex items-center gap-2">
                <label class="text-sm text-gray-400">Min Count:</label>
                <input type="number" id="intent-min-count" value="1" min="1"
                       onchange="filterIntents()"
                       class="bg-gray-700 text-white rounded px-3 py-1 text-sm border border-gray-600 w-20">
            </div>
            <div class="flex items-center gap-2">
                <label class="text-sm text-gray-400">Category:</label>
                <select id="intent-category-filter" onchange="filterIntents()"
                        class="bg-gray-700 text-white rounded px-3 py-1 text-sm border border-gray-600">
                    <option value="">All Categories</option>
                    <option value="utility">Utility</option>
                    <option value="commerce">Commerce</option>
                    <option value="health">Health</option>
                    <option value="entertainment">Entertainment</option>
                    <option value="travel">Travel</option>
                    <option value="home">Home</option>
                    <option value="finance">Finance</option>
                    <option value="education">Education</option>
                    <option value="other">Other</option>
                </select>
            </div>
            ${selectedIntents.size > 0 ? `
                <button onclick="mergeSelectedIntents()"
                        class="px-3 py-1 bg-purple-600 hover:bg-purple-700 text-white rounded text-sm">
                    Merge Selected (${selectedIntents.size})
                </button>
            ` : ''}
        </div>
    `;
}

/**
 * Render intents table
 */
function renderIntentsTable() {
    if (emergingIntentsData.length === 0) {
        return `
            <div class="text-center text-gray-400 py-8">
                <div class="text-4xl mb-4">🔍</div>
                <p>No emerging intents found</p>
                <p class="text-sm mt-2">Novel intents will appear here when users ask about things we don't have services for.</p>
            </div>
        `;
    }

    return `
        <div class="overflow-x-auto">
            <table class="w-full">
                <thead>
                    <tr class="border-b border-gray-700">
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Select</th>
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Intent</th>
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Category</th>
                        <th class="px-4 py-3 text-center text-xs font-medium text-gray-400 uppercase">Count</th>
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">First Seen</th>
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Last Seen</th>
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                        <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-gray-700">
                    ${emergingIntentsData.map(intent => renderIntentRow(intent)).join('')}
                </tbody>
            </table>
        </div>
    `;
}

/**
 * Render a single intent row
 */
function renderIntentRow(intent) {
    const statusColors = {
        discovered: 'bg-yellow-900 text-yellow-300',
        reviewed: 'bg-blue-900 text-blue-300',
        promoted: 'bg-green-900 text-green-300',
        rejected: 'bg-red-900 text-red-300'
    };

    const statusColor = statusColors[intent.status] || 'bg-gray-700 text-gray-300';
    const isSelected = selectedIntents.has(intent.id);

    const firstSeen = intent.first_seen ? new Date(intent.first_seen).toLocaleDateString() : 'N/A';
    const lastSeen = intent.last_seen ? new Date(intent.last_seen).toLocaleDateString() : 'N/A';

    return `
        <tr class="hover:bg-gray-800/50 ${isSelected ? 'bg-purple-900/20' : ''}">
            <td class="px-4 py-3">
                <input type="checkbox"
                       ${isSelected ? 'checked' : ''}
                       ${intent.status !== 'discovered' ? 'disabled' : ''}
                       onchange="toggleIntentSelection(${intent.id})"
                       class="rounded bg-gray-700 border-gray-600 text-purple-500 focus:ring-purple-500">
            </td>
            <td class="px-4 py-3">
                <div class="font-medium text-white">${intent.display_name || intent.canonical_name}</div>
                <div class="text-xs text-gray-500 font-mono">${intent.canonical_name}</div>
                <div class="text-xs text-gray-400 mt-1">${intent.description || ''}</div>
                ${intent.sample_queries && intent.sample_queries.length > 0 ? `
                    <details class="mt-2">
                        <summary class="text-xs text-purple-400 cursor-pointer">Sample queries (${intent.sample_queries.length})</summary>
                        <ul class="mt-1 text-xs text-gray-400 list-disc list-inside">
                            ${intent.sample_queries.slice(0, 5).map(q => `<li>${escapeHtml(q)}</li>`).join('')}
                        </ul>
                    </details>
                ` : ''}
            </td>
            <td class="px-4 py-3">
                <span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-300">
                    ${intent.suggested_category || 'uncategorized'}
                </span>
            </td>
            <td class="px-4 py-3 text-center">
                <span class="text-xl font-bold ${intent.occurrence_count >= 5 ? 'text-yellow-400' : 'text-white'}">
                    ${intent.occurrence_count}
                </span>
            </td>
            <td class="px-4 py-3 text-sm text-gray-400">${firstSeen}</td>
            <td class="px-4 py-3 text-sm text-gray-400">${lastSeen}</td>
            <td class="px-4 py-3">
                <span class="px-2 py-1 text-xs rounded ${statusColor}">
                    ${intent.status}
                </span>
                ${intent.promoted_to_intent ? `
                    <div class="text-xs text-green-400 mt-1">→ ${intent.promoted_to_intent}</div>
                ` : ''}
                ${intent.rejection_reason ? `
                    <div class="text-xs text-red-400 mt-1" title="${escapeHtml(intent.rejection_reason)}">
                        Reason: ${intent.rejection_reason.substring(0, 30)}...
                    </div>
                ` : ''}
            </td>
            <td class="px-4 py-3">
                <div class="flex flex-wrap gap-1">
                    ${intent.status === 'discovered' ? `
                        <button onclick="showPromoteModal(${intent.id})"
                                class="px-2 py-1 text-xs bg-green-600 hover:bg-green-700 text-white rounded"
                                title="Promote to known intent">
                            Promote
                        </button>
                        <button onclick="showRejectModal(${intent.id})"
                                class="px-2 py-1 text-xs bg-red-600 hover:bg-red-700 text-white rounded"
                                title="Reject this intent">
                            Reject
                        </button>
                        <button onclick="markReviewed(${intent.id})"
                                class="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded"
                                title="Mark as reviewed">
                            Review
                        </button>
                    ` : ''}
                </div>
            </td>
        </tr>
    `;
}

/**
 * Toggle intent selection for merging
 */
function toggleIntentSelection(intentId) {
    if (selectedIntents.has(intentId)) {
        selectedIntents.delete(intentId);
    } else {
        selectedIntents.add(intentId);
    }
    renderEmergingIntents();
}

/**
 * Filter intents based on controls
 */
async function filterIntents() {
    const status = document.getElementById('intent-status-filter')?.value || '';
    const minCount = document.getElementById('intent-min-count')?.value || '1';
    const category = document.getElementById('intent-category-filter')?.value || '';

    try {
        let url = `/api/emerging-intents?sort_by=occurrence_count&sort_order=desc&limit=100&min_count=${minCount}`;
        if (status) url += `&status=${status}`;
        if (category) url += `&category=${category}`;

        const response = await fetch(url, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to filter intents: ${response.statusText}`);
        }

        emergingIntentsData = await response.json();
        renderEmergingIntents();
    } catch (error) {
        console.error('Failed to filter intents:', error);
        safeShowToast('Failed to filter intents', 'error');
    }
}

/**
 * Show promote modal
 */
function showPromoteModal(intentId) {
    const intent = emergingIntentsData.find(i => i.id === intentId);
    if (!intent) return;

    // Get known intent categories
    const knownIntents = [
        'control', 'weather', 'airports', 'sports', 'flights', 'events',
        'streaming', 'news', 'stocks', 'recipes', 'dining', 'directions',
        'general_info', 'text_me_that'
    ];

    const modalHtml = `
        <div id="promote-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-gray-800 rounded-lg p-6 max-w-md w-full mx-4 border border-gray-700">
                <h3 class="text-lg font-semibold text-white mb-4">Promote Intent</h3>
                <p class="text-gray-400 mb-4">
                    Promoting <strong class="text-white">${intent.display_name}</strong> will mark it as a known intent category.
                </p>
                <div class="mb-4">
                    <label class="block text-sm text-gray-400 mb-2">Target Intent Category:</label>
                    <select id="promote-target" class="w-full bg-gray-700 text-white rounded px-3 py-2 border border-gray-600">
                        ${knownIntents.map(i => `<option value="${i}">${i}</option>`).join('')}
                        <option value="new">Create New Category...</option>
                    </select>
                </div>
                <div id="new-intent-field" class="mb-4 hidden">
                    <label class="block text-sm text-gray-400 mb-2">New Intent Name:</label>
                    <input type="text" id="new-intent-name"
                           class="w-full bg-gray-700 text-white rounded px-3 py-2 border border-gray-600"
                           placeholder="snake_case_name">
                </div>
                <div class="flex justify-end gap-2">
                    <button onclick="closePromoteModal()"
                            class="px-4 py-2 text-gray-400 hover:text-white">Cancel</button>
                    <button onclick="promoteIntent(${intentId})"
                            class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded">Promote</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Handle target select change
    document.getElementById('promote-target').addEventListener('change', (e) => {
        const newField = document.getElementById('new-intent-field');
        if (e.target.value === 'new') {
            newField.classList.remove('hidden');
        } else {
            newField.classList.add('hidden');
        }
    });
}

function closePromoteModal() {
    document.getElementById('promote-modal')?.remove();
}

/**
 * Promote an intent
 */
async function promoteIntent(intentId) {
    const targetSelect = document.getElementById('promote-target');
    let targetIntent = targetSelect.value;

    if (targetIntent === 'new') {
        targetIntent = document.getElementById('new-intent-name')?.value?.trim();
        if (!targetIntent) {
            safeShowToast('Please enter a new intent name', 'error');
            return;
        }
    }

    try {
        const response = await fetch(`/api/emerging-intents/${intentId}/promote`, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ target_intent: targetIntent })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to promote intent');
        }

        closePromoteModal();
        safeShowToast('Intent promoted successfully', 'success');
        await loadEmergingIntents();
    } catch (error) {
        console.error('Failed to promote intent:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Show reject modal
 */
function showRejectModal(intentId) {
    const intent = emergingIntentsData.find(i => i.id === intentId);
    if (!intent) return;

    const modalHtml = `
        <div id="reject-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-gray-800 rounded-lg p-6 max-w-md w-full mx-4 border border-gray-700">
                <h3 class="text-lg font-semibold text-white mb-4">Reject Intent</h3>
                <p class="text-gray-400 mb-4">
                    Rejecting <strong class="text-white">${intent.display_name}</strong> marks it as "won't implement".
                </p>
                <div class="mb-4">
                    <label class="block text-sm text-gray-400 mb-2">Rejection Reason:</label>
                    <textarea id="reject-reason" rows="3"
                              class="w-full bg-gray-700 text-white rounded px-3 py-2 border border-gray-600"
                              placeholder="Why is this intent being rejected?"></textarea>
                </div>
                <div class="flex justify-end gap-2">
                    <button onclick="closeRejectModal()"
                            class="px-4 py-2 text-gray-400 hover:text-white">Cancel</button>
                    <button onclick="rejectIntent(${intentId})"
                            class="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded">Reject</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

function closeRejectModal() {
    document.getElementById('reject-modal')?.remove();
}

/**
 * Reject an intent
 */
async function rejectIntent(intentId) {
    const reason = document.getElementById('reject-reason')?.value?.trim();
    if (!reason) {
        safeShowToast('Please provide a rejection reason', 'error');
        return;
    }

    try {
        const response = await fetch(`/api/emerging-intents/${intentId}/reject`, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ reason })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to reject intent');
        }

        closeRejectModal();
        safeShowToast('Intent rejected', 'success');
        await loadEmergingIntents();
    } catch (error) {
        console.error('Failed to reject intent:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Mark intent as reviewed
 */
async function markReviewed(intentId) {
    try {
        const response = await fetch(`/api/emerging-intents/${intentId}/review`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to mark as reviewed');
        }

        safeShowToast('Intent marked as reviewed', 'success');
        await loadEmergingIntents();
    } catch (error) {
        console.error('Failed to mark as reviewed:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Merge selected intents
 */
async function mergeSelectedIntents() {
    if (selectedIntents.size < 2) {
        safeShowToast('Select at least 2 intents to merge', 'error');
        return;
    }

    const selectedIds = Array.from(selectedIntents);
    const selectedItems = emergingIntentsData.filter(i => selectedIds.includes(i.id));

    // Show merge modal to select target
    const modalHtml = `
        <div id="merge-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-gray-800 rounded-lg p-6 max-w-md w-full mx-4 border border-gray-700">
                <h3 class="text-lg font-semibold text-white mb-4">Merge Intents</h3>
                <p class="text-gray-400 mb-4">
                    Merging ${selectedItems.length} intents. Select which intent to keep as the target:
                </p>
                <div class="mb-4">
                    <label class="block text-sm text-gray-400 mb-2">Target Intent (others will merge into this):</label>
                    <select id="merge-target" class="w-full bg-gray-700 text-white rounded px-3 py-2 border border-gray-600">
                        ${selectedItems.map(i => `
                            <option value="${i.id}">${i.display_name} (${i.occurrence_count} occurrences)</option>
                        `).join('')}
                    </select>
                </div>
                <div class="flex justify-end gap-2">
                    <button onclick="closeMergeModal()"
                            class="px-4 py-2 text-gray-400 hover:text-white">Cancel</button>
                    <button onclick="executeMerge()"
                            class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded">Merge</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

function closeMergeModal() {
    document.getElementById('merge-modal')?.remove();
}

async function executeMerge() {
    const targetId = parseInt(document.getElementById('merge-target').value);
    const sourceIds = Array.from(selectedIntents).filter(id => id !== targetId);

    try {
        const response = await fetch('/api/emerging-intents/merge', {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                target_id: targetId,
                source_ids: sourceIds
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to merge intents');
        }

        closeMergeModal();
        selectedIntents.clear();
        safeShowToast('Intents merged successfully', 'success');
        await loadEmergingIntents();
    } catch (error) {
        console.error('Failed to merge intents:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Show error state
 */
function showEmergingIntentsError(message) {
    const container = document.getElementById('emerging-intents-container');
    if (container) {
        container.innerHTML = `
            <div class="text-center text-red-400 py-8">
                <div class="text-4xl mb-4">❌</div>
                <p>Error: ${message}</p>
                <button onclick="loadEmergingIntents()" class="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded">
                    Try Again
                </button>
            </div>
        `;
    }
}

/**
 * Initialize emerging intents page
 */
function initEmergingIntentsPage() {
    console.log('Initializing emerging intents page');
    selectedIntents.clear();
    loadEmergingIntents();

    // Set up auto-refresh using RefreshManager
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('emerging-intents-refresh', loadEmergingIntents, 60000);
    } else {
        setInterval(() => loadEmergingIntents(), 60000);
    }
}

/**
 * Cleanup emerging intents page
 */
function destroyEmergingIntentsPage() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('emerging-intents-refresh');
    }
}

// Export for external use
if (typeof window !== 'undefined') {
    window.initEmergingIntentsPage = initEmergingIntentsPage;
    window.destroyEmergingIntentsPage = destroyEmergingIntentsPage;
    window.loadEmergingIntents = loadEmergingIntents;
    window.filterIntents = filterIntents;
    window.toggleIntentSelection = toggleIntentSelection;
    window.showPromoteModal = showPromoteModal;
    window.closePromoteModal = closePromoteModal;
    window.promoteIntent = promoteIntent;
    window.showRejectModal = showRejectModal;
    window.closeRejectModal = closeRejectModal;
    window.rejectIntent = rejectIntent;
    window.markReviewed = markReviewed;
    window.mergeSelectedIntents = mergeSelectedIntents;
    window.closeMergeModal = closeMergeModal;
    window.executeMerge = executeMerge;
}
