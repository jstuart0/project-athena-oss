// Calendar Sources Management JavaScript
// Handles CRUD operations and sync for iCal calendar sources (Airbnb, VRBO, Lodgify, etc.)

const CALENDAR_SOURCES_API = '/api/calendar-sources';

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let allCalendarSources = [];
let sourceTypes = [];

async function loadCalendarSources() {
    try {
        showLoadingState('calendar-sources-container');

        const response = await fetch(CALENDAR_SOURCES_API, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load calendar sources');

        allCalendarSources = await response.json();
        renderCalendarSources(allCalendarSources);

        // Also load source types for the form
        loadSourceTypes();
    } catch (error) {
        console.error('Error loading calendar sources:', error);
        showError('calendar-sources-container', 'Failed to load calendar sources');
    }
}

async function loadSourceTypes() {
    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/types`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (response.ok) {
            sourceTypes = await response.json();
        }
    } catch (error) {
        console.error('Error loading source types:', error);
        // Use defaults
        sourceTypes = [
            { type: 'airbnb', name: 'Airbnb' },
            { type: 'vrbo', name: 'VRBO' },
            { type: 'lodgify', name: 'Lodgify' },
            { type: 'generic_ical', name: 'Generic iCal' }
        ];
    }
}

function showLoadingState(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="animate-pulse">Loading calendar sources...</div>
            </div>
        `;
    }
}

function showError(containerId, message) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg p-4 text-center">
                <p class="text-red-400">${message}</p>
            </div>
        `;
    }
}

// ============================================================================
// RENDER FUNCTIONS
// ============================================================================

function renderCalendarSources(sources) {
    const container = document.getElementById('calendar-sources-container');

    if (!sources || sources.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-4xl mb-4">📅</div>
                <p class="text-lg">No calendar sources configured</p>
                <p class="text-sm mt-2">Add your first iCal source from Airbnb, VRBO, or Lodgify to sync guest reservations</p>
            </div>
        `;
        return;
    }

    container.innerHTML = sources.map(source => renderSourceCard(source)).join('');
}

