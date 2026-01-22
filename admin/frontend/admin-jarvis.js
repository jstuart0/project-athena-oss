/**
 * Admin Jarvis - Real-time Pipeline Monitoring
 *
 * WebSocket client for receiving and displaying pipeline events from the orchestrator.
 * Provides real-time visibility into Athena's query processing pipeline.
 */

// =============================================================================
// WebSocket State
// =============================================================================

let adminJarvisWs = null;
let ajIsConnected = false;
let ajReconnectAttempts = 0;
let ajReconnectTimeout = null;
const AJ_MAX_RECONNECT_ATTEMPTS = 5;
const AJ_RECONNECT_DELAY = 3000;

// Stats tracking
let ajStats = {
    eventsCount: 0,
    activeSessions: new Map(), // session_id -> session info
    toolCalls: 0,
    latencies: [], // Array of latency values for averaging
    maxLatencies: 100 // Keep last 100 latencies
};

// Event log
let ajEventLog = [];
const AJ_MAX_EVENTS = 500;

// =============================================================================
// WebSocket Connection Management
// =============================================================================

/**
 * Toggle WebSocket connection on/off.
 */
function toggleWebSocketConnection() {
    if (ajIsConnected) {
        disconnectWebSocket();
    } else {
        connectWebSocket();
    }
}

/**
 * Connect to the Admin Jarvis WebSocket endpoint.
 */
function connectWebSocket() {
    if (adminJarvisWs && adminJarvisWs.readyState === WebSocket.OPEN) {
        console.log('[AdminJarvis] Already connected');
        return;
    }

    // Build WebSocket URL - use same host as page, switch to WSS for HTTPS
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;

    // Get auth token if available (stored as 'auth_token' by app.js)
    const token = localStorage.getItem('auth_token') || '';

    // WebSocket endpoint on admin backend
    const wsUrl = `${protocol}//${host}/ws/admin-jarvis${token ? `?token=${token}` : ''}`;

    console.log('[AdminJarvis] Connecting to:', wsUrl);
    updateWsStatus('connecting');

    try {
        adminJarvisWs = new WebSocket(wsUrl);

        adminJarvisWs.onopen = handleWsOpen;
        adminJarvisWs.onclose = handleWsClose;
        adminJarvisWs.onerror = handleWsError;
        adminJarvisWs.onmessage = handleWsMessage;
    } catch (error) {
        console.error('[AdminJarvis] WebSocket creation error:', error);
        updateWsStatus('error');
    }
}

/**
 * Disconnect from WebSocket.
 */
function disconnectWebSocket() {
    if (adminJarvisWs) {
        adminJarvisWs.close(1000, 'User disconnected');
        adminJarvisWs = null;
    }
    ajIsConnected = false;
    ajReconnectAttempts = 0;
    if (ajReconnectTimeout) {
        clearTimeout(ajReconnectTimeout);
        ajReconnectTimeout = null;
    }
    updateWsStatus('disconnected');
}

/**
 * Handle WebSocket open event.
 */
function handleWsOpen() {
    console.log('[AdminJarvis] WebSocket connected');
    ajIsConnected = true;
    ajReconnectAttempts = 0;
    updateWsStatus('connected');

    // Send ping to verify connection
    sendWsMessage({ type: 'ping' });

    // Start heartbeat
    startHeartbeat();

    addEventToLog({
        event_type: 'system',
        timestamp: Date.now() / 1000,
        data: { message: 'Connected to Admin Jarvis WebSocket' }
    });
}

/**
 * Handle WebSocket close event.
 */
function handleWsClose(event) {
    console.log('[AdminJarvis] WebSocket closed:', event.code, event.reason);
    ajIsConnected = false;
    stopHeartbeat();

    if (event.code !== 1000) {
        // Abnormal close - attempt reconnect
        updateWsStatus('disconnected');
        scheduleReconnect();
    } else {
        updateWsStatus('disconnected');
    }

    addEventToLog({
        event_type: 'system',
        timestamp: Date.now() / 1000,
        data: { message: `Disconnected: ${event.reason || 'Connection closed'}` }
    });
}

/**
 * Handle WebSocket error event.
 */
function handleWsError(error) {
    console.error('[AdminJarvis] WebSocket error:', error);
    updateWsStatus('error');

    addEventToLog({
        event_type: 'error',
        timestamp: Date.now() / 1000,
        data: { message: 'WebSocket connection error' }
    });
}

