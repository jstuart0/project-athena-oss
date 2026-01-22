/**
 * Voice Automations Management
 * Admin UI for managing voice-created automations with guest session lifecycle support.
 */

// ============================================================================
// State
// ============================================================================

let automationsData = [];
let automationsFilter = {
    owner_type: '',
    status: 'active',
    guest_name: '',
    include_archived: false
};

// ============================================================================
// Load Functions
// ============================================================================

async function loadVoiceAutomations() {
    try {
        showLoading('automations-container');

        // Build query params
        const params = new URLSearchParams();
        if (automationsFilter.owner_type) params.append('owner_type', automationsFilter.owner_type);
        if (automationsFilter.status) params.append('status', automationsFilter.status);
        if (automationsFilter.guest_name) params.append('guest_name', automationsFilter.guest_name);
        if (automationsFilter.include_archived) params.append('include_archived', 'true');

        const response = await fetch(`/api/voice-automations?${params.toString()}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to load automations');
        }

        automationsData = await response.json();
        renderAutomationsPage();

    } catch (error) {
        console.error('Error loading voice automations:', error);
        showError('Failed to load voice automations');
    }
}

async function loadGuestSummary() {
    try {
        // Get unique guests with active automations
        const activeGuests = new Map();
        const archivedGuests = new Map();

        // Fetch all automations including archived
        const response = await fetch('/api/voice-automations?include_archived=true', {
            headers: getAuthHeaders()
        });

        if (!response.ok) return { active: [], archived: [] };

        const allAutomations = await response.json();

        allAutomations.forEach(auto => {
            if (auto.owner_type === 'guest' && auto.guest_name) {
                if (auto.status === 'active') {
                    if (!activeGuests.has(auto.guest_name)) {
                        activeGuests.set(auto.guest_name, { name: auto.guest_name, count: 0 });
                    }
                    activeGuests.get(auto.guest_name).count++;
                } else if (auto.status === 'archived') {
                    if (!archivedGuests.has(auto.guest_name)) {
                        archivedGuests.set(auto.guest_name, { name: auto.guest_name, count: 0 });
                    }
                    archivedGuests.get(auto.guest_name).count++;
                }
            }
        });

        return {
            active: Array.from(activeGuests.values()),
            archived: Array.from(archivedGuests.values())
        };

    } catch (error) {
        console.error('Error loading guest summary:', error);
        return { active: [], archived: [] };
    }
}

// ============================================================================
// Render Functions
// ============================================================================

async function renderAutomationsPage() {
    const container = document.getElementById('automations-container');
    if (!container) return;

    const guestSummary = await loadGuestSummary();

    container.innerHTML = `
        <div class="space-y-6">
            <!-- Guest Management Section -->
            <div class="bg-dark-card rounded-lg p-6 border border-dark-border">
                <h3 class="text-lg font-semibold text-white mb-4">Guest Session Management</h3>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <!-- Active Guests -->
                    <div>
                        <h4 class="text-sm font-medium text-gray-400 mb-3">Active Guests</h4>
                        ${guestSummary.active.length === 0 ?
                            '<p class="text-gray-500 text-sm">No guests with active automations</p>' :
                            `<div class="space-y-2">
                                ${guestSummary.active.map(g => `
                                    <div class="flex items-center justify-between bg-dark-bg p-3 rounded-lg">
                                        <div>
                                            <span class="text-white font-medium">${escapeHtml(g.name)}</span>
                                            <span class="text-gray-400 text-sm ml-2">${g.count} automation${g.count !== 1 ? 's' : ''}</span>
                                        </div>
                                        <button onclick="handleGuestDeparture('${escapeHtml(g.name)}')"
                                                class="px-3 py-1 bg-orange-500/20 text-orange-400 rounded hover:bg-orange-500/30 text-sm">
                                            Guest Departed
                                        </button>
                                    </div>
                                `).join('')}
                            </div>`
                        }
                    </div>

                    <!-- Archived Guests -->
                    <div>
                        <h4 class="text-sm font-medium text-gray-400 mb-3">Guests with Archived Automations</h4>
                        ${guestSummary.archived.length === 0 ?
                            '<p class="text-gray-500 text-sm">No archived guest automations</p>' :
                            `<div class="space-y-2">
                                ${guestSummary.archived.map(g => `
                                    <div class="flex items-center justify-between bg-dark-bg p-3 rounded-lg">
                                        <div>
                                            <span class="text-white font-medium">${escapeHtml(g.name)}</span>
                                            <span class="text-gray-400 text-sm ml-2">${g.count} archived</span>
                                        </div>
                                        <button onclick="handleGuestReturn('${escapeHtml(g.name)}')"
                                                class="px-3 py-1 bg-green-500/20 text-green-400 rounded hover:bg-green-500/30 text-sm">
                                            Guest Returned
                                        </button>
                                    </div>
                                `).join('')}
                            </div>`
                        }
                    </div>
                </div>
            </div>

            <!-- Filters -->
            <div class="bg-dark-card rounded-lg p-4 border border-dark-border">
                <div class="flex flex-wrap gap-4 items-center">
                    <div>
                        <label class="text-sm text-gray-400 mr-2">Owner Type:</label>
                        <select id="filter-owner-type" onchange="updateAutomationsFilter()"
                                class="bg-dark-bg border border-dark-border rounded px-3 py-1 text-white text-sm">
                            <option value="">All</option>
                            <option value="owner" ${automationsFilter.owner_type === 'owner' ? 'selected' : ''}>Owner</option>
                            <option value="guest" ${automationsFilter.owner_type === 'guest' ? 'selected' : ''}>Guest</option>
                        </select>
                    </div>
                    <div>
                        <label class="text-sm text-gray-400 mr-2">Status:</label>
                        <select id="filter-status" onchange="updateAutomationsFilter()"
                                class="bg-dark-bg border border-dark-border rounded px-3 py-1 text-white text-sm">
                            <option value="active" ${automationsFilter.status === 'active' ? 'selected' : ''}>Active</option>
                            <option value="archived" ${automationsFilter.status === 'archived' ? 'selected' : ''}>Archived</option>
                            <option value="">All</option>
                        </select>
                    </div>
                    <div>
                        <label class="text-sm text-gray-400 mr-2">Guest Name:</label>
                        <input type="text" id="filter-guest-name" placeholder="Search..."
                               value="${escapeHtml(automationsFilter.guest_name)}"
                               onchange="updateAutomationsFilter()"
                               class="bg-dark-bg border border-dark-border rounded px-3 py-1 text-white text-sm w-32">
                    </div>
                    <div class="flex items-center">
                        <input type="checkbox" id="filter-include-archived"
                               ${automationsFilter.include_archived ? 'checked' : ''}
                               onchange="updateAutomationsFilter()"
                               class="mr-2">
                        <label for="filter-include-archived" class="text-sm text-gray-400">Include Archived</label>
                    </div>
                    <button onclick="loadVoiceAutomations()"
                            class="px-4 py-1 bg-accent-blue text-white rounded hover:bg-accent-blue/80 text-sm ml-auto">
                        Refresh
                    </button>
                </div>
            </div>

            <!-- Automations Table -->
            <div class="bg-dark-card rounded-lg border border-dark-border overflow-hidden">
                <table class="w-full">
                    <thead class="bg-dark-bg">
                        <tr>
                            <th class="px-4 py-3 text-left text-sm font-medium text-gray-400">Name</th>
                            <th class="px-4 py-3 text-left text-sm font-medium text-gray-400">Owner</th>
                            <th class="px-4 py-3 text-left text-sm font-medium text-gray-400">Trigger</th>
                            <th class="px-4 py-3 text-left text-sm font-medium text-gray-400">Status</th>
                            <th class="px-4 py-3 text-left text-sm font-medium text-gray-400">Triggered</th>
                            <th class="px-4 py-3 text-left text-sm font-medium text-gray-400">Actions</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-dark-border">
                        ${automationsData.length === 0 ?
                            `<tr><td colspan="6" class="px-4 py-8 text-center text-gray-500">No automations found</td></tr>` :
                            automationsData.map(auto => renderAutomationRow(auto)).join('')
                        }
                    </tbody>
                </table>
            </div>

            <!-- Summary -->
            <div class="text-sm text-gray-400">
                Showing ${automationsData.length} automation${automationsData.length !== 1 ? 's' : ''}
            </div>
        </div>
    `;
}

function renderAutomationRow(auto) {
    const triggerDesc = formatTrigger(auto.trigger_config);
    const ownerDisplay = auto.owner_type === 'guest' ?
        `<span class="text-purple-400">Guest: ${escapeHtml(auto.guest_name || 'Unknown')}</span>` :
        '<span class="text-blue-400">Owner</span>';

    const statusBadge = auto.status === 'active' ?
        '<span class="px-2 py-1 bg-green-500/20 text-green-400 rounded text-xs">Active</span>' :
        '<span class="px-2 py-1 bg-gray-500/20 text-gray-400 rounded text-xs">Archived</span>';

    const lastTriggered = auto.last_triggered_at ?
        formatRelativeTime(auto.last_triggered_at) :
        '<span class="text-gray-500">Never</span>';

    return `
        <tr class="hover:bg-dark-bg/50">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(auto.name)}</div>
                <div class="text-xs text-gray-500">${escapeHtml(auto.ha_automation_id || '')}</div>
            </td>
            <td class="px-4 py-3">${ownerDisplay}</td>
            <td class="px-4 py-3 text-sm text-gray-300">${triggerDesc}</td>
            <td class="px-4 py-3">${statusBadge}</td>
            <td class="px-4 py-3 text-sm">
                <div class="text-gray-300">${lastTriggered}</div>
                <div class="text-xs text-gray-500">${auto.trigger_count} times</div>
            </td>
            <td class="px-4 py-3">
                <div class="flex gap-2">
                    <button onclick="showAutomationDetail(${auto.id})"
                            class="px-2 py-1 bg-dark-bg border border-dark-border rounded text-sm text-gray-300 hover:text-white">
                        View
                    </button>
                    ${auto.status === 'active' ? `
                        <button onclick="archiveAutomation(${auto.id})"
                                class="px-2 py-1 bg-orange-500/20 text-orange-400 rounded text-sm hover:bg-orange-500/30">
                            Archive
                        </button>
                    ` : `
                        <button onclick="restoreAutomation(${auto.id})"
                                class="px-2 py-1 bg-green-500/20 text-green-400 rounded text-sm hover:bg-green-500/30">
                            Restore
                        </button>
                    `}
                    ${auto.owner_type === 'owner' ? `
                        <button onclick="deleteAutomation(${auto.id})"
                                class="px-2 py-1 bg-red-500/20 text-red-400 rounded text-sm hover:bg-red-500/30">
                            Delete
                        </button>
                    ` : ''}
                </div>
            </td>
        </tr>
    `;
}

function formatTrigger(config) {
    if (!config) return '<span class="text-gray-500">Unknown</span>';

    const type = config.type || config.platform || 'unknown';

    if (type === 'time' || config.at) {
        return `Time: ${config.at || config.time || '?'}`;
    } else if (type === 'sun' || config.event === 'sunset' || config.event === 'sunrise') {
        return `Sun: ${config.event || type}`;
    } else if (type === 'motion' || type === 'state') {
        return `State: ${config.entity_id || '?'}`;
    } else if (type === 'numeric_state') {
        const above = config.above ? `> ${config.above}` : '';
        const below = config.below ? `< ${config.below}` : '';
        return `Numeric: ${config.entity_id || '?'} ${above} ${below}`;
    }

    return type;
}

function formatRelativeTime(dateStr) {
    if (!dateStr) return '';

    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    return date.toLocaleDateString();
}

// ============================================================================
// Filter Functions
// ============================================================================

function updateAutomationsFilter() {
    automationsFilter.owner_type = document.getElementById('filter-owner-type')?.value || '';
    automationsFilter.status = document.getElementById('filter-status')?.value || '';
    automationsFilter.guest_name = document.getElementById('filter-guest-name')?.value || '';
    automationsFilter.include_archived = document.getElementById('filter-include-archived')?.checked || false;

    loadVoiceAutomations();
}

// ============================================================================
// Action Functions
// ============================================================================

async function archiveAutomation(id) {
    if (!confirm('Archive this automation?')) return;

    try {
        const response = await fetch(`/api/voice-automations/${id}/archive?reason=user_deleted`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to archive');

        showSuccess('Automation archived');
        loadVoiceAutomations();

    } catch (error) {
        console.error('Error archiving automation:', error);
        showError('Failed to archive automation');
    }
}

async function restoreAutomation(id) {
    try {
        const response = await fetch(`/api/voice-automations/${id}/restore`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to restore');

        showSuccess('Automation restored');
        loadVoiceAutomations();

    } catch (error) {
        console.error('Error restoring automation:', error);
        showError('Failed to restore automation');
    }
}

async function deleteAutomation(id) {
    if (!confirm('Permanently delete this automation? This cannot be undone.')) return;

    try {
        const response = await fetch(`/api/voice-automations/${id}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to delete');

        showSuccess('Automation deleted');
        loadVoiceAutomations();

    } catch (error) {
        console.error('Error deleting automation:', error);
        showError('Failed to delete automation');
    }
}

