// Tool Calling Administration JavaScript
// Handles all CRUD operations and UI interactions for tool calling management

const TOOL_CALLING_API_BASE = '/api/tool-calling';

// ============================================================================
// TOOL CALLING SETTINGS PAGE
// ============================================================================

async function loadToolCallingSettings() {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/settings/public`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load settings');

        const settings = await response.json();
        renderToolCallingSettings(settings);
    } catch (error) {
        console.error('Error loading tool calling settings:', error);
        showError('tool-calling-settings-container', 'Failed to load tool calling settings');
    }
}

function renderToolCallingSettings(settings) {
    const container = document.getElementById('tool-calling-settings-container');

    container.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6">
            <h3 class="text-lg font-semibold text-white mb-4">Global Settings</h3>

            <div class="space-y-4">
                <div class="flex items-center justify-between py-3 border-b border-dark-border">
                    <div>
                        <div class="text-white font-medium flex items-center">Tool Calling System${infoIcon('tool-enabled')}</div>
                        <div class="text-sm text-gray-400">Enable or disable the entire tool calling system</div>
                    </div>
                    <label class="relative inline-flex items-center cursor-pointer">
                        <input type="checkbox" id="setting-enabled" ${settings.enabled ? 'checked' : ''}
                               onchange="updateSetting('enabled', this.checked)" class="sr-only peer">
                        <div class="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-green-600"></div>
                    </label>
                </div>

                <div class="py-3 border-b border-dark-border">
                    <label class="block text-white font-medium mb-2 flex items-center">Max Parallel Tools${infoIcon('tool-parallel')}</label>
                    <div class="text-sm text-gray-400 mb-2">Maximum number of tools that can execute in parallel</div>
                    <input type="number" id="setting-max-parallel" value="${settings.max_parallel_tools}"
                           onchange="updateSetting('max_parallel_tools', parseInt(this.value))"
                           min="1" max="10"
                           class="w-32 px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="py-3 border-b border-dark-border">
                    <label class="block text-white font-medium mb-2 flex items-center">Tool Call Timeout (seconds)${infoIcon('tool-timeout')}</label>
                    <div class="text-sm text-gray-400 mb-2">Maximum time to wait for tool execution</div>
                    <input type="number" id="setting-timeout" value="${settings.tool_call_timeout_seconds}"
                           onchange="updateSetting('tool_call_timeout_seconds', parseInt(this.value))"
                           min="5" max="300"
                           class="w-32 px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="py-3 border-b border-dark-border">
                    <label class="block text-white font-medium mb-2 flex items-center">Default Model${infoIcon('llm-backend', 'LLM model to use for tool calling decisions. Faster models reduce latency but may be less accurate.')}</label>
                    <div class="text-sm text-gray-400 mb-2">LLM model to use for tool calling decisions</div>
                    <select id="setting-model" onchange="updateSetting('default_model', this.value)"
                            class="w-64 px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        <option value="gpt-4o-mini" ${settings.default_model === 'gpt-4o-mini' ? 'selected' : ''}>GPT-4o Mini (OpenAI)</option>
                        <option value="phi3:mini" ${settings.default_model === 'phi3:mini' ? 'selected' : ''}>Phi-3 Mini (Ollama)</option>
                        <option value="llama3.1:8b" ${settings.default_model === 'llama3.1:8b' ? 'selected' : ''}>Llama 3.1 8B (Ollama)</option>
                    </select>
                </div>

                <div class="py-3 border-b border-dark-border">
                    <label class="block text-white font-medium mb-2 flex items-center">LLM Backend${infoIcon('llm-backend')}</label>
                    <div class="text-sm text-gray-400 mb-2">Which LLM provider to use</div>
                    <select id="setting-backend" onchange="updateSetting('llm_backend', this.value)"
                            class="w-64 px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        <option value="openai" ${settings.llm_backend === 'openai' ? 'selected' : ''}>OpenAI</option>
                        <option value="ollama" ${settings.llm_backend === 'ollama' ? 'selected' : ''}>Ollama (Local)</option>
                    </select>
                </div>

                <div class="py-3">
                    <label class="block text-white font-medium mb-2 flex items-center">Temperature${infoIcon('tool-temperature')}</label>
                    <div class="text-sm text-gray-400 mb-2">LLM temperature for tool calling (0.0 - 1.0)</div>
                    <input type="number" id="setting-temperature" value="${settings.temperature}"
                           onchange="updateSetting('temperature', parseFloat(this.value))"
                           min="0" max="1" step="0.1"
                           class="w-32 px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>
            </div>
        </div>

        <div class="mt-4 p-4 bg-blue-900/20 border border-blue-500/30 rounded-lg">
            <div class="flex items-start gap-3">
                <span class="text-2xl">ℹ️</span>
                <div class="text-sm text-blue-200">
                    <strong>Note:</strong> Changes to these settings take effect immediately for new requests.
                    Existing in-flight requests will use the settings they started with.
                </div>
            </div>
        </div>
    `;
}

