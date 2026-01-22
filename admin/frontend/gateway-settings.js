/**
 * Gateway Settings Management UI
 *
 * Manages gateway service configuration via Admin API:
 * - Service URLs (orchestrator, Ollama fallback)
 * - Intent classification settings (model, temperature, max tokens, timeout)
 * - Session management (timeout, max age, cleanup interval)
 * - Rate limiting (enabled, requests per minute)
 * - Circuit breaker (enabled, failure threshold, recovery timeout)
 *
 * All settings take effect within 60 seconds (gateway config cache TTL).
 */

let gatewayConfig = null;
let gatewayStatus = null;

/**
 * Get authentication headers
 */
function getGatewayAuthHeaders() {
    const token = localStorage.getItem('auth_token');
    return {
        'Content-Type': 'application/json',
        'Authorization': token ? `Bearer ${token}` : ''
    };
}

/**
 * Safe wrapper to call app.js showToast function
 */
function gatewayShowToast(message, type = 'info') {
    if (typeof window.showToast === 'function') {
        window.showToast(message, type);
    } else {
        console.log(`[${type}] ${message}`);
    }
}

/**
 * Load gateway configuration from backend
 */
async function loadGatewayConfig() {
    try {
        const response = await fetch('/api/gateway-config', {
            headers: getGatewayAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load gateway config: ${response.statusText}`);
        }

        gatewayConfig = await response.json();
        console.log('Gateway config loaded:', gatewayConfig);

        renderGatewaySettings();
    } catch (error) {
        console.error('Failed to load gateway config:', error);
        gatewayShowToast('Failed to load gateway settings', 'error');
        showGatewayError(error.message);
    }
}

/**
 * Save gateway configuration
 */
async function saveGatewayConfig() {
    try {
        const updates = collectGatewayFormData();

        const response = await fetch('/api/gateway-config', {
            method: 'PATCH',
            headers: getGatewayAuthHeaders(),
            body: JSON.stringify(updates)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save gateway config');
        }

        gatewayConfig = await response.json();
        gatewayShowToast('Gateway settings saved. Changes will take effect within 60 seconds.', 'success');
        renderGatewaySettings();

    } catch (error) {
        console.error('Failed to save gateway config:', error);
        gatewayShowToast(error.message, 'error');
    }
}

/**
 * Reset gateway configuration to defaults
 */
async function resetGatewayConfig() {
    if (!confirm('Are you sure you want to reset all gateway settings to defaults?')) {
        return;
    }

    try {
        const response = await fetch('/api/gateway-config/reset', {
            method: 'POST',
            headers: getGatewayAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to reset gateway config');
        }

        const result = await response.json();
        gatewayConfig = result.config;
        gatewayShowToast('Gateway settings reset to defaults', 'success');
        renderGatewaySettings();

    } catch (error) {
        console.error('Failed to reset gateway config:', error);
        gatewayShowToast(error.message, 'error');
    }
}

/**
 * Collect form data from the settings form
 */
function collectGatewayFormData() {
    return {
        // Service URLs
        orchestrator_url: document.getElementById('gw-orchestrator-url')?.value || gatewayConfig.orchestrator_url,
        ollama_fallback_url: document.getElementById('gw-ollama-url')?.value || gatewayConfig.ollama_fallback_url,

        // Intent Classification
        intent_model: document.getElementById('gw-intent-model')?.value || gatewayConfig.intent_model,
        intent_temperature: parseFloat(document.getElementById('gw-intent-temperature')?.value) || gatewayConfig.intent_temperature,
        intent_max_tokens: parseInt(document.getElementById('gw-intent-max-tokens')?.value) || gatewayConfig.intent_max_tokens,
        intent_timeout_seconds: parseInt(document.getElementById('gw-intent-timeout')?.value) || gatewayConfig.intent_timeout_seconds,

        // Timeouts
        orchestrator_timeout_seconds: parseInt(document.getElementById('gw-orchestrator-timeout')?.value) || gatewayConfig.orchestrator_timeout_seconds,

        // Session Management
        session_timeout_seconds: parseInt(document.getElementById('gw-session-timeout')?.value) || gatewayConfig.session_timeout_seconds,
        session_max_age_seconds: parseInt(document.getElementById('gw-session-max-age')?.value) || gatewayConfig.session_max_age_seconds,
        session_cleanup_interval_seconds: parseInt(document.getElementById('gw-session-cleanup')?.value) || gatewayConfig.session_cleanup_interval_seconds,

        // Cache
        cache_ttl_seconds: parseInt(document.getElementById('gw-cache-ttl')?.value) || gatewayConfig.cache_ttl_seconds,

        // Rate Limiting
        rate_limit_enabled: document.getElementById('gw-rate-limit-enabled')?.checked ?? gatewayConfig.rate_limit_enabled,
        rate_limit_requests_per_minute: parseInt(document.getElementById('gw-rate-limit-rpm')?.value) || gatewayConfig.rate_limit_requests_per_minute,

        // Circuit Breaker
        circuit_breaker_enabled: document.getElementById('gw-circuit-breaker-enabled')?.checked ?? gatewayConfig.circuit_breaker_enabled,
        circuit_breaker_failure_threshold: parseInt(document.getElementById('gw-circuit-failure-threshold')?.value) || gatewayConfig.circuit_breaker_failure_threshold,
        circuit_breaker_recovery_timeout_seconds: parseInt(document.getElementById('gw-circuit-recovery-timeout')?.value) || gatewayConfig.circuit_breaker_recovery_timeout_seconds,
    };
}

/**
 * Render gateway settings form
 */
function renderGatewaySettings() {
    const container = document.getElementById('gateway-settings-container');
    if (!container) return;

    if (!gatewayConfig) {
        container.innerHTML = '<div class="loading">Loading gateway settings...</div>';
        return;
    }

    container.innerHTML = `
        <div class="space-y-6">
            <!-- Service URLs Section -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span>Service URLs</span>
                </h3>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Orchestrator URL</label>
                        <input type="text" id="gw-orchestrator-url" value="${gatewayConfig.orchestrator_url}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Primary orchestrator service endpoint</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Ollama Fallback URL</label>
                        <input type="text" id="gw-ollama-url" value="${gatewayConfig.ollama_fallback_url}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Fallback when orchestrator unavailable</p>
                    </div>
                </div>
            </div>

            <!-- Intent Classification Section -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span>Intent Classification</span>
                </h3>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Model</label>
                        <input type="text" id="gw-intent-model" value="${gatewayConfig.intent_model}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">LLM model for intent detection</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Temperature</label>
                        <input type="number" id="gw-intent-temperature" value="${gatewayConfig.intent_temperature}"
                            min="0" max="2" step="0.1"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">0.0 - 2.0 (lower = more deterministic)</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Max Tokens</label>
                        <input type="number" id="gw-intent-max-tokens" value="${gatewayConfig.intent_max_tokens}"
                            min="1" max="100"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">1 - 100 tokens for classification</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Timeout (seconds)</label>
                        <input type="number" id="gw-intent-timeout" value="${gatewayConfig.intent_timeout_seconds}"
                            min="1" max="60"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">1 - 60 seconds</p>
                    </div>
                </div>
            </div>

            <!-- Timeouts & Sessions Section -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span>Timeouts & Sessions</span>
                </h3>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Orchestrator Timeout (s)</label>
                        <input type="number" id="gw-orchestrator-timeout" value="${gatewayConfig.orchestrator_timeout_seconds}"
                            min="5" max="300"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">5 - 300 seconds</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Session Timeout (s)</label>
                        <input type="number" id="gw-session-timeout" value="${gatewayConfig.session_timeout_seconds}"
                            min="60" max="3600"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">60 - 3600 seconds (inactivity)</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Max Session Age (s)</label>
                        <input type="number" id="gw-session-max-age" value="${gatewayConfig.session_max_age_seconds}"
                            min="3600" max="604800"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">1 hour - 7 days max lifetime</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Cleanup Interval (s)</label>
                        <input type="number" id="gw-session-cleanup" value="${gatewayConfig.session_cleanup_interval_seconds}"
                            min="10" max="600"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">10 - 600 seconds</p>
                    </div>
                </div>
            </div>

            <!-- Cache Section -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span>Cache Settings</span>
                </h3>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Cache TTL (seconds)</label>
                        <input type="number" id="gw-cache-ttl" value="${gatewayConfig.cache_ttl_seconds}"
                            min="5" max="300"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">5 - 300 seconds (config cache lifetime)</p>
                    </div>
                </div>
            </div>

            <!-- Rate Limiting Section -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span>Rate Limiting</span>
                    <span class="text-xs px-2 py-1 rounded ${gatewayConfig.rate_limit_enabled ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">
                        ${gatewayConfig.rate_limit_enabled ? 'Enabled' : 'Disabled'}
                    </span>
                </h3>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div class="flex items-center gap-4">
                        <label class="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" id="gw-rate-limit-enabled" class="sr-only peer"
                                ${gatewayConfig.rate_limit_enabled ? 'checked' : ''}>
                            <div class="w-11 h-6 bg-gray-700 rounded-full peer peer-checked:bg-blue-600 peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all"></div>
                        </label>
                        <span class="text-sm text-gray-400">Enable Rate Limiting</span>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Requests per Minute</label>
                        <input type="number" id="gw-rate-limit-rpm" value="${gatewayConfig.rate_limit_requests_per_minute}"
                            min="1" max="1000"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">1 - 1000 requests per minute</p>
                    </div>
                </div>
            </div>

            <!-- Circuit Breaker Section -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                    <span>Circuit Breaker</span>
                    <span class="text-xs px-2 py-1 rounded ${gatewayConfig.circuit_breaker_enabled ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">
                        ${gatewayConfig.circuit_breaker_enabled ? 'Enabled' : 'Disabled'}
                    </span>
                </h3>
                <p class="text-sm text-gray-500 mb-4">Prevents cascade failures when the orchestrator is unavailable. Falls back to Ollama when circuit is open.</p>
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div class="flex items-center gap-4">
                        <label class="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" id="gw-circuit-breaker-enabled" class="sr-only peer"
                                ${gatewayConfig.circuit_breaker_enabled ? 'checked' : ''}>
                            <div class="w-11 h-6 bg-gray-700 rounded-full peer peer-checked:bg-blue-600 peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all"></div>
                        </label>
                        <span class="text-sm text-gray-400">Enable Circuit Breaker</span>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Failure Threshold</label>
                        <input type="number" id="gw-circuit-failure-threshold" value="${gatewayConfig.circuit_breaker_failure_threshold}"
                            min="1" max="50"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Failures before opening circuit</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Recovery Timeout (s)</label>
                        <input type="number" id="gw-circuit-recovery-timeout" value="${gatewayConfig.circuit_breaker_recovery_timeout_seconds}"
                            min="5" max="300"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Seconds before testing recovery</p>
                    </div>
                </div>
            </div>

            <!-- Actions -->
            <div class="flex justify-between items-center">
                <button onclick="resetGatewayConfig()"
                    class="px-4 py-2 bg-red-600/20 hover:bg-red-600/30 text-red-400 rounded-lg text-sm font-medium transition-colors border border-red-600/30">
                    Reset to Defaults
                </button>
                <div class="flex gap-3">
                    <button onclick="loadGatewayConfig()"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm font-medium transition-colors">
                        Cancel
                    </button>
                    <button onclick="saveGatewayConfig()"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors">
                        Save Settings
                    </button>
                </div>
            </div>

            <!-- Last Updated Info -->
            ${gatewayConfig.updated_at ? `
                <div class="text-xs text-gray-500 text-right">
                    Last updated: ${new Date(gatewayConfig.updated_at).toLocaleString()}
                </div>
            ` : ''}
        </div>
    `;
}

/**
 * Show error state
 */
function showGatewayError(message) {
    const container = document.getElementById('gateway-settings-container');
    if (container) {
        container.innerHTML = `
            <div class="bg-red-900/20 border border-red-700/50 rounded-lg p-4">
                <p class="text-red-200">Error: ${message}</p>
                <button onclick="loadGatewayConfig()" class="mt-2 px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm">
                    Retry
                </button>
            </div>
        `;
    }
}

/**
 * Initialize gateway settings page
 */
function initGatewaySettingsPage() {
    console.log('Initializing gateway settings page');
    loadGatewayConfig();
}

// Export for external use
if (typeof window !== 'undefined') {
    window.loadGatewayConfig = loadGatewayConfig;
    window.saveGatewayConfig = saveGatewayConfig;
    window.resetGatewayConfig = resetGatewayConfig;
    window.initGatewaySettingsPage = initGatewaySettingsPage;
}
