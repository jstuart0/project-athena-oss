/**
 * Performance Presets Management
 *
 * Full CRUD interface for managing performance presets.
 * Presets bundle all performance-related settings for easy A/B testing.
 */

let presetsData = [];
let presetsMLXData = null;
let presetFeatureFlags = [];
let presetAvailableModels = [];

/**
 * Load MLX applicability data
 */
async function loadMLXApplicability() {
    try {
        const response = await fetch('/api/llm-backends/public/mlx-applicability', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            presetsMLXData = await response.json();
        }
    } catch (error) {
        console.error('Failed to load MLX applicability:', error);
        presetsMLXData = null;
    }
}

/**
 * Load all feature flags from backend
 */
async function loadPresetFeatureFlags() {
    try {
        const response = await fetch('/api/features', { headers: getAuthHeaders() });
        if (response.ok) {
            presetFeatureFlags = await response.json();
        }
    } catch (error) {
        console.error('Failed to load feature flags:', error);
        presetFeatureFlags = [];
    }
}

/**
 * Load available models from component-models endpoint
 */
async function loadPresetAvailableModels() {
    try {
        const response = await fetch('/api/component-models/available-models', { headers: getAuthHeaders() });
        if (response.ok) {
            const data = await response.json();
            presetAvailableModels = data.models || [];
        }
    } catch (error) {
        console.error('Failed to load available models:', error);
        presetAvailableModels = [];
    }
}

/**
 * Load all presets
 */
async function loadPresets() {
    const container = document.getElementById('presets-container');
    if (!container) return;

    container.innerHTML = `
        <div class="text-center text-gray-400 py-12">
            <div class="animate-pulse">
                <div class="text-4xl mb-4">⚡</div>
                <p>Loading presets...</p>
            </div>
        </div>
    `;

    try {
        // Load presets, MLX data, feature flags, and available models in parallel
        const [presetsResponse] = await Promise.all([
            fetch('/api/presets', { headers: getAuthHeaders() }),
            loadMLXApplicability(),
            loadPresetFeatureFlags(),
            loadPresetAvailableModels()
        ]);

        if (!presetsResponse.ok) throw new Error('Failed to load presets');

        presetsData = await presetsResponse.json();
        renderPresets();
    } catch (error) {
        console.error('Failed to load presets:', error);
        container.innerHTML = `
            <div class="text-center text-red-400 py-12">
                <div class="text-4xl mb-4">⚠️</div>
                <p>Failed to load presets</p>
                <button onclick="loadPresets()" class="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                    Retry
                </button>
            </div>
        `;
    }
}

/**
 * Render MLX warning banner if applicable
 */
function renderMLXWarningBanner() {
    if (!presetsMLXData) return '';

    const { mlx_feature_enabled, summary, mlx_latency_impact_ms, mlx_models } = presetsMLXData;

    // Safety checks
    if (!summary || !mlx_models) return '';

    // Case 1: MLX enabled but no components using it
    if (mlx_feature_enabled && summary.components_using_mlx === 0 && mlx_models.length > 0) {
        return `
            <div class="bg-yellow-900/30 border border-yellow-700 rounded-xl p-4 mb-6">
                <div class="flex items-start gap-3">
                    <span class="text-2xl">⚠️</span>
                    <div class="flex-1">
                        <h4 class="text-white font-medium mb-1">MLX Backend Enabled but Unused</h4>
                        <p class="text-sm text-yellow-200/80">
                            The MLX backend feature is enabled, but none of your components are using MLX models.
                            To benefit from Apple Silicon acceleration (estimated ${Math.abs(mlx_latency_impact_ms)}ms savings),
                            go to <a href="#" onclick="showTab('llm-components')" class="underline text-yellow-300 hover:text-yellow-200">LLM Components</a>
                            and assign MLX models to your components.
                        </p>
                    </div>
                </div>
            </div>
        `;
    }

    // Case 2: MLX could provide significant savings but is disabled
    if (!mlx_feature_enabled && mlx_models.length > 0 && Math.abs(mlx_latency_impact_ms) > 100) {
        return `
            <div class="bg-blue-900/20 border border-blue-700/50 rounded-xl p-4 mb-6">
                <div class="flex items-start gap-3">
                    <span class="text-2xl">💡</span>
                    <div class="flex-1">
                        <h4 class="text-white font-medium mb-1">Apple Silicon Acceleration Available</h4>
                        <p class="text-sm text-blue-200/80">
                            ${mlx_models.length} MLX models are configured and could save an estimated
                            <span class="font-medium text-green-400">${Math.abs(mlx_latency_impact_ms)}ms</span> per request.
                            Enable the "MLX Backend" feature in
                            <a href="#" onclick="showTab('features')" class="underline text-blue-300 hover:text-blue-200">Features</a>
                            to use Apple Silicon acceleration.
                        </p>
                    </div>
                </div>
            </div>
        `;
    }

    // Case 3: MLX is working well - show a positive indicator
    if (mlx_feature_enabled && summary.components_using_mlx > 0) {
        return `
            <div class="bg-green-900/20 border border-green-700/50 rounded-xl p-3 mb-6">
                <div class="flex items-center gap-3">
                    <span class="text-xl">✅</span>
                    <div class="flex-1">
                        <span class="text-sm text-green-300">
                            MLX Backend Active: ${summary.components_using_mlx}/${summary.total_components} components using Apple Silicon acceleration
                            <span class="text-green-400">(saving ~${Math.abs(mlx_latency_impact_ms)}ms)</span>
                        </span>
                    </div>
                </div>
            </div>
        `;
    }

    return '';
}

