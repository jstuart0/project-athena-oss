/**
 * Athena Integrations - External Service Provider Management
 *
 * Card grid for managing external service integrations:
 * - Voice: LiveKit
 * - Communication: SMS/Twilio
 * - RAG: Weather, Sports, Dining, etc.
 * - Scheduling: Calendar APIs
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[Integrations] Athena namespace not found');
        return;
    }

    // Available integration providers
    const providers = [
        // Voice
        { id: 'livekit', name: 'LiveKit', icon: 'video', category: 'Voice', description: 'Real-time voice/video streaming' },

        // Communication
        { id: 'sms', name: 'SMS (Twilio)', icon: 'message-square', category: 'Communication', description: 'Send SMS notifications' },

        // Scheduling
        { id: 'calendar', name: 'Google Calendar', icon: 'calendar', category: 'Scheduling', description: 'Calendar events and reminders' },

        // Location
        { id: 'directions', name: 'Google Maps', icon: 'map', category: 'Location', description: 'Directions and travel time' },

        // RAG Services
        { id: 'weather', name: 'Weather', icon: 'cloud-sun', category: 'RAG Services', description: 'Current and forecast weather' },
        { id: 'sports', name: 'Sports', icon: 'trophy', category: 'RAG Services', description: 'Live scores and schedules' },
        { id: 'dining', name: 'Dining', icon: 'utensils', category: 'RAG Services', description: 'Restaurant search and reservations' },
        { id: 'news', name: 'News', icon: 'newspaper', category: 'RAG Services', description: 'News headlines and articles' },
        { id: 'stocks', name: 'Stocks', icon: 'trending-up', category: 'RAG Services', description: 'Stock prices and market data' },
        { id: 'flights', name: 'Flights', icon: 'plane', category: 'RAG Services', description: 'Flight status and tracking' }
    ];

    // Page state
    const state = {
        statuses: {},
        refreshInterval: null
    };

    /**
     * Initialize Integrations page.
     */
    async function init() {
        console.log('[Integrations] Initializing');

        const container = document.getElementById('integrations-content');
        if (!container) {
            console.warn('[Integrations] Container not found');
            return;
        }

        // Show loading state
        container.innerHTML = '<div class="text-center py-8 text-gray-400">Loading integrations...</div>';

        // Load statuses
        await loadStatuses();

        // Render
        render(container);

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }

        console.log('[Integrations] Initialized');
    }

    /**
     * Destroy page and cleanup.
     */
    function destroy() {
        if (state.refreshInterval) {
            clearInterval(state.refreshInterval);
            state.refreshInterval = null;
        }
        state.statuses = {};
        console.log('[Integrations] Destroyed');
    }

    /**
     * Load status for all providers.
     */
    async function loadStatuses() {
        // Load statuses in parallel
        await Promise.all(providers.map(async (p) => {
            try {
                // Try multiple endpoints for status
                let status = { status: 'unknown' };

                // First try the dedicated integrations endpoint
                try {
                    status = await Athena.api(`/api/integrations/${p.id}/status`);
                } catch {
                    // Fall back to external API keys check
                    try {
                        const keyCheck = await Athena.api(`/api/external-api-keys/public/${p.id}/status`);
                        status = keyCheck.configured ? { status: 'connected' } : { status: 'not_configured' };
                    } catch {
                        // Check if it's a RAG service that's running
                        if (p.category === 'RAG Services') {
                            try {
                                const ragStatus = await Athena.api('/api/service-registry/services');
                                const service = ragStatus?.find(s => s.name.toLowerCase().includes(p.id));
                                if (service?.enabled) {
                                    status = { status: 'connected' };
                                }
                            } catch {}
                        }
                    }
                }

                state.statuses[p.id] = status;
            } catch {
                state.statuses[p.id] = { status: 'unknown', error: 'Failed to load' };
            }
        }));
    }

    /**
     * Render the integrations page.
     */
    function render(container) {
        // Group providers by category
        const categories = {};
        providers.forEach(p => {
            if (!categories[p.category]) {
                categories[p.category] = [];
            }
            categories[p.category].push({ ...p, status: state.statuses[p.id] || {} });
        });

        // Count connected integrations
        const connectedCount = Object.values(state.statuses).filter(s =>
            s.status === 'connected' || s.status === 'healthy'
        ).length;

        container.innerHTML = `
            <!-- Header Stats -->
            <div class="flex items-center justify-between mb-6">
                <div class="flex items-center gap-4">
                    <div class="px-4 py-2 bg-dark-card border border-dark-border rounded-lg">
                        <span class="text-gray-400 text-sm">Connected:</span>
                        <span class="text-white font-bold ml-2">${connectedCount} / ${providers.length}</span>
                    </div>
                </div>
                <button onclick="Athena.pages.Integrations.refresh()"
                        class="flex items-center gap-2 px-4 py-2 text-gray-400 hover:text-white hover:bg-dark-elevated rounded-lg transition-colors">
                    <i data-lucide="refresh-cw" class="w-4 h-4"></i>
                    Refresh Status
                </button>
            </div>

            <!-- Integration Categories -->
            ${Object.entries(categories).map(([category, categoryProviders]) => `
                <div class="mb-8">
                    <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                        ${getCategoryIcon(category)}
                        ${category}
                        <span class="text-sm font-normal text-gray-500">
                            (${categoryProviders.filter(p => p.status.status === 'connected' || p.status.status === 'healthy').length}/${categoryProviders.length} connected)
                        </span>
                    </h3>
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        ${categoryProviders.map(p => renderCard(p)).join('')}
                    </div>
                </div>
            `).join('')}
        `;
    }

    /**
     * Get icon for category.
     */
    function getCategoryIcon(category) {
        const icons = {
            'Voice': '<i data-lucide="mic" class="w-5 h-5 text-blue-400"></i>',
            'Communication': '<i data-lucide="message-circle" class="w-5 h-5 text-green-400"></i>',
            'Scheduling': '<i data-lucide="calendar" class="w-5 h-5 text-purple-400"></i>',
            'Location': '<i data-lucide="map-pin" class="w-5 h-5 text-orange-400"></i>',
            'RAG Services': '<i data-lucide="database" class="w-5 h-5 text-yellow-400"></i>'
        };
        return icons[category] || '<i data-lucide="puzzle" class="w-5 h-5 text-gray-400"></i>';
    }

    /**
     * Render a single integration card.
     */
    function renderCard(provider) {
        const status = provider.status || {};
        const isConnected = status.status === 'connected' || status.status === 'healthy';
        const isError = status.status === 'error';
        const statusColor = isConnected ? 'green' : isError ? 'red' : 'gray';
        const statusText = isConnected ? 'Connected' : isError ? 'Error' : status.status || 'Not configured';

        return `
            <div class="bg-dark-card border border-dark-border rounded-xl p-5 hover:border-dark-border-hover transition-all">
                <div class="flex items-start justify-between mb-4">
                    <div class="flex items-center gap-3">
                        <div class="p-2 bg-${statusColor}-500/20 rounded-lg">
                            <i data-lucide="${provider.icon}" class="w-5 h-5 text-${statusColor}-400"></i>
                        </div>
                        <div>
                            <h4 class="text-white font-medium">${escapeHtml(provider.name)}</h4>
                            <span class="text-xs ${isConnected ? 'text-green-400' : 'text-gray-500'}">
                                ${escapeHtml(statusText)}
                            </span>
                        </div>
                    </div>

                    <!-- Action dropdown -->
                    <div class="relative">
                        <button onclick="Athena.pages.Integrations.toggleDropdown('${provider.id}')"
                                class="p-2 hover:bg-dark-elevated rounded-lg transition-colors">
                            <i data-lucide="more-vertical" class="w-4 h-4 text-gray-400"></i>
                        </button>
                        <div id="dropdown-${provider.id}"
                             class="hidden absolute right-0 top-full mt-1 w-40 bg-dark-elevated border border-dark-border rounded-lg shadow-lg z-20">
                            <button onclick="Athena.pages.Integrations.configure('${provider.id}')"
                                    class="w-full text-left px-4 py-2 text-sm text-gray-300 hover:bg-dark-bg rounded-t-lg transition-colors">
                                <i data-lucide="settings" class="w-4 h-4 inline mr-2"></i>
                                Configure
                            </button>
                            <button onclick="Athena.pages.Integrations.test('${provider.id}')"
                                    class="w-full text-left px-4 py-2 text-sm text-gray-300 hover:bg-dark-bg transition-colors">
                                <i data-lucide="play" class="w-4 h-4 inline mr-2"></i>
                                Test Connection
                            </button>
                            ${isConnected ? `
                                <button onclick="Athena.pages.Integrations.disconnect('${provider.id}')"
                                        class="w-full text-left px-4 py-2 text-sm text-red-400 hover:bg-dark-bg rounded-b-lg transition-colors">
                                    <i data-lucide="unplug" class="w-4 h-4 inline mr-2"></i>
                                    Disconnect
                                </button>
                            ` : ''}
                        </div>
                    </div>
                </div>

                <!-- Description -->
                <p class="text-sm text-gray-500 mb-4">${escapeHtml(provider.description)}</p>

                <!-- Stats/Quota if available -->
                ${status.quota ? renderQuotaBar(status.quota) : ''}

                <!-- Last sync time -->
                ${status.last_sync ? `
                    <p class="text-xs text-gray-600 mt-3">
                        Last sync: ${formatTimeAgo(status.last_sync)}
                    </p>
                ` : ''}

                <!-- Error message -->
                ${status.error && !isConnected ? `
                    <div class="mt-3 p-2 bg-red-500/10 border border-red-500/20 rounded text-xs text-red-400">
                        ${escapeHtml(status.error)}
                    </div>
                ` : ''}
            </div>
        `;
    }

    /**
     * Render a quota progress bar.
     */
    function renderQuotaBar(quota) {
        const percentage = quota.limit > 0 ? (quota.used / quota.limit * 100) : 0;
        const color = percentage > 90 ? 'red' : percentage > 70 ? 'yellow' : 'blue';

        return `
            <div class="space-y-2 text-sm">
                <div class="flex justify-between">
                    <span class="text-gray-500">API Quota</span>
                    <span class="text-gray-300">${quota.used.toLocaleString()} / ${quota.limit.toLocaleString()}</span>
                </div>
                <div class="w-full bg-dark-bg rounded-full h-1.5">
                    <div class="bg-${color}-500 h-1.5 rounded-full transition-all"
                         style="width: ${Math.min(percentage, 100)}%"></div>
                </div>
            </div>
        `;
    }

    /**
     * Toggle dropdown menu.
     */
    function toggleDropdown(id) {
        const dropdown = document.getElementById(`dropdown-${id}`);

        // Close all other dropdowns
        document.querySelectorAll('[id^="dropdown-"]').forEach(d => {
            if (d.id !== `dropdown-${id}`) {
                d.classList.add('hidden');
            }
        });

        // Toggle this dropdown
        dropdown?.classList.toggle('hidden');

        // Close when clicking outside
        const closeHandler = (e) => {
            if (!e.target.closest(`#dropdown-${id}`) && !e.target.closest(`[onclick*="toggleDropdown('${id}')"]`)) {
                dropdown?.classList.add('hidden');
                document.removeEventListener('click', closeHandler);
            }
        };

        if (!dropdown?.classList.contains('hidden')) {
            setTimeout(() => document.addEventListener('click', closeHandler), 0);
        }
    }

    /**
     * Open configuration drawer for an integration.
     */
    function configure(id) {
        const provider = providers.find(p => p.id === id);
        if (!provider) return;

        // Close dropdown
        document.getElementById(`dropdown-${id}`)?.classList.add('hidden');

        if (!Athena.components.Drawer) {
            alert('Drawer component not available');
            return;
        }

        Athena.components.Drawer.open({
            title: `Configure ${provider.name}`,
            width: '500px',
            content: `
                <div class="space-y-6">
                    <div class="p-4 bg-dark-bg rounded-lg">
                        <div class="flex items-center gap-3 mb-3">
                            <i data-lucide="${provider.icon}" class="w-6 h-6 text-blue-400"></i>
                            <div>
                                <p class="text-white font-medium">${escapeHtml(provider.name)}</p>
                                <p class="text-sm text-gray-500">${escapeHtml(provider.category)}</p>
                            </div>
                        </div>
                        <p class="text-sm text-gray-400">${escapeHtml(provider.description)}</p>
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">API Key</label>
                        <input type="password" id="integration-api-key"
                               placeholder="Enter API key"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                        <p class="text-xs text-gray-500 mt-1">Your API key will be encrypted before storage</p>
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Endpoint URL (optional)</label>
                        <input type="url" id="integration-endpoint"
                               placeholder="https://api.example.com"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                    </div>

                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="integration-enabled" checked
                               class="w-4 h-4 rounded border-dark-border bg-dark-bg text-blue-600 focus:ring-blue-500">
                        <label for="integration-enabled" class="text-sm text-gray-400">Enable this integration</label>
                    </div>
                </div>
            `,
            saveText: 'Save Configuration',
            onSave: async () => {
                const apiKey = document.getElementById('integration-api-key').value.trim();
                const endpoint = document.getElementById('integration-endpoint').value.trim();
                const enabled = document.getElementById('integration-enabled').checked;

                if (!apiKey) {
                    if (Athena.components.Toast) {
                        Athena.components.Toast.warning('API key is required');
                    }
                    throw new Error('Validation failed');
                }

                try {
                    await Athena.api(`/api/integrations/${id}/configure`, {
                        method: 'POST',
                        body: JSON.stringify({
                            api_key: apiKey,
                            endpoint_url: endpoint || null,
                            enabled
                        })
                    });

                    if (Athena.components.Toast) {
                        Athena.components.Toast.success(`${provider.name} configured successfully`);
                    }

                    // Refresh the page
                    await refresh();
                } catch (error) {
                    console.error('[Integrations] Configure failed:', error);
                    throw error;
                }
            }
        });

        // Initialize icons in drawer
        setTimeout(() => {
            if (window.lucide) lucide.createIcons();
        }, 100);
    }

    /**
     * Test connection for an integration.
     */
    async function test(id) {
        const provider = providers.find(p => p.id === id);
        if (!provider) return;

        // Close dropdown
        document.getElementById(`dropdown-${id}`)?.classList.add('hidden');

        if (Athena.components.Toast) {
            Athena.components.Toast.info(`Testing ${provider.name} connection...`);
        }

        try {
            await Athena.api(`/api/integrations/${id}/test`, { method: 'POST' });

            if (Athena.components.Toast) {
                Athena.components.Toast.success(`${provider.name} connection successful!`);
            }

            // Update status
            state.statuses[id] = { status: 'connected' };
            const container = document.getElementById('integrations-content');
            if (container) {
                render(container);
                if (window.lucide) lucide.createIcons({ nodes: [container] });
            }
        } catch (error) {
            console.error('[Integrations] Test failed:', error);
            if (Athena.components.Toast) {
                Athena.components.Toast.error(`${provider.name} connection failed`);
            }

            state.statuses[id] = { status: 'error', error: error.message || 'Connection failed' };
            const container = document.getElementById('integrations-content');
            if (container) {
                render(container);
                if (window.lucide) lucide.createIcons({ nodes: [container] });
            }
        }
    }

    /**
     * Disconnect an integration.
     */
    async function disconnect(id) {
        const provider = providers.find(p => p.id === id);
        if (!provider) return;

        // Close dropdown
        document.getElementById(`dropdown-${id}`)?.classList.add('hidden');

        if (!confirm(`Are you sure you want to disconnect ${provider.name}?`)) return;

        try {
            await Athena.api(`/api/integrations/${id}/disconnect`, { method: 'POST' });

            if (Athena.components.Toast) {
                Athena.components.Toast.success(`${provider.name} disconnected`);
            }

            // Refresh
            await refresh();
        } catch (error) {
            console.error('[Integrations] Disconnect failed:', error);
            if (Athena.components.Toast) {
                Athena.components.Toast.error(`Failed to disconnect ${provider.name}`);
            }
        }
    }

    /**
     * Refresh all integration statuses.
     */
    async function refresh() {
        const container = document.getElementById('integrations-content');
        if (!container) return;

        container.innerHTML = '<div class="text-center py-8 text-gray-400">Refreshing statuses...</div>';

        await loadStatuses();
        render(container);

        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }

        if (Athena.components.Toast) {
            Athena.components.Toast.info('Integration statuses refreshed');
        }
    }

    /**
     * Format timestamp to relative time.
     */
    function formatTimeAgo(timestamp) {
        if (!timestamp) return 'Unknown';
        const date = new Date(timestamp);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffMins < 1440) return `${Math.floor(diffMins / 60)}h ago`;
        return `${Math.floor(diffMins / 1440)}d ago`;
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
    Athena.pages.Integrations = {
        init,
        destroy,
        refresh,
        toggleDropdown,
        configure,
        test,
        disconnect
    };

    // Also export as global for compatibility
    window.Integrations = Athena.pages.Integrations;

})(window.Athena);
