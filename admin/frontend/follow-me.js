/**
 * Follow-Me Audio Configuration Management
 *
 * Manages the follow-me audio feature that automatically transfers
 * music playback to rooms based on motion sensor detection.
 *
 * Features:
 * - Enable/disable follow-me functionality
 * - Mode selection (off, single user, party)
 * - Timing configuration (debounce, grace period)
 * - Room-to-motion-sensor mapping
 * - Room exclusion management
 * - Quiet hours configuration
 */

const FOLLOW_ME_API = '/api/follow-me';

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let followMeConfig = null;
let roomMotionSensors = [];
let excludedRooms = [];

/**
 * Load all follow-me configuration data
 */
async function loadFollowMeData() {
    try {
        showLoadingState('follow-me-container');

        // Load config, rooms, and excluded rooms in parallel
        const [configResponse, roomsResponse, excludedResponse] = await Promise.all([
            fetch(`${FOLLOW_ME_API}/config`, { headers: getAuthHeaders() }),
            fetch(`${FOLLOW_ME_API}/rooms`, { headers: getAuthHeaders() }),
            fetch(`${FOLLOW_ME_API}/excluded`, { headers: getAuthHeaders() })
        ]);

        if (configResponse.ok) {
            followMeConfig = await configResponse.json();
        } else if (configResponse.status === 404) {
            // No config yet - use defaults
            followMeConfig = {
                enabled: true,
                mode: 'single',
                debounce_seconds: 5.0,
                grace_period_seconds: 30.0,
                min_motion_duration_seconds: 2.0,
                quiet_hours_start: 23,
                quiet_hours_end: 7
            };
        } else {
            throw new Error(`Failed to load config: ${configResponse.statusText}`);
        }

        if (roomsResponse.ok) {
            roomMotionSensors = await roomsResponse.json();
        }

        if (excludedResponse.ok) {
            excludedRooms = await excludedResponse.json();
        }

        renderFollowMeUI();
    } catch (error) {
        console.error('Error loading follow-me data:', error);
        safeShowToast('Failed to load follow-me configuration', 'error');
        showError('follow-me-container', 'Failed to load follow-me configuration');
    }
}

/**
 * Update follow-me configuration
 */
async function updateFollowMeConfig(updates) {
    try {
        const response = await fetch(`${FOLLOW_ME_API}/config`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(updates)
        });

        if (!response.ok) {
            throw new Error(`Failed to update config: ${response.statusText}`);
        }

        followMeConfig = await response.json();
        safeShowToast('Configuration updated', 'success');
        renderFollowMeUI();
    } catch (error) {
        console.error('Error updating config:', error);
        safeShowToast('Failed to update configuration', 'error');
    }
}

/**
 * Create or update room motion sensor mapping
 */
async function saveRoomSensor(roomName, motionEntityId, enabled, priority) {
    try {
        const response = await fetch(`${FOLLOW_ME_API}/rooms`, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                room_name: roomName,
                motion_entity_id: motionEntityId,
                enabled: enabled,
                priority: priority
            })
        });

        if (!response.ok) {
            throw new Error(`Failed to save room sensor: ${response.statusText}`);
        }

        safeShowToast('Room sensor saved', 'success');
        await loadFollowMeData();
    } catch (error) {
        console.error('Error saving room sensor:', error);
        safeShowToast('Failed to save room sensor', 'error');
    }
}

/**
 * Delete room motion sensor mapping
 */
async function deleteRoomSensor(roomName) {
    if (!confirm(`Remove motion sensor mapping for "${roomName}"?`)) {
        return;
    }

    try {
        const response = await fetch(`${FOLLOW_ME_API}/rooms/${encodeURIComponent(roomName)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to delete room sensor: ${response.statusText}`);
        }

        safeShowToast('Room sensor removed', 'success');
        await loadFollowMeData();
    } catch (error) {
        console.error('Error deleting room sensor:', error);
        safeShowToast('Failed to delete room sensor', 'error');
    }
}

/**
 * Add room to exclusion list
 */
async function addExcludedRoom(roomName, reason) {
    try {
        const response = await fetch(`${FOLLOW_ME_API}/excluded`, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                room_name: roomName,
                reason: reason || null
            })
        });

        if (!response.ok) {
            throw new Error(`Failed to add excluded room: ${response.statusText}`);
        }

        safeShowToast('Room excluded', 'success');
        await loadFollowMeData();
    } catch (error) {
        console.error('Error adding excluded room:', error);
        safeShowToast('Failed to exclude room', 'error');
    }
}

