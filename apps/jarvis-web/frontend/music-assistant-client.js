/**
 * Music Assistant WebSocket Client for Jarvis Web Browser Playback
 *
 * Connects to Music Assistant server, handles authentication, search,
 * and playback control for streaming audio directly to the browser.
 *
 * Based on Music Assistant 2.7+ WebSocket API specification.
 */

class MusicAssistantClient {
    constructor(config = {}) {
        // Default to gateway proxy (avoids CORS issues)
        this.serverUrl = config.serverUrl || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ma/ws`;
        this.streamBaseUrl = config.streamBaseUrl || null; // Set after connection
        this.ws = null;
        this.messageId = 0;
        this.pendingRequests = new Map();
        this.state = 'disconnected'; // disconnected, connecting, connected, authenticated
        this.serverInfo = null;
        this.playerId = null; // Assigned player ID for browser
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 5000;

        // Event callbacks
        this.onStateChange = null;
        this.onPlayerUpdate = null;
        this.onQueueUpdate = null;
        this.onError = null;
        this.onConnect = null;
        this.onDisconnect = null;
    }

    /**
     * Connect to Music Assistant WebSocket server
     * @returns {Promise<object>} Server info on successful connection
     */
    async connect() {
        return new Promise((resolve, reject) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                console.log('[MA Client] Already connected');
                resolve(this.serverInfo);
                return;
            }

            this._setState('connecting');
            console.log('[MA Client] Connecting to:', this.serverUrl);

            try {
                this.ws = new WebSocket(this.serverUrl);
            } catch (error) {
                console.error('[MA Client] WebSocket creation failed:', error);
                this._setState('disconnected');
                reject(error);
                return;
            }

            const connectionTimeout = setTimeout(() => {
                console.error('[MA Client] Connection timeout');
                this.ws.close();
                this._setState('disconnected');
                reject(new Error('Connection timeout'));
            }, 10000);

            this.ws.onopen = () => {
                console.log('[MA Client] WebSocket opened');
                clearTimeout(connectionTimeout);
                this.reconnectAttempts = 0;
            };

            this.ws.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    this._handleMessage(message, resolve, reject);
                } catch (e) {
                    console.error('[MA Client] Failed to parse message:', e);
                }
            };

            this.ws.onerror = (error) => {
                console.error('[MA Client] WebSocket error:', error);
                clearTimeout(connectionTimeout);
                if (this.onError) {
                    this.onError(error);
                }
            };

            this.ws.onclose = (event) => {
                console.log('[MA Client] WebSocket closed:', event.code, event.reason);
                clearTimeout(connectionTimeout);
                this._setState('disconnected');

                if (this.onDisconnect) {
                    this.onDisconnect(event);
                }

                // Auto-reconnect if not explicitly closed
                if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
                    this._scheduleReconnect();
                }
            };
        });
    }

    /**
     * Disconnect from Music Assistant
     */
    disconnect() {
        this.reconnectAttempts = this.maxReconnectAttempts; // Prevent auto-reconnect
        if (this.ws) {
            this.ws.close(1000, 'Client disconnect');
            this.ws = null;
        }
        this._setState('disconnected');
    }

    /**
     * Search for music
     * @param {string} query - Search query
     * @param {string[]} mediaTypes - Types to search (track, artist, album, playlist)
     * @param {number} limit - Maximum results per type
     * @returns {Promise<object>} Search results
     */
    async search(query, mediaTypes = ['track', 'artist', 'album', 'playlist'], limit = 25) {
        console.log('[MA Client] Searching:', query);
        return this._sendCommand('music/search', {
            search_query: query,
            media_types: mediaTypes,
            limit: limit
        });
    }

    /**
     * Get available players
     * @returns {Promise<object[]>} List of players
     */
    async getPlayers() {
        return this._sendCommand('players');
    }

    /**
     * Play media on a player
     * @param {object|string} media - Media item or URI to play
     * @param {string} playerId - Target player ID (defaults to browser player)
     * @param {boolean} radioMode - Enable radio mode for continuous playback
     * @returns {Promise<object>} Playback result
     */
    async playMedia(media, playerId = this.playerId, radioMode = true) {
        if (!playerId) {
            throw new Error('No player ID available. Call initializeBrowserPlayer first.');
        }

        const mediaUri = typeof media === 'string' ? media : media.uri;
        console.log('[MA Client] Playing media:', mediaUri, 'on player:', playerId);

        return this._sendCommand('player_queues/play_media', {
            queue_id: playerId,
            media: typeof media === 'string' ? { uri: media } : media,
            option: 'replace', // Replace current queue
            radio_mode: radioMode
        });
    }

    /**
     * Execute a player command
     * @param {string} command - Command: play, pause, stop, next, previous
     * @param {string} playerId - Target player ID
     * @returns {Promise<object>} Command result
     */
    async playerCommand(command, playerId = this.playerId) {
        if (!playerId) {
            throw new Error('No player ID available');
        }

        console.log('[MA Client] Player command:', command, 'on:', playerId);
        return this._sendCommand('players/cmd/' + command, {
            player_id: playerId
        });
    }

    /**
     * Set player volume
     * @param {number} level - Volume level 0-100
     * @param {string} playerId - Target player ID
     * @returns {Promise<object>} Result
     */
    async setVolume(level, playerId = this.playerId) {
        if (!playerId) {
            throw new Error('No player ID available');
        }

        console.log('[MA Client] Setting volume:', level);
        return this._sendCommand('players/cmd/volume_set', {
            player_id: playerId,
            volume_level: Math.max(0, Math.min(100, level))
        });
    }

    /**
     * Get queue items for a player
     * @param {string} playerId - Player ID
     * @param {number} limit - Maximum items to return
     * @returns {Promise<object[]>} Queue items
     */
    async getQueueItems(playerId = this.playerId, limit = 50) {
        if (!playerId) {
            return [];
        }

        return this._sendCommand('player_queues/items', {
            queue_id: playerId,
            limit: limit
        });
    }

    /**
     * Initialize browser as a Music Assistant player
     * Attempts to register with MA or find existing browser player
     * @param {string} deviceName - Name for the browser player
     * @returns {Promise<string|null>} Player ID or null if failed
     */
    async initializeBrowserPlayer(deviceName = 'Jarvis Web Browser') {
        console.log('[MA Client] Initializing browser player:', deviceName);

        try {
            // First, get list of existing players
            const players = await this.getPlayers();

            // Look for existing browser/web player
            if (Array.isArray(players)) {
                const browserPlayer = players.find(p =>
                    p.name?.toLowerCase().includes('browser') ||
                    p.name?.toLowerCase().includes('web') ||
                    p.player_id?.includes('browser')
                );

                if (browserPlayer) {
                    this.playerId = browserPlayer.player_id;
                    console.log('[MA Client] Found existing browser player:', this.playerId);
                    return this.playerId;
                }
            }

            // Try to create/register browser player
            // Note: This may not be supported by all MA versions
            try {
                const result = await this._sendCommand('players/create', {
                    name: deviceName,
                    player_type: 'slimproto', // Or 'web' depending on MA version
                    device_info: {
                        manufacturer: 'Jarvis Web',
                        model: navigator.userAgent.substring(0, 50)
                    }
                });

                if (result && result.player_id) {
                    this.playerId = result.player_id;
                    console.log('[MA Client] Created browser player:', this.playerId);
                    return this.playerId;
                }
            } catch (createError) {
                console.warn('[MA Client] Browser player creation not supported:', createError.message);
            }

            // If we can't create a browser player, we'll use stream URLs directly
            // and control playback via local audio element
            console.log('[MA Client] Using direct streaming mode (no dedicated player)');
            this.playerId = null;
            return null;

        } catch (error) {
            console.error('[MA Client] Failed to initialize browser player:', error);
            return null;
        }
    }

    /**
     * Get direct stream URL for a track
     * Used for HTML5 audio playback fallback
     * @param {string} uri - Track URI
     * @returns {string} Stream URL
     */
    getStreamUrl(uri) {
        if (!this.streamBaseUrl) {
            // Extract host from WebSocket URL
            const wsUrl = new URL(this.serverUrl);
            // MA streams on port 8097 by default
            this.streamBaseUrl = `http://${wsUrl.hostname}:8097`;
        }