/**
 * Handle incoming WebSocket message.
 */
function handleWsMessage(event) {
    try {
        const message = JSON.parse(event.data);

        // Handle different message types
        switch (message.event_type) {
            case 'pong':
                // Heartbeat response - ignore
                break;
            case 'heartbeat':
                // Server heartbeat - respond with ping
                sendWsMessage({ type: 'ping' });
                break;
            case 'subscribed':
            case 'unsubscribed':
                // Subscription confirmations
                console.log('[AdminJarvis]', message.event_type, message.data);
                break;
            case 'error':
                console.error('[AdminJarvis] Server error:', message.data);
                addEventToLog(message);
                break;
            default:
                // Pipeline event - process and display
                processPipelineEvent(message);
        }
    } catch (error) {
        console.error('[AdminJarvis] Failed to parse message:', error, event.data);
    }
}

/**
 * Send message to WebSocket server.
 */
function sendWsMessage(message) {
    if (adminJarvisWs && adminJarvisWs.readyState === WebSocket.OPEN) {
        adminJarvisWs.send(JSON.stringify(message));
    }
}

/**
 * Schedule reconnection attempt.
 */
function scheduleReconnect() {
    if (ajReconnectAttempts >= AJ_MAX_RECONNECT_ATTEMPTS) {
        console.log('[AdminJarvis] Max reconnect attempts reached');
        updateWsStatus('failed');
        return;
    }

    ajReconnectAttempts++;
    const delay = AJ_RECONNECT_DELAY * ajReconnectAttempts;

    console.log(`[AdminJarvis] Reconnecting in ${delay}ms (attempt ${ajReconnectAttempts})`);

    ajReconnectTimeout = setTimeout(() => {
        connectWebSocket();
    }, delay);
}

// =============================================================================
// Heartbeat
// =============================================================================

let ajHeartbeatInterval = null;

function startHeartbeat() {
    stopHeartbeat();
    ajHeartbeatInterval = setInterval(() => {
        if (ajIsConnected) {
            sendWsMessage({ type: 'ping' });
        }
    }, 30000); // Every 30 seconds
}

function stopHeartbeat() {
    if (ajHeartbeatInterval) {
        clearInterval(ajHeartbeatInterval);
        ajHeartbeatInterval = null;
    }
}

// =============================================================================
// Event Processing
// =============================================================================

/**
 * Process a pipeline event and update UI.
 */
function processPipelineEvent(event) {
    // Update stats
    ajStats.eventsCount++;

    // Handle specific event types
    switch (event.event_type) {
        case 'session_start':
            handleSessionStart(event);
            break;
        case 'session_end':
            handleSessionEnd(event);
            break;
        case 'tool_selected':
        case 'tool_result':
            ajStats.toolCalls++;
            break;
        case 'response_generated':
            if (event.data && event.data.latency_ms) {
                recordLatency(event.data.latency_ms);
            }
            break;
    }

    // Add to log
    addEventToLog(event);

    // Update display
    updateStatsDisplay();
}

/**
 * Handle session start event.
 */
function handleSessionStart(event) {
    const sessionId = event.session_id;
    const data = event.data || {};

    ajStats.activeSessions.set(sessionId, {
        startTime: event.timestamp * 1000,
        query: data.query || '',
        interface: event.interface || 'unknown',
        intent: null,
        status: 'processing'
    });

    updateActiveSessionsDisplay();
}

/**
 * Handle session end event.
 */
function handleSessionEnd(event) {
    const sessionId = event.session_id;
    const session = ajStats.activeSessions.get(sessionId);

    if (session) {
        // Calculate duration
        const duration = Date.now() - session.startTime;
        recordLatency(duration);

        // Remove from active sessions after a short delay
        setTimeout(() => {
            ajStats.activeSessions.delete(sessionId);
            updateActiveSessionsDisplay();
        }, 2000);
    }

    // Mark as completed briefly
    if (session) {
        session.status = 'completed';
        updateActiveSessionsDisplay();
    }
}

/**
 * Record a latency value.
 */
function recordLatency(latencyMs) {
    ajStats.latencies.push(latencyMs);
    if (ajStats.latencies.length > ajStats.maxLatencies) {
        ajStats.latencies.shift();
    }
}

