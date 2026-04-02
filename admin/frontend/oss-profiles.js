let ossProfilesState = {
    status: null,
    effectiveConfig: null,
    preview: null,
};

async function initOSSProfilesPage() {
    const container = document.getElementById('oss-profiles-container');
    if (!container) return;

    container.innerHTML = `
        <div class="text-center text-gray-400 py-12">
            <div class="text-4xl mb-4">🧭</div>
            <p>Inspecting live runtime and tuning profiles...</p>
        </div>
    `;

    try {
        const [status, effectiveConfig] = await Promise.all([
            apiRequest('/api/oss-profiles/status'),
            apiRequest('/api/oss-profiles/effective-config'),
        ]);
        ossProfilesState.status = status;
        ossProfilesState.effectiveConfig = effectiveConfig;
        renderOSSProfilesPage();
    } catch (error) {
        container.innerHTML = `
            <div class="bg-red-900/20 border border-red-500/30 rounded-xl p-6 text-red-200">
                <div class="font-semibold mb-2">Failed to load OSS tuning data</div>
                <div class="text-sm mb-4">${escapeHtml(error.message)}</div>
                <button onclick="initOSSProfilesPage()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">
                    Retry
                </button>
            </div>
        `;
    }
}

function renderOSSProfilesPage() {
    const container = document.getElementById('oss-profiles-container');
    if (!container || !ossProfilesState.status || !ossProfilesState.effectiveConfig) return;

    const status = ossProfilesState.status;
    const effective = ossProfilesState.effectiveConfig;

    container.innerHTML = `
        <div class="space-y-6">
            ${renderOSSProfileSummary(status)}
            ${renderOSSProfileActions(status)}
            ${renderOSSProfileIssues(status)}
            ${renderOSSProfileInventory(status)}
            ${renderOSSEffectiveConfig(effective)}
        </div>
    `;

    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
}

function statusPill(label, tone) {
    const tones = {
        green: 'bg-green-500/15 text-green-300 border-green-500/30',
        yellow: 'bg-yellow-500/15 text-yellow-300 border-yellow-500/30',
        red: 'bg-red-500/15 text-red-300 border-red-500/30',
        blue: 'bg-blue-500/15 text-blue-300 border-blue-500/30',
        gray: 'bg-gray-500/15 text-gray-300 border-gray-500/30',
    };
    return `<span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border ${tones[tone] || tones.gray}">${escapeHtml(label)}</span>`;
}

function humanizeState(value) {
    return (value || 'unknown').replace(/_/g, ' ');
}

function renderOSSProfileSummary(status) {
    const availabilityTone = status.availability_state === 'serving' ? 'green' : status.availability_state === 'partially_serving' ? 'yellow' : 'red';
    const qualityTone = status.config_quality_state === 'healthy' ? 'green' : status.config_quality_state === 'fallback_active' ? 'yellow' : 'red';
    const riskTone = status.performance_risk_state === 'low' ? 'green' : status.performance_risk_state === 'medium' ? 'yellow' : 'red';
    const backends = status.backend_status || [];
    const installedCount = backends.reduce((sum, backend) => {
        return sum + (backend.models || []).filter(model => ['installed', 'served'].includes(model.status)).length;
    }, 0);
    const runtimeSync = status.runtime_sync || {};
    const runtimeTone = runtimeSync.state === 'pending_reload' ? 'yellow' : 'green';

    return `
        <div class="grid grid-cols-1 lg:grid-cols-5 gap-4">
            <div class="bg-dark-card border border-dark-border rounded-xl p-5">
                <div class="text-xs uppercase tracking-wide text-gray-500 mb-2">Availability</div>
                <div class="text-lg font-semibold text-white mb-2">${statusPill(humanizeState(status.availability_state), availabilityTone)}</div>
                <div class="text-sm text-gray-400">Backend reachability and assignment coverage.</div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-5">
                <div class="text-xs uppercase tracking-wide text-gray-500 mb-2">Config Quality</div>
                <div class="text-lg font-semibold text-white mb-2">${statusPill(humanizeState(status.config_quality_state), qualityTone)}</div>
                <div class="text-sm text-gray-400">Whether the live install matches the best available tuning plan.</div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-5">
                <div class="text-xs uppercase tracking-wide text-gray-500 mb-2">Performance Risk</div>
                <div class="text-lg font-semibold text-white mb-2">${statusPill(humanizeState(status.performance_risk_state), riskTone)}</div>
                <div class="text-sm text-gray-400">Computed from assignment fallbacks, missing models, and backend health.</div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-5">
                <div class="text-xs uppercase tracking-wide text-gray-500 mb-2">Live Runtime</div>
                <div class="text-sm text-white mb-2">Active: <span class="font-medium">${escapeHtml(status.active_profile || 'none')}</span></div>
                <div class="text-sm text-white mb-2">Recommended: <span class="font-medium">${escapeHtml(status.suggested_profile || 'none')}</span></div>
                <div class="text-sm text-gray-400">${installedCount} served/installed model(s)</div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-5">
                <div class="text-xs uppercase tracking-wide text-gray-500 mb-2">Runtime Sync</div>
                <div class="text-lg font-semibold text-white mb-2">${statusPill(humanizeState(runtimeSync.state || 'synchronized'), runtimeTone)}</div>
                <div class="text-sm text-gray-400">${escapeHtml(runtimeSync.required_action || 'No pending action')}</div>
                ${runtimeSync.state === 'pending_reload' ? `
                    <button onclick="retryOSSRuntimeSync()" class="mt-3 px-3 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm font-medium">
                        Retry Sync
                    </button>
                ` : ''}
            </div>
        </div>
    `;
}