async function updateSetting(key, value) {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/settings/1`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ [key]: value })
        });

        if (!response.ok) throw new Error('Failed to update setting');

        showToast('Setting updated successfully', 'success');
    } catch (error) {
        console.error('Error updating setting:', error);
        showToast('Failed to update setting', 'error');
        // Reload to revert UI
        loadToolCallingSettings();
    }
}

// ============================================================================
// TOOL REGISTRY PAGE
// ============================================================================

let allTools = [];

async function loadToolRegistry() {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/public`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load tools');

        allTools = await response.json();
        filterTools();
    } catch (error) {
        console.error('Error loading tool registry:', error);
        showError('tool-registry-container', 'Failed to load tool registry');
    }
}

function filterTools() {
    const category = document.getElementById('tool-category-filter').value;
    const enabledOnly = document.getElementById('enabled-only-filter').checked;
    const guestAllowed = document.getElementById('guest-allowed-filter').checked;

    let filtered = allTools;

    if (category) {
        filtered = filtered.filter(t => t.category === category);
    }
    if (enabledOnly) {
        filtered = filtered.filter(t => t.enabled);
    }
    if (guestAllowed) {
        filtered = filtered.filter(t => t.guest_mode_allowed);
    }

    renderToolRegistry(filtered);
}

function renderToolRegistry(tools) {
    const container = document.getElementById('tool-registry-container');

    if (tools.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">🛠️</div>
                <p>No tools match the current filters</p>
            </div>
        `;
        return;
    }

    container.innerHTML = tools.map(tool => `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6">
            <div class="flex items-start justify-between mb-4">
                <div class="flex-1">
                    <div class="flex items-center gap-3 mb-2 flex-wrap">
                        <h3 class="text-lg font-semibold text-white">${tool.display_name}</h3>
                        <span class="px-2 py-1 text-xs rounded-full ${tool.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400'} flex items-center">
                            ${tool.enabled ? '✓ Enabled' : '✗ Disabled'}${infoIcon('tool-enabled')}
                        </span>
                        ${tool.guest_mode_allowed ? `<span class="px-2 py-1 text-xs rounded-full bg-blue-900/30 text-blue-400 flex items-center">Guest OK${infoIcon('tool-guest-allowed')}</span>` : ''}
                        <span class="px-2 py-1 text-xs rounded-full bg-purple-900/30 text-purple-400 flex items-center">${tool.category.toUpperCase()}${infoIcon('tool-category')}</span>
                    </div>
                    <p class="text-sm text-gray-400 mb-3">${tool.description}</p>
                    <div class="text-xs text-gray-500">
                        <strong>Tool Name:</strong> ${tool.tool_name}<br>
                        <strong>Service URL:</strong> ${tool.service_url || 'N/A'}
                    </div>
                </div>
                <div class="flex gap-2 ml-4">
                    <button onclick="showEditToolModal(${tool.id})"
                            class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors">
                        Edit
                    </button>
                    <button onclick="toggleTool(${tool.id}, ${!tool.enabled})"
                            class="px-3 py-1 ${tool.enabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded text-sm transition-colors">
                        ${tool.enabled ? 'Disable' : 'Enable'}
                    </button>
                </div>
            </div>

            <!-- Web Search Fallback Toggle -->
            <div class="flex items-center justify-between py-3 border-t border-dark-border mt-4">
                <div>
                    <div class="text-sm text-white font-medium flex items-center">
                        Web Search Fallback
                        ${infoIcon('tool-fallback', 'When enabled, if this tool fails, the system will automatically search the web for relevant information instead.')}
                    </div>
                    <div class="text-xs text-gray-400">Fall back to web search when service is unavailable</div>
                </div>
                <label class="relative inline-flex items-center cursor-pointer">
                    <input type="checkbox"
                           ${tool.web_search_fallback_enabled ? 'checked' : ''}
                           onchange="toggleToolFallback(${tool.id}, this.checked)"
                           class="sr-only peer">
                    <div class="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-orange-600"></div>
                </label>
            </div>

            <!-- Required API Keys Section -->
            <div class="py-3 border-t border-dark-border">
                <div class="flex items-center justify-between mb-2">
                    <div class="text-sm text-white font-medium flex items-center">
                        Required API Keys
                        ${infoIcon('tool-api-keys', 'API keys that will be automatically injected when this tool is called.')}
                    </div>
                    <button onclick="showApiKeyRequirementsModal(${tool.id})"
                            class="px-2 py-1 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded text-xs transition-colors">
                        Manage
                    </button>
                </div>
                <div id="tool-${tool.id}-api-keys" class="flex flex-wrap gap-2">
                    ${renderToolApiKeyBadges(tool)}
                </div>
            </div>

            <details class="mt-4">
                <summary class="cursor-pointer text-sm text-blue-400 hover:text-blue-300">
                    View Function Schema
                </summary>
                <pre class="mt-2 p-3 bg-dark-bg border border-dark-border rounded text-xs text-gray-300 overflow-x-auto">${JSON.stringify(tool.function_schema, null, 2)}</pre>
            </details>
        </div>
    `).join('');
}

async function toggleTool(toolId, enabled) {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/${toolId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ enabled })
        });

        if (!response.ok) throw new Error('Failed to toggle tool');

        showToast(`Tool ${enabled ? 'enabled' : 'disabled'} successfully`, 'success');
        loadToolRegistry();
    } catch (error) {
        console.error('Error toggling tool:', error);
        showToast('Failed to update tool', 'error');
    }
}

async function toggleToolFallback(toolId, enabled) {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/${toolId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ web_search_fallback_enabled: enabled })
        });

        if (!response.ok) throw new Error('Failed to toggle fallback');

        showToast(`Web search fallback ${enabled ? 'enabled' : 'disabled'} for tool`, 'success');
    } catch (error) {
        console.error('Error toggling fallback:', error);
        showToast('Failed to update fallback setting', 'error');
        // Reload to revert UI
        loadToolRegistry();
    }
}

function showCreateToolModal() {
    // TODO: Implement tool creation modal
    showToast('Tool creation UI coming soon - use API or seed script', 'info');
}

function showEditToolModal(toolId) {
    const tool = allTools.find(t => t.id === toolId);
    if (!tool) {
        showToast('Tool not found', 'error');
        return;
    }

    // Create modal HTML
    const modalHtml = `
        <div id="edit-tool-modal" class="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto">
                <div class="flex justify-between items-center mb-6">
                    <h2 class="text-xl font-semibold text-white">Edit Tool: ${tool.display_name}</h2>
                    <button onclick="closeEditToolModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button>
                </div>

                <form id="edit-tool-form" class="space-y-4">
                    <input type="hidden" id="edit-tool-id" value="${tool.id}">

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Display Name</label>
                        <input type="text" id="edit-display-name" value="${tool.display_name}"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Description</label>
                        <textarea id="edit-description" rows="2"
                                  class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">${tool.description}</textarea>
                    </div>

                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Timeout (seconds)</label>
                            <input type="number" id="edit-timeout" value="${tool.timeout_seconds}" min="5" max="300"
                                   class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Priority</label>
                            <input type="number" id="edit-priority" value="${tool.priority}" min="1" max="1000"
                                   class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        </div>
                    </div>

                    <div class="space-y-3 pt-2">
                        <div class="flex items-center justify-between py-2 border-b border-dark-border">
                            <div>
                                <div class="text-white font-medium">Enabled</div>
                                <div class="text-xs text-gray-400">Tool is available for use</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" id="edit-enabled" ${tool.enabled ? 'checked' : ''} class="sr-only peer">
                                <div class="w-11 h-6 bg-gray-700 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-green-600"></div>
                            </label>
                        </div>

                        <div class="flex items-center justify-between py-2 border-b border-dark-border">
                            <div>
                                <div class="text-white font-medium">Guest Mode Allowed</div>
                                <div class="text-xs text-gray-400">Tool available for guest users</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" id="edit-guest-mode" ${tool.guest_mode_allowed ? 'checked' : ''} class="sr-only peer">
                                <div class="w-11 h-6 bg-gray-700 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600"></div>
                            </label>
                        </div>

                        <div class="flex items-center justify-between py-2">
                            <div>
                                <div class="text-white font-medium">Web Search Fallback</div>
                                <div class="text-xs text-gray-400">Fall back to web search when service is unavailable</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" id="edit-fallback" ${tool.web_search_fallback_enabled ? 'checked' : ''} class="sr-only peer">
                                <div class="w-11 h-6 bg-gray-700 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-orange-600"></div>
                            </label>
                        </div>
                    </div>

                    <div class="flex justify-end gap-3 pt-4 border-t border-dark-border mt-4">
                        <button type="button" onclick="closeEditToolModal()"
                                class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-colors">
                            Cancel
                        </button>
                        <button type="submit"
                                class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors">
                            Save Changes
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;

    // Add modal to DOM
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Handle form submission
    document.getElementById('edit-tool-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await saveToolEdits();
    });

    // Close on escape key
    document.addEventListener('keydown', handleEditModalEscape);
}

