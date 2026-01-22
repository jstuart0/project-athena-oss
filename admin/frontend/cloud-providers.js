/**
 * Cloud LLM Providers Management
 * Handles configuration and usage tracking for OpenAI, Anthropic, and Google cloud providers
 */

// Provider configurations
const CLOUD_PROVIDERS = {
    openai: {
        name: 'OpenAI',
        icon: '🤖',
        color: 'emerald',
        models: [
            'gpt-5',
            'gpt-4.5-preview',
            'gpt-4o',
            'gpt-4o-mini',
            'o3',
            'o3-mini',
            'o1',
            'o1-mini',
            'o1-preview',
            'gpt-4-turbo',
            'gpt-4',
            'gpt-3.5-turbo'
        ],
        docUrl: 'https://platform.openai.com/docs'
    },
    anthropic: {
        name: 'Anthropic',
        icon: '🧠',
        color: 'orange',
        models: [
            'claude-sonnet-4-20250514',
            'claude-3-5-sonnet-20241022',
            'claude-3-5-haiku-20241022',
            'claude-3-opus-20240229',
            'claude-3-sonnet-20240229',
            'claude-3-haiku-20240307'
        ],
        docUrl: 'https://docs.anthropic.com'
    },
    google: {
        name: 'Google',
        icon: '🔮',
        color: 'blue',
        models: [
            'gemini-2.0-flash',
            'gemini-2.0-flash-exp',
            'gemini-1.5-pro',
            'gemini-1.5-flash',
            'gemini-1.5-flash-8b'
        ],
        docUrl: 'https://ai.google.dev/docs'
    }
};

// Color classes for providers
const PROVIDER_COLORS = {
    emerald: {
        bg: 'bg-emerald-500/20',
        border: 'border-emerald-500/30',
        text: 'text-emerald-400',
        button: 'bg-emerald-600 hover:bg-emerald-700'
    },
    orange: {
        bg: 'bg-orange-500/20',
        border: 'border-orange-500/30',
        text: 'text-orange-400',
        button: 'bg-orange-600 hover:bg-orange-700'
    },
    blue: {
        bg: 'bg-blue-500/20',
        border: 'border-blue-500/30',
        text: 'text-blue-400',
        button: 'bg-blue-600 hover:bg-blue-700'
    }
};

// Load cloud providers on tab activation
function loadCloudProviders() {
    loadProviderCards();
    loadUsageSummary();
    loadRecentUsage();
    loadCostChart();
}

