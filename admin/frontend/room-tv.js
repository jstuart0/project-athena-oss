/**
 * Room TV Configuration Management
 *
 * Manages Apple TV configuration for multi-room voice assistant system.
 * Each room can have an Apple TV mapped to it for voice control.
 *
 * Features:
 * - Room-to-Apple TV mapping
 * - App configuration (profile screens, delays, guest access)
 * - Feature flags (multi-TV commands, auto profile select)
 * - Apple TV discovery from Home Assistant
 * - Direct control testing (launch apps, remote commands)
 */

const ROOM_TV_API = '/api/room-tv';

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let allTVConfigs = [];
let allAppConfigs = [];
let allFeatureFlags = [];
let discoveredAppleTVs = [];

/**
 * Load all room TV configurations
 */
async function loadTVConfigs() {
    try {
        showTVLoadingState('room-tv-container');

        const [configResponse, appResponse, flagsResponse] = await Promise.all([
            fetch(ROOM_TV_API, { headers: getAuthHeaders() }),
            fetch(`${ROOM_TV_API}/apps/all`, { headers: getAuthHeaders() }),
            fetch(`${ROOM_TV_API}/features`, { headers: getAuthHeaders() })
        ]);

        if (!configResponse.ok) {
            throw new Error(`Failed to load TV configs: ${configResponse.statusText}`);
        }

        allTVConfigs = await configResponse.json();
        allAppConfigs = appResponse.ok ? await appResponse.json() : [];
        allFeatureFlags = flagsResponse.ok ? await flagsResponse.json() : [];

        renderTVConfigs();
    } catch (error) {
        console.error('Error loading TV configs:', error);
        safeShowToast('Failed to load TV configurations', 'error');
        showTVError('room-tv-container', 'Failed to load TV configurations');
    }
}

/**
 * Show loading state in container
 */
function showTVLoadingState(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="animate-pulse">Loading TV configurations...</div>
            </div>
        `;
    }
}

/**
 * Show error state in container
 */
function showTVError(containerId, message) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg p-4 text-center">
                <p class="text-red-400">${escapeHtml(message)}</p>
            </div>
        `;
    }
}

// ============================================================================
// RENDER FUNCTIONS
// ============================================================================

/**
 * Render all TV configurations in tabs
 */
function renderTVConfigs() {
    const container = document.getElementById('room-tv-container');

    // Room configs section
    const roomsHtml = renderRoomTVSection();

    // App configs section
    const appsHtml = renderAppConfigSection();

    // Feature flags section
    const flagsHtml = renderFeatureFlagsSection();

    container.innerHTML = `
        <!-- Tab Navigation -->
        <div class="mb-6 border-b border-gray-700">
            <nav class="flex gap-4" role="tablist">
                <button onclick="showTVSubTab('rooms')" id="tv-subtab-rooms" class="tv-subtab px-4 py-2 text-sm font-medium border-b-2 border-blue-500 text-blue-400">
                    Room Mappings
                </button>
                <button onclick="showTVSubTab('apps')" id="tv-subtab-apps" class="tv-subtab px-4 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-300">
                    App Configuration
                </button>
                <button onclick="showTVSubTab('features')" id="tv-subtab-features" class="tv-subtab px-4 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-300">
                    Feature Flags
                </button>
                <button onclick="showTVSubTab('control')" id="tv-subtab-control" class="tv-subtab px-4 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-300">
                    Remote Control
                </button>
            </nav>
        </div>

        <!-- Tab Content -->
        <div id="tv-subtab-content-rooms" class="tv-subtab-content">
            ${roomsHtml}
        </div>
        <div id="tv-subtab-content-apps" class="tv-subtab-content hidden">
            ${appsHtml}
        </div>
        <div id="tv-subtab-content-features" class="tv-subtab-content hidden">
            ${flagsHtml}
        </div>
        <div id="tv-subtab-content-control" class="tv-subtab-content hidden">
            ${renderRemoteControlSection()}
        </div>
    `;
}

/**
 * Show a TV sub-tab
 */
