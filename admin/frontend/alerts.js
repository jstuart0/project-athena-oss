/**
 * Project Athena - Alerts Management
 * Handles system alerts for stuck sensors, service issues, etc.
 */

// ============================================================================
// Alerts Bell Dropdown (Header)
// ============================================================================

let alertsDropdownOpen = false;
let alertsRefreshInterval = null;

function toggleAlertsDropdown() {
    const dropdown = document.getElementById('alerts-dropdown');
    alertsDropdownOpen = !alertsDropdownOpen;

    if (alertsDropdownOpen) {
        dropdown.classList.remove('hidden');
        loadAlertsDropdown();
    } else {
        dropdown.classList.add('hidden');
    }
}

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
    const container = document.getElementById('alerts-container');
    if (container && !container.contains(e.target) && alertsDropdownOpen) {
        toggleAlertsDropdown();
    }
});

async function loadAlertsDropdown() {
    const list = document.getElementById('alerts-list');

    try {
        // Fetch active alerts
        const response = await fetch(`${API_BASE}/api/alerts?status=active&limit=10`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            // Try public endpoint if authenticated one fails
            const publicResponse = await fetch(`${API_BASE}/api/alerts/public/active-by-type?alert_type=stuck_sensor`);
            if (publicResponse.ok) {
                const data = await publicResponse.json();
                renderDropdownAlerts(data.alerts || []);
                return;
            }
            throw new Error('Failed to fetch alerts');
        }

        const alerts = await response.json();
        renderDropdownAlerts(alerts);

    } catch (error) {
        console.error('Error loading alerts:', error);
        list.innerHTML = '<div class="p-4 text-center text-gray-500 text-sm">Unable to load alerts</div>';
    }
}

function renderDropdownAlerts(alerts) {
    const list = document.getElementById('alerts-list');
    const badge = document.getElementById('alerts-badge');
    const sidebarBadge = document.getElementById('sidebar-alerts-badge');

    // Update badge
    const activeCount = alerts.length;
    if (activeCount > 0) {
        badge.textContent = activeCount > 9 ? '9+' : activeCount;
        badge.classList.remove('hidden');
        if (sidebarBadge) {
            sidebarBadge.textContent = activeCount;
            sidebarBadge.classList.remove('hidden');
        }
    } else {
        badge.classList.add('hidden');
        if (sidebarBadge) {
            sidebarBadge.classList.add('hidden');
        }
    }

    if (alerts.length === 0) {
        list.innerHTML = '<div class="p-4 text-center text-gray-500 text-sm">No active alerts</div>';
        return;
    }

    list.innerHTML = alerts.map(alert => `
        <div class="p-3 border-b border-dark-border hover:bg-gray-800/50 cursor-pointer" onclick="showAlertDetail(${alert.id})">
            <div class="flex items-start gap-2">
                <span class="text-lg">${getSeverityIcon(alert.severity)}</span>
                <div class="flex-1 min-w-0">
                    <div class="text-sm font-medium text-white truncate">${escapeHtml(alert.title)}</div>
                    <div class="text-xs text-gray-400 truncate">${escapeHtml(alert.message)}</div>
                    <div class="text-xs text-gray-500 mt-1">${formatTimeAgo(alert.created_at)}</div>
                </div>
            </div>
        </div>
    `).join('');
}

function getSeverityIcon(severity) {
    switch (severity) {
        case 'critical': return '🔴';
        case 'error': return '🟠';
        case 'warning': return '🟡';
        case 'info': return '🔵';
        default: return '⚪';
    }
}

function getSeverityColor(severity) {
    switch (severity) {
        case 'critical': return 'text-red-500 bg-red-500/10 border-red-500/30';
        case 'error': return 'text-orange-500 bg-orange-500/10 border-orange-500/30';
        case 'warning': return 'text-yellow-500 bg-yellow-500/10 border-yellow-500/30';
        case 'info': return 'text-blue-500 bg-blue-500/10 border-blue-500/30';
        default: return 'text-gray-500 bg-gray-500/10 border-gray-500/30';
    }
}

function getStatusColor(status) {
    switch (status) {
        case 'active': return 'text-red-400 bg-red-500/10';
        case 'acknowledged': return 'text-yellow-400 bg-yellow-500/10';
        case 'resolved': return 'text-green-400 bg-green-500/10';
        case 'dismissed': return 'text-gray-400 bg-gray-500/10';
        default: return 'text-gray-400 bg-gray-500/10';
    }
}

