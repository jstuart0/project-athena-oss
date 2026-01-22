/**
 * Escalation Presets Management
 *
 * Manages model escalation behavior through configurable presets and rules.
 * Allows switching between presets, creating custom rules, and manual overrides.
 */

const ESCALATION_API_BASE = '/api/escalation';
const ESCALATION_REFRESH_INTERVAL = 30000;  // 30 seconds

let escalationPresetsData = [];
let escalationStatsData = null;
let recentEventsData = [];
let selectedStatsPeriod = 24;  // hours

// ============== Metrics Loading ==============

async function loadEscalationStats(hours = null) {
    if (hours !== null) {
        selectedStatsPeriod = hours;
    }
    try {
        const response = await fetch(`${ESCALATION_API_BASE}/events/stats?hours=${selectedStatsPeriod}`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to load stats');
        escalationStatsData = await response.json();
        renderMetricsUI();
    } catch (error) {
        console.error('Error loading escalation stats:', error);
    }
}

async function loadRecentEvents() {
    try {
        const response = await fetch(`${ESCALATION_API_BASE}/events/recent?limit=15`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to load recent events');
        recentEventsData = await response.json();
        renderRecentEventsUI();
    } catch (error) {
        console.error('Error loading recent events:', error);
    }
}

function renderMetricsUI() {
    const container = document.getElementById('metrics-container');
    if (!container || !escalationStatsData) return;

    const stats = escalationStatsData;
    const triggerTypes = Object.entries(stats.by_trigger_type || {}).sort((a, b) => b[1] - a[1]);
    const modelTypes = Object.entries(stats.by_target_model || {}).sort((a, b) => b[1] - a[1]);

    container.innerHTML = `
        <!-- Period Selector -->
        <div class="flex items-center justify-between mb-4">
            <h3 class="text-lg font-semibold text-white">Escalation Metrics</h3>
            <div class="flex gap-2">
                <button onclick="loadEscalationStats(1)" class="px-3 py-1 text-sm rounded ${selectedStatsPeriod === 1 ? 'bg-blue-600 text-white' : 'bg-dark-bg text-gray-400 hover:text-white'}">1h</button>
                <button onclick="loadEscalationStats(6)" class="px-3 py-1 text-sm rounded ${selectedStatsPeriod === 6 ? 'bg-blue-600 text-white' : 'bg-dark-bg text-gray-400 hover:text-white'}">6h</button>
                <button onclick="loadEscalationStats(24)" class="px-3 py-1 text-sm rounded ${selectedStatsPeriod === 24 ? 'bg-blue-600 text-white' : 'bg-dark-bg text-gray-400 hover:text-white'}">24h</button>
                <button onclick="loadEscalationStats(72)" class="px-3 py-1 text-sm rounded ${selectedStatsPeriod === 72 ? 'bg-blue-600 text-white' : 'bg-dark-bg text-gray-400 hover:text-white'}">3d</button>
                <button onclick="loadEscalationStats(168)" class="px-3 py-1 text-sm rounded ${selectedStatsPeriod === 168 ? 'bg-blue-600 text-white' : 'bg-dark-bg text-gray-400 hover:text-white'}">7d</button>
            </div>
        </div>

        <!-- Key Stats Cards -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
                <div class="text-3xl font-bold text-white">${stats.total_escalations || 0}</div>
                <div class="text-sm text-gray-400">Total Escalations</div>
            </div>
            <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
                <div class="text-3xl font-bold text-white">${stats.unique_sessions || 0}</div>
                <div class="text-sm text-gray-400">Unique Sessions</div>
            </div>
            <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
                <div class="text-3xl font-bold text-yellow-400">${stats.by_target_model?.complex || 0}</div>
                <div class="text-sm text-gray-400">→ Complex</div>
            </div>
            <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
                <div class="text-3xl font-bold text-red-400">${stats.by_target_model?.super_complex || 0}</div>
                <div class="text-sm text-gray-400">→ Super Complex</div>
            </div>
        </div>

        <!-- Breakdown Charts -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <!-- By Trigger Type -->
            <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
                <h4 class="text-sm font-medium text-white mb-3">By Trigger Type</h4>
                ${triggerTypes.length === 0 ? `
                    <p class="text-sm text-gray-500 italic">No escalations in this period</p>
                ` : `
                    <div class="space-y-2">
                        ${triggerTypes.map(([type, count]) => {
                            const pct = stats.total_escalations > 0 ? Math.round((count / stats.total_escalations) * 100) : 0;
                            return `
                                <div class="flex items-center gap-3">
                                    <div class="flex-1">
                                        <div class="flex justify-between text-sm mb-1">
                                            <span class="text-gray-300">${formatTriggerType(type)}</span>
                                            <span class="text-gray-400">${count} (${pct}%)</span>
                                        </div>
                                        <div class="h-2 bg-dark-bg rounded-full overflow-hidden">
                                            <div class="h-full bg-blue-500 rounded-full" style="width: ${pct}%"></div>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                `}
            </div>

            <!-- Top Rules -->
            <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
                <h4 class="text-sm font-medium text-white mb-3">Top Triggered Rules</h4>
                ${(stats.top_rules || []).length === 0 ? `
                    <p class="text-sm text-gray-500 italic">No escalations in this period</p>
                ` : `
                    <div class="space-y-2">
                        ${stats.top_rules.slice(0, 5).map((rule, i) => `
                            <div class="flex items-center justify-between p-2 bg-dark-bg rounded">
                                <div class="flex items-center gap-2">
                                    <span class="text-xs text-gray-500">#${i + 1}</span>
                                    <span class="text-sm text-white">${rule.rule_name}</span>
                                    <span class="px-1.5 py-0.5 text-xs rounded bg-blue-900/30 text-blue-400">${rule.trigger_type}</span>
                                </div>
                                <span class="text-sm font-medium text-gray-300">${rule.count}</span>
                            </div>
                        `).join('')}
                    </div>
                `}
            </div>
        </div>
    `;
}

function renderRecentEventsUI() {
    const container = document.getElementById('recent-events-container');
    if (!container) return;

    container.innerHTML = `
        <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
            <div class="flex items-center justify-between mb-3">
                <h4 class="text-sm font-medium text-white">Recent Escalation Events</h4>
                <button onclick="loadRecentEvents()" class="text-xs text-blue-400 hover:text-blue-300">Refresh</button>
            </div>
            ${recentEventsData.length === 0 ? `
                <p class="text-sm text-gray-500 italic text-center py-4">No recent escalation events</p>
            ` : `
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="text-left text-gray-400 border-b border-dark-border">
                                <th class="pb-2 font-medium">Time</th>
                                <th class="pb-2 font-medium">Session</th>
                                <th class="pb-2 font-medium">Event</th>
                                <th class="pb-2 font-medium">Rule</th>
                                <th class="pb-2 font-medium">Target</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-dark-border">
                            ${recentEventsData.map(event => `
                                <tr class="text-gray-300">
                                    <td class="py-2 text-xs text-gray-400">${formatEventTime(event.created_at)}</td>
                                    <td class="py-2 font-mono text-xs">${(event.session_id || '').substring(0, 15)}...</td>
                                    <td class="py-2">
                                        <span class="px-1.5 py-0.5 text-xs rounded ${getEventTypeBadgeClass(event.event_type)}">
                                            ${event.event_type}
                                        </span>
                                    </td>
                                    <td class="py-2 text-xs">${event.rule_name || '-'}</td>
                                    <td class="py-2">
                                        <span class="px-1.5 py-0.5 text-xs rounded ${event.to_model === 'super_complex' ? 'bg-red-900/30 text-red-400' : 'bg-yellow-900/30 text-yellow-400'}">
                                            ${event.to_model || '-'}
                                        </span>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `}
        </div>
    `;
}

function formatTriggerType(type) {
    const labels = {
        'clarification': 'Clarification',
        'user_correction': 'User Correction',
        'user_frustration': 'User Frustration',
        'explicit_request': 'Explicit Request',
        'empty_results': 'Empty Results',
        'tool_failure': 'Tool Failure',
        'short_response': 'Short Response',
        'short_query': 'Short Query',
        'long_query': 'Long Query',
        'repeated_query': 'Repeated Query',
        'always': 'Always'
    };
    return labels[type] || type;
}

function formatEventTime(isoString) {
    if (!isoString) return '-';
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffMins < 1440) return `${Math.floor(diffMins / 60)}h ago`;
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function getEventTypeBadgeClass(eventType) {
    switch (eventType) {
        case 'escalation': return 'bg-orange-900/30 text-orange-400';
        case 'de-escalation': return 'bg-green-900/30 text-green-400';
        case 'manual_override': return 'bg-purple-900/30 text-purple-400';
        case 'override_cancelled': return 'bg-gray-700 text-gray-300';
        default: return 'bg-gray-700 text-gray-300';
    }
}

// ============== Loading ==============

async function loadEscalationPresets() {
    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to load presets');
        escalationPresetsData = await response.json();
        renderPresetsUI();
    } catch (error) {
        console.error('Error loading presets:', error);
        const container = document.getElementById('escalation-container');
        if (container) {
            container.innerHTML = `
                <div class="text-center text-red-400 py-8">
                    <p>Failed to load escalation presets</p>
                    <button onclick="loadEscalationPresets()" class="mt-2 px-4 py-2 bg-blue-600 rounded text-white">Retry</button>
                </div>
            `;
        }
    }
}

// ============== Rendering ==============

function renderPresetsUI() {
    const container = document.getElementById('escalation-container');
    if (!container) return;

    const activePreset = escalationPresetsData.find(p => p.is_active);
    const inactivePresets = escalationPresetsData.filter(p => !p.is_active);

    container.innerHTML = `
        <!-- Active Preset Section -->
        <div class="mb-6">
            <div class="flex items-center justify-between mb-3">
                <h3 class="text-lg font-semibold text-white">Active Preset</h3>
                <button onclick="showCreateEscalationPresetModal()"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors">
                    + New Preset
                </button>
            </div>
            ${activePreset ? renderEscalationPresetCard(activePreset, true) : `
                <div class="p-4 bg-dark-card border border-dark-border rounded-lg text-center">
                    <p class="text-gray-400">No active preset. Activate one below.</p>
                </div>
            `}
        </div>

        <!-- Other Presets -->
        <div>
            <h3 class="text-lg font-semibold text-white mb-3">Available Presets</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                ${inactivePresets.map(p => renderEscalationPresetCard(p, false)).join('')}
            </div>
        </div>
    `;

    // Re-initialize Lucide icons
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
}

function renderEscalationPresetCard(preset, isActive) {
    const rules = preset.rules || [];
    const enabledRules = rules.filter(r => r.enabled);
    const autoActivate = preset.auto_activate_conditions;

    return `
        <div class="p-4 bg-dark-card border ${isActive ? 'border-green-600' : 'border-dark-border'} rounded-lg">
            <div class="flex items-start justify-between mb-3">
                <div class="flex-1">
                    <div class="flex items-center gap-2">
                        <h4 class="font-semibold text-white">${preset.name}</h4>
                        ${isActive ? `
                            <span class="px-2 py-0.5 text-xs rounded bg-green-900/30 text-green-400">Active</span>
                        ` : ''}
                        ${autoActivate ? `
                            <span class="px-2 py-0.5 text-xs rounded bg-blue-900/30 text-blue-400" title="${formatAutoActivate(autoActivate)}">
                                Auto
                            </span>
                        ` : ''}
                    </div>
                    <p class="text-sm text-gray-400 mt-1">${preset.description || 'No description'}</p>
                </div>
                <div class="flex gap-1">
                    ${!isActive ? `
                        <button onclick="activateEscalationPreset(${preset.id})"
                                class="px-3 py-1 bg-green-600 hover:bg-green-700 text-white rounded text-sm"
                                title="Activate">
                            Activate
                        </button>
                    ` : ''}
                    <button onclick="showEditEscalationPresetModal(${preset.id})"
                            class="px-2 py-1 bg-blue-600 text-white hover:bg-blue-700 rounded text-xs font-bold" title="Edit">
                        Edit
                    </button>
                    <button onclick="showCloneEscalationPresetModal(${preset.id}, '${preset.name.replace(/'/g, "\\'")}')"
                            class="p-1.5 text-purple-400 hover:text-purple-300 hover:bg-dark-bg rounded" title="Clone">
                        <i data-lucide="copy" class="w-4 h-4"></i>
                    </button>
                    ${!isActive ? `
                        <button onclick="deleteEscalationPreset(${preset.id}, '${preset.name.replace(/'/g, "\\'")}')"
                                class="p-1.5 text-red-400 hover:text-red-300 hover:bg-dark-bg rounded" title="Delete">
                            <i data-lucide="trash-2" class="w-4 h-4"></i>
                        </button>
                    ` : ''}
                </div>
            </div>

            <!-- Rules Summary -->
            <div class="mt-3 pt-3 border-t border-dark-border">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-sm text-gray-400">${enabledRules.length} active rules</span>
                    <button onclick="showAddRuleModal(${preset.id})"
                            class="text-xs text-blue-400 hover:text-blue-300">
                        + Add Rule
                    </button>
                </div>
                ${rules.length > 0 ? `
                    <div class="space-y-1">
                        ${rules.slice(0, 5).map(rule => renderRuleCompact(rule)).join('')}
                        <button onclick="showAllRulesModal(${preset.id})"
                                class="text-xs text-blue-400 hover:text-blue-300 mt-2">
                            View all ${rules.length} rules →
                        </button>
                    </div>
                ` : `
                    <p class="text-xs text-gray-500 italic">No rules defined</p>
                `}
            </div>
        </div>
    `;
}

function renderRuleCompact(rule) {
    return `
        <div class="flex items-center justify-between p-2 bg-dark-bg rounded text-sm ${!rule.enabled ? 'opacity-50' : ''}">
            <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-medium text-white truncate">${rule.rule_name}</span>
                    <span class="px-1.5 py-0.5 text-xs rounded bg-purple-900/30 text-purple-400">
                        P${rule.priority}
                    </span>
                    <span class="px-1.5 py-0.5 text-xs rounded ${rule.escalation_target === 'super_complex' ? 'bg-red-900/30 text-red-400' : 'bg-yellow-900/30 text-yellow-400'}">
                        &rarr; ${rule.escalation_target}
                    </span>
                    <span class="px-1.5 py-0.5 text-xs rounded bg-gray-700 text-gray-300">
                        ${rule.escalation_duration} turns
                    </span>
                </div>
                <div class="text-xs text-gray-500 mt-1">
                    ${rule.trigger_type}: ${formatTriggerPatterns(rule.trigger_type, rule.trigger_patterns)}
                </div>
            </div>
            <div class="flex gap-1 ml-2">
                <button onclick="toggleRule(${rule.id})"
                        class="p-1 ${rule.enabled ? 'text-green-400 hover:text-green-300' : 'text-gray-500 hover:text-gray-400'}"
                        title="${rule.enabled ? 'Disable' : 'Enable'}">
                    <i data-lucide="${rule.enabled ? 'toggle-right' : 'toggle-left'}" class="w-4 h-4"></i>
                </button>
                <button onclick="showEditRuleModal(${rule.id})"
                        class="p-1 text-blue-400 hover:text-blue-300" title="Edit">
                    <i data-lucide="edit-2" class="w-4 h-4"></i>
                </button>
                <button onclick="deleteRule(${rule.id}, '${rule.rule_name}')"
                        class="p-1 text-red-400 hover:text-red-300" title="Delete">
                    <i data-lucide="trash-2" class="w-4 h-4"></i>
                </button>
            </div>
        </div>
    `;
}

function formatTriggerPatterns(type, patterns) {
    if (!patterns) return '-';

    switch (type) {
        case 'clarification':
        case 'user_correction':
        case 'user_frustration':
        case 'explicit_request':
            const p = patterns.patterns || [];
            return p.slice(0, 3).map(s => `"${s}"`).join(', ') + (p.length > 3 ? ` +${p.length - 3} more` : '');
        case 'empty_results':
        case 'tool_failure':
            return Object.entries(patterns).map(([k, v]) => `${k}=${v}`).join(', ');
        case 'short_response':
            return `max ${patterns.max_length} chars`;
        case 'short_query':
            return `max ${patterns.max_words} words`;
        case 'long_query':
            return `min ${patterns.min_words} words`;
        case 'repeated_query':
            return `similarity > ${patterns.similarity_threshold}`;
        case 'always':
            return 'Always trigger';
        default:
            return JSON.stringify(patterns).slice(0, 50);
    }
}

function formatAutoActivate(conditions) {
    if (!conditions) return '';
    const parts = [];
    if (conditions.time_range) {
        parts.push(`${conditions.time_range.start} - ${conditions.time_range.end}`);
    }
    if (conditions.user_mode) {
        parts.push(`Mode: ${conditions.user_mode}`);
    }
    return parts.join(', ') || 'Custom conditions';
}

// ============== Actions ==============

async function activateEscalationPreset(presetId) {
    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets/${presetId}/activate`, {
            method: 'PUT',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to activate preset');

        safeShowToast('Preset activated', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error activating preset:', error);
        safeShowToast('Failed to activate preset', 'error');
    }
}

async function toggleRule(ruleId) {
    try {
        const response = await fetch(`${ESCALATION_API_BASE}/rules/${ruleId}/toggle`, {
            method: 'PUT',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to toggle rule');

        await loadEscalationPresets();
    } catch (error) {
        console.error('Error toggling rule:', error);
        safeShowToast('Failed to toggle rule', 'error');
    }
}

async function deleteEscalationPreset(presetId, presetName) {
    if (!confirm(`Delete preset "${presetName}"? This will also delete all its rules.`)) return;

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets/${presetId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to delete preset');

        safeShowToast('Preset deleted', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error deleting preset:', error);
        safeShowToast('Failed to delete preset', 'error');
    }
}

async function deleteRule(ruleId, ruleName) {
    if (!confirm(`Delete rule "${ruleName}"?`)) return;

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/rules/${ruleId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to delete rule');

        safeShowToast('Rule deleted', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error deleting rule:', error);
        safeShowToast('Failed to delete rule', 'error');
    }
}

// ============== Modals ==============

function showCreateEscalationPresetModal() {
    const modal = document.createElement('div');
    modal.id = 'create-preset-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
            <h3 class="text-lg font-semibold text-white mb-4">Create New Preset</h3>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Name</label>
                    <input type="text" id="new-preset-name"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                           placeholder="My Custom Preset">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Description</label>
                    <textarea id="new-preset-description"
                              class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                              rows="2" placeholder="Describe when to use this preset..."></textarea>
                </div>
            </div>
            <div class="flex justify-end gap-3 mt-6">
                <button onclick="closeEscalationModal('create-preset-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Cancel
                </button>
                <button onclick="createEscalationPreset()"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Create
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function createEscalationPreset() {
    const name = document.getElementById('new-preset-name').value.trim();
    const description = document.getElementById('new-preset-description').value.trim();

    if (!name) {
        safeShowToast('Please enter a name', 'error');
        return;
    }

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({ name, description })
        });
        if (!response.ok) throw new Error('Failed to create preset');

        closeEscalationModal('create-preset-modal');
        safeShowToast('Preset created', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error creating preset:', error);
        safeShowToast('Failed to create preset', 'error');
    }
}

function showCloneEscalationPresetModal(presetId, presetName) {
    const modal = document.createElement('div');
    modal.id = 'clone-preset-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
            <h3 class="text-lg font-semibold text-white mb-4">Clone "${presetName}"</h3>
            <div>
                <label class="block text-sm text-gray-400 mb-1">New Name</label>
                <input type="text" id="clone-preset-name"
                       class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                       value="${presetName} (Copy)">
            </div>
            <div class="flex justify-end gap-3 mt-6">
                <button onclick="closeEscalationModal('clone-preset-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Cancel
                </button>
                <button onclick="cloneEscalationPreset(${presetId})"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Clone
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function cloneEscalationPreset(presetId) {
    const newName = document.getElementById('clone-preset-name').value.trim();

    if (!newName) {
        safeShowToast('Please enter a name', 'error');
        return;
    }

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets/${presetId}/clone?new_name=${encodeURIComponent(newName)}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to clone preset');

        closeEscalationModal('clone-preset-modal');
        safeShowToast('Preset cloned', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error cloning preset:', error);
        safeShowToast('Failed to clone preset', 'error');
    }
}

function showAddRuleModal(presetId) {
    const modal = document.createElement('div');
    modal.id = 'add-rule-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50 overflow-y-auto';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg my-8">
            <h3 class="text-lg font-semibold text-white mb-4">Add Rule</h3>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Rule Name</label>
                    <input type="text" id="rule-name"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Trigger Type</label>
                    <select id="rule-trigger-type" onchange="updateTriggerPatternsUI()"
                            class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                        <option value="clarification">Clarification (response patterns)</option>
                        <option value="user_correction">User Correction (input patterns)</option>
                        <option value="user_frustration">User Frustration (input patterns)</option>
                        <option value="explicit_request">Explicit Request (input patterns)</option>
                        <option value="empty_results">Empty Results (tool check)</option>
                        <option value="tool_failure">Tool Failure (error check)</option>
                        <option value="short_response">Short Response (length check)</option>
                        <option value="short_query">Short Query (word count)</option>
                        <option value="long_query">Long Query (word count)</option>
                        <option value="repeated_query">Repeated Query (similarity check)</option>
                        <option value="always">Always (demo mode)</option>
                    </select>
                </div>
                <div id="trigger-patterns-container">
                    <!-- Dynamic based on trigger type -->
                </div>
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Escalate To</label>
                        <select id="rule-target"
                                class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                            <option value="complex">Complex</option>
                            <option value="super_complex">Super Complex</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Duration (turns)</label>
                        <input type="number" id="rule-duration" value="5" min="1" max="999"
                               class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    </div>
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Priority (higher = checked first)</label>
                    <input type="number" id="rule-priority" value="100" min="1" max="1000"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Description</label>
                    <input type="text" id="rule-description"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            </div>
            <div class="flex justify-end gap-3 mt-6">
                <button onclick="closeEscalationModal('add-rule-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Cancel
                </button>
                <button onclick="addRule(${presetId})"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Add Rule
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    updateTriggerPatternsUI();
}

function updateTriggerPatternsUI() {
    const type = document.getElementById('rule-trigger-type')?.value;
    const container = document.getElementById('trigger-patterns-container');
    if (!container) return;

    switch (type) {
        case 'clarification':
        case 'user_correction':
        case 'user_frustration':
        case 'explicit_request':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Patterns (one per line)</label>
                    <textarea id="rule-patterns" rows="4"
                              class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white font-mono text-sm"
                              placeholder="could you clarify\nwhat do you mean\ncan you specify"></textarea>
                </div>
            `;
            break;
        case 'empty_results':
            container.innerHTML = `
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="check-empty" checked class="rounded">
                        Check empty
                    </label>
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="check-null" checked class="rounded">
                        Check null
                    </label>
                </div>
            `;
            break;
        case 'tool_failure':
            container.innerHTML = `
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="on-error" checked class="rounded">
                        Trigger on error
                    </label>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Consecutive failures</label>
                        <input type="number" id="consecutive-failures" value="1" min="1"
                               class="w-20 bg-dark-bg border border-dark-border rounded px-2 py-1 text-white">
                    </div>
                </div>
            `;
            break;
        case 'short_response':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Max response length (chars)</label>
                    <input type="number" id="max-length" value="50" min="1"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            `;
            break;
        case 'short_query':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Max query words</label>
                    <input type="number" id="max-words" value="3" min="1"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            `;
            break;
        case 'long_query':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Min query words</label>
                    <input type="number" id="min-words" value="40" min="5"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    <p class="text-xs text-gray-500 mt-1">Queries with this many words or more will trigger escalation. 40+ words suggests complex multi-part questions.</p>
                </div>
            `;
            break;
        case 'repeated_query':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Similarity threshold (0.0-1.0)</label>
                    <input type="number" id="similarity-threshold" value="0.8" min="0" max="1" step="0.1"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    <p class="text-xs text-gray-500 mt-1">Higher = more similar queries required to trigger. 0.8 is recommended.</p>
                </div>
            `;
            break;
        case 'always':
            container.innerHTML = `
                <p class="text-sm text-yellow-400">This rule will always trigger escalation (use for Demo Mode)</p>
            `;
            break;
        default:
            container.innerHTML = '';
    }
}

function getTriggerPatterns() {
    const type = document.getElementById('rule-trigger-type').value;

    switch (type) {
        case 'clarification':
        case 'user_correction':
        case 'user_frustration':
        case 'explicit_request':
            const patterns = document.getElementById('rule-patterns').value
                .split('\n')
                .map(s => s.trim())
                .filter(s => s);
            return { patterns };
        case 'empty_results':
            return {
                check_empty: document.getElementById('check-empty').checked,
                check_null: document.getElementById('check-null').checked
            };
        case 'tool_failure':
            return {
                on_error: document.getElementById('on-error').checked,
                consecutive_failures: parseInt(document.getElementById('consecutive-failures')?.value || '1')
            };
        case 'short_response':
            return { max_length: parseInt(document.getElementById('max-length').value) };
        case 'short_query':
            return { max_words: parseInt(document.getElementById('max-words').value) };
        case 'long_query':
            return { min_words: parseInt(document.getElementById('min-words').value) };
        case 'repeated_query':
            return { similarity_threshold: parseFloat(document.getElementById('similarity-threshold').value) };
        case 'always':
            return { always: true };
        default:
            return {};
    }
}

async function addRule(presetId) {
    const ruleName = document.getElementById('rule-name').value.trim();
    const triggerType = document.getElementById('rule-trigger-type').value;
    const target = document.getElementById('rule-target').value;
    const duration = parseInt(document.getElementById('rule-duration').value);
    const priority = parseInt(document.getElementById('rule-priority').value);
    const description = document.getElementById('rule-description').value.trim();

    if (!ruleName) {
        safeShowToast('Please enter a rule name', 'error');
        return;
    }

    const triggerPatterns = getTriggerPatterns();

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets/${presetId}/rules`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                rule_name: ruleName,
                trigger_type: triggerType,
                trigger_patterns: triggerPatterns,
                escalation_target: target,
                escalation_duration: duration,
                priority: priority,
                description: description
            })
        });
        if (!response.ok) throw new Error('Failed to add rule');

        closeEscalationModal('add-rule-modal');
        safeShowToast('Rule added', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error adding rule:', error);
        safeShowToast('Failed to add rule', 'error');
    }
}

function closeEscalationModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.remove();
}

// ============== Edit Rule Modal ==============

function showEditRuleModal(ruleId) {
    console.log('showEditRuleModal called with ruleId:', ruleId);
    // Find the rule data from escalationPresetsData
    let rule = null;
    let presetId = null;
    for (const preset of escalationPresetsData) {
        const found = (preset.rules || []).find(r => r.id === ruleId);
        if (found) {
            rule = found;
            presetId = preset.id;
            break;
        }
    }
    if (!rule) {
        safeShowToast('Rule not found', 'error');
        return;
    }

    const modal = document.createElement('div');
    modal.id = 'edit-rule-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50 overflow-y-auto';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg my-8">
            <h3 class="text-lg font-semibold text-white mb-4">Edit Rule: ${rule.rule_name}</h3>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Rule Name</label>
                    <input type="text" id="edit-rule-name" value="${rule.rule_name}"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Trigger Type</label>
                    <select id="edit-rule-trigger-type" onchange="updateEditTriggerPatternsUI()"
                            class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                        <option value="clarification" ${rule.trigger_type === 'clarification' ? 'selected' : ''}>Clarification (response patterns)</option>
                        <option value="user_correction" ${rule.trigger_type === 'user_correction' ? 'selected' : ''}>User Correction (input patterns)</option>
                        <option value="user_frustration" ${rule.trigger_type === 'user_frustration' ? 'selected' : ''}>User Frustration (input patterns)</option>
                        <option value="explicit_request" ${rule.trigger_type === 'explicit_request' ? 'selected' : ''}>Explicit Request (input patterns)</option>
                        <option value="empty_results" ${rule.trigger_type === 'empty_results' ? 'selected' : ''}>Empty Results (tool check)</option>
                        <option value="tool_failure" ${rule.trigger_type === 'tool_failure' ? 'selected' : ''}>Tool Failure (error check)</option>
                        <option value="short_response" ${rule.trigger_type === 'short_response' ? 'selected' : ''}>Short Response (length check)</option>
                        <option value="short_query" ${rule.trigger_type === 'short_query' ? 'selected' : ''}>Short Query (word count)</option>
                        <option value="long_query" ${rule.trigger_type === 'long_query' ? 'selected' : ''}>Long Query (word count)</option>
                        <option value="repeated_query" ${rule.trigger_type === 'repeated_query' ? 'selected' : ''}>Repeated Query (similarity check)</option>
                        <option value="always" ${rule.trigger_type === 'always' ? 'selected' : ''}>Always (demo mode)</option>
                    </select>
                </div>
                <div id="edit-trigger-patterns-container">
                    <!-- Dynamic based on trigger type -->
                </div>
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Escalate To</label>
                        <select id="edit-rule-target"
                                class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                            <option value="complex" ${rule.escalation_target === 'complex' ? 'selected' : ''}>Complex</option>
                            <option value="super_complex" ${rule.escalation_target === 'super_complex' ? 'selected' : ''}>Super Complex</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Duration (turns)</label>
                        <input type="number" id="edit-rule-duration" value="${rule.escalation_duration || 5}" min="1" max="999"
                               class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    </div>
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Priority (higher = checked first)</label>
                    <input type="number" id="edit-rule-priority" value="${rule.priority || 100}" min="1" max="1000"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Description</label>
                    <input type="text" id="edit-rule-description" value="${rule.description || ''}"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            </div>
            <div class="flex justify-end gap-3 mt-6">
                <button onclick="closeEscalationModal('edit-rule-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Cancel
                </button>
                <button onclick="updateRule(${ruleId})"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Save Changes
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // Store rule data for pattern population
    modal.dataset.rulePatterns = JSON.stringify(rule.trigger_patterns || {});
    updateEditTriggerPatternsUI();
}

function updateEditTriggerPatternsUI() {
    const type = document.getElementById('edit-rule-trigger-type')?.value;
    const container = document.getElementById('edit-trigger-patterns-container');
    const modal = document.getElementById('edit-rule-modal');
    if (!container || !modal) return;

    const existingPatterns = JSON.parse(modal.dataset.rulePatterns || '{}');

    switch (type) {
        case 'clarification':
        case 'user_correction':
        case 'user_frustration':
        case 'explicit_request':
            const patterns = existingPatterns.patterns || [];
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Patterns (one per line)</label>
                    <textarea id="edit-rule-patterns" rows="4"
                              class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white font-mono text-sm"
                              placeholder="could you clarify\nwhat do you mean\ncan you specify">${patterns.join('\n')}</textarea>
                </div>
            `;
            break;
        case 'empty_results':
            container.innerHTML = `
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="edit-check-empty" ${existingPatterns.check_empty !== false ? 'checked' : ''} class="rounded">
                        Check empty
                    </label>
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="edit-check-null" ${existingPatterns.check_null !== false ? 'checked' : ''} class="rounded">
                        Check null
                    </label>
                </div>
            `;
            break;
        case 'tool_failure':
            container.innerHTML = `
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="edit-on-error" ${existingPatterns.on_error !== false ? 'checked' : ''} class="rounded">
                        Trigger on error
                    </label>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Consecutive failures</label>
                        <input type="number" id="edit-consecutive-failures" value="${existingPatterns.consecutive_failures || 1}" min="1"
                               class="w-20 bg-dark-bg border border-dark-border rounded px-2 py-1 text-white">
                    </div>
                </div>
            `;
            break;
        case 'short_response':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Max response length (chars)</label>
                    <input type="number" id="edit-max-length" value="${existingPatterns.max_length || 50}" min="1"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            `;
            break;
        case 'short_query':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Max query words</label>
                    <input type="number" id="edit-max-words" value="${existingPatterns.max_words || 3}" min="1"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            `;
            break;
        case 'long_query':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Min query words</label>
                    <input type="number" id="edit-min-words" value="${existingPatterns.min_words || 40}" min="5"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    <p class="text-xs text-gray-500 mt-1">Queries with this many words or more will trigger escalation.</p>
                </div>
            `;
            break;
        case 'repeated_query':
            container.innerHTML = `
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Similarity threshold (0.0-1.0)</label>
                    <input type="number" id="edit-similarity-threshold" value="${existingPatterns.similarity_threshold || 0.8}" min="0" max="1" step="0.1"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                    <p class="text-xs text-gray-500 mt-1">Higher = more similar queries required to trigger</p>
                </div>
            `;
            break;
        case 'always':
            container.innerHTML = `
                <p class="text-sm text-yellow-400">This rule will always trigger escalation (use for Demo Mode)</p>
            `;
            break;
        default:
            container.innerHTML = '';
    }
}

function getEditTriggerPatterns() {
    const type = document.getElementById('edit-rule-trigger-type').value;

    switch (type) {
        case 'clarification':
        case 'user_correction':
        case 'user_frustration':
        case 'explicit_request':
            const patterns = document.getElementById('edit-rule-patterns').value
                .split('\n')
                .map(s => s.trim())
                .filter(s => s);
            return { patterns };
        case 'empty_results':
            return {
                check_empty: document.getElementById('edit-check-empty').checked,
                check_null: document.getElementById('edit-check-null').checked
            };
        case 'tool_failure':
            return {
                on_error: document.getElementById('edit-on-error').checked,
                consecutive_failures: parseInt(document.getElementById('edit-consecutive-failures')?.value || '1')
            };
        case 'short_response':
            return { max_length: parseInt(document.getElementById('edit-max-length').value) };
        case 'short_query':
            return { max_words: parseInt(document.getElementById('edit-max-words').value) };
        case 'long_query':
            return { min_words: parseInt(document.getElementById('edit-min-words').value) };
        case 'repeated_query':
            return { similarity_threshold: parseFloat(document.getElementById('edit-similarity-threshold').value) };
        case 'always':
            return { always: true };
        default:
            return {};
    }
}

async function updateRule(ruleId) {
    const ruleName = document.getElementById('edit-rule-name').value.trim();
    const triggerType = document.getElementById('edit-rule-trigger-type').value;
    const target = document.getElementById('edit-rule-target').value;
    const duration = parseInt(document.getElementById('edit-rule-duration').value);
    const priority = parseInt(document.getElementById('edit-rule-priority').value);
    const description = document.getElementById('edit-rule-description').value.trim();

    if (!ruleName) {
        safeShowToast('Please enter a rule name', 'error');
        return;
    }

    const triggerPatterns = getEditTriggerPatterns();

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/rules/${ruleId}`, {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                rule_name: ruleName,
                trigger_type: triggerType,
                trigger_patterns: triggerPatterns,
                escalation_target: target,
                escalation_duration: duration,
                priority: priority,
                description: description
            })
        });
        if (!response.ok) throw new Error('Failed to update rule');

        closeEscalationModal('edit-rule-modal');
        safeShowToast('Rule updated', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error updating rule:', error);
        safeShowToast('Failed to update rule', 'error');
    }
}

