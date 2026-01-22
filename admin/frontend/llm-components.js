/**
 * LLM Components Configuration - Frontend JavaScript
 * Handles component model assignment with auto-refresh and validation
 */

// State
let availableModels = [];
let componentAssignments = [];
let mlxApplicability = null;
let autoRefreshInterval = null;
const AUTO_REFRESH_MS = 5 * 60 * 1000; // 5 minutes

// ============================================================================
// Initialization
// ============================================================================

async function loadLLMComponents() {
    // Load all data in parallel
    await Promise.all([
        loadAvailableModels(),
        loadComponentAssignments(),
        loadMLXApplicability()
    ]);

    // Render MLX summary card
    renderMLXSummary();

    // Start auto-refresh
    startAutoRefresh();
}

function startAutoRefresh() {
    const refreshCallback = async () => {
        await loadAvailableModels();
        updateLastRefreshTime();
    };

    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('llm-components-refresh', refreshCallback, AUTO_REFRESH_MS);
    } else {
        if (autoRefreshInterval) {
            clearInterval(autoRefreshInterval);
        }
        autoRefreshInterval = setInterval(refreshCallback, AUTO_REFRESH_MS);
    }

    updateLastRefreshTime();
}

function stopAutoRefresh() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('llm-components-refresh');
    } else if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
}

function updateLastRefreshTime() {
    const el = document.getElementById('llm-components-last-refresh');
    if (el) {
        el.textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;
    }
}

// ============================================================================
// Data Loading
// ============================================================================

async function loadAvailableModels() {
    try {
        const data = await apiRequest('/api/component-models/available-models');
        availableModels = data.models || [];

        document.getElementById('available-models-count').textContent = availableModels.length;

        // Re-render dropdowns if assignments are loaded
        if (componentAssignments.length > 0) {
            renderAllComponentTables();
        }

    } catch (error) {
        console.error('Failed to load available models:', error);
        document.getElementById('available-models-count').textContent = 'Error';
    }
}

async function refreshAvailableModels() {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Refreshing...';

    await loadAvailableModels();

    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="refresh-cw" class="w-4 h-4 inline-block mr-1"></i> Refresh Models';
    if (typeof lucide !== 'undefined') lucide.createIcons();
    updateLastRefreshTime();
}

async function loadComponentAssignments() {
    try {
        componentAssignments = await apiRequest('/api/component-models');
        renderAllComponentTables();
    } catch (error) {
        console.error('Failed to load component assignments:', error);
        showComponentError('Failed to load component assignments');
    }
}

async function loadMLXApplicability() {
    try {
        mlxApplicability = await apiRequest('/api/llm-backends/public/mlx-applicability');
    } catch (error) {
        console.error('Failed to load MLX applicability:', error);
        mlxApplicability = null;
    }
}

function renderMLXSummary() {
    const container = document.getElementById('mlx-summary-container');
    if (!container || !mlxApplicability) return;

    const { summary, mlx_feature_enabled, mlx_latency_impact_ms, mlx_models } = mlxApplicability;

    // Determine status and styling
    let statusClass, statusText, statusIcon;
    if (!mlx_feature_enabled) {
        statusClass = 'bg-gray-800 border-gray-700';
        statusText = 'MLX Backend Disabled';
        statusIcon = '⚪';
    } else if (summary.components_using_mlx === 0) {
        statusClass = 'bg-yellow-900/30 border-yellow-700';
        statusText = 'MLX Enabled but Unused';
        statusIcon = '⚠️';
    } else {
        statusClass = 'bg-green-900/30 border-green-700';
        statusText = 'MLX Active';
        statusIcon = '✅';
    }

    container.innerHTML = `
        <div class="${statusClass} border rounded-lg p-4 mb-6">
            <div class="flex items-center justify-between mb-3">
                <div class="flex items-center gap-2">
                    <span class="text-xl">${statusIcon}</span>
                    <h3 class="text-lg font-semibold text-white">${statusText}</h3>
                </div>
                <div class="text-sm text-gray-400">
                    Latency impact: <span class="font-mono ${mlx_latency_impact_ms < 0 ? 'text-green-400' : 'text-red-400'}">${mlx_latency_impact_ms > 0 ? '+' : ''}${mlx_latency_impact_ms}ms</span>
                </div>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                <div class="bg-gray-800/50 rounded p-3">
                    <div class="text-gray-400">Components Using MLX</div>
                    <div class="text-2xl font-bold ${summary.components_using_mlx > 0 ? 'text-green-400' : 'text-gray-500'}">${summary.components_using_mlx}</div>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <div class="text-gray-400">Components on Ollama</div>
                    <div class="text-2xl font-bold text-blue-400">${summary.components_not_using_mlx}</div>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <div class="text-gray-400">MLX Models Available</div>
                    <div class="text-2xl font-bold text-purple-400">${summary.mlx_models_available}</div>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <div class="text-gray-400">MLX Utilization</div>
                    <div class="text-2xl font-bold ${summary.mlx_utilization_percent > 0 ? 'text-green-400' : 'text-gray-500'}">${summary.mlx_utilization_percent}%</div>
                </div>
            </div>
            ${mlx_models.length > 0 ? `
                <div class="mt-3 pt-3 border-t border-gray-700">
                    <div class="text-xs text-gray-400 mb-2">Available MLX Models:</div>
                    <div class="flex flex-wrap gap-2">
                        ${mlx_models.map(m => `
                            <span class="px-2 py-1 text-xs rounded ${m.enabled ? 'bg-purple-900/50 text-purple-300' : 'bg-gray-700 text-gray-400'}">
                                ${escapeHtml(m.model_name)}
                            </span>
                        `).join('')}
                    </div>
                </div>
            ` : ''}
            ${!mlx_feature_enabled && mlx_models.length > 0 ? `
                <div class="mt-3 p-2 bg-yellow-900/20 border border-yellow-800 rounded text-sm text-yellow-300">
                    💡 Enable the "MLX Backend" feature flag in Features to use Apple Silicon acceleration
                </div>
            ` : ''}
            ${mlx_feature_enabled && summary.components_using_mlx === 0 && mlx_models.length > 0 ? `
                <div class="mt-3 p-2 bg-yellow-900/20 border border-yellow-800 rounded text-sm text-yellow-300">
                    ⚠️ MLX is enabled but no components are using MLX models. Assign MLX models to components below to benefit from Apple Silicon acceleration.
                </div>
            ` : ''}
        </div>
    `;
}