function handleEditModalEscape(e) {
    if (e.key === 'Escape') {
        closeEditToolModal();
    }
}

function closeEditToolModal() {
    const modal = document.getElementById('edit-tool-modal');
    if (modal) {
        modal.remove();
    }
    document.removeEventListener('keydown', handleEditModalEscape);
}

async function saveToolEdits() {
    const toolId = document.getElementById('edit-tool-id').value;

    const updates = {
        display_name: document.getElementById('edit-display-name').value,
        description: document.getElementById('edit-description').value,
        timeout_seconds: parseInt(document.getElementById('edit-timeout').value),
        priority: parseInt(document.getElementById('edit-priority').value),
        enabled: document.getElementById('edit-enabled').checked,
        guest_mode_allowed: document.getElementById('edit-guest-mode').checked,
        web_search_fallback_enabled: document.getElementById('edit-fallback').checked
    };

    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/${toolId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify(updates)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update tool');
        }

        showToast('Tool updated successfully', 'success');
        closeEditToolModal();
        loadToolRegistry();
    } catch (error) {
        console.error('Error saving tool:', error);
        showToast(`Failed to save: ${error.message}`, 'error');
    }
}

// ============================================================================
// TOOL API KEY REQUIREMENTS
// ============================================================================

// Cache for available API keys
let availableApiKeys = [];
let toolApiKeyRequirements = {};