/**
 * Render presets grid
 */
function renderPresets() {
    const container = document.getElementById('presets-container');
    if (!container) return;

    const systemPresets = presetsData.filter(p => p.is_system);
    const userPresets = presetsData.filter(p => !p.is_system);

    container.innerHTML = `
        <!-- MLX Warning Banner -->
        ${renderMLXWarningBanner()}

        <!-- Header -->
        <div class="flex justify-between items-center mb-6">
            <div>
                <h2 class="text-xl font-semibold text-white">Performance Presets</h2>
                <p class="text-sm text-gray-400 mt-1">Save and switch between different performance configurations</p>
            </div>
            <div class="flex gap-3">
                <button onclick="captureCurrentAsPresetFromList()" class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium flex items-center gap-2">
                    <span>💾</span> Save Current
                </button>
                <button onclick="showCreatePresetModal()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium flex items-center gap-2">
                    <span>+</span> New Preset
                </button>
            </div>
        </div>

        <!-- System Presets -->
        <div class="mb-8">
            <h3 class="text-lg font-medium text-white mb-4 flex items-center gap-2">
                <span>🔒</span> System Presets
                <span class="text-xs text-gray-500 font-normal">(read-only, duplicate to customize)</span>
            </h3>
            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
                ${systemPresets.map(p => renderPresetCard(p)).join('')}
            </div>
        </div>

        <!-- User Presets -->
        <div class="mb-8">
            <h3 class="text-lg font-medium text-white mb-4 flex items-center gap-2">
                <span>👤</span> My Presets
            </h3>
            ${userPresets.length === 0 ? `
                <div class="bg-dark-card border border-dark-border border-dashed rounded-xl p-8 text-center">
                    <div class="text-4xl mb-3">✨</div>
                    <p class="text-gray-400">No custom presets yet</p>
                    <p class="text-sm text-gray-500 mt-1">Duplicate a system preset or create your own</p>
                </div>
            ` : `
                <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
                    ${userPresets.map(p => renderPresetCard(p)).join('')}
                </div>
            `}
        </div>
    `;
}

/**
 * Render a single preset card
 */