function renderSourceCard(source) {
    const typeIcon = getSourceTypeIcon(source.source_type);
    const statusBadge = getStatusBadge(source);
    const lastSyncText = source.last_sync_at
        ? formatDate(source.last_sync_at)
        : 'Never synced';

    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 mb-4">
            <div class="flex justify-between items-start mb-4">
                <div class="flex items-start gap-4">
                    <div class="text-3xl">${typeIcon}</div>
                    <div>
                        <div class="flex items-center gap-3">
                            <h3 class="text-xl font-semibold text-white">${escapeHtml(source.name)}</h3>
                            <span class="px-2 py-1 text-xs rounded ${source.enabled ? 'bg-green-900/50 text-green-400' : 'bg-red-900/50 text-red-400'}">
                                ${source.enabled ? 'Enabled' : 'Disabled'}
                            </span>
                        </div>
                        <p class="text-gray-500 text-sm mt-1">${getSourceTypeName(source.source_type)}</p>
                        ${source.description ? `<p class="text-gray-400 text-sm mt-2">${escapeHtml(source.description)}</p>` : ''}
                    </div>
                </div>
                <div class="flex gap-2">
                    <button onclick="testCalendarSource(${source.id})" class="px-3 py-1 bg-purple-600 hover:bg-purple-700 text-white rounded text-sm" title="Test Connection">
                        Test
                    </button>
                    <button onclick="syncCalendarSource(${source.id})" class="px-3 py-1 bg-green-600 hover:bg-green-700 text-white rounded text-sm" title="Sync Now">
                        Sync
                    </button>
                    <button onclick="editCalendarSource(${source.id})" class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm">
                        Edit
                    </button>
                    <button onclick="deleteCalendarSource(${source.id}, '${escapeHtml(source.name)}')" class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm">
                        Delete
                    </button>
                </div>
            </div>

            <!-- URL Display (masked) -->
            <div class="mb-4 bg-gray-800/50 rounded px-3 py-2">
                <label class="text-xs text-gray-500">iCal URL</label>
                <p class="text-gray-300 font-mono text-sm">${escapeHtml(source.ical_url_masked || source.ical_url)}</p>
            </div>

            <!-- Stats Row -->
            <div class="grid grid-cols-4 gap-4 text-sm">
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Last Sync</label>
                    <p class="text-white">${lastSyncText}</p>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Status</label>
                    ${statusBadge}
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Events Found</label>
                    <p class="text-white">${source.last_event_count || 0}</p>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Sync Interval</label>
                    <p class="text-white">${source.sync_interval_minutes} min</p>
                </div>
            </div>

            ${source.last_sync_error ? `
                <div class="mt-4 bg-red-900/30 border border-red-700 rounded p-3">
                    <label class="text-xs text-red-400 block">Last Error</label>
                    <p class="text-red-300 text-sm">${escapeHtml(source.last_sync_error)}</p>
                </div>
            ` : ''}
        </div>
    `;
}

function getSourceTypeIcon(type) {
    const icons = {
        'airbnb': '🏠',
        'vrbo': '🏡',
        'lodgify': '🏢',
        'generic_ical': '📅'
    };
    return icons[type] || '📅';
}

function getSourceTypeName(type) {
    const source = sourceTypes.find(s => s.type === type);
    return source ? source.name : type;
}

function getStatusBadge(source) {
    const statusClasses = {
        'success': 'bg-green-900/50 text-green-400',
        'failed': 'bg-red-900/50 text-red-400',
        'pending': 'bg-yellow-900/50 text-yellow-400'
    };
    const status = source.last_sync_status || 'pending';
    return `<span class="px-2 py-1 rounded text-xs ${statusClasses[status] || statusClasses.pending}">${status}</span>`;
}

function formatDate(isoString) {
    if (!isoString) return 'Never';
    const date = new Date(isoString);
    return date.toLocaleString();
}

// ============================================================================
// MODAL FUNCTIONS
// ============================================================================

function showCreateCalendarSourceModal() {
    const modal = document.createElement('div');
    modal.id = 'calendar-source-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';

    const typeOptions = sourceTypes.map(t =>
        `<option value="${t.type}">${t.name}</option>`
    ).join('');

    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <h3 class="text-xl font-semibold text-white mb-4">Add Calendar Source</h3>

            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Name *</label>
                    <input type="text" id="source-name" placeholder="Airbnb - Main Property"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-blue-500">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Source Type *</label>
                    <select id="source-type"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        ${typeOptions}
                    </select>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">iCal URL *</label>
                    <input type="url" id="source-ical-url" placeholder="https://www.airbnb.com/calendar/ical/..."
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-blue-500">
                    <p class="text-xs text-gray-500 mt-1">Find this in your platform's calendar export settings</p>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Sync Interval (minutes)</label>
                    <input type="number" id="source-sync-interval" value="30" min="5" max="1440"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Priority</label>
                    <input type="number" id="source-priority" value="1" min="1" max="10"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    <p class="text-xs text-gray-500 mt-1">Higher priority sources take precedence for conflicting events</p>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Description (optional)</label>
                    <textarea id="source-description" rows="2" placeholder="Notes about this calendar source"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-blue-500"></textarea>
                </div>

                <div class="flex items-center gap-2">
                    <input type="checkbox" id="source-enabled" checked
                        class="w-4 h-4 bg-gray-800 border-gray-700 rounded text-blue-600 focus:ring-blue-500">
                    <label for="source-enabled" class="text-sm text-gray-400">Enabled</label>
                </div>
            </div>

            <div class="flex justify-between mt-6">
                <button onclick="testUrlFromModal()" class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm">
                    Test URL
                </button>
                <div class="flex gap-3">
                    <button onclick="closeCalendarSourceModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="createCalendarSource()" class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm">
                        Create Source
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeCalendarSourceModal();
    });
}

function showEditCalendarSourceModal(source) {
    const modal = document.createElement('div');
    modal.id = 'calendar-source-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';

    const typeOptions = sourceTypes.map(t =>
        `<option value="${t.type}" ${t.type === source.source_type ? 'selected' : ''}>${t.name}</option>`
    ).join('');

    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <h3 class="text-xl font-semibold text-white mb-4">Edit Calendar Source</h3>

            <input type="hidden" id="edit-source-id" value="${source.id}">

            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Name *</label>
                    <input type="text" id="source-name" value="${escapeHtml(source.name)}"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Source Type *</label>
                    <select id="source-type"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        ${typeOptions}
                    </select>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">iCal URL *</label>
                    <input type="url" id="source-ical-url" value="${escapeHtml(source.ical_url)}"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Sync Interval (minutes)</label>
                    <input type="number" id="source-sync-interval" value="${source.sync_interval_minutes}" min="5" max="1440"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Priority</label>
                    <input type="number" id="source-priority" value="${source.priority}" min="1" max="10"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Description</label>
                    <textarea id="source-description" rows="2"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">${escapeHtml(source.description || '')}</textarea>
                </div>

                <div class="flex items-center gap-2">
                    <input type="checkbox" id="source-enabled" ${source.enabled ? 'checked' : ''}
                        class="w-4 h-4 bg-gray-800 border-gray-700 rounded text-blue-600 focus:ring-blue-500">
                    <label for="source-enabled" class="text-sm text-gray-400">Enabled</label>
                </div>
            </div>

            <div class="flex justify-between mt-6">
                <button onclick="testUrlFromModal()" class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm">
                    Test URL
                </button>
                <div class="flex gap-3">
                    <button onclick="closeCalendarSourceModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="updateCalendarSource()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                        Update Source
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeCalendarSourceModal();
    });
}

function closeCalendarSourceModal() {
    const modal = document.getElementById('calendar-source-modal');
    if (modal) modal.remove();
}

// ============================================================================
// CRUD OPERATIONS
// ============================================================================

async function createCalendarSource() {
    const name = document.getElementById('source-name').value.trim();
    const sourceType = document.getElementById('source-type').value;
    const icalUrl = document.getElementById('source-ical-url').value.trim();
    const syncInterval = parseInt(document.getElementById('source-sync-interval').value) || 30;
    const priority = parseInt(document.getElementById('source-priority').value) || 1;
    const description = document.getElementById('source-description').value.trim();
    const enabled = document.getElementById('source-enabled').checked;

    if (!name || !icalUrl) {
        showToast('Name and iCal URL are required', 'error');
        return;
    }

    try {
        const response = await fetch(CALENDAR_SOURCES_API, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                name,
                source_type: sourceType,
                ical_url: icalUrl,
                sync_interval_minutes: syncInterval,
                priority,
                description: description || null,
                enabled
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create calendar source');
        }

        showToast('Calendar source created successfully', 'success');
        closeCalendarSourceModal();
        loadCalendarSources();
    } catch (error) {
        console.error('Error creating calendar source:', error);
        showToast(error.message, 'error');
    }
}

async function editCalendarSource(sourceId) {
    const source = allCalendarSources.find(s => s.id === sourceId);
    if (!source) {
        showToast('Source not found', 'error');
        return;
    }

    // Fetch full source with URL
    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/${sourceId}`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (response.ok) {
            const fullSource = await response.json();
            showEditCalendarSourceModal(fullSource);
        } else {
            showEditCalendarSourceModal(source);
        }
    } catch (error) {
        showEditCalendarSourceModal(source);
    }
}