function showTVSubTab(tabName) {
    // Update tab buttons
    document.querySelectorAll('.tv-subtab').forEach(btn => {
        btn.classList.remove('border-blue-500', 'text-blue-400');
        btn.classList.add('border-transparent', 'text-gray-400');
    });
    const activeBtn = document.getElementById(`tv-subtab-${tabName}`);
    if (activeBtn) {
        activeBtn.classList.remove('border-transparent', 'text-gray-400');
        activeBtn.classList.add('border-blue-500', 'text-blue-400');
    }

    // Show/hide content
    document.querySelectorAll('.tv-subtab-content').forEach(content => {
        content.classList.add('hidden');
    });
    const activeContent = document.getElementById(`tv-subtab-content-${tabName}`);
    if (activeContent) {
        activeContent.classList.remove('hidden');
    }
}

/**
 * Render room TV mappings section
 */
function renderRoomTVSection() {
    if (allTVConfigs.length === 0) {
        return `
            <div class="text-center text-gray-400 py-8">
                <div class="text-4xl mb-4">📺</div>
                <p class="text-lg">No Apple TVs configured</p>
                <p class="text-sm mt-2">Click "Discover Apple TVs" to find devices</p>
            </div>
        `;
    }

    const cardsHtml = allTVConfigs.map(config => `
        <div class="bg-gray-800 rounded-lg p-4 border border-gray-700">
            <div class="flex justify-between items-start mb-3">
                <div>
                    <h3 class="text-lg font-medium text-white">${escapeHtml(config.display_name)}</h3>
                    <p class="text-sm text-gray-400">${escapeHtml(config.room_name)}</p>
                </div>
                <div class="flex items-center gap-2">
                    <span class="px-2 py-1 rounded text-xs ${config.enabled ? 'bg-green-900/50 text-green-400' : 'bg-red-900/50 text-red-400'}">
                        ${config.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                    <button onclick="editTVConfig(${config.id})" class="text-blue-400 hover:text-blue-300 text-sm">Edit</button>
                    <button onclick="deleteTVConfig(${config.id}, '${escapeHtml(config.room_name)}')" class="text-red-400 hover:text-red-300 text-sm">Delete</button>
                </div>
            </div>
            <div class="text-sm text-gray-400 space-y-1">
                <p><span class="text-gray-500">Media Player:</span> ${escapeHtml(config.media_player_entity_id)}</p>
                <p><span class="text-gray-500">Remote:</span> ${escapeHtml(config.remote_entity_id)}</p>
            </div>
            <div class="mt-3 pt-3 border-t border-gray-700 flex gap-2">
                <button onclick="testTVPower('${config.room_name}', 'on')" class="px-3 py-1 bg-green-600/20 text-green-400 rounded text-xs hover:bg-green-600/40">
                    Power On
                </button>
                <button onclick="testTVPower('${config.room_name}', 'off')" class="px-3 py-1 bg-red-600/20 text-red-400 rounded text-xs hover:bg-red-600/40">
                    Power Off
                </button>
                <button onclick="showAppLauncher('${config.room_name}')" class="px-3 py-1 bg-blue-600/20 text-blue-400 rounded text-xs hover:bg-blue-600/40">
                    Launch App
                </button>
            </div>
        </div>
    `).join('');

    return `
        <div class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            ${cardsHtml}
        </div>
    `;
}

/**
 * Render app configuration section
 */