// ============================================================================
// Rendering
// ============================================================================

function renderAllComponentTables() {
    const categories = {
        'orchestrator': 'orchestrator-components-table',
        'validation': 'validation-components-table',
        'control': 'control-components-table'
    };

    for (const [category, containerId] of Object.entries(categories)) {
        const components = componentAssignments.filter(c => c.category === category);
        renderComponentTable(containerId, components);
    }
}

function renderComponentTable(containerId, components) {
    const container = document.getElementById(containerId);

    if (components.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                No components in this category
            </div>
        `;
        return;
    }

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Component${typeof infoIcon === 'function' ? infoIcon('llm-component-name') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Model${typeof infoIcon === 'function' ? infoIcon('llm-component-model') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Temperature${typeof infoIcon === 'function' ? infoIcon('llm-component-temperature') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Status${typeof infoIcon === 'function' ? infoIcon('llm-component-status') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${components.map(c => renderComponentRow(c)).join('')}
            </tbody>
        </table>
    `;
}

function renderComponentRow(component) {
    // Check if this model is MLX
    const isMLXModel = mlxApplicability && (
        mlxApplicability.mlx_models.some(m => m.model_name === component.model_name) ||
        component.model_name.toLowerCase().includes('mlx')
    );
    const backendType = component.backend_type || (isMLXModel ? 'mlx' : 'ollama');

    // Build model options with backend indicators (MLX, Cloud, Ollama)
    const modelOptions = availableModels.map(m => {
        const modelIsMLX = mlxApplicability && (
            mlxApplicability.mlx_models.some(mlx => mlx.model_name === m.name) ||
            m.name.toLowerCase().includes('mlx')
        );
        const isCloudModel = m.backend_type && ['openai', 'anthropic', 'google'].includes(m.backend_type);

        let indicator = '';
        let sizeInfo = '';
        if (isCloudModel) {
            indicator = ` [${m.provider || m.backend_type.toUpperCase()}]`;
        } else if (modelIsMLX) {
            indicator = ' [MLX]';
        }
        if (m.size > 0) {
            sizeInfo = ` (${formatBytes(m.size)})`;
        }

        return `<option value="${escapeHtml(m.name)}" ${m.name === component.model_name ? 'selected' : ''}>
            ${escapeHtml(m.name)}${indicator}${sizeInfo}
        </option>`;
    }).join('');

    // Add MLX models from llm_backends that might not be in Ollama list
    let mlxModelOptions = '';
    if (mlxApplicability && mlxApplicability.mlx_models) {
        const availableModelNames = new Set(availableModels.map(m => m.name));
        mlxApplicability.mlx_models.forEach(m => {
            if (!availableModelNames.has(m.model_name)) {
                const selected = m.model_name === component.model_name ? 'selected' : '';
                mlxModelOptions += `<option value="${escapeHtml(m.model_name)}" ${selected}>
                    ${escapeHtml(m.model_name)} [MLX]
                </option>`;
            }
        });
    }

    // Add current model if not in available list
    const currentModelInList = availableModels.some(m => m.name === component.model_name) ||
        (mlxApplicability && mlxApplicability.mlx_models.some(m => m.model_name === component.model_name));
    const currentModelOption = !currentModelInList ?
        `<option value="${escapeHtml(component.model_name)}" selected>${escapeHtml(component.model_name)} (current)</option>` : '';

    const statusBadge = component.enabled
        ? '<span class="px-2 py-1 text-xs rounded bg-green-900 text-green-300">Enabled</span>'
        : '<span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-400">Disabled</span>';

    // Backend badge - detect cloud models by prefix
    const isCloudModel = component.model_name && component.model_name.includes('/') &&
        ['openai', 'anthropic', 'google'].includes(component.model_name.split('/')[0]);
    const cloudProvider = isCloudModel ? component.model_name.split('/')[0] : null;

    let backendBadge;
    if (cloudProvider === 'openai') {
        backendBadge = '<span class="px-2 py-1 text-xs rounded bg-emerald-900 text-emerald-300">OpenAI</span>';
    } else if (cloudProvider === 'anthropic') {
        backendBadge = '<span class="px-2 py-1 text-xs rounded bg-orange-900 text-orange-300">Anthropic</span>';
    } else if (cloudProvider === 'google') {
        backendBadge = '<span class="px-2 py-1 text-xs rounded bg-blue-900 text-blue-300">Google</span>';
    } else if (isMLXModel) {
        backendBadge = '<span class="px-2 py-1 text-xs rounded bg-purple-900 text-purple-300">MLX</span>';
    } else {
        backendBadge = '<span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-300">Ollama</span>';
    }

    return `
        <tr class="hover:bg-gray-800/50">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(component.display_name)}</div>
                <div class="text-xs text-gray-500">${escapeHtml(component.description || '')}</div>
            </td>
            <td class="px-4 py-3">
                <div class="flex items-center gap-2">
                    <select
                        id="model-select-${component.component_name}"
                        onchange="updateComponentModel('${component.component_name}', this.value)"
                        class="bg-gray-700 border border-gray-600 rounded px-3 py-1 text-sm text-white flex-1 max-w-xs">
                        ${currentModelOption}
                        ${modelOptions}
                        ${mlxModelOptions ? `<optgroup label="MLX Models">${mlxModelOptions}</optgroup>` : ''}
                    </select>
                    ${backendBadge}
                </div>
            </td>
            <td class="px-4 py-3">
                <input
                    type="number"
                    step="0.1"
                    min="0"
                    max="2"
                    value="${component.temperature || 0.7}"
                    onchange="updateComponentTemperature('${component.component_name}', this.value)"
                    class="bg-gray-700 border border-gray-600 rounded px-3 py-1 text-sm text-white w-20">
            </td>
            <td class="px-4 py-3">${statusBadge}</td>
            <td class="px-4 py-3">
                <button
                    onclick="toggleComponent('${component.component_name}')"
                    class="text-sm ${component.enabled ? 'text-yellow-400 hover:text-yellow-300' : 'text-green-400 hover:text-green-300'}">
                    ${component.enabled ? 'Disable' : 'Enable'}
                </button>
            </td>
        </tr>
    `;
}

// ============================================================================
// Actions
// ============================================================================

async function updateComponentModel(componentName, modelName) {
    try {
        await apiRequest(`/api/component-models/${componentName}`, {
            method: 'PUT',
            body: JSON.stringify({ model_name: modelName })
        });

        showToast(`Updated ${componentName} to use ${modelName}`, 'success');
        await loadComponentAssignments();

    } catch (error) {
        showToast(`Failed to update: ${error.message}`, 'error');
        await loadComponentAssignments(); // Reload to reset dropdown
    }
}

async function updateComponentTemperature(componentName, temperature) {
    try {
        await apiRequest(`/api/component-models/${componentName}`, {
            method: 'PUT',
            body: JSON.stringify({ temperature: parseFloat(temperature) })
        });

        showToast(`Updated temperature for ${componentName}`, 'success');

    } catch (error) {
        showToast(`Failed to update: ${error.message}`, 'error');
        await loadComponentAssignments();
    }
}

async function toggleComponent(componentName) {
    try {
        await apiRequest(`/api/component-models/${componentName}/toggle`, {
            method: 'POST'
        });

        showToast(`Toggled ${componentName}`, 'success');
        await loadComponentAssignments();

    } catch (error) {
        showToast(`Failed to toggle: ${error.message}`, 'error');
    }
}

// ============================================================================
// Utilities
// ============================================================================

function showComponentError(message) {
    ['orchestrator-components-table', 'validation-components-table', 'control-components-table'].forEach(id => {
        const container = document.getElementById(id);
        if (container) {
            container.innerHTML = `
                <div class="text-center text-red-400 py-8">
                    ${escapeHtml(message)}
                </div>
            `;
        }
    });
}

// formatBytes, escapeHtml, and showNotification are now provided by utils.js

// Cleanup on tab switch
window.addEventListener('beforeunload', () => {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
    }
});