// Load provider configuration cards
async function loadProviderCards(noCache = false) {
    const container = document.getElementById('cloud-providers-container');
    container.innerHTML = '<div class="col-span-3 text-center text-gray-400 py-8">Loading providers...</div>';

    try {
        const fetchOptions = {
            headers: getAuthHeaders()
        };
        // Force fresh fetch when noCache is true (after saving API key)
        if (noCache) {
            fetchOptions.cache = 'no-store';
            fetchOptions.headers['Cache-Control'] = 'no-cache';
        }
        const response = await fetch('/api/cloud-providers', fetchOptions);

        if (!response.ok) throw new Error('Failed to fetch providers');

        const providers = await response.json();
        const providerMap = {};
        providers.forEach(p => providerMap[p.provider] = p);

        let html = '';
        for (const [key, config] of Object.entries(CLOUD_PROVIDERS)) {
            const provider = providerMap[key];
            const colors = PROVIDER_COLORS[config.color];
            const isConfigured = provider?.enabled && provider?.has_api_key;

            html += `
                <div class="bg-dark-card border ${colors.border} rounded-lg p-6 relative">
                    <!-- Status Badge -->
                    <div class="absolute top-4 right-4">
                        ${isConfigured
                            ? '<span class="px-2 py-1 bg-green-500/20 text-green-400 text-xs rounded-full">Active</span>'
                            : '<span class="px-2 py-1 bg-gray-500/20 text-gray-400 text-xs rounded-full">Not Configured</span>'
                        }
                    </div>

                    <!-- Provider Header -->
                    <div class="flex items-center gap-3 mb-4">
                        <div class="${colors.bg} p-3 rounded-lg">
                            <span class="text-2xl">${config.icon}</span>
                        </div>
                        <div>
                            <h3 class="text-lg font-semibold text-white">${config.name}</h3>
                            <a href="${config.docUrl}" target="_blank" class="text-xs ${colors.text} hover:underline">
                                Documentation →
                            </a>
                        </div>
                    </div>

                    <!-- API Key Status -->
                    <div class="mb-4">
                        <label class="text-sm text-gray-400 block mb-2">API Key</label>
                        <div class="flex items-center gap-2">
                            ${provider?.has_api_key
                                ? `<span class="text-green-400 text-sm">••••••••${provider.api_key_masked || '****'}</span>`
                                : '<span class="text-gray-500 text-sm">Not set</span>'
                            }
                            <button onclick="openApiKeyModal('${key}')"
                                    class="ml-auto px-3 py-1 ${colors.button} text-white rounded text-xs">
                                ${provider?.has_api_key ? 'Update' : 'Configure'}
                            </button>
                        </div>
                    </div>

                    <!-- Default Model -->
                    <div class="mb-4">
                        <label class="text-sm text-gray-400 block mb-2">Default Model</label>
                        <select id="default-model-${key}"
                                onchange="updateDefaultModel('${key}')"
                                class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white text-sm"
                                ${!isConfigured ? 'disabled' : ''}>
                            ${config.models.map(m =>
                                `<option value="${m}" ${provider?.default_model === m ? 'selected' : ''}>${m}</option>`
                            ).join('')}
                        </select>
                    </div>

                    <!-- Rate Limits -->
                    <div class="grid grid-cols-2 gap-3 mb-4">
                        <div>
                            <label class="text-xs text-gray-400 block mb-1">RPM Limit</label>
                            <input type="number" id="rpm-${key}"
                                   value="${provider?.rate_limit_rpm || 60}"
                                   onchange="updateRateLimits('${key}')"
                                   class="w-full bg-dark-bg border border-dark-border rounded px-2 py-1 text-white text-sm"
                                   ${!isConfigured ? 'disabled' : ''}>
                        </div>
                        <div>
                            <label class="text-xs text-gray-400 block mb-1">TPM Limit</label>
                            <input type="number" id="tpm-${key}"
                                   value="${provider?.rate_limit_tpm || 100000}"
                                   onchange="updateRateLimits('${key}')"
                                   class="w-full bg-dark-bg border border-dark-border rounded px-2 py-1 text-white text-sm"
                                   ${!isConfigured ? 'disabled' : ''}>
                        </div>
                    </div>

                    <!-- Health Check -->
                    <div class="flex items-center justify-between pt-3 border-t border-dark-border">
                        <div id="health-${key}" class="text-sm text-gray-400">
                            ${isConfigured ? 'Checking...' : 'Not available'}
                        </div>
                        ${isConfigured ? `
                            <button onclick="checkProviderHealth('${key}')"
                                    class="text-xs ${colors.text} hover:underline">
                                Test Connection
                            </button>
                        ` : ''}
                    </div>
                </div>
            `;
        }

        container.innerHTML = html;

        // Check health for configured providers
        for (const [key, config] of Object.entries(CLOUD_PROVIDERS)) {
            const provider = providerMap[key];
            if (provider?.enabled && provider?.has_api_key) {
                checkProviderHealth(key);
            }
        }

    } catch (error) {
        console.error('Error loading providers:', error);
        container.innerHTML = `
            <div class="col-span-3 text-center text-red-400 py-8">
                Failed to load providers. Please try again.
            </div>
        `;
    }
}

