// Memory Management JavaScript
// Handles all CRUD operations and UI interactions for hierarchical memory system

const MEMORIES_API = '/api/memories';

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let allMemories = [];
let allGuestSessions = [];
let memoryConfig = {};

async function loadMemories() {
    try {
        const scope = document.getElementById('memory-scope-filter')?.value || '';
        const sessionId = document.getElementById('memory-session-filter')?.value || '';
        const category = document.getElementById('memory-category-filter')?.value || '';

        let url = MEMORIES_API + '?';
        if (scope) url += `scope=${scope}&`;
        if (sessionId) url += `guest_session_id=${sessionId}&`;
        if (category) url += `category=${category}&`;

        const response = await fetch(url, {
            credentials: 'include'  // Use session cookie
        });

        if (!response.ok) throw new Error('Failed to load memories');

        const data = await response.json();
        // API returns {memories: [], total: n, counts: {...}}
        allMemories = data.memories || [];
        renderMemories(allMemories);
        updateMemoryStats();
    } catch (error) {
        console.error('Error loading memories:', error);
        showMemoryError('memories-container', 'Failed to load memories');
    }
}

async function loadGuestSessions() {
    try {
        const response = await fetch(`${MEMORIES_API}/guest-sessions`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) {
            // Handle auth errors gracefully
            console.warn('Guest sessions endpoint not accessible, using empty list');
            allGuestSessions = [];
            renderGuestSessions(allGuestSessions);
            updateSessionFilter();
            return;
        }

        const data = await response.json();
        // API returns {sessions: [...]}
        allGuestSessions = data.sessions || [];
        renderGuestSessions(allGuestSessions);
        updateSessionFilter();
    } catch (error) {
        console.error('Error loading guest sessions:', error);
        showMemoryError('guest-sessions-container', 'Failed to load guest sessions');
    }
}

async function loadMemoryConfig() {
    try {
        const response = await fetch(`${MEMORIES_API}/config`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) {
            // Handle auth errors gracefully
            console.warn('Memory config endpoint not accessible, using defaults');
            memoryConfig = {};
            renderMemoryConfig(memoryConfig);
            return;
        }

        const data = await response.json();
        // API returns {config: {...}}
        memoryConfig = data.config || {};
        renderMemoryConfig(memoryConfig);
    } catch (error) {
        console.error('Error loading memory config:', error);
        showMemoryError('memory-config-container', 'Failed to load configuration');
    }
}

