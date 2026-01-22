// ============================================================================
// CRUD EXTENSIONS FOR ADMIN UI
// This file contains complete CRUD implementations for:
// - Validation Models
// - Hallucination Checks
// - Multi-Intent Configuration
// ============================================================================

// ============================================================================
// VALIDATION MODELS CRUD
// ============================================================================

function showCreateValidationModelModal() {
    const modal = document.createElement('div');
    modal.id = 'create-validation-model-modal';
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-3xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div class="flex justify-between items-center mb-6">
                <h2 class="text-xl font-semibold text-white">Add Validation Model</h2>
                <button onclick="closeModal('create-validation-model-modal')" class="text-gray-400 hover:text-white">✕</button>
            </div>

            <form onsubmit="createValidationModel(event)" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Name *</label>
                        <input type="text" id="vm-name" required
                            placeholder="e.g., phi3-primary"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Model ID *</label>
                        <input type="text" id="vm-model-id" required
                            placeholder="e.g., phi3:mini"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Model Type *</label>
                        <select id="vm-model-type" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            <option value="primary">Primary</option>
                            <option value="validation">Validation</option>
                            <option value="fallback">Fallback</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Endpoint URL *</label>
                        <input type="text" id="vm-endpoint" required
                            placeholder="http://localhost:11434"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Categories (comma-separated)</label>
                    <input type="text" id="vm-categories"
                        placeholder="home_control, weather, sports"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="grid grid-cols-3 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Temperature</label>
                        <input type="number" id="vm-temperature" value="0.1" step="0.1" min="0" max="2"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Max Tokens</label>
                        <input type="number" id="vm-max-tokens" value="200"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Timeout (seconds)</label>
                        <input type="number" id="vm-timeout" value="30"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Weight</label>
                        <input type="number" id="vm-weight" value="1.0" step="0.1" min="0" max="1"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Min Confidence</label>
                        <input type="number" id="vm-min-confidence" value="0.7" step="0.1" min="0" max="1"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div class="flex items-center gap-2">
                    <input type="checkbox" id="vm-enabled" checked
                        class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                    <label for="vm-enabled" class="text-sm text-gray-400">Enable model</label>
                </div>

                <div class="flex justify-end gap-3 pt-4">
                    <button type="button" onclick="closeModal('create-validation-model-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm transition-colors">
                        Cancel
                    </button>
                    <button type="submit"
                        class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm transition-colors">
                        Create Model
                    </button>
                </div>
            </form>
        </div>
    `;
    document.body.appendChild(modal);
}

async function createValidationModel(event) {
    event.preventDefault();

    const categories = document.getElementById('vm-categories').value
        .split(',')
        .map(c => c.trim())
        .filter(c => c);

    const data = {
        name: document.getElementById('vm-name').value,
        model_id: document.getElementById('vm-model-id').value,
        model_type: document.getElementById('vm-model-type').value,
        endpoint_url: document.getElementById('vm-endpoint').value,
        use_for_categories: categories,
        temperature: parseFloat(document.getElementById('vm-temperature').value),
        max_tokens: parseInt(document.getElementById('vm-max-tokens').value),
        timeout_seconds: parseInt(document.getElementById('vm-timeout').value),
        weight: parseFloat(document.getElementById('vm-weight').value),
        min_confidence_required: parseFloat(document.getElementById('vm-min-confidence').value),
        enabled: document.getElementById('vm-enabled').checked
    };

    try {
        await apiRequest('/api/validation-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        closeModal('create-validation-model-modal');
        loadValidationModels();
        showSuccess('Validation model created successfully');
    } catch (error) {
        showError(`Failed to create validation model: ${error.message}`);
    }
}

async function deleteValidationModel(modelId, modelName) {
    if (!confirm(`Are you sure you want to delete the validation model "${modelName}"?`)) {
        return;
    }

    try {
        await apiRequest(`/api/validation-models/${modelId}`, { method: 'DELETE' });
        loadValidationModels();
        showSuccess('Validation model deleted successfully');
    } catch (error) {
        showError(`Failed to delete validation model: ${error.message}`);
    }
}

// ============================================================================
// HALLUCINATION CHECKS CRUD
// ============================================================================

function showCreateHallucinationCheckModal() {
    const modal = document.createElement('div');
    modal.id = 'create-hallucination-check-modal';
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-3xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div class="flex justify-between items-center mb-6">
                <h2 class="text-xl font-semibold text-white">Add Hallucination Check</h2>
                <button onclick="closeModal('create-hallucination-check-modal')" class="text-gray-400 hover:text-white">✕</button>
            </div>

            <form onsubmit="createHallucinationCheck(event)" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Name *</label>
                        <input type="text" id="hc-name" required
                            placeholder="e.g., weather_location_required"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Display Name *</label>
                        <input type="text" id="hc-display-name" required
                            placeholder="e.g., Weather Location Check"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Description</label>
                    <input type="text" id="hc-description"
                        placeholder="Describe what this check validates"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Check Type *</label>
                        <select id="hc-check-type" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            <option value="required_elements">Required Elements</option>
                            <option value="fact_checking">Fact Checking</option>
                            <option value="confidence_threshold">Confidence Threshold</option>
                            <option value="cross_validation">Cross Validation</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Severity *</label>
                        <select id="hc-severity" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            <option value="error">Error</option>
                            <option value="warning" selected>Warning</option>
                            <option value="info">Info</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Priority</label>
                        <input type="number" id="hc-priority" value="100"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Categories (comma-separated)</label>
                    <input type="text" id="hc-categories"
                        placeholder="weather, home_control, sports"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Configuration (JSON)</label>
                    <textarea id="hc-configuration" rows="3"
                        placeholder='{"required_fields": ["location"]}'
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white font-mono text-sm">{}</textarea>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Error Message Template</label>
                    <input type="text" id="hc-error-message"
                        placeholder="Response failed validation check"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Confidence Threshold</label>
                        <input type="number" id="hc-confidence" value="0.7" step="0.1" min="0" max="1"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div class="space-y-2">
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="hc-enabled" checked
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="hc-enabled" class="text-sm text-gray-400">Enable check</label>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="hc-cross-model"
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="hc-cross-model" class="text-sm text-gray-400">Require cross-model validation</label>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="hc-auto-fix"
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="hc-auto-fix" class="text-sm text-gray-400">Enable auto-fix</label>
                    </div>
                </div>

                <div class="flex justify-end gap-3 pt-4">
                    <button type="button" onclick="closeModal('create-hallucination-check-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm transition-colors">
                        Cancel
                    </button>
                    <button type="submit"
                        class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm transition-colors">
                        Create Check
                    </button>
                </div>
            </form>
        </div>
    `;
    document.body.appendChild(modal);
}

