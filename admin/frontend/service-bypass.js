/**
 * RAG Service Bypass Configuration
 *
 * Allows configuring which RAG services should bypass to cloud LLMs
 * instead of using dedicated local RAG services.
 */

let bypassConfigs = [];

async function initServiceBypassPage() {
    await loadBypassConfigs();
    renderServiceBypass();
}

async function loadBypassConfigs() {
    try {
        const response = await fetch('/api/rag-service-bypass', {
            headers: getAuthHeaders()
        });
        if (response.ok) {
            bypassConfigs = await response.json();
        }
    } catch (error) {
        console.error('Failed to load bypass configs:', error);
    }
}

function renderServiceBypass() {
    const container = document.getElementById('service-bypass-container');
    if (!container) return;

    const enabledCount = bypassConfigs.filter(c => c.bypass_enabled).length;

    let html = `
        <!-- Header -->
        <div class="flex justify-between items-center mb-6">
            <div>
                <h2 class="text-2xl font-bold text-white">Service Bypass Configuration</h2>
                <p class="text-gray-400 mt-1">Route specific services to cloud LLMs instead of local APIs</p>
            </div>
            <span class="px-3 py-1 rounded-full text-sm ${enabledCount > 0 ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">
                ${enabledCount} service${enabledCount !== 1 ? 's' : ''} bypassed
            </span>
        </div>

        <!-- Info Box -->
        <div class="bg-blue-500/10 border border-blue-500/30 rounded-xl p-4 mb-6">
            <div class="flex items-start gap-3">
                <span class="text-blue-400 text-xl">💡</span>
                <div class="text-sm text-blue-300">
                    <p class="font-medium mb-2">When to use service bypass:</p>
                    <ul class="space-y-1 text-blue-300/80">
                        <li>• <strong>Recipes:</strong> Cloud LLMs have vast recipe knowledge and can adapt/modify on the fly</li>
                        <li>• <strong>Web Search:</strong> Cloud LLMs with web access are optimized for search synthesis</li>
                        <li>• <strong>Keep Local:</strong> Weather, stocks, sports, flights - need real-time API data</li>
                    </ul>
                </div>
            </div>
        </div>

        <!-- Service Cards -->
        <div class="grid gap-4">
            ${bypassConfigs.length > 0 ? bypassConfigs.map(config => renderBypassCard(config)).join('') : `
                <div class="bg-dark-card border border-dark-border rounded-xl p-8 text-center">
                    <span class="text-4xl mb-4 block">🔀</span>
                    <p class="text-gray-400">No bypass configurations found</p>
                    <p class="text-sm text-gray-500 mt-2">Run the migration to seed initial configurations</p>
                </div>
            `}
        </div>
    `;

    container.innerHTML = html;
}

function renderBypassCard(config) {
    const isEnabled = config.bypass_enabled;

    return `
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <div class="p-4 flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-12 h-12 rounded-lg ${isEnabled ? 'bg-purple-500/20' : 'bg-gray-500/20'} flex items-center justify-center">
                        <span class="text-2xl">${getServiceIcon(config.service_name)}</span>
                    </div>
                    <div>
                        <h3 class="text-lg font-semibold text-white">${config.display_name || config.service_name}</h3>
                        <p class="text-sm text-gray-400 max-w-lg">${config.description || ''}</p>
                    </div>
                </div>

                <div class="flex items-center gap-4">
                    ${config.cloud_provider ? `
                        <span class="text-sm text-gray-400">
                            → ${config.cloud_provider}${config.cloud_model ? ` (${config.cloud_model})` : ''}
                        </span>
                    ` : `
                        <span class="text-sm text-gray-500">Any cloud provider</span>
                    `}

                    <button onclick="toggleBypass('${config.service_name}')"
                            class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${isEnabled ? 'bg-purple-600' : 'bg-gray-600'}">
                        <span class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${isEnabled ? 'translate-x-6' : 'translate-x-1'}"></span>
                    </button>

                    <button onclick="editBypassConfig('${config.service_name}')"
                            class="p-2 hover:bg-gray-700 rounded-lg transition-colors"
                            title="Configure">
                        ⚙️
                    </button>
                </div>
            </div>

            ${isEnabled && config.system_prompt ? `
                <div class="px-4 pb-4">
                    <details class="text-sm">
                        <summary class="text-gray-400 cursor-pointer hover:text-gray-300">View system prompt</summary>
                        <pre class="mt-2 p-3 bg-dark-bg rounded-lg text-gray-300 whitespace-pre-wrap text-xs">${escapeHtmlBypass(config.system_prompt)}</pre>
                    </details>
                </div>
            ` : ''}
        </div>
    `;
}

function getServiceIcon(serviceName) {
    const icons = {
        'recipes': '👨‍🍳',
        'websearch': '🔍',
        'news': '📰',
        'streaming': '🎬',
        'weather': '🌤️',
        'stocks': '📈',
        'sports': '⚽',
        'flights': '✈️',
        'dining': '🍽️',
        'airports': '🛫',
        'directions': '🗺️',
        'events': '🎭',
    };
    return icons[serviceName] || '🔧';
}