function renderToolApiKeyBadges(tool) {
    // Check if tool has cached required_api_keys (from JSONB field)
    const apiKeys = tool.required_api_keys || [];

    if (apiKeys.length === 0) {
        return '<span class="text-xs text-gray-500">No API keys configured</span>';
    }

    return apiKeys.map(key => `
        <span class="px-2 py-1 text-xs rounded-full bg-yellow-900/30 text-yellow-400 flex items-center gap-1">
            🔑 ${key}
        </span>
    `).join('');
}

async function loadAvailableApiKeys() {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/api-keys/available?enabled_only=false`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load API keys');
        availableApiKeys = await response.json();
    } catch (error) {
        console.error('Error loading available API keys:', error);
        availableApiKeys = [];
    }
}

async function loadToolApiKeyRequirements(toolId) {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/${toolId}/api-keys`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load requirements');
        toolApiKeyRequirements[toolId] = await response.json();
    } catch (error) {
        console.error('Error loading tool API key requirements:', error);
        toolApiKeyRequirements[toolId] = [];
    }
}

async function showApiKeyRequirementsModal(toolId) {
    const tool = allTools.find(t => t.id === toolId);
    if (!tool) {
        showToast('Tool not found', 'error');
        return;
    }

    // Load data
    await Promise.all([
        loadAvailableApiKeys(),
        loadToolApiKeyRequirements(toolId)
    ]);

    const requirements = toolApiKeyRequirements[toolId] || [];

    // Create modal HTML
    const modalHtml = `
        <div id="api-keys-modal" class="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto">
                <div class="flex justify-between items-center mb-6">
                    <h2 class="text-xl font-semibold text-white">API Key Requirements: ${tool.display_name}</h2>
                    <button onclick="closeApiKeyRequirementsModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button>
                </div>

                <div class="mb-6">
                    <p class="text-sm text-gray-400">
                        Configure which API keys are required for this tool. When the tool is called,
                        the orchestrator will automatically inject the specified keys into the request.
                    </p>
                </div>

                <!-- Current Requirements -->
                <div class="mb-6">
                    <h3 class="text-lg font-medium text-white mb-3">Current Requirements</h3>
                    <div id="current-api-requirements" class="space-y-2">
                        ${requirements.length === 0
                            ? '<div class="text-gray-500 text-sm py-4 text-center">No API keys configured for this tool</div>'
                            : requirements.map(req => renderApiKeyRequirement(req)).join('')}
                    </div>
                </div>

                <!-- Add New Requirement -->
                <div class="border-t border-dark-border pt-6">
                    <h3 class="text-lg font-medium text-white mb-3">Add API Key Requirement</h3>
                    <div class="space-y-4">
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">API Key Service</label>
                                <select id="new-api-key-service"
                                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                    <option value="">-- Select API Key --</option>
                                    ${availableApiKeys.map(key => `
                                        <option value="${key.service_name}"
                                                ${requirements.some(r => r.api_key_service === key.service_name) ? 'disabled' : ''}>
                                            ${key.service_name} (${key.api_name})${requirements.some(r => r.api_key_service === key.service_name) ? ' - Already Added' : ''}
                                        </option>
                                    `).join('')}
                                </select>
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Inject As (param name)</label>
                                <input type="text" id="new-inject-as" placeholder="e.g., api_key, google_api_key"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                        </div>
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Description</label>
                                <input type="text" id="new-description" placeholder="Why this key is needed"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                            <div class="flex items-end">
                                <label class="flex items-center gap-2">
                                    <input type="checkbox" id="new-is-required" checked
                                           class="w-4 h-4 rounded border-gray-600 bg-dark-bg text-blue-600">
                                    <span class="text-sm text-gray-300">Required (fail if missing)</span>
                                </label>
                            </div>
                        </div>
                        <div class="flex justify-end">
                            <button onclick="addApiKeyRequirement(${toolId})"
                                    class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg transition-colors">
                                Add Requirement
                            </button>
                        </div>
                    </div>
                </div>

                <div class="flex justify-end gap-3 pt-6 border-t border-dark-border mt-6">
                    <button onclick="closeApiKeyRequirementsModal()"
                            class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-colors">
                        Close
                    </button>
                </div>
            </div>
        </div>
    `;

    // Add modal to DOM
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Close on escape key
    document.addEventListener('keydown', handleApiKeysModalEscape);
}

