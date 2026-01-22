/**
 * Room Audio Configuration Management
 *
 * Manages audio playback configuration for multi-room voice assistant system.
 * Each room can have:
 * - Single speaker (one media_player entity)
 * - Stereo pair (two media_player entities for left/right)
 * - Speaker group (multiple media_player entities grouped together)
 *
 * Features:
 * - Dynamic entity discovery from Home Assistant
 * - Speaker type-specific configuration fields
 * - Audio test functionality
 * - Volume control per room
 * - Provider selection (music_assistant, Spotify, etc.)
 * - Radio mode toggle
 */

const ROOM_AUDIO_API = '/api/room-audio';
const ROOM_GROUPS_API_URL = '/api/room-groups';  // Renamed to avoid conflict with room-groups.js

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let allRoomConfigs = [];
let roomGroupsForAudio = [];  // Renamed to avoid conflict with room-groups.js
let availableEntities = [];
let entitiesLastFetched = null;
const ENTITIES_CACHE_DURATION = 300000; // 5 minutes

/**
 * Load all room audio configurations and room groups
 */
async function loadRoomConfigs() {
    try {
        showLoadingState('room-audio-container');

        // Load both room configs and room groups in parallel
        const [configResponse, groupsResponse] = await Promise.all([
            fetch(ROOM_AUDIO_API, { headers: getAuthHeaders() }),
            fetch(ROOM_GROUPS_API_URL, { headers: getAuthHeaders() })
        ]);

        if (!configResponse.ok) {
            throw new Error(`Failed to load room configs: ${configResponse.statusText}`);
        }

        allRoomConfigs = await configResponse.json();

        // Load room groups (don't fail if this errors)
        if (groupsResponse.ok) {
            roomGroupsForAudio = await groupsResponse.json();
        } else {
            console.warn('Failed to load room groups for inherited display');
            roomGroupsForAudio = [];
        }

        renderRoomConfigs(allRoomConfigs);
    } catch (error) {
        console.error('Error loading room configs:', error);
        safeShowToast('Failed to load room configurations', 'error');
        showError('room-audio-container', 'Failed to load room configurations');
    }
}

/**
 * Discover available media player entities from Home Assistant
 * @param {boolean} forceRefresh - Force refresh even if cache is valid
 */
async function discoverEntities(forceRefresh = false) {
    const now = Date.now();
    if (!forceRefresh && entitiesLastFetched && (now - entitiesLastFetched) < ENTITIES_CACHE_DURATION) {
        console.log('Using cached entities');
        return availableEntities;
    }

    try {
        safeShowToast('Discovering media players...', 'info');

        const response = await fetch(`${ROOM_AUDIO_API}/discover/entities`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to discover entities');
        }

        const data = await response.json();
        // Backend returns array directly, not wrapped in {entities: [...]}
        availableEntities = Array.isArray(data) ? data : (data.entities || []);
        entitiesLastFetched = now;

        safeShowToast(`Found ${availableEntities.length} media players`, 'success');
        return availableEntities;
    } catch (error) {
        console.error('Error discovering entities:', error);
        safeShowToast('Failed to discover media players', 'error');
        return [];
    }
}

/**
 * Show loading state in container
 */