// =============================================================================
// UI Updates
// =============================================================================

/**
 * Update WebSocket status indicator.
 */
function updateWsStatus(status) {
    const statusEl = document.getElementById('ws-status');
    const toggleBtn = document.getElementById('ws-toggle-btn');

    if (!statusEl || !toggleBtn) return;

    switch (status) {
        case 'connected':
            statusEl.textContent = '🟢 Connected';
            statusEl.className = 'px-3 py-1 rounded-full text-sm font-medium bg-green-900 text-green-400';
            toggleBtn.innerHTML = '<i data-lucide="plug-zap" class="w-4 h-4 inline-block mr-1"></i> Disconnect';
            toggleBtn.className = 'px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium transition-colors';
            break;
        case 'connecting':
            statusEl.textContent = '🟡 Connecting...';
            statusEl.className = 'px-3 py-1 rounded-full text-sm font-medium bg-yellow-900 text-yellow-400';
            break;
        case 'disconnected':
            statusEl.textContent = '⚪ Disconnected';
            statusEl.className = 'px-3 py-1 rounded-full text-sm font-medium bg-gray-700 text-gray-400';
            toggleBtn.innerHTML = '<i data-lucide="plug" class="w-4 h-4 inline-block mr-1"></i> Connect';
            toggleBtn.className = 'px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium transition-colors';
            break;
        case 'error':
        case 'failed':
            statusEl.textContent = '🔴 Error';
            statusEl.className = 'px-3 py-1 rounded-full text-sm font-medium bg-red-900 text-red-400';
            toggleBtn.innerHTML = '<i data-lucide="plug" class="w-4 h-4 inline-block mr-1"></i> Reconnect';
            toggleBtn.className = 'px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium transition-colors';
            break;
    }
}

/**
 * Update stats display.
 */
function updateStatsDisplay() {
    const eventsEl = document.getElementById('aj-events-count');
    const sessionsEl = document.getElementById('aj-sessions-count');
    const toolsEl = document.getElementById('aj-tools-count');
    const latencyEl = document.getElementById('aj-avg-latency');

    if (eventsEl) eventsEl.textContent = ajStats.eventsCount.toLocaleString();
    if (sessionsEl) sessionsEl.textContent = ajStats.activeSessions.size.toString();
    if (toolsEl) toolsEl.textContent = ajStats.toolCalls.toLocaleString();

    if (latencyEl) {
        if (ajStats.latencies.length > 0) {
            const avg = ajStats.latencies.reduce((a, b) => a + b, 0) / ajStats.latencies.length;
            latencyEl.textContent = Math.round(avg).toLocaleString();
        } else {
            latencyEl.textContent = '-';
        }
    }
}

/**
 * Update active sessions display.
 */
function updateActiveSessionsDisplay() {
    const container = document.getElementById('aj-active-sessions');
    if (!container) return;

    if (ajStats.activeSessions.size === 0) {
        container.innerHTML = '<p class="text-gray-500 text-sm">No active sessions.</p>';
        return;
    }

    let html = '';
    ajStats.activeSessions.forEach((session, sessionId) => {
        const statusColor = session.status === 'completed' ? 'text-green-400' : 'text-blue-400';
        const statusIcon = session.status === 'completed' ? '✓' : '⟳';

        html += `
            <div class="flex items-center gap-4 p-3 bg-dark-bg rounded-lg">
                <span class="${statusColor} font-mono">${statusIcon}</span>
                <div class="flex-1">
                    <div class="text-white text-sm font-medium truncate">${escapeHtml(session.query || 'Processing...')}</div>
                    <div class="text-gray-500 text-xs">
                        Session: ${sessionId.substring(0, 8)}... |
                        Interface: ${session.interface}
                        ${session.intent ? ` | Intent: ${session.intent}` : ''}
                    </div>
                </div>
                <div class="text-gray-400 text-xs">
                    ${formatDuration(Date.now() - session.startTime)}
                </div>
            </div>
        `;
    });

    container.innerHTML = html;
}

/**
 * Add event to the log display.
 */
function addEventToLog(event) {
    // Add to internal log
    ajEventLog.unshift(event);
    if (ajEventLog.length > AJ_MAX_EVENTS) {
        ajEventLog.pop();
    }

    // Update display
    renderEventLog();
}

