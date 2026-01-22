/**
 * Athena Mission Control - Dashboard Page
 *
 * Main landing page showing system health at a glance.
 * Features:
 * - Voice health status with sparkline
 * - Live traffic metrics
 * - Pending actions requiring attention
 * - Alert summary
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[MissionControl] Athena namespace not found');
        return;
    }

    // Page state
    const state = {
        charts: {},
        refreshInterval: null,
        data: null
    };

    // Configuration
    const config = {
        refreshIntervalMs: 60000, // 1 minute
        sparklinePoints: 20
    };

    /**
     * Initialize Mission Control page.
     */
    async function init() {
        console.log('[MissionControl] Initializing');

        const container = document.getElementById('mission-control-content');
        if (!container) {
            console.warn('[MissionControl] Container not found');
            return;
        }

        // Check auth state first
        if (window.AppState && !window.AppState.isAuthenticated()) {
            showLoginPrompt();
            console.log('[MissionControl] Waiting for authentication');
            return;
        }

        // Render layout
        container.innerHTML = renderLayout();

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }

        // Load data
        await refresh();

        // Setup auto-refresh (only if authenticated)
        if (window.AppState && window.AppState.isAuthenticated()) {
            state.refreshInterval = setInterval(refresh, config.refreshIntervalMs);
        }

        console.log('[MissionControl] Initialized');
    }

    /**
     * Render the page layout.
     */
    function renderLayout() {
        return `
            <!-- Header with refresh -->
            <div class="flex items-center justify-between mb-6">
                <div>
                    <p class="text-sm text-gray-400">System overview at a glance</p>
                </div>
                <button onclick="Athena.pages.MissionControl.refresh()"
                        class="flex items-center gap-2 px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-dark-elevated rounded-lg transition-colors">
                    <i data-lucide="refresh-cw" class="w-4 h-4"></i>
                    Refresh
                </button>
            </div>

            <!-- Dashboard Cards Grid -->
            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
                <!-- Voice Health Card -->
                <div id="card-voice-health" class="bg-dark-card border border-dark-border rounded-xl p-5" data-testid="voice-health-card">
                    <div class="flex items-center justify-between mb-4">
                        <div class="flex items-center gap-2">
                            <div class="p-2 bg-green-500/20 rounded-lg">
                                <i data-lucide="activity" class="w-5 h-5 text-green-400"></i>
                            </div>
                            <h3 class="text-sm font-medium text-gray-400">Voice Health</h3>
                        </div>
                        <a href="#service-control" class="text-xs text-blue-400 hover:text-blue-300">Details</a>
                    </div>
                    <div class="flex items-end justify-between">
                        <div>
                            <p id="voice-health-value" class="text-3xl font-bold text-white">--</p>
                            <p id="voice-health-label" class="text-xs text-gray-500 mt-1">Loading...</p>
                        </div>
                        <div class="w-24 h-12">
                            <canvas id="voice-health-sparkline"></canvas>
                        </div>
                    </div>
                </div>

                <!-- Live Traffic Card -->
                <div id="card-live-traffic" class="bg-dark-card border border-dark-border rounded-xl p-5" data-testid="live-traffic-card">
                    <div class="flex items-center justify-between mb-4">
                        <div class="flex items-center gap-2">
                            <div class="p-2 bg-blue-500/20 rounded-lg">
                                <i data-lucide="zap" class="w-5 h-5 text-blue-400"></i>
                            </div>
                            <h3 class="text-sm font-medium text-gray-400">Live Traffic</h3>
                        </div>
                        <a href="#metrics" class="text-xs text-blue-400 hover:text-blue-300">Analytics</a>
                    </div>
                    <div class="flex items-end justify-between">
                        <div>
                            <p id="traffic-value" class="text-3xl font-bold text-white">--</p>
                            <p id="traffic-label" class="text-xs text-gray-500 mt-1">requests/min</p>
                        </div>
                        <div class="w-24 h-12">
                            <canvas id="traffic-sparkline"></canvas>
                        </div>
                    </div>
                </div>

                <!-- Pending Actions Card -->
                <div id="card-pending-actions" class="bg-dark-card border border-dark-border rounded-xl p-5">
                    <div class="flex items-center justify-between mb-4">
                        <div class="flex items-center gap-2">
                            <div class="p-2 bg-yellow-500/20 rounded-lg">
                                <i data-lucide="alert-circle" class="w-5 h-5 text-yellow-400"></i>
                            </div>
                            <h3 class="text-sm font-medium text-gray-400">Pending Actions</h3>
                        </div>
                    </div>
                    <div id="pending-actions-list" class="space-y-2">
                        <p class="text-sm text-gray-500">Loading...</p>
                    </div>
                </div>

                <!-- Alerts Summary Card -->
                <div id="card-alerts" class="bg-dark-card border border-dark-border rounded-xl p-5">
                    <div class="flex items-center justify-between mb-4">
                        <div class="flex items-center gap-2">
                            <div class="p-2 bg-red-500/20 rounded-lg">
                                <i data-lucide="bell" class="w-5 h-5 text-red-400"></i>
                            </div>
                            <h3 class="text-sm font-medium text-gray-400">Alerts</h3>
                        </div>
                        <a href="#alerts" class="text-xs text-blue-400 hover:text-blue-300">View All</a>
                    </div>
                    <div id="alerts-summary">
                        <p class="text-sm text-gray-500">Loading...</p>
                    </div>
                </div>
            </div>

            <!-- Service Status Grid -->
            <div class="bg-dark-card border border-dark-border rounded-xl p-5">
                <div class="flex items-center justify-between mb-4">
                    <h3 class="text-lg font-semibold text-white">Service Status</h3>
                    <a href="#service-control" class="text-sm text-blue-400 hover:text-blue-300">Manage Services</a>
                </div>
                <div id="service-status-grid" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3">
                    <p class="text-sm text-gray-500 col-span-full">Loading services...</p>
                </div>
            </div>
        `;
    }

    /**
     * Refresh dashboard data.
     */
    async function refresh() {
        try {
            // Check auth state first
            if (window.AppState && !window.AppState.isAuthenticated()) {
                showLoginPrompt();
                return;
            }

            // Fetch dashboard data
            state.data = await Athena.api('/api/dashboard');
            updateUI();
        } catch (error) {
            console.error('[MissionControl] Refresh failed:', error);
            // Check if it's an auth error
            if (error.message && error.message.includes('Not authenticated')) {
                showLoginPrompt();
                return;
            }
            if (Athena.components.Toast) {
                Athena.components.Toast.error('Failed to load dashboard data');
            }
        }
    }

    /**
     * Show login prompt for unauthenticated users.
     */
    function showLoginPrompt() {
        const container = document.getElementById('mission-control-content');
        if (container) {
            container.innerHTML = `
                <div class="flex flex-col items-center justify-center py-16">
                    <div class="text-6xl mb-4">🔐</div>
                    <h2 class="text-xl font-semibold text-white mb-2">Authentication Required</h2>
                    <p class="text-gray-400 mb-6">Please login to view Mission Control.</p>
                    <button onclick="window.Auth && Auth.login()"
                        class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                        Login with Authentik
                    </button>
                </div>
            `;
        }
    }

    /**
     * Update UI with current data.
     */
    function updateUI() {
        if (!state.data) return;

        const { voice_health, traffic, pending_actions, alerts, services } = state.data;

        // Update Voice Health
        updateVoiceHealth(voice_health);

        // Update Traffic
        updateTraffic(traffic);

        // Update Pending Actions
        updatePendingActions(pending_actions);

        // Update Alerts
        updateAlerts(alerts);

        // Update Service Status
        updateServiceStatus(services);
    }

    /**
     * Update voice health card.
     */
    function updateVoiceHealth(data) {
        if (!data) return;

        const valueEl = document.getElementById('voice-health-value');
        const labelEl = document.getElementById('voice-health-label');
        const canvas = document.getElementById('voice-health-sparkline');

        if (valueEl) {
            valueEl.textContent = `${data.healthy}/${data.total}`;
            valueEl.className = `text-3xl font-bold ${data.percentage >= 90 ? 'text-green-400' : data.percentage >= 70 ? 'text-yellow-400' : 'text-red-400'}`;
        }

        if (labelEl) {
            labelEl.textContent = `${data.percentage}% healthy`;
        }

        // Update sparkline
        if (canvas && Athena.charts?.createSparkline) {
            if (state.charts.voiceHealth) {
                Athena.charts.updateData(state.charts.voiceHealth, data.history);
            } else {
                state.charts.voiceHealth = Athena.charts.createSparkline(canvas, {
                    data: data.history,
                    color: data.percentage >= 90 ? '#22c55e' : data.percentage >= 70 ? '#f59e0b' : '#ef4444'
                });
            }
        }
    }

    /**
     * Update traffic card.
     */
    function updateTraffic(data) {
        if (!data) return;

        const valueEl = document.getElementById('traffic-value');
        const labelEl = document.getElementById('traffic-label');
        const canvas = document.getElementById('traffic-sparkline');

        if (valueEl) {
            valueEl.textContent = data.requests_per_minute.toFixed(1);
        }

        if (labelEl) {
            labelEl.textContent = `${Athena.utils?.formatNumber?.(data.total_24h) || data.total_24h} events/24h`;
        }

        // Update sparkline
        if (canvas && Athena.charts?.createSparkline) {
            if (state.charts.traffic) {
                Athena.charts.updateData(state.charts.traffic, data.history);
            } else {
                state.charts.traffic = Athena.charts.createSparkline(canvas, {
                    data: data.history,
                    color: '#3b82f6'
                });
            }
        }
    }

    /**
     * Update pending actions card.
     */
    function updatePendingActions(actions) {
        const container = document.getElementById('pending-actions-list');
        if (!container) return;

        if (!actions || actions.length === 0) {
            container.innerHTML = `
                <div class="flex items-center gap-2 text-green-400">
                    <i data-lucide="check-circle" class="w-4 h-4"></i>
                    <span class="text-sm">All clear</span>
                </div>
            `;
        } else {
            container.innerHTML = actions.map(action => {
                const color = action.severity === 'critical' ? 'red' : action.severity === 'warning' ? 'yellow' : 'blue';
                return `
                    <a href="#${action.action}" class="flex items-center justify-between p-2 rounded-lg hover:bg-dark-elevated transition-colors">
                        <div class="flex items-center gap-2">
                            <span class="w-2 h-2 rounded-full bg-${color}-500"></span>
                            <span class="text-sm text-gray-300">${escapeHtml(action.message)}</span>
                        </div>
                        <i data-lucide="chevron-right" class="w-4 h-4 text-gray-500"></i>
                    </a>
                `;
            }).join('');
        }

        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }
    }

    /**
     * Update alerts summary card.
     */
    function updateAlerts(data) {
        const container = document.getElementById('alerts-summary');
        if (!container || !data) return;

        if (data.total === 0) {
            container.innerHTML = `
                <div class="flex items-center gap-2 text-green-400">
                    <i data-lucide="check-circle" class="w-4 h-4"></i>
                    <span class="text-sm">No active alerts</span>
                </div>
            `;
        } else {
            container.innerHTML = `
                <div class="flex items-center gap-4 mb-3">
                    ${data.critical > 0 ? `<span class="text-sm"><span class="text-red-400 font-bold">${data.critical}</span> critical</span>` : ''}
                    ${data.warning > 0 ? `<span class="text-sm"><span class="text-yellow-400 font-bold">${data.warning}</span> warning</span>` : ''}
                    ${data.info > 0 ? `<span class="text-sm"><span class="text-blue-400 font-bold">${data.info}</span> info</span>` : ''}
                </div>
                <div class="space-y-1">
                    ${data.recent.slice(0, 2).map(alert => `
                        <div class="text-xs text-gray-400 truncate">
                            <span class="w-1.5 h-1.5 rounded-full bg-${alert.severity === 'critical' ? 'red' : alert.severity === 'warning' ? 'yellow' : 'blue'}-500 inline-block mr-2"></span>
                            ${escapeHtml(alert.title)}
                        </div>
                    `).join('')}
                </div>
            `;
        }

        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }
    }

    /**
     * Update service status grid.
     */
    function updateServiceStatus(services) {
        const container = document.getElementById('service-status-grid');
        if (!container) return;

        if (!services || services.length === 0) {
            container.innerHTML = `<p class="text-sm text-gray-500 col-span-full">No services found</p>`;
            return;
        }

        container.innerHTML = services.map(service => {
            const isHealthy = service.status === 'healthy';
            const statusColor = isHealthy ? 'green' : service.status === 'degraded' ? 'yellow' : 'red';

            return `
                <div class="flex flex-col items-center p-3 bg-dark-bg rounded-lg">
                    <div class="w-10 h-10 rounded-full bg-${statusColor}-500/20 flex items-center justify-center mb-2">
                        <span class="w-3 h-3 rounded-full bg-${statusColor}-500 ${!isHealthy ? 'animate-pulse' : ''}"></span>
                    </div>
                    <p class="text-xs text-gray-300 text-center truncate w-full">${escapeHtml(service.name)}</p>
                    ${service.latency_ms ? `<p class="text-xs text-gray-500">${service.latency_ms}ms</p>` : ''}
                </div>
            `;
        }).join('');
    }

    /**
     * Destroy page and cleanup.
     */
    function destroy() {
        // Clear refresh interval
        if (state.refreshInterval) {
            clearInterval(state.refreshInterval);
            state.refreshInterval = null;
        }

        // Destroy charts
        Object.values(state.charts).forEach(chart => {
            if (Athena.charts?.destroy) {
                Athena.charts.destroy(chart);
            }
        });
        state.charts = {};
        state.data = null;

        console.log('[MissionControl] Destroyed');
    }

    /**
     * Escape HTML.
     */
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Register page controller
    Athena.pages.MissionControl = {
        init,
        destroy,
        refresh
    };

    // Also export as global for compatibility
    window.MissionControl = Athena.pages.MissionControl;

})(window.Athena);