async function checkQdrantHealth() {
    try {
        const response = await fetch(`${MEMORIES_API}/qdrant/health`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        const health = await response.json();
        updateQdrantStatus(health);
    } catch (error) {
        console.error('Error checking Qdrant health:', error);
        updateQdrantStatus({ healthy: false, error: error.message });
    }
}

function updateQdrantStatus(health) {
    const statusEl = document.getElementById('qdrant-status');
    if (!statusEl) return;

    // API returns status: "healthy" not healthy: true
    const isHealthy = health.status === 'healthy' || health.healthy;

    if (isHealthy) {
        statusEl.innerHTML = `
            <span class="px-2 py-1 text-xs rounded-full bg-green-900/30 text-green-400">
                Qdrant Connected
            </span>
            <span class="text-xs text-gray-500 ml-2">${health.vectors_count || health.points_count || 0} vectors</span>
        `;
    } else {
        statusEl.innerHTML = `
            <span class="px-2 py-1 text-xs rounded-full bg-red-900/30 text-red-400">
                Qdrant Unavailable
            </span>
            <span class="text-xs text-gray-500 ml-2">${health.error || 'Connection failed'}</span>
        `;
    }
}

function updateMemoryStats() {
    const globalCount = allMemories.filter(m => m.scope === 'global').length;
    const ownerCount = allMemories.filter(m => m.scope === 'owner').length;
    const guestCount = allMemories.filter(m => m.scope === 'guest').length;

    document.getElementById('stat-global-memories').textContent = globalCount;
    document.getElementById('stat-owner-memories').textContent = ownerCount;
    document.getElementById('stat-guest-memories').textContent = guestCount;
}

function updateSessionFilter() {
    const select = document.getElementById('memory-session-filter');
    if (!select) return;

    // Keep current selection
    const currentValue = select.value;

    // Clear options except first
    select.innerHTML = '<option value="">All Sessions</option>';

    // Add active sessions first
    const activeSessions = allGuestSessions.filter(s => s.status === 'active');
    if (activeSessions.length > 0) {
        const activeGroup = document.createElement('optgroup');
        activeGroup.label = 'Active Sessions';
        activeSessions.forEach(session => {
            const option = document.createElement('option');
            option.value = session.id;
            option.textContent = `${session.guest_name} (${formatDate(session.check_in_date)} - ${formatDate(session.check_out_date)})`;
            activeGroup.appendChild(option);
        });
        select.appendChild(activeGroup);
    }

    // Add recent sessions
    const recentSessions = allGuestSessions.filter(s => s.status !== 'active').slice(0, 10);
    if (recentSessions.length > 0) {
        const recentGroup = document.createElement('optgroup');
        recentGroup.label = 'Recent Sessions';
        recentSessions.forEach(session => {
            const option = document.createElement('option');
            option.value = session.id;
            option.textContent = `${session.guest_name} (${formatDate(session.check_in_date)} - ${formatDate(session.check_out_date)})`;
            recentGroup.appendChild(option);
        });
        select.appendChild(recentGroup);
    }

    // Restore selection if still valid
    if (currentValue) {
        select.value = currentValue;
    }
}

// ============================================================================
// RENDER FUNCTIONS
// ============================================================================

function renderMemories(memories) {
    const container = document.getElementById('memories-container');

    if (!memories || memories.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">🧠</div>
                <p>No memories found</p>
                <p class="text-sm mt-2">Create your first memory to get started</p>
            </div>
        `;
        return;
    }

    // Sort by importance (highest first), then by created_at
    memories.sort((a, b) => {
        if (b.importance !== a.importance) {
            return b.importance - a.importance;
        }
        return new Date(b.created_at) - new Date(a.created_at);
    });

    container.innerHTML = `
        <table class="crud-table">
            <thead>
                <tr>
                    <th>Scope</th>
                    <th>Content</th>
                    <th>Category</th>
                    <th>Importance</th>
                    <th>Access Count</th>
                    <th>Session</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${memories.map(memory => `
                    <tr>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${getScopeColor(memory.scope)}">
                                ${memory.scope.toUpperCase()}
                            </span>
                        </td>
                        <td class="max-w-md">
                            <div class="text-gray-300 truncate" title="${escapeHtml(memory.content)}">
                                ${escapeHtml(memory.summary || memory.content.substring(0, 100))}
                            </div>
                            ${memory.source_type ? `<div class="text-xs text-gray-500 mt-1">Source: ${memory.source_type}</div>` : ''}
                        </td>
                        <td>
                            ${memory.category ? `
                                <span class="px-2 py-1 text-xs rounded-full bg-gray-700 text-gray-300">
                                    ${memory.category}
                                </span>
                            ` : '<span class="text-gray-500">-</span>'}
                        </td>
                        <td class="text-center">
                            <span class="${getImportanceColor(memory.importance)}">
                                ${(memory.importance * 100).toFixed(0)}%
                            </span>
                        </td>
                        <td class="text-center text-gray-400">
                            ${memory.access_count}
                        </td>
                        <td>
                            ${memory.guest_session_id ? `
                                <span class="text-xs text-gray-400">Session #${memory.guest_session_id}</span>
                            ` : '<span class="text-gray-500">-</span>'}
                        </td>
                        <td>
                            <div class="flex gap-2">
                                <button onclick="showEditMemoryModal(${memory.id})"
                                        class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors">
                                    Edit
                                </button>
                                ${memory.scope !== 'global' ? `
                                    <button onclick="promoteMemory(${memory.id})"
                                            class="px-3 py-1 bg-purple-600 hover:bg-purple-700 text-white rounded text-sm transition-colors"
                                            title="Promote to ${memory.scope === 'guest' ? 'owner' : 'global'}">
                                        Promote
                                    </button>
                                ` : ''}
                                <button onclick="deleteMemory(${memory.id})"
                                        class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors">
                                    Delete
                                </button>
                            </div>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

function renderGuestSessions(sessions) {
    const container = document.getElementById('guest-sessions-container');

    if (!sessions || sessions.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">🏠</div>
                <p>No guest sessions found</p>
                <p class="text-sm mt-2">Sessions are created from Lodgify bookings</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `
        <table class="crud-table">
            <thead>
                <tr>
                    <th>Guest</th>
                    <th>Check-in</th>
                    <th>Check-out</th>
                    <th>Status</th>
                    <th>Memories</th>
                    <th>Lodgify ID</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${sessions.map(session => `
                    <tr>
                        <td>
                            <div class="text-white font-medium">${escapeHtml(session.guest_name)}</div>
                            ${session.guest_email ? `<div class="text-xs text-gray-500">${escapeHtml(session.guest_email)}</div>` : ''}
                        </td>
                        <td class="text-gray-300">${formatDate(session.check_in_date)}</td>
                        <td class="text-gray-300">${formatDate(session.check_out_date)}</td>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${getSessionStatusColor(session.status)}">
                                ${session.status.toUpperCase()}
                            </span>
                        </td>
                        <td class="text-center text-gray-400">
                            ${session.memory_count || 0}
                        </td>
                        <td class="text-xs text-gray-500 font-mono">
                            ${session.lodgify_booking_id || '-'}
                        </td>
                        <td>
                            <div class="flex gap-2">
                                <button onclick="viewSessionMemories(${session.id})"
                                        class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors">
                                    View Memories
                                </button>
                                ${session.status === 'completed' ? `
                                    <button onclick="cleanupSessionMemories(${session.id})"
                                            class="px-3 py-1 bg-orange-600 hover:bg-orange-700 text-white rounded text-sm transition-colors"
                                            title="Remove expired guest memories">
                                        Cleanup
                                    </button>
                                ` : ''}
                            </div>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

function renderMemoryConfig(config) {
    const container = document.getElementById('memory-config-container');

    const configItems = [
        { key: 'guest_memory_retention_days', label: 'Guest Memory Retention (days)', description: 'Days to keep guest memories after checkout' },
        { key: 'auto_promote_threshold', label: 'Auto-Promote Threshold', description: 'Importance threshold for automatic promotion' },
        { key: 'max_memories_per_query', label: 'Max Memories per Query', description: 'Maximum memories to return in search results' },
        { key: 'embedding_model', label: 'Embedding Model', description: 'Model used for generating embeddings' },
        { key: 'similarity_threshold', label: 'Similarity Threshold', description: 'Minimum similarity score for memory retrieval' }
    ];

    container.innerHTML = `
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            ${configItems.map(item => {
                const value = config[item.key];
                return `
                    <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                        <div class="flex justify-between items-start mb-2">
                            <label class="text-sm font-medium text-white">${item.label}</label>
                            <button onclick="editConfigValue('${item.key}')" class="text-xs text-blue-400 hover:text-blue-300">Edit</button>
                        </div>
                        <div class="text-lg font-mono text-blue-400" id="config-${item.key}">
                            ${value !== undefined ? (typeof value === 'object' ? JSON.stringify(value) : value) : 'Not set'}
                        </div>
                        <div class="text-xs text-gray-500 mt-1">${item.description}</div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

// ============================================================================
// MODAL OPERATIONS
// ============================================================================

function showCreateMemoryModal() {
    document.getElementById('memory-form').reset();
    document.getElementById('memory-id').value = '';
    document.getElementById('memory-importance').value = '0.5';
    document.getElementById('memory-modal-title').textContent = 'Create Memory';

    // Update session dropdown
    const sessionSelect = document.getElementById('memory-guest-session');
    sessionSelect.innerHTML = '<option value="">No Session (Owner/Global)</option>';
    allGuestSessions.filter(s => s.status === 'active').forEach(session => {
        const option = document.createElement('option');
        option.value = session.id;
        option.textContent = `${session.guest_name} (${formatDate(session.check_in_date)})`;
        sessionSelect.appendChild(option);
    });

    const modal = document.getElementById('memory-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function showEditMemoryModal(memoryId) {
    const memory = allMemories.find(m => m.id === memoryId);
    if (!memory) {
        showToast('Memory not found', 'error');
        return;
    }

    document.getElementById('memory-id').value = memory.id;
    document.getElementById('memory-content').value = memory.content;
    document.getElementById('memory-summary').value = memory.summary || '';
    document.getElementById('memory-scope').value = memory.scope;
    document.getElementById('memory-category').value = memory.category || '';
    document.getElementById('memory-importance').value = memory.importance;
    document.getElementById('memory-modal-title').textContent = 'Edit Memory';

    // Update session dropdown
    const sessionSelect = document.getElementById('memory-guest-session');
    sessionSelect.innerHTML = '<option value="">No Session (Owner/Global)</option>';
    allGuestSessions.forEach(session => {
        const option = document.createElement('option');
        option.value = session.id;
        option.textContent = `${session.guest_name} (${formatDate(session.check_in_date)})`;
        if (session.id === memory.guest_session_id) option.selected = true;
        sessionSelect.appendChild(option);
    });

    const modal = document.getElementById('memory-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function closeMemoryModal(event) {
    if (event && event.target !== event.currentTarget && !event.target.classList.contains('close-btn')) {
        return;
    }

    const modal = document.getElementById('memory-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

async function saveMemory(event) {
    event.preventDefault();

    const memoryId = document.getElementById('memory-id').value;
    const isEdit = !!memoryId;

    const data = {
        content: document.getElementById('memory-content').value,
        summary: document.getElementById('memory-summary').value || null,
        scope: document.getElementById('memory-scope').value,
        category: document.getElementById('memory-category').value || null,
        importance: parseFloat(document.getElementById('memory-importance').value),
        guest_session_id: document.getElementById('memory-guest-session').value || null
    };

    try {
        const url = isEdit ? `${MEMORIES_API}/${memoryId}` : MEMORIES_API;
        const method = isEdit ? 'PUT' : 'POST';

        const response = await fetch(url, {
            method: method,
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || 'Failed to save memory');
        }

        showToast(isEdit ? 'Memory updated successfully' : 'Memory created successfully', 'success');
        closeMemoryModal();
        loadMemories();
    } catch (error) {
        console.error('Error saving memory:', error);
        showToast(error.message || 'Failed to save memory', 'error');
    }
}

async function promoteMemory(memoryId) {
    const memory = allMemories.find(m => m.id === memoryId);
    if (!memory) return;

    const newScope = memory.scope === 'guest' ? 'owner' : 'global';

    if (!confirm(`Promote this memory to ${newScope.toUpperCase()} scope?\n\nThis will make it available to ${newScope === 'global' ? 'all users' : 'the owner'}.`)) {
        return;
    }

    try {
        const response = await fetch(`${MEMORIES_API}/${memoryId}/promote`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ target_scope: newScope })
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || 'Failed to promote memory');
        }

        showToast(`Memory promoted to ${newScope} successfully`, 'success');
        loadMemories();
    } catch (error) {
        console.error('Error promoting memory:', error);
        showToast(error.message || 'Failed to promote memory', 'error');
    }
}

async function deleteMemory(memoryId) {
    const memory = allMemories.find(m => m.id === memoryId);
    if (!memory) return;

    const preview = memory.summary || memory.content.substring(0, 50);
    if (!confirm(`Are you sure you want to delete this memory?\n\n"${preview}..."\n\nThis action cannot be undone.`)) {
        return;
    }

    try {
        const response = await fetch(`${MEMORIES_API}/${memoryId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${getToken()}`
            }
        });

        if (!response.ok) throw new Error('Failed to delete memory');

        showToast('Memory deleted successfully', 'success');
        loadMemories();
    } catch (error) {
        console.error('Error deleting memory:', error);
        showToast('Failed to delete memory', 'error');
    }
}

function viewSessionMemories(sessionId) {
    // Set filter and reload
    document.getElementById('memory-scope-filter').value = 'guest';
    document.getElementById('memory-session-filter').value = sessionId;
    loadMemories();

    // Switch to memories sub-tab
    showMemorySubTab('memories');
}

async function cleanupSessionMemories(sessionId) {
    const session = allGuestSessions.find(s => s.id === sessionId);
    if (!session) return;

    if (!confirm(`Clean up memories for session "${session.guest_name}"?\n\nThis will remove all guest-scoped memories for this completed session that exceed the retention period.`)) {
        return;
    }

    try {
        const response = await fetch(`${MEMORIES_API}/guest-sessions/${sessionId}/cleanup`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${getToken()}`
            }
        });

        if (!response.ok) throw new Error('Failed to cleanup memories');

        const result = await response.json();
        showToast(`Cleaned up ${result.deleted_count} memories`, 'success');
        loadGuestSessions();
    } catch (error) {
        console.error('Error cleaning up memories:', error);
        showToast('Failed to cleanup memories', 'error');
    }
}

async function seedDefaultConfig() {
    if (!confirm('Seed default memory configuration values?\n\nThis will set default values for any missing configuration keys.')) {
        return;
    }

    try {
        const response = await fetch(`${MEMORIES_API}/config/seed-defaults`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${getToken()}`
            }
        });

        if (!response.ok) throw new Error('Failed to seed defaults');

        showToast('Default configuration seeded successfully', 'success');
        loadMemoryConfig();
    } catch (error) {
        console.error('Error seeding defaults:', error);
        showToast('Failed to seed defaults', 'error');
    }
}

function editConfigValue(key) {
    const currentValue = memoryConfig[key];
    const newValue = prompt(`Enter new value for ${key}:`, currentValue !== undefined ? String(currentValue) : '');

    if (newValue === null) return; // User cancelled

    updateConfigValue(key, newValue);
}

async function updateConfigValue(key, value) {
    try {
        // Try to parse as JSON/number if appropriate
        let parsedValue = value;
        try {
            parsedValue = JSON.parse(value);
        } catch {
            // Keep as string if not valid JSON
            if (!isNaN(value) && value.trim() !== '') {
                parsedValue = parseFloat(value);
            }
        }

        const response = await fetch(`${MEMORIES_API}/config/${key}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ value: parsedValue })
        });

        if (!response.ok) throw new Error('Failed to update config');

        showToast('Configuration updated', 'success');
        loadMemoryConfig();
    } catch (error) {
        console.error('Error updating config:', error);
        showToast('Failed to update configuration', 'error');
    }
}

// ============================================================================
// SEARCH OPERATIONS
// ============================================================================

async function searchMemories() {
    const query = document.getElementById('memory-search-input').value;
    if (!query.trim()) {
        showToast('Please enter a search query', 'error');
        return;
    }

    const scope = document.getElementById('memory-search-scope').value;
    const mode = document.getElementById('memory-search-mode').value;
    const limit = parseInt(document.getElementById('memory-search-limit').value) || 10;

    try {
        const response = await fetch(`${MEMORIES_API}/search`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                query: query,
                scope: scope || null,
                mode: mode,
                limit: limit
            })
        });

        if (!response.ok) throw new Error('Search failed');

        const results = await response.json();
        renderSearchResults(results);
    } catch (error) {
        console.error('Error searching memories:', error);
        showToast('Search failed', 'error');
    }
}