async function updateCalendarSource() {
    const sourceId = document.getElementById('edit-source-id').value;
    const name = document.getElementById('source-name').value.trim();
    const sourceType = document.getElementById('source-type').value;
    const icalUrl = document.getElementById('source-ical-url').value.trim();
    const syncInterval = parseInt(document.getElementById('source-sync-interval').value) || 30;
    const priority = parseInt(document.getElementById('source-priority').value) || 1;
    const description = document.getElementById('source-description').value.trim();
    const enabled = document.getElementById('source-enabled').checked;

    if (!name || !icalUrl) {
        showToast('Name and iCal URL are required', 'error');
        return;
    }

    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/${sourceId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                name,
                source_type: sourceType,
                ical_url: icalUrl,
                sync_interval_minutes: syncInterval,
                priority,
                description: description || null,
                enabled
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update calendar source');
        }

        showToast('Calendar source updated successfully', 'success');
        closeCalendarSourceModal();
        loadCalendarSources();
    } catch (error) {
        console.error('Error updating calendar source:', error);
        showToast(error.message, 'error');
    }
}

async function deleteCalendarSource(sourceId, sourceName) {
    if (!confirm(`Are you sure you want to delete "${sourceName}"?\n\nThis will NOT delete events that were already synced from this source.`)) {
        return;
    }

    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/${sourceId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) {
            throw new Error('Failed to delete calendar source');
        }

        showToast('Calendar source deleted', 'success');
        loadCalendarSources();
    } catch (error) {
        console.error('Error deleting calendar source:', error);
        showToast(error.message, 'error');
    }
}

