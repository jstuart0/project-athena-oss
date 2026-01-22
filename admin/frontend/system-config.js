/**
 * System Configuration Dashboard
 *
 * A unified, professional view of all system settings and their current state.
 * Aggregates HA Voice Optimizations, Conversation Settings, and Service Status.
 */

// Configuration data
let configData = {
    haOptimizations: [],
    conversationSettings: null,
    serviceStatus: {
        gateway: null,
        orchestrator: null,
        ollama: null
    },
    activePreset: null,
    lastRefresh: null
};

/**
 * Load all configuration data
 */
async function loadSystemConfig() {
    const container = document.getElementById('system-config-container');
    if (!container) return;

    // Show loading state
    container.innerHTML = `
        <div class="text-center text-gray-400 py-12">
            <div class="animate-pulse">
                <div class="text-4xl mb-4">⚙️</div>
                <p>Loading system configuration...</p>
            </div>
        </div>
    `;

    try {
        // Load all data in parallel
        await Promise.all([
            loadHAOptimizations(),
            loadConversationSettingsForConfig(),
            loadServiceStatus(),
            loadActivePreset()
        ]);

        configData.lastRefresh = new Date();
        renderSystemConfig();
    } catch (error) {
        console.error('Failed to load system configuration:', error);
        container.innerHTML = `
            <div class="text-center text-red-400 py-12">
                <div class="text-4xl mb-4">⚠️</div>
                <p>Failed to load configuration</p>
                <p class="text-sm text-gray-500 mt-2">${error.message}</p>
                <button onclick="loadSystemConfig()" class="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                    Retry
                </button>
            </div>
        `;
    }
}

/**
 * Load HA Voice Optimization flags
 */