function renderPresetCard(preset) {
    const isActive = preset.is_active;
    const borderClass = isActive ? 'border-green-500 ring-2 ring-green-500/20' : 'border-dark-border hover:border-blue-500/50';
    const bgClass = isActive ? 'bg-gradient-to-br from-green-900/20 to-dark-card' : 'bg-dark-card';

    const latencyMs = preset.estimated_latency_ms || 0;
    const latencyColor = latencyMs < 2000 ? 'green' :
                        latencyMs < 4000 ? 'yellow' : 'orange';

    // Get summary of settings
    const settings = preset.settings || {};
    const featureFlags = settings.feature_flags || {};
    const enabledFlagsCount = Object.values(featureFlags).filter(v => v === true).length;
    const totalFlagsCount = Object.keys(featureFlags).length;

    // Model info
    const simpleModel = settings.tool_calling_simple_model || 'default';
    const complexModel = settings.tool_calling_complex_model || 'default';

    return `
        <div class="${bgClass} border ${borderClass} rounded-xl overflow-hidden transition-all relative group">
            <!-- Header -->
            <div class="p-5 pb-4">
                ${isActive ? `
                    <div class="absolute top-3 right-3">
                        <span class="px-2.5 py-1 bg-green-600 text-white text-xs font-bold rounded-full shadow-lg">ACTIVE</span>
                    </div>
                ` : ''}

                <div class="flex items-start gap-3 mb-3">
                    <span class="text-3xl">${preset.icon || '⚡'}</span>
                    <div class="flex-1 min-w-0 pr-16">
                        <h4 class="font-semibold text-white text-lg">${escapeHtml(preset.name)}</h4>
                        ${preset.is_system ? `
                            <span class="inline-block mt-1 px-2 py-0.5 text-xs bg-gray-700 text-gray-300 rounded">System</span>
                        ` : ''}
                    </div>
                </div>

                <p class="text-sm text-gray-400 leading-relaxed">${escapeHtml(preset.description || 'No description')}</p>
            </div>

            <!-- Settings Summary -->
            <div class="px-5 pb-4">
                <div class="bg-dark-bg/50 rounded-lg p-3 space-y-2">
                    <div class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                        <div class="flex justify-between">
                            <span class="text-gray-500">Simple Model</span>
                            <span class="text-gray-300 font-mono text-xs">${simpleModel.split(':')[0]}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-gray-500">Complex Model</span>
                            <span class="text-gray-300 font-mono text-xs">${complexModel.split(':')[0]}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-gray-500">History</span>
                            <span class="text-gray-300 capitalize">${settings.history_mode || 'full'}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-gray-500">Est. Latency</span>
                            <span class="text-${latencyColor}-400 font-medium">${latencyMs ? `~${(latencyMs/1000).toFixed(1)}s` : 'N/A'}</span>
                        </div>
                    </div>
                    <div class="pt-2 border-t border-dark-border flex justify-between items-center">
                        <span class="text-gray-500 text-sm">Feature Flags</span>
                        <span class="text-sm">
                            <span class="text-green-400 font-medium">${enabledFlagsCount}</span>
                            <span class="text-gray-500">/ ${totalFlagsCount} enabled</span>
                        </span>
                    </div>
                </div>
            </div>

            <!-- Actions -->
            <div class="px-5 pb-5">
                <div class="flex gap-2">
                    ${!isActive ? `
                        <button onclick="activatePreset(${preset.id})" class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white text-sm font-medium rounded-lg transition-colors">
                            ✓ Activate
                        </button>
                    ` : `
                        <button disabled class="flex-1 px-4 py-2 bg-green-700/50 text-green-300 text-sm font-medium rounded-lg cursor-default">
                            ✓ Currently Active
                        </button>
                    `}
                    <button onclick="showPresetDetailsModal(${preset.id})" class="px-3 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 text-sm rounded-lg transition-colors" title="View Details">
                        👁️
                    </button>
                    <button onclick="duplicatePreset(${preset.id})" class="px-3 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 text-sm rounded-lg transition-colors" title="Duplicate">
                        📋
                    </button>
                    ${!preset.is_system ? `
                        <button onclick="showEditPresetModal(${preset.id})" class="px-3 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 text-sm rounded-lg transition-colors" title="Edit">
                            ✏️
                        </button>
                        <button onclick="deletePreset(${preset.id})" class="px-3 py-2 bg-dark-bg hover:bg-red-900/50 text-gray-300 hover:text-red-400 text-sm rounded-lg transition-colors" title="Delete">
                            🗑️
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
}

/**
 * Show preset details modal
 */
