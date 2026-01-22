// Base Knowledge Management JavaScript
// Handles all CRUD operations and UI interactions for base knowledge management

const BASE_KNOWLEDGE_API = '/api/base-knowledge';

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let allKnowledge = [];

async function loadBaseKnowledge() {
    try {
        const response = await fetch(BASE_KNOWLEDGE_API, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load base knowledge');

        allKnowledge = await response.json();
        filterBaseKnowledge();
    } catch (error) {
        console.error('Error loading base knowledge:', error);
        showError('base-knowledge-container', 'Failed to load base knowledge entries');
    }
}

function filterBaseKnowledge() {
    const category = document.getElementById('knowledge-category-filter')?.value;
    const appliesTo = document.getElementById('knowledge-applies-filter')?.value;
    const enabledOnly = document.getElementById('knowledge-enabled-filter')?.checked;

    let filtered = allKnowledge;

    if (category) {
        filtered = filtered.filter(k => k.category === category);
    }
    if (appliesTo) {
        filtered = filtered.filter(k => k.applies_to === appliesTo);
    }
    if (enabledOnly) {
        filtered = filtered.filter(k => k.enabled);
    }

    renderBaseKnowledge(filtered);
}

function renderBaseKnowledge(knowledge) {
    const container = document.getElementById('base-knowledge-container');

    if (!knowledge || knowledge.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">📚</div>
                <p>No base knowledge entries found</p>
                <p class="text-sm mt-2">Create your first entry to get started</p>
            </div>
        `;
        return;
    }

    // Sort by priority (highest first), then by created_at
    knowledge.sort((a, b) => {
        if (b.priority !== a.priority) {
            return b.priority - a.priority;
        }
        return new Date(b.created_at) - new Date(a.created_at);
    });

    container.innerHTML = `
        <table class="crud-table">
            <thead>
                <tr>
                    <th><span class="inline-flex items-center gap-1">Category${typeof infoIcon === 'function' ? infoIcon('knowledge-category') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Key${typeof infoIcon === 'function' ? infoIcon('knowledge-key') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Value${typeof infoIcon === 'function' ? infoIcon('knowledge-value') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Applies To${typeof infoIcon === 'function' ? infoIcon('knowledge-applies-to') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Priority${typeof infoIcon === 'function' ? infoIcon('knowledge-priority') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Status${typeof infoIcon === 'function' ? infoIcon('knowledge-status') : ''}</span></th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${knowledge.map(entry => `
                    <tr>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${getCategoryColor(entry.category)}">
                                ${entry.category.toUpperCase()}
                            </span>
                        </td>
                        <td class="text-white font-medium">${entry.key}</td>
                        <td class="max-w-md">
                            <div class="text-gray-300 truncate" title="${escapeHtml(entry.value)}">
                                ${escapeHtml(entry.value)}
                            </div>
                            ${entry.description ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(entry.description)}</div>` : ''}
                        </td>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${getAppliesToColor(entry.applies_to)}">
                                ${entry.applies_to.toUpperCase()}
                            </span>
                        </td>
                        <td class="text-center">
                            <span class="${entry.priority > 50 ? 'text-green-400' : entry.priority > 0 ? 'text-blue-400' : 'text-gray-400'}">
                                ${entry.priority}
                            </span>
                        </td>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${entry.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400'}">
                                ${entry.enabled ? '✓ Enabled' : '✗ Disabled'}
                            </span>
                        </td>
                        <td>
                            <div class="flex gap-2">
                                <button onclick="showEditKnowledgeModal(${entry.id})"
                                        class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors">
                                    Edit
                                </button>
                                <button onclick="toggleKnowledge(${entry.id}, ${!entry.enabled})"
                                        class="px-3 py-1 ${entry.enabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded text-sm transition-colors">
                                    ${entry.enabled ? 'Disable' : 'Enable'}
                                </button>
                                <button onclick="deleteKnowledge(${entry.id})"
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

// ============================================================================
// MODAL OPERATIONS
// ============================================================================

function showCreateKnowledgeModal() {
    // Reset form
    document.getElementById('knowledge-form').reset();
    document.getElementById('knowledge-id').value = '';
    document.getElementById('knowledge-enabled').checked = true;
    document.getElementById('knowledge-priority').value = '0';
    document.getElementById('knowledge-modal-title').textContent = 'Add Base Knowledge Entry';

    // Show modal
    const modal = document.getElementById('knowledge-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function showEditKnowledgeModal(knowledgeId) {
    const entry = allKnowledge.find(k => k.id === knowledgeId);
    if (!entry) {
        showToast('Entry not found', 'error');
        return;
    }

    // Populate form
    document.getElementById('knowledge-id').value = entry.id;
    document.getElementById('knowledge-category').value = entry.category;
    document.getElementById('knowledge-key').value = entry.key;
    document.getElementById('knowledge-value').value = entry.value;
    document.getElementById('knowledge-applies-to').value = entry.applies_to;
    document.getElementById('knowledge-priority').value = entry.priority;
    document.getElementById('knowledge-description').value = entry.description || '';
    document.getElementById('knowledge-enabled').checked = entry.enabled;
    document.getElementById('knowledge-modal-title').textContent = 'Edit Base Knowledge Entry';

    // Show modal
    const modal = document.getElementById('knowledge-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function closeKnowledgeModal(event) {
    // Only close if clicking outside or close button
    if (event && event.target !== event.currentTarget && !event.target.classList.contains('close-btn')) {
        return;
    }

    const modal = document.getElementById('knowledge-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

async function saveKnowledge(event) {
    event.preventDefault();

    const knowledgeId = document.getElementById('knowledge-id').value;
    const isEdit = !!knowledgeId;

    const data = {
        category: document.getElementById('knowledge-category').value,
        key: document.getElementById('knowledge-key').value,
        value: document.getElementById('knowledge-value').value,
        applies_to: document.getElementById('knowledge-applies-to').value,
        priority: parseInt(document.getElementById('knowledge-priority').value),
        description: document.getElementById('knowledge-description').value || null,
        enabled: document.getElementById('knowledge-enabled').checked
    };

    try {
        const url = isEdit ? `${BASE_KNOWLEDGE_API}/${knowledgeId}` : BASE_KNOWLEDGE_API;
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
            throw new Error(errorData.detail || 'Failed to save entry');
        }

        showToast(isEdit ? 'Entry updated successfully' : 'Entry created successfully', 'success');
        closeKnowledgeModal();
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error saving knowledge:', error);
        showToast(error.message || 'Failed to save entry', 'error');
    }
}

async function toggleKnowledge(knowledgeId, enabled) {
    try {
        const response = await fetch(`${BASE_KNOWLEDGE_API}/${knowledgeId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ enabled })
        });

        if (!response.ok) throw new Error('Failed to toggle entry');

        showToast(`Entry ${enabled ? 'enabled' : 'disabled'} successfully`, 'success');
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error toggling knowledge:', error);
        showToast('Failed to toggle entry', 'error');
    }
}

async function deleteKnowledge(knowledgeId) {
    const entry = allKnowledge.find(k => k.id === knowledgeId);
    if (!entry) return;

    if (!confirm(`Are you sure you want to delete the entry "${entry.key}"?\n\nThis action cannot be undone.`)) {
        return;
    }

    try {
        const response = await fetch(`${BASE_KNOWLEDGE_API}/${knowledgeId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${getToken()}`
            }
        });

        if (!response.ok) throw new Error('Failed to delete entry');

        showToast('Entry deleted successfully', 'success');
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error deleting knowledge:', error);
        showToast('Failed to delete entry', 'error');
    }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function getCategoryColor(category) {
    const colors = {
        'property': 'bg-purple-900/30 text-purple-400',
        'location': 'bg-blue-900/30 text-blue-400',
        'user': 'bg-green-900/30 text-green-400',
        'temporal': 'bg-orange-900/30 text-orange-400',
        'general': 'bg-gray-700 text-gray-300'
    };
    return colors[category] || colors.general;
}

function getAppliesToColor(appliesTo) {
    const colors = {
        'both': 'bg-blue-900/30 text-blue-400',
        'guest': 'bg-green-900/30 text-green-400',
        'owner': 'bg-purple-900/30 text-purple-400'
    };
    return colors[appliesTo] || colors.both;
}

// escapeHtml and showNotification are now provided by utils.js

function showError(containerId, message) {
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

// Auto-load when tab is shown
if (typeof window.tabChangeCallbacks === 'undefined') {
    window.tabChangeCallbacks = {};
}

window.tabChangeCallbacks['base-knowledge'] = loadBaseKnowledge;