// ============== All Rules Modal ==============

function showAllRulesModal(presetId) {
    const preset = escalationPresetsData.find(p => p.id === presetId);
    if (!preset) {
        safeShowToast('Preset not found', 'error');
        return;
    }

    const rules = preset.rules || [];

    const modal = document.createElement('div');
    modal.id = 'all-rules-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50 overflow-y-auto';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-3xl my-8 max-h-[80vh] overflow-hidden flex flex-col">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-lg font-semibold text-white">${preset.name} - All Rules (${rules.length})</h3>
                <button onclick="showAddRuleModal(${presetId}); closeEscalationModal('all-rules-modal');"
                        class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm">
                    + Add Rule
                </button>
            </div>
            <div class="overflow-y-auto flex-1 space-y-2">
                ${rules.length === 0 ? `
                    <p class="text-sm text-gray-500 italic text-center py-4">No rules defined</p>
                ` : rules.sort((a, b) => b.priority - a.priority).map(rule => `
                    <div class="flex items-center justify-between p-3 bg-dark-bg rounded border border-dark-border ${!rule.enabled ? 'opacity-50' : ''}">
                        <div class="flex-1">
                            <div class="flex items-center gap-2 flex-wrap">
                                <span class="text-sm font-medium text-white">${rule.rule_name}</span>
                                <span class="px-1.5 py-0.5 text-xs rounded bg-purple-900/30 text-purple-400">
                                    P${rule.priority}
                                </span>
                                <span class="px-1.5 py-0.5 text-xs rounded ${rule.escalation_target === 'super_complex' ? 'bg-red-900/30 text-red-400' : 'bg-yellow-900/30 text-yellow-400'}">
                                    &rarr; ${rule.escalation_target}
                                </span>
                                <span class="px-1.5 py-0.5 text-xs rounded bg-gray-700 text-gray-300">
                                    ${rule.escalation_duration} turns
                                </span>
                                <span class="px-1.5 py-0.5 text-xs rounded bg-blue-900/30 text-blue-400">
                                    ${rule.trigger_type}
                                </span>
                            </div>
                            <div class="text-xs text-gray-500 mt-1">
                                ${rule.description || formatTriggerPatterns(rule.trigger_type, rule.trigger_patterns)}
                            </div>
                        </div>
                        <div class="flex gap-1 ml-4">
                            <button onclick="toggleRule(${rule.id})"
                                    class="p-1.5 ${rule.enabled ? 'text-green-400 hover:text-green-300' : 'text-gray-500 hover:text-gray-400'} hover:bg-dark-card rounded"
                                    title="${rule.enabled ? 'Disable' : 'Enable'}">
                                <i data-lucide="${rule.enabled ? 'toggle-right' : 'toggle-left'}" class="w-4 h-4"></i>
                            </button>
                            <button onclick="closeEscalationModal('all-rules-modal'); showEditRuleModal(${rule.id});"
                                    class="p-1.5 text-blue-400 hover:text-blue-300 hover:bg-dark-card rounded" title="Edit">
                                <i data-lucide="edit-2" class="w-4 h-4"></i>
                            </button>
                            <button onclick="deleteRuleFromModal(${rule.id}, '${rule.rule_name.replace(/'/g, "\\'")}', ${presetId})"
                                    class="p-1.5 text-red-400 hover:text-red-300 hover:bg-dark-card rounded" title="Delete">
                                <i data-lucide="trash-2" class="w-4 h-4"></i>
                            </button>
                        </div>
                    </div>
                `).join('')}
            </div>
            <div class="flex justify-end mt-4 pt-4 border-t border-dark-border">
                <button onclick="closeEscalationModal('all-rules-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Close
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // Re-initialize Lucide icons for the modal
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
}