// ============================================================================
// SYNC AND TEST OPERATIONS
// ============================================================================

async function testCalendarSource(sourceId) {
    showToast('Testing connection...', 'info');

    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/${sourceId}/test`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        const result = await response.json();

        if (result.success) {
            showTestResultModal(result);
        } else {
            showToast(`Test failed: ${result.message}`, 'error');
        }
    } catch (error) {
        console.error('Error testing calendar source:', error);
        showToast('Failed to test connection', 'error');
    }
}

async function testUrlFromModal() {
    const url = document.getElementById('source-ical-url').value.trim();
    const sourceType = document.getElementById('source-type').value;

    if (!url) {
        showToast('Please enter an iCal URL first', 'error');
        return;
    }

    showToast('Testing URL...', 'info');

    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/test-url?url=${encodeURIComponent(url)}&source_type=${sourceType}`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        const result = await response.json();

        if (result.success) {
            showTestResultModal(result);
        } else {
            showToast(`Test failed: ${result.message}`, 'error');
        }
    } catch (error) {
        console.error('Error testing URL:', error);
        showToast('Failed to test URL', 'error');
    }
}

function showTestResultModal(result) {
    const modal = document.createElement('div');
    modal.id = 'test-result-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';

    const eventsHtml = result.sample_events && result.sample_events.length > 0
        ? result.sample_events.map(e => `
            <div class="bg-gray-800/50 rounded p-3">
                <p class="text-white font-medium">${escapeHtml(e.title || 'Reservation')}</p>
                <p class="text-gray-400 text-sm">Check-in: ${formatDate(e.checkin)}</p>
                <p class="text-gray-400 text-sm">Check-out: ${formatDate(e.checkout)}</p>
                ${e.guest_name ? `<p class="text-gray-400 text-sm">Guest: ${escapeHtml(e.guest_name)}</p>` : ''}
            </div>
        `).join('')
        : '<p class="text-gray-500">No upcoming events found</p>';

    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg mx-4">
            <div class="flex items-center gap-3 mb-4">
                <div class="text-3xl">✅</div>
                <h3 class="text-xl font-semibold text-white">Connection Successful</h3>
            </div>

            <div class="bg-green-900/30 border border-green-700 rounded p-3 mb-4">
                <p class="text-green-400">${result.message}</p>
            </div>

            <div class="mb-4">
                <h4 class="text-sm font-medium text-gray-300 mb-2">Upcoming Events Preview</h4>
                <div class="space-y-2">
                    ${eventsHtml}
                </div>
            </div>

            <div class="flex justify-end">
                <button onclick="document.getElementById('test-result-modal').remove()"
                    class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                    Close
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.remove();
    });
}

async function syncCalendarSource(sourceId) {
    showToast('Syncing calendar...', 'info');

    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/${sourceId}/sync`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        const result = await response.json();

        if (result.success) {
            showToast(`Sync complete: ${result.events_added} added, ${result.events_updated} updated`, 'success');
            loadCalendarSources();
        } else {
            showToast(`Sync failed: ${result.message}`, 'error');
            loadCalendarSources();
        }
    } catch (error) {
        console.error('Error syncing calendar source:', error);
        showToast('Sync failed', 'error');
    }
}

async function syncAllCalendarSources() {
    showToast('Triggering sync for all sources...', 'info');

    try {
        const response = await fetch(`${CALENDAR_SOURCES_API}/sync-all`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        const result = await response.json();
        showToast(result.message, 'success');

        // Reload after a short delay
        setTimeout(() => loadCalendarSources(), 2000);
    } catch (error) {
        console.error('Error syncing all sources:', error);
        showToast('Failed to trigger sync', 'error');
    }
}

// ============================================================================
// UTILITIES
// ============================================================================

// escapeHtml, getAuthToken, and showNotification are now provided by utils.js

function getToken() {
    return getAuthToken();
}

// ============================================================================
// INITIALIZATION
// ============================================================================

function initCalendarSources() {
    loadCalendarSources();
    console.log('Calendar Sources module initialized');
}

// Auto-initialize when page loads
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        if (window.location.hash === '#calendar-sources') {
            initCalendarSources();
        }
    });
} else {
    if (window.location.hash === '#calendar-sources') {
        initCalendarSources();
    }
}