function formatTimeAgo(dateString) {
    if (!dateString) return 'Unknown';
    const date = new Date(dateString);
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================================================
// Alerts Page
// ============================================================================

async function loadAlertsPage() {
    await Promise.all([
        loadAlertsStats(),
        loadAlertsList()
    ]);

    // Start auto-refresh using RefreshManager (prevents interval accumulation)
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('alerts-refresh', loadAlertsDropdown, 30000);
    } else {
        // Fallback to manual interval management
        if (alertsRefreshInterval) {
            clearInterval(alertsRefreshInterval);
        }
        alertsRefreshInterval = setInterval(loadAlertsDropdown, 30000);
    }
}

/**
 * Cleanup alerts page (called when navigating away)
 */
function destroyAlertsPage() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('alerts-refresh');
    } else if (alertsRefreshInterval) {
        clearInterval(alertsRefreshInterval);
        alertsRefreshInterval = null;
    }
}

async function loadAlertsStats() {
    try {
        const response = await fetch(`${API_BASE}/api/alerts/stats`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            // Fallback to public endpoint to count
            const publicResponse = await fetch(`${API_BASE}/api/alerts/public/active-by-type?alert_type=stuck_sensor`);
            if (publicResponse.ok) {
                const data = await publicResponse.json();
                document.getElementById('stat-active').textContent = data.count || 0;
                document.getElementById('stat-acknowledged').textContent = 0;
                document.getElementById('stat-resolved').textContent = 0;
                document.getElementById('stat-total').textContent = data.count || 0;
            }
            return;
        }

        const stats = await response.json();
        document.getElementById('stat-active').textContent = stats.active || 0;
        document.getElementById('stat-acknowledged').textContent = stats.acknowledged || 0;
        document.getElementById('stat-resolved').textContent = stats.resolved || 0;
        document.getElementById('stat-total').textContent = stats.total || 0;

    } catch (error) {
        console.error('Error loading alerts stats:', error);
    }
}

async function loadAlertsList() {
    const container = document.getElementById('alerts-page-container');
    const statusFilter = document.getElementById('alerts-status-filter')?.value || '';
    const typeFilter = document.getElementById('alerts-type-filter')?.value || '';

    try {
        let url = `${API_BASE}/api/alerts?limit=100`;
        if (statusFilter) url += `&status=${statusFilter}`;
        if (typeFilter) url += `&alert_type=${typeFilter}`;

        const response = await fetch(url, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            // Try public endpoint
            const publicResponse = await fetch(`${API_BASE}/api/alerts/public/active-by-type?alert_type=${typeFilter || 'stuck_sensor'}`);
            if (publicResponse.ok) {
                const data = await publicResponse.json();
                renderAlertsList(data.alerts || []);
                return;
            }
            throw new Error('Failed to load alerts');
        }

        const alerts = await response.json();
        renderAlertsList(alerts);

    } catch (error) {
        console.error('Error loading alerts list:', error);
        container.innerHTML = '<div class="p-4 text-center text-gray-500">Unable to load alerts. Please try again.</div>';
    }
}