function renderOSSProfileActions(status) {
    const recommended = status.suggested_profile || 'ollama_qwen_small';
    return `
        <div class="bg-dark-card border border-dark-border rounded-xl p-6">
            <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                <div>
                    <h3 class="text-lg font-semibold text-white">Profile Actions</h3>
                    <p class="text-sm text-gray-400">Preview the assignment plan first, then apply missing-only, reconcile managed fields, or overwrite profile scope.</p>
                </div>
                <div class="flex flex-wrap gap-3">
                    <button onclick="initOSSProfilesPage()" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">
                        Refresh
                    </button>
                    <button onclick="previewOSSProfile('${recommended}', 'fill_missing_only')" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">
                        Preview
                    </button>
                    <button onclick="downloadOSSEffectiveConfig()" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">
                        Export Effective Config
                    </button>
                </div>
            </div>
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <div class="border border-dark-border rounded-lg p-4">
                    <div class="font-medium text-white mb-2">Fill Missing Only</div>
                    <p class="text-sm text-gray-400 mb-4">Safe first-run action. Creates missing rows and fills blank fields without overriding operator-owned values.</p>
                    <button onclick="applyOSSProfile('${recommended}', 'fill_missing_only')" class="w-full px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">
                        Apply Recommended Defaults
                    </button>
                </div>
                <div class="border border-dark-border rounded-lg p-4">
                    <div class="font-medium text-white mb-2">Reconcile Profile</div>
                    <p class="text-sm text-gray-400 mb-4">Updates fields still owned by the active profile and leaves detached user-managed fields alone.</p>
                    <button onclick="applyOSSProfile('${recommended}', 'reconcile_profile')" class="w-full px-4 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm font-medium">
                        Reconcile Managed Fields
                    </button>
                </div>
                <div class="border border-dark-border rounded-lg p-4">
                    <div class="font-medium text-white mb-2">Overwrite Profile Scope</div>
                    <p class="text-sm text-gray-400 mb-4">Force the selected profile back onto every managed backend, model config, assignment, and gateway field.</p>
                    <button onclick="applyOSSProfile('${recommended}', 'overwrite_all')" class="w-full px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium">
                        Overwrite Managed Scope
                    </button>
                </div>
            </div>
        </div>
    `;
}

function renderOSSProfileIssues(status) {
    const issues = status.issues || [];
    return `
        <div class="bg-dark-card border border-dark-border rounded-xl p-6">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h3 class="text-lg font-semibold text-white">Diagnostics</h3>
                    <p class="text-sm text-gray-400">Computed live from backend capabilities and the current assignment plan.</p>
                </div>
                <div class="text-sm text-gray-500">${issues.length} issue(s)</div>
            </div>
            ${issues.length === 0 ? `
                <div class="bg-green-500/10 border border-green-500/30 rounded-lg p-4 text-green-200">
                    No active diagnostics issues. The current install appears ready and tuned.
                </div>
            ` : `
                <div class="space-y-3">
                    ${issues.map(issue => `
                        <div class="border border-${issue.severity === 'high' ? 'red' : 'yellow'}-500/30 bg-${issue.severity === 'high' ? 'red' : 'yellow'}-500/10 rounded-lg p-4">
                            <div class="flex items-center justify-between gap-3 mb-2">
                                <div class="font-medium text-white">${escapeHtml(issue.summary)}</div>
                                ${statusPill(issue.severity, issue.severity === 'high' ? 'red' : 'yellow')}
                            </div>
                            ${issue.detail ? `<div class="text-sm text-gray-300 mb-2">${escapeHtml(String(issue.detail))}</div>` : ''}
                            <div class="text-sm text-gray-400">${escapeHtml(issue.remediation || '')}</div>
                        </div>
                    `).join('')}
                </div>
            `}
        </div>
    `;
}