function showLoadingState(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="animate-pulse">Loading room audio configurations...</div>
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
 * Render all room configurations
 */
function renderRoomConfigs(configs) {
    const container = document.getElementById('room-audio-container');

    // Get names of rooms that have explicit audio configs
    const configuredRoomNames = new Set(configs.map(c => c.room_name.toLowerCase()));

    // Build inherited rooms section from room groups
    const inheritedRoomsHtml = renderInheritedRooms(configuredRoomNames);

    if ((!configs || configs.length === 0) && !inheritedRoomsHtml) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-4xl mb-4">🔊</div>
                <p class="text-lg">No room audio configurations</p>
                <p class="text-sm mt-2">Add your first room to configure audio playback</p>
            </div>
        `;
        return;
    }

    const configsHtml = configs.map(config => renderRoomCard(config)).join('');
    container.innerHTML = configsHtml + inheritedRoomsHtml;
}

/**
 * Render inherited rooms section from room groups
 * Shows rooms that are part of room groups but don't have explicit audio configs
 */
function renderInheritedRooms(configuredRoomNames) {
    // Collect all rooms from enabled room groups
    const inheritedRooms = [];

    for (const group of roomGroupsForAudio) {
        if (!group.enabled) continue;

        const members = group.members || [];
        for (const member of members) {
            const roomName = member.room_name || '';
            // Only show if this room doesn't have an explicit audio config
            if (!configuredRoomNames.has(roomName.toLowerCase())) {
                inheritedRooms.push({
                    room_name: roomName,
                    display_name: member.display_name || roomName.replace(/_/g, ' '),
                    group_name: group.name,
                    group_display_name: group.display_name
                });
            }
        }
    }

    if (inheritedRooms.length === 0) {
        return '';
    }

    // Group inherited rooms by their room group
    const roomsByGroup = {};
    for (const room of inheritedRooms) {
        if (!roomsByGroup[room.group_name]) {
            roomsByGroup[room.group_name] = {
                display_name: room.group_display_name,
                rooms: []
            };
        }
        roomsByGroup[room.group_name].rooms.push(room);
    }

    const groupCardsHtml = Object.entries(roomsByGroup).map(([groupName, groupData]) => {
        const roomsHtml = groupData.rooms.map(room => `
            <div class="flex items-center justify-between bg-gray-800/30 rounded px-3 py-2">
                <div class="flex items-center gap-2">
                    <span class="text-gray-400">🔗</span>
                    <span class="text-gray-300">${escapeHtml(room.display_name)}</span>
                    <span class="text-gray-500 text-xs font-mono">(${escapeHtml(room.room_name)})</span>
                </div>
                <span class="text-xs text-gray-500">No explicit config</span>
            </div>
        `).join('');

        return `
            <div class="bg-gray-800/20 border border-gray-700/50 rounded-lg p-4 mb-3">
                <div class="flex items-center gap-2 mb-3">
                    <span class="text-gray-400">🏠</span>
                    <h4 class="text-sm font-medium text-gray-400">From "${escapeHtml(groupData.display_name)}" group</h4>
                </div>
                <div class="space-y-2">
                    ${roomsHtml}
                </div>
            </div>
        `;
    }).join('');

    return `
        <div class="mt-6 pt-6 border-t border-gray-700">
            <div class="flex items-center gap-2 mb-4">
                <span class="text-lg">📋</span>
                <h3 class="text-lg font-semibold text-gray-400">Inherited from Room Groups</h3>
                <span class="px-2 py-0.5 text-xs bg-gray-700 text-gray-400 rounded">Read-only</span>
            </div>
            <p class="text-sm text-gray-500 mb-4">
                These rooms are included via Room Groups but don't have explicit audio configurations.
                Music commands for these rooms will use default settings.
                <a href="#" onclick="switchTab('room-groups'); return false;" class="text-blue-400 hover:text-blue-300">Manage Room Groups →</a>
            </p>
            ${groupCardsHtml}
        </div>
    `;
}

/**
 * Render individual room configuration card
 */
function renderRoomCard(config) {
    const speakerTypeLabel = getSpeakerTypeLabel(config.speaker_type);
    const statusBadge = config.enabled
        ? '<span class="px-2 py-1 text-xs rounded bg-green-900/50 text-green-400">Enabled</span>'
        : '<span class="px-2 py-1 text-xs rounded bg-red-900/50 text-red-400">Disabled</span>';

    const lastTestedText = config.last_tested_at
        ? formatDate(config.last_tested_at)
        : 'Never tested';

    const testStatusIcon = getTestStatusIcon(config.last_test_success);

    // Build entity list based on speaker type
    let entityListHtml = '';
    if (config.speaker_type === 'single') {
        entityListHtml = `<p class="text-gray-300 font-mono text-sm">${escapeHtml(config.primary_entity_id)}</p>`;
    } else if (config.speaker_type === 'stereo_pair') {
        entityListHtml = `
            <p class="text-gray-300 font-mono text-sm">L: ${escapeHtml(config.primary_entity_id)}</p>
            <p class="text-gray-300 font-mono text-sm">R: ${escapeHtml(config.secondary_entity_id || 'N/A')}</p>
        `;
    } else if (config.speaker_type === 'group') {
        const members = config.group_entity_ids || [];
        entityListHtml = members.map(m => `<p class="text-gray-300 font-mono text-sm">• ${escapeHtml(m)}</p>`).join('');
    }

    return `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 mb-4">
            <div class="flex justify-between items-start mb-4">
                <div class="flex items-start gap-4">
                    <div class="text-3xl">🔊</div>
                    <div>
                        <div class="flex items-center gap-3">
                            <h3 class="text-xl font-semibold text-white">${escapeHtml(config.display_name || config.room_name)}</h3>
                            ${statusBadge}
                        </div>
                        <p class="text-gray-500 text-sm mt-1">${speakerTypeLabel}</p>
                    </div>
                </div>
                <div class="flex gap-2">
                    <button onclick="testRoomAudio(${config.id})" class="px-3 py-1 bg-purple-600 hover:bg-purple-700 text-white rounded text-sm" title="Test Audio">
                        Test
                    </button>
                    <button onclick="editRoomConfig(${config.id})" class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm">
                        Edit
                    </button>
                    <button onclick="deleteRoomConfig(${config.id}, '${escapeHtml(config.room_name)}')" class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm">
                        Delete
                    </button>
                </div>
            </div>

            <!-- Entity Display -->
            <div class="mb-4 bg-gray-800/50 rounded px-3 py-2">
                <label class="text-xs text-gray-500">Entities</label>
                ${entityListHtml}
            </div>

            <!-- Stats Row -->
            <div class="grid grid-cols-4 gap-4 text-sm">
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Volume</label>
                    <p class="text-white">${Math.round((config.default_volume || 0.5) * 100)}%</p>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Provider</label>
                    <p class="text-white">${escapeHtml(config.preferred_provider || 'Default')}</p>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Radio Mode</label>
                    <p class="text-white">${config.use_radio_mode ? 'On' : 'Off'}</p>
                </div>
                <div class="bg-gray-800/50 rounded p-3">
                    <label class="text-xs text-gray-500 block">Last Test</label>
                    <p class="text-white flex items-center gap-1">
                        ${testStatusIcon} ${lastTestedText}
                    </p>
                </div>
            </div>
        </div>
    `;
}

/**
 * Get speaker type label
 */
function getSpeakerTypeLabel(type) {
    const labels = {
        'single': 'Single Speaker',
        'stereo_pair': 'Stereo Pair',
        'group': 'Speaker Group'
    };
    return labels[type] || type;
}

/**
 * Get test status icon
 */
function getTestStatusIcon(success) {
    if (success === null || success === undefined) return '⏺';
    return success ? '✅' : '❌';
}

/**
 * Format date
 */
function formatDate(isoString) {
    if (!isoString) return 'Never';
    const date = new Date(isoString);
    return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// ============================================================================
// MODAL FUNCTIONS
// ============================================================================

/**
 * Show create room modal
 */
async function showCreateRoomModal() {
    // Ensure entities are loaded
    await discoverEntities();

    const modal = document.createElement('div');
    modal.id = 'room-audio-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';

    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
            <h3 class="text-xl font-semibold text-white mb-4">Add Room Audio Configuration</h3>

            <div class="space-y-4">
                <!-- Room Name -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Room Name *</label>
                    <select id="room-name-select" onchange="handleRoomNameChange()"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <option value="">Select a room...</option>
                        <option value="office">Office</option>
                        <option value="kitchen">Kitchen</option>
                        <option value="living_room">Living Room</option>
                        <option value="master_bedroom">Master Bedroom</option>
                        <option value="master_bath">Master Bath</option>
                        <option value="main_bath">Main Bath</option>
                        <option value="alpha">Alpha (Guest Room 1)</option>
                        <option value="beta">Beta (Guest Room 2)</option>
                        <option value="basement_bath">Basement Bath</option>
                        <option value="dining_room">Dining Room</option>
                        <option value="custom">Custom...</option>
                    </select>
                    <input type="text" id="room-name-custom" placeholder="Enter custom room name" style="display: none;"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white mt-2 focus:outline-none focus:border-blue-500">
                </div>

                <!-- Display Name -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Display Name</label>
                    <input type="text" id="room-display-name" placeholder="Office (optional)"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-blue-500">
                    <p class="text-xs text-gray-500 mt-1">Friendly name for display (defaults to room name)</p>
                </div>

                <!-- Speaker Type -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Speaker Type *</label>
                    <div class="space-y-2">
                        <label class="flex items-center gap-2">
                            <input type="radio" name="speaker-type" value="single" checked onchange="handleSpeakerTypeChange()"
                                class="w-4 h-4 text-blue-600 bg-gray-800 border-gray-700">
                            <span class="text-white">Single Speaker</span>
                        </label>
                        <label class="flex items-center gap-2">
                            <input type="radio" name="speaker-type" value="stereo_pair" onchange="handleSpeakerTypeChange()"
                                class="w-4 h-4 text-blue-600 bg-gray-800 border-gray-700">
                            <span class="text-white">Stereo Pair (L/R)</span>
                        </label>
                        <label class="flex items-center gap-2">
                            <input type="radio" name="speaker-type" value="group" onchange="handleSpeakerTypeChange()"
                                class="w-4 h-4 text-blue-600 bg-gray-800 border-gray-700">
                            <span class="text-white">Speaker Group</span>
                        </label>
                    </div>
                </div>

                <!-- Primary Speaker (always visible) -->
                <div id="primary-speaker-field">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Primary Speaker Entity *</label>
                    ${renderEntitySelect('primary-speaker-entity', availableEntities)}
                </div>

                <!-- Secondary Speaker (stereo pair only) -->
                <div id="secondary-speaker-field" style="display: none;">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Secondary Speaker Entity (Right) *</label>
                    ${renderEntitySelect('secondary-speaker-entity', availableEntities)}
                </div>

                <!-- Group Members (group only) -->
                <div id="group-members-field" style="display: none;">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Group Members *</label>
                    <div id="group-members-container" class="space-y-2">
                        <div class="flex gap-2">
                            ${renderEntitySelect('group-member-0', availableEntities)}
                            <button onclick="removeGroupMember(0)" class="px-3 py-2 bg-red-600 hover:bg-red-700 text-white rounded">Remove</button>
                        </div>
                    </div>
                    <button onclick="addGroupMember()" class="mt-2 px-3 py-2 bg-green-600 hover:bg-green-700 text-white rounded text-sm">
                        + Add Member
                    </button>
                </div>

                <!-- Volume -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Volume Level: <span id="volume-display">50</span>%</label>
                    <input type="range" id="room-volume" min="0" max="100" value="50" oninput="document.getElementById('volume-display').textContent = this.value"
                        class="w-full h-2 bg-gray-700 rounded-lg appearance-none cursor-pointer slider">
                </div>

                <!-- Provider -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Provider</label>
                    <select id="room-provider"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <option value="">Default</option>
                        <option value="music_assistant">Music Assistant</option>
                        <option value="spotify">Spotify</option>
                        <option value="apple_music">Apple Music</option>
                        <option value="youtube_music">YouTube Music</option>
                    </select>
                </div>

                <!-- Radio Mode -->
                <div class="flex items-center gap-2">
                    <input type="checkbox" id="room-radio-mode"
                        class="w-4 h-4 bg-gray-800 border-gray-700 rounded text-blue-600 focus:ring-blue-500">
                    <label for="room-radio-mode" class="text-sm text-gray-400">Radio Mode (continuous playback)</label>
                </div>

                <!-- Enable -->
                <div class="flex items-center gap-2">
                    <input type="checkbox" id="room-enabled" checked
                        class="w-4 h-4 bg-gray-800 border-gray-700 rounded text-blue-600 focus:ring-blue-500">
                    <label for="room-enabled" class="text-sm text-gray-400">Enabled</label>
                </div>
            </div>

            <div class="flex justify-between mt-6">
                <button onclick="refreshEntities()" class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm flex items-center gap-2">
                    <i data-lucide="refresh-cw" class="w-4 h-4"></i> Refresh Entities
                </button>
                <div class="flex gap-3">
                    <button onclick="closeRoomAudioModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="createRoomConfig()" class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm">
                        Create Configuration
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeRoomAudioModal();
    });
}

/**
 * Show edit room modal
 */
async function showEditRoomModal(config) {
    // Ensure entities are loaded
    await discoverEntities();

    const modal = document.createElement('div');
    modal.id = 'room-audio-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';

    const groupMembersHtml = config.speaker_type === 'group' && config.group_entity_ids
        ? config.group_entity_ids.map((member, idx) => `
            <div class="flex gap-2 mb-2" id="group-member-row-${idx}">
                ${renderEntitySelect(`group-member-${idx}`, availableEntities, member)}
                <button onclick="removeGroupMember(${idx})" class="px-3 py-2 bg-red-600 hover:bg-red-700 text-white rounded">Remove</button>
            </div>
        `).join('')
        : `<div class="flex gap-2">
            ${renderEntitySelect('group-member-0', availableEntities)}
            <button onclick="removeGroupMember(0)" class="px-3 py-2 bg-red-600 hover:bg-red-700 text-white rounded">Remove</button>
        </div>`;

    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
            <h3 class="text-xl font-semibold text-white mb-4">Edit Room Audio Configuration</h3>

            <input type="hidden" id="edit-room-id" value="${config.id}">

            <div class="space-y-4">
                <!-- Room Name (read-only when editing) -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Room Name</label>
                    <input type="text" id="room-name-readonly" value="${escapeHtml(config.room_name)}" readonly
                        class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-gray-400 cursor-not-allowed">
                    <p class="text-xs text-gray-500 mt-1">Room name cannot be changed after creation</p>
                </div>

                <!-- Display Name -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Display Name</label>
                    <input type="text" id="room-display-name" value="${escapeHtml(config.display_name || '')}"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                </div>

                <!-- Speaker Type -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Speaker Type *</label>
                    <div class="space-y-2">
                        <label class="flex items-center gap-2">
                            <input type="radio" name="speaker-type" value="single" ${config.speaker_type === 'single' ? 'checked' : ''} onchange="handleSpeakerTypeChange()"
                                class="w-4 h-4 text-blue-600 bg-gray-800 border-gray-700">
                            <span class="text-white">Single Speaker</span>
                        </label>
                        <label class="flex items-center gap-2">
                            <input type="radio" name="speaker-type" value="stereo_pair" ${config.speaker_type === 'stereo_pair' ? 'checked' : ''} onchange="handleSpeakerTypeChange()"
                                class="w-4 h-4 text-blue-600 bg-gray-800 border-gray-700">
                            <span class="text-white">Stereo Pair (L/R)</span>
                        </label>
                        <label class="flex items-center gap-2">
                            <input type="radio" name="speaker-type" value="group" ${config.speaker_type === 'group' ? 'checked' : ''} onchange="handleSpeakerTypeChange()"
                                class="w-4 h-4 text-blue-600 bg-gray-800 border-gray-700">
                            <span class="text-white">Speaker Group</span>
                        </label>
                    </div>
                </div>

                <!-- Primary Speaker -->
                <div id="primary-speaker-field">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Primary Speaker Entity *</label>
                    ${renderEntitySelect('primary-speaker-entity', availableEntities, config.primary_entity_id)}
                </div>

                <!-- Secondary Speaker -->
                <div id="secondary-speaker-field" style="display: ${config.speaker_type === 'stereo_pair' ? 'block' : 'none'};">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Secondary Speaker Entity (Right) *</label>
                    ${renderEntitySelect('secondary-speaker-entity', availableEntities, config.secondary_entity_id)}
                </div>

                <!-- Group Members -->
                <div id="group-members-field" style="display: ${config.speaker_type === 'group' ? 'block' : 'none'};">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Group Members *</label>
                    <div id="group-members-container" class="space-y-2">
                        ${groupMembersHtml}
                    </div>
                    <button onclick="addGroupMember()" class="mt-2 px-3 py-2 bg-green-600 hover:bg-green-700 text-white rounded text-sm">
                        + Add Member
                    </button>
                </div>

                <!-- Volume -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Volume Level: <span id="volume-display">${config.default_volume}</span>%</label>
                    <input type="range" id="room-volume" min="0" max="100" value="${config.default_volume}" oninput="document.getElementById('volume-display').textContent = this.value"
                        class="w-full h-2 bg-gray-700 rounded-lg appearance-none cursor-pointer slider">
                </div>

                <!-- Provider -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Provider</label>
                    <select id="room-provider"
                        class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
                        <option value="" ${!config.preferred_provider ? 'selected' : ''}>Default</option>
                        <option value="music_assistant" ${config.preferred_provider === 'music_assistant' ? 'selected' : ''}>Music Assistant</option>
                        <option value="spotify" ${config.preferred_provider === 'spotify' ? 'selected' : ''}>Spotify</option>
                        <option value="apple_music" ${config.preferred_provider === 'apple_music' ? 'selected' : ''}>Apple Music</option>
                        <option value="youtube_music" ${config.preferred_provider === 'youtube_music' ? 'selected' : ''}>YouTube Music</option>
                    </select>
                </div>

                <!-- Radio Mode -->
                <div class="flex items-center gap-2">
                    <input type="checkbox" id="room-radio-mode" ${config.use_radio_mode ? 'checked' : ''}
                        class="w-4 h-4 bg-gray-800 border-gray-700 rounded text-blue-600 focus:ring-blue-500">
                    <label for="room-radio-mode" class="text-sm text-gray-400">Radio Mode (continuous playback)</label>
                </div>

                <!-- Enable -->
                <div class="flex items-center gap-2">
                    <input type="checkbox" id="room-enabled" ${config.enabled ? 'checked' : ''}
                        class="w-4 h-4 bg-gray-800 border-gray-700 rounded text-blue-600 focus:ring-blue-500">
                    <label for="room-enabled" class="text-sm text-gray-400">Enabled</label>
                </div>
            </div>

            <div class="flex justify-between mt-6">
                <button onclick="refreshEntities()" class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm flex items-center gap-2">
                    <i data-lucide="refresh-cw" class="w-4 h-4"></i> Refresh Entities
                </button>
                <div class="flex gap-3">
                    <button onclick="closeRoomAudioModal()" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm">
                        Cancel
                    </button>
                    <button onclick="updateRoomConfig()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                        Update Configuration
                    </button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeRoomAudioModal();
    });
}

/**
 * Render entity select dropdown
 */
function renderEntitySelect(id, entities, selectedValue = '') {
    return `
        <select id="${id}" class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500">
            <option value="">Select entity...</option>
            ${entities.map(e => `
                <option value="${escapeHtml(e.entity_id)}" ${e.entity_id === selectedValue ? 'selected' : ''}>
                    ${escapeHtml(e.friendly_name || e.entity_id)}
                </option>
            `).join('')}
        </select>
    `;
}

/**
 * Close room audio modal
 */
function closeRoomAudioModal() {
    const modal = document.getElementById('room-audio-modal');
    if (modal) modal.remove();
}

// ============================================================================
// MODAL EVENT HANDLERS
// ============================================================================

/**
 * Handle room name selection change
 */
function handleRoomNameChange() {
    const select = document.getElementById('room-name-select');
    const customInput = document.getElementById('room-name-custom');

    if (select.value === 'custom') {
        customInput.style.display = 'block';
    } else {
        customInput.style.display = 'none';
    }
}

/**
 * Handle speaker type change
 */
function handleSpeakerTypeChange() {
    const speakerType = document.querySelector('input[name="speaker-type"]:checked').value;

    const primaryField = document.getElementById('primary-speaker-field');
    const secondaryField = document.getElementById('secondary-speaker-field');
    const groupField = document.getElementById('group-members-field');

    // Show/hide fields based on speaker type
    primaryField.style.display = speakerType === 'group' ? 'none' : 'block';
    secondaryField.style.display = speakerType === 'stereo_pair' ? 'block' : 'none';
    groupField.style.display = speakerType === 'group' ? 'block' : 'none';
}

/**
 * Add group member field
 */
function addGroupMember() {
    const container = document.getElementById('group-members-container');
    const memberCount = container.querySelectorAll('select[id^="group-member-"]').length;

    const newRow = document.createElement('div');
    newRow.className = 'flex gap-2 mb-2';
    newRow.id = `group-member-row-${memberCount}`;
    newRow.innerHTML = `
        ${renderEntitySelect(`group-member-${memberCount}`, availableEntities)}
        <button onclick="removeGroupMember(${memberCount})" class="px-3 py-2 bg-red-600 hover:bg-red-700 text-white rounded">Remove</button>
    `;

    container.appendChild(newRow);
}

/**
 * Remove group member field
 */
function removeGroupMember(index) {
    const row = document.getElementById(`group-member-row-${index}`);
    if (row) {
        row.remove();
    }
}

/**
 * Refresh entities from Home Assistant
 */
async function refreshEntities() {
    await discoverEntities(true);

    // Refresh entity selects in modal
    const primarySelect = document.getElementById('primary-speaker-entity');
    const secondarySelect = document.getElementById('secondary-speaker-entity');

    if (primarySelect) {
        const currentValue = primarySelect.value;
        primarySelect.innerHTML = renderEntityOptions(availableEntities, currentValue);
    }

    if (secondarySelect) {
        const currentValue = secondarySelect.value;
        secondarySelect.innerHTML = renderEntityOptions(availableEntities, currentValue);
    }

    // Refresh group member selects
    const groupSelects = document.querySelectorAll('select[id^="group-member-"]');
    groupSelects.forEach(select => {
        const currentValue = select.value;
        select.innerHTML = renderEntityOptions(availableEntities, currentValue);
    });
}

/**
 * Render entity options for select
 */
function renderEntityOptions(entities, selectedValue = '') {
    return `
        <option value="">Select entity...</option>
        ${entities.map(e => `
            <option value="${escapeHtml(e.entity_id)}" ${e.entity_id === selectedValue ? 'selected' : ''}>
                ${escapeHtml(e.friendly_name || e.entity_id)}
            </option>
        `).join('')}
    `;
}

// ============================================================================
// CRUD OPERATIONS
// ============================================================================

/**
 * Create new room configuration
 */
async function createRoomConfig() {
    const roomNameSelect = document.getElementById('room-name-select');
    const roomNameCustom = document.getElementById('room-name-custom');
    const roomName = roomNameSelect.value === 'custom' ? roomNameCustom.value.trim() : roomNameSelect.value;
    const displayName = document.getElementById('room-display-name').value.trim();
    const speakerType = document.querySelector('input[name="speaker-type"]:checked').value;
    const volume = parseInt(document.getElementById('room-volume').value);
    const provider = document.getElementById('room-provider').value || null;
    const radioMode = document.getElementById('room-radio-mode').checked;
    const enabled = document.getElementById('room-enabled').checked;

    if (!roomName) {
        safeShowToast('Room name is required', 'error');
        return;
    }

    // Build entity configuration based on speaker type
    let primaryEntity = null;
    let secondaryEntity = null;
    let groupMembers = null;

    if (speakerType === 'single') {
        primaryEntity = document.getElementById('primary-speaker-entity').value;
        if (!primaryEntity) {
            safeShowToast('Primary speaker entity is required', 'error');
            return;
        }
    } else if (speakerType === 'stereo_pair') {
        primaryEntity = document.getElementById('primary-speaker-entity').value;
        secondaryEntity = document.getElementById('secondary-speaker-entity').value;
        if (!primaryEntity || !secondaryEntity) {
            safeShowToast('Both primary and secondary speaker entities are required for stereo pair', 'error');
            return;
        }
    } else if (speakerType === 'group') {
        groupMembers = [];
        const memberSelects = document.querySelectorAll('select[id^="group-member-"]');
        memberSelects.forEach(select => {
            const value = select.value;
            if (value) groupMembers.push(value);
        });
        if (groupMembers.length === 0) {
            safeShowToast('At least one group member is required', 'error');
            return;
        }
    }

    try {
        const response = await fetch(ROOM_AUDIO_API, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                room_name: roomName,
                display_name: displayName || null,
                speaker_type: speakerType,
                primary_entity_id: primaryEntity,
                secondary_entity_id: secondaryEntity,
                group_entity_ids: groupMembers,
                default_volume: volume / 100,  // Convert percentage to 0-1 range
                preferred_provider: provider,
                use_radio_mode: radioMode,
                enabled: enabled
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create room configuration');
        }

        safeShowToast('Room configuration created successfully', 'success');
        closeRoomAudioModal();
        loadRoomConfigs();
    } catch (error) {
        console.error('Error creating room config:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Edit room configuration
 */
async function editRoomConfig(roomId) {
    const config = allRoomConfigs.find(c => c.id === roomId);
    if (!config) {
        safeShowToast('Room configuration not found', 'error');
        return;
    }

    // Fetch full config with entities
    try {
        const response = await fetch(`${ROOM_AUDIO_API}/${roomId}`, {
            headers: getAuthHeaders()
        });
        if (response.ok) {
            const fullConfig = await response.json();
            showEditRoomModal(fullConfig);
        } else {
            showEditRoomModal(config);
        }
    } catch (error) {
        showEditRoomModal(config);
    }
}

/**
 * Update room configuration
 */
async function updateRoomConfig() {
    const roomId = document.getElementById('edit-room-id').value;
    const displayName = document.getElementById('room-display-name').value.trim();
    const speakerType = document.querySelector('input[name="speaker-type"]:checked').value;
    const volume = parseInt(document.getElementById('room-volume').value);
    const provider = document.getElementById('room-provider').value || null;
    const radioMode = document.getElementById('room-radio-mode').checked;
    const enabled = document.getElementById('room-enabled').checked;

    // Build entity configuration
    let primaryEntity = null;
    let secondaryEntity = null;
    let groupMembers = null;

    if (speakerType === 'single') {
        primaryEntity = document.getElementById('primary-speaker-entity').value;
        if (!primaryEntity) {
            safeShowToast('Primary speaker entity is required', 'error');
            return;
        }
    } else if (speakerType === 'stereo_pair') {
        primaryEntity = document.getElementById('primary-speaker-entity').value;
        secondaryEntity = document.getElementById('secondary-speaker-entity').value;
        if (!primaryEntity || !secondaryEntity) {
            safeShowToast('Both primary and secondary speaker entities are required for stereo pair', 'error');
            return;
        }
    } else if (speakerType === 'group') {
        groupMembers = [];
        const memberSelects = document.querySelectorAll('select[id^="group-member-"]');
        memberSelects.forEach(select => {
            const value = select.value;
            if (value) groupMembers.push(value);
        });
        if (groupMembers.length === 0) {
            safeShowToast('At least one group member is required', 'error');
            return;
        }
    }

    try {
        const response = await fetch(`${ROOM_AUDIO_API}/${roomId}`, {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                display_name: displayName || null,
                speaker_type: speakerType,
                primary_entity_id: primaryEntity,
                secondary_entity_id: secondaryEntity,
                group_entity_ids: groupMembers,
                default_volume: volume / 100,  // Convert percentage to 0-1 range
                preferred_provider: provider,
                use_radio_mode: radioMode,
                enabled: enabled
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update room configuration');
        }

        safeShowToast('Room configuration updated successfully', 'success');
        closeRoomAudioModal();
        loadRoomConfigs();
    } catch (error) {
        console.error('Error updating room config:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Delete room configuration
 */
async function deleteRoomConfig(roomId, roomName) {
    if (!confirm(`Are you sure you want to delete the audio configuration for "${roomName}"?`)) {
        return;
    }

    try {
        const response = await fetch(`${ROOM_AUDIO_API}/${roomId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to delete room configuration');
        }

        safeShowToast('Room configuration deleted', 'success');
        loadRoomConfigs();
    } catch (error) {
        console.error('Error deleting room config:', error);
        safeShowToast(error.message, 'error');
    }
}