function renderApiKeyRequirement(req) {
    return `
        <div class="flex items-center justify-between p-3 bg-dark-bg border border-dark-border rounded-lg">
            <div class="flex-1">
                <div class="flex items-center gap-2 mb-1">
                    <span class="text-white font-medium">${req.api_key_service}</span>
                    <span class="px-2 py-0.5 text-xs rounded-full ${req.is_required ? 'bg-red-900/30 text-red-400' : 'bg-gray-700 text-gray-400'}">
                        ${req.is_required ? 'Required' : 'Optional'}
                    </span>
                </div>
                <div class="text-xs text-gray-400">
                    ${req.inject_as ? `Inject as: <code class="bg-gray-800 px-1 rounded">${req.inject_as}</code>` : 'No injection name set'}
                    ${req.description ? ` • ${req.description}` : ''}
                </div>
            </div>
            <button onclick="deleteApiKeyRequirement(${req.id}, ${req.tool_id})"
                    class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors ml-4">
                Remove
            </button>
        </div>
    `;
}

function handleApiKeysModalEscape(e) {
    if (e.key === 'Escape') {
        closeApiKeyRequirementsModal();
    }
}

function closeApiKeyRequirementsModal() {
    const modal = document.getElementById('api-keys-modal');
    if (modal) {
        modal.remove();
    }
    document.removeEventListener('keydown', handleApiKeysModalEscape);
}

async function addApiKeyRequirement(toolId) {
    const apiKeyService = document.getElementById('new-api-key-service').value;
    const injectAs = document.getElementById('new-inject-as').value;
    const description = document.getElementById('new-description').value;
    const isRequired = document.getElementById('new-is-required').checked;

    if (!apiKeyService) {
        showToast('Please select an API key service', 'error');
        return;
    }

    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/${toolId}/api-keys`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                tool_id: toolId,
                api_key_service: apiKeyService,
                is_required: isRequired,
                inject_as: injectAs || null,
                description: description || null
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add requirement');
        }

        showToast('API key requirement added', 'success');

        // Refresh modal
        closeApiKeyRequirementsModal();
        await showApiKeyRequirementsModal(toolId);

        // Refresh tool registry to update badges
        loadToolRegistry();
    } catch (error) {
        console.error('Error adding API key requirement:', error);
        showToast(`Failed to add: ${error.message}`, 'error');
    }
}

async function deleteApiKeyRequirement(requirementId, toolId) {
    if (!confirm('Are you sure you want to remove this API key requirement?')) {
        return;
    }

    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/api-key-requirements/${requirementId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete requirement');
        }

        showToast('API key requirement removed', 'success');

        // Refresh modal
        closeApiKeyRequirementsModal();
        await showApiKeyRequirementsModal(toolId);

        // Refresh tool registry to update badges
        loadToolRegistry();
    } catch (error) {
        console.error('Error deleting API key requirement:', error);
        showToast(`Failed to delete: ${error.message}`, 'error');
    }
}

// ============================================================================
// TRIGGER CONFIGURATION PAGE
// ============================================================================

async function loadTriggers() {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/triggers/public`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load triggers');

        const triggers = await response.json();
        renderTriggers(triggers);
    } catch (error) {
        console.error('Error loading triggers:', error);
        showError('triggers-container', 'Failed to load triggers');
    }
}

