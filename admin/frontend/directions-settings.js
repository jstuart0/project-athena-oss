/**
 * Directions Settings Management UI
 *
 * Provides configuration interface for the Directions RAG service:
 * - Default travel mode selection
 * - Transit preferences
 * - SMS and response options
 * - Cache and performance settings
 * - Waypoint/stops configuration
 */

let directionsSettings = [];

/**
 * Load directions settings from backend
 */
async function loadDirectionsSettings() {
    try {
        const response = await fetch('/api/directions-settings', {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load settings: ${response.statusText}`);
        }

        directionsSettings = await response.json();
        renderDirectionsSettings();
    } catch (error) {
        console.error('Failed to load directions settings:', error);
        safeShowToast('Failed to load directions settings', 'error');
        showDirectionsError(error.message);
    }
}

/**
 * Update a setting value
 */
async function updateDirectionsSetting(settingId, newValue) {
    try {
        const response = await fetch(`/api/directions-settings/${settingId}`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ setting_value: newValue })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        const updated = await response.json();

        // Update local data
        const index = directionsSettings.findIndex(s => s.id === settingId);
        if (index !== -1) {
            directionsSettings[index] = updated;
        }

        renderDirectionsSettings();
        safeShowToast(`Setting "${updated.display_name}" updated`, 'success');

    } catch (error) {
        console.error('Failed to update setting:', error);
        safeShowToast(error.message, 'error');
        await loadDirectionsSettings();
    }
}

/**
 * Reset all settings to defaults
 */
async function resetDirectionsSettings() {
    if (!confirm('Reset all directions settings to defaults?')) {
        return;
    }

    try {
        const response = await fetch('/api/directions-settings/reset', {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to reset settings');
        }

        await loadDirectionsSettings();
        safeShowToast('Settings reset to defaults', 'success');

    } catch (error) {
        console.error('Failed to reset settings:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Render settings grouped by category
 */
function renderDirectionsSettings() {
    const container = document.getElementById('directions-settings-container');
    if (!container) return;

    if (directionsSettings.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-12">
                <div class="text-4xl mb-4">&#128204;</div>
                <p class="text-lg">No directions settings configured</p>
                <p class="text-sm mt-2">Run the database migration to add settings</p>
            </div>
        `;
        return;
    }

    // Group by category
    const categories = {};
    directionsSettings.forEach(setting => {
        if (!categories[setting.category]) {
            categories[setting.category] = [];
        }
        categories[setting.category].push(setting);
    });

    const categoryNames = {
        'defaults': 'Default Values',
        'api': 'API Settings',
        'performance': 'Performance',
        'sms': 'SMS Options',
        'response': 'Response Format',
        'waypoints': 'Waypoints/Stops'
    };

    const categoryOrder = ['defaults', 'waypoints', 'sms', 'response', 'api', 'performance'];

    let html = `
        <div class="flex justify-between items-center mb-6">
            <div>
                <h2 class="text-xl font-semibold text-white">Directions Service Configuration</h2>
                <p class="text-gray-400 text-sm mt-1">Configure default behavior for route planning and navigation</p>
            </div>
            <button onclick="resetDirectionsSettings()"
                class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg transition-colors">
                Reset to Defaults
            </button>
        </div>
        <div class="grid gap-6 md:grid-cols-2">
    `;

    categoryOrder.forEach(category => {
        const settings = categories[category];
        if (!settings) return;

        html += `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 pb-2 border-b border-dark-border">
                    ${categoryNames[category] || category}
                </h3>
                <div class="space-y-4">
                    ${settings.map(setting => renderSettingRow(setting)).join('')}
                </div>
            </div>
        `;
    });

    html += '</div>';
    container.innerHTML = html;
}

/**
 * Render individual setting row
 */
function renderSettingRow(setting) {
    let inputHtml;

    if (setting.setting_type === 'boolean') {
        inputHtml = `
            <label class="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" class="sr-only peer"
                    ${setting.setting_value === true ? 'checked' : ''}
                    onchange="updateDirectionsSetting(${setting.id}, this.checked)"
                />
                <div class="w-11 h-6 bg-gray-600 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-blue-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600"></div>
            </label>
        `;
    } else if (setting.setting_key === 'default_travel_mode') {
        inputHtml = `
            <select onchange="updateDirectionsSetting(${setting.id}, this.value)"
                class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[140px]">
                <option value="driving" ${setting.raw_value === 'driving' ? 'selected' : ''}>Driving</option>
                <option value="walking" ${setting.raw_value === 'walking' ? 'selected' : ''}>Walking</option>
                <option value="bicycling" ${setting.raw_value === 'bicycling' ? 'selected' : ''}>Bicycling</option>
                <option value="transit" ${setting.raw_value === 'transit' ? 'selected' : ''}>Transit</option>
            </select>
        `;
    } else if (setting.setting_key === 'default_transit_mode') {
        inputHtml = `
            <select onchange="updateDirectionsSetting(${setting.id}, this.value)"
                class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[140px]">
                <option value="bus" ${setting.raw_value === 'bus' ? 'selected' : ''}>Bus</option>
                <option value="train" ${setting.raw_value === 'train' ? 'selected' : ''}>Train</option>
                <option value="subway" ${setting.raw_value === 'subway' ? 'selected' : ''}>Subway</option>
                <option value="tram" ${setting.raw_value === 'tram' ? 'selected' : ''}>Tram</option>
            </select>
        `;
    } else if (setting.setting_key === 'default_stop_position') {
        inputHtml = `
            <select onchange="updateDirectionsSetting(${setting.id}, this.value)"
                class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[140px]">
                <option value="beginning" ${setting.raw_value === 'beginning' ? 'selected' : ''}>Beginning</option>
                <option value="quarter" ${setting.raw_value === 'quarter' ? 'selected' : ''}>Quarter</option>
                <option value="halfway" ${setting.raw_value === 'halfway' ? 'selected' : ''}>Halfway</option>
                <option value="three_quarters" ${setting.raw_value === 'three_quarters' ? 'selected' : ''}>Three Quarters</option>
                <option value="end" ${setting.raw_value === 'end' ? 'selected' : ''}>End</option>
            </select>
        `;
    } else if (setting.setting_type === 'integer') {
        inputHtml = `
            <input type="number" value="${setting.raw_value}" min="1"
                onchange="updateDirectionsSetting(${setting.id}, this.value)"
                class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500 w-24"
            />
        `;
    } else {
        inputHtml = `
            <input type="text" value="${escapeHtml(setting.raw_value)}"
                onchange="updateDirectionsSetting(${setting.id}, this.value)"
                class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[140px]"
            />
        `;
    }

    return `
        <div class="flex justify-between items-center py-2">
            <div class="flex-1 pr-4">
                <div class="text-white font-medium">${escapeHtml(setting.display_name)}</div>
                <div class="text-gray-500 text-sm">${escapeHtml(setting.description || '')}</div>
            </div>
            <div class="flex-shrink-0">
                ${inputHtml}
            </div>
        </div>
    `;
}

/**
 * Show error state
 */
function showDirectionsError(message) {
    const container = document.getElementById('directions-settings-container');
    if (container) {
        container.innerHTML = `
            <div class="text-center text-red-400 py-12">
                <div class="text-4xl mb-4">&#9888;</div>
                <p class="text-lg">Error loading settings</p>
                <p class="text-sm mt-2">${escapeHtml(message)}</p>
                <button onclick="loadDirectionsSettings()"
                    class="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Retry
                </button>
            </div>
        `;
    }
}

/**
 * Initialize directions settings page
 */
function initDirectionsSettingsPage() {
    console.log('Initializing directions settings page');
    loadDirectionsSettings();
    loadOriginPlaceholderSettings();
}

// ============================================================================
// Origin Placeholder Settings (System-wide)
// ============================================================================

let originPlaceholderPatterns = [];

/**
 * Load origin placeholder patterns from settings API
 */
async function loadOriginPlaceholderSettings() {
    try {
        const response = await fetch('/api/settings/directions-origin-placeholders', {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load placeholder settings: ${response.statusText}`);
        }

        const data = await response.json();
        originPlaceholderPatterns = data.patterns_list || [];
        renderOriginPlaceholderSettings(data);
    } catch (error) {
        console.error('Failed to load origin placeholder settings:', error);
        renderOriginPlaceholderError(error.message);
    }
}

/**
 * Save origin placeholder patterns
 */
async function saveOriginPlaceholderPatterns() {
    const input = document.getElementById('origin-placeholder-input');
    if (!input) return;

    try {
        const response = await fetch('/api/settings/directions-origin-placeholders', {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ placeholder_patterns: input.value })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save placeholder patterns');
        }

        const result = await response.json();
        safeShowToast(result.message, 'success');
        await loadOriginPlaceholderSettings();

    } catch (error) {
        console.error('Failed to save placeholder patterns:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Add a new placeholder pattern
 */
function addPlaceholderPattern() {
    const input = document.getElementById('new-placeholder-input');
    if (!input || !input.value.trim()) return;

    const newPattern = input.value.trim().toLowerCase();
    if (originPlaceholderPatterns.includes(newPattern)) {
        safeShowToast('Pattern already exists', 'warning');
        return;
    }

    originPlaceholderPatterns.push(newPattern);
    updatePlaceholderInput();
    input.value = '';
    saveOriginPlaceholderPatterns();
}

/**
 * Remove a placeholder pattern
 */
function removePlaceholderPattern(pattern) {
    originPlaceholderPatterns = originPlaceholderPatterns.filter(p => p !== pattern);
    updatePlaceholderInput();
    saveOriginPlaceholderPatterns();
}

/**
 * Update hidden input with comma-separated patterns
 */
function updatePlaceholderInput() {
    const input = document.getElementById('origin-placeholder-input');
    if (input) {
        input.value = originPlaceholderPatterns.join(',');
    }
}

/**
 * Render origin placeholder settings section
 */
function renderOriginPlaceholderSettings(data) {
    const container = document.getElementById('origin-placeholder-container');
    if (!container) return;

    const patterns = data.patterns_list || [];

    container.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 mt-6">
            <div class="flex justify-between items-start mb-4 pb-2 border-b border-dark-border">
                <div>
                    <h3 class="text-lg font-semibold text-white">Origin Placeholder Patterns</h3>
                    <p class="text-gray-400 text-sm mt-1">
                        When LLMs use these placeholder values as the origin in direction requests,
                        they are replaced with the user's actual current location.
                    </p>
                </div>
                <span class="text-blue-400 text-sm bg-blue-900/30 px-2 py-1 rounded">
                    ${patterns.length} patterns
                </span>
            </div>

            <div class="mb-4">
                <div class="flex flex-wrap gap-2">
                    ${patterns.map(pattern => `
                        <span class="inline-flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 text-white rounded-full text-sm">
                            "${escapeHtml(pattern)}"
                            <button onclick="removePlaceholderPattern('${escapeHtml(pattern)}')"
                                class="text-gray-400 hover:text-red-400 transition-colors ml-1">
                                &times;
                            </button>
                        </span>
                    `).join('')}
                </div>
            </div>

            <div class="flex gap-2">
                <input type="text" id="new-placeholder-input"
                    placeholder="Add new pattern (e.g., 'from here')"
                    class="flex-1 px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                    onkeypress="if(event.key === 'Enter') addPlaceholderPattern()"
                />
                <button onclick="addPlaceholderPattern()"
                    class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors">
                    Add Pattern
                </button>
            </div>

            <input type="hidden" id="origin-placeholder-input" value="${patterns.join(',')}" />

            <div class="mt-4 p-3 bg-gray-800/50 rounded-lg border border-gray-700">
                <p class="text-gray-400 text-sm">
                    <strong class="text-white">How it works:</strong> When a user asks "Give me directions to Baltimore"
                    and the LLM sets <code class="text-blue-400">origin: "current location"</code>, the orchestrator
                    detects this placeholder and replaces it with the user's actual GPS location or search location override.
                </p>
            </div>
        </div>
    `;
}

/**
 * Render error state for origin placeholder settings
 */
function renderOriginPlaceholderError(message) {
    const container = document.getElementById('origin-placeholder-container');
    if (container) {
        container.innerHTML = `
            <div class="bg-dark-card border border-red-500/30 rounded-lg p-6 mt-6">
                <div class="text-red-400">
                    <strong>Error loading origin placeholder settings:</strong> ${escapeHtml(message)}
                </div>
                <button onclick="loadOriginPlaceholderSettings()"
                    class="mt-3 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Retry
                </button>
            </div>
        `;
    }
}

// Export for external use
if (typeof window !== 'undefined') {
    window.loadDirectionsSettings = loadDirectionsSettings;
    window.updateDirectionsSetting = updateDirectionsSetting;
    window.resetDirectionsSettings = resetDirectionsSettings;
    window.initDirectionsSettingsPage = initDirectionsSettingsPage;
    window.loadOriginPlaceholderSettings = loadOriginPlaceholderSettings;
    window.saveOriginPlaceholderPatterns = saveOriginPlaceholderPatterns;
    window.addPlaceholderPattern = addPlaceholderPattern;
    window.removePlaceholderPattern = removePlaceholderPattern;
}

console.log('Directions Settings JS loaded');
