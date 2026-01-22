/**
 * API Keys Management Module
 *
 * Allows users to create, view, and revoke their API keys.
 */

// API endpoint
const USER_API_KEYS_URL = '/api/user-api-keys';

/**
 * Initialize API keys module
 */
function initApiKeys() {
    // Module will be loaded when tab is shown
    console.log('API Keys module initialized');
}

/**
 * Load and display user's API keys
 */
async function loadUserApiKeys() {
    const container = document.getElementById('user-api-keys-container');
    if (!container) return;

    container.innerHTML = '<div class="text-gray-400 text-center py-8">Loading...</div>';

    try {
        const response = await fetch(USER_API_KEYS_URL, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load API keys');

        const keys = await response.json();
        renderUserApiKeys(keys, container);
    } catch (error) {
        console.error('Error loading API keys:', error);
        container.innerHTML = '<div class="text-red-400 text-center py-8">Failed to load API keys</div>';
    }
}

/**
 * Render API keys list
 */
function renderUserApiKeys(keys, container) {
    if (keys.length === 0) {
        container.innerHTML = `
            <div class="bg-dark-surface border border-dark-border rounded-lg p-8 text-center">
                <div class="text-4xl mb-4">🔑</div>
                <p class="text-gray-400 mb-2">No API keys yet</p>
                <p class="text-gray-500 text-sm">Create one to enable programmatic access to the API.</p>
            </div>
        `;
        return;
    }

    const html = keys.map(key => {
        const statusClass = key.revoked ? 'bg-red-900/30 border-red-700' : 'bg-dark-surface border-dark-border';
        const statusBadge = key.revoked
            ? '<span class="px-2 py-1 bg-red-600/30 text-red-300 text-xs rounded-full">Revoked</span>'
            : '<span class="px-2 py-1 bg-green-600/30 text-green-300 text-xs rounded-full">Active</span>';

        const expiresText = key.expires_at
            ? `Expires: ${new Date(key.expires_at).toLocaleDateString()}`
            : 'No expiration';

        const lastUsedText = key.last_used_at
            ? `Last used: ${new Date(key.last_used_at).toLocaleString()}`
            : 'Never used';

        return `
            <div class="${statusClass} border rounded-lg p-4 transition-all hover:border-blue-500/50" data-key-id="${key.id}">
                <div class="flex justify-between items-start mb-3">
                    <div>
                        <h3 class="text-white font-medium">${escapeHtmlApiKeys(key.name)}</h3>
                        <code class="text-gray-400 text-sm font-mono">${key.key_prefix}...</code>
                    </div>
                    <div class="flex items-center gap-2">
                        ${statusBadge}
                        ${!key.revoked ? `
                            <button onclick="revokeUserApiKey(${key.id}, '${escapeHtmlApiKeys(key.name)}')"
                                    class="px-3 py-1 bg-red-600/20 hover:bg-red-600/40 text-red-300 rounded text-sm transition-colors">
                                Revoke
                            </button>
                        ` : ''}
                    </div>
                </div>
                <div class="flex flex-wrap gap-2 mb-3">
                    ${key.scopes.map(s => `<span class="px-2 py-1 bg-blue-600/20 text-blue-300 text-xs rounded">${escapeHtmlApiKeys(s)}</span>`).join('')}
                </div>
                <div class="flex flex-wrap gap-4 text-xs text-gray-500">
                    <span>Created: ${new Date(key.created_at).toLocaleDateString()}</span>
                    <span>${expiresText}</span>
                    <span>${lastUsedText}</span>
                    <span>${key.request_count} requests</span>
                </div>
                ${key.revoked && key.revoked_at ? `
                    <div class="mt-2 text-xs text-red-400">
                        Revoked: ${new Date(key.revoked_at).toLocaleString()}
                    </div>
                ` : ''}
            </div>
        `;
    }).join('');

    container.innerHTML = html;
}

// Available scopes for API keys
const API_KEY_SCOPES = [
    { value: 'read:*', label: 'Read All', description: 'Read access to all resources' },
    { value: 'write:*', label: 'Write All', description: 'Create/update access to all resources' },
    { value: 'delete:*', label: 'Delete All', description: 'Delete access to all resources' },
    { value: 'read:devices', label: 'Read Devices', description: 'View device configurations' },
    { value: 'write:devices', label: 'Write Devices', description: 'Modify device configurations' },
    { value: 'read:features', label: 'Read Features', description: 'View feature flags' },
    { value: 'write:features', label: 'Write Features', description: 'Modify feature flags' },
    { value: 'read:rag-services', label: 'Read RAG Services', description: 'View RAG service configs' },
    { value: 'write:rag-services', label: 'Write RAG Services', description: 'Modify RAG services' },
];

/**
 * Toggle all scope checkboxes
 */
function toggleAllScopes(selectAll) {
    const checkboxes = document.querySelectorAll('#scope-checkboxes input[type="checkbox"]');
    checkboxes.forEach(cb => cb.checked = selectAll);
}

/**
 * Show create API key modal
 */
function showCreateUserApiKeyModal() {
    const scopeCheckboxes = API_KEY_SCOPES.map(scope => `
        <label class="flex items-start gap-2 cursor-pointer hover:bg-dark-bg/50 p-2 rounded">
            <input type="checkbox" name="scopes" value="${scope.value}"
                   class="mt-1 w-4 h-4 accent-blue-500">
            <div>
                <span class="text-white text-sm">${scope.label}</span>
                <span class="text-gray-500 text-xs block">${scope.description}</span>
            </div>
        </label>
    `).join('');

    const modalHtml = `
        <div id="create-api-key-modal" class="fixed inset-0 bg-black/70 flex items-center justify-center z-50" onclick="if(event.target === this) closeApiKeyModal()">
            <div class="bg-dark-surface border border-dark-border rounded-lg p-6 w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
                <h3 class="text-xl font-semibold text-white mb-4">Create API Key</h3>
                <form id="create-api-key-form" onsubmit="createUserApiKey(event)">
                    <div class="space-y-4">
                        <div>
                            <label class="block text-gray-400 text-sm mb-1">Name *</label>
                            <input type="text" name="name" required
                                   placeholder="e.g., CI/CD Pipeline, Local Development"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-4 py-2 text-white focus:border-blue-500 focus:outline-none">
                        </div>
                        <div>
                            <label class="block text-gray-400 text-sm mb-2">Permissions *</label>
                            <div class="flex gap-2 mb-2">
                                <button type="button" onclick="toggleAllScopes(true)"
                                        class="px-3 py-1 bg-blue-600/20 hover:bg-blue-600/40 text-blue-300 rounded text-xs transition-colors">
                                    Select All
                                </button>
                                <button type="button" onclick="toggleAllScopes(false)"
                                        class="px-3 py-1 bg-gray-600/20 hover:bg-gray-600/40 text-gray-300 rounded text-xs transition-colors">
                                    Clear All
                                </button>
                            </div>
                            <div id="scope-checkboxes" class="bg-dark-bg border border-dark-border rounded-lg p-2 max-h-48 overflow-y-auto space-y-1">
                                ${scopeCheckboxes}
                            </div>
                        </div>
                        <div>
                            <label class="block text-gray-400 text-sm mb-1">Expires In (days)</label>
                            <input type="number" name="expires_in_days" min="1" max="365" value="90"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-4 py-2 text-white focus:border-blue-500 focus:outline-none">
                        </div>
                        <div>
                            <label class="block text-gray-400 text-sm mb-1">Reason (optional)</label>
                            <input type="text" name="reason"
                                   placeholder="e.g., For automated testing"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-4 py-2 text-white focus:border-blue-500 focus:outline-none">
                        </div>
                    </div>
                    <div class="flex justify-end gap-3 mt-6">
                        <button type="button" onclick="closeApiKeyModal()"
                                class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm transition-colors">
                            Cancel
                        </button>
                        <button type="submit"
                                class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm transition-colors">
                            Create API Key
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

/**
 * Create new API key
 */
async function createUserApiKey(event) {
    event.preventDefault();

    const form = event.target;
    const name = form.querySelector('[name="name"]').value;
    const expiresInDays = parseInt(form.querySelector('[name="expires_in_days"]').value) || 90;
    const reason = form.querySelector('[name="reason"]').value;

    // Collect checked scopes from checkboxes
    const scopeCheckboxes = form.querySelectorAll('[name="scopes"]:checked');
    const scopes = Array.from(scopeCheckboxes).map(cb => cb.value);

    if (!name || scopes.length === 0) {
        alert('Name and at least one permission are required');
        return;
    }

    try {
        const response = await fetch(USER_API_KEYS_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                name,
                scopes,
                expires_in_days: expiresInDays,
                reason: reason || null
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create API key');
        }

        const result = await response.json();
        closeApiKeyModal();

        // Show the raw key (only shown once!)
        showNewKeyModal(result);

        // Reload list
        loadUserApiKeys();

    } catch (error) {
        console.error('Error creating API key:', error);
        alert('Error: ' + error.message);
    }
}

/**
 * Show modal with new API key (only shown once)
 */
function showNewKeyModal(keyData) {
    const modalHtml = `
        <div id="new-api-key-modal" class="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
            <div class="bg-dark-surface border border-dark-border rounded-lg p-6 w-full max-w-lg mx-4">
                <h3 class="text-xl font-semibold text-white mb-4">API Key Created</h3>

                <div class="bg-yellow-900/30 border border-yellow-700 rounded-lg p-4 mb-4">
                    <p class="text-yellow-200 text-sm font-medium">
                        Copy this key now. It will never be shown again.
                    </p>
                </div>

                <div class="bg-dark-bg border border-dark-border rounded-lg p-4 mb-4">
                    <div class="flex items-center gap-2">
                        <code id="new-api-key-value" class="text-green-400 font-mono text-sm flex-grow break-all">${escapeHtmlApiKeys(keyData.api_key)}</code>
                        <button onclick="copyUserApiKey()"
                                class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm flex-shrink-0 transition-colors">
                            Copy
                        </button>
                    </div>
                </div>

                <div class="text-sm text-gray-400 space-y-1 mb-4">
                    <p><strong>Name:</strong> ${escapeHtmlApiKeys(keyData.name)}</p>
                    <p><strong>Scopes:</strong> ${keyData.scopes.join(', ')}</p>
                    <p><strong>Expires:</strong> ${keyData.expires_at ? new Date(keyData.expires_at).toLocaleDateString() : 'Never'}</p>
                </div>

                <div class="bg-dark-bg border border-dark-border rounded-lg p-4 mb-4">
                    <p class="text-gray-400 text-xs mb-2">Usage Example:</p>
                    <code class="text-gray-300 text-xs font-mono break-all">curl -H "X-API-Key: ${escapeHtmlApiKeys(keyData.api_key)}" http://localhost:8080/api/devices</code>
                </div>

                <div class="flex justify-end">
                    <button onclick="closeNewKeyModal()"
                            class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm transition-colors">
                        I've Saved the Key
                    </button>
                </div>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

/**
 * Copy API key to clipboard
 */
function copyUserApiKey() {
    const keyElement = document.getElementById('new-api-key-value');
    navigator.clipboard.writeText(keyElement.textContent).then(() => {
        showNotification('API key copied to clipboard', 'success');
    }).catch(err => {
        console.error('Failed to copy:', err);
        // Fallback for older browsers
        const textArea = document.createElement('textarea');
        textArea.value = keyElement.textContent;
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        showNotification('API key copied to clipboard', 'success');
    });
}

/**
 * Revoke an API key
 */
async function revokeUserApiKey(keyId, keyName) {
    if (!confirm(`Are you sure you want to revoke the API key "${keyName}"?\n\nThis cannot be undone.`)) {
        return;
    }

    const reason = prompt('Reason for revocation (optional):');

    try {
        const response = await fetch(`${USER_API_KEYS_URL}/${keyId}`, {
            method: 'DELETE',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ reason: reason || null })
        });

        if (!response.ok && response.status !== 204) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to revoke API key');
        }

        showNotification('API key revoked', 'success');
        loadUserApiKeys();

    } catch (error) {
        console.error('Error revoking API key:', error);
        showNotification('Error: ' + error.message, 'error');
    }
}

/**
 * Close create API key modal
 */
function closeApiKeyModal() {
    const modal = document.getElementById('create-api-key-modal');
    if (modal) modal.remove();
}

/**
 * Close new key display modal
 */
function closeNewKeyModal() {
    const modal = document.getElementById('new-api-key-modal');
    if (modal) modal.remove();
}

/**
 * Helper: Escape HTML
 */
function escapeHtmlApiKeys(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initApiKeys);
} else {
    initApiKeys();
}
