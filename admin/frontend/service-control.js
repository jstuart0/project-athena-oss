/**
 * Service Control - Frontend JavaScript
 * Handles start/stop/restart of services and Ollama models
 */

// State
let services = [];
let ragServices = [];
let ollamaModels = [];
let ollamaHealth = null;
let restartHistory = [];

// Service dependency graph
const SERVICE_DEPENDENCIES = {
    'gateway': {
        dependents: ['orchestrator', 'voice-pipelines'],
        warning: 'Stopping Gateway will disconnect all voice clients and API consumers.'
    },
    'orchestrator': {
        dependents: ['rag-services'],
        warning: 'Stopping Orchestrator will halt all AI query processing.'
    },
    'ollama': {
        dependents: ['orchestrator'],
        warning: 'Stopping Ollama will disable all local LLM inference.'
    },
    'qdrant': {
        dependents: ['orchestrator'],
        warning: 'Stopping Qdrant will disable RAG vector search.'
    },
    'redis': {
        dependents: ['gateway', 'orchestrator'],
        warning: 'Stopping Redis will clear session cache and rate limiting state.'
    }
};

// Service restart macros
const SERVICE_MACROS = [
    {
        id: 'voice-pipeline-restart',
        name: 'Restart Voice Pipeline',
        description: 'Gateway + Orchestrator (recommended order)',
        services: ['gateway', 'orchestrator'],
        icon: 'audio-waveform'
    },
    {
        id: 'full-stack-restart',
        name: 'Restart Full Stack',
        description: 'All core services in dependency order',
        services: ['redis', 'qdrant', 'ollama', 'orchestrator', 'gateway'],
        icon: 'refresh-cw'
    },
    {
        id: 'llm-refresh',
        name: 'Refresh LLM Layer',
        description: 'Ollama + Orchestrator to reload models',
        services: ['ollama', 'orchestrator'],
        icon: 'brain'
    }
];

// ============================================================================
// Initialization
// ============================================================================

async function loadServiceControl() {
    await Promise.all([
        loadServices(),
        loadRagServicesFromRegistry(),
        loadOllamaHealth(),
        loadOllamaModels(),
        loadRestartHistory()
    ]);
    renderMacros();
    renderRestartTimeline();
}

// ============================================================================
// Data Loading
// ============================================================================

async function loadServices() {
    try {
        const response = await apiRequest('/api/services');
        // API returns {services: [...]} not plain array
        services = response.services || response || [];
        renderServiceTables();
        updateServiceCounts();
    } catch (error) {
        console.error('Failed to load services:', error);
        showServiceError('Failed to load services');
    }
}

async function loadRagServicesFromRegistry() {
    try {
        const response = await apiRequest('/api/service-registry/services');
        // Service registry returns {services: [...], total_services, healthy_services, overall_health}
        ragServices = response.services || [];
        renderRagServicesTable();
        updateRagServiceCounts(response);
    } catch (error) {
        console.error('Failed to load RAG services from registry:', error);
        const container = document.getElementById('rag-services-table');
        if (container) {
            container.innerHTML = '<div class="text-center text-red-400 py-8">Failed to load RAG services from registry</div>';
        }
    }
}

async function loadOllamaHealth() {
    try {
        ollamaHealth = await apiRequest('/api/service-control/ollama/health');
        renderOllamaStatus();
    } catch (error) {
        console.error('Failed to load Ollama health:', error);
        ollamaHealth = {
            healthy: false,
            status: 'error',
            api_reachable: false,
            models_loaded: 0,
            version: null
        };
        renderOllamaStatus();
    }
}

async function loadOllamaModels() {
    try {
        ollamaModels = await apiRequest('/api/service-control/ollama/models');
        renderOllamaModelsTable();
        updateModelCounts();
    } catch (error) {
        console.error('Failed to load Ollama models:', error);
        const container = document.getElementById('ollama-models-table');
        if (container) {
            container.innerHTML = `
                <div class="text-center text-red-400 py-8">Failed to load Ollama models - Ollama may be offline</div>
            `;
        }
    }
}