function renderTriggers(triggers) {
    const container = document.getElementById('triggers-container');

    // Sort by priority descending
    triggers.sort((a, b) => b.priority - a.priority);

    container.innerHTML = triggers.map(trigger => {
        const config = trigger.config || {};
        return `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex items-start justify-between mb-4">
                    <div class="flex-1">
                        <div class="flex items-center gap-3 mb-2">
                            <h3 class="text-lg font-semibold text-white">${trigger.trigger_name}</h3>
                            <span class="px-2 py-1 text-xs rounded-full bg-purple-900/30 text-purple-400">
                                Priority: ${trigger.priority}
                            </span>
                            <span class="px-2 py-1 text-xs rounded-full ${trigger.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400'}">
                                ${trigger.enabled ? '✓ Active' : '✗ Inactive'}
                            </span>
                        </div>
                        <p class="text-sm text-gray-400 mb-3">${trigger.description}</p>
                        <div class="text-xs text-gray-500">
                            <strong>Type:</strong> ${trigger.trigger_type}
                        </div>
                    </div>
                    <div class="flex gap-2 ml-4">
                        <button onclick="toggleTrigger(${trigger.id}, ${!trigger.enabled})"
                                class="px-3 py-1 ${trigger.enabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded text-sm transition-colors">
                            ${trigger.enabled ? 'Disable' : 'Enable'}
                        </button>
                    </div>
                </div>

                ${renderTriggerConfig(trigger.trigger_type, config)}
            </div>
        `;
    }).join('');
}

function renderTriggerConfig(type, config) {
    switch (type) {
        case 'confidence':
            return `
                <div class="mt-4 p-3 bg-dark-bg border border-dark-border rounded">
                    <div class="text-sm text-gray-400">
                        <strong class="text-white">Threshold:</strong> ${config.threshold || 0.6}
                        <span class="ml-2">(triggers when confidence < ${config.threshold || 0.6})</span>
                    </div>
                </div>
            `;
        case 'intent':
            return `
                <div class="mt-4 p-3 bg-dark-bg border border-dark-border rounded">
                    <div class="text-sm text-gray-400">
                        <strong class="text-white">Target Intents:</strong> ${(config.intents || []).join(', ')}
                    </div>
                </div>
            `;
        case 'keywords':
            return `
                <div class="mt-4 p-3 bg-dark-bg border border-dark-border rounded">
                    <div class="text-sm text-gray-400">
                        <strong class="text-white">Keywords:</strong> ${(config.keywords || []).join(', ')}<br>
                        <strong class="text-white">Min Keywords:</strong> ${config.min_keywords || 1}
                    </div>
                </div>
            `;
        case 'empty_rag':
            return `
                <div class="mt-4 p-3 bg-dark-bg border border-dark-border rounded">
                    <div class="text-sm text-gray-400">
                        <strong class="text-white">Checks:</strong>
                        ${config.check_null ? '✓ Null data' : ''}
                        ${config.check_empty ? '✓ Empty data' : ''}
                    </div>
                </div>
            `;
        case 'validation':
            return `
                <div class="mt-4 p-3 bg-dark-bg border border-dark-border rounded">
                    <div class="text-sm text-gray-400">
                        <strong class="text-white">Validation Node:</strong> ${config.check_validation_node ? 'Enabled' : 'Disabled'}
                    </div>
                </div>
            `;
        default:
            return '';
    }
}

async function toggleTrigger(triggerId, enabled) {
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/triggers/${triggerId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ enabled })
        });

        if (!response.ok) throw new Error('Failed to toggle trigger');

        showToast(`Trigger ${enabled ? 'enabled' : 'disabled'} successfully`, 'success');
        loadTriggers();
    } catch (error) {
        console.error('Error toggling trigger:', error);
        showToast('Failed to update trigger', 'error');
    }
}

// ============================================================================
// TOOL METRICS PAGE
// ============================================================================