/**
 * Render the event log.
 */
function renderEventLog() {
    const container = document.getElementById('aj-event-log');
    if (!container) return;

    const filterValue = document.getElementById('aj-event-filter')?.value || 'all';

    // Filter events
    const filteredEvents = ajEventLog.filter(event => {
        if (filterValue === 'all') return true;
        return event.event_type === filterValue;
    });

    if (filteredEvents.length === 0) {
        container.innerHTML = '<p class="text-gray-500">No matching events.</p>';
        return;
    }

    let html = '';
    filteredEvents.slice(0, 100).forEach(event => {
        html += renderEventItem(event);
    });

    container.innerHTML = html;

    // Auto-scroll if enabled
    const autoScroll = document.getElementById('aj-auto-scroll');
    if (autoScroll && autoScroll.checked) {
        container.scrollTop = 0; // Scroll to top (newest events)
    }
}

/**
 * Render a single event item.
 */
function renderEventItem(event) {
    const timestamp = new Date(event.timestamp * 1000).toLocaleTimeString();
    const eventType = event.event_type || 'unknown';
    const sessionId = event.session_id ? event.session_id.substring(0, 8) : '-';

    // Color based on event type
    const colors = {
        'session_start': 'text-green-400',
        'session_end': 'text-gray-400',
        'intent_detected': 'text-blue-400',
        'tool_selected': 'text-purple-400',
        'tool_result': 'text-cyan-400',
        'response_generated': 'text-yellow-400',
        'error': 'text-red-400',
        'system': 'text-gray-500'
    };

    const color = colors[eventType] || 'text-white';

    // Format data
    let dataStr = '';
    if (event.data) {
        if (typeof event.data === 'object') {
            // Show key parts of data
            if (event.data.message) {
                dataStr = event.data.message;
            } else if (event.data.query) {
                dataStr = `Query: "${event.data.query.substring(0, 50)}..."`;
            } else if (event.data.tool_name) {
                dataStr = `Tool: ${event.data.tool_name}`;
            } else if (event.data.intent) {
                dataStr = `Intent: ${event.data.intent}`;
            } else {
                dataStr = JSON.stringify(event.data).substring(0, 100);
            }
        } else {
            dataStr = String(event.data).substring(0, 100);
        }
    }

    return `
        <div class="flex gap-2 py-1 border-b border-dark-border hover:bg-dark-card/50">
            <span class="text-gray-500 w-20 flex-shrink-0">${timestamp}</span>
            <span class="text-gray-600 w-16 flex-shrink-0 font-mono text-xs">${sessionId}</span>
            <span class="${color} w-32 flex-shrink-0">${eventType}</span>
            <span class="text-gray-300 truncate flex-1">${escapeHtml(dataStr)}</span>
        </div>
    `;
}

/**
 * Clear the event log.
 */
function clearEventLog() {
    ajEventLog = [];
    ajStats.eventsCount = 0;
    ajStats.toolCalls = 0;
    ajStats.latencies = [];
    updateStatsDisplay();
    renderEventLog();
}

/**
 * Filter events based on selection.
 */
function filterEvents() {
    renderEventLog();
}

// =============================================================================
// Tool Proposals - Auto-Approve Settings
// =============================================================================

let ajAutoApproveEnabled = false;

/**
 * Load auto-approve settings.
 */
async function loadAutoApproveSettings() {
    try {
        const response = await fetch('/api/settings/tool-proposals');
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const settings = await response.json();
        ajAutoApproveEnabled = settings.auto_approve_enabled;
        updateAutoApproveToggle();
    } catch (error) {
        console.error('[AdminJarvis] Failed to load auto-approve settings:', error);
    }
}

/**
 * Toggle auto-approve mode.
 */
async function toggleAutoApprove() {
    const newValue = !ajAutoApproveEnabled;

    try {
        const token = localStorage.getItem('auth_token');
        const response = await fetch('/api/settings/tool-proposals', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': token ? `Bearer ${token}` : ''
            },
            body: JSON.stringify({ auto_approve_enabled: newValue })
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        ajAutoApproveEnabled = newValue;
        updateAutoApproveToggle();
        showNotification(
            `Auto-approve ${newValue ? 'enabled' : 'disabled'}`,
            newValue ? 'warning' : 'success'
        );
    } catch (error) {
        console.error('[AdminJarvis] Failed to toggle auto-approve:', error);
        showNotification('Failed to update auto-approve setting', 'error');
    }
}