/**
 * Remove room from exclusion list
 */
async function removeExcludedRoom(roomName) {
    try {
        const response = await fetch(`${FOLLOW_ME_API}/excluded/${encodeURIComponent(roomName)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to remove excluded room: ${response.statusText}`);
        }

        safeShowToast('Room exclusion removed', 'success');
        await loadFollowMeData();
    } catch (error) {
        console.error('Error removing excluded room:', error);
        safeShowToast('Failed to remove exclusion', 'error');
    }
}

// ============================================================================
// UI RENDERING
// ============================================================================

/**
 * Render the complete follow-me UI
 */
function renderFollowMeUI() {
    const container = document.getElementById('follow-me-container');
    if (!container) return;

    container.innerHTML = `
        <div class="space-y-6">
            <!-- Header -->
            <div class="flex items-center justify-between">
                <div>
                    <h2 class="text-2xl font-bold text-white">Follow-Me Audio</h2>
                    <p class="text-gray-400 mt-1">Automatically transfer music playback based on motion detection</p>
                </div>
                <button onclick="loadFollowMeData()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-2">
                    <i data-lucide="refresh-cw" class="w-4 h-4"></i> Refresh
                </button>
            </div>

            <!-- Main Configuration Card -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <h3 class="text-lg font-semibold text-white mb-4">Configuration</h3>

                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    <!-- Enable/Disable -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Status</label>
                        <div class="flex items-center gap-3">
                            <button onclick="updateFollowMeConfig({enabled: ${!followMeConfig?.enabled}})"
                                class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${followMeConfig?.enabled ? 'bg-blue-600' : 'bg-gray-600'}">
                                <span class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${followMeConfig?.enabled ? 'translate-x-6' : 'translate-x-1'}"></span>
                            </button>
                            <span class="text-sm ${followMeConfig?.enabled ? 'text-green-400' : 'text-gray-400'}">
                                ${followMeConfig?.enabled ? 'Enabled' : 'Disabled'}
                            </span>
                        </div>
                    </div>

                    <!-- Mode Selection -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Mode</label>
                        <select onchange="updateFollowMeConfig({mode: this.value})"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                            <option value="off" ${followMeConfig?.mode === 'off' ? 'selected' : ''}>Off</option>
                            <option value="single" ${followMeConfig?.mode === 'single' ? 'selected' : ''}>Single User</option>
                            <option value="party" ${followMeConfig?.mode === 'party' ? 'selected' : ''}>Party Mode</option>
                        </select>
                        <p class="text-xs text-gray-500 mt-1">
                            ${followMeConfig?.mode === 'single' ? 'Music follows one person between rooms' :
                              followMeConfig?.mode === 'party' ? 'Music plays in all rooms with motion' : 'Follow-me disabled'}
                        </p>
                    </div>

                    <!-- Debounce Time -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Debounce (seconds)</label>
                        <input type="number" min="1" max="30" step="0.5" value="${followMeConfig?.debounce_seconds || 5}"
                            onchange="updateFollowMeConfig({debounce_seconds: parseFloat(this.value)})"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Minimum time between room transfers</p>
                    </div>

                    <!-- Grace Period -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Grace Period (seconds)</label>
                        <input type="number" min="5" max="120" step="5" value="${followMeConfig?.grace_period_seconds || 30}"
                            onchange="updateFollowMeConfig({grace_period_seconds: parseFloat(this.value)})"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Keep playing after motion clears</p>
                    </div>

                    <!-- Min Motion Duration -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Min Motion Duration (seconds)</label>
                        <input type="number" min="0.5" max="10" step="0.5" value="${followMeConfig?.min_motion_duration_seconds || 2}"
                            onchange="updateFollowMeConfig({min_motion_duration_seconds: parseFloat(this.value)})"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Ignore brief motion events</p>
                    </div>

                    <!-- Quiet Hours -->
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Quiet Hours</label>
                        <div class="flex items-center gap-2">
                            <input type="number" min="0" max="23" value="${followMeConfig?.quiet_hours_start ?? 23}"
                                onchange="updateFollowMeConfig({quiet_hours_start: parseInt(this.value)})"
                                class="w-16 px-2 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white text-center focus:outline-none focus:border-blue-500">
                            <span class="text-gray-400">to</span>
                            <input type="number" min="0" max="23" value="${followMeConfig?.quiet_hours_end ?? 7}"
                                onchange="updateFollowMeConfig({quiet_hours_end: parseInt(this.value)})"
                                class="w-16 px-2 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white text-center focus:outline-none focus:border-blue-500">
                        </div>
                        <p class="text-xs text-gray-500 mt-1">No auto-transfers during these hours</p>
                    </div>
                </div>
            </div>

            <!-- Room Motion Sensors -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex items-center justify-between mb-4">
                    <h3 class="text-lg font-semibold text-white">Room Motion Sensors</h3>
                    <button onclick="showAddRoomSensorModal()"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors">
                        + Add Room
                    </button>
                </div>

                ${roomMotionSensors.length === 0 ? `
                    <div class="text-center py-8 text-gray-500">
                        <p>No room sensors configured</p>
                        <p class="text-sm mt-1">Add rooms and their motion sensors to enable follow-me</p>
                    </div>
                ` : `
                    <div class="overflow-x-auto">
                        <table class="w-full">
                            <thead>
                                <tr class="border-b border-gray-700">
                                    <th class="text-left py-3 px-4 text-sm font-medium text-gray-400">Room</th>
                                    <th class="text-left py-3 px-4 text-sm font-medium text-gray-400">Motion Entity</th>
                                    <th class="text-left py-3 px-4 text-sm font-medium text-gray-400">Priority</th>
                                    <th class="text-left py-3 px-4 text-sm font-medium text-gray-400">Status</th>
                                    <th class="text-right py-3 px-4 text-sm font-medium text-gray-400">Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${roomMotionSensors.map(sensor => `
                                    <tr class="border-b border-gray-800 hover:bg-gray-800/50">
                                        <td class="py-3 px-4 text-white font-medium">${escapeHtml(sensor.room_name)}</td>
                                        <td class="py-3 px-4 text-gray-300 font-mono text-sm">${escapeHtml(sensor.motion_entity_id)}</td>
                                        <td class="py-3 px-4 text-gray-300">${sensor.priority}</td>
                                        <td class="py-3 px-4">
                                            <span class="px-2 py-1 rounded text-xs font-medium ${sensor.enabled ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">
                                                ${sensor.enabled ? 'Enabled' : 'Disabled'}
                                            </span>
                                        </td>
                                        <td class="py-3 px-4 text-right">
                                            <button onclick="showEditRoomSensorModal('${escapeHtml(sensor.room_name)}', '${escapeHtml(sensor.motion_entity_id)}', ${sensor.enabled}, ${sensor.priority})"
                                                class="text-blue-400 hover:text-blue-300 mr-3">Edit</button>
                                            <button onclick="deleteRoomSensor('${escapeHtml(sensor.room_name)}')"
                                                class="text-red-400 hover:text-red-300">Delete</button>
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `}
            </div>

            <!-- Excluded Rooms -->
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex items-center justify-between mb-4">
                    <div>
                        <h3 class="text-lg font-semibold text-white">Excluded Rooms</h3>
                        <p class="text-sm text-gray-400">Rooms that won't participate in follow-me transfers</p>
                    </div>
                    <button onclick="showAddExcludedRoomModal()"
                        class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors">
                        + Exclude Room
                    </button>
                </div>

                ${excludedRooms.length === 0 ? `
                    <div class="text-center py-6 text-gray-500">
                        <p>No rooms excluded</p>
                    </div>
                ` : `
                    <div class="flex flex-wrap gap-2">
                        ${excludedRooms.map(room => `
                            <div class="flex items-center gap-2 px-3 py-2 bg-gray-800 rounded-lg">
                                <span class="text-white">${escapeHtml(room.room_name)}</span>
                                ${room.reason ? `<span class="text-xs text-gray-500">(${escapeHtml(room.reason)})</span>` : ''}
                                <button onclick="removeExcludedRoom('${escapeHtml(room.room_name)}')"
                                    class="text-gray-400 hover:text-red-400 ml-1">×</button>
                            </div>
                        `).join('')}
                    </div>
                `}
            </div>
        </div>
    `;
}

// ============================================================================
// MODALS
// ============================================================================

/**
 * Show modal to add a new room sensor mapping
 */
function showAddRoomSensorModal() {
    const modalHtml = `
        <div id="room-sensor-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target === this) closeRoomSensorModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
                <h3 class="text-lg font-semibold text-white mb-4">Add Room Sensor</h3>

                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Room Name</label>
                        <input type="text" id="sensor-room-name" placeholder="e.g., living_room"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Motion Entity ID</label>
                        <input type="text" id="sensor-entity-id" placeholder="e.g., binary_sensor.living_room_motion"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Priority</label>
                        <input type="number" id="sensor-priority" value="5" min="0" max="100"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <p class="text-xs text-gray-500 mt-1">Higher priority rooms preferred when multiple have motion</p>
                    </div>

                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="sensor-enabled" checked class="rounded">
                        <label for="sensor-enabled" class="text-sm text-gray-300">Enabled</label>
                    </div>
                </div>

                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeRoomSensorModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="submitRoomSensor()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                        Add Room
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

/**
 * Show modal to edit an existing room sensor
 */
function showEditRoomSensorModal(roomName, entityId, enabled, priority) {
    const modalHtml = `
        <div id="room-sensor-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target === this) closeRoomSensorModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
                <h3 class="text-lg font-semibold text-white mb-4">Edit Room Sensor</h3>

                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Room Name</label>
                        <input type="text" id="sensor-room-name" value="${escapeHtml(roomName)}" readonly
                            class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-gray-400 cursor-not-allowed">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Motion Entity ID</label>
                        <input type="text" id="sensor-entity-id" value="${escapeHtml(entityId)}"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Priority</label>
                        <input type="number" id="sensor-priority" value="${priority}" min="0" max="100"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>

                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="sensor-enabled" ${enabled ? 'checked' : ''} class="rounded">
                        <label for="sensor-enabled" class="text-sm text-gray-300">Enabled</label>
                    </div>
                </div>

                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeRoomSensorModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="submitRoomSensor()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                        Save Changes
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

/**
 * Close room sensor modal
 */
function closeRoomSensorModal() {
    const modal = document.getElementById('room-sensor-modal');
    if (modal) modal.remove();
}

/**
 * Submit room sensor form
 */
function submitRoomSensor() {
    const roomName = document.getElementById('sensor-room-name').value.trim();
    const entityId = document.getElementById('sensor-entity-id').value.trim();
    const priority = parseInt(document.getElementById('sensor-priority').value) || 0;
    const enabled = document.getElementById('sensor-enabled').checked;

    if (!roomName || !entityId) {
        safeShowToast('Please fill in all required fields', 'error');
        return;
    }

    closeRoomSensorModal();
    saveRoomSensor(roomName, entityId, enabled, priority);
}

/**
 * Show modal to add an excluded room
 */
function showAddExcludedRoomModal() {
    const modalHtml = `
        <div id="excluded-room-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target === this) closeExcludedRoomModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
                <h3 class="text-lg font-semibold text-white mb-4">Exclude Room</h3>

                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Room Name</label>
                        <input type="text" id="excluded-room-name" placeholder="e.g., guest_bedroom"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-2">Reason (optional)</label>
                        <input type="text" id="excluded-room-reason" placeholder="e.g., Guest privacy"
                            class="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                </div>

                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeExcludedRoomModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="submitExcludedRoom()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                        Exclude Room
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

/**
 * Close excluded room modal
 */
function closeExcludedRoomModal() {
    const modal = document.getElementById('excluded-room-modal');
    if (modal) modal.remove();
}

/**
 * Submit excluded room form
 */
function submitExcludedRoom() {
    const roomName = document.getElementById('excluded-room-name').value.trim();
    const reason = document.getElementById('excluded-room-reason').value.trim();

    if (!roomName) {
        safeShowToast('Please enter a room name', 'error');
        return;
    }

    closeExcludedRoomModal();
    addExcludedRoom(roomName, reason);
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Show loading state in container
 */
function showLoadingState(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="flex items-center justify-center py-12">
                <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
                <span class="ml-3 text-gray-400">Loading...</span>
            </div>
        `;
    }
}

/**
 * Show error state in container
 */
function showError(containerId, message) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="flex items-center justify-center py-12 text-red-400">
                <span class="text-2xl mr-3">⚠️</span>
                <span>${message}</span>
            </div>
        `;
    }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

// Load data when follow-me tab is shown
document.addEventListener('DOMContentLoaded', () => {
    // The showTab function in app.js will call loadFollowMeData() when 'follow-me' tab is selected
});