async function loadToolMetrics() {
    try {
        const timeframe = document.getElementById('metrics-timeframe').value;
        // Convert timeframe to hours_ago
        const hoursMap = { '1h': 1, '24h': 24, '7d': 168, '30d': 720 };
        const hoursAgo = hoursMap[timeframe] || 24;

        const response = await fetch(`${TOOL_CALLING_API_BASE}/metrics/aggregated?hours_ago=${hoursAgo}`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load metrics');

        const aggregatedMetrics = await response.json();

        // Transform aggregated metrics to expected format
        const totalCalls = aggregatedMetrics.reduce((sum, m) => sum + m.total_calls, 0);
        const successfulCalls = aggregatedMetrics.reduce((sum, m) => sum + m.success_count, 0);
        const avgLatency = aggregatedMetrics.length > 0
            ? aggregatedMetrics.reduce((sum, m) => sum + (m.avg_latency_ms * m.total_calls), 0) / totalCalls
            : 0;
        const mostUsed = aggregatedMetrics.length > 0
            ? aggregatedMetrics.reduce((max, m) => m.total_calls > max.total_calls ? m : max, aggregatedMetrics[0])
            : null;

        const metrics = {
            total_calls: totalCalls,
            successful_calls: successfulCalls,
            avg_latency_ms: avgLatency,
            most_used_tool: mostUsed?.tool_name || 'N/A',
            by_tool: aggregatedMetrics.map(m => ({
                tool_name: m.tool_name,
                call_count: m.total_calls,
                success_rate: m.success_rate,
                avg_latency_ms: m.avg_latency_ms,
                last_called: m.last_called
            }))
        };

        renderToolMetrics(metrics);
    } catch (error) {
        console.error('Error loading metrics:', error);
        showError('tool-metrics-container', 'Failed to load metrics');
    }
}

function renderToolMetrics(metrics) {
    // Update summary stats
    document.getElementById('metric-total-calls').textContent = metrics.total_calls || '0';
    document.getElementById('metric-success-rate').textContent =
        metrics.total_calls > 0 ? `${((metrics.successful_calls / metrics.total_calls) * 100).toFixed(1)}%` : 'N/A';
    document.getElementById('metric-avg-latency').textContent =
        metrics.avg_latency_ms ? `${metrics.avg_latency_ms.toFixed(0)}ms` : 'N/A';
    document.getElementById('metric-top-tool').textContent = metrics.most_used_tool || 'N/A';

    const container = document.getElementById('tool-metrics-container');

    if (!metrics.by_tool || metrics.by_tool.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">📊</div>
                <p>No metrics data available for the selected timeframe</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg overflow-hidden">
            <table class="data-table">
                <thead>
                    <tr>
                        <th class="text-left px-4 py-3">Tool Name</th>
                        <th class="text-right px-4 py-3">Calls</th>
                        <th class="text-right px-4 py-3">Success Rate</th>
                        <th class="text-right px-4 py-3">Avg Latency</th>
                        <th class="text-right px-4 py-3">Last Used</th>
                    </tr>
                </thead>
                <tbody>
                    ${metrics.by_tool.map(tool => `
                        <tr>
                            <td class="px-4 py-3 text-white font-medium">${tool.tool_name}</td>
                            <td class="px-4 py-3 text-right text-gray-300">${tool.call_count}</td>
                            <td class="px-4 py-3 text-right">
                                <span class="${tool.success_rate >= 90 ? 'text-green-400' : tool.success_rate >= 70 ? 'text-yellow-400' : 'text-red-400'}">
                                    ${tool.success_rate.toFixed(1)}%
                                </span>
                            </td>
                            <td class="px-4 py-3 text-right text-gray-300">${tool.avg_latency_ms.toFixed(0)}ms</td>
                            <td class="px-4 py-3 text-right text-gray-400 text-sm">${new Date(tool.last_called).toLocaleString()}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

// ============================================================================
// TOOL TESTING PAGE
// ============================================================================

async function initToolTesting() {
    // Load tools for selection
    try {
        const response = await fetch(`${TOOL_CALLING_API_BASE}/tools/public?enabled_only=true`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!response.ok) throw new Error('Failed to load tools');

        const tools = await response.json();
        const select = document.getElementById('test-tool-select');

        select.innerHTML = '<option value="">-- Select a tool --</option>' +
            tools.map(tool => `
                <option value="${tool.tool_name}" data-schema='${JSON.stringify(tool.function_schema)}'>
                    ${tool.display_name} (${tool.tool_name})
                </option>
            `).join('');

        // Auto-fill example args when tool is selected
        select.addEventListener('change', function() {
            if (this.value) {
                const schema = JSON.parse(this.selectedOptions[0].dataset.schema);
                const exampleArgs = generateExampleArgs(schema);
                document.getElementById('test-tool-args').value = JSON.stringify(exampleArgs, null, 2);
            } else {
                document.getElementById('test-tool-args').value = '';
            }
        });
    } catch (error) {
        console.error('Error loading tools for testing:', error);
        showToast('Failed to load tools', 'error');
    }
}

function generateExampleArgs(schema) {
    const params = schema.function?.parameters?.properties || {};
    const example = {};

    for (const [key, prop] of Object.entries(params)) {
        if (prop.type === 'string') {
            example[key] = prop.default || prop.description || 'example';
        } else if (prop.type === 'number' || prop.type === 'integer') {
            example[key] = prop.default || 1;
        } else if (prop.type === 'boolean') {
            example[key] = prop.default !== undefined ? prop.default : true;
        }
    }

    return example;
}

async function testTool() {
    const toolName = document.getElementById('test-tool-select').value;
    const argsText = document.getElementById('test-tool-args').value;
    const guestMode = document.getElementById('test-guest-mode').checked;

    if (!toolName) {
        showToast('Please select a tool', 'error');
        return;
    }

    let args;
    try {
        args = JSON.parse(argsText);
    } catch (error) {
        showToast('Invalid JSON in tool arguments', 'error');
        return;
    }

    const resultsContainer = document.getElementById('test-results-container');
    resultsContainer.innerHTML = `
        <div class="text-center text-gray-400 py-8">
            <div class="text-4xl mb-2">⏳</div>
            <p>Running test...</p>
        </div>
    `;

    try {
        const startTime = Date.now();

        // Call the tool via the orchestrator's tool execution endpoint
        const response = await fetch('/test-tool', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                tool_name: toolName,
                arguments: args,
                guest_mode: guestMode
            })
        });

        const latency = Date.now() - startTime;
        const result = await response.json();

        renderTestResults(result, latency, response.ok);
        addToTestHistory(toolName, args, result, latency, response.ok);
    } catch (error) {
        console.error('Error testing tool:', error);
        resultsContainer.innerHTML = `
            <div class="p-4 bg-red-900/20 border border-red-500/30 rounded-lg">
                <div class="flex items-start gap-3">
                    <span class="text-2xl">❌</span>
                    <div>
                        <div class="text-red-400 font-semibold">Test Failed</div>
                        <div class="text-sm text-red-300 mt-1">${error.message}</div>
                    </div>
                </div>
            </div>
        `;
    }
}

function renderTestResults(result, latency, success) {
    const container = document.getElementById('test-results-container');

    container.innerHTML = `
        <div class="space-y-4">
            <div class="p-4 ${success ? 'bg-green-900/20 border-green-500/30' : 'bg-red-900/20 border-red-500/30'} border rounded-lg">
                <div class="flex items-center gap-3 mb-2">
                    <span class="text-2xl">${success ? '✅' : '❌'}</span>
                    <div class="text-lg font-semibold ${success ? 'text-green-400' : 'text-red-400'}">
                        ${success ? 'Test Successful' : 'Test Failed'}
                    </div>
                </div>
                <div class="text-sm ${success ? 'text-green-300' : 'text-red-300'}">
                    Latency: ${latency}ms
                </div>
            </div>

            <div>
                <div class="text-sm font-medium text-gray-400 mb-2">Response:</div>
                <pre class="p-4 bg-dark-bg border border-dark-border rounded-lg text-xs text-gray-300 overflow-x-auto max-h-96">${JSON.stringify(result, null, 2)}</pre>
            </div>
        </div>
    `;
}

let testHistory = [];

function addToTestHistory(toolName, args, result, latency, success) {
    testHistory.unshift({
        toolName,
        args,
        result,
        latency,
        success,
        timestamp: new Date()
    });

    // Keep only last 10
    testHistory = testHistory.slice(0, 10);

    renderTestHistory();
}

function renderTestHistory() {
    const container = document.getElementById('test-history-container');

    if (testHistory.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <p>No recent tests</p>
            </div>
        `;
        return;
    }

    container.innerHTML = testHistory.map((test, idx) => `
        <div class="flex items-center justify-between py-3 ${idx > 0 ? 'border-t border-dark-border' : ''}">
            <div class="flex-1">
                <div class="flex items-center gap-2">
                    <span class="text-lg">${test.success ? '✅' : '❌'}</span>
                    <span class="text-white font-medium">${test.toolName}</span>
                    <span class="text-xs text-gray-500">${test.latency}ms</span>
                </div>
                <div class="text-xs text-gray-400 mt-1">${test.timestamp.toLocaleTimeString()}</div>
            </div>
            <button onclick="rerunTest(${idx})" class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors">
                Rerun
            </button>
        </div>
    `).join('');
}

function rerunTest(index) {
    const test = testHistory[index];
    document.getElementById('test-tool-select').value = test.toolName;
    document.getElementById('test-tool-args').value = JSON.stringify(test.args, null, 2);
    testTool();
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function showError(containerId, message) {
    const container = document.getElementById(containerId);
    container.innerHTML = `
        <div class="p-4 bg-red-900/20 border border-red-500/30 rounded-lg">
            <div class="flex items-start gap-3">
                <span class="text-2xl">❌</span>
                <div>
                    <div class="text-red-400 font-semibold">Error</div>
                    <div class="text-sm text-red-300 mt-1">${message}</div>
                </div>
            </div>
        </div>
    `;
}

function showToast(message, type = 'info') {
    // Simple toast notification (can be enhanced)
    const colors = {
        success: 'bg-green-600',
        error: 'bg-red-600',
        info: 'bg-blue-600'
    };

    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 ${colors[type]} text-white px-6 py-3 rounded-lg shadow-lg z-50 transition-opacity`;
    toast.textContent = message;

    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================================================
// INITIALIZATION
// ============================================================================

// Auto-load when tabs are shown
if (typeof window.tabChangeCallbacks === 'undefined') {
    window.tabChangeCallbacks = {};
}

window.tabChangeCallbacks['tool-calling-settings'] = loadToolCallingSettings;
window.tabChangeCallbacks['tool-registry'] = loadToolRegistry;
window.tabChangeCallbacks['trigger-config'] = loadTriggers;
window.tabChangeCallbacks['tool-metrics'] = loadToolMetrics;
window.tabChangeCallbacks['tool-testing'] = initToolTesting;