function renderAppConfigSection() {
    if (allAppConfigs.length === 0) {
        return `
            <div class="text-center text-gray-400 py-8">
                <p>No apps configured. Sync apps from an Apple TV to get started.</p>
            </div>
        `;
    }

    const tableRows = allAppConfigs.map(app => `
        <tr class="border-b border-gray-700 hover:bg-gray-800/50">
            <td class="py-3 px-4">
                <div class="flex items-center gap-3">
                    ${app.icon_url ? `<img src="${escapeHtml(app.icon_url)}" class="w-8 h-8 rounded" alt="" onerror="this.style.display='none'">` : '<div class="w-8 h-8 bg-gray-700 rounded flex items-center justify-center text-xs">📱</div>'}
                    <div>
                        <div class="font-medium text-white">${escapeHtml(app.display_name)}</div>
                        <div class="text-xs text-gray-500">${escapeHtml(app.app_name)}</div>
                    </div>
                </div>
            </td>
            <td class="py-3 px-4 text-center">
                <input type="checkbox" ${app.enabled ? 'checked' : ''} onchange="updateAppConfig(${app.id}, 'enabled', this.checked)" class="rounded bg-gray-700 border-gray-600">
            </td>
            <td class="py-3 px-4 text-center">
                <input type="checkbox" ${app.has_profile_screen ? 'checked' : ''} onchange="updateAppConfig(${app.id}, 'has_profile_screen', this.checked)" class="rounded bg-gray-700 border-gray-600">
            </td>
            <td class="py-3 px-4 text-center">
                <input type="number" value="${app.profile_select_delay_ms}" onchange="updateAppConfig(${app.id}, 'profile_select_delay_ms', parseInt(this.value))" class="w-20 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-white text-sm text-center" min="500" max="5000" step="100">
            </td>
            <td class="py-3 px-4 text-center">
                <input type="checkbox" ${app.guest_allowed ? 'checked' : ''} onchange="updateAppConfig(${app.id}, 'guest_allowed', this.checked)" class="rounded bg-gray-700 border-gray-600">
            </td>
            <td class="py-3 px-4">
                <input type="text" value="${escapeHtml(app.deep_link_scheme || '')}" onchange="updateAppConfig(${app.id}, 'deep_link_scheme', this.value || null)" placeholder="e.g. youtube" class="w-24 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-white text-sm">
            </td>
            <td class="py-3 px-4 text-center">
                <input type="number" value="${app.sort_order}" onchange="updateAppConfig(${app.id}, 'sort_order', parseInt(this.value))" class="w-16 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-white text-sm text-center" min="0">
            </td>
        </tr>
    `).join('');

    return `
        <div class="overflow-x-auto">
            <table class="w-full text-sm">
                <thead>
                    <tr class="text-left text-gray-400 border-b border-gray-700">
                        <th class="py-3 px-4">App</th>
                        <th class="py-3 px-4 text-center">Enabled</th>
                        <th class="py-3 px-4 text-center">Profile Screen</th>
                        <th class="py-3 px-4 text-center">Delay (ms)</th>
                        <th class="py-3 px-4 text-center">Guest OK</th>
                        <th class="py-3 px-4">Deep Link</th>
                        <th class="py-3 px-4 text-center">Order</th>
                    </tr>
                </thead>
                <tbody>
                    ${tableRows}
                </tbody>
            </table>
        </div>
    `;
}

/**
 * Render feature flags section
 */
function renderFeatureFlagsSection() {
    const flagsHtml = allFeatureFlags.map(flag => `
        <div class="bg-gray-800 rounded-lg p-4 border border-gray-700 flex justify-between items-center">
            <div>
                <h4 class="font-medium text-white">${escapeHtml(flag.feature_name.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()))}</h4>
                <p class="text-sm text-gray-400">${escapeHtml(flag.description || '')}</p>
            </div>
            <label class="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" ${flag.enabled ? 'checked' : ''} onchange="updateFeatureFlag('${flag.feature_name}', this.checked)" class="sr-only peer">
                <div class="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600"></div>
            </label>
        </div>
    `).join('');

    return `
        <div class="space-y-4">
            ${flagsHtml || '<p class="text-gray-400 text-center py-4">No feature flags configured</p>'}
        </div>
    `;
}

/**
 * Render remote control section
 */