/**
 * Update the auto-approve toggle UI.
 */
function updateAutoApproveToggle() {
    const toggle = document.getElementById('aj-auto-approve-toggle');
    const statusText = document.getElementById('aj-auto-approve-status');

    if (toggle) {
        toggle.checked = ajAutoApproveEnabled;
    }

    if (statusText) {
        if (ajAutoApproveEnabled) {
            statusText.textContent = 'ON - New proposals auto-approved';
            statusText.className = 'text-yellow-400 text-sm';
        } else {
            statusText.textContent = 'OFF - Manual approval required';
            statusText.className = 'text-gray-400 text-sm';
        }
    }
}

// =============================================================================
// Tool Proposals
// =============================================================================

/**
 * Load pending tool proposals.
 */
async function loadToolProposals() {
    const container = document.getElementById('aj-tool-proposals');
    if (!container) return;

    container.innerHTML = '<p class="text-gray-500 text-sm">Loading...</p>';

    try {
        const response = await fetch('/api/tool-proposals?status=pending');
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const proposals = await response.json();

        if (proposals.length === 0) {
            container.innerHTML = '<p class="text-gray-500 text-sm">No pending tool proposals.</p>';
            return;
        }

        let html = '';
        proposals.forEach(proposal => {
            html += renderToolProposal(proposal);
        });

        container.innerHTML = html;
    } catch (error) {
        console.error('[AdminJarvis] Failed to load tool proposals:', error);
        container.innerHTML = `<p class="text-red-400 text-sm">Failed to load proposals: ${error.message}</p>`;
    }
}

/**
 * Render a tool proposal card.
 */
function renderToolProposal(proposal) {
    const createdAt = new Date(proposal.created_at).toLocaleDateString();

    return `
        <div class="bg-dark-bg rounded-lg p-4 border border-dark-border">
            <div class="flex justify-between items-start mb-3">
                <div>
                    <h4 class="text-white font-medium">${escapeHtml(proposal.name)}</h4>
                    <p class="text-gray-400 text-sm">${escapeHtml(proposal.description)}</p>
                </div>
                <span class="text-xs text-gray-500">${createdAt}</span>
            </div>

            <div class="mb-3">
                <span class="text-gray-500 text-xs">Trigger phrases:</span>
                <div class="flex flex-wrap gap-1 mt-1">
                    ${(proposal.trigger_phrases || []).map(p =>
                        `<span class="px-2 py-0.5 bg-dark-card rounded text-xs text-gray-300">${escapeHtml(p)}</span>`
                    ).join('')}
                </div>
            </div>

            <div class="flex gap-2">
                <button onclick="approveToolProposal('${proposal.proposal_id}')"
                    class="px-3 py-1 bg-green-600 hover:bg-green-700 text-white rounded text-sm">
                    ✓ Approve
                </button>
                <button onclick="rejectToolProposal('${proposal.proposal_id}')"
                    class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm">
                    ✗ Reject
                </button>
                <button onclick="viewToolProposalDetails('${proposal.proposal_id}')"
                    class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm">
                    View Details
                </button>
            </div>
        </div>
    `;
}

/**
 * Approve a tool proposal.
 */
async function approveToolProposal(proposalId) {
    if (!confirm('Approve this tool proposal? It will be deployed to n8n.')) {
        return;
    }

    try {
        const token = localStorage.getItem('auth_token');
        const response = await fetch(`/api/tool-proposals/${proposalId}/approve`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': token ? `Bearer ${token}` : ''
            },
            credentials: 'include'
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || `HTTP ${response.status}`);
        }

        showNotification('Tool proposal approved and deployed', 'success');
        loadToolProposals();
    } catch (error) {
        console.error('[AdminJarvis] Failed to approve proposal:', error);
        showNotification(`Failed to approve: ${error.message}`, 'error');
    }
}

/**
 * Reject a tool proposal.
 */