async function deleteRuleFromModal(ruleId, ruleName, presetId) {
    if (!confirm(`Delete rule "${ruleName}"?`)) return;

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/rules/${ruleId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to delete rule');

        safeShowToast('Rule deleted', 'success');
        await loadEscalationPresets();
        // Refresh the modal with updated data
        closeEscalationModal('all-rules-modal');
        showAllRulesModal(presetId);
    } catch (error) {
        console.error('Error deleting rule:', error);
        safeShowToast('Failed to delete rule', 'error');
    }
}

// ============== Edit Preset Modal ==============

function showEditEscalationPresetModal(presetId) {
    const preset = escalationPresetsData.find(p => p.id === presetId);
    if (!preset) {
        safeShowToast('Preset not found', 'error');
        return;
    }

    const autoConditions = preset.auto_activate_conditions || {};

    const modal = document.createElement('div');
    modal.id = 'edit-preset-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg">
            <h3 class="text-lg font-semibold text-white mb-4">Edit Preset: ${preset.name}</h3>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Name</label>
                    <input type="text" id="edit-preset-name" value="${preset.name}"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Description</label>
                    <textarea id="edit-preset-description"
                              class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white"
                              rows="2">${preset.description || ''}</textarea>
                </div>

                <!-- Auto-Activation Settings -->
                <div class="border-t border-dark-border pt-4">
                    <h4 class="text-sm font-medium text-white mb-3">Auto-Activation (Optional)</h4>

                    <div class="space-y-3">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">User Mode</label>
                            <select id="edit-preset-user-mode"
                                    class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                                <option value="" ${!autoConditions.user_mode ? 'selected' : ''}>None (don't auto-activate by mode)</option>
                                <option value="guest" ${autoConditions.user_mode === 'guest' ? 'selected' : ''}>Guest Mode</option>
                                <option value="demo" ${autoConditions.user_mode === 'demo' ? 'selected' : ''}>Demo Mode</option>
                                <option value="owner" ${autoConditions.user_mode === 'owner' ? 'selected' : ''}>Owner Mode</option>
                            </select>
                        </div>

                        <div class="grid grid-cols-2 gap-3">
                            <div>
                                <label class="block text-sm text-gray-400 mb-1">Time Start (HH:MM)</label>
                                <input type="time" id="edit-preset-time-start"
                                       value="${autoConditions.time_range?.start || ''}"
                                       class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                            </div>
                            <div>
                                <label class="block text-sm text-gray-400 mb-1">Time End (HH:MM)</label>
                                <input type="time" id="edit-preset-time-end"
                                       value="${autoConditions.time_range?.end || ''}"
                                       class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                            </div>
                        </div>
                        <p class="text-xs text-gray-500">Leave time fields empty to disable time-based activation. Supports overnight ranges (e.g., 23:00 to 06:00).</p>
                    </div>
                </div>
            </div>
            <div class="flex justify-end gap-3 mt-6">
                <button onclick="closeEscalationModal('edit-preset-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Cancel
                </button>
                <button onclick="updateEscalationPreset(${presetId})"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg">
                    Save Changes
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function updateEscalationPreset(presetId) {
    const name = document.getElementById('edit-preset-name').value.trim();
    const description = document.getElementById('edit-preset-description').value.trim();
    const userMode = document.getElementById('edit-preset-user-mode').value;
    const timeStart = document.getElementById('edit-preset-time-start').value;
    const timeEnd = document.getElementById('edit-preset-time-end').value;

    if (!name) {
        safeShowToast('Please enter a name', 'error');
        return;
    }

    // Build auto_activate_conditions
    let autoActivateConditions = null;
    if (userMode || (timeStart && timeEnd)) {
        autoActivateConditions = {};
        if (userMode) {
            autoActivateConditions.user_mode = userMode;
        }
        if (timeStart && timeEnd) {
            autoActivateConditions.time_range = {
                start: timeStart,
                end: timeEnd
            };
        }
    }

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/presets/${presetId}`, {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                name,
                description,
                auto_activate_conditions: autoActivateConditions
            })
        });
        if (!response.ok) throw new Error('Failed to update preset');

        closeEscalationModal('edit-preset-modal');
        safeShowToast('Preset updated', 'success');
        await loadEscalationPresets();
    } catch (error) {
        console.error('Error updating preset:', error);
        safeShowToast('Failed to update preset', 'error');
    }
}

// ============== Manual Override UI ==============

let overridesData = [];

async function loadOverrides() {
    try {
        const response = await fetch(`${ESCALATION_API_BASE}/overrides/active`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to load overrides');
        overridesData = await response.json();
        renderOverridesPanel();
    } catch (error) {
        console.error('Error loading overrides:', error);
    }
}

function renderOverridesPanel() {
    const container = document.getElementById('overrides-container');
    if (!container) return;

    container.innerHTML = `
        <div class="p-4 bg-dark-card border border-dark-border rounded-lg">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h3 class="text-lg font-semibold text-white">Manual Overrides</h3>
                    <p class="text-sm text-gray-400">Force a specific model for testing or debugging</p>
                </div>
                <button onclick="showCreateOverrideModal()"
                        class="px-4 py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg text-sm font-medium transition-colors">
                    + New Override
                </button>
            </div>

            ${overridesData.length === 0 ? `
                <p class="text-sm text-gray-500 italic text-center py-4">No active overrides</p>
            ` : `
                <div class="space-y-2">
                    ${overridesData.map(override => `
                        <div class="flex items-center justify-between p-3 bg-dark-bg rounded border border-orange-800/50">
                            <div class="flex-1">
                                <div class="flex items-center gap-2">
                                    <span class="text-sm font-mono text-white">${override.session_id.substring(0, 20)}...</span>
                                    <span class="px-2 py-0.5 text-xs rounded bg-orange-900/30 text-orange-400">
                                        &rarr; ${override.escalated_to}
                                    </span>
                                    ${override.is_manual_override ? '<span class="px-2 py-0.5 text-xs rounded bg-purple-900/30 text-purple-400">Manual</span>' : ''}
                                </div>
                                <div class="text-xs text-gray-500 mt-1">
                                    ${override.turns_remaining ? `${override.turns_remaining} turns remaining` : ''}
                                    ${override.expires_at ? `Expires: ${new Date(override.expires_at).toLocaleString()}` : ''}
                                </div>
                            </div>
                            <button onclick="cancelOverride('${override.session_id}')"
                                    class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm">
                                Cancel
                            </button>
                        </div>
                    `).join('')}
                </div>
            `}
        </div>
    `;
}

function showCreateOverrideModal() {
    const modal = document.createElement('div');
    modal.id = 'create-override-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
            <h3 class="text-lg font-semibold text-white mb-4">Create Manual Override</h3>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Session ID</label>
                    <input type="text" id="override-session-id"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white font-mono"
                           placeholder="e.g., jarvis-web-abc123 or * for all sessions">
                    <p class="text-xs text-gray-500 mt-1">Use * to apply override globally to all new sessions</p>
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Force Model</label>
                    <select id="override-target"
                            class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                        <option value="simple">Simple (fastest, basic)</option>
                        <option value="complex">Complex (balanced)</option>
                        <option value="super_complex">Super Complex (most capable)</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Duration Type</label>
                    <select id="override-duration-type" onchange="updateOverrideDurationUI()"
                            class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                        <option value="turns">Number of turns</option>
                        <option value="time">Time-based (minutes)</option>
                        <option value="indefinite">Indefinite (until cancelled)</option>
                    </select>
                </div>
                <div id="override-duration-container">
                    <label class="block text-sm text-gray-400 mb-1">Turns</label>
                    <input type="number" id="override-duration-value" value="10" min="1" max="999"
                           class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
                </div>
            </div>
            <div class="flex justify-end gap-3 mt-6">
                <button onclick="closeEscalationModal('create-override-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                    Cancel
                </button>
                <button onclick="createOverride()"
                        class="px-4 py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg">
                    Create Override
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function updateOverrideDurationUI() {
    const type = document.getElementById('override-duration-type').value;
    const container = document.getElementById('override-duration-container');

    if (type === 'turns') {
        container.innerHTML = `
            <label class="block text-sm text-gray-400 mb-1">Number of Turns</label>
            <input type="number" id="override-duration-value" value="10" min="1" max="999"
                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
        `;
    } else if (type === 'time') {
        container.innerHTML = `
            <label class="block text-sm text-gray-400 mb-1">Duration (minutes)</label>
            <input type="number" id="override-duration-value" value="30" min="1" max="1440"
                   class="w-full bg-dark-bg border border-dark-border rounded-lg px-3 py-2 text-white">
        `;
    } else {
        container.innerHTML = `
            <p class="text-sm text-yellow-400">Override will remain active until manually cancelled</p>
        `;
    }
}

async function createOverride() {
    const sessionId = document.getElementById('override-session-id').value.trim();
    const targetModel = document.getElementById('override-target').value;
    const durationType = document.getElementById('override-duration-type').value;
    const durationValue = document.getElementById('override-duration-value')?.value;

    if (!sessionId) {
        safeShowToast('Please enter a session ID', 'error');
        return;
    }

    const payload = {
        session_id: sessionId,
        target_model: targetModel,
        duration_type: durationType
    };

    if (durationType === 'turns') {
        payload.duration_turns = parseInt(durationValue);
    } else if (durationType === 'time') {
        payload.duration_minutes = parseInt(durationValue);
    }

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/override`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(payload)
        });
        if (!response.ok) throw new Error('Failed to create override');

        closeEscalationModal('create-override-modal');
        safeShowToast('Override created', 'success');
        await loadOverrides();
    } catch (error) {
        console.error('Error creating override:', error);
        safeShowToast('Failed to create override', 'error');
    }
}