// Open API key configuration modal
function openApiKeyModal(provider) {
    const config = CLOUD_PROVIDERS[provider];
    const colors = PROVIDER_COLORS[config.color];

    const modal = document.createElement('div');
    modal.id = 'api-key-modal';
    modal.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md mx-4">
            <div class="flex items-center gap-3 mb-6">
                <div class="${colors.bg} p-2 rounded-lg">
                    <span class="text-xl">${config.icon}</span>
                </div>
                <h3 class="text-lg font-semibold text-white">Configure ${config.name} API Key</h3>
            </div>

            <form onsubmit="saveApiKey(event, '${provider}')">
                <div class="mb-4">
                    <label class="text-sm text-gray-400 block mb-2">API Key</label>
                    <input type="password" id="api-key-input"
                           placeholder="sk-... or similar"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-4 py-3 text-white"
                           required>
                    <p class="text-xs text-gray-500 mt-2">
                        Get your API key from
                        <a href="${config.docUrl}" target="_blank" class="${colors.text} hover:underline">
                            ${config.name} Dashboard
                        </a>
                    </p>
                </div>

                <div class="mb-4">
                    <label class="text-sm text-gray-400 block mb-2">Organization ID (optional)</label>
                    <input type="text" id="org-id-input"
                           placeholder="org-..."
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-4 py-3 text-white">
                </div>

                <div class="flex justify-end gap-3">
                    <button type="button" onclick="closeApiKeyModal()"
                            class="px-4 py-2 border border-dark-border text-gray-400 rounded-lg hover:bg-dark-bg">
                        Cancel
                    </button>
                    <button type="submit" class="px-4 py-2 ${colors.button} text-white rounded-lg">
                        Save & Verify
                    </button>
                </div>
            </form>
        </div>
    `;

    document.body.appendChild(modal);
    document.getElementById('api-key-input').focus();
}

// Close API key modal
function closeApiKeyModal() {
    const modal = document.getElementById('api-key-modal');
    if (modal) modal.remove();
}

// Save API key
async function saveApiKey(event, provider) {
    event.preventDefault();

    const apiKey = document.getElementById('api-key-input').value;
    const orgId = document.getElementById('org-id-input').value;

    try {
        const response = await fetch(`/api/cloud-providers/${provider}/setup`, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                api_key: apiKey,
                organization_id: orgId || null
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save API key');
        }

        const result = await response.json();

        closeApiKeyModal();
        showNotification(`${CLOUD_PROVIDERS[provider].name} configured successfully!`, 'success');

        // Force reload cards without cache
        await loadProviderCards(true);

    } catch (error) {
        console.error('Error saving API key:', error);
        showNotification(error.message, 'error');
    }
}

// Update default model
async function updateDefaultModel(provider) {
    const model = document.getElementById(`default-model-${provider}`).value;

    try {
        const response = await fetch(`/api/cloud-providers/${provider}`, {
            method: 'PATCH',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ default_model: model })
        });

        if (!response.ok) throw new Error('Failed to update model');

        showNotification('Default model updated', 'success');

    } catch (error) {
        console.error('Error updating model:', error);
        showNotification('Failed to update model', 'error');
    }
}

// Update rate limits
async function updateRateLimits(provider) {
    const rpm = parseInt(document.getElementById(`rpm-${provider}`).value);
    const tpm = parseInt(document.getElementById(`tpm-${provider}`).value);

    try {
        const response = await fetch(`/api/cloud-providers/${provider}`, {
            method: 'PATCH',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                rate_limit_rpm: rpm,
                rate_limit_tpm: tpm
            })
        });

        if (!response.ok) throw new Error('Failed to update rate limits');

        showNotification('Rate limits updated', 'success');

    } catch (error) {
        console.error('Error updating rate limits:', error);
        showNotification('Failed to update rate limits', 'error');
    }
}

// Check provider health
async function checkProviderHealth(provider) {
    const healthEl = document.getElementById(`health-${provider}`);
    healthEl.innerHTML = '<span class="text-yellow-400">Checking...</span>';

    try {
        const response = await fetch(`/api/cloud-providers/${provider}/health`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Health check failed');

        const result = await response.json();

        if (result.healthy) {
            healthEl.innerHTML = `
                <span class="text-green-400">Connected</span>
                <span class="text-gray-500 text-xs ml-2">${result.latency_ms}ms</span>
            `;
        } else {
            healthEl.innerHTML = `<span class="text-red-400">${result.error || 'Connection failed'}</span>`;
        }

    } catch (error) {
        healthEl.innerHTML = '<span class="text-red-400">Error checking health</span>';
    }
}

// Load usage summary
async function loadUsageSummary() {
    const container = document.getElementById('usage-summary-container');
    const period = document.getElementById('usage-period').value;

    try {
        const response = await fetch(`/api/cloud-llm-usage/summary/${period}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to fetch usage');

        const data = await response.json();

        container.innerHTML = `
            <div class="bg-dark-bg rounded-lg p-4">
                <div class="text-gray-400 text-sm mb-1">Total Requests</div>
                <div class="text-2xl font-semibold text-white">${formatNumber(data.total_requests || 0)}</div>
            </div>
            <div class="bg-dark-bg rounded-lg p-4">
                <div class="text-gray-400 text-sm mb-1">Total Tokens</div>
                <div class="text-2xl font-semibold text-white">${formatNumber(data.total_tokens || 0)}</div>
            </div>
            <div class="bg-dark-bg rounded-lg p-4">
                <div class="text-gray-400 text-sm mb-1">Total Cost</div>
                <div class="text-2xl font-semibold text-emerald-400">$${(data.total_cost_usd || 0).toFixed(4)}</div>
            </div>
            <div class="bg-dark-bg rounded-lg p-4">
                <div class="text-gray-400 text-sm mb-1">Avg Latency</div>
                <div class="text-2xl font-semibold text-white">${Math.round(data.avg_latency_ms || 0)}ms</div>
            </div>
        `;

    } catch (error) {
        console.error('Error loading usage summary:', error);
        container.innerHTML = '<div class="col-span-4 text-center text-gray-400">No usage data available</div>';
    }
}