async function createHallucinationCheck(event) {
    event.preventDefault();

    const categories = document.getElementById('hc-categories').value
        .split(',')
        .map(c => c.trim())
        .filter(c => c);

    let configuration = {};
    try {
        configuration = JSON.parse(document.getElementById('hc-configuration').value);
    } catch (e) {
        showError('Invalid JSON in configuration field');
        return;
    }

    const data = {
        name: document.getElementById('hc-name').value,
        display_name: document.getElementById('hc-display-name').value,
        description: document.getElementById('hc-description').value || null,
        check_type: document.getElementById('hc-check-type').value,
        severity: document.getElementById('hc-severity').value,
        priority: parseInt(document.getElementById('hc-priority').value),
        applies_to_categories: categories,
        configuration: configuration,
        error_message_template: document.getElementById('hc-error-message').value || null,
        confidence_threshold: parseFloat(document.getElementById('hc-confidence').value),
        enabled: document.getElementById('hc-enabled').checked,
        require_cross_model_validation: document.getElementById('hc-cross-model').checked,
        auto_fix_enabled: document.getElementById('hc-auto-fix').checked
    };

    try {
        await apiRequest('/api/hallucination-checks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        closeModal('create-hallucination-check-modal');
        loadHallucinationChecks();
        showSuccess('Hallucination check created successfully');
    } catch (error) {
        showError(`Failed to create hallucination check: ${error.message}`);
    }
}

async function deleteHallucinationCheck(checkId, checkName) {
    if (!confirm(`Are you sure you want to delete the hallucination check "${checkName}"?`)) {
        return;
    }

    try {
        await apiRequest(`/api/hallucination-checks/${checkId}`, { method: 'DELETE' });
        loadHallucinationChecks();
        showSuccess('Hallucination check deleted successfully');
    } catch (error) {
        showError(`Failed to delete hallucination check: ${error.message}`);
    }
}

// ============================================================================
// MULTI-INTENT CONFIG EDIT
// ============================================================================

function showEditMultiIntentConfigModal(config) {
    const modal = document.createElement('div');
    modal.id = 'edit-multi-intent-config-modal';
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div class="flex justify-between items-center mb-6">
                <h2 class="text-xl font-semibold text-white">Edit Multi-Intent Configuration</h2>
                <button onclick="closeModal('edit-multi-intent-config-modal')" class="text-gray-400 hover:text-white">✕</button>
            </div>

            <form onsubmit="updateMultiIntentConfig(event)" class="space-y-4">
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Max Intents Per Query</label>
                        <input type="number" id="mi-max-intents" value="${config.max_intents_per_query}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Min Words Per Intent</label>
                        <input type="number" id="mi-min-words" value="${config.min_words_per_intent}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Combination Strategy</label>
                    <select id="mi-strategy"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        <option value="concatenate" ${config.combination_strategy === 'concatenate' ? 'selected' : ''}>Concatenate</option>
                        <option value="priority" ${config.combination_strategy === 'priority' ? 'selected' : ''}>Priority</option>
                        <option value="parallel" ${config.combination_strategy === 'parallel' ? 'selected' : ''}>Parallel</option>
                    </select>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Separators (comma-separated)</label>
                    <input type="text" id="mi-separators" value="${config.separators.join(', ')}"
                        placeholder="and, then, also"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Context Words to Preserve (comma-separated)</label>
                    <input type="text" id="mi-context-words" value="${config.context_words_to_preserve.join(', ')}"
                        placeholder="the, my, in, at, to"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="space-y-2">
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="mi-enabled" ${config.enabled ? 'checked' : ''}
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="mi-enabled" class="text-sm text-gray-400">Enable multi-intent processing</label>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="mi-context-preservation" ${config.context_preservation ? 'checked' : ''}
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="mi-context-preservation" class="text-sm text-gray-400">Preserve context between intents</label>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="mi-parallel" ${config.parallel_processing ? 'checked' : ''}
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="mi-parallel" class="text-sm text-gray-400">Enable parallel processing</label>
                    </div>
                </div>

                <div class="flex justify-end gap-3 pt-4">
                    <button type="button" onclick="closeModal('edit-multi-intent-config-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm transition-colors">
                        Cancel
                    </button>
                    <button type="submit"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm transition-colors">
                        Save Changes
                    </button>
                </div>
            </form>
        </div>
    `;
    document.body.appendChild(modal);
}

async function updateMultiIntentConfig(event) {
    event.preventDefault();

    const separators = document.getElementById('mi-separators').value
        .split(',')
        .map(s => s.trim())
        .filter(s => s);

    const contextWords = document.getElementById('mi-context-words').value
        .split(',')
        .map(w => w.trim())
        .filter(w => w);

    const data = {
        enabled: document.getElementById('mi-enabled').checked,
        max_intents_per_query: parseInt(document.getElementById('mi-max-intents').value),
        min_words_per_intent: parseInt(document.getElementById('mi-min-words').value),
        combination_strategy: document.getElementById('mi-strategy').value,
        separators: separators,
        context_words_to_preserve: contextWords,
        context_preservation: document.getElementById('mi-context-preservation').checked,
        parallel_processing: document.getElementById('mi-parallel').checked
    };

    try {
        await apiRequest('/api/multi-intent/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        closeModal('edit-multi-intent-config-modal');
        loadMultiIntentConfig();
        showSuccess('Multi-intent configuration updated successfully');
    } catch (error) {
        showError(`Failed to update multi-intent config: ${error.message}`);
    }
}
