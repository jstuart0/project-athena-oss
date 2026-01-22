/**
 * Music Configuration UI - Redesigned
 *
 * Modern, visually appealing music configuration management:
 * - Music feature toggle with visual status
 * - Music Assistant connection with live status
 * - Provider accounts (Spotify, Apple Music, etc.)
 * - Playback defaults with visual controls
 * - Genre seed artists with card-based management
 */

let musicConfig = null;
let musicFeature = null;
let genres = [];
let editingGenre = null;

// ============================================================================
// DATA LOADING
// ============================================================================

async function loadMusicConfig() {
    try {
        const response = await fetch('/api/music-config', {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load music config: ${response.statusText}`);
        }

        musicConfig = await response.json();

        await Promise.all([
            loadGenres(),
            loadMusicFeature()
        ]);

        renderMusicConfig();
    } catch (error) {
        console.error('Failed to load music config:', error);
        safeShowToast('Failed to load music configuration', 'error');
        showMusicConfigError(error.message);
    }
}

async function loadMusicFeature() {
    try {
        const response = await fetch('/api/features', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            const features = await response.json();
            musicFeature = features.find(f => f.name === 'music_playback');
        }
    } catch (error) {
        console.error('Failed to load music feature:', error);
    }
}

async function loadGenres() {
    try {
        const response = await fetch('/api/music-config/genres', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            const data = await response.json();
            const genresDict = data.genres || {};
            genres = Object.entries(genresDict).map(([name, artists]) => ({
                genre_name: name,
                artists: artists || []
            }));
            genres.sort((a, b) => a.genre_name.localeCompare(b.genre_name));
        } else {
            genres = [];
        }
    } catch (error) {
        console.error('Failed to load genres:', error);
        genres = [];
    }
}

// ============================================================================
// MAIN RENDER
// ============================================================================

function renderMusicConfig() {
    const container = document.getElementById('music-config-container');
    if (!container) return;

    if (!musicConfig) {
        container.innerHTML = `
            <div class="flex items-center justify-center h-64 text-gray-400">
                <div class="text-center">
                    <div class="text-5xl mb-4">🎵</div>
                    <p>Music configuration not available</p>
                </div>
            </div>
        `;
        return;
    }

    const isEnabled = musicFeature?.enabled ?? false;
    const connectionStatus = musicConfig.connection_status || 'unknown';

    container.innerHTML = `
        <!-- Hero Header -->
        <div class="mb-8">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-14 h-14 rounded-2xl bg-gradient-to-br from-green-500 to-emerald-600 flex items-center justify-center shadow-lg shadow-green-500/20">
                        <span class="text-2xl">🎵</span>
                    </div>
                    <div>
                        <h2 class="text-2xl font-bold text-white">Music Configuration</h2>
                        <p class="text-gray-400 text-sm">Manage Music Assistant, providers, and playback settings</p>
                    </div>
                </div>
                <div class="flex items-center gap-3">
                    ${renderMasterToggle(isEnabled)}
                </div>
            </div>
        </div>

        <!-- Status Cards Row -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
            ${renderStatusCard('Connection', connectionStatus === 'connected' ? 'Connected' : 'Disconnected', connectionStatus === 'connected' ? 'green' : 'red', '🔗')}
            ${renderStatusCard('Provider', musicConfig.default_provider || 'Spotify', 'blue', '🎧')}
            ${renderStatusCard('Volume', `${musicConfig.default_volume ?? 50}%`, 'purple', '🔊')}
        </div>

        <!-- Main Content Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            ${renderConnectionCard()}
            ${renderPlaybackCard()}
        </div>

        <!-- Accounts Section -->
        ${renderAccountsSection()}

        <!-- Genre Seeds Section -->
        ${renderGenresSection()}

        <!-- Advanced Settings (Collapsible) -->
        ${renderAdvancedSettings()}
    `;

    // Initialize any interactive elements
    initVolumeSlider();
}

// ============================================================================
// COMPONENT RENDERS
// ============================================================================

function renderMasterToggle(isEnabled) {
    return `
        <div class="flex items-center gap-3 px-4 py-2 rounded-xl ${isEnabled ? 'bg-green-500/10 border border-green-500/30' : 'bg-gray-800 border border-gray-700'}">
            <span class="text-sm ${isEnabled ? 'text-green-400' : 'text-gray-400'}">${isEnabled ? 'Music Enabled' : 'Music Disabled'}</span>
            <button onclick="toggleMusicFeature()"
                class="relative w-12 h-6 rounded-full transition-all duration-300 ${isEnabled ? 'bg-green-500' : 'bg-gray-600'}">
                <div class="absolute top-1 ${isEnabled ? 'right-1' : 'left-1'} w-4 h-4 bg-white rounded-full shadow-md transition-all duration-300"></div>
            </button>
        </div>
    `;
}

function renderStatusCard(label, value, color, icon) {
    const colors = {
        green: 'from-green-500/20 to-green-600/10 border-green-500/30 text-green-400',
        red: 'from-red-500/20 to-red-600/10 border-red-500/30 text-red-400',
        blue: 'from-blue-500/20 to-blue-600/10 border-blue-500/30 text-blue-400',
        purple: 'from-purple-500/20 to-purple-600/10 border-purple-500/30 text-purple-400',
    };

    return `
        <div class="bg-gradient-to-br ${colors[color]} border rounded-xl p-4">
            <div class="flex items-center gap-3">
                <span class="text-2xl">${icon}</span>
                <div>
                    <div class="text-xs text-gray-400 uppercase tracking-wider">${label}</div>
                    <div class="text-lg font-semibold ${colors[color].split(' ').pop()}">${escapeHtml(value)}</div>
                </div>
            </div>
        </div>
    `;
}

function renderConnectionCard() {
    const url = musicConfig.music_assistant_url || '';
    const status = musicConfig.connection_status || 'unknown';
    const isConnected = status === 'connected';

    return `
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <div class="px-5 py-4 border-b border-dark-border flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
                        <span class="text-xl">🎛️</span>
                    </div>
                    <div>
                        <h3 class="font-semibold text-white">Music Assistant</h3>
                        <p class="text-xs text-gray-400">Server connection settings</p>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <div class="w-2 h-2 rounded-full ${isConnected ? 'bg-green-400 animate-pulse' : 'bg-red-400'}"></div>
                    <span class="text-xs ${isConnected ? 'text-green-400' : 'text-red-400'}">${isConnected ? 'Connected' : 'Disconnected'}</span>
                </div>
            </div>
            <div class="p-5">
                <div class="mb-4">
                    <label class="block text-sm text-gray-400 mb-2">Server URL</label>
                    <div class="flex gap-2">
                        <input
                            type="url"
                            id="music-assistant-url"
                            value="${escapeHtml(url)}"
                            placeholder="http://localhost:8095"
                            class="flex-1 px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-white placeholder-gray-500 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none transition-all"
                        />
                        <button onclick="testConnection()"
                            class="px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-gray-300 hover:bg-gray-800 hover:border-gray-600 transition-all flex items-center gap-2">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>
                            </svg>
                            Test
                        </button>
                    </div>
                </div>
                <button onclick="saveConnectionSettings()"
                    class="w-full py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-lg transition-colors">
                    Save Connection
                </button>
            </div>
        </div>
    `;
}

function renderPlaybackCard() {
    const volume = musicConfig.default_volume ?? 50;
    const radioMode = musicConfig.default_radio_mode ?? false;
    const provider = musicConfig.default_provider || 'spotify';

    const providers = [
        { id: 'spotify', name: 'Spotify', icon: '💚' },
        { id: 'apple_music', name: 'Apple Music', icon: '🍎' },
        { id: 'youtube_music', name: 'YouTube Music', icon: '📺' },
        { id: 'tidal', name: 'Tidal', icon: '🌊' },
        { id: 'local', name: 'Local Library', icon: '📁' },
    ];

    return `
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <div class="px-5 py-4 border-b border-dark-border">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
                        <span class="text-xl">🎚️</span>
                    </div>
                    <div>
                        <h3 class="font-semibold text-white">Playback Defaults</h3>
                        <p class="text-xs text-gray-400">Default volume and provider settings</p>
                    </div>
                </div>
            </div>
            <div class="p-5 space-y-5">
                <!-- Volume Slider -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <label class="text-sm text-gray-400">Default Volume</label>
                        <span id="volume-display" class="text-sm font-mono text-purple-400">${volume}%</span>
                    </div>
                    <div class="relative">
                        <input
                            type="range"
                            id="default-volume"
                            min="0"
                            max="100"
                            value="${volume}"
                            class="w-full h-2 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-purple-500"
                            oninput="updateVolumeDisplay(this.value)"
                        />
                        <div class="absolute -bottom-5 left-0 right-0 flex justify-between text-xs text-gray-600">
                            <span>0</span>
                            <span>50</span>
                            <span>100</span>
                        </div>
                    </div>
                </div>

                <!-- Provider Selection -->
                <div class="pt-4">
                    <label class="block text-sm text-gray-400 mb-3">Default Provider</label>
                    <div class="grid grid-cols-3 gap-2">
                        ${providers.map(p => `
                            <button onclick="selectProvider('${p.id}')"
                                class="provider-btn flex flex-col items-center gap-1 p-3 rounded-lg border transition-all ${provider === p.id ? 'bg-purple-500/20 border-purple-500/50 text-purple-400' : 'bg-dark-bg border-dark-border text-gray-400 hover:border-gray-600'}">
                                <span class="text-lg">${p.icon}</span>
                                <span class="text-xs">${p.name}</span>
                            </button>
                        `).join('')}
                    </div>
                    <input type="hidden" id="default-provider" value="${provider}" />
                </div>

                <!-- Radio Mode Toggle -->
                <div class="flex items-center justify-between pt-2">
                    <div>
                        <div class="text-sm text-white">Radio Mode</div>
                        <div class="text-xs text-gray-500">Auto-play similar tracks</div>
                    </div>
                    <button onclick="toggleRadioMode()" id="radio-mode-toggle"
                        class="relative w-12 h-6 rounded-full transition-all duration-300 ${radioMode ? 'bg-purple-500' : 'bg-gray-600'}">
                        <div class="absolute top-1 ${radioMode ? 'right-1' : 'left-1'} w-4 h-4 bg-white rounded-full shadow-md transition-all duration-300"></div>
                    </button>
                    <input type="hidden" id="default-radio-mode" value="${radioMode}" />
                </div>

                <button onclick="saveDefaultSettings()"
                    class="w-full py-2.5 bg-purple-600 hover:bg-purple-700 text-white font-medium rounded-lg transition-colors mt-4">
                    Save Playback Settings
                </button>
            </div>
        </div>
    `;
}

function renderAccountsSection() {
    const accounts = musicConfig.spotify_accounts || [];

    return `
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden mb-8">
            <div class="px-5 py-4 border-b border-dark-border flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
                        <span class="text-xl">👤</span>
                    </div>
                    <div>
                        <h3 class="font-semibold text-white">Connected Accounts</h3>
                        <p class="text-xs text-gray-400">Manage your music service accounts</p>
                    </div>
                </div>
                <span class="px-3 py-1 bg-gray-800 rounded-full text-xs text-gray-400">${accounts.length}/5 accounts</span>
            </div>
            <div class="p-5">
                ${accounts.length === 0 ? `
                    <div class="text-center py-8 text-gray-500">
                        <div class="text-4xl mb-2">🎧</div>
                        <p>No accounts connected yet</p>
                        <p class="text-xs mt-1">Add a Spotify account to get started</p>
                    </div>
                ` : `
                    <div class="space-y-3 mb-4">
                        ${accounts.map((account, index) => `
                            <div class="flex items-center justify-between p-3 bg-dark-bg rounded-lg border border-dark-border">
                                <div class="flex items-center gap-3">
                                    <div class="w-10 h-10 rounded-full bg-green-500/20 flex items-center justify-center">
                                        <span class="text-lg">💚</span>
                                    </div>
                                    <div>
                                        <div class="text-white font-medium">${escapeHtml(account.name || `Account ${index + 1}`)}</div>
                                        <div class="text-xs text-gray-500">${escapeHtml(account.email || 'Spotify Account')}</div>
                                    </div>
                                </div>
                                <button onclick="removeSpotifyAccount('${escapeHtml(account.id)}')"
                                    class="p-2 text-gray-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors">
                                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                                    </svg>
                                </button>
                            </div>
                        `).join('')}
                    </div>
                `}
                ${accounts.length < 5 ? `
                    <button onclick="showAddSpotifyAccountModal()"
                        class="w-full py-2.5 border-2 border-dashed border-gray-700 hover:border-green-500/50 text-gray-400 hover:text-green-400 rounded-lg transition-all flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
                        </svg>
                        Add Account
                    </button>
                ` : ''}
            </div>
        </div>
    `;
}

function renderGenresSection() {
    const selectionMode = musicConfig.genre_selection_mode || 'random';

    return `
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden mb-8">
            <div class="px-5 py-4 border-b border-dark-border flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-lg bg-orange-500/20 flex items-center justify-center">
                        <span class="text-xl">🎸</span>
                    </div>
                    <div>
                        <h3 class="font-semibold text-white">Genre Seed Artists</h3>
                        <p class="text-xs text-gray-400">Configure artists for genre-based playback</p>
                    </div>
                </div>
                <select id="genre-selection-mode" onchange="saveGenreSelectionMode()"
                    class="px-3 py-1.5 bg-dark-bg border border-dark-border rounded-lg text-sm text-gray-300 focus:border-orange-500 outline-none">
                    <option value="random" ${selectionMode === 'random' ? 'selected' : ''}>🎲 Random</option>
                    <option value="first" ${selectionMode === 'first' ? 'selected' : ''}>1️⃣ First</option>
                    <option value="rotate" ${selectionMode === 'rotate' ? 'selected' : ''}>🔄 Rotate</option>
                </select>
            </div>
            <div class="p-5">
                ${genres.length === 0 ? `
                    <div class="text-center py-8 text-gray-500">
                        <div class="text-4xl mb-2">🎵</div>
                        <p>No genres configured yet</p>
                        <p class="text-xs mt-1">Add genres with seed artists for better recommendations</p>
                    </div>
                ` : `
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-4">
                        ${genres.map(genre => renderGenreCard(genre)).join('')}
                    </div>
                `}
                <button onclick="showAddGenreModal()"
                    class="w-full py-2.5 border-2 border-dashed border-gray-700 hover:border-orange-500/50 text-gray-400 hover:text-orange-400 rounded-lg transition-all flex items-center justify-center gap-2">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
                    </svg>
                    Add Genre
                </button>
            </div>
        </div>
    `;
}

function renderGenreCard(genre) {
    const artistCount = genre.artists?.length || 0;
    const previewArtists = genre.artists?.slice(0, 3) || [];

    return `
        <div class="bg-dark-bg border border-dark-border rounded-xl p-4 hover:border-orange-500/30 transition-all group">
            <div class="flex items-start justify-between mb-3">
                <div>
                    <h4 class="font-semibold text-white">${escapeHtml(genre.genre_name)}</h4>
                    <p class="text-xs text-gray-500">${artistCount} artist${artistCount !== 1 ? 's' : ''}</p>
                </div>
                <div class="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button onclick="showEditGenreModal('${escapeHtml(genre.genre_name)}')"
                        class="p-1.5 text-gray-400 hover:text-orange-400 hover:bg-orange-500/10 rounded-lg transition-colors">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>
                        </svg>
                    </button>
                    <button onclick="deleteGenre('${escapeHtml(genre.genre_name)}')"
                        class="p-1.5 text-gray-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                        </svg>
                    </button>
                </div>
            </div>
            <div class="flex flex-wrap gap-1.5">
                ${previewArtists.map(artist => `
                    <span class="px-2 py-0.5 bg-orange-500/10 text-orange-400 text-xs rounded-full">${escapeHtml(artist)}</span>
                `).join('')}
                ${artistCount > 3 ? `
                    <span class="px-2 py-0.5 bg-gray-700 text-gray-400 text-xs rounded-full">+${artistCount - 3} more</span>
                ` : ''}
            </div>
        </div>
    `;
}

function renderAdvancedSettings() {
    const enabled = musicConfig.health_monitoring_enabled ?? true;
    const timeout = musicConfig.stream_timeout_seconds ?? 10;
    const autoRestart = musicConfig.auto_restart_on_failure ?? true;

    return `
        <details class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <summary class="px-5 py-4 cursor-pointer hover:bg-gray-800/50 transition-colors flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-lg bg-gray-500/20 flex items-center justify-center">
                        <span class="text-xl">⚙️</span>
                    </div>
                    <div>
                        <h3 class="font-semibold text-white">Advanced Settings</h3>
                        <p class="text-xs text-gray-400">Stream health monitoring and recovery</p>
                    </div>
                </div>
                <svg class="w-5 h-5 text-gray-400 transform transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                </svg>
            </summary>
            <div class="px-5 pb-5 pt-2 border-t border-dark-border">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
                    <!-- Health Monitoring Toggle -->
                    <div class="p-4 bg-dark-bg rounded-lg border border-dark-border">
                        <div class="flex items-center justify-between">
                            <div>
                                <div class="text-sm text-white">Health Monitoring</div>
                                <div class="text-xs text-gray-500">Monitor stream status</div>
                            </div>
                            <button onclick="toggleHealthMonitoring()" id="health-monitoring-toggle"
                                class="relative w-10 h-5 rounded-full transition-all duration-300 ${enabled ? 'bg-blue-500' : 'bg-gray-600'}">
                                <div class="absolute top-0.5 ${enabled ? 'right-0.5' : 'left-0.5'} w-4 h-4 bg-white rounded-full shadow-md transition-all duration-300"></div>
                            </button>
                            <input type="hidden" id="health-monitoring-enabled" value="${enabled}" />
                        </div>
                    </div>

                    <!-- Timeout Setting -->
                    <div class="p-4 bg-dark-bg rounded-lg border border-dark-border">
                        <label class="block text-sm text-white mb-2">Stream Timeout</label>
                        <div class="flex items-center gap-2">
                            <input type="number" id="stream-timeout" value="${timeout}" min="5" max="60"
                                class="w-20 px-3 py-1.5 bg-dark-card border border-dark-border rounded-lg text-white text-center focus:border-blue-500 outline-none" />
                            <span class="text-sm text-gray-400">seconds</span>
                        </div>
                    </div>

                    <!-- Auto Restart Toggle -->
                    <div class="p-4 bg-dark-bg rounded-lg border border-dark-border">
                        <div class="flex items-center justify-between">
                            <div>
                                <div class="text-sm text-white">Auto Restart</div>
                                <div class="text-xs text-gray-500">Recover failed streams</div>
                            </div>
                            <button onclick="toggleAutoRestart()" id="auto-restart-toggle"
                                class="relative w-10 h-5 rounded-full transition-all duration-300 ${autoRestart ? 'bg-blue-500' : 'bg-gray-600'}">
                                <div class="absolute top-0.5 ${autoRestart ? 'right-0.5' : 'left-0.5'} w-4 h-4 bg-white rounded-full shadow-md transition-all duration-300"></div>
                            </button>
                            <input type="hidden" id="auto-restart" value="${autoRestart}" />
                        </div>
                    </div>
                </div>
                <button onclick="saveHealthMonitoring()"
                    class="w-full py-2.5 bg-gray-700 hover:bg-gray-600 text-white font-medium rounded-lg transition-colors">
                    Save Advanced Settings
                </button>
            </div>
        </details>
    `;
}

// ============================================================================
// INTERACTIVE FUNCTIONS
// ============================================================================

function initVolumeSlider() {
    const slider = document.getElementById('default-volume');
    if (slider) {
        updateVolumeDisplay(slider.value);
    }
}

function updateVolumeDisplay(value) {
    const display = document.getElementById('volume-display');
    if (display) {
        display.textContent = `${value}%`;
    }
}

function selectProvider(providerId) {
    document.getElementById('default-provider').value = providerId;
    // Update UI
    document.querySelectorAll('.provider-btn').forEach(btn => {
        const isSelected = btn.onclick.toString().includes(providerId);
        if (isSelected) {
            btn.classList.remove('bg-dark-bg', 'border-dark-border', 'text-gray-400');
            btn.classList.add('bg-purple-500/20', 'border-purple-500/50', 'text-purple-400');
        } else {
            btn.classList.add('bg-dark-bg', 'border-dark-border', 'text-gray-400');
            btn.classList.remove('bg-purple-500/20', 'border-purple-500/50', 'text-purple-400');
        }
    });
}

function toggleRadioMode() {
    const input = document.getElementById('default-radio-mode');
    const toggle = document.getElementById('radio-mode-toggle');
    const currentValue = input.value === 'true';
    const newValue = !currentValue;
    input.value = newValue;

    if (newValue) {
        toggle.classList.remove('bg-gray-600');
        toggle.classList.add('bg-purple-500');
        toggle.firstElementChild.classList.remove('left-1');
        toggle.firstElementChild.classList.add('right-1');
    } else {
        toggle.classList.add('bg-gray-600');
        toggle.classList.remove('bg-purple-500');
        toggle.firstElementChild.classList.add('left-1');
        toggle.firstElementChild.classList.remove('right-1');
    }
}

function toggleHealthMonitoring() {
    const input = document.getElementById('health-monitoring-enabled');
    const toggle = document.getElementById('health-monitoring-toggle');
    const currentValue = input.value === 'true';
    const newValue = !currentValue;
    input.value = newValue;

    toggle.classList.toggle('bg-blue-500', newValue);
    toggle.classList.toggle('bg-gray-600', !newValue);
    toggle.firstElementChild.classList.toggle('right-0.5', newValue);
    toggle.firstElementChild.classList.toggle('left-0.5', !newValue);
}

function toggleAutoRestart() {
    const input = document.getElementById('auto-restart');
    const toggle = document.getElementById('auto-restart-toggle');
    const currentValue = input.value === 'true';
    const newValue = !currentValue;
    input.value = newValue;

    toggle.classList.toggle('bg-blue-500', newValue);
    toggle.classList.toggle('bg-gray-600', !newValue);
    toggle.firstElementChild.classList.toggle('right-0.5', newValue);
    toggle.firstElementChild.classList.toggle('left-0.5', !newValue);
}

// ============================================================================
// FEATURE TOGGLE
// ============================================================================

async function toggleMusicFeature() {
    if (!musicFeature) {
        safeShowToast('Music feature not found', 'error');
        return;
    }

    try {
        const response = await fetch(`/api/features/${musicFeature.id}/toggle`, {
            method: 'PUT',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to toggle feature');
        }

        const updatedFeature = await response.json();
        musicFeature = updatedFeature;

        const status = updatedFeature.enabled ? 'enabled' : 'disabled';
        safeShowToast(`Music feature ${status}`, 'success');

        renderMusicConfig();
    } catch (error) {
        console.error('Failed to toggle music feature:', error);
        safeShowToast(error.message, 'error');
        await loadMusicConfig();
    }
}

// ============================================================================
// CONNECTION SETTINGS
// ============================================================================

async function testConnection() {
    const urlInput = document.getElementById('music-assistant-url');
    const url = urlInput.value.trim();

    if (!url) {
        safeShowToast('Please enter Music Assistant URL', 'error');
        return;
    }

    safeShowToast('Testing connection...', 'info');

    try {
        const response = await fetch('/api/music-config/test-connection', {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({ url })
        });

        const result = await response.json();

        if (response.ok && result.connected) {
            safeShowToast('Connection successful!', 'success');
            musicConfig.connection_status = 'connected';
            renderMusicConfig();
        } else {
            safeShowToast(result.error || 'Connection failed', 'error');
        }
    } catch (error) {
        console.error('Connection test failed:', error);
        safeShowToast('Connection test failed', 'error');
    }
}

async function saveConnectionSettings() {
    const url = document.getElementById('music-assistant-url').value.trim();

    if (!url) {
        safeShowToast('Please enter Music Assistant URL', 'error');
        return;
    }

    try {
        const response = await fetch('/api/music-config', {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({ music_assistant_url: url })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save settings');
        }

        musicConfig = await response.json();
        safeShowToast('Connection settings saved', 'success');
        renderMusicConfig();
    } catch (error) {
        console.error('Failed to save connection settings:', error);
        safeShowToast(error.message, 'error');
    }
}

// ============================================================================
// SPOTIFY ACCOUNTS
// ============================================================================

function showAddSpotifyAccountModal() {
    const modal = document.getElementById('spotify-account-modal');
    if (!modal) {
        createSpotifyAccountModal();
    }

    document.getElementById('spotify-account-form').reset();
    document.getElementById('spotify-modal-title').textContent = 'Add Spotify Account';
    document.getElementById('spotify-account-modal').classList.remove('hidden');
    document.getElementById('spotify-account-modal').classList.add('flex');
}

function createSpotifyAccountModal() {
    const modalHtml = `
        <div id="spotify-account-modal" class="fixed inset-0 bg-black/60 backdrop-blur-sm hidden items-center justify-center z-50">
            <div class="bg-dark-card border border-dark-border rounded-2xl w-full max-w-md mx-4 shadow-2xl">
                <div class="px-6 py-4 border-b border-dark-border flex items-center justify-between">
                    <h3 id="spotify-modal-title" class="text-lg font-semibold text-white">Add Spotify Account</h3>
                    <button onclick="closeSpotifyAccountModal()" class="text-gray-400 hover:text-white transition-colors">
                        <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                        </svg>
                    </button>
                </div>
                <form id="spotify-account-form" onsubmit="saveSpotifyAccount(event)" class="p-6 space-y-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">Account Name</label>
                        <input type="text" id="spotify-name" required
                            class="w-full px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-white placeholder-gray-500 focus:border-green-500 focus:ring-1 focus:ring-green-500 outline-none" />
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">Email (optional)</label>
                        <input type="email" id="spotify-email"
                            class="w-full px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-white placeholder-gray-500 focus:border-green-500 focus:ring-1 focus:ring-green-500 outline-none" />
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">Access Token</label>
                        <textarea id="spotify-token" required rows="3"
                            class="w-full px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-white placeholder-gray-500 focus:border-green-500 focus:ring-1 focus:ring-green-500 outline-none resize-none font-mono text-sm"></textarea>
                    </div>
                    <div class="flex gap-3 pt-2">
                        <button type="button" onclick="closeSpotifyAccountModal()"
                            class="flex-1 py-2.5 bg-gray-700 hover:bg-gray-600 text-white font-medium rounded-lg transition-colors">
                            Cancel
                        </button>
                        <button type="submit"
                            class="flex-1 py-2.5 bg-green-600 hover:bg-green-700 text-white font-medium rounded-lg transition-colors">
                            Save Account
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

function closeSpotifyAccountModal() {
    const modal = document.getElementById('spotify-account-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

async function saveSpotifyAccount(event) {
    event.preventDefault();

    const data = {
        name: document.getElementById('spotify-name').value,
        email: document.getElementById('spotify-email').value || null,
        access_token: document.getElementById('spotify-token').value
    };

    try {
        const response = await fetch('/api/music-config/spotify-accounts', {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save account');
        }

        safeShowToast('Spotify account added', 'success');
        closeSpotifyAccountModal();
        await loadMusicConfig();
    } catch (error) {
        console.error('Failed to save Spotify account:', error);
        safeShowToast(error.message, 'error');
    }
}

async function removeSpotifyAccount(accountId) {
    if (!confirm('Remove this Spotify account?')) {
        return;
    }

    try {
        const response = await fetch(`/api/music-config/spotify-accounts/${accountId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to remove account');
        }

        safeShowToast('Spotify account removed', 'success');
        await loadMusicConfig();
    } catch (error) {
        console.error('Failed to remove Spotify account:', error);
        safeShowToast('Failed to remove account', 'error');
    }
}

// ============================================================================
// DEFAULT SETTINGS
// ============================================================================

async function saveDefaultSettings() {
    const volume = parseInt(document.getElementById('default-volume').value);
    const provider = document.getElementById('default-provider').value;
    const radioMode = document.getElementById('default-radio-mode').value === 'true';

    try {
        const response = await fetch('/api/music-config', {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                default_volume: volume,
                default_provider: provider,
                default_radio_mode: radioMode
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save settings');
        }

        musicConfig = await response.json();
        safeShowToast('Playback settings saved', 'success');
    } catch (error) {
        console.error('Failed to save default settings:', error);
        safeShowToast(error.message, 'error');
    }
}

// ============================================================================
// HEALTH MONITORING
// ============================================================================

async function saveHealthMonitoring() {
    const enabled = document.getElementById('health-monitoring-enabled').value === 'true';
    const timeout = parseInt(document.getElementById('stream-timeout').value);
    const autoRestart = document.getElementById('auto-restart').value === 'true';

    try {
        const response = await fetch('/api/music-config', {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                health_monitoring_enabled: enabled,
                stream_timeout_seconds: timeout,
                auto_restart_on_failure: autoRestart
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save settings');
        }

        musicConfig = await response.json();
        safeShowToast('Advanced settings saved', 'success');
    } catch (error) {
        console.error('Failed to save health monitoring settings:', error);
        safeShowToast(error.message, 'error');
    }
}

// ============================================================================
// GENRE MANAGEMENT
// ============================================================================

async function saveGenreSelectionMode() {
    const mode = document.getElementById('genre-selection-mode').value;

    try {
        const response = await fetch('/api/music-config', {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({ genre_selection_mode: mode })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save selection mode');
        }

        musicConfig = await response.json();
        safeShowToast('Selection mode saved', 'success');
    } catch (error) {
        console.error('Failed to save selection mode:', error);
        safeShowToast(error.message, 'error');
    }
}

function showAddGenreModal() {
    editingGenre = null;
    document.getElementById('genre-form')?.reset();
    const titleEl = document.getElementById('genre-modal-title');
    if (titleEl) titleEl.textContent = 'Add Genre';
    const nameEl = document.getElementById('genre-name');
    if (nameEl) nameEl.disabled = false;
    const artistsEl = document.getElementById('artists-list');
    if (artistsEl) artistsEl.innerHTML = renderEmptyArtistsList();
    showGenreModal();
}

async function showEditGenreModal(genreName) {
    const genre = genres.find(g => g.genre_name === genreName);
    if (!genre) return;

    editingGenre = genreName;
    const nameEl = document.getElementById('genre-name');
    if (nameEl) {
        nameEl.value = genreName;
        nameEl.disabled = true;
    }
    const titleEl = document.getElementById('genre-modal-title');
    if (titleEl) titleEl.textContent = `Edit ${genreName}`;

    renderArtistsList(genre.artists || []);
    showGenreModal();
}

function showGenreModal() {
    let modal = document.getElementById('genre-modal');
    if (!modal) {
        createGenreModal();
        modal = document.getElementById('genre-modal');
    }
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function createGenreModal() {
    const modalHtml = `
        <div id="genre-modal" class="fixed inset-0 bg-black/60 backdrop-blur-sm hidden items-center justify-center z-50">
            <div class="bg-dark-card border border-dark-border rounded-2xl w-full max-w-lg mx-4 shadow-2xl">
                <div class="px-6 py-4 border-b border-dark-border flex items-center justify-between">
                    <h3 id="genre-modal-title" class="text-lg font-semibold text-white">Add Genre</h3>
                    <button onclick="closeGenreModal()" class="text-gray-400 hover:text-white transition-colors">
                        <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                        </svg>
                    </button>
                </div>
                <form id="genre-form" onsubmit="saveGenre(event)" class="p-6 space-y-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">Genre Name</label>
                        <input type="text" id="genre-name" required
                            class="w-full px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-white placeholder-gray-500 focus:border-orange-500 focus:ring-1 focus:ring-orange-500 outline-none"
                            placeholder="e.g., Classic Rock" />
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">Add Artists</label>
                        <div class="relative">
                            <input type="text" id="artist-search" autocomplete="off"
                                class="w-full px-4 py-2.5 bg-dark-bg border border-dark-border rounded-lg text-white placeholder-gray-500 focus:border-orange-500 focus:ring-1 focus:ring-orange-500 outline-none"
                                placeholder="Search for artist..."
                                oninput="searchArtists(this.value)" />
                            <div id="artist-suggestions" class="absolute left-0 right-0 top-full mt-1 bg-dark-card border border-dark-border rounded-lg shadow-xl max-h-48 overflow-y-auto hidden z-10"></div>
                        </div>
                    </div>
                    <div id="artists-list" class="min-h-[60px]">
                        ${renderEmptyArtistsList()}
                    </div>
                    <div class="flex gap-3 pt-2">
                        <button type="button" onclick="closeGenreModal()"
                            class="flex-1 py-2.5 bg-gray-700 hover:bg-gray-600 text-white font-medium rounded-lg transition-colors">
                            Cancel
                        </button>
                        <button type="submit"
                            class="flex-1 py-2.5 bg-orange-600 hover:bg-orange-700 text-white font-medium rounded-lg transition-colors">
                            Save Genre
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);
}

function closeGenreModal() {
    const modal = document.getElementById('genre-modal');
    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }
    const nameEl = document.getElementById('genre-name');
    if (nameEl) nameEl.disabled = false;
    editingGenre = null;
}

function renderEmptyArtistsList() {
    return `
        <div class="py-6 text-center text-gray-500 border-2 border-dashed border-gray-700 rounded-lg">
            <div class="text-2xl mb-1">🎤</div>
            <p class="text-sm">No artists added yet</p>
        </div>
    `;
}

function renderArtistsList(artists) {
    const container = document.getElementById('artists-list');
    if (!container) return;

    if (artists.length === 0) {
        container.innerHTML = renderEmptyArtistsList();
        return;
    }

    container.innerHTML = `
        <div class="flex flex-wrap gap-2">
            ${artists.map((artist, index) => `
                <div class="flex items-center gap-1.5 px-3 py-1.5 bg-orange-500/20 text-orange-400 rounded-full text-sm">
                    <span>${escapeHtml(artist)}</span>
                    <button type="button" onclick="removeArtistFromList(${index})" class="hover:text-orange-200 transition-colors">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                        </svg>
                    </button>
                </div>
            `).join('')}
        </div>
    `;
}

let artistSearchTimeout;
async function searchArtists(query) {
    clearTimeout(artistSearchTimeout);
    const suggestionsEl = document.getElementById('artist-suggestions');

    if (!query || query.length < 2) {
        if (suggestionsEl) suggestionsEl.classList.add('hidden');
        return;
    }

    artistSearchTimeout = setTimeout(async () => {
        try {
            const response = await fetch(`/api/music-config/artists/search?q=${encodeURIComponent(query)}`, {
                headers: getAuthHeaders()
            });

            if (!response.ok) return;

            const results = await response.json();
            renderArtistSuggestions(results);
        } catch (error) {
            console.error('Artist search failed:', error);
        }
    }, 300);
}

function renderArtistSuggestions(artists) {
    const container = document.getElementById('artist-suggestions');
    if (!container) return;

    if (artists.length === 0) {
        container.classList.add('hidden');
        return;
    }

    container.innerHTML = artists.map(artist => {
        const name = typeof artist === 'string' ? artist : artist.name;
        return `
            <div class="px-4 py-2.5 hover:bg-orange-500/20 cursor-pointer text-gray-300 hover:text-orange-400 transition-colors"
                onclick="selectArtist('${escapeHtml(name)}')">
                ${escapeHtml(name)}
            </div>
        `;
    }).join('');

    container.classList.remove('hidden');
}

function selectArtist(artistName) {
    const artistChips = document.querySelectorAll('#artists-list .flex > div span:first-child');
    const currentArtists = Array.from(artistChips).map(chip => chip.textContent);

    if (currentArtists.includes(artistName)) {
        safeShowToast('Artist already added', 'warning');
        return;
    }

    currentArtists.push(artistName);
    renderArtistsList(currentArtists);

    document.getElementById('artist-search').value = '';
    document.getElementById('artist-suggestions').classList.add('hidden');
}

function removeArtistFromList(index) {
    const artistChips = document.querySelectorAll('#artists-list .flex > div span:first-child');
    const currentArtists = Array.from(artistChips).map(chip => chip.textContent);
    currentArtists.splice(index, 1);
    renderArtistsList(currentArtists);
}

async function saveGenre(event) {
    event.preventDefault();

    const genreName = document.getElementById('genre-name').value.trim();
    const artistChips = document.querySelectorAll('#artists-list .flex > div span:first-child');
    const artists = Array.from(artistChips).map(chip => chip.textContent);

    if (artists.length === 0) {
        safeShowToast('Please add at least one artist', 'error');
        return;
    }

    try {
        const method = editingGenre ? 'PUT' : 'POST';
        const url = editingGenre
            ? `/api/music-config/genres/${encodeURIComponent(editingGenre)}`
            : '/api/music-config/genres';

        const response = await fetch(url, {
            method: method,
            headers: getAuthHeaders(),
            body: JSON.stringify({ genre_name: genreName, artists: artists })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save genre');
        }

        safeShowToast(`Genre ${editingGenre ? 'updated' : 'added'}`, 'success');
        closeGenreModal();
        await loadGenres();
        renderMusicConfig();
    } catch (error) {
        console.error('Failed to save genre:', error);
        safeShowToast(error.message, 'error');
    }
}

async function deleteGenre(genreName) {
    if (!confirm(`Delete "${genreName}" genre?`)) {
        return;
    }

    try {
        const response = await fetch(`/api/music-config/genres/${encodeURIComponent(genreName)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to delete genre');
        }

        safeShowToast('Genre deleted', 'success');
        await loadGenres();
        renderMusicConfig();
    } catch (error) {
        console.error('Failed to delete genre:', error);
        safeShowToast('Failed to delete genre', 'error');
    }
}

// ============================================================================
// ERROR HANDLING
// ============================================================================

function showMusicConfigError(message) {
    const container = document.getElementById('music-config-container');
    if (container) {
        container.innerHTML = `
            <div class="flex items-center justify-center h-64">
                <div class="text-center">
                    <div class="text-5xl mb-4">⚠️</div>
                    <p class="text-red-400">${escapeHtml(message)}</p>
                </div>
            </div>
        `;
    }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

function initMusicConfigPage() {
    console.log('Initializing music configuration page');
    loadMusicConfig();

    // Auto-refresh using RefreshManager
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('music-config-refresh', loadMusicConfig, 30000);
    } else {
        setInterval(() => loadMusicConfig(), 30000);
    }
}

function destroyMusicConfigPage() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('music-config-refresh');
    }
}

// Export for external use
if (typeof window !== 'undefined') {
    window.initMusicConfigPage = initMusicConfigPage;
    window.destroyMusicConfigPage = destroyMusicConfigPage;
    window.toggleMusicFeature = toggleMusicFeature;
    window.testConnection = testConnection;
    window.saveConnectionSettings = saveConnectionSettings;
    window.showAddSpotifyAccountModal = showAddSpotifyAccountModal;
    window.closeSpotifyAccountModal = closeSpotifyAccountModal;
    window.saveSpotifyAccount = saveSpotifyAccount;
    window.removeSpotifyAccount = removeSpotifyAccount;
    window.updateVolumeDisplay = updateVolumeDisplay;
    window.selectProvider = selectProvider;
    window.toggleRadioMode = toggleRadioMode;
    window.toggleHealthMonitoring = toggleHealthMonitoring;
    window.toggleAutoRestart = toggleAutoRestart;
    window.saveDefaultSettings = saveDefaultSettings;
    window.saveHealthMonitoring = saveHealthMonitoring;
    window.saveGenreSelectionMode = saveGenreSelectionMode;
    window.showAddGenreModal = showAddGenreModal;
    window.showEditGenreModal = showEditGenreModal;
    window.closeGenreModal = closeGenreModal;
    window.searchArtists = searchArtists;
    window.selectArtist = selectArtist;
    window.removeArtistFromList = removeArtistFromList;
    window.saveGenre = saveGenre;
    window.deleteGenre = deleteGenre;
}