// Load recent usage table
async function loadRecentUsage() {
    const tbody = document.getElementById('recent-usage-table');

    try {
        const response = await fetch('/api/cloud-llm-usage/recent?limit=10', {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to fetch recent usage');

        const data = await response.json();

        if (!data.length) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" class="text-center text-gray-400 py-8">No usage data yet</td>
                </tr>
            `;
            return;
        }

        tbody.innerHTML = data.map(row => `
            <tr class="border-b border-dark-border/50">
                <td class="py-3 text-gray-400">${formatTime(row.created_at)}</td>
                <td class="py-3">
                    <span class="px-2 py-1 ${getProviderBadgeClass(row.provider)} rounded text-xs">
                        ${row.provider}
                    </span>
                </td>
                <td class="py-3 text-white">${row.model}</td>
                <td class="py-3 text-gray-300">${formatNumber(row.input_tokens + row.output_tokens)}</td>
                <td class="py-3 text-emerald-400">$${row.cost_usd.toFixed(6)}</td>
                <td class="py-3 text-gray-400">${row.latency_ms}ms</td>
            </tr>
        `).join('');

    } catch (error) {
        console.error('Error loading recent usage:', error);
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-red-400 py-8">Failed to load usage data</td>
            </tr>
        `;
    }
}

// Load cost chart
async function loadCostChart() {
    const container = document.getElementById('cost-chart-container');

    try {
        const response = await fetch('/api/cloud-llm-usage/analytics/daily?days=14', {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to fetch analytics');

        const data = await response.json();

        if (!data.length) {
            container.innerHTML = `
                <div class="flex items-center justify-center h-full text-gray-400">
                    No cost data available yet
                </div>
            `;
            return;
        }

        // Simple bar chart using CSS
        const maxCost = Math.max(...data.map(d => d.total_cost_usd || 0), 0.01);

        container.innerHTML = `
            <div class="flex items-end justify-between h-48 gap-2">
                ${data.map(d => {
                    const height = Math.max(((d.total_cost_usd || 0) / maxCost) * 100, 2);
                    const date = new Date(d.date);
                    const dayLabel = date.toLocaleDateString('en-US', { weekday: 'short' });

                    return `
                        <div class="flex-1 flex flex-col items-center group">
                            <div class="w-full bg-blue-500/40 rounded-t hover:bg-blue-500/60 transition-colors relative"
                                 style="height: ${height}%">
                                <div class="absolute -top-8 left-1/2 transform -translate-x-1/2
                                            bg-dark-bg border border-dark-border rounded px-2 py-1 text-xs text-white
                                            opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap">
                                    $${(d.total_cost_usd || 0).toFixed(4)}
                                </div>
                            </div>
                            <div class="text-xs text-gray-500 mt-2">${dayLabel}</div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;

    } catch (error) {
        console.error('Error loading cost chart:', error);
        container.innerHTML = `
            <div class="flex items-center justify-center h-full text-red-400">
                Failed to load chart data
            </div>
        `;
    }
}

// Helper functions
function getProviderBadgeClass(provider) {
    const colors = {
        openai: 'bg-emerald-500/20 text-emerald-400',
        anthropic: 'bg-orange-500/20 text-orange-400',
        google: 'bg-blue-500/20 text-blue-400'
    };
    return colors[provider] || 'bg-gray-500/20 text-gray-400';
}

function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
}

function formatTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: true
    });
}

// Use global getAuthHeaders from utils.js which includes Content-Type: application/json

// Show notification (uses existing function from main app if available)
function showNotification(message, type = 'info') {
    if (typeof window.showToast === 'function') {
        window.showToast(message, type);
    } else {
        console.log(`[${type}] ${message}`);
        alert(message);
    }
}

// Initialize when tab is shown
document.addEventListener('DOMContentLoaded', function() {
    // Add tab activation listener
    const originalShowTab = window.showTab;
    if (originalShowTab) {
        window.showTab = function(tabName) {
            originalShowTab(tabName);
            if (tabName === 'cloud-providers') {
                loadCloudProviders();
            }
        };
    }
});