        return `${this.streamBaseUrl}/stream/${encodeURIComponent(uri)}`;
    }

    /**
     * Subscribe to player events
     * @param {string} playerId - Player ID to monitor
     * @returns {Promise<object>} Subscription result
     */
    async subscribeToPlayer(playerId = this.playerId) {
        if (!playerId) return null;

        return this._sendCommand('subscribe_events', {
            event_types: ['player_updated', 'queue_updated'],
            filter: { player_id: playerId }
        });
    }

    /**
     * Check if client is connected
     * @returns {boolean}
     */
    isConnected() {
        return this.state === 'connected' || this.state === 'authenticated';
    }

    // ==================== Private Methods ====================

    _setState(newState) {
        const oldState = this.state;
        this.state = newState;
        console.log('[MA Client] State:', oldState, '->', newState);

        if (this.onStateChange) {
            this.onStateChange(newState, oldState);
        }
    }

    _sendCommand(command, args = {}) {
        return new Promise((resolve, reject) => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                reject(new Error('Not connected to Music Assistant'));
                return;
            }

            const msgId = ++this.messageId;
            this.pendingRequests.set(msgId, { resolve, reject, command });

            const message = {
                message_id: msgId,
                command: command,
                args: args
            };

            console.log('[MA Client] Sending:', command);
            this.ws.send(JSON.stringify(message));

            // Timeout after 30 seconds
            setTimeout(() => {
                if (this.pendingRequests.has(msgId)) {
                    this.pendingRequests.delete(msgId);
                    reject(new Error(`Request timeout: ${command}`));
                }
            }, 30000);
        });
    }

    _handleMessage(message, connectResolve, connectReject) {
        // Handle gateway error messages
        if (message.error) {
            console.error('[MA Client] Gateway error:', message.error, message.details);
            this._setState('disconnected');
            if (this.onError) {
                this.onError(new Error(message.details || message.error));
            }
            if (connectReject) {
                connectReject(new Error(message.details || message.error));
            }
            return;
        }

        // Handle server_info (initial connection message)
        if (message.type === 'server_info' || message.server_info || message.server_id) {
            this.serverInfo = message.server_info || message;
            console.log('[MA Client] Server info received:', this.serverInfo);

            // Check if gateway already authenticated for us
            if (message._gateway_authenticated) {
                console.log('[MA Client] Gateway authenticated - ready for commands');
                this._setState('authenticated');
            } else {
                console.log('[MA Client] No gateway auth - may need manual authentication');
                this._setState('connected');
            }

            if (this.onConnect) {
                this.onConnect(this.serverInfo);
            }

            if (connectResolve) {
                connectResolve(this.serverInfo);
            }
            return;
        }

        // Handle response to pending request
        if (message.message_id && this.pendingRequests.has(message.message_id)) {
            const { resolve, reject, command } = this.pendingRequests.get(message.message_id);
            this.pendingRequests.delete(message.message_id);

            if (message.error_code || message.error) {
                const errorMsg = message.details || message.error?.message || `Error in ${command}`;
                console.error('[MA Client] Command error:', errorMsg);
                reject(new Error(errorMsg));
            } else {
                resolve(message.result !== undefined ? message.result : message);
            }
            return;
        }

        // Handle server-initiated events
        if (message.event) {
            this._handleEvent(message.event, message.data || message);
            return;
        }

        // Log unhandled messages
        console.log('[MA Client] Unhandled message:', message);
    }

    _handleEvent(eventType, data) {
        console.log('[MA Client] Event:', eventType);

        switch (eventType) {
            case 'player_updated':
                if (this.onPlayerUpdate) {
                    this.onPlayerUpdate(data);
                }
                break;

            case 'queue_updated':
                if (this.onQueueUpdate) {
                    this.onQueueUpdate(data);
                }
                break;

            case 'media_item_updated':
                // Track/album/artist info updated
                break;

            default:
                console.log('[MA Client] Unhandled event type:', eventType);
        }
    }

    _scheduleReconnect() {
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * Math.min(this.reconnectAttempts, 3);

        console.log(`[MA Client] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);

        setTimeout(() => {
            if (this.state === 'disconnected') {
                this.connect().catch(err => {
                    console.error('[MA Client] Reconnection failed:', err.message);
                });
            }
        }, delay);
    }
}

// Export for use in Jarvis Web
window.MusicAssistantClient = MusicAssistantClient;