function renderSearchResults(results) {
    const container = document.getElementById('memory-search-results');

    if (!results || results.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">🔍</div>
                <p>No matching memories found</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `
        <div class="space-y-3">
            ${results.map((result, idx) => `
                <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                    <div class="flex justify-between items-start mb-2">
                        <span class="px-2 py-1 text-xs rounded-full ${getScopeColor(result.scope)}">
                            ${result.scope.toUpperCase()}
                        </span>
                        <span class="text-sm text-blue-400">
                            Score: ${(result.score * 100).toFixed(1)}%
                        </span>
                    </div>
                    <div class="text-gray-300">
                        ${escapeHtml(result.content)}
                    </div>
                    ${result.category ? `
                        <div class="mt-2 text-xs text-gray-500">
                            Category: ${result.category}
                        </div>
                    ` : ''}
                </div>
            `).join('')}
        </div>
    `;
}

// ============================================================================
// SUB-TAB NAVIGATION
// ============================================================================

function showMemorySubTab(tabName) {
    // Hide all sub-tabs
    document.querySelectorAll('.memory-subtab').forEach(tab => {
        tab.classList.add('hidden');
    });

    // Remove active class from all sub-tab buttons
    document.querySelectorAll('.memory-subtab-btn').forEach(btn => {
        btn.classList.remove('bg-blue-600', 'text-white');
        btn.classList.add('bg-dark-card', 'text-gray-400');
    });

    // Show selected sub-tab
    document.getElementById(`memory-subtab-${tabName}`).classList.remove('hidden');

    // Activate button
    document.getElementById(`memory-subtab-btn-${tabName}`).classList.remove('bg-dark-card', 'text-gray-400');
    document.getElementById(`memory-subtab-btn-${tabName}`).classList.add('bg-blue-600', 'text-white');

    // Load data for the sub-tab
    if (tabName === 'memories') {
        loadMemories();
    } else if (tabName === 'sessions') {
        loadGuestSessions();
    } else if (tabName === 'search') {
        // Clear search results
        document.getElementById('memory-search-results').innerHTML = '';
    } else if (tabName === 'config') {
        loadMemoryConfig();
    }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function getScopeColor(scope) {
    const colors = {
        'global': 'bg-purple-900/30 text-purple-400',
        'owner': 'bg-blue-900/30 text-blue-400',
        'guest': 'bg-green-900/30 text-green-400'
    };
    return colors[scope] || colors.guest;
}

function getImportanceColor(importance) {
    if (importance >= 0.8) return 'text-red-400 font-bold';
    if (importance >= 0.6) return 'text-orange-400';
    if (importance >= 0.4) return 'text-yellow-400';
    return 'text-gray-400';
}

function getSessionStatusColor(status) {
    const colors = {
        'active': 'bg-green-900/30 text-green-400',
        'upcoming': 'bg-blue-900/30 text-blue-400',
        'completed': 'bg-gray-700 text-gray-400',
        'cancelled': 'bg-red-900/30 text-red-400'
    };
    return colors[status] || colors.completed;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function showMemoryError(containerId, message) {
    const container = document.getElementById(containerId);
    container.innerHTML = `
        <div class="p-4 bg-red-900/20 border border-red-500/30 rounded-lg">
            <div class="flex items-start gap-3">
                <span class="text-2xl">X</span>
                <div>
                    <div class="text-red-400 font-semibold">Error</div>
                    <div class="text-sm text-red-300 mt-1">${message}</div>
                </div>
            </div>
        </div>
    `;
}

// ============================================================================
// INITIALIZATION
// ============================================================================

function loadAllMemoryData() {
    checkQdrantHealth();
    loadMemories();
    loadGuestSessions();
    loadMemoryConfig();
}

// Auto-load when tab is shown
if (typeof window.tabChangeCallbacks === 'undefined') {
    window.tabChangeCallbacks = {};
}

window.tabChangeCallbacks['memory-management'] = loadAllMemoryData;