async function refreshServiceStatus() {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Refreshing...';

    try {
        await apiRequest('/api/service-control/refresh-status', { method: 'POST' });

        // Wait a moment for health checks to complete
        await new Promise(resolve => setTimeout(resolve, 2000));

        await loadServiceControl();
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="refresh-cw" class="w-4 h-4 inline-block mr-1"></i> Refresh Status';
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
}

// ============================================================================
// Rendering
// ============================================================================

function renderServiceTables() {
    const types = {
        'core': 'core-services-table',
        'llm': 'core-services-table', // Group with core
        'infrastructure': 'infrastructure-services-table'
        // Note: RAG services are now loaded from service registry separately
    };

    // Group services by type (use service_type or infer from service_name)
    // SKIP RAG services - those come from the service registry now
    const grouped = {};
    for (const service of services) {
        let serviceType = service.service_type;
        // Infer type from service name if not provided
        if (!serviceType) {
            const name = (service.service_name || '').toLowerCase();
            // Skip RAG services - they're loaded from registry
            if (name.includes('rag') || name.includes('weather') || name.includes('sports') ||
                name.includes('news') || name.includes('stocks') || name.includes('dining') ||
                name.includes('flight') || name.includes('airport') || name.includes('recipe') ||
                name.includes('streaming') || name.includes('event') || name.includes('websearch')) {
                continue; // Skip - handled by loadRagServicesFromRegistry()
            } else if (name.includes('ollama') || name.includes('llm')) {
                serviceType = 'llm';
            } else if (name.includes('redis') || name.includes('postgres') || name.includes('qdrant')) {
                serviceType = 'infrastructure';
            } else {
                serviceType = 'core';
            }
        } else if (serviceType === 'rag') {
            continue; // Skip - handled by loadRagServicesFromRegistry()
        }
        const tableId = types[serviceType] || 'core-services-table';
        if (!grouped[tableId]) grouped[tableId] = [];
        grouped[tableId].push(service);
    }

    // Render each table (core, llm, infrastructure)
    for (const [tableId, serviceList] of Object.entries(grouped)) {
        renderServiceTable(tableId, serviceList);
    }

    // Handle empty tables
    for (const tableId of Object.values(types)) {
        if (!grouped[tableId]) {
            const container = document.getElementById(tableId);
            if (container) {
                container.innerHTML = '<div class="text-center text-gray-400 py-8">No services</div>';
            }
        }
    }
}

function renderServiceTable(containerId, serviceList) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (serviceList.length === 0) {
        container.innerHTML = '<div class="text-center text-gray-400 py-8">No services</div>';
        return;
    }

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Service${typeof infoIcon === 'function' ? infoIcon('service-name') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Endpoint${typeof infoIcon === 'function' ? infoIcon('service-endpoint') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Status${typeof infoIcon === 'function' ? infoIcon('service-status') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${serviceList.map(s => renderServiceRow(s)).join('')}
            </tbody>
        </table>
    `;
}

function renderServiceRow(service) {
    // Normalize field names - API uses different names than expected
    const displayName = service.display_name || service.service_name || 'Unknown';
    const host = service.host || service.ip_address || 'unknown';
    const isRunning = service.is_running ?? (service.status === 'running' || service.status === 'healthy' || service.status === 'online');
    const serviceName = service.service_name || service.name;

    const statusBadge = isRunning
        ? '<span class="px-2 py-1 text-xs rounded bg-green-900 text-green-300">● Running</span>'
        : '<span class="px-2 py-1 text-xs rounded bg-red-900 text-red-300">● Stopped</span>';

    const errorText = service.last_error
        ? `<div class="text-xs text-red-400 mt-1">${escapeHtml(service.last_error.substring(0, 100))}</div>`
        : '';

    return `
        <tr class="hover:bg-gray-800/50">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(displayName)}</div>
                <div class="text-xs text-gray-500">${escapeHtml(service.description || '')}</div>
            </td>
            <td class="px-4 py-3">
                <div class="text-sm text-gray-300">${host}:${service.port}</div>
                ${errorText}
            </td>
            <td class="px-4 py-3">${statusBadge}</td>
            <td class="px-4 py-3">
                <div class="flex gap-2">
                    ${isRunning ? `
                        <button onclick="stopService('${serviceName}')"
                                class="px-2 py-1 text-xs bg-red-600 hover:bg-red-700 text-white rounded">
                            Stop
                        </button>
                        <button onclick="restartService('${serviceName}')"
                                class="px-2 py-1 text-xs bg-yellow-600 hover:bg-yellow-700 text-white rounded">
                            Restart
                        </button>
                    ` : `
                        <button onclick="startService('${serviceName}')"
                                class="px-2 py-1 text-xs bg-green-600 hover:bg-green-700 text-white rounded">
                            Start
                        </button>
                    `}
                </div>
            </td>
        </tr>
    `;
}

function renderOllamaStatus() {
    const container = document.getElementById('ollama-status-panel');
    if (!container) return;

    if (!ollamaHealth) {
        container.innerHTML = '<div class="text-center text-gray-400 py-4">Loading Ollama status...</div>';
        return;
    }

    let statusBadge, statusColor;
    switch (ollamaHealth.status) {
        case 'healthy':
            statusBadge = '<span class="px-3 py-1 text-sm rounded-full bg-green-900 text-green-300">Healthy</span>';
            statusColor = 'green';
            break;
        case 'idle':
            statusBadge = '<span class="px-3 py-1 text-sm rounded-full bg-blue-900 text-blue-300">Idle (No Models Loaded)</span>';
            statusColor = 'blue';
            break;
        case 'offline':
            statusBadge = '<span class="px-3 py-1 text-sm rounded-full bg-red-900 text-red-300">Offline</span>';
            statusColor = 'red';
            break;
        case 'control_agent_offline':
            statusBadge = '<span class="px-3 py-1 text-sm rounded-full bg-yellow-900 text-yellow-300">Control Agent Offline</span>';
            statusColor = 'yellow';
            break;
        default:
            statusBadge = '<span class="px-3 py-1 text-sm rounded-full bg-gray-700 text-gray-300">Unknown</span>';
            statusColor = 'gray';
    }

    const isOnline = ollamaHealth.healthy || ollamaHealth.api_reachable;

    container.innerHTML = `
        <div class="flex items-center justify-between p-4 bg-gray-800 rounded-lg">
            <div class="flex items-center gap-4">
                <div class="w-3 h-3 rounded-full ${isOnline ? 'bg-green-500 animate-pulse' : 'bg-red-500'}"></div>
                <div>
                    <div class="flex items-center gap-3">
                        <span class="text-lg font-semibold text-white">Ollama LLM Server</span>
                        ${statusBadge}
                    </div>
                    <div class="text-sm text-gray-400 mt-1">
                        ${ollamaHealth.version ? `Version: ${escapeHtml(ollamaHealth.version)}` : 'Version: Unknown'}
                        | Models Loaded: ${ollamaHealth.models_loaded || 0}
                        | Host: ${ollamaHealth.host || 'localhost:11434'}
                    </div>
                </div>
            </div>
            <div class="flex gap-2">
                ${isOnline ? `
                    <button onclick="stopOllama()"
                            class="px-3 py-2 text-sm bg-red-600 hover:bg-red-700 text-white rounded transition">
                        Stop
                    </button>
                    <button onclick="restartOllama()"
                            class="px-3 py-2 text-sm bg-yellow-600 hover:bg-yellow-700 text-white rounded transition">
                        Restart
                    </button>
                ` : `
                    <button onclick="startOllama()"
                            class="px-3 py-2 text-sm bg-green-600 hover:bg-green-700 text-white rounded transition">
                        Start Ollama
                    </button>
                `}
                <button onclick="refreshOllamaHealth()"
                        class="px-3 py-2 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded transition">
                    Refresh
                </button>
            </div>
        </div>
    `;
}

function renderOllamaModelsTable() {
    const container = document.getElementById('ollama-models-table');
    if (!container) return;

    if (!ollamaHealth || !ollamaHealth.healthy) {
        container.innerHTML = '<div class="text-center text-gray-400 py-8">Ollama is offline - start it to manage models</div>';
        return;
    }

    if (ollamaModels.length === 0) {
        container.innerHTML = '<div class="text-center text-gray-400 py-8">No models available</div>';
        return;
    }

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Model${typeof infoIcon === 'function' ? infoIcon('ollama-model-name') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Size${typeof infoIcon === 'function' ? infoIcon('ollama-model-size') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Status${typeof infoIcon === 'function' ? infoIcon('ollama-model-status') : ''}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${ollamaModels.map(m => renderModelRow(m)).join('')}
            </tbody>
        </table>
    `;
}

function renderModelRow(model) {
    const statusBadge = model.loaded
        ? '<span class="px-2 py-1 text-xs rounded bg-green-900 text-green-300">● Loaded</span>'
        : '<span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-400">○ Not Loaded</span>';

    return `
        <tr class="hover:bg-gray-800/50">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(model.name)}</div>
            </td>
            <td class="px-4 py-3 text-sm text-gray-300">${formatBytes(model.size)}</td>
            <td class="px-4 py-3">${statusBadge}</td>
            <td class="px-4 py-3">
                ${model.loaded ? `
                    <button onclick="unloadModel('${escapeHtml(model.name)}')"
                            class="px-2 py-1 text-xs bg-yellow-600 hover:bg-yellow-700 text-white rounded">
                        Unload
                    </button>
                ` : `
                    <button onclick="loadModel('${escapeHtml(model.name)}')"
                            class="px-2 py-1 text-xs bg-green-600 hover:bg-green-700 text-white rounded">
                        Load
                    </button>
                `}
            </td>
        </tr>
    `;
}

function updateServiceCounts() {
    const isServiceRunning = (s) => s.is_running ?? (s.status === 'running' || s.status === 'healthy' || s.status === 'online');
    const running = services.filter(isServiceRunning).length;
    const stopped = services.filter(s => !isServiceRunning(s)).length;

    document.getElementById('services-running-count').textContent = running;
    document.getElementById('services-stopped-count').textContent = stopped;
}

// ============================================================================
// RAG Services from Registry (Source of Truth)
// ============================================================================

function renderRagServicesTable() {
    const container = document.getElementById('rag-services-table');
    if (!container) return;

    if (ragServices.length === 0) {
        container.innerHTML = '<div class="text-center text-gray-400 py-8">No RAG services registered</div>';
        return;
    }

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Service</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Endpoint</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${ragServices.map(s => renderRagServiceRow(s)).join('')}
            </tbody>
        </table>
    `;
}

function renderRagServiceRow(service) {
    const displayName = service.display_name || service.name || 'Unknown';
    const endpointUrl = service.endpoint_url || '';
    const host = service.host || 'unknown';
    const port = service.port || 0;

    // Status badge based on registry health check
    let statusBadge;
    switch (service.status) {
        case 'healthy':
            statusBadge = '<span class="px-2 py-1 text-xs rounded bg-green-900 text-green-300">● Healthy</span>';
            break;
        case 'unhealthy':
        case 'degraded':
            statusBadge = '<span class="px-2 py-1 text-xs rounded bg-yellow-900 text-yellow-300">● Degraded</span>';
            break;
        case 'offline':
        case 'error':
        case 'timeout':
            statusBadge = '<span class="px-2 py-1 text-xs rounded bg-red-900 text-red-300">● Offline</span>';
            break;
        case 'disabled':
            statusBadge = '<span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-400">○ Disabled</span>';
            break;
        default:
            statusBadge = '<span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-400">? Unknown</span>';
    }

    const healthMessage = service.health_message
        ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(service.health_message.substring(0, 100))}</div>`
        : '';

    const isEnabled = service.enabled !== false;

    return `
        <tr class="hover:bg-gray-800/50">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(displayName)}</div>
                <div class="text-xs text-gray-500">${escapeHtml(service.name || '')}</div>
            </td>
            <td class="px-4 py-3">
                <div class="text-sm text-gray-300">${host}:${port}</div>
                ${healthMessage}
            </td>
            <td class="px-4 py-3">${statusBadge}</td>
            <td class="px-4 py-3">
                <div class="flex gap-2">
                    <button onclick="toggleRagService('${escapeHtml(service.name)}')"
                            class="px-2 py-1 text-xs ${isEnabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded">
                        ${isEnabled ? 'Disable' : 'Enable'}
                    </button>
                    <button onclick="refreshRagService('${escapeHtml(service.name)}')"
                            class="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded">
                        Refresh
                    </button>
                </div>
            </td>
        </tr>
    `;
}

function updateRagServiceCounts(registryResponse) {
    const total = registryResponse.total_services || ragServices.length;
    const healthy = registryResponse.healthy_services || ragServices.filter(s => s.status === 'healthy').length;
    const offline = total - healthy;

    // Update RAG-specific counters if they exist
    const ragRunningEl = document.getElementById('rag-services-running-count');
    const ragStoppedEl = document.getElementById('rag-services-stopped-count');

    if (ragRunningEl) ragRunningEl.textContent = healthy;
    if (ragStoppedEl) ragStoppedEl.textContent = offline;
}

async function toggleRagService(serviceName) {
    try {
        const result = await apiRequest(`/api/service-registry/services/${encodeURIComponent(serviceName)}/toggle`, {
            method: 'POST'
        });
        showToast(result.message, 'success');
        await loadRagServicesFromRegistry();
    } catch (error) {
        showToast(`Failed to toggle ${serviceName}: ${error.message}`, 'error');
    }
}

async function refreshRagService(serviceName) {
    try {
        const result = await apiRequest(`/api/service-registry/services/${encodeURIComponent(serviceName)}/refresh`, {
            method: 'POST'
        });
        showToast(result.message, 'success');
        await loadRagServicesFromRegistry();
    } catch (error) {
        showToast(`Failed to refresh ${serviceName}: ${error.message}`, 'error');
    }
}

function updateModelCounts() {
    const loaded = ollamaModels.filter(m => m.loaded).length;
    const total = ollamaModels.length;

    document.getElementById('ollama-models-loaded').textContent = loaded;
    document.getElementById('ollama-models-total').textContent = total;
}

// ============================================================================
// Service Actions
// ============================================================================

async function startService(serviceName) {
    await serviceAction(serviceName, 'start');
}

async function stopService(serviceName) {
    if (!confirm(`Are you sure you want to stop ${serviceName}?`)) return;
    await serviceAction(serviceName, 'stop');
}

async function restartService(serviceName) {
    await serviceAction(serviceName, 'restart');
}

async function serviceAction(serviceName, action) {
    try {
        const result = await apiRequest(`/api/service-control/${serviceName}/${action}`, {
            method: 'POST'
        });

        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(result.message, 'warning');
        }

        await loadServices();

    } catch (error) {
        showToast(`Failed to ${action} ${serviceName}: ${error.message}`, 'error');
    }
}

// ============================================================================
// Model Actions
// ============================================================================

async function loadModel(modelName) {
    try {
        showToast(`Loading ${modelName}... This may take a moment.`, 'info');

        const result = await apiRequest(`/api/service-control/ollama/models/${encodeURIComponent(modelName)}/load`, {
            method: 'POST'
        });

        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(result.message, 'error');
        }

        await loadOllamaModels();

    } catch (error) {
        showToast(`Failed to load model: ${error.message}`, 'error');
    }
}

