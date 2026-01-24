/**
 * Athena Activity Sidebar - Recent Actions Panel
 *
 * Displays recent audit log entries with undo capability.
 * Fetches from /api/audit/recent and provides real-time updates.
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[AthenaActivity] Athena namespace not found');
        return;
    }

    // State
    let isVisible = false;
    let sidebarElement = null;
    let activities = [];
    let refreshInterval = null;

    // Configuration
    const config = {
        maxItems: 10,
        refreshIntervalMs: 30000, // 30 seconds
        position: 'right'
    };

    /**
     * Initialize the activity sidebar.
     */
    function init() {
        console.log('[AthenaActivity] Initializing');

        // Create sidebar element if not exists
        if (!sidebarElement) {
            sidebarElement = document.getElementById('activity-sidebar');
            if (!sidebarElement) {
                createSidebar();
            }
        }

        // Load initial data
        refresh();

        // Setup auto-refresh
        if (refreshInterval) clearInterval(refreshInterval);
        refreshInterval = setInterval(refresh, config.refreshIntervalMs);

        // Listen for audit events from WebSocket if available
        if (window.WebSocketClient) {
            window.WebSocketClient.on('audit_update', handleAuditUpdate);
        }
    }

    /**
     * Create the sidebar element.
     */
    function createSidebar() {
        sidebarElement = document.createElement('aside');
        sidebarElement.id = 'activity-sidebar';
        sidebarElement.className = `
            fixed top-16 right-0 bottom-0 w-80
            bg-dark-card border-l border-dark-border
            transform transition-transform duration-300 ease-out
            translate-x-full
            z-40 flex flex-col
        `;
        sidebarElement.setAttribute('role', 'complementary');
        sidebarElement.setAttribute('aria-label', 'Recent activity');

        sidebarElement.innerHTML = `
            <!-- Header -->
            <div class="flex items-center justify-between px-4 py-3 border-b border-dark-border">
                <div class="flex items-center gap-2">
                    <i data-lucide="activity" class="w-4 h-4 text-blue-400"></i>
                    <h3 class="text-sm font-semibold text-white">Recent Activity</h3>
                </div>
                <button id="activity-close" class="p-1 text-gray-400 hover:text-white rounded transition-colors"
                        aria-label="Close activity sidebar">
                    <i data-lucide="x" class="w-4 h-4"></i>
                </button>
            </div>

            <!-- Activity list -->
            <div id="activity-list" class="flex-1 overflow-y-auto">
                <div class="p-4 text-center text-gray-500 text-sm">Loading...</div>
            </div>

            <!-- Footer -->
            <div class="px-4 py-3 border-t border-dark-border">
                <button id="activity-refresh" class="w-full flex items-center justify-center gap-2 px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-dark-elevated rounded-lg transition-colors">
                    <i data-lucide="refresh-cw" class="w-4 h-4"></i>
                    Refresh
                </button>
            </div>
        `;

        document.body.appendChild(sidebarElement);

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [sidebarElement] });
        }

        // Bind events
        document.getElementById('activity-close').addEventListener('click', hide);
        document.getElementById('activity-refresh').addEventListener('click', refresh);
    }

    /**
     * Show the activity sidebar.
     */
    function show() {
        if (!sidebarElement) init();

        sidebarElement.classList.remove('translate-x-full');
        sidebarElement.classList.add('translate-x-0');
        isVisible = true;

        // Refresh on show
        refresh();

        if (Athena.emit) Athena.emit('activity:show');
    }

    /**
     * Hide the activity sidebar.
     */
    function hide() {
        if (!sidebarElement) return;

        sidebarElement.classList.remove('translate-x-0');
        sidebarElement.classList.add('translate-x-full');
        isVisible = false;

        if (Athena.emit) Athena.emit('activity:hide');
    }

    /**
     * Toggle the activity sidebar.
     */
    function toggle() {
        if (isVisible) {
            hide();
        } else {
            show();
        }
    }

    /**
     * Refresh activity data from API.
     */
    async function refresh() {
        const listEl = document.getElementById('activity-list');
        if (!listEl) return;

        try {
            const response = await Athena.api('/api/audit/recent?limit=' + config.maxItems);
            // API returns {entries: [...]} - extract the array
            activities = (response && response.entries) || [];
            render();
        } catch (error) {
            console.error('[AthenaActivity] Refresh failed:', error);
            listEl.innerHTML = `
                <div class="p-4 text-center text-red-400 text-sm">
                    Failed to load activity
                </div>
            `;
        }
    }

    /**
     * Render the activity list.
     */
    function render() {
        const listEl = document.getElementById('activity-list');
        if (!listEl) return;

        if (activities.length === 0) {
            listEl.innerHTML = `
                <div class="p-4 text-center text-gray-500 text-sm">
                    No recent activity
                </div>
            `;
            return;
        }

        listEl.innerHTML = activities.map(activity => renderActivity(activity)).join('');

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [listEl] });
        }

        // Bind undo buttons
        listEl.querySelectorAll('.activity-undo').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const auditId = e.currentTarget.dataset.auditId;
                handleUndo(auditId);
            });
        });
    }

    /**
     * Render a single activity item.
     */
    function renderActivity(activity) {
        const { id, action, resource_type, username, timestamp, reversible, undo_description } = activity;

        const icon = getActionIcon(action);
        const color = getActionColor(action);
        const timeAgo = Athena.utils?.formatDate?.(timestamp) || formatTimeAgo(timestamp);
        const description = formatDescription(activity);

        return `
            <div class="activity-item px-4 py-3 border-b border-dark-border hover:bg-dark-elevated/50 transition-colors">
                <div class="flex items-start gap-3">
                    <div class="flex-shrink-0 mt-0.5">
                        <div class="w-8 h-8 rounded-full bg-${color}-500/20 flex items-center justify-center">
                            <i data-lucide="${icon}" class="w-4 h-4 text-${color}-400"></i>
                        </div>
                    </div>
                    <div class="flex-1 min-w-0">
                        <p class="text-sm text-white">${description}</p>
                        <div class="flex items-center gap-2 mt-1">
                            <span class="text-xs text-gray-500">${username || 'System'}</span>
                            <span class="text-xs text-gray-600">&bull;</span>
                            <span class="text-xs text-gray-500">${timeAgo}</span>
                        </div>
                        ${reversible ? `
                            <button class="activity-undo mt-2 text-xs text-blue-400 hover:text-blue-300 transition-colors"
                                    data-audit-id="${id}"
                                    title="${undo_description || 'Undo this action'}">
                                <i data-lucide="undo-2" class="w-3 h-3 inline mr-1"></i>
                                Undo
                            </button>
                        ` : ''}
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Handle undo action.
     */
    async function handleUndo(auditId) {
        try {
            if (Athena.components.Toast) {
                Athena.components.Toast.info('Undoing action...');
            }

            const response = await Athena.api(`/api/audit/${auditId}/undo`, { method: 'POST' });

            if (response.success) {
                if (Athena.components.Toast) {
                    Athena.components.Toast.success(response.message || 'Action undone successfully');
                }

                // Refresh activity list
                await refresh();

                // Emit event for other components to react
                if (Athena.emit) Athena.emit('activity:undo', { auditId, response });
            }
        } catch (error) {
            console.error('[AthenaActivity] Undo failed:', error);
            if (Athena.components.Toast) {
                Athena.components.Toast.error('Failed to undo: ' + (error.message || 'Unknown error'));
            }
        }
    }

    /**
     * Handle real-time audit update from WebSocket.
     */
    function handleAuditUpdate(data) {
        // Add to top of list
        activities.unshift(data);

        // Trim to max items
        if (activities.length > config.maxItems) {
            activities = activities.slice(0, config.maxItems);
        }

        // Re-render
        render();
    }

    /**
     * Get icon for action type.
     */
    function getActionIcon(action) {
        const icons = {
            create: 'plus-circle',
            update: 'edit',
            delete: 'trash-2',
            toggle: 'toggle-right',
            start: 'play',
            stop: 'square',
            restart: 'refresh-cw',
            undo: 'undo-2',
            login: 'log-in',
            logout: 'log-out'
        };
        return icons[action] || 'activity';
    }

    /**
     * Get color for action type.
     */
    function getActionColor(action) {
        const colors = {
            create: 'green',
            update: 'blue',
            delete: 'red',
            toggle: 'purple',
            start: 'green',
            stop: 'red',
            restart: 'yellow',
            undo: 'orange',
            login: 'green',
            logout: 'gray'
        };
        return colors[action] || 'gray';
    }

    /**
     * Format activity description.
     */
    function formatDescription(activity) {
        const { action, resource_type, new_value } = activity;

        // Handle specific action types
        if (action === 'toggle' && resource_type === 'feature') {
            const enabled = new_value?.enabled;
            const featureName = new_value?.name || resource_type;
            return `${enabled ? 'Enabled' : 'Disabled'} <span class="text-gray-400">${escapeHtml(featureName)}</span>`;
        }

        if (action === 'restart') {
            return `Restarted <span class="text-gray-400">${escapeHtml(resource_type)}</span>`;
        }

        // Default format
        const actionText = action.charAt(0).toUpperCase() + action.slice(1);
        return `${actionText} <span class="text-gray-400">${escapeHtml(resource_type)}</span>`;
    }

    /**
     * Format timestamp to relative time.
     */
    function formatTimeAgo(timestamp) {
        if (!timestamp) return '';
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

    /**
     * Destroy the activity sidebar.
     */
    function destroy() {
        if (refreshInterval) {
            clearInterval(refreshInterval);
            refreshInterval = null;
        }
        if (sidebarElement) {
            sidebarElement.remove();
            sidebarElement = null;
        }
        isVisible = false;
    }

    // Export to Athena namespace
    Athena.components.Activity = {
        init,
        show,
        hide,
        toggle,
        refresh,
        destroy,
        isVisible: () => isVisible,
        config
    };

    // Also export as global for compatibility
    window.AthenaActivity = Athena.components.Activity;

})(window.Athena);