function showPresetDetailsModal(presetId) {
    const preset = presetsData.find(p => p.id === presetId);
    if (!preset) return;

    const settings = preset.settings || {};
    const featureFlags = settings.feature_flags || {};

    const modal = document.createElement('div');
    modal.id = 'preset-details-modal';
    modal.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-xl w-full max-w-3xl max-h-[90vh] overflow-hidden flex flex-col">
            <!-- Header -->
            <div class="p-6 border-b border-dark-border flex items-start justify-between">
                <div class="flex items-center gap-4">
                    <span class="text-4xl">${preset.icon || '⚡'}</span>
                    <div>
                        <h3 class="text-xl font-semibold text-white">${escapeHtml(preset.name)}</h3>
                        <p class="text-sm text-gray-400 mt-1">${escapeHtml(preset.description || 'No description')}</p>
                        <div class="flex gap-2 mt-2 flex-wrap">
                            ${preset.is_system ? '<span class="px-2 py-0.5 text-xs bg-gray-700 text-gray-300 rounded">System Preset</span>' : ''}
                            ${preset.is_active ? '<span class="px-2 py-0.5 text-xs bg-green-600 text-white rounded">Active</span>' : ''}
                            ${preset.estimated_latency_ms ? `<span class="px-2 py-0.5 text-xs bg-blue-900 text-blue-300 rounded">~${(preset.estimated_latency_ms/1000).toFixed(1)}s latency</span>` : ''}
                            ${preset.created_at ? `<span class="px-2 py-0.5 text-xs bg-gray-800 text-gray-400 rounded">Created: ${formatLocalTime(preset.created_at)}</span>` : ''}
                        </div>
                    </div>
                </div>
                <button onclick="closePresetDetailsModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button>
            </div>

            <!-- Content -->
            <div class="flex-1 overflow-y-auto p-6 space-y-6">
                <!-- Gateway Settings -->
                <div>
                    <h4 class="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-blue-500"></span>
                        Gateway Intent Classification
                    </h4>
                    <div class="bg-dark-bg rounded-lg p-4">
                        <div class="grid grid-cols-3 gap-4 text-sm">
                            <div>
                                <span class="text-gray-500 block">Model</span>
                                <span class="text-white font-mono">${settings.gateway_intent_model || 'phi3:mini'}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Temperature</span>
                                <span class="text-white">${settings.gateway_intent_temperature ?? 0.1}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Max Tokens</span>
                                <span class="text-white">${settings.gateway_intent_max_tokens || 10}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- LLM Routing -->
                <div>
                    <h4 class="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-purple-500"></span>
                        LLM Model Routing
                    </h4>
                    <div class="bg-dark-bg rounded-lg p-4">
                        <div class="grid grid-cols-2 gap-4 text-sm">
                            <div>
                                <span class="text-gray-500 block">Intent Classifier</span>
                                <span class="text-white font-mono">${settings.intent_classifier_model || 'qwen2.5:1.5b'}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Simple Queries</span>
                                <span class="text-white font-mono">${settings.tool_calling_simple_model || 'qwen2.5:7b'}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Complex Queries</span>
                                <span class="text-white font-mono">${settings.tool_calling_complex_model || 'qwen2.5:7b'}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Super Complex Queries</span>
                                <span class="text-white font-mono">${settings.tool_calling_super_complex_model || 'qwen2.5:14b'}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Response Synthesis</span>
                                <span class="text-white font-mono">${settings.response_synthesis_model || 'qwen2.5:7b'}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- LLM Parameters -->
                <div>
                    <h4 class="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-yellow-500"></span>
                        LLM Parameters
                    </h4>
                    <div class="bg-dark-bg rounded-lg p-4">
                        <div class="grid grid-cols-3 gap-4 text-sm">
                            <div>
                                <span class="text-gray-500 block">Temperature</span>
                                <span class="text-white">${settings.llm_temperature ?? 0.5}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Max Tokens</span>
                                <span class="text-white">${settings.llm_max_tokens || 512}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Keep Alive</span>
                                <span class="text-white">${settings.llm_keep_alive_seconds === -1 ? 'Forever' : `${settings.llm_keep_alive_seconds}s`}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Conversation Settings -->
                <div>
                    <h4 class="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-green-500"></span>
                        Conversation Settings
                    </h4>
                    <div class="bg-dark-bg rounded-lg p-4">
                        <div class="grid grid-cols-2 gap-4 text-sm">
                            <div>
                                <span class="text-gray-500 block">History Mode</span>
                                <span class="text-white capitalize">${settings.history_mode || 'full'}</span>
                            </div>
                            <div>
                                <span class="text-gray-500 block">Max History Messages</span>
                                <span class="text-white">${settings.max_llm_history_messages ?? 10}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Feature Flags -->
                <div>
                    <h4 class="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-orange-500"></span>
                        Feature Flags
                        <span class="text-xs text-gray-500 font-normal">(${Object.values(featureFlags).filter(v => v).length} of ${Object.keys(featureFlags).length} enabled)</span>
                    </h4>
                    <div class="bg-dark-bg rounded-lg p-4">
                        ${Object.keys(featureFlags).length === 0 ? `
                            <p class="text-gray-500 text-sm">No feature flags configured in this preset</p>
                        ` : `
                            <div class="grid grid-cols-2 gap-2">
                                ${Object.entries(featureFlags).map(([name, enabled]) => `
                                    <div class="flex items-center gap-2 text-sm">
                                        <span class="w-4 h-4 rounded flex items-center justify-center ${enabled ? 'bg-green-600 text-white' : 'bg-gray-700 text-gray-400'}">
                                            ${enabled ? '✓' : '✕'}
                                        </span>
                                        <span class="${enabled ? 'text-white' : 'text-gray-500'}">${formatFlagName(name)}</span>
                                    </div>
                                `).join('')}
                            </div>
                        `}
                    </div>
                </div>
            </div>

            <!-- Footer -->
            <div class="p-6 border-t border-dark-border flex justify-between">
                <button onclick="duplicatePreset(${preset.id}); closePresetDetailsModal();" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                    📋 Duplicate This Preset
                </button>
                <button onclick="closePresetDetailsModal()" class="px-4 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 rounded-lg text-sm">
                    Close
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closePresetDetailsModal();
    });
}