async function cancelOverride(sessionId) {
    if (!confirm(`Cancel override for session "${sessionId}"?`)) return;

    try {
        const response = await fetch(`${ESCALATION_API_BASE}/override/${encodeURIComponent(sessionId)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error('Failed to cancel override');

        safeShowToast('Override cancelled', 'success');
        await loadOverrides();
    } catch (error) {
        console.error('Error cancelling override:', error);
        safeShowToast('Failed to cancel override', 'error');
    }
}

// ============== Lifecycle ==============

function initEscalationPage() {
    loadEscalationPresets();
    loadOverrides();
    loadEscalationStats();
    loadRecentEvents();

    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('escalation-refresh', loadEscalationPresets, ESCALATION_REFRESH_INTERVAL);
        RefreshManager.createInterval('overrides-refresh', loadOverrides, ESCALATION_REFRESH_INTERVAL);
        RefreshManager.createInterval('stats-refresh', loadEscalationStats, ESCALATION_REFRESH_INTERVAL);
        RefreshManager.createInterval('events-refresh', loadRecentEvents, ESCALATION_REFRESH_INTERVAL);
    }
}

function destroyEscalationPage() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('escalation-refresh');
        RefreshManager.clearInterval('overrides-refresh');
        RefreshManager.clearInterval('stats-refresh');
        RefreshManager.clearInterval('events-refresh');
    }
}

// Helper function for toast notifications (safe fallback)
function safeShowToast(message, type) {
    if (typeof showToast === 'function') {
        showToast(message, type);
    } else {
        console.log(`[${type}] ${message}`);
    }
}

// Export functions
if (typeof window !== 'undefined') {
    window.initEscalationPage = initEscalationPage;
    window.destroyEscalationPage = destroyEscalationPage;
    window.loadEscalationPresets = loadEscalationPresets;
    window.activateEscalationPreset = activateEscalationPreset;
    window.toggleRule = toggleRule;
    window.deleteEscalationPreset = deleteEscalationPreset;
    window.deleteRule = deleteRule;
    window.showCreateEscalationPresetModal = showCreateEscalationPresetModal;
    window.createEscalationPreset = createEscalationPreset;
    window.showCloneEscalationPresetModal = showCloneEscalationPresetModal;
    window.cloneEscalationPreset = cloneEscalationPreset;
    window.showAddRuleModal = showAddRuleModal;
    window.addRule = addRule;
    window.updateTriggerPatternsUI = updateTriggerPatternsUI;
    window.closeEscalationModal = closeEscalationModal;
    // Edit Rule Modal
    window.showEditRuleModal = showEditRuleModal;
    window.updateEditTriggerPatternsUI = updateEditTriggerPatternsUI;
    window.updateRule = updateRule;
    // All Rules Modal
    window.showAllRulesModal = showAllRulesModal;
    window.deleteRuleFromModal = deleteRuleFromModal;
    // Edit Preset Modal
    window.showEditEscalationPresetModal = showEditEscalationPresetModal;
    window.updateEscalationPreset = updateEscalationPreset;
    // Manual Override UI
    window.loadOverrides = loadOverrides;
    window.showCreateOverrideModal = showCreateOverrideModal;
    window.updateOverrideDurationUI = updateOverrideDurationUI;
    window.createOverride = createOverride;
    window.cancelOverride = cancelOverride;
    // Metrics UI
    window.loadEscalationStats = loadEscalationStats;
    window.loadRecentEvents = loadRecentEvents;
}