function renderRemoteControlSection() {
    if (allTVConfigs.length === 0) {
        return `
            <div class="text-center text-gray-400 py-8">
                <p>Configure a room first to use remote control</p>
            </div>
        `;
    }

    const roomOptions = allTVConfigs.filter(c => c.enabled).map(c =>
        `<option value="${escapeHtml(c.room_name)}">${escapeHtml(c.display_name)}</option>`
    ).join('');

    const appOptions = allAppConfigs.filter(a => a.enabled).map(a =>
        `<option value="${escapeHtml(a.app_name)}">${escapeHtml(a.display_name)}</option>`
    ).join('');

    return `
        <div class="max-w-md mx-auto">
            <!-- Room Selector -->
            <div class="mb-6">
                <label class="block text-sm font-medium text-gray-400 mb-2">Select TV</label>
                <select id="remote-room-select" class="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white">
                    ${roomOptions}
                </select>
            </div>

            <!-- App Launcher -->
            <div class="mb-6 p-4 bg-gray-800 rounded-lg border border-gray-700">
                <h4 class="text-sm font-medium text-gray-400 mb-3">Launch App</h4>
                <div class="flex gap-2">
                    <select id="app-select" class="flex-1 px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm">
                        ${appOptions}
                    </select>
                    <button onclick="launchSelectedApp()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm">
                        Launch
                    </button>
                </div>
            </div>

            <!-- Navigation Pad -->
            <div class="p-4 bg-gray-800 rounded-lg border border-gray-700">
                <h4 class="text-sm font-medium text-gray-400 mb-4 text-center">Navigation</h4>
                <div class="grid grid-cols-3 gap-2 max-w-48 mx-auto">
                    <div></div>
                    <button onclick="sendRemoteCommand('up')" class="p-4 bg-gray-700 hover:bg-gray-600 rounded-lg text-white text-lg">
                        ▲
                    </button>
                    <div></div>
                    <button onclick="sendRemoteCommand('left')" class="p-4 bg-gray-700 hover:bg-gray-600 rounded-lg text-white text-lg">
                        ◀
                    </button>
                    <button onclick="sendRemoteCommand('select')" class="p-4 bg-blue-600 hover:bg-blue-700 rounded-lg text-white font-medium">
                        OK
                    </button>
                    <button onclick="sendRemoteCommand('right')" class="p-4 bg-gray-700 hover:bg-gray-600 rounded-lg text-white text-lg">
                        ▶
                    </button>
                    <div></div>
                    <button onclick="sendRemoteCommand('down')" class="p-4 bg-gray-700 hover:bg-gray-600 rounded-lg text-white text-lg">
                        ▼
                    </button>
                    <div></div>
                </div>

                <!-- Menu/Home Buttons -->
                <div class="flex justify-center gap-4 mt-4">
                    <button onclick="sendRemoteCommand('menu')" class="px-6 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-white text-sm">
                        Menu
                    </button>
                    <button onclick="sendRemoteCommand('home')" class="px-6 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-white text-sm">
                        Home
                    </button>
                </div>

                <!-- Playback Controls -->
                <div class="flex justify-center gap-2 mt-4 pt-4 border-t border-gray-700">
                    <button onclick="sendRemoteCommand('play')" class="p-3 bg-gray-700 hover:bg-gray-600 rounded-lg text-white">
                        ▶️
                    </button>
                    <button onclick="sendRemoteCommand('pause')" class="p-3 bg-gray-700 hover:bg-gray-600 rounded-lg text-white">
                        ⏸️
                    </button>
                </div>

                <!-- Power Controls -->
                <div class="flex justify-center gap-4 mt-4 pt-4 border-t border-gray-700">
                    <button onclick="powerControl('on')" class="px-4 py-2 bg-green-600/20 text-green-400 hover:bg-green-600/40 rounded-lg text-sm">
                        Power On
                    </button>
                    <button onclick="powerControl('off')" class="px-4 py-2 bg-red-600/20 text-red-400 hover:bg-red-600/40 rounded-lg text-sm">
                        Power Off
                    </button>
                </div>
            </div>

            <!-- State Display -->
            <div id="tv-state-display" class="mt-4 p-4 bg-gray-800 rounded-lg border border-gray-700">
                <div class="flex justify-between items-center mb-2">
                    <h4 class="text-sm font-medium text-gray-400">Current State</h4>
                    <button onclick="refreshTVState()" class="text-xs text-blue-400 hover:text-blue-300">Refresh</button>
                </div>
                <div id="tv-state-content" class="text-sm text-gray-400">
                    Click Refresh to load state
                </div>
            </div>
        </div>
    `;
}