function renderOSSProfileInventory(status) {
    const backends = status.backend_status || [];
    const profiles = status.profiles || [];

    return `
        <div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
            <div class="bg-dark-card border border-dark-border rounded-xl p-6">
                <div class="flex items-center justify-between mb-4">
                    <div>
                        <h3 class="text-lg font-semibold text-white">Available Profiles</h3>
                        <p class="text-sm text-gray-400">Each profile now carries a full assignment plan with rationale.</p>
                    </div>
                    <div class="text-xs text-gray-500">${profiles.length} profile(s)</div>
                </div>
                <div class="space-y-4">
                    ${profiles.map(profile => `
                        <div class="border border-dark-border rounded-lg p-4">
                            <div class="flex items-center justify-between gap-3 mb-2">
                                <div>
                                    <div class="font-medium text-white">${escapeHtml(profile.display_name)}</div>
                                    <div class="text-xs text-gray-500">v${escapeHtml(profile.version)} · hash ${escapeHtml(profile.content_hash || '')}</div>
                                </div>
                                ${profile.recommended ? statusPill('recommended', 'blue') : ''}
                            </div>
                            <div class="text-sm text-gray-400 mb-3">${escapeHtml(profile.description || '')}</div>
                            <div class="flex flex-wrap gap-2 mb-3">
                                ${(profile.installed_matches || []).map(model => statusPill(`installed: ${model}`, 'green')).join('')}
                                ${(profile.installable_matches || []).map(model => statusPill(`installable: ${model}`, 'yellow')).join('')}
                                ${(profile.unavailable_matches || []).map(model => statusPill(`missing: ${model}`, 'red')).join('')}
                            </div>
                            <div class="text-xs text-gray-500 mb-3">Plan status: ${escapeHtml(profile.plan?.aggregated_status || 'unknown')}</div>
                            <div class="flex flex-wrap gap-2">
                                <button onclick="previewOSSProfile('${profile.name}', 'fill_missing_only')" class="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">
                                    Preview
                                </button>
                                <button onclick="applyOSSProfile('${profile.name}', 'fill_missing_only')" class="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">
                                    Initialize
                                </button>
                                <button onclick="applyOSSProfile('${profile.name}', 'reconcile_profile')" class="px-3 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm font-medium">
                                    Reconcile
                                </button>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
            <div class="bg-dark-card border border-dark-border rounded-xl p-6">
                <div class="flex items-center justify-between mb-4">
                    <div>
                        <h3 class="text-lg font-semibold text-white">Backend Inventory</h3>
                        <p class="text-sm text-gray-400">Backend-aware capability view across Ollama, MLX, OpenAI-compatible, and cloud adapters.</p>
                    </div>
                    <div>${statusPill(`${backends.length} backends`, 'blue')}</div>
                </div>
                <div class="space-y-4">
                    ${backends.map(backend => `
                        <div class="border border-dark-border rounded-lg p-4">
                            <div class="flex items-center justify-between gap-3 mb-3">
                                <div>
                                    <div class="font-medium text-white">${escapeHtml(backend.display_name)}</div>
                                    <div class="text-sm text-gray-400">${escapeHtml(backend.endpoint_url || 'provider-managed')}</div>
                                </div>
                                <div class="flex items-center gap-2">
                                    ${statusPill(backend.status, backend.healthy ? 'green' : 'yellow')}
                                    ${statusPill(backend.backend_type, 'blue')}
                                </div>
                            </div>
                            <div class="space-y-2">
                                ${(backend.models || []).map(model => {
                                    const tone = ['installed', 'served'].includes(model.status) ? 'green' : model.installability_state === 'installable' ? 'yellow' : 'gray';
                                    return `
                                        <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-2 border border-dark-border rounded-lg p-3">
                                            <div>
                                                <div class="font-medium text-white">${escapeHtml(model.name)}</div>
                                                <div class="text-xs text-gray-500">${escapeHtml(model.family || model.status)}</div>
                                            </div>
                                            <div class="flex items-center gap-2">
                                                ${statusPill(model.status, tone)}
                                                ${backend.supports_install_action && model.installability_state === 'installable' ? `
                                                    <button onclick="installOSSModel('${model.name}')" class="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">
                                                        Install
                                                    </button>
                                                ` : ''}
                                            </div>
                                        </div>
                                    `;
                                }).join('')}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        </div>
    `;
}

function renderOSSEffectiveConfig(effective) {
    const rows = effective.components || [];
    const gateway = effective.gateway || {};
    const models = effective.models || [];
    return `
        <div class="bg-dark-card border border-dark-border rounded-xl p-6">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h3 class="text-lg font-semibold text-white">Effective Runtime Config</h3>
                    <p class="text-sm text-gray-400">Resolved values with provenance and planner rationale.</p>
                </div>
                <div class="text-xs text-gray-500">Generated ${escapeHtml(new Date(effective.generated_at).toLocaleString())}</div>
            </div>
            <div class="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-6">
                <div class="border border-dark-border rounded-lg p-4">
                    <div class="flex items-center justify-between gap-3 mb-3">
                        <div>
                            <div class="font-medium text-white">Gateway Runtime</div>
                            <div class="text-xs text-gray-500">Intent routing defaults and fallback endpoint.</div>
                        </div>
                        <div class="flex gap-2">
                            <button onclick="resetOSSRecord('GatewayConfig', '1')" class="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">Reset</button>
                            <button onclick="detachOSSRecord('GatewayConfig', '1')" class="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">Detach</button>
                        </div>
                    </div>
                    <div class="space-y-3 text-sm">
                        ${renderGatewayField('Intent model', gateway.intent_model, 'GatewayConfig', '1', 'intent_model')}
                        ${renderGatewayField('Intent temperature', gateway.intent_temperature, 'GatewayConfig', '1', 'intent_temperature')}
                        ${renderGatewayField('Intent max tokens', gateway.intent_max_tokens, 'GatewayConfig', '1', 'intent_max_tokens')}
                        ${renderGatewayField('Intent timeout', gateway.intent_timeout_seconds, 'GatewayConfig', '1', 'intent_timeout_seconds')}
                        <div class="border border-dark-border rounded-lg p-3">
                            <div class="text-xs uppercase tracking-wide text-gray-500 mb-1">Ollama fallback URL</div>
                            <div class="text-white break-all">${escapeHtml(String(gateway.ollama_fallback_url || 'unset'))}</div>
                        </div>
                    </div>
                </div>
                <div class="border border-dark-border rounded-lg p-4">
                    <div class="flex items-center justify-between gap-3 mb-3">
                        <div>
                            <div class="font-medium text-white">Model Configurations</div>
                            <div class="text-xs text-gray-500">Profile-owned defaults by model.</div>
                        </div>
                        <div class="text-xs text-gray-500">${models.length} model row(s)</div>
                    </div>
                    <div class="space-y-3 max-h-96 overflow-y-auto pr-1">
                        ${models.map(model => `
                            <div class="border border-dark-border rounded-lg p-3">
                                <div class="flex items-center justify-between gap-3 mb-2">
                                    <div>
                                        <div class="font-medium text-white">${escapeHtml(model.model_name)}</div>
                                        <div class="text-xs text-gray-500">${escapeHtml(model.backend_type || 'unknown backend')} · ${escapeHtml(model.source || 'unset')}</div>
                                    </div>
                                    <div class="flex gap-2">
                                        <button onclick="resetOSSRecord('ModelConfiguration', '${escapeHtml(model.identifier)}')" class="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">Reset</button>
                                        <button onclick="detachOSSRecord('ModelConfiguration', '${escapeHtml(model.identifier)}')" class="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">Detach</button>
                                    </div>
                                </div>
                                <div class="grid grid-cols-2 gap-3 text-sm">
                                    <div>
                                        <div class="text-xs uppercase tracking-wide text-gray-500">Max tokens</div>
                                        <div class="text-white">${escapeHtml(String(model.max_tokens ?? 'unset'))}</div>
                                    </div>
                                    <div>
                                        <div class="text-xs uppercase tracking-wide text-gray-500">Timeout</div>
                                        <div class="text-white">${escapeHtml(String(model.timeout_seconds ?? 'unset'))}s</div>
                                    </div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>
            <div class="overflow-x-auto">
                <table class="crud-table">
                    <thead>
                        <tr>
                            <th><span>Component</span></th>
                            <th><span>Model</span></th>
                            <th><span>Backend</span></th>
                            <th><span>Max Tokens</span></th>
                            <th><span>Temperature</span></th>
                            <th><span>Timeout</span></th>
                            <th><span>Planner</span></th>
                            <th><span>Actions</span></th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.map(row => `
                            <tr>
                                <td>
                                    <div class="font-medium text-white">${escapeHtml(row.display_name)}</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.category)}</div>
                                </td>
                                <td>
                                    <div>${escapeHtml(String(row.model_name.value))}</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.model_name.source)}</div>
                                    <div class="mt-2 flex gap-2">
                                        <button onclick="resetOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'model_name')" class="text-xs text-blue-300 hover:text-blue-200">reset</button>
                                        <button onclick="detachOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'model_name')" class="text-xs text-gray-400 hover:text-gray-200">detach</button>
                                    </div>
                                </td>
                                <td>
                                    <div>${escapeHtml(String(row.backend_type.value))}</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.backend_type.source)}</div>
                                    <div class="mt-2 flex gap-2">
                                        <button onclick="resetOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'backend_type')" class="text-xs text-blue-300 hover:text-blue-200">reset</button>
                                        <button onclick="detachOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'backend_type')" class="text-xs text-gray-400 hover:text-gray-200">detach</button>
                                    </div>
                                </td>
                                <td>
                                    <div>${escapeHtml(String(row.max_tokens.value))}</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.max_tokens.source)}</div>
                                    <div class="mt-2 flex gap-2">
                                        <button onclick="resetOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'max_tokens')" class="text-xs text-blue-300 hover:text-blue-200">reset</button>
                                        <button onclick="detachOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'max_tokens')" class="text-xs text-gray-400 hover:text-gray-200">detach</button>
                                    </div>
                                </td>
                                <td>
                                    <div>${escapeHtml(String(row.temperature.value))}</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.temperature.source)}</div>
                                    <div class="mt-2 flex gap-2">
                                        <button onclick="resetOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'temperature')" class="text-xs text-blue-300 hover:text-blue-200">reset</button>
                                        <button onclick="detachOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'temperature')" class="text-xs text-gray-400 hover:text-gray-200">detach</button>
                                    </div>
                                </td>
                                <td>
                                    <div>${escapeHtml(String(row.timeout_seconds.value))}s</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.timeout_seconds.source)}</div>
                                    <div class="mt-2 flex gap-2">
                                        <button onclick="resetOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'timeout_seconds')" class="text-xs text-blue-300 hover:text-blue-200">reset</button>
                                        <button onclick="detachOSSField('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}', 'timeout_seconds')" class="text-xs text-gray-400 hover:text-gray-200">detach</button>
                                    </div>
                                </td>
                                <td>
                                    <div class="text-sm text-white">${escapeHtml(row.planner?.decision_state || 'unknown')}</div>
                                    <div class="text-xs text-gray-500">${escapeHtml(row.planner?.rationale_summary || '')}</div>
                                </td>
                                <td>
                                    <div class="flex flex-col gap-2 min-w-[120px]">
                                        <button onclick="resetOSSRecord('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}')" class="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">Reset Record</button>
                                        <button onclick="detachOSSRecord('${escapeHtml(row.record_type)}', '${escapeHtml(row.identifier)}')" class="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">Detach Record</button>
                                    </div>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function renderGatewayField(label, field, recordType, identifier, fieldName) {
    return `
        <div class="border border-dark-border rounded-lg p-3">
            <div class="flex items-start justify-between gap-3">
                <div>
                    <div class="text-xs uppercase tracking-wide text-gray-500 mb-1">${escapeHtml(label)}</div>
                    <div class="text-white">${escapeHtml(String(field?.value ?? 'unset'))}</div>
                    <div class="text-xs text-gray-500 mt-1">${escapeHtml(field?.source || 'unset')}</div>
                </div>
                <div class="flex gap-2">
                    <button onclick="resetOSSField('${recordType}', '${identifier}', '${fieldName}')" class="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium">Reset</button>
                    <button onclick="detachOSSField('${recordType}', '${identifier}', '${fieldName}')" class="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm font-medium">Detach</button>
                </div>
            </div>
        </div>
    `;
}

async function applyOSSProfile(profileName, mode) {
    const confirmed = mode !== 'overwrite_all' || confirm(`Overwrite all profile-managed values with ${profileName}?`);
    if (!confirmed) return;

    try {
        const result = await apiRequest('/api/oss-profiles/apply', {
            method: 'POST',
            body: JSON.stringify({
                profile_name: profileName,
                mode,
            }),
        });
        showSuccess(`Applied ${profileName} with ${mode}. Touched ${result.touched_fields} field(s).`);
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to apply profile: ${error.message}`);
    }
}

async function installOSSModel(modelName) {
    try {
        showSuccess(`Installing ${modelName}...`);
        await apiRequest('/api/oss-profiles/install-model', {
            method: 'POST',
            body: JSON.stringify({ model_name: modelName }),
        });
        showSuccess(`Installed ${modelName}. Refreshing diagnostics...`);
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to install model: ${error.message}`);
    }
}

async function previewOSSProfile(profileName, mode) {
    try {
        const preview = await apiRequest('/api/oss-profiles/preview', {
            method: 'POST',
            body: JSON.stringify({
                profile_name: profileName,
                mode,
            }),
        });
        ossProfilesState.preview = preview;
        const changes = (preview.changes || []).map(change =>
            `${change.component_name}: ${change.current_model || 'unset'} -> ${change.planned_model || 'none'} (${change.decision_state})`
        ).join('\n');
        const impacts = preview.reload_plan?.required_action || 'none';
        alert(`Profile: ${profileName}\nMode: ${mode}\nReload impact: ${impacts}\n\n${changes || 'No assignment changes detected.'}`);
    } catch (error) {
        showError(`Failed to preview profile: ${error.message}`);
    }
}

async function retryOSSRuntimeSync() {
    try {
        const result = await apiRequest('/api/oss-profiles/runtime-sync/retry', { method: 'POST' });
        showSuccess(`Runtime sync state: ${result.runtime_sync?.state || 'updated'}`);
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to retry runtime sync: ${error.message}`);
    }
}