function closePresetDetailsModal() {
    const modal = document.getElementById('preset-details-modal');
    if (modal) modal.remove();
}

/**
 * Format feature flag name for display
 */
function formatFlagName(name) {
    return name
        .replace(/^ha_/, 'HA: ')
        .replace(/^rag_/, 'RAG: ')
        .replace(/_/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase());
}

/**
 * Format UTC timestamp to local time
 */
function formatLocalTime(utcString) {
    if (!utcString) return 'Unknown';
    const date = new Date(utcString);
    return date.toLocaleString();
}

/**
 * Generate model options for dropdown selects
 * @param {string} selectedModel - Currently selected model
 * @param {string} category - Category hint (simple, complex, super_complex, synthesis)
 */
function generateModelOptions(selectedModel, category = 'general') {
    // Get unique model names from presetAvailableModels (which includes ollama and cloud models)
    const modelSet = new Set();

    // Add models from available models list
    presetAvailableModels.forEach(m => {
        modelSet.add(m.name);
    });

    // Always ensure the selected model is in the list (in case it's not in current available models)
    if (selectedModel) {
        modelSet.add(selectedModel);
    }

    // Add common fallback models in case presetAvailableModels is empty
    if (modelSet.size === 0) {
        ['phi3:mini', 'qwen2.5:3b', 'qwen2.5:7b', 'qwen2.5:14b', 'qwen3:4b', 'qwen3:8b'].forEach(m => modelSet.add(m));
    }

    // Convert to array and sort
    const models = Array.from(modelSet).sort((a, b) => {
        // Sort cloud models last
        const aCloud = a.includes('/');
        const bCloud = b.includes('/');
        if (aCloud !== bCloud) return aCloud ? 1 : -1;
        return a.localeCompare(b);
    });

    // Generate options
    return models.map(model => {
        const isSelected = model === selectedModel;
        const displayName = model.includes('/')
            ? model  // Cloud model - show full name
            : model.split(':')[0] + (model.includes(':') ? `:${model.split(':')[1]}` : '');
        return `<option value="${escapeHtml(model)}" ${isSelected ? 'selected' : ''}>${escapeHtml(displayName)}</option>`;
    }).join('');
}

/**
 * Activate a preset
 */