// ============================================================================
// API FUNCTIONS
// ============================================================================

/**
 * Discover Apple TVs from Home Assistant
 */
async function discoverAppleTVs() {
    try {
        safeShowToast('Discovering Apple TVs...', 'info');

        const response = await fetch(`${ROOM_TV_API}/discover`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Discovery failed');
        }

        discoveredAppleTVs = await response.json();

        if (discoveredAppleTVs.length === 0) {
            safeShowToast('No Apple TVs found in Home Assistant', 'warning');
            return;
        }

        safeShowToast(`Found ${discoveredAppleTVs.length} Apple TV(s)`, 'success');
        showDiscoveryModal(discoveredAppleTVs);
    } catch (error) {
        console.error('Error discovering Apple TVs:', error);
        safeShowToast('Failed to discover Apple TVs', 'error');
    }
}

/**
 * Show discovery modal with found Apple TVs
 */
function showDiscoveryModal(appleTVs) {
    const itemsHtml = appleTVs.map(tv => `
        <div class="flex items-center justify-between p-3 bg-gray-700 rounded-lg ${tv.already_configured ? 'opacity-50' : ''}">
            <div>
                <div class="font-medium text-white">${escapeHtml(tv.friendly_name)}</div>
                <div class="text-xs text-gray-400">${escapeHtml(tv.entity_id)}</div>
                <div class="text-xs text-gray-500">${tv.app_count} apps | ${tv.state}</div>
            </div>
            <div>
                ${tv.already_configured
                    ? '<span class="text-xs text-green-400">Already configured</span>'
                    : `<button onclick="addAppleTVFromDiscovery('${escapeHtml(tv.entity_id)}', '${escapeHtml(tv.remote_entity_id)}', '${escapeHtml(tv.suggested_room)}')" class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm">Add</button>`
                }
            </div>
        </div>
    `).join('');

    showModal('Discovered Apple TVs', `
        <div class="space-y-3">
            ${itemsHtml}
        </div>
    `, [
        { label: 'Close', onClick: 'closeModal()' }
    ]);
}

/**
 * Add Apple TV from discovery
 */
async function addAppleTVFromDiscovery(entityId, remoteId, suggestedRoom) {
    const roomName = prompt('Enter room name:', suggestedRoom);
    if (!roomName) return;

    try {
        const response = await fetch(ROOM_TV_API, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                room_name: roomName,
                media_player_entity_id: entityId,
                remote_entity_id: remoteId,
                enabled: true
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add');
        }

        safeShowToast(`Added ${roomName} TV`, 'success');
        closeModal();
        loadTVConfigs();

        // Sync apps from this device
        await syncAppsFromDevice(entityId);
    } catch (error) {
        console.error('Error adding Apple TV:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Sync apps from a device
 */
async function syncAppsFromDevice(entityId) {
    try {
        safeShowToast('Syncing apps from device...', 'info');

        const response = await fetch(`${ROOM_TV_API}/apps/sync/${encodeURIComponent(entityId)}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Sync failed');
        }

        const result = await response.json();
        safeShowToast(`Synced ${result.total_apps} apps (${result.new_apps_added} new)`, 'success');
        loadTVConfigs(); // Reload to show new apps
    } catch (error) {
        console.error('Error syncing apps:', error);
        safeShowToast('Failed to sync apps', 'error');
    }
}

/**
 * Update app configuration
 */
async function updateAppConfig(appId, field, value) {
    try {
        const response = await fetch(`${ROOM_TV_API}/apps/${appId}`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ [field]: value })
        });

        if (!response.ok) {
            throw new Error('Update failed');
        }

        safeShowToast('App updated', 'success');
    } catch (error) {
        console.error('Error updating app:', error);
        safeShowToast('Failed to update app', 'error');
        loadTVConfigs(); // Reload to reset UI
    }
}

/**
 * Update feature flag
 */