function renderAlertsList(alerts) {
    const container = document.getElementById('alerts-page-container');

    if (alerts.length === 0) {
        container.innerHTML = '<div class="p-8 text-center text-gray-500 bg-dark-card border border-dark-border rounded-lg">No alerts found matching your filters.</div>';
        return;
    }

    container.innerHTML = alerts.map(alert => `
        <div class="bg-dark-card border ${getSeverityColor(alert.severity).split(' ')[2]} rounded-lg p-4">
            <div class="flex items-start justify-between">
                <div class="flex items-start gap-3 flex-1">
                    <span class="text-2xl">${getSeverityIcon(alert.severity)}</span>
                    <div class="flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <h3 class="text-lg font-medium text-white">${escapeHtml(alert.title)}</h3>
                            <span class="px-2 py-0.5 text-xs rounded ${getStatusColor(alert.status)}">${alert.status}</span>
                            <span class="px-2 py-0.5 text-xs rounded bg-gray-700 text-gray-300">${alert.alert_type}</span>
                        </div>
                        <p class="text-sm text-gray-300 mb-2">${escapeHtml(alert.message)}</p>
                        <div class="flex items-center gap-4 text-xs text-gray-500">
                            <span>Created: ${formatTimeAgo(alert.created_at)}</span>
                            ${alert.entity_id ? `<span>Entity: ${alert.entity_id}</span>` : ''}
                            ${alert.acknowledged_at ? `<span>Acknowledged: ${formatTimeAgo(alert.acknowledged_at)}</span>` : ''}
                            ${alert.resolved_at ? `<span>Resolved: ${formatTimeAgo(alert.resolved_at)}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex gap-2 ml-4">
                    ${alert.status === 'active' ? `
                        <button onclick="acknowledgeAlert(${alert.id})"
                            class="px-3 py-1.5 bg-yellow-600 hover:bg-yellow-700 text-white rounded text-sm transition-colors">
                            Acknowledge
                        </button>
                    ` : ''}
                    ${alert.status !== 'resolved' ? `
                        <button onclick="resolveAlert(${alert.id})"
                            class="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white rounded text-sm transition-colors">
                            Resolve
                        </button>
                    ` : ''}
                    <button onclick="deleteAlert(${alert.id})"
                        class="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors">
                        Delete
                    </button>
                </div>
            </div>
        </div>
    `).join('');
}

// Use global getAuthHeaders from utils.js which includes Content-Type: application/json

async function acknowledgeAlert(alertId) {
    try {
        const response = await fetch(`${API_BASE}/api/alerts/${alertId}`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                ...getAuthHeaders()
            },
            body: JSON.stringify({ status: 'acknowledged' })
        });

        if (response.ok) {
            loadAlertsPage();
            loadAlertsDropdown();
        } else {
            alert('Failed to acknowledge alert');
        }
    } catch (error) {
        console.error('Error acknowledging alert:', error);
        alert('Failed to acknowledge alert');
    }
}

async function resolveAlert(alertId) {
    const notes = prompt('Resolution notes (optional):');

    try {
        const response = await fetch(`${API_BASE}/api/alerts/${alertId}`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                ...getAuthHeaders()
            },
            body: JSON.stringify({
                status: 'resolved',
                resolution_notes: notes || null
            })
        });

        if (response.ok) {
            loadAlertsPage();
            loadAlertsDropdown();
        } else {
            alert('Failed to resolve alert');
        }
    } catch (error) {
        console.error('Error resolving alert:', error);
        alert('Failed to resolve alert');
    }
}

async function deleteAlert(alertId) {
    if (!confirm('Are you sure you want to delete this alert?')) return;

    try {
        const response = await fetch(`${API_BASE}/api/alerts/${alertId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            loadAlertsPage();
            loadAlertsDropdown();
        } else {
            alert('Failed to delete alert');
        }
    } catch (error) {
        console.error('Error deleting alert:', error);
        alert('Failed to delete alert');
    }
}

async function acknowledgeAllAlerts() {
    if (!confirm('Acknowledge all active alerts?')) return;

    const statusFilter = document.getElementById('alerts-status-filter')?.value || '';
    const typeFilter = document.getElementById('alerts-type-filter')?.value || '';

    try {
        let url = `${API_BASE}/api/alerts/acknowledge-all`;
        const params = new URLSearchParams();
        if (typeFilter) params.append('alert_type', typeFilter);
        if (params.toString()) url += `?${params}`;

        const response = await fetch(url, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            const result = await response.json();
            loadAlertsPage();
            loadAlertsDropdown();
            if (result.acknowledged_count) {
                console.log(`Acknowledged ${result.acknowledged_count} alerts`);
            }
        } else {
            alert('Failed to acknowledge alerts');
        }
    } catch (error) {
        console.error('Error acknowledging all alerts:', error);
        alert('Failed to acknowledge alerts');
    }
}

function showAlertDetail(alertId) {
    // Close dropdown and navigate to alerts tab
    if (alertsDropdownOpen) {
        toggleAlertsDropdown();
    }
    showTab('alerts');
    // Could scroll to specific alert if needed
}

// ============================================================================
// Initialize on Load
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // Load initial alerts count for badge
    setTimeout(loadAlertsDropdown, 1000);
});