async function unloadModel(modelName) {
    try {
        const result = await apiRequest(`/api/service-control/ollama/models/${encodeURIComponent(modelName)}/unload`, {
            method: 'POST'
        });

        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(result.message, 'error');
        }

        await loadOllamaModels();

    } catch (error) {
        showToast(`Failed to unload model: ${error.message}`, 'error');
    }
}

// ============================================================================
// Ollama Service Control
// ============================================================================

async function startOllama() {
    try {
        showToast('Starting Ollama... This may take a moment.', 'info');

        const result = await apiRequest('/api/service-control/ollama/start', {
            method: 'POST'
        });

        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(result.message, 'error');
        }

        // Refresh both health and models
        await loadOllamaHealth();
        await loadOllamaModels();

    } catch (error) {
        showToast(`Failed to start Ollama: ${error.message}`, 'error');
    }
}

async function stopOllama() {
    if (!confirm('Are you sure you want to stop Ollama? This will unload all models.')) return;

    try {
        const result = await apiRequest('/api/service-control/ollama/stop', {
            method: 'POST'
        });

        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(result.message, 'error');
        }

        // Refresh health and models
        await loadOllamaHealth();
        await loadOllamaModels();

    } catch (error) {
        showToast(`Failed to stop Ollama: ${error.message}`, 'error');
    }
}

