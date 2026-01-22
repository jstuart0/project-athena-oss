/**
 * Sendspin Audio Streaming Client for Jarvis Web
 *
 * Implements the Sendspin protocol for real-time audio streaming from
 * Music Assistant to the browser. Based on the official spec:
 * https://www.sendspin-audio.com/spec/
 *
 * Protocol Flow:
 * 1. Client sends client/hello JSON with supported roles
 * 2. Server responds with server/hello JSON
 * 3. Client sends client/time for clock sync
 * 4. Server sends server/time response
 * 5. Server sends stream/start when audio begins
 * 6. Server sends binary audio frames (type 4)
 * 7. Server sends stream/end when done
 */

class SendspinClient {
    constructor(config = {}) {
        // Connection config - use gateway proxy to avoid CORS
        this.wsUrl = config.wsUrl || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ma/sendspin`;
        this.directUrl = config.directUrl || null;

        // Client identity
        this.clientId = config.clientId || this._generateClientId();
        this.clientName = config.clientName || 'Jarvis Web Browser';

        // WebSocket connection
        this.ws = null;
        this.state = 'disconnected'; // disconnected, connecting, handshaking, syncing, streaming

        // Server info (from server/hello)
        this.serverId = null;
        this.serverName = null;
        this.activeRoles = [];

        // Audio context and playback
        this.audioContext = null;
        this.gainNode = null;
        this.volume = config.volume || 0.8;

        // Audio buffer queue for scheduling
        this.audioQueue = [];
        this.scheduledTime = 0;
        this.bufferAhead = 0.5; // Schedule 500ms ahead per spec

        // Clock synchronization (NTP-style)
        this.clockSync = {
            offset: 0,           // Server time - client time (microseconds)
            drift: 0,            // Clock drift rate
            variance: 1000000,   // Uncertainty (starts high)
            lastSync: 0,         // Last sync timestamp
            syncInterval: 5000,  // Sync every 5 seconds
            measurements: [],    // Recent measurements for averaging
            pendingRequest: null // Timestamp of pending request
        };

        // Current stream info
        this.currentStream = null;
        this.codecInfo = null;
        this.sampleRate = 48000;

        // Drift correction
        this.driftCorrection = {
            deadband: 0.001,      // 1ms - ignore tiny drifts
            sampleLimit: 0.015,   // 15ms - use sample adjustment
            rateLimit: 0.200,     // 200ms - use rate adjustment
            currentMode: 'none',
            playbackRate: 1.0
        };

        // Reconnection
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 2000;
        this.syncIntervalId = null;

        // Event callbacks
        this.onStateChange = null;
        this.onStreamStart = null;
        this.onStreamEnd = null;
        this.onError = null;
        this.onSyncUpdate = null;
    }

    /**
     * Generate a unique client ID
     */
    _generateClientId() {
        const stored = localStorage.getItem('sendspin_client_id');
        if (stored) return stored;

        const id = 'jarvis-web-' + Math.random().toString(36).substr(2, 9);
        localStorage.setItem('sendspin_client_id', id);
        return id;
    }

    /**
     * Initialize Web Audio context
     * Must be called after user interaction (browser requirement)
     */
    async initAudio() {
        if (this.audioContext && this.audioContext.state !== 'closed') {
            // Resume if suspended (mobile browsers start suspended)
            if (this.audioContext.state === 'suspended') {
                console.log('[Sendspin] Resuming suspended AudioContext');
                await this.audioContext.resume();
            }
            console.log('[Sendspin] AudioContext state:', this.audioContext.state);
            return;
        }

        this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: this.sampleRate,
            latencyHint: 'playback'
        });

        // Create gain node for volume control
        this.gainNode = this.audioContext.createGain();
        this.gainNode.gain.value = this.volume;
        this.gainNode.connect(this.audioContext.destination);

        // Resume AudioContext (required for mobile browsers)
        if (this.audioContext.state === 'suspended') {
            console.log('[Sendspin] Resuming new AudioContext');
            await this.audioContext.resume();
        }

        console.log('[Sendspin] Audio context initialized, sample rate:', this.audioContext.sampleRate, 'state:', this.audioContext.state);
    }

    /**
     * Connect to Sendspin WebSocket endpoint
     * Gateway handles authentication with MA, we just wait for audio.
     * @param {string} playerId - MA player ID to stream for (optional)
     */
    async connect(playerId = null) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            console.log('[Sendspin] Already connected');
            return true;
        }

        // Ensure audio context is initialized
        await this.initAudio();

        // Build URL with client_id (so gateway can register us as a unique player)
        const params = new URLSearchParams();
        params.set('client_id', this.clientId);
        if (playerId) {
            params.set('player_id', playerId);
        }
        const url = `${this.wsUrl}?${params.toString()}`;

        this._setState('connecting');
        console.log('[Sendspin] Connecting to:', url);

        return new Promise((resolve, reject) => {
            try {
                this.ws = new WebSocket(url);
                this.ws.binaryType = 'arraybuffer';
            } catch (error) {
                console.error('[Sendspin] WebSocket creation failed:', error);
                this._setState('disconnected');
                reject(error);
                return;
            }

            const connectionTimeout = setTimeout(() => {
                this.ws.close();
                this._setState('disconnected');
                reject(new Error('Connection timeout'));
            }, 10000);

            let resolved = false;

            this.ws.onopen = () => {
                console.log('[Sendspin] WebSocket connected');
                clearTimeout(connectionTimeout);
                this.reconnectAttempts = 0;
                // Don't send client/hello - gateway handles auth with MA
                this._setState('authenticating');
            };

            this.ws.onmessage = (event) => {
                this._handleMessage(event.data);

                // Resolve once we're connected and authenticated
                if (!resolved && (this.state === 'ready' || this.state === 'streaming')) {
                    resolved = true;
                    resolve(true);
                }
            };

            this.ws.onerror = (error) => {
                console.error('[Sendspin] WebSocket error:', error);
                clearTimeout(connectionTimeout);
                if (this.onError) this.onError(error);
                if (!resolved) {
                    resolved = true;
                    reject(error);
                }
            };

            this.ws.onclose = (event) => {
                console.log('[Sendspin] WebSocket closed:', event.code, event.reason);
                clearTimeout(connectionTimeout);
                this._stopClockSync();
                this._setState('disconnected');

                // Auto-reconnect on unexpected close
                if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
                    this._scheduleReconnect(playerId);
                }
            };

            // Timeout for initial connection
            setTimeout(() => {
                if (!resolved && (this.state === 'authenticating' || this.state === 'connecting')) {
                    console.warn('[Sendspin] Connection taking too long, resolving anyway');
                    resolved = true;
                    resolve(true);
                }
            }, 5000);
        });
    }

    /**
     * Disconnect from Sendspin
     */
    disconnect() {
        this.reconnectAttempts = this.maxReconnectAttempts; // Prevent auto-reconnect
        this._stopClockSync();

        if (this.ws) {
            this.ws.close(1000, 'Client disconnect');
            this.ws = null;
        }

        this._clearAudioQueue();
        this._setState('disconnected');
    }

    /**
     * Set playback volume
     */
    setVolume(level) {
        this.volume = Math.max(0, Math.min(1, level));
        if (this.gainNode) {
            this.gainNode.gain.value = this.volume;
        }
    }

    /**
     * Check if connected
     */
    isConnected() {
        return this.ws && this.ws.readyState === WebSocket.OPEN;
    }

    // ==================== Handshake ====================

    /**
     * Send client/hello message per Sendspin spec
     */
    _sendClientHello() {
        const hello = {
            type: 'client/hello',
            payload: {
                client_id: this.clientId,
                name: this.clientName,
                version: 1,
                supported_roles: ['player@v1'],
                device_info: {
                    product_name: 'Jarvis Web',
                    manufacturer: 'Project Athena',
                    software_version: '1.0.0'
                },
                // IMPORTANT: Use "player_support" not "player@v1_support"!
                // MA's Sendspin implementation uses the legacy field name.
                player_support: {
                    supported_formats: [
                        {
                            codec: 'opus',
                            channels: 2,
                            sample_rate: 48000,
                            bit_depth: 16
                        },
                        {
                            codec: 'pcm',
                            channels: 2,
                            sample_rate: 48000,
                            bit_depth: 16
                        },
                        {
                            codec: 'flac',
                            channels: 2,
                            sample_rate: 48000,
                            bit_depth: 16
                        }
                    ],
                    buffer_capacity: 524288,
                    supported_commands: ['volume', 'mute']
                }
            }
        };

        console.log('[Sendspin] Sending client/hello:', hello);
        this.ws.send(JSON.stringify(hello));
    }

    // ==================== Message Handling ====================

    /**
     * Handle incoming WebSocket message
     */
    _handleMessage(data) {
        if (typeof data === 'string') {
            // JSON control message
            try {
                const message = JSON.parse(data);
                this._handleJsonMessage(message);
            } catch (e) {
                console.error('[Sendspin] Failed to parse JSON message:', e);
            }
        } else {
            // Binary audio message
            this._handleBinaryMessage(data);
        }
    }

    /**
     * Handle JSON messages (control, handshake, sync)
     * MA uses a simpler protocol than the public Sendspin spec:
     * - Gateway sends: {"type": "connected", "authenticated": true, "client_id": "..."}
     * - MA sends: {"type": "auth_ok"} after auth
     * - MA sends: {"type": "stream_start", ...} when audio begins
     * - Binary audio frames follow
     */
    _handleJsonMessage(message) {
        const type = message.type;
        const payload = message.payload || message;

        console.log('[Sendspin] JSON message:', type);

        switch (type) {
            // Gateway messages
            case 'connected':
                this._handleGatewayConnected(message);
                break;

            // MA authentication response
            case 'auth_ok':
                console.log('[Sendspin] Authentication successful');
                this._setState('ready');
                break;

            // Standard Sendspin messages (in case MA follows spec)
            case 'server/hello':
                this._handleServerHello(payload);
                break;

            case 'server/time':
                this._handleServerTime(payload);
                break;

            // MA stream control
            case 'stream_start':
            case 'stream/start':
                this._handleStreamStart(payload);
                break;

            case 'stream_end':
            case 'stream/end':
                this._handleStreamEnd(payload);
                break;

            case 'stream_clear':
            case 'stream/clear':
                this._handleStreamClear(payload);
                break;

            // Player control
            case 'player/state':
            case 'player_state':
                this._handlePlayerState(payload);
                break;

            case 'player/command':
            case 'player_command':
                this._handlePlayerCommand(payload);
                break;

            // Error handling
            case 'error':
                console.error('[Sendspin] Server error:', payload);
                if (this.onError) this.onError(new Error(payload.message || payload));
                break;

            default:
                console.log('[Sendspin] Unknown message type:', type, payload);
        }
    }

    /**
     * Handle gateway connected message
     * Gateway now handles the full Sendspin handshake (auth, client/hello, server/hello, client/state)
     * so we just need to acknowledge the connection and be ready for audio.
     */
    _handleGatewayConnected(message) {
        console.log('[Sendspin] Gateway connected:', message);

        // Use the client_id assigned by gateway
        if (message.client_id) {
            this.clientId = message.client_id;
        }

        // Gateway handles the full handshake now
        const playerRegistered = message.player_registered;

        if (playerRegistered) {
            console.log('[Sendspin] Player registered with MA via Gateway');
            // We should have already received server/hello before this message
            this._setState('ready');
            this._startClockSync();
        } else if (message.authenticated) {
            console.log('[Sendspin] Gateway authenticated but player not registered');
            this._setState('ready');
        } else {
            console.warn('[Sendspin] Gateway not authenticated - may not receive audio');
            this._setState('ready');
        }
    }

    /**
     * Handle server/hello response
     */
    _handleServerHello(payload) {
        console.log('[Sendspin] Received server/hello:', payload);

        this.serverId = payload.server_id;
        this.serverName = payload.name;
        this.activeRoles = payload.active_roles || [];

        console.log('[Sendspin] Server:', this.serverName, 'Active roles:', this.activeRoles);

        // CRITICAL: Send client/state immediately after server/hello
        // This is required within 5 seconds for player registration to complete
        this._sendClientState();

        // Mark as ready for audio
        this._setState('ready');

        // Start clock synchronization
        this._startClockSync();
    }

    /**
     * Send client/state message (required after server/hello for player role)
     */
    _sendClientState() {
        const state = {
            type: 'client/state',
            payload: {
                state: 'synchronized',
                player: {
                    volume: Math.round(this.volume * 100),
                    muted: false
                }
            }
        };

        console.log('[Sendspin] Sending client/state');
        this.ws.send(JSON.stringify(state));
    }

    /**
     * Handle server/time response for clock sync
     */
    _handleServerTime(payload) {
        const now = this._getMonotonicTimeUs();

        const clientTransmitted = payload.client_transmitted;
        const serverReceived = payload.server_received;
        const serverTransmitted = payload.server_transmitted;

        // Calculate round-trip time
        const rtt = now - clientTransmitted;

        // Calculate offset using NTP algorithm
        // offset = ((serverReceived - clientTransmitted) + (serverTransmitted - now)) / 2
        const offset = ((serverReceived - clientTransmitted) + (serverTransmitted - now)) / 2;

        // Apply Kalman filter for smooth offset tracking
        this._kalmanUpdate(offset);

        console.log('[Sendspin] Clock sync - offset:', (this.clockSync.offset / 1000).toFixed(2), 'ms, RTT:', (rtt / 1000).toFixed(2), 'ms');

        if (this.state === 'syncing') {
            this._setState('streaming');
        }

        if (this.onSyncUpdate) {
            this.onSyncUpdate({
                offset: this.clockSync.offset,
                drift: this.clockSync.drift,
                rtt: rtt
            });
        }
    }

    /**
     * Handle binary audio messages
     * Format: [type:1][timestamp:8][audio_data:N]
     */
    _handleBinaryMessage(data) {
        const view = new DataView(data);
        const messageType = view.getUint8(0);

        switch (messageType) {
            case 4: // Audio data for player role
                this._processAudioData(data, view);
                break;

            default:
                console.log('[Sendspin] Unknown binary message type:', messageType);
        }
    }

    // ==================== Stream Management ====================

    _handleStreamStart(payload) {
        console.log('[Sendspin] Stream started:', payload);

        this.currentStream = {
            id: payload.stream_id,
            codec: payload.codec || 'opus',
            sampleRate: payload.sample_rate || 48000,
            channels: payload.channels || 2,
            bitDepth: payload.bit_depth || 16,
            // Capture duration and metadata if MA sends them
            duration: payload.duration || payload.duration_ms || null,
            mediaItem: payload.media_item || payload.metadata || null
        };

        this.codecInfo = this.currentStream;
        this.sampleRate = this.currentStream.sampleRate;
        this.scheduledTime = this.audioContext.currentTime + this.bufferAhead;

        // Initialize Opus decoder if needed
        if (this.currentStream.codec.toLowerCase() === 'opus') {
            this._initOpusDecoder();
        }

        this._setState('streaming');

        if (this.onStreamStart) {
            this.onStreamStart(this.currentStream);
        }
    }

    _handleStreamEnd(payload) {
        console.log('[Sendspin] Stream ended:', payload);

        this.currentStream = null;
        this._setState('syncing');

        if (this.onStreamEnd) {
            this.onStreamEnd({ streamId: payload?.stream_id });
        }
    }

    _handleStreamClear(payload) {
        console.log('[Sendspin] Stream clear (seek):', payload);
        this._clearAudioQueue();
        this.scheduledTime = this.audioContext.currentTime + this.bufferAhead;
    }

    _handlePlayerState(payload) {
        console.log('[Sendspin] Player state update:', payload);
        // Handle volume/mute state from server
        if (payload.volume !== undefined) {
            this.setVolume(payload.volume / 100);
        }
    }

    _handlePlayerCommand(payload) {
        console.log('[Sendspin] Player command:', payload);
        // Handle commands like volume, mute
        switch (payload.command) {
            case 'volume':
                this.setVolume(payload.value / 100);
                break;
            case 'mute':
                this.gainNode.gain.value = payload.value ? 0 : this.volume;
                break;
        }
    }

    // ==================== Audio Processing ====================

    /**
     * Initialize AudioDecoder for Opus (WebCodecs API)
     */
    async _initOpusDecoder() {
        if (this.opusDecoder) return;

        if (typeof AudioDecoder === 'undefined') {
            console.warn('[Sendspin] AudioDecoder not available, falling back to generic decode');
            return;
        }

        try {
            this.opusDecoder = new AudioDecoder({
                output: (audioData) => {
                    // Convert AudioData to AudioBuffer for Web Audio API
                    const audioBuffer = this.audioContext.createBuffer(
                        audioData.numberOfChannels,
                        audioData.numberOfFrames,
                        audioData.sampleRate
                    );

                    for (let i = 0; i < audioData.numberOfChannels; i++) {
                        const channelData = audioBuffer.getChannelData(i);
                        audioData.copyTo(channelData, { planeIndex: i, format: 'f32-planar' });
                    }

                    // Schedule audio playback
                    this._scheduleDecodedAudio(audioBuffer);
                    audioData.close();
                },
                error: (e) => {
                    console.error('[Sendspin] AudioDecoder error:', e);
                }
            });

            // Configure for Opus
            await this.opusDecoder.configure({
                codec: 'opus',
                sampleRate: this.sampleRate || 48000,
                numberOfChannels: this.codecInfo?.channels || 2
            });

            console.log('[Sendspin] Opus AudioDecoder initialized');
        } catch (e) {
            console.warn('[Sendspin] Failed to initialize AudioDecoder:', e);
            this.opusDecoder = null;
        }
    }

    /**
     * Schedule decoded audio for playback
     */
    _scheduleDecodedAudio(audioBuffer) {
        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.playbackRate.value = this.driftCorrection.playbackRate;
        source.connect(this.gainNode);

        const currentTime = this.audioContext.currentTime;
        const startTime = Math.max(this.scheduledTime, currentTime + 0.01);

        source.start(startTime);

        this.audioQueue.push({
            source,
            startTime: startTime,
            duration: audioBuffer.duration
        });

        this.scheduledTime = startTime + audioBuffer.duration;
        this._cleanupAudioQueue();
    }

    /**
     * Process incoming audio data
     */
    async _processAudioData(data, view) {
        // Ensure AudioContext is running (mobile browsers may suspend it)
        if (this.audioContext.state === 'suspended') {
            console.log('[Sendspin] Resuming AudioContext for audio playback');
            await this.audioContext.resume();
        }

        // Read timestamp (bytes 1-8, int64 big-endian microseconds)
        const timestampHigh = view.getUint32(1, false);
        const timestampLow = view.getUint32(5, false);
        const serverTimestamp = timestampHigh * 0x100000000 + timestampLow;

        // Audio data starts at byte 9
        const audioData = new Uint8Array(data, 9);

        // Log first audio packet for debugging
        if (!this._audioPacketLogged) {
            this._audioPacketLogged = true;
            console.log('[Sendspin] First audio packet:', {
                totalBytes: data.byteLength,
                audioBytes: audioData.length,
                codec: this.codecInfo?.codec || 'unknown',
                audioContextState: this.audioContext.state,
                gainValue: this.gainNode?.gain?.value
            });
        }

        // Convert server timestamp to client time
        const clientTimeUs = serverTimestamp - this.clockSync.offset;
        const clientTimeSec = clientTimeUs / 1000000;

        // Decode audio
        let audioBuffer;
        try {
            audioBuffer = await this._decodeAudio(audioData);
        } catch (error) {
            console.error('[Sendspin] Audio decode failed:', error);
            return;
        }

        if (!audioBuffer) return;

        // Calculate when to schedule playback
        const currentTime = this.audioContext.currentTime;
        let scheduleTime = clientTimeSec;

        // Apply drift correction
        const drift = scheduleTime - currentTime;
        scheduleTime = this._applyDriftCorrection(scheduleTime, drift);

        // Queue audio for playback
        this._scheduleAudio(audioBuffer, Math.max(scheduleTime, currentTime + 0.01));
    }

    /**
     * Decode audio data based on codec
     */
    async _decodeAudio(data) {
        const codec = this.codecInfo?.codec || 'opus';

        switch (codec.toLowerCase()) {
            case 'pcm':
            case 'raw':
                return this._decodePCM(data);

            case 'opus':
                // Try WebCodecs AudioDecoder first (handles raw Opus frames)
                if (this.opusDecoder && this.opusDecoder.state === 'configured') {
                    return this._decodeOpus(data);
                }
                // Fall through to generic if AudioDecoder not available
                return this._decodeGeneric(data);

            case 'flac':
            default:
                return this._decodeGeneric(data);
        }
    }

    /**
     * Decode Opus using WebCodecs AudioDecoder
     * Returns null because decoding is asynchronous (handled by decoder callback)
     */
    _decodeOpus(data) {
        try {
            // Create EncodedAudioChunk from raw Opus frame
            const chunk = new EncodedAudioChunk({
                type: 'key', // Opus frames are all keyframes
                timestamp: 0, // Will be adjusted by our scheduling
                data: data
            });

            this.opusDecoder.decode(chunk);
            return null; // Decoding happens asynchronously
        } catch (e) {
            console.warn('[Sendspin] Opus decode error:', e);
            return null;
        }
    }

    /**
     * Decode raw PCM audio
     */
    _decodePCM(data) {
        const channels = this.codecInfo?.channels || 2;
        const sampleRate = this.codecInfo?.sampleRate || 48000;
        const bitDepth = this.codecInfo?.bitDepth || 16;
        const bytesPerSample = bitDepth / 8;

        const samples = Math.floor(data.length / (channels * bytesPerSample));

        if (samples <= 0) {
            console.warn('[Sendspin] PCM decode: no samples, data length:', data.length);
            return null;
        }

        // Log first PCM packet for debugging
        if (!this._pcmLogged) {
            this._pcmLogged = true;
            console.log('[Sendspin] PCM decode:', {
                channels,
                sampleRate,
                bitDepth,
                bytesPerSample,
                dataLength: data.length,
                samples,
                firstBytes: Array.from(data.slice(0, 16))
            });
        }

        const audioBuffer = this.audioContext.createBuffer(channels, samples, sampleRate);
        const view = new DataView(data.buffer, data.byteOffset, data.byteLength);

        for (let channel = 0; channel < channels; channel++) {
            const channelData = audioBuffer.getChannelData(channel);

            for (let i = 0; i < samples; i++) {
                const byteIndex = (i * channels + channel) * bytesPerSample;

                if (byteIndex + bytesPerSample > data.byteLength) {
                    break; // Prevent out-of-bounds read
                }

                if (bytesPerSample === 2) {
                    const sample = view.getInt16(byteIndex, true);
                    channelData[i] = sample / 32768.0;
                } else if (bytesPerSample === 4) {
                    channelData[i] = view.getFloat32(byteIndex, true);
                }
            }
        }

        return audioBuffer;
    }

    /**
     * Decode audio using Web Audio API
     */
    async _decodeGeneric(data) {
        try {
            const audioBuffer = await this.audioContext.decodeAudioData(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
            return audioBuffer;
        } catch (e) {
            console.warn('[Sendspin] Generic decode failed:', e.message);
            return null;
        }
    }

    /**
     * Schedule audio buffer for playback
     */
    _scheduleAudio(audioBuffer, time) {
        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.playbackRate.value = this.driftCorrection.playbackRate;
        source.connect(this.gainNode);

        if (time < this.audioContext.currentTime) {
            source.start(0);
        } else {
            source.start(time);
        }

        this.audioQueue.push({
            source,
            startTime: time,
            duration: audioBuffer.duration
        });

        this.scheduledTime = Math.max(this.scheduledTime, time + audioBuffer.duration);
        this._cleanupAudioQueue();
    }

    _clearAudioQueue() {
        for (const item of this.audioQueue) {
            try {
                item.source.stop();
            } catch (e) {
                // Already stopped
            }
        }
        this.audioQueue = [];
        this.scheduledTime = 0;
    }

    _cleanupAudioQueue() {
        const currentTime = this.audioContext.currentTime;
        this.audioQueue = this.audioQueue.filter(item =>
            item.startTime + item.duration > currentTime
        );
    }

    // ==================== Clock Synchronization ====================

    /**
     * Get monotonic time in microseconds
     */
    _getMonotonicTimeUs() {
        return Math.floor(performance.now() * 1000);
    }

    /**
     * Start clock synchronization
     */
    _startClockSync() {
        this._setState('syncing');

        // Send initial sync request
        this._sendTimeRequest();

        // Set up periodic sync
        this.syncIntervalId = setInterval(() => {
            if (this.isConnected()) {
                this._sendTimeRequest();
            }
        }, this.clockSync.syncInterval);
    }

    _stopClockSync() {
        if (this.syncIntervalId) {
            clearInterval(this.syncIntervalId);
            this.syncIntervalId = null;
        }
    }

    /**
     * Send client/time message for clock synchronization
     */
    _sendTimeRequest() {
        if (!this.isConnected()) return;

        const now = this._getMonotonicTimeUs();
        this.clockSync.pendingRequest = now;

        const message = {
            type: 'client/time',
            payload: {
                client_transmitted: now
            }
        };

        this.ws.send(JSON.stringify(message));
    }

    /**
     * Kalman filter update for clock synchronization
     */
    _kalmanUpdate(measurement) {
        const Q = 100;     // Process noise
        const R = 10000;   // Measurement noise (network jitter)

        // Prediction step
        const predictedOffset = this.clockSync.offset + this.clockSync.drift;
        const predictedVariance = this.clockSync.variance + Q;

        // Update step
        const K = predictedVariance / (predictedVariance + R);
        this.clockSync.offset = predictedOffset + K * (measurement - predictedOffset);
        this.clockSync.variance = (1 - K) * predictedVariance;

        // Update drift estimate
        this.clockSync.measurements.push(measurement);
        if (this.clockSync.measurements.length > 10) {
            this.clockSync.measurements.shift();
        }

        if (this.clockSync.measurements.length >= 2) {
            const recent = this.clockSync.measurements.slice(-5);
            const avgDrift = recent.reduce((a, b, i, arr) =>
                i > 0 ? a + (b - arr[i - 1]) : 0, 0) / (recent.length - 1);
            this.clockSync.drift = avgDrift * 0.1;
        }

        this.clockSync.lastSync = Date.now();
    }

    // ==================== Drift Correction ====================

    _applyDriftCorrection(scheduleTime, drift) {
        const absDrift = Math.abs(drift);

        if (absDrift < this.driftCorrection.deadband) {
            this.driftCorrection.currentMode = 'none';
            this.driftCorrection.playbackRate = 1.0;
            return scheduleTime;
        }

        if (absDrift < this.driftCorrection.sampleLimit) {
            this.driftCorrection.currentMode = 'sample';
            this.driftCorrection.playbackRate = 1.0;
            return scheduleTime;
        }

        if (absDrift < this.driftCorrection.rateLimit) {
            this.driftCorrection.currentMode = 'rate';
            const correction = drift > 0 ? 0.98 : 1.02;
            this.driftCorrection.playbackRate = correction;
            return scheduleTime;
        }

        console.log('[Sendspin] Hard resync, drift:', drift * 1000, 'ms');
        this.driftCorrection.currentMode = 'resync';
        this.driftCorrection.playbackRate = 1.0;
        this._clearAudioQueue();
        return this.audioContext.currentTime + this.bufferAhead;
    }

    // ==================== State Management ====================

    _setState(newState) {
        const oldState = this.state;
        this.state = newState;
        console.log('[Sendspin] State:', oldState, '->', newState);

        if (this.onStateChange) {
            this.onStateChange(newState, oldState);
        }
    }

    _scheduleReconnect(playerId) {
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * Math.min(this.reconnectAttempts, 3);

        console.log(`[Sendspin] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);