async function resetOSSField(recordType, identifier, fieldName) {
    try {
        const result = await apiRequest('/api/oss-profiles/fields/reset', {
            method: 'POST',
            body: JSON.stringify({ record_type: recordType, identifier, field_name: fieldName }),
        });
        showSuccess(`Reset ${recordType}:${identifier}.${fieldName} to profile-managed value.`);
        if (result.runtime_sync?.state === 'pending_reload') {
            showSuccess(`Runtime sync now requires ${result.runtime_sync.required_action}.`);
        }
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to reset field: ${error.message}`);
    }
}

async function detachOSSField(recordType, identifier, fieldName) {
    const confirmed = confirm(`Detach ${recordType}:${identifier}.${fieldName} from profile management?`);
    if (!confirmed) return;
    try {
        const result = await apiRequest('/api/oss-profiles/fields/detach', {
            method: 'POST',
            body: JSON.stringify({ record_type: recordType, identifier, field_name: fieldName }),
        });
        showSuccess(`Detached ${recordType}:${identifier}.${fieldName}.`);
        if (result.runtime_sync?.state === 'pending_reload') {
            showSuccess(`Runtime sync now requires ${result.runtime_sync.required_action}.`);
        }
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to detach field: ${error.message}`);
    }
}

async function resetOSSRecord(recordType, identifier) {
    try {
        const result = await apiRequest('/api/oss-profiles/records/reset', {
            method: 'POST',
            body: JSON.stringify({ record_type: recordType, identifier }),
        });
        showSuccess(`Reset ${recordType}:${identifier}. Updated ${result.touched_fields || 0} field(s).`);
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to reset record: ${error.message}`);
    }
}

async function detachOSSRecord(recordType, identifier) {
    const confirmed = confirm(`Detach all profile-managed fields for ${recordType}:${identifier}?`);
    if (!confirmed) return;
    try {
        const result = await apiRequest('/api/oss-profiles/records/detach', {
            method: 'POST',
            body: JSON.stringify({ record_type: recordType, identifier }),
        });
        showSuccess(`Detached ${recordType}:${identifier}. Updated ${result.touched_fields || 0} field(s).`);
        await initOSSProfilesPage();
    } catch (error) {
        showError(`Failed to detach record: ${error.message}`);
    }
}

function downloadOSSEffectiveConfig() {
    if (!ossProfilesState.effectiveConfig) return;
    const blob = new Blob([JSON.stringify(ossProfilesState.effectiveConfig, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `athena-effective-config-${Date.now()}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}