async function restartOllama() {
    try {
        showToast('Restarting Ollama... This may take a moment.', 'info');

        const result = await apiRequest('/api/service-control/ollama/restart', {
            method: 'POST'
        });

        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(result.message, 'error');
        }

        // Wait a bit then refresh
        await new Promise(resolve => setTimeout(resolve, 2000));
        await loadOllamaHealth();
        await loadOllamaModels();

    } catch (error) {
        showToast(`Failed to restart Ollama: ${error.message}`, 'error');
    }
}

async function refreshOllamaHealth() {
    showToast('Refreshing Ollama status...', 'info');
    await loadOllamaHealth();
    await loadOllamaModels();
    showToast('Ollama status refreshed', 'success');
}

// ============================================================================
// Utilities
// ============================================================================

function showServiceError(message) {
    ['core-services-table', 'rag-services-table', 'infrastructure-services-table'].forEach(id => {
        const container = document.getElementById(id);
        if (container) {
            container.innerHTML = `<div class="text-center text-red-400 py-8">${escapeHtml(message)}</div>`;
        }
    });
}

// formatBytes, escapeHtml, and showNotification are now provided by utils.js

// ============================================================================
// Runbook: Timeline, Macros, and Dependency Warnings
// ============================================================================

/**
 * Load restart history from audit log
 */
