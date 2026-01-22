/**
 * Model Configuration Management UI
 *
 * Provides dynamic LLM model configuration with:
 * - Model-specific Ollama/MLX options
 * - Preset configurations (speed, balanced, quality, mirostat)
 * - Real-time editing with immediate save
 * - Default fallback configuration (_default)
 */

let modelConfigs = [];
let presets = {};

/**
 * Load all model configurations
 */
async function loadModelConfigs() {
    try {
        const response = await fetch('/api/model-configs', {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load model configs: ${response.statusText}`);
        }

        modelConfigs = await response.json();
        console.log('Model configs loaded:', modelConfigs.length);

        // Also load presets
        await loadPresets();

        renderModelConfigs();
    } catch (error) {
        console.error('Failed to load model configs:', error);
        safeShowToast('Failed to load model configurations', 'error');
        showModelConfigError(error.message);
    }
}

/**
 * Load available presets
 */
async function loadPresets() {
    try {
        const response = await fetch('/api/model-configs/presets', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            presets = await response.json();
        }
    } catch (error) {
        console.error('Failed to load presets:', error);
    }
}

/**
 * Render model configurations
 */
function renderModelConfigs() {
    const container = document.getElementById('model-configs-container');
    if (!container) return;

    if (modelConfigs.length === 0) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-4xl mb-4">🎛️</div>
                <p class="text-gray-400">No model configurations found</p>
                <button onclick="showAddConfigModal()" class="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors">
                    Add Configuration
                </button>
            </div>
        `;
        return;
    }

    // Summary cards
    const enabledCount = modelConfigs.filter(c => c.enabled).length;
    const ollamaCount = modelConfigs.filter(c => c.backend_type === 'ollama').length;
    const mlxCount = modelConfigs.filter(c => c.backend_type === 'mlx').length;
    const autoCount = modelConfigs.filter(c => c.backend_type === 'auto').length;

    let html = `
        <!-- Summary Cards -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-blue-500/20">
                        <span class="text-xl">🎛️</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">Total Models</div>
                        <div class="text-2xl font-bold text-white">${modelConfigs.length}</div>
                    </div>
                </div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-green-500/20">
                        <span class="text-xl">✓</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">Enabled</div>
                        <div class="text-2xl font-bold text-green-400">${enabledCount}</div>
                    </div>
                </div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-orange-500/20">
                        <span class="text-xl">🦙</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">Ollama</div>
                        <div class="text-2xl font-bold text-orange-400">${ollamaCount}</div>
                    </div>
                </div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-purple-500/20">
                        <span class="text-xl">🍎</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">MLX / Auto</div>
                        <div class="text-2xl font-bold text-purple-400">${mlxCount + autoCount}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Add Button -->
        <div class="flex justify-end mb-4">
            <button onclick="showAddConfigModal()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-2">
                <span>+</span>
                <span>Add Configuration</span>
            </button>
        </div>

        <!-- Configurations Table -->
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <div class="overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="text-left text-gray-400 border-b border-dark-border bg-dark-bg">
                            <th class="p-4 font-medium">Model</th>
                            <th class="p-4 font-medium">Backend</th>
                            <th class="p-4 font-medium">Temperature</th>
                            <th class="p-4 font-medium">Context</th>
                            <th class="p-4 font-medium">Sampling</th>
                            <th class="p-4 font-medium">Status</th>
                            <th class="p-4 font-medium text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${modelConfigs.map(config => renderConfigRow(config)).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;

    container.innerHTML = html;
}

/**
 * Render a single configuration row
 */
function renderConfigRow(config) {
    const isDefault = config.model_name === '_default';
    const ollamaOpts = config.ollama_options || {};

    // Determine sampling method
    let samplingMethod = 'Standard';
    let samplingColor = 'text-gray-400';
    if (ollamaOpts.mirostat === 1) {
        samplingMethod = 'Mirostat 1';
        samplingColor = 'text-purple-400';
    } else if (ollamaOpts.mirostat === 2) {
        samplingMethod = 'Mirostat 2.0';
        samplingColor = 'text-green-400';
    } else if (ollamaOpts.top_k || ollamaOpts.top_p) {
        samplingMethod = `Top-K/P`;
        samplingColor = 'text-blue-400';
    }

    const backendColors = {
        'ollama': 'bg-orange-500/20 text-orange-400',
        'mlx': 'bg-purple-500/20 text-purple-400',
        'auto': 'bg-blue-500/20 text-blue-400'
    };
    const backendColor = backendColors[config.backend_type] || 'bg-gray-500/20 text-gray-400';

    return `
        <tr class="border-b border-dark-border/50 hover:bg-dark-bg/50 transition-colors">
            <td class="p-4">
                <div class="flex items-center gap-2">
                    ${isDefault ? '<span class="text-yellow-500" title="Default fallback">⭐</span>' : ''}
                    <div>
                        <div class="font-medium text-white">${config.display_name || config.model_name}</div>
                        <div class="text-xs text-gray-500">${config.model_name}</div>
                    </div>
                </div>
            </td>
            <td class="p-4">
                <span class="px-2 py-1 rounded-full text-xs font-medium ${backendColor}">
                    ${config.backend_type}
                </span>
            </td>
            <td class="p-4 font-mono text-gray-300">${config.temperature}</td>
            <td class="p-4 font-mono text-gray-300">${ollamaOpts.num_ctx || '-'}</td>
            <td class="p-4">
                <span class="${samplingColor}">${samplingMethod}</span>
            </td>
            <td class="p-4">
                <span class="px-2 py-1 rounded-full text-xs font-medium ${config.enabled ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">
                    ${config.enabled ? 'Enabled' : 'Disabled'}
                </span>
            </td>
            <td class="p-4 text-right">
                <div class="flex items-center justify-end gap-2">
                    <button onclick="showEditConfigModal(${config.id})" class="p-2 hover:bg-dark-bg rounded-lg transition-colors" title="Edit">
                        <svg class="w-4 h-4 text-gray-400 hover:text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                        </svg>
                    </button>
                    <button onclick="toggleModelConfig(${config.id})" class="p-2 hover:bg-dark-bg rounded-lg transition-colors" title="Toggle">
                        <svg class="w-4 h-4 ${config.enabled ? 'text-green-400' : 'text-gray-400'}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" />
                        </svg>
                    </button>
                    ${!isDefault ? `
                    <button onclick="deleteModelConfig(${config.id}, '${config.model_name}')" class="p-2 hover:bg-red-500/20 rounded-lg transition-colors" title="Delete">
                        <svg class="w-4 h-4 text-gray-400 hover:text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                    </button>
                    ` : ''}
                </div>
            </td>
        </tr>
    `;
}

/**
 * Show add configuration modal
 */
function showAddConfigModal() {
    showConfigModal(null);
}

/**
 * Show edit configuration modal
 */
function showEditConfigModal(configId) {
    const config = modelConfigs.find(c => c.id === configId);
    if (config) {
        showConfigModal(config);
    }
}

/**
 * Show configuration modal (add/edit)
 */
function showConfigModal(config) {
    const isEdit = config !== null;
    const title = isEdit ? `Edit: ${config.display_name || config.model_name}` : 'Add Model Configuration';

    const ollamaOpts = config?.ollama_options || {};

    const html = `
        <div id="config-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id === 'config-modal') closeConfigModal()">
            <div class="bg-dark-card border border-dark-border rounded-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto m-4">
                <!-- Header -->
                <div class="p-6 border-b border-dark-border">
                    <h3 class="text-xl font-semibold text-white">${title}</h3>
                </div>

                <!-- Form -->
                <form id="config-form" onsubmit="saveModelConfig(event, ${config?.id || 'null'})" class="p-6 space-y-6">
                    <!-- Basic Settings -->
                    <div class="space-y-4">
                        <h4 class="text-sm font-medium text-gray-400 uppercase tracking-wider">Basic Settings</h4>

                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Model Name *</label>
                                <input type="text" name="model_name" value="${config?.model_name || ''}"
                                       ${isEdit ? 'readonly' : ''}
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white ${isEdit ? 'opacity-50' : ''}"
                                       placeholder="e.g., qwen3:8b"
                                       required>
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Display Name</label>
                                <input type="text" name="display_name" value="${config?.display_name || ''}"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white"
                                       placeholder="e.g., Qwen3 8B (Mirostat)">
                            </div>
                        </div>

                        <div class="grid grid-cols-3 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Backend</label>
                                <select name="backend_type" class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                    <option value="ollama" ${config?.backend_type === 'ollama' ? 'selected' : ''}>Ollama</option>
                                    <option value="mlx" ${config?.backend_type === 'mlx' ? 'selected' : ''}>MLX</option>
                                    <option value="auto" ${config?.backend_type === 'auto' ? 'selected' : ''}>Auto</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Temperature</label>
                                <input type="number" name="temperature" value="${config?.temperature ?? 0.7}"
                                       step="0.1" min="0" max="2"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Max Tokens</label>
                                <input type="number" name="max_tokens" value="${config?.max_tokens ?? 2048}"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                        </div>

                        <div>
                            <label class="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" name="enabled" ${config?.enabled !== false ? 'checked' : ''}
                                       class="w-4 h-4 rounded border-dark-border bg-dark-bg">
                                <span class="text-sm text-gray-300">Enabled</span>
                            </label>
                        </div>
                    </div>

                    <!-- Quick Presets -->
                    <div class="space-y-4">
                        <h4 class="text-sm font-medium text-gray-400 uppercase tracking-wider">Quick Presets</h4>
                        <div class="flex flex-wrap gap-2">
                            ${Object.entries(presets).map(([key, preset]) => `
                                <button type="button" onclick="applyPreset('${key}')"
                                        class="px-3 py-1.5 bg-dark-bg border border-dark-border rounded-lg text-sm text-gray-300 hover:border-blue-500/50 hover:text-white transition-colors">
                                    ${preset.name}
                                </button>
                            `).join('')}
                        </div>
                    </div>

                    <!-- Ollama Options -->
                    <div class="space-y-4">
                        <h4 class="text-sm font-medium text-gray-400 uppercase tracking-wider">Ollama Options</h4>

                        <div class="grid grid-cols-3 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Context Window (num_ctx)</label>
                                <input type="number" name="num_ctx" value="${ollamaOpts.num_ctx || ''}"
                                       placeholder="4096"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Batch Size (num_batch)</label>
                                <input type="number" name="num_batch" value="${ollamaOpts.num_batch || ''}"
                                       placeholder="256"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-300 mb-1">Max Predict (num_predict)</label>
                                <input type="number" name="num_predict" value="${ollamaOpts.num_predict || ''}"
                                       placeholder=""
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            </div>
                        </div>

                        <div class="border-t border-dark-border pt-4">
                            <h5 class="text-xs font-medium text-gray-500 mb-3">SAMPLING PARAMETERS</h5>
                            <div class="grid grid-cols-3 gap-4">
                                <div>
                                    <label class="block text-sm font-medium text-gray-300 mb-1">Top K</label>
                                    <input type="number" name="top_k" value="${ollamaOpts.top_k || ''}"
                                           placeholder="30"
                                           class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                </div>
                                <div>
                                    <label class="block text-sm font-medium text-gray-300 mb-1">Top P</label>
                                    <input type="number" name="top_p" value="${ollamaOpts.top_p || ''}"
                                           step="0.01" min="0" max="1"
                                           placeholder="0.85"
                                           class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                </div>
                                <div>
                                    <label class="block text-sm font-medium text-gray-300 mb-1">Repeat Penalty</label>
                                    <input type="number" name="repeat_penalty" value="${ollamaOpts.repeat_penalty || ''}"
                                           step="0.01" min="0.5" max="2"
                                           placeholder="1.08"
                                           class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                </div>
                            </div>
                        </div>

                        <div class="border-t border-dark-border pt-4">
                            <h5 class="text-xs font-medium text-gray-500 mb-3">MIROSTAT (ADAPTIVE SAMPLING)</h5>
                            <div class="grid grid-cols-3 gap-4">
                                <div>
                                    <label class="block text-sm font-medium text-gray-300 mb-1">Mirostat Mode</label>
                                    <select name="mirostat" class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                        <option value="" ${!ollamaOpts.mirostat ? 'selected' : ''}>Off</option>
                                        <option value="1" ${ollamaOpts.mirostat === 1 ? 'selected' : ''}>Mirostat 1</option>
                                        <option value="2" ${ollamaOpts.mirostat === 2 ? 'selected' : ''}>Mirostat 2.0 (Recommended)</option>
                                    </select>
                                </div>
                                <div>
                                    <label class="block text-sm font-medium text-gray-300 mb-1">Tau (Target Entropy)</label>
                                    <input type="number" name="mirostat_tau" value="${ollamaOpts.mirostat_tau || ''}"
                                           step="0.1" min="0" max="10"
                                           placeholder="5.0"
                                           class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                </div>
                                <div>
                                    <label class="block text-sm font-medium text-gray-300 mb-1">Eta (Learning Rate)</label>
                                    <input type="number" name="mirostat_eta" value="${ollamaOpts.mirostat_eta || ''}"
                                           step="0.01" min="0" max="1"
                                           placeholder="0.1"
                                           class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Description -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Description</label>
                        <textarea name="description" rows="2"
                                  class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white"
                                  placeholder="Configuration notes...">${config?.description || ''}</textarea>
                    </div>

                    <!-- Actions -->
                    <div class="flex justify-end gap-3 pt-4 border-t border-dark-border">
                        <button type="button" onclick="closeConfigModal()"
                                class="px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-300 hover:text-white transition-colors">
                            Cancel
                        </button>
                        <button type="submit"
                                class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                            ${isEdit ? 'Save Changes' : 'Create Configuration'}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', html);
}

/**
 * Close configuration modal
 */
function closeConfigModal() {
    const modal = document.getElementById('config-modal');
    if (modal) {
        modal.remove();
    }
}

/**
 * Apply preset to form
 */
function applyPreset(presetName) {
    const preset = presets[presetName];
    if (!preset) return;

    const form = document.getElementById('config-form');
    if (!form) return;

    // Apply temperature
    if (preset.temperature !== undefined) {
        form.querySelector('[name="temperature"]').value = preset.temperature;
    }

    // Apply Ollama options
    const ollamaOpts = preset.ollama_options || {};
    if (ollamaOpts.num_ctx) form.querySelector('[name="num_ctx"]').value = ollamaOpts.num_ctx;
    if (ollamaOpts.num_batch) form.querySelector('[name="num_batch"]').value = ollamaOpts.num_batch;
    if (ollamaOpts.top_k) form.querySelector('[name="top_k"]').value = ollamaOpts.top_k;
    if (ollamaOpts.top_p) form.querySelector('[name="top_p"]').value = ollamaOpts.top_p;
    if (ollamaOpts.repeat_penalty) form.querySelector('[name="repeat_penalty"]').value = ollamaOpts.repeat_penalty;

    // Mirostat settings
    if (ollamaOpts.mirostat !== undefined) {
        form.querySelector('[name="mirostat"]').value = ollamaOpts.mirostat;
    } else {
        form.querySelector('[name="mirostat"]').value = '';
    }
    if (ollamaOpts.mirostat_tau) form.querySelector('[name="mirostat_tau"]').value = ollamaOpts.mirostat_tau;
    if (ollamaOpts.mirostat_eta) form.querySelector('[name="mirostat_eta"]').value = ollamaOpts.mirostat_eta;

    // Update description
    const descField = form.querySelector('[name="description"]');
    if (descField && !descField.value) {
        descField.value = `${preset.name}: ${preset.description}`;
    }

    safeShowToast(`Applied "${preset.name}" preset`, 'success');
}

/**
 * Save model configuration
 */
async function saveModelConfig(event, configId) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);

    // Build Ollama options
    const ollamaOptions = {};
    const ollamaFields = ['num_ctx', 'num_batch', 'num_predict', 'top_k', 'top_p', 'repeat_penalty', 'mirostat', 'mirostat_tau', 'mirostat_eta'];

    ollamaFields.forEach(field => {
        const value = formData.get(field);
        if (value !== '' && value !== null) {
            if (field === 'mirostat') {
                ollamaOptions[field] = parseInt(value);
            } else if (['top_p', 'repeat_penalty', 'mirostat_tau', 'mirostat_eta'].includes(field)) {
                ollamaOptions[field] = parseFloat(value);
            } else {
                ollamaOptions[field] = parseInt(value);
            }
        }
    });

    const payload = {
        model_name: formData.get('model_name'),
        display_name: formData.get('display_name') || null,
        backend_type: formData.get('backend_type'),
        enabled: formData.get('enabled') === 'on',
        temperature: parseFloat(formData.get('temperature')),
        max_tokens: parseInt(formData.get('max_tokens')),
        ollama_options: ollamaOptions,
        description: formData.get('description') || null
    };

    try {
        const url = configId ? `/api/model-configs/${configId}` : '/api/model-configs';
        const method = configId ? 'PUT' : 'POST';

        const response = await fetch(url, {
            method,
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save configuration');
        }

        closeConfigModal();
        safeShowToast(configId ? 'Configuration updated' : 'Configuration created', 'success');
        await loadModelConfigs();

    } catch (error) {
        console.error('Failed to save config:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Toggle model configuration enabled state
 */
async function toggleModelConfig(configId) {
    try {
        const response = await fetch(`/api/model-configs/${configId}/toggle`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to toggle configuration');
        }

        const updated = await response.json();
        safeShowToast(`${updated.display_name || updated.model_name} ${updated.enabled ? 'enabled' : 'disabled'}`, 'success');
        await loadModelConfigs();

    } catch (error) {
        console.error('Failed to toggle config:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Delete model configuration
 */
async function deleteModelConfig(configId, modelName) {
    if (!confirm(`Delete configuration for "${modelName}"?`)) {
        return;
    }

    try {
        const response = await fetch(`/api/model-configs/${configId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete configuration');
        }

        safeShowToast(`Configuration for "${modelName}" deleted`, 'success');
        await loadModelConfigs();

    } catch (error) {
        console.error('Failed to delete config:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Show error state
 */
function showModelConfigError(message) {
    const container = document.getElementById('model-configs-container');
    if (container) {
        container.innerHTML = `
            <div class="bg-red-500/10 border border-red-500/30 rounded-xl p-6 text-center">
                <div class="text-3xl mb-3">⚠️</div>
                <h3 class="text-lg font-semibold text-red-400 mb-2">Failed to Load Configurations</h3>
                <p class="text-gray-400">${message}</p>
                <button onclick="loadModelConfigs()" class="mt-4 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium transition-colors">
                    Try Again
                </button>
            </div>
        `;
    }
}

/**
 * Initialize model config page
 */
function initModelConfigPage() {
    console.log('Initializing model config page');
    loadModelConfigs();
}

// Export for external use
if (typeof window !== 'undefined') {
    window.loadModelConfigs = loadModelConfigs;
    window.showAddConfigModal = showAddConfigModal;
    window.showEditConfigModal = showEditConfigModal;
    window.closeConfigModal = closeConfigModal;
    window.applyPreset = applyPreset;
    window.saveModelConfig = saveModelConfig;
    window.toggleModelConfig = toggleModelConfig;
    window.deleteModelConfig = deleteModelConfig;
    window.initModelConfigPage = initModelConfigPage;
}