async function toggleBypass(serviceName) {
    try {
        const response = await fetch(`/api/rag-service-bypass/${serviceName}/toggle`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            const result = await response.json();
            if (typeof safeShowToast === 'function') {
                safeShowToast(result.message, 'success');
            }
            await loadBypassConfigs();
            renderServiceBypass();
        } else {
            throw new Error('Toggle failed');
        }
    } catch (error) {
        if (typeof safeShowToast === 'function') {
            safeShowToast('Failed to toggle bypass', 'error');
        }
    }
}

function editBypassConfig(serviceName) {
    const config = bypassConfigs.find(c => c.service_name === serviceName);
    if (!config) return;

    const html = `
        <div id="bypass-config-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
             onclick="if(event.target.id === 'bypass-config-modal') closeBypassModal()">
            <div class="bg-dark-card border border-dark-border rounded-xl w-full max-w-2xl m-4 max-h-[90vh] overflow-y-auto">
                <div class="p-6 border-b border-dark-border">
                    <h3 class="text-xl font-semibold text-white">Configure ${config.display_name || serviceName}</h3>
                    <p class="text-sm text-gray-400 mt-1">Customize how this service uses cloud LLMs</p>
                </div>

                <form id="bypass-config-form" onsubmit="saveBypassConfig(event, '${serviceName}')" class="p-6 space-y-6">
                    <!-- Cloud Provider -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Cloud Provider</label>
                        <select name="cloud_provider" class="w-full px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            <option value="">Any available</option>
                            <option value="openai" ${config.cloud_provider === 'openai' ? 'selected' : ''}>OpenAI</option>
                            <option value="anthropic" ${config.cloud_provider === 'anthropic' ? 'selected' : ''}>Anthropic</option>
                            <option value="google" ${config.cloud_provider === 'google' ? 'selected' : ''}>Google</option>
                        </select>
                    </div>

                    <!-- Cloud Model -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Model (optional)</label>
                        <input type="text" name="cloud_model" value="${config.cloud_model || ''}"
                               placeholder="e.g., gpt-4o, claude-sonnet"
                               class="w-full px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        <p class="mt-1 text-xs text-gray-500">Leave empty to use provider's default model</p>
                    </div>

                    <!-- System Prompt -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">System Prompt</label>
                        <textarea name="system_prompt" rows="8"
                                  class="w-full px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-white font-mono text-sm"
                        >${escapeHtmlBypass(config.system_prompt || '')}</textarea>
                        <p class="mt-1 text-xs text-gray-500">Instructions for the cloud LLM when handling this service</p>
                    </div>

                    <!-- Temperature & Max Tokens -->
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-2">Temperature</label>
                            <input type="number" name="temperature" value="${config.temperature || 0.7}"
                                   min="0" max="2" step="0.1"
                                   class="w-full px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-2">Max Tokens</label>
                            <input type="number" name="max_tokens" value="${config.max_tokens || 1024}"
                                   min="100" max="4096"
                                   class="w-full px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        </div>
                    </div>

                    <!-- Actions -->
                    <div class="flex justify-end gap-3 pt-4">
                        <button type="button" onclick="closeBypassModal()"
                                class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg">
                            Cancel
                        </button>
                        <button type="submit"
                                class="px-6 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg font-medium">
                            Save Configuration
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', html);
}

function closeBypassModal() {
    document.getElementById('bypass-config-modal')?.remove();
}

async function saveBypassConfig(event, serviceName) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);

    const configData = {
        cloud_provider: formData.get('cloud_provider') || null,
        cloud_model: formData.get('cloud_model') || null,
        system_prompt: formData.get('system_prompt'),
        temperature: parseFloat(formData.get('temperature')),
        max_tokens: parseInt(formData.get('max_tokens')),
    };

    try {
        const response = await fetch(`/api/rag-service-bypass/${serviceName}`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(configData)
        });

        if (response.ok) {
            closeBypassModal();
            if (typeof safeShowToast === 'function') {
                safeShowToast('Configuration saved', 'success');
            }
            await loadBypassConfigs();
            renderServiceBypass();
        } else {
            throw new Error('Save failed');
        }
    } catch (error) {
        if (typeof safeShowToast === 'function') {
            safeShowToast('Failed to save configuration', 'error');
        }
    }
}

function escapeHtmlBypass(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Register tab callback
window.tabChangeCallbacks = window.tabChangeCallbacks || {};
window.tabChangeCallbacks['service-bypass'] = initServiceBypassPage;

// Expose functions globally
window.initServiceBypassPage = initServiceBypassPage;
window.toggleBypass = toggleBypass;
window.editBypassConfig = editBypassConfig;
window.closeBypassModal = closeBypassModal;
window.saveBypassConfig = saveBypassConfig;