async function handleGuestDeparture(guestName) {
    if (!confirm(`Archive all automations for guest "${guestName}"?`)) return;

    try {
        const response = await fetch('/api/voice-automations/archive-guest', {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ guest_name: guestName, reason: 'guest_departed' })
        });

        if (!response.ok) throw new Error('Failed to archive guest automations');

        const result = await response.json();
        showSuccess(`Archived ${result.archived_count} automation(s) for ${guestName}`);
        loadVoiceAutomations();

    } catch (error) {
        console.error('Error handling guest departure:', error);
        showError('Failed to archive guest automations');
    }
}

async function handleGuestReturn(guestName) {
    if (!confirm(`Restore all archived automations for returning guest "${guestName}"?`)) return;

    try {
        const response = await fetch('/api/voice-automations/restore-guest', {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ guest_name: guestName })
        });

        if (!response.ok) throw new Error('Failed to restore guest automations');

        const result = await response.json();
        showSuccess(`Restored ${result.restored_count} automation(s) for ${guestName}`);
        loadVoiceAutomations();

    } catch (error) {
        console.error('Error handling guest return:', error);
        showError('Failed to restore guest automations');
    }
}

async function showAutomationDetail(id) {
    const auto = automationsData.find(a => a.id === id);
    if (!auto) return;

    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.onclick = (e) => { if (e.target === modal) modal.remove(); };

    modal.innerHTML = `
        <div class="bg-dark-card rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto border border-dark-border">
            <div class="flex justify-between items-start mb-4">
                <h3 class="text-lg font-semibold text-white">${escapeHtml(auto.name)}</h3>
                <button onclick="this.closest('.fixed').remove()" class="text-gray-400 hover:text-white text-xl">&times;</button>
            </div>

            <div class="space-y-4">
                <div class="grid grid-cols-2 gap-4 text-sm">
                    <div>
                        <span class="text-gray-400">ID:</span>
                        <span class="text-white ml-2">${auto.id}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">HA ID:</span>
                        <span class="text-white ml-2 font-mono text-xs">${escapeHtml(auto.ha_automation_id || 'N/A')}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Owner:</span>
                        <span class="text-white ml-2">${auto.owner_type}${auto.guest_name ? ` (${escapeHtml(auto.guest_name)})` : ''}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Status:</span>
                        <span class="text-white ml-2">${auto.status}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Created:</span>
                        <span class="text-white ml-2">${auto.created_at ? new Date(auto.created_at).toLocaleString() : 'Unknown'}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Room:</span>
                        <span class="text-white ml-2">${escapeHtml(auto.created_by_room || 'Unknown')}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Trigger Count:</span>
                        <span class="text-white ml-2">${auto.trigger_count}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Last Triggered:</span>
                        <span class="text-white ml-2">${auto.last_triggered_at ? new Date(auto.last_triggered_at).toLocaleString() : 'Never'}</span>
                    </div>
                </div>

                <div>
                    <h4 class="text-sm font-medium text-gray-400 mb-2">Trigger Configuration</h4>
                    <pre class="bg-dark-bg p-3 rounded text-xs text-gray-300 overflow-x-auto">${JSON.stringify(auto.trigger_config, null, 2)}</pre>
                </div>

                ${auto.conditions_config && auto.conditions_config.length > 0 ? `
                    <div>
                        <h4 class="text-sm font-medium text-gray-400 mb-2">Conditions</h4>
                        <pre class="bg-dark-bg p-3 rounded text-xs text-gray-300 overflow-x-auto">${JSON.stringify(auto.conditions_config, null, 2)}</pre>
                    </div>
                ` : ''}

                <div>
                    <h4 class="text-sm font-medium text-gray-400 mb-2">Actions</h4>
                    <pre class="bg-dark-bg p-3 rounded text-xs text-gray-300 overflow-x-auto">${JSON.stringify(auto.actions_config, null, 2)}</pre>
                </div>

                ${auto.status === 'archived' ? `
                    <div class="bg-orange-500/10 border border-orange-500/30 rounded p-3">
                        <p class="text-orange-400 text-sm">
                            <strong>Archived:</strong> ${auto.archived_at ? new Date(auto.archived_at).toLocaleString() : 'Unknown'}
                            ${auto.archive_reason ? ` - Reason: ${escapeHtml(auto.archive_reason)}` : ''}
                        </p>
                    </div>
                ` : ''}
            </div>
        </div>
    `;

    document.body.appendChild(modal);
}

// ============================================================================
// Utility Functions (if not already defined in utils.js)
// ============================================================================

function showLoading(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="flex items-center justify-center py-12">
                <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-accent-blue"></div>
            </div>
        `;
    }
}

// ============================================================================
// Export for global access
// ============================================================================

window.loadVoiceAutomations = loadVoiceAutomations;
window.updateAutomationsFilter = updateAutomationsFilter;
window.archiveAutomation = archiveAutomation;
window.restoreAutomation = restoreAutomation;
window.deleteAutomation = deleteAutomation;
window.handleGuestDeparture = handleGuestDeparture;
window.handleGuestReturn = handleGuestReturn;
window.showAutomationDetail = showAutomationDetail;
