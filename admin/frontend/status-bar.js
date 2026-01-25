/**
 * Real-time Status Bar for Athena Admin
 * Polls service health and updates indicators
 *
 * Part of Phase 1: Design Foundation
 *
 * ARCHITECTURE NOTE: Uses Phase 0 modules (ApiClient, AppState, RefreshManager)
 * to ensure consistent auth handling and interval management.
 */

(function() {
    // Dependency check
    if (!window.ApiClient || !window.AppState || !window.RefreshManager) {
        console.error('[StatusBar] Missing dependencies. Ensure state.js, api-client.js, and refresh-manager.js load first.');
        return;
    }

    const StatusBar = {
        services: [
            // Health endpoints - these should be accessible publicly
            { id: 'gateway', name: 'Gateway', endpoint: '/health', public: true },
            { id: 'ollama', name: 'Ollama', endpoint: '/api/services/ollama/health', public: false },
            { id: 'orchestrator', name: 'Orchestrator', endpoint: '/api/services/orchestrator/health', public: false },
            { id: 'redis', name: 'Redis', endpoint: '/api/services/redis/health', public: false },
            { id: 'qdrant', name: 'Qdrant', endpoint: '/api/services/qdrant/health', public: false },
            { id: 'mlx', name: 'MLX', endpoint: '/api/services/mlx/health', public: false, optional: true }
        ],
        pollInterval: 30000, // 30 seconds
        intervalKey: 'status-bar-poll',
        _started: false,

        /**
         * Check health of a single service
         * Uses ApiClient for consistent error handling
         */
        async checkService(service) {
            const element = document.getElementById(`status-${service.id}`);
            const dot = element?.querySelector('.status-dot');
            if (!dot) return;

            try {
                // Use ApiClient with skipAuth for public endpoints
                const data = await ApiClient.get(service.endpoint, {
                    skipAuth: service.public
                });

                // Determine status from response
                let status = 'healthy';
                let tooltip = service.name;

                if (data) {
                    if (data.status === 'not_configured') {
                        // Optional service not configured (e.g., MLX without MLX_URL)
                        status = 'unknown';
                        tooltip = `${service.name}: Not configured`;
                    } else if (data.status === 'offline' || data.status === 'error' || data.healthy === false) {
                        status = 'error';
                        tooltip = `${service.name}: Offline`;
                        if (data.error) tooltip += ` (${data.error})`;
                    } else if (data.status === 'degraded' || data.status === 'warning') {
                        status = 'warning';
                        tooltip = `${service.name}: Degraded`;
                    } else if (data.status === 'online') {
                        status = 'healthy';
                        tooltip = `${service.name}: Online`;
                        if (data.response_time_ms) tooltip += ` (${data.response_time_ms}ms)`;
                    }
                }

                this._updateDot(dot, status, tooltip);

            } catch (error) {
                // Determine status from error
                if (error.status === 401) {
                    // Auth required but user not logged in
                    this._updateDot(dot, 'unknown', `${service.name}: Login required`);
                } else if (error.status >= 400 && error.status < 500) {
                    this._updateDot(dot, 'warning', `${service.name}: Degraded (${error.status})`);
                } else {
                    this._updateDot(dot, 'error', `${service.name}: Unreachable`);
                }
            }
        },

        /**
         * Update status dot appearance
         */
        _updateDot(dot, status, tooltip) {
            // Remove all status classes
            dot.classList.remove('healthy', 'warning', 'error', 'unknown');

            // Add new status class
            dot.classList.add(status);

            // Update tooltip
            dot.title = tooltip;

            // Update parent element's aria-label
            const parent = dot.closest('.status-item');
            if (parent) {
                parent.setAttribute('aria-label', tooltip);
            }
        },

        /**
         * Check all services
         */
        async checkAll() {
            // Only poll if page is visible (browser tab is active)
            if (document.hidden) return;

            await Promise.all(this.services.map(s => this.checkService(s)));
        },

        /**
         * Update alerts badge count
         */
        async updateAlertsBadge() {
            if (!AppState.isAuthenticated()) return;

            try {
                const data = await ApiClient.get('/api/alerts/active/count');
                const badge = document.getElementById('alerts-badge');
                if (badge) {
                    const count = data?.count || 0;
                    badge.textContent = count > 99 ? '99+' : count;
                    badge.dataset.count = count;
                    badge.style.display = count > 0 ? 'flex' : 'none';
                }
            } catch (error) {
                // Silently fail - alerts badge is not critical
                console.debug('[StatusBar] Failed to update alerts badge:', error.message);
            }
        },

        /**
         * Start polling using RefreshManager (auto-cleanup on tab switch)
         */
        start() {
            if (this._started) return;

            // Initial check
            this.checkAll();
            this.updateAlertsBadge();

            // Use RefreshManager for managed intervals
            RefreshManager.createInterval(
                this.intervalKey,
                () => {
                    this.checkAll();
                    this.updateAlertsBadge();
                },
                this.pollInterval
            );

            // Subscribe to auth changes - update status when login/logout occurs
            AppState.subscribe((type, data) => {
                if (type === 'auth') {
                    // Re-check services when auth state changes
                    this.checkAll();
                    this.updateAlertsBadge();
                }
            });

            // Listen for visibility changes
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden) {
                    this.checkAll();
                }
            });

            this._started = true;
        },

        /**
         * Stop polling
         */
        stop() {
            RefreshManager.clearInterval(this.intervalKey);
            this._started = false;
        },

        /**
         * Add a custom service to monitor
         */
        addService(id, name, endpoint, isPublic = false) {
            this.services.push({ id, name, endpoint, public: isPublic });
        },

        /**
         * Manually refresh status
         */
        refresh() {
            this.checkAll();
            this.updateAlertsBadge();
        }
    };

    // Expose on window
    window.StatusBar = StatusBar;
})();