async function updateFeatureFlag(featureName, enabled) {
    try {
        const response = await fetch(`${ROOM_TV_API}/features/${featureName}?enabled=${enabled}`, {
            method: 'PUT',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Update failed');
        }

        safeShowToast(`${featureName.replace(/_/g, ' ')} ${enabled ? 'enabled' : 'disabled'}`, 'success');
    } catch (error) {
        console.error('Error updating feature flag:', error);
        safeShowToast('Failed to update feature', 'error');
        loadTVConfigs(); // Reload to reset UI
    }
}

/**
 * Delete TV config
 */
async function deleteTVConfig(configId, roomName) {
    if (!confirm(`Delete TV configuration for ${roomName}?`)) return;

    try {
        const response = await fetch(`${ROOM_TV_API}/${configId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Delete failed');
        }

        safeShowToast(`Deleted ${roomName}`, 'success');
        loadTVConfigs();
    } catch (error) {
        console.error('Error deleting TV config:', error);
        safeShowToast('Failed to delete', 'error');
    }
}

/**
 * Edit TV config
 */
function editTVConfig(configId) {
    const config = allTVConfigs.find(c => c.id === configId);
    if (!config) return;

    showModal('Edit TV Configuration', `
        <div class="space-y-4">
            <div>
                <label class="block text-sm font-medium text-gray-400 mb-1">Display Name</label>
                <input type="text" id="edit-tv-display-name" value="${escapeHtml(config.display_name)}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white">
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-400 mb-1">Media Player Entity</label>
                <input type="text" id="edit-tv-media-player" value="${escapeHtml(config.media_player_entity_id)}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white">
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-400 mb-1">Remote Entity</label>
                <input type="text" id="edit-tv-remote" value="${escapeHtml(config.remote_entity_id)}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white">
            </div>
            <div class="flex items-center gap-2">
                <input type="checkbox" id="edit-tv-enabled" ${config.enabled ? 'checked' : ''} class="rounded bg-gray-700 border-gray-600">
                <label class="text-sm text-gray-400">Enabled</label>
            </div>
        </div>
    `, [
        { label: 'Cancel', onClick: 'closeModal()', classes: 'bg-gray-600 hover:bg-gray-700' },
        { label: 'Save', onClick: `saveTVConfigEdit(${configId})`, classes: 'bg-blue-600 hover:bg-blue-700' }
    ]);
}

/**
 * Save TV config edit
 */
async function saveTVConfigEdit(configId) {
    const data = {
        display_name: document.getElementById('edit-tv-display-name').value,
        media_player_entity_id: document.getElementById('edit-tv-media-player').value,
        remote_entity_id: document.getElementById('edit-tv-remote').value,
        enabled: document.getElementById('edit-tv-enabled').checked
    };

    try {
        const response = await fetch(`${ROOM_TV_API}/${configId}`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            throw new Error('Update failed');
        }

        safeShowToast('TV configuration updated', 'success');
        closeModal();
        loadTVConfigs();
    } catch (error) {
        console.error('Error updating TV config:', error);
        safeShowToast('Failed to update', 'error');
    }
}

// ============================================================================
// REMOTE CONTROL FUNCTIONS
// ============================================================================

/**
 * Get selected room from remote control
 */
function getSelectedRoom() {
    const select = document.getElementById('remote-room-select');
    return select ? select.value : null;
}

/**
 * Send remote command
 */
async function sendRemoteCommand(command) {
    const room = getSelectedRoom();
    if (!room) {
        safeShowToast('Select a room first', 'warning');
        return;
    }

    try {
        const response = await fetch(`${ROOM_TV_API}/control/${room}/remote?command=${command}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Command failed');
        }

        // Visual feedback
        const btn = event.target;
        btn.classList.add('ring-2', 'ring-blue-500');
        setTimeout(() => btn.classList.remove('ring-2', 'ring-blue-500'), 200);
    } catch (error) {
        console.error('Error sending remote command:', error);
        safeShowToast('Command failed', 'error');
    }
}

/**
 * Power control
 */
async function powerControl(action) {
    const room = getSelectedRoom();
    if (!room) {
        safeShowToast('Select a room first', 'warning');
        return;
    }

    try {
        const response = await fetch(`${ROOM_TV_API}/control/${room}/power?action=${action}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Power control failed');
        }

        safeShowToast(`Turned ${action}`, 'success');
    } catch (error) {
        console.error('Error with power control:', error);
        safeShowToast('Power control failed', 'error');
    }
}

/**
 * Test TV power from room cards
 */
async function testTVPower(roomName, action) {
    try {
        const response = await fetch(`${ROOM_TV_API}/control/${roomName}/power?action=${action}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Power control failed');
        }

        safeShowToast(`${roomName}: Turned ${action}`, 'success');
    } catch (error) {
        console.error('Error with power control:', error);
        safeShowToast('Power control failed', 'error');
    }
}

/**
 * Launch selected app
 */
async function launchSelectedApp() {
    const room = getSelectedRoom();
    const appSelect = document.getElementById('app-select');
    const appName = appSelect ? appSelect.value : null;

    if (!room || !appName) {
        safeShowToast('Select room and app', 'warning');
        return;
    }

    try {
        const response = await fetch(`${ROOM_TV_API}/control/${room}/launch?app_name=${encodeURIComponent(appName)}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Launch failed');
        }

        safeShowToast(`Launching ${appName}`, 'success');
    } catch (error) {
        console.error('Error launching app:', error);
        safeShowToast('Launch failed', 'error');
    }
}

/**
 * Show app launcher for a specific room
 */
function showAppLauncher(roomName) {
    const appOptions = allAppConfigs.filter(a => a.enabled).map(a =>
        `<option value="${escapeHtml(a.app_name)}">${escapeHtml(a.display_name)}</option>`
    ).join('');

    showModal(`Launch App on ${roomName}`, `
        <div class="space-y-4">
            <select id="launch-app-select" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white">
                ${appOptions}
            </select>
        </div>
    `, [
        { label: 'Cancel', onClick: 'closeModal()', classes: 'bg-gray-600 hover:bg-gray-700' },
        { label: 'Launch', onClick: `launchAppOnRoom('${roomName}')`, classes: 'bg-blue-600 hover:bg-blue-700' }
    ]);
}

/**
 * Launch app on specific room
 */
async function launchAppOnRoom(roomName) {
    const appSelect = document.getElementById('launch-app-select');
    const appName = appSelect ? appSelect.value : null;

    if (!appName) return;

    closeModal();

    try {
        const response = await fetch(`${ROOM_TV_API}/control/${roomName}/launch?app_name=${encodeURIComponent(appName)}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Launch failed');
        }

        safeShowToast(`Launching ${appName} on ${roomName}`, 'success');
    } catch (error) {
        console.error('Error launching app:', error);
        safeShowToast('Launch failed', 'error');
    }
}

/**
 * Refresh TV state
 */
async function refreshTVState() {
    const room = getSelectedRoom();
    if (!room) {
        safeShowToast('Select a room first', 'warning');
        return;
    }

    const stateContent = document.getElementById('tv-state-content');
    stateContent.innerHTML = '<div class="animate-pulse">Loading...</div>';

    try {
        const response = await fetch(`${ROOM_TV_API}/control/${room}/state`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to get state');
        }

        const state = await response.json();

        stateContent.innerHTML = `
            <div class="space-y-1">
                <p><span class="text-gray-500">State:</span> <span class="${state.state === 'playing' ? 'text-green-400' : 'text-white'}">${state.state}</span></p>
                ${state.app_name ? `<p><span class="text-gray-500">App:</span> ${escapeHtml(state.app_name)}</p>` : ''}
                ${state.media_title ? `<p><span class="text-gray-500">Title:</span> ${escapeHtml(state.media_title)}</p>` : ''}
                ${state.media_artist ? `<p><span class="text-gray-500">Artist:</span> ${escapeHtml(state.media_artist)}</p>` : ''}
            </div>
        `;
    } catch (error) {
        console.error('Error getting TV state:', error);
        stateContent.innerHTML = '<span class="text-red-400">Failed to get state</span>';
    }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

// Load TV configs when tab is shown
document.addEventListener('DOMContentLoaded', function() {
    // Will be called when showTab('room-tv') is invoked
});