async function activatePreset(presetId) {
    try {
        const response = await fetch(`/api/presets/${presetId}/activate`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to activate preset');
        }

        const preset = await response.json();
        safeShowToast(`Activated "${preset.name}" preset`, 'success');

        await loadPresets();

        // Refresh system config if on that page
        if (typeof loadSystemConfig === 'function') {
            loadSystemConfig();
        }
    } catch (error) {
        console.error('Failed to activate preset:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Duplicate a preset
 */
async function duplicatePreset(presetId) {
    try {
        const response = await fetch(`/api/presets/${presetId}/duplicate`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to duplicate preset');

        const preset = await response.json();
        safeShowToast(`Created "${preset.name}"`, 'success');

        await loadPresets();
    } catch (error) {
        console.error('Failed to duplicate preset:', error);
        safeShowToast('Failed to duplicate preset', 'error');
    }
}

/**
 * Delete a preset
 */
async function deletePreset(presetId) {
    const preset = presetsData.find(p => p.id === presetId);
    if (!preset) return;

    if (!confirm(`Delete "${preset.name}"? This cannot be undone.`)) return;

    try {
        const response = await fetch(`/api/presets/${presetId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete preset');
        }

        safeShowToast('Preset deleted', 'success');
        await loadPresets();
    } catch (error) {
        console.error('Failed to delete preset:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Capture current settings as a new preset (from list page)
 */
async function captureCurrentAsPresetFromList() {
    const name = prompt('Name for this preset:', `My Settings ${new Date().toLocaleDateString()}`);
    if (!name) return;

    try {
        const response = await fetch(`/api/presets/capture-current?name=${encodeURIComponent(name)}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to capture settings');
        }

        const preset = await response.json();
        safeShowToast(`Saved "${preset.name}" preset`, 'success');

        await loadPresets();
    } catch (error) {
        console.error('Failed to capture preset:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Show create preset modal
 */
function showCreatePresetModal() {
    // Get default settings from balanced preset
    const balanced = presetsData.find(p => p.name === 'Balanced');
    const defaultSettings = balanced?.settings || {};

    const modal = document.createElement('div');
    modal.id = 'preset-modal';
    modal.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-xl w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col">
            <div class="p-6 border-b border-dark-border">
                <h3 class="text-lg font-semibold text-white">Create New Preset</h3>
            </div>

            <form id="preset-form" class="flex-1 overflow-y-auto p-6 space-y-6">
                <!-- Basic Info -->
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Name</label>
                        <input type="text" name="name" required
                               class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                               placeholder="My Custom Preset">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Icon</label>
                        <input type="text" name="icon" maxlength="2"
                               class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                               placeholder="⚡" value="⚡">
                    </div>
                </div>

                <div>
                    <label class="block text-sm text-gray-400 mb-1">Description</label>
                    <textarea name="description" rows="2"
                              class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                              placeholder="Describe this preset..."></textarea>
                </div>

                <!-- Model Settings -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">LLM Model Routing</h4>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Simple Queries</label>
                            <select name="tool_calling_simple_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(defaultSettings.tool_calling_simple_model || 'qwen2.5:7b', 'simple')}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Complex Queries</label>
                            <select name="tool_calling_complex_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(defaultSettings.tool_calling_complex_model || 'qwen2.5:7b', 'complex')}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Super Complex Queries</label>
                            <select name="tool_calling_super_complex_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(defaultSettings.tool_calling_super_complex_model || 'qwen2.5:14b', 'super_complex')}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Response Synthesis</label>
                            <select name="response_synthesis_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(defaultSettings.response_synthesis_model || 'qwen2.5:7b', 'synthesis')}
                            </select>
                        </div>
                    </div>
                </div>

                <!-- LLM Parameters -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">LLM Parameters</h4>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Temperature</label>
                            <input type="number" name="llm_temperature" step="0.1" min="0" max="2"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                                   value="${defaultSettings.llm_temperature || 0.5}">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Max Tokens</label>
                            <input type="number" name="llm_max_tokens" min="64" max="4096"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                                   value="${defaultSettings.llm_max_tokens || 512}">
                        </div>
                    </div>
                </div>

                <!-- Conversation Settings -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">Conversation Settings</h4>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">History Mode</label>
                            <select name="history_mode" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                <option value="none">None (Fastest)</option>
                                <option value="summarized" selected>Summarized (Balanced)</option>
                                <option value="full">Full (Best Context)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Max History Messages</label>
                            <input type="number" name="max_llm_history_messages" min="0" max="50"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                                   value="${defaultSettings.max_llm_history_messages || 5}">
                        </div>
                    </div>
                </div>

                <!-- Feature Flags -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">Feature Flags</h4>
                    <div class="grid grid-cols-2 gap-2 max-h-48 overflow-y-auto">
                        ${renderFeatureFlagCheckboxes(defaultSettings.feature_flags || {})}
                    </div>
                </div>
            </form>

            <!-- Actions -->
            <div class="p-6 border-t border-dark-border flex justify-end gap-3">
                <button type="button" onclick="closePresetModal()" class="px-4 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 rounded-lg">
                    Cancel
                </button>
                <button type="submit" form="preset-form" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Create Preset
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closePresetModal();
    });

    document.getElementById('preset-form').addEventListener('submit', handleCreatePreset);
}

function renderFeatureFlagCheckboxes(defaults) {
    // Use all feature flags from the loaded list, or fallback to defaults
    const flagsToRender = presetFeatureFlags.length > 0
        ? presetFeatureFlags.map(f => ({ name: f.name, label: f.display_name || formatFlagName(f.name) }))
        : [
            { name: 'ha_room_detection_cache', label: 'Room Detection Cache' },
            { name: 'ha_simple_command_fastpath', label: 'Simple Command Fast Path' },
            { name: 'ha_parallel_init', label: 'Parallel Initialization' },
            { name: 'ha_precomputed_summaries', label: 'Precomputed Summaries' },
            { name: 'ha_session_warmup', label: 'Session Warmup' },
            { name: 'ha_intent_prerouting', label: 'Intent Pre-routing' }
        ];

    return flagsToRender.map(flag => `
        <label class="flex items-center gap-2 text-sm text-gray-300 cursor-pointer py-1">
            <input type="checkbox" name="flag_${flag.name}"
                   ${defaults[flag.name] === true ? 'checked' : ''}
                   class="rounded border-gray-600 bg-dark-bg text-blue-600 focus:ring-blue-500">
            ${escapeHtml(flag.label)}
        </label>
    `).join('');
}

async function handleCreatePreset(e) {
    e.preventDefault();

    const form = e.target;
    const formData = new FormData(form);

    // Build feature flags from all checkboxes
    const feature_flags = {};
    presetFeatureFlags.forEach(flag => {
        feature_flags[flag.name] = formData.get(`flag_${flag.name}`) === 'on';
    });

    const settings = {
        // Gateway intent (use defaults)
        gateway_intent_model: 'phi3:mini',
        gateway_intent_temperature: 0.1,
        gateway_intent_max_tokens: 10,

        // Intent classifier
        intent_classifier_model: 'qwen2.5:1.5b',

        // Tool calling models
        tool_calling_simple_model: formData.get('tool_calling_simple_model'),
        tool_calling_complex_model: formData.get('tool_calling_complex_model'),
        tool_calling_super_complex_model: formData.get('tool_calling_super_complex_model'),
        response_synthesis_model: formData.get('response_synthesis_model'),

        // LLM parameters
        llm_temperature: parseFloat(formData.get('llm_temperature')),
        llm_max_tokens: parseInt(formData.get('llm_max_tokens')),
        llm_keep_alive_seconds: -1,

        // Conversation
        history_mode: formData.get('history_mode'),
        max_llm_history_messages: parseInt(formData.get('max_llm_history_messages')),

        // Feature flags
        feature_flags: feature_flags
    };

    const data = {
        name: formData.get('name'),
        description: formData.get('description'),
        icon: formData.get('icon') || null,
        settings: settings
    };

    try {
        const response = await fetch('/api/presets', {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create preset');
        }

        closePresetModal();
        safeShowToast('Preset created!', 'success');
        await loadPresets();
    } catch (error) {
        console.error('Failed to create preset:', error);
        safeShowToast(error.message, 'error');
    }
}

function closePresetModal() {
    const modal = document.getElementById('preset-modal');
    if (modal) modal.remove();
}

/**
 * Show edit preset modal
 */
function showEditPresetModal(presetId) {
    const preset = presetsData.find(p => p.id === presetId);
    if (!preset) return;

    const settings = preset.settings || {};

    const modal = document.createElement('div');
    modal.id = 'preset-modal';
    modal.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-xl w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col">
            <div class="p-6 border-b border-dark-border">
                <h3 class="text-lg font-semibold text-white">Edit Preset: ${escapeHtml(preset.name)}</h3>
            </div>

            <form id="edit-preset-form" class="flex-1 overflow-y-auto p-6 space-y-6">
                <input type="hidden" name="preset_id" value="${preset.id}">

                <!-- Basic Info -->
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Name</label>
                        <input type="text" name="name" required value="${escapeHtml(preset.name)}"
                               class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Icon</label>
                        <input type="text" name="icon" maxlength="2" value="${preset.icon || ''}"
                               class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-sm text-gray-400 mb-1">Description</label>
                    <textarea name="description" rows="2"
                              class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">${escapeHtml(preset.description || '')}</textarea>
                </div>

                <!-- Model Settings -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">LLM Model Routing</h4>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Simple Queries</label>
                            <select name="tool_calling_simple_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(settings.tool_calling_simple_model, 'simple')}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Complex Queries</label>
                            <select name="tool_calling_complex_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(settings.tool_calling_complex_model, 'complex')}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Super Complex Queries</label>
                            <select name="tool_calling_super_complex_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(settings.tool_calling_super_complex_model, 'super_complex')}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Response Synthesis</label>
                            <select name="response_synthesis_model" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                ${generateModelOptions(settings.response_synthesis_model, 'synthesis')}
                            </select>
                        </div>
                    </div>
                </div>

                <!-- LLM Parameters -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">LLM Parameters</h4>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Temperature</label>
                            <input type="number" name="llm_temperature" step="0.1" min="0" max="2"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                                   value="${settings.llm_temperature || 0.5}">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Max Tokens</label>
                            <input type="number" name="llm_max_tokens" min="64" max="4096"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                                   value="${settings.llm_max_tokens || 512}">
                        </div>
                    </div>
                </div>

                <!-- Conversation Settings -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">Conversation Settings</h4>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">History Mode</label>
                            <select name="history_mode" class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                <option value="none" ${settings.history_mode === 'none' ? 'selected' : ''}>None (Fastest)</option>
                                <option value="summarized" ${settings.history_mode === 'summarized' ? 'selected' : ''}>Summarized (Balanced)</option>
                                <option value="full" ${settings.history_mode === 'full' ? 'selected' : ''}>Full (Best Context)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Max History Messages</label>
                            <input type="number" name="max_llm_history_messages" min="0" max="50"
                                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                                   value="${settings.max_llm_history_messages || 5}">
                        </div>
                    </div>
                </div>

                <!-- Feature Flags -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">Feature Flags</h4>
                    <div class="grid grid-cols-2 gap-2 max-h-48 overflow-y-auto">
                        ${renderFeatureFlagCheckboxes(settings.feature_flags || {})}
                    </div>
                </div>
            </form>

            <!-- Actions -->
            <div class="p-6 border-t border-dark-border flex justify-end gap-3">
                <button type="button" onclick="closePresetModal()" class="px-4 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 rounded-lg">
                    Cancel
                </button>
                <button type="submit" form="edit-preset-form" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Save Changes
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closePresetModal();
    });

    document.getElementById('edit-preset-form').addEventListener('submit', handleEditPreset);
}

async function handleEditPreset(e) {
    e.preventDefault();

    const form = e.target;
    const formData = new FormData(form);
    const presetId = formData.get('preset_id');

    // Build feature flags from all checkboxes
    const feature_flags = {};
    presetFeatureFlags.forEach(flag => {
        feature_flags[flag.name] = formData.get(`flag_${flag.name}`) === 'on';
    });

    const settings = {
        // Gateway intent (preserve or use defaults)
        gateway_intent_model: 'phi3:mini',
        gateway_intent_temperature: 0.1,
        gateway_intent_max_tokens: 10,

        // Intent classifier
        intent_classifier_model: 'qwen2.5:1.5b',

        // Tool calling models
        tool_calling_simple_model: formData.get('tool_calling_simple_model'),
        tool_calling_complex_model: formData.get('tool_calling_complex_model'),
        tool_calling_super_complex_model: formData.get('tool_calling_super_complex_model'),
        response_synthesis_model: formData.get('response_synthesis_model'),

        // LLM parameters
        llm_temperature: parseFloat(formData.get('llm_temperature')),
        llm_max_tokens: parseInt(formData.get('llm_max_tokens')),
        llm_keep_alive_seconds: -1,

        // Conversation
        history_mode: formData.get('history_mode'),
        max_llm_history_messages: parseInt(formData.get('max_llm_history_messages')),

        // Feature flags
        feature_flags: feature_flags
    };

    const data = {
        name: formData.get('name'),
        description: formData.get('description'),
        icon: formData.get('icon') || null,
        settings: settings
    };

    try {
        const response = await fetch(`/api/presets/${presetId}`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update preset');
        }

        closePresetModal();
        safeShowToast('Preset updated!', 'success');
        await loadPresets();
    } catch (error) {
        console.error('Failed to update preset:', error);
        safeShowToast(error.message, 'error');
    }
}

// Initialize
function initPresetsPage() {
    console.log('Initializing Presets page');
    loadPresets();
}

// Export
if (typeof window !== 'undefined') {
    window.initPresetsPage = initPresetsPage;
    window.loadPresets = loadPresets;
    window.activatePreset = activatePreset;
    window.duplicatePreset = duplicatePreset;
    window.deletePreset = deletePreset;
    window.showCreatePresetModal = showCreatePresetModal;
    window.showEditPresetModal = showEditPresetModal;
    window.showPresetDetailsModal = showPresetDetailsModal;
    window.closePresetModal = closePresetModal;
    window.closePresetDetailsModal = closePresetDetailsModal;
    window.captureCurrentAsPresetFromList = captureCurrentAsPresetFromList;
}