async function loadHAOptimizations() {
    try {
        const response = await fetch('/api/features', {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to load features');

        const allFeatures = await response.json();

        // Filter to only HA optimization flags
        const haOptNames = [
            'ha_room_detection_cache',
            'ha_simple_command_fastpath',
            'ha_parallel_init',
            'ha_precomputed_summaries',
            'ha_session_warmup',
            'ha_intent_prerouting'
        ];

        configData.haOptimizations = allFeatures.filter(f => haOptNames.includes(f.name));
    } catch (error) {
        console.error('Failed to load HA optimizations:', error);
        configData.haOptimizations = [];
    }
}

/**
 * Load conversation settings
 */
async function loadConversationSettingsForConfig() {
    try {
        const response = await fetch('/api/conversation/settings', {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to load conversation settings');

        configData.conversationSettings = await response.json();
    } catch (error) {
        console.error('Failed to load conversation settings:', error);
        configData.conversationSettings = null;
    }
}

/**
 * Load service status via admin backend (avoids CORS issues)
 */
async function loadServiceStatus() {
    // Use /api/status endpoint which checks all services at once
    try {
        const response = await fetch('/api/status', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            const data = await response.json();

            // Parse service status from the response
            // Services are returned with names like "gateway (studio)", "orchestrator (studio)", "ollama (studio)"
            for (const service of data.services || []) {
                const name = service.name.toLowerCase();
                const status = service.healthy ? 'healthy' :
                              (service.status === 'error' || service.status === 'timeout') ? 'offline' : 'unhealthy';

                if (name.includes('gateway')) {
                    configData.serviceStatus.gateway = status;
                } else if (name.includes('orchestrator')) {
                    configData.serviceStatus.orchestrator = status;
                } else if (name.includes('ollama')) {
                    configData.serviceStatus.ollama = status;
                }
            }
        } else {
            // Fallback to offline if status endpoint fails
            configData.serviceStatus.gateway = 'offline';
            configData.serviceStatus.orchestrator = 'offline';
            configData.serviceStatus.ollama = 'offline';
        }
    } catch (error) {
        console.error('Failed to load service status:', error);
        configData.serviceStatus.gateway = 'offline';
        configData.serviceStatus.orchestrator = 'offline';
        configData.serviceStatus.ollama = 'offline';
    }
}

/**
 * Render the system configuration dashboard
 */
function renderSystemConfig() {
    const container = document.getElementById('system-config-container');
    if (!container) return;

    const lastRefreshTime = configData.lastRefresh
        ? configData.lastRefresh.toLocaleTimeString()
        : 'Never';

    container.innerHTML = `
        <!-- Active Preset Picker -->
        ${renderPresetPicker()}

        <!-- Header with refresh info -->
        <div class="flex justify-between items-center mb-6">
            <div>
                <p class="text-sm text-gray-500">Last updated: ${lastRefreshTime}</p>
            </div>
            <button onclick="loadSystemConfig()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-2">
                <i data-lucide="refresh-cw" class="w-4 h-4"></i> Refresh All
            </button>
        </div>

        <!-- Service Status Cards -->
        ${renderServiceStatus()}

        <!-- HA Voice Optimizations Section -->
        ${renderHAOptimizations()}

        <!-- Conversation Settings Section -->
        ${renderConversationSummary()}

        <!-- Quick Actions -->
        ${renderQuickActions()}
    `;
}

/**
 * Render service status cards
 */
function renderServiceStatus() {
    const services = [
        {
            name: 'Gateway',
            status: configData.serviceStatus.gateway,
            port: '8000',
            description: 'Voice request routing, fast-path commands'
        },
        {
            name: 'Orchestrator',
            status: configData.serviceStatus.orchestrator,
            port: '8001',
            description: 'Query processing, tool execution, LLM coordination'
        },
        {
            name: 'Ollama',
            status: configData.serviceStatus.ollama,
            port: '11434',
            description: 'Local LLM inference (qwen2.5, llama3.1)'
        }
    ];

    return `
        <div class="mb-8">
            <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span class="text-xl">🖥️</span> Service Status
            </h3>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                ${services.map(svc => {
                    const statusColor = svc.status === 'healthy' ? 'green' :
                                       svc.status === 'unhealthy' ? 'yellow' : 'red';
                    const statusIcon = svc.status === 'healthy' ? '✅' :
                                      svc.status === 'unhealthy' ? '⚠️' : '❌';
                    const statusText = svc.status === 'healthy' ? 'Healthy' :
                                      svc.status === 'unhealthy' ? 'Degraded' : 'Offline';

                    return `
                        <div class="bg-dark-card border border-dark-border rounded-xl p-4 hover:border-${statusColor}-500/50 transition-all">
                            <div class="flex items-start justify-between mb-2">
                                <h4 class="font-medium text-white">${svc.name}</h4>
                                <span class="text-xl">${statusIcon}</span>
                            </div>
                            <div class="flex items-center gap-2 mb-2">
                                <span class="w-2 h-2 rounded-full bg-${statusColor}-500 animate-pulse"></span>
                                <span class="text-sm text-${statusColor}-400">${statusText}</span>
                            </div>
                            <p class="text-xs text-gray-500">${svc.description}</p>
                            <p class="text-xs text-gray-600 mt-1">Port: ${svc.port}</p>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

/**
 * Render HA Voice Optimizations section
 */
function renderHAOptimizations() {
    if (configData.haOptimizations.length === 0) {
        return `
            <div class="mb-8">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span class="text-xl">⚡</span> HA Voice Optimizations
                </h3>
                <div class="bg-dark-card border border-dark-border rounded-xl p-6 text-center text-gray-400">
                    <p>No optimization flags found</p>
                    <p class="text-sm text-gray-500 mt-1">Run the migration to add optimization flags</p>
                </div>
            </div>
        `;
    }

    // Sort by priority
    const sorted = [...configData.haOptimizations].sort((a, b) => a.priority - b.priority);

    // Count enabled/disabled
    const enabledCount = sorted.filter(f => f.enabled).length;
    const totalCount = sorted.length;

    return `
        <div class="mb-8">
            <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span class="text-xl">⚡</span> HA Voice Optimizations
                <span class="ml-auto text-sm font-normal text-gray-400">
                    ${enabledCount}/${totalCount} enabled
                </span>
            </h3>
            <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
                <div class="divide-y divide-dark-border">
                    ${sorted.map((opt, index) => renderOptimizationRow(opt, index)).join('')}
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a single optimization row
 */
function renderOptimizationRow(opt, index) {
    const isEnabled = opt.enabled;
    const statusClass = isEnabled ? 'text-green-400' : 'text-gray-500';
    const bgClass = isEnabled ? 'bg-green-900/10' : '';
    const statusBadge = isEnabled
        ? '<span class="px-2 py-0.5 bg-green-900/50 text-green-400 rounded-full text-xs font-medium">ON</span>'
        : '<span class="px-2 py-0.5 bg-gray-800 text-gray-500 rounded-full text-xs font-medium">OFF</span>';

    // Estimated savings based on flag
    const savingsMap = {
        'ha_room_detection_cache': '100-200ms',
        'ha_parallel_init': '50-100ms',
        'ha_simple_command_fastpath': '300-2000ms',
        'ha_precomputed_summaries': '200-500ms',
        'ha_session_warmup': '50-100ms',
        'ha_intent_prerouting': '200-1000ms'
    };
    const savings = savingsMap[opt.name] || 'Variable';

    return `
        <div class="flex items-center justify-between p-4 ${bgClass} hover:bg-dark-bg/50 transition-colors">
            <div class="flex items-center gap-4">
                <div class="w-8 h-8 rounded-lg bg-dark-bg flex items-center justify-center text-gray-400 font-mono text-sm">
                    ${index + 1}
                </div>
                <div>
                    <div class="font-medium text-white flex items-center gap-2">
                        ${opt.display_name}
                        ${statusBadge}
                    </div>
                    <div class="text-sm text-gray-400 mt-0.5">${opt.description || ''}</div>
                </div>
            </div>
            <div class="flex items-center gap-4">
                <div class="text-right">
                    <div class="text-xs text-gray-500">Est. Savings</div>
                    <div class="text-sm ${isEnabled ? 'text-green-400' : 'text-gray-500'}">${savings}</div>
                </div>
                <label class="relative inline-flex items-center cursor-pointer">
                    <input type="checkbox"
                           class="sr-only peer"
                           ${isEnabled ? 'checked' : ''}
                           onchange="toggleOptimization(${opt.id}, this.checked)">
                    <div class="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer
                                peer-checked:after:translate-x-full peer-checked:after:border-white
                                after:content-[''] after:absolute after:top-[2px] after:left-[2px]
                                after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all
                                peer-checked:bg-green-600"></div>
                </label>
            </div>
        </div>
    `;
}

/**
 * Render conversation settings summary
 */
function renderConversationSummary() {
    const settings = configData.conversationSettings;

    if (!settings) {
        return `
            <div class="mb-8">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span class="text-xl">💬</span> Conversation Settings
                </h3>
                <div class="bg-dark-card border border-dark-border rounded-xl p-6 text-center text-gray-400">
                    <p>Unable to load conversation settings</p>
                </div>
            </div>
        `;
    }

    const historyModeColors = {
        'none': 'green',
        'summarized': 'blue',
        'full': 'purple'
    };
    const modeColor = historyModeColors[settings.history_mode] || 'gray';

    return `
        <div class="mb-8">
            <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span class="text-xl">💬</span> Conversation Settings
                <a href="javascript:showTab('conversation-context')" class="ml-auto text-sm text-blue-400 hover:text-blue-300">
                    Edit Settings →
                </a>
            </h3>
            <div class="bg-dark-card border border-dark-border rounded-xl p-6">
                <div class="grid grid-cols-2 md:grid-cols-4 gap-6">
                    <div>
                        <div class="text-xs text-gray-500 uppercase tracking-wide mb-1">Context</div>
                        <div class="flex items-center gap-2">
                            <span class="w-2 h-2 rounded-full ${settings.enabled ? 'bg-green-500' : 'bg-red-500'}"></span>
                            <span class="text-white font-medium">${settings.enabled ? 'Enabled' : 'Disabled'}</span>
                        </div>
                    </div>
                    <div>
                        <div class="text-xs text-gray-500 uppercase tracking-wide mb-1">History Mode</div>
                        <span class="px-2 py-1 bg-${modeColor}-900/30 text-${modeColor}-400 rounded text-sm font-medium capitalize">
                            ${settings.history_mode || 'full'}
                        </span>
                    </div>
                    <div>
                        <div class="text-xs text-gray-500 uppercase tracking-wide mb-1">Session Timeout</div>
                        <div class="text-white font-medium">${formatDuration(settings.timeout_seconds || 1800)}</div>
                    </div>
                    <div>
                        <div class="text-xs text-gray-500 uppercase tracking-wide mb-1">Max Messages</div>
                        <div class="text-white font-medium">${settings.max_messages || 20}</div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render quick actions
 */
function renderQuickActions() {
    return `
        <div class="mb-8">
            <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span class="text-xl">🚀</span> Quick Actions
            </h3>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                <button onclick="enableAllOptimizations()" class="bg-dark-card border border-dark-border rounded-xl p-4 hover:border-green-500/50 hover:bg-green-900/10 transition-all text-left group">
                    <div class="text-xl mb-2">⚡</div>
                    <div class="font-medium text-white group-hover:text-green-400">Enable All</div>
                    <div class="text-xs text-gray-500">Turn on all optimizations</div>
                </button>
                <button onclick="disableAllOptimizations()" class="bg-dark-card border border-dark-border rounded-xl p-4 hover:border-orange-500/50 hover:bg-orange-900/10 transition-all text-left group">
                    <div class="text-xl mb-2">🔒</div>
                    <div class="font-medium text-white group-hover:text-orange-400">Disable All</div>
                    <div class="text-xs text-gray-500">Revert to baseline</div>
                </button>
                <button onclick="invalidateAllCaches()" class="bg-dark-card border border-dark-border rounded-xl p-4 hover:border-blue-500/50 hover:bg-blue-900/10 transition-all text-left group">
                    <div class="mb-2"><i data-lucide="trash-2" class="w-5 h-5 text-gray-400 group-hover:text-blue-400"></i></div>
                    <div class="font-medium text-white group-hover:text-blue-400">Clear Caches</div>
                    <div class="text-xs text-gray-500">Invalidate all service caches</div>
                </button>
                <button onclick="showTab('features')" class="bg-dark-card border border-dark-border rounded-xl p-4 hover:border-purple-500/50 hover:bg-purple-900/10 transition-all text-left group">
                    <div class="text-xl mb-2">🎛️</div>
                    <div class="font-medium text-white group-hover:text-purple-400">All Features</div>
                    <div class="text-xs text-gray-500">View all feature flags</div>
                </button>
            </div>
        </div>
    `;
}

/**
 * Toggle optimization flag
 */
async function toggleOptimization(featureId, enabled) {
    try {
        const response = await fetch(`/api/features/${featureId}/toggle`, {
            method: 'PUT',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to toggle optimization');
        }

        const updated = await response.json();
        safeShowToast(`${updated.display_name} ${updated.enabled ? 'enabled' : 'disabled'}`, 'success');

        // Reload configuration
        await loadSystemConfig();

    } catch (error) {
        console.error('Failed to toggle optimization:', error);
        safeShowToast('Failed to toggle optimization', 'error');
        await loadSystemConfig(); // Reload to restore correct state
    }
}

/**
 * Enable all optimizations
 */
async function enableAllOptimizations() {
    try {
        for (const opt of configData.haOptimizations) {
            if (!opt.enabled) {
                await fetch(`/api/features/${opt.id}/toggle`, {
                    method: 'PUT',
                    headers: getAuthHeaders()
                });
            }
        }
        safeShowToast('All optimizations enabled', 'success');
        await loadSystemConfig();
    } catch (error) {
        console.error('Failed to enable all:', error);
        safeShowToast('Failed to enable all optimizations', 'error');
    }
}

/**
 * Disable all optimizations
 */
async function disableAllOptimizations() {
    try {
        for (const opt of configData.haOptimizations) {
            if (opt.enabled) {
                await fetch(`/api/features/${opt.id}/toggle`, {
                    method: 'PUT',
                    headers: getAuthHeaders()
                });
            }
        }
        safeShowToast('All optimizations disabled', 'success');
        await loadSystemConfig();
    } catch (error) {
        console.error('Failed to disable all:', error);
        safeShowToast('Failed to disable all optimizations', 'error');
    }
}

/**
 * Invalidate all service caches via admin backend
 */
async function invalidateAllCaches() {
    try {
        // Use admin backend to trigger cache invalidation
        const response = await fetch('/api/services/invalidate-caches', {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            safeShowToast('All service caches invalidated', 'success');
        } else {
            throw new Error('Failed to invalidate caches');
        }
    } catch (error) {
        console.error('Cache invalidation failed:', error);
        safeShowToast('Cache invalidation failed - service may not support this', 'warning');
    }
}

/**
 * Format duration in seconds to human readable
 */
function formatDuration(seconds) {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    return `${Math.floor(seconds / 3600)}h`;
}

/**
 * Load active preset
 */
async function loadActivePreset() {
    try {
        const response = await fetch('/api/presets/active', {
            headers: getAuthHeaders()
        });
        if (response.ok) {
            configData.activePreset = await response.json();
        } else {
            configData.activePreset = null;
        }
    } catch (error) {
        console.error('Failed to load active preset:', error);
        configData.activePreset = null;
    }
}

/**
 * Render preset picker section
 */
function renderPresetPicker() {
    const preset = configData.activePreset;

    const latencyColor = !preset?.estimated_latency_ms ? 'gray' :
                        preset.estimated_latency_ms < 2000 ? 'green' :
                        preset.estimated_latency_ms < 4000 ? 'yellow' : 'orange';

    return `
        <div class="mb-8 bg-gradient-to-r from-blue-900/20 to-purple-900/20 border border-blue-500/30 rounded-xl p-6">
            <div class="flex items-center justify-between flex-wrap gap-4">
                <div class="flex items-center gap-4">
                    <span class="text-3xl">${preset?.icon || '⚡'}</span>
                    <div>
                        <div class="text-xs text-gray-500 uppercase tracking-wide mb-1">Active Preset</div>
                        <h2 class="text-xl font-semibold text-white">${preset?.name || 'No preset active'}</h2>
                        <p class="text-sm text-gray-400 mt-1">${preset?.description || 'Configure a preset to get started'}</p>
                    </div>
                </div>
                <div class="flex items-center gap-3">
                    ${preset ? `
                        <div class="text-right mr-4">
                            <div class="text-xs text-gray-500">Est. Latency</div>
                            <div class="text-lg font-mono text-${latencyColor}-400">
                                ~${((preset.estimated_latency_ms || 0) / 1000).toFixed(1)}s
                            </div>
                        </div>
                    ` : ''}
                    <button onclick="captureCurrentAsPreset()" class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium flex items-center gap-2" title="Save current settings as a new preset">
                        <span>📸</span> Save Current
                    </button>
                    <button onclick="showPresetSwitcher()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium flex items-center gap-2">
                        <i data-lucide="refresh-cw" class="w-4 h-4"></i> Switch Preset
                    </button>
                    <a href="javascript:showTab('presets')" class="px-4 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 rounded-lg text-sm">
                        Manage →
                    </a>
                </div>
            </div>
        </div>
    `;
}

/**
 * Show preset switcher modal
 */
async function showPresetSwitcher() {
    try {
        const response = await fetch('/api/presets', {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to load presets');

        const presets = await response.json();

        const modal = document.createElement('div');
        modal.id = 'preset-switcher-modal';
        modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
        modal.innerHTML = `
            <div class="bg-dark-card border border-dark-border rounded-xl p-6 w-full max-w-lg">
                <h3 class="text-lg font-semibold text-white mb-4">Switch Preset</h3>
                <div class="space-y-2 max-h-96 overflow-y-auto">
                    ${presets.map(p => {
                        const latencyColor = !p.estimated_latency_ms ? 'gray' :
                                            p.estimated_latency_ms < 2000 ? 'green' :
                                            p.estimated_latency_ms < 4000 ? 'yellow' : 'orange';
                        return `
                            <button onclick="quickActivatePreset(${p.id})"
                                    class="w-full flex items-center gap-3 p-4 rounded-lg border ${p.is_active ? 'border-green-500 bg-green-900/20' : 'border-dark-border hover:border-blue-500/50 hover:bg-dark-bg'} transition-all text-left">
                                <span class="text-2xl">${p.icon || '⚡'}</span>
                                <div class="flex-1">
                                    <div class="font-medium text-white flex items-center gap-2">
                                        ${p.name}
                                        ${p.is_active ? '<span class="text-xs bg-green-600 text-white px-2 py-0.5 rounded-full">ACTIVE</span>' : ''}
                                    </div>
                                    <div class="text-sm text-gray-400">${p.description || ''}</div>
                                </div>
                                <div class="text-right">
                                    <div class="text-xs text-gray-500">Latency</div>
                                    <div class="text-sm text-${latencyColor}-400">~${((p.estimated_latency_ms || 0) / 1000).toFixed(1)}s</div>
                                </div>
                            </button>
                        `;
                    }).join('')}
                </div>
                <div class="flex justify-end mt-4 pt-4 border-t border-dark-border">
                    <button onclick="closePresetSwitcher()" class="px-4 py-2 bg-dark-bg hover:bg-dark-border text-gray-300 rounded-lg">
                        Cancel
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closePresetSwitcher();
        });
    } catch (error) {
        console.error('Failed to show preset switcher:', error);
        safeShowToast('Failed to load presets', 'error');
    }
}

/**
 * Close preset switcher modal
 */
function closePresetSwitcher() {
    const modal = document.getElementById('preset-switcher-modal');
    if (modal) modal.remove();
}

/**
 * Quickly activate a preset
 */
async function quickActivatePreset(presetId) {
    closePresetSwitcher();

    try {
        const response = await fetch(`/api/presets/${presetId}/activate`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error('Failed to activate preset');

        const preset = await response.json();
        safeShowToast(`Switched to "${preset.name}"`, 'success');

        await loadSystemConfig();
    } catch (error) {
        console.error('Failed to activate preset:', error);
        safeShowToast('Failed to switch preset', 'error');
    }
}

/**
 * Capture current settings as a new preset
 */
async function captureCurrentAsPreset() {
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

        await loadSystemConfig();
    } catch (error) {
        console.error('Failed to capture preset:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Initialize system configuration page
 */
function initSystemConfigPage() {
    console.log('Initializing System Configuration page');
    loadSystemConfig();

    // Auto-refresh every 30 seconds using RefreshManager
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('system-config-refresh', loadSystemConfig, 30000);
    } else {
        setInterval(loadSystemConfig, 30000);
    }
}

/**
 * Cleanup system configuration page
 */
function destroySystemConfigPage() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('system-config-refresh');
    }
}

// Export for external use
if (typeof window !== 'undefined') {
    window.initSystemConfigPage = initSystemConfigPage;
    window.destroySystemConfigPage = destroySystemConfigPage;
    window.loadSystemConfig = loadSystemConfig;
    window.toggleOptimization = toggleOptimization;
    window.enableAllOptimizations = enableAllOptimizations;
    window.disableAllOptimizations = disableAllOptimizations;
    window.invalidateAllCaches = invalidateAllCaches;
    window.showPresetSwitcher = showPresetSwitcher;
    window.closePresetSwitcher = closePresetSwitcher;
    window.quickActivatePreset = quickActivatePreset;
    window.captureCurrentAsPreset = captureCurrentAsPreset;
}