        setTimeout(() => {
            if (this.state === 'disconnected') {
                this.connect(playerId).catch(err => {
                    console.error('[Sendspin] Reconnection failed:', err.message);
                });
            }
        }, delay);
    }

    /**
     * Get current playback state
     */
    getState() {
        return {
            state: this.state,
            isStreaming: this.currentStream !== null,
            streamId: this.currentStream?.id,
            clockOffset: this.clockSync.offset,
            driftMode: this.driftCorrection.currentMode,
            queueLength: this.audioQueue.length,
            serverId: this.serverId,
            serverName: this.serverName,
            activeRoles: this.activeRoles
        };
    }
}

// =============================================================================
// Sendspin Integration with Music Player
// =============================================================================

class SendspinAudioSource {
    constructor(musicPlayer) {
        this.musicPlayer = musicPlayer;
        this.client = new SendspinClient();
        this.isActive = false;

        this.client.onStateChange = (state) => {
            console.log('[SendspinSource] State:', state);
            if (state === 'streaming') {
                this.isActive = true;
            } else if (state === 'disconnected') {
                this.isActive = false;
            }
        };

        this.client.onStreamStart = (info) => {
            console.log('[SendspinSource] Stream started:', info);
            if (this.musicPlayer) {
                this.musicPlayer.isPlaying = true;
                this.musicPlayer._updatePlayButton();
            }
        };

        this.client.onStreamEnd = (info) => {
            console.log('[SendspinSource] Stream ended');
            if (this.musicPlayer) {
                this.musicPlayer.isPlaying = false;
                this.musicPlayer._updatePlayButton();
            }
        };

        this.client.onError = (error) => {
            console.error('[SendspinSource] Error:', error);
            if (this.musicPlayer?.onError) {
                this.musicPlayer.onError(error);
            }
        };
    }

    async connect(playerId) {
        return this.client.connect(playerId);
    }

    disconnect() {
        this.client.disconnect();
        this.isActive = false;
    }

    setVolume(level) {
        this.client.setVolume(level);
    }

    isStreaming() {
        return this.isActive && this.client.state === 'streaming';
    }
}

// Export for use in Jarvis Web
window.SendspinClient = SendspinClient;
window.SendspinAudioSource = SendspinAudioSource;