async function loadRestartHistory() {
    try {
        const response = await apiRequest('/api/audit?action_type=service_restart&limit=20');
        restartHistory = response.items || response || [];
    } catch (error) {
        console.warn('Failed to load restart history:', error);
        restartHistory = [];
    }
}

/**
 * Render the service macros section
 */
function renderMacros() {
    const container = document.getElementById('service-macros');
    if (!container) return;

    container.innerHTML = `
        <div class="mb-4">
            <h3 class="text-lg font-semibold text-white mb-2">Quick Actions</h3>
            <p class="text-sm text-gray-400">Common service restart combinations</p>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            ${SERVICE_MACROS.map(macro => `
                <button onclick="executeMacro('${macro.id}')"
                        class="p-4 bg-dark-elevated hover:bg-gray-700 rounded-lg border border-dark-border transition-all text-left group">
                    <div class="flex items-center gap-3 mb-2">
                        <div class="p-2 bg-blue-500/20 rounded-lg">
                            <i data-lucide="${macro.icon}" class="w-5 h-5 text-blue-400"></i>
                        </div>
                        <span class="font-medium text-white">${escapeHtml(macro.name)}</span>
                    </div>
                    <p class="text-sm text-gray-400">${escapeHtml(macro.description)}</p>
                    <div class="mt-3 flex flex-wrap gap-1">
                        ${macro.services.map(s => `
                            <span class="px-2 py-0.5 text-xs bg-gray-700 text-gray-300 rounded">${escapeHtml(s)}</span>
                        `).join('')}
                    </div>
                </button>
            `).join('')}
        </div>
    `;

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

/**
 * Render the restart timeline
 */
function renderRestartTimeline() {
    const container = document.getElementById('restart-timeline');
    if (!container) return;

    if (restartHistory.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-500 py-8">
                <i data-lucide="history" class="w-8 h-8 mx-auto mb-2 opacity-50"></i>
                <p>No recent restart history</p>
            </div>
        `;
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }

    container.innerHTML = `
        <div class="mb-4">
            <h3 class="text-lg font-semibold text-white mb-2">Restart History</h3>
            <p class="text-sm text-gray-400">Recent service restart events</p>
        </div>
        <div class="relative">
            <div class="absolute left-4 top-0 bottom-0 w-0.5 bg-dark-border"></div>
            <div class="space-y-4">
                ${restartHistory.map(event => renderTimelineEvent(event)).join('')}
            </div>
        </div>
    `;

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

/**
 * Render a single timeline event
 */
function renderTimelineEvent(event) {
    const timestamp = new Date(event.created_at || event.timestamp);
    const timeAgo = formatTimeAgo(timestamp);
    const serviceName = event.entity_name || event.service_name || 'Unknown';
    const action = event.action_type || event.action || 'restart';
    const user = event.user_email || event.user || 'System';

    let iconColor = 'text-blue-400';
    let bgColor = 'bg-blue-500/20';
    let icon = 'refresh-cw';

    if (action.includes('stop')) {
        iconColor = 'text-red-400';
        bgColor = 'bg-red-500/20';
        icon = 'square';
    } else if (action.includes('start')) {
        iconColor = 'text-green-400';
        bgColor = 'bg-green-500/20';
        icon = 'play';
    }

    return `
        <div class="relative pl-10">
            <div class="absolute left-2 w-4 h-4 rounded-full ${bgColor} flex items-center justify-center">
                <i data-lucide="${icon}" class="w-2.5 h-2.5 ${iconColor}"></i>
            </div>
            <div class="bg-dark-elevated rounded-lg p-3 border border-dark-border">
                <div class="flex items-center justify-between">
                    <span class="font-medium text-white">${escapeHtml(serviceName)}</span>
                    <span class="text-xs text-gray-500">${escapeHtml(timeAgo)}</span>
                </div>
                <p class="text-sm text-gray-400 mt-1">${escapeHtml(action)} by ${escapeHtml(user)}</p>
            </div>
        </div>
    `;
}

/**
 * Format time ago string
 */
function formatTimeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);

    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

/**
 * Execute a service macro
 */
async function executeMacro(macroId) {
    const macro = SERVICE_MACROS.find(m => m.id === macroId);
    if (!macro) return;

    // Show confirmation modal with dependency info
    const confirmed = await showServiceConfirmModal({
        title: macro.name,
        message: `This will restart the following services in order:`,
        services: macro.services,
        action: 'restart'
    });

    if (!confirmed) return;

    // Execute restarts in sequence
    showToast(`Executing ${macro.name}...`, 'info');

    for (const serviceName of macro.services) {
        try {
            showToast(`Restarting ${serviceName}...`, 'info');
            await apiRequest(`/api/service-control/${serviceName}/restart`, { method: 'POST' });
            // Wait between restarts
            await new Promise(resolve => setTimeout(resolve, 2000));
        } catch (error) {
            showToast(`Failed to restart ${serviceName}: ${error.message}`, 'error');
            return;
        }
    }

    showToast(`${macro.name} completed successfully`, 'success');
    await loadServiceControl();
}

/**
 * Show service action confirmation modal with dependency warnings
 */
function showServiceConfirmModal({ title, message, services, action, serviceName }) {
    return new Promise((resolve) => {
        // Check for dependency warnings
        let warnings = [];
        const targetServices = serviceName ? [serviceName] : services;

        for (const svc of targetServices) {
            const dep = SERVICE_DEPENDENCIES[svc.toLowerCase()];
            if (dep && (action === 'stop' || action === 'restart')) {
                warnings.push({
                    service: svc,
                    warning: dep.warning,
                    dependents: dep.dependents
                });
            }
        }

        // Create modal
        const modal = document.createElement('div');
        modal.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50';
        modal.id = 'service-confirm-modal';

        modal.innerHTML = `
            <div class="bg-dark-card rounded-lg shadow-xl border border-dark-border max-w-md w-full mx-4">
                <div class="p-6">
                    <div class="flex items-center gap-3 mb-4">
                        <div class="p-2 bg-yellow-500/20 rounded-lg">
                            <i data-lucide="alert-triangle" class="w-6 h-6 text-yellow-400"></i>
                        </div>
                        <h3 class="text-lg font-semibold text-white">${escapeHtml(title)}</h3>
                    </div>

                    <p class="text-gray-300 mb-4">${escapeHtml(message)}</p>

                    ${services ? `
                        <div class="flex flex-wrap gap-2 mb-4">
                            ${services.map((s, i) => `
                                <span class="inline-flex items-center gap-1 px-3 py-1 bg-gray-700 text-gray-200 rounded-full text-sm">
                                    ${i > 0 ? '<i data-lucide="arrow-right" class="w-3 h-3 text-gray-500"></i>' : ''}
                                    ${escapeHtml(s)}
                                </span>
                            `).join('')}
                        </div>
                    ` : ''}

                    ${warnings.length > 0 ? `
                        <div class="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 mb-4">
                            <p class="text-sm font-medium text-yellow-400 mb-2">Warning</p>
                            ${warnings.map(w => `
                                <p class="text-sm text-yellow-300/80 mb-1">
                                    <strong>${escapeHtml(w.service)}:</strong> ${escapeHtml(w.warning)}
                                </p>
                            `).join('')}
                        </div>
                    ` : ''}
                </div>

                <div class="px-6 py-4 bg-gray-800/50 border-t border-dark-border flex justify-end gap-3 rounded-b-lg">
                    <button id="confirm-cancel" class="px-4 py-2 text-sm text-gray-400 hover:text-white transition">
                        Cancel
                    </button>
                    <button id="confirm-proceed" class="px-4 py-2 text-sm bg-yellow-600 hover:bg-yellow-700 text-white rounded-lg transition">
                        Proceed
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(modal);
        if (typeof lucide !== 'undefined') lucide.createIcons();

        // Event handlers
        modal.querySelector('#confirm-cancel').onclick = () => {
            modal.remove();
            resolve(false);
        };

        modal.querySelector('#confirm-proceed').onclick = () => {
            modal.remove();
            resolve(true);
        };

        modal.onclick = (e) => {
            if (e.target === modal) {
                modal.remove();
                resolve(false);
            }
        };

        // Escape key closes
        const escHandler = (e) => {
            if (e.key === 'Escape') {
                modal.remove();
                resolve(false);
                document.removeEventListener('keydown', escHandler);
            }
        };
        document.addEventListener('keydown', escHandler);
    });
}

/**
 * Enhanced stop service with dependency warning
 */
async function stopServiceWithWarning(serviceName) {
    const confirmed = await showServiceConfirmModal({
        title: `Stop ${serviceName}`,
        message: `Are you sure you want to stop ${serviceName}?`,
        serviceName: serviceName,
        action: 'stop'
    });

    if (!confirmed) return;
    await serviceAction(serviceName, 'stop');
}

/**
 * Enhanced restart service with dependency warning
 */
async function restartServiceWithWarning(serviceName) {
    const confirmed = await showServiceConfirmModal({
        title: `Restart ${serviceName}`,
        message: `Are you sure you want to restart ${serviceName}?`,
        serviceName: serviceName,
        action: 'restart'
    });

    if (!confirmed) return;
    await serviceAction(serviceName, 'restart');
}