async function rejectToolProposal(proposalId) {
    const reason = prompt('Reason for rejection (optional):');

    try {
        const token = localStorage.getItem('auth_token');
        const response = await fetch(`/api/tool-proposals/${proposalId}/reject`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': token ? `Bearer ${token}` : ''
            },
            credentials: 'include',
            body: JSON.stringify({ reason: reason || '' })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || `HTTP ${response.status}`);
        }

        showNotification('Tool proposal rejected', 'info');
        loadToolProposals();
    } catch (error) {
        console.error('[AdminJarvis] Failed to reject proposal:', error);
        showNotification(`Failed to reject: ${error.message}`, 'error');
    }
}

/**
 * View tool proposal details.
 */
function viewToolProposalDetails(proposalId) {
    // TODO: Implement modal with full proposal details including workflow JSON
    alert('Details modal not yet implemented');
}

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Escape HTML to prevent XSS.
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Format duration in human-readable format.
 */
function formatDuration(ms) {
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

/**
 * Show a notification toast.
 */
function showNotification(message, type = 'info') {
    // Reuse existing notification system if available
    if (typeof showToast === 'function') {
        showToast(message, type);
    } else {
        console.log(`[${type}] ${message}`);
    }
}

// =============================================================================
// Polling Fallback (when WebSocket unavailable)
// =============================================================================

let ajPollingInterval = null;
let ajLastEventTimestamp = null;
const AJ_POLLING_INTERVAL = 2000; // 2 seconds

/**
 * Start polling for events (fallback when WebSocket fails).
 */
function startPolling() {
    if (ajPollingInterval) return;

    console.log('[AdminJarvis] Starting polling fallback');
    ajLastEventTimestamp = Date.now() / 1000;

    ajPollingInterval = setInterval(async () => {
        await pollForEvents();
    }, AJ_POLLING_INTERVAL);
}

/**
 * Stop polling for events.
 */
function stopPolling() {
    if (ajPollingInterval) {
        clearInterval(ajPollingInterval);
        ajPollingInterval = null;
        console.log('[AdminJarvis] Stopped polling');
    }
}

/**
 * Poll for new events from REST API.
 */
async function pollForEvents() {
    try {
        const since = ajLastEventTimestamp || (Date.now() / 1000 - 60);
        const response = await fetch(`/api/pipeline-events?since=${since}&limit=50`);

        if (!response.ok) {
            console.warn('[AdminJarvis] Poll failed:', response.status);
            return;
        }

        const events = await response.json();

        if (events.length > 0) {
            // Update timestamp to latest
            ajLastEventTimestamp = Math.max(...events.map(e => e.timestamp));

            // Process events
            events.forEach(event => processPipelineEvent(event));
        }
    } catch (error) {
        console.error('[AdminJarvis] Polling error:', error);
    }
}

/**
 * Toggle between WebSocket and polling mode.
 */
function toggleConnectionMode() {
    const modeSelect = document.getElementById('aj-connection-mode');
    const mode = modeSelect ? modeSelect.value : 'websocket';

    if (mode === 'websocket') {
        stopPolling();
        connectWebSocket();
    } else {
        disconnectWebSocket();
        startPolling();
        updateWsStatus('polling');
    }
}

// Enhance updateWsStatus to handle polling
const originalUpdateWsStatus = updateWsStatus;
updateWsStatus = function(status) {
    if (status === 'polling') {
        const statusEl = document.getElementById('ws-status');
        if (statusEl) {
            statusEl.innerHTML = '<i data-lucide="refresh-cw" class="w-3 h-3 inline-block mr-1 animate-spin"></i> Polling';
            statusEl.className = 'px-3 py-1 rounded-full text-sm font-medium bg-blue-900 text-blue-400';
        }
        return;
    }
    originalUpdateWsStatus(status);
};

// =============================================================================
// Initialization
// =============================================================================

/**
 * Initialize Admin Jarvis when tab is shown.
 */
function initAdminJarvis() {
    console.log('[AdminJarvis] Initializing...');
    updateStatsDisplay();
    updateActiveSessionsDisplay();
    loadAutoApproveSettings();
    loadToolProposals();
}

/**
 * Cleanup Admin Jarvis when navigating away.
 * Disconnects WebSocket and stops all intervals.
 */
function destroyAdminJarvis() {
    console.log('[AdminJarvis] Cleaning up...');
    disconnectWebSocket();
    stopHeartbeat();
    stopPolling();
}

// Export cleanup function
if (typeof window !== 'undefined') {
    window.destroyAdminJarvis = destroyAdminJarvis;
}

// Tab initialization is now handled by showTab() switch case in app.js