// ============================================================================
// TEST OPERATIONS
// ============================================================================

/**
 * Test room audio playback
 */
async function testRoomAudio(roomId) {
    safeShowToast('Testing audio playback...', 'info');

    try {
        const response = await fetch(`${ROOM_AUDIO_API}/${roomId}/test`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        const result = await response.json();

        if (result.success) {
            safeShowToast(`Audio test successful: ${result.message}`, 'success');
        } else {
            safeShowToast(`Audio test failed: ${result.message}`, 'error');
        }

        // Reload to update test status
        loadRoomConfigs();
    } catch (error) {
        console.error('Error testing audio:', error);
        safeShowToast('Failed to test audio playback', 'error');
    }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

/**
 * Initialize room audio page
 */
function initRoomAudioPage() {
    console.log('Initializing Room Audio page');

    // Load room configurations
    loadRoomConfigs();

    // Pre-cache entities for faster modal loading
    discoverEntities();
}

// Export functions for external use
if (typeof window !== 'undefined') {
    window.initRoomAudioPage = initRoomAudioPage;
    window.showCreateRoomModal = showCreateRoomModal;
    window.editRoomConfig = editRoomConfig;
    window.deleteRoomConfig = deleteRoomConfig;
    window.testRoomAudio = testRoomAudio;
    window.handleRoomNameChange = handleRoomNameChange;
    window.handleSpeakerTypeChange = handleSpeakerTypeChange;
    window.addGroupMember = addGroupMember;
    window.removeGroupMember = removeGroupMember;
    window.refreshEntities = refreshEntities;
    window.createRoomConfig = createRoomConfig;
    window.updateRoomConfig = updateRoomConfig;
    window.closeRoomAudioModal = closeRoomAudioModal;
}
