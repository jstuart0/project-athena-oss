/**
 * LiveKit WebRTC Client for Jarvis Web Interface
 *
 * Provides real-time audio streaming to Athena:
 * - WebRTC audio capture and streaming
 * - Client-side wake word detection (optional)
 * - Voice Activity Detection (VAD)
 * - Low-latency audio playback
 * - Interruption handling
 *
 * Usage:
 *   const client = new LiveKitClient();
 *   await client.connect();
 *   // Audio streams automatically, wake word triggers query
 */

// =============================================================================
// Configuration
// =============================================================================

const DEFAULT_CONFIG = {
    gatewayUrl: window.location.origin,
    sampleRate: 16000,
    channels: 1,
    vadThreshold: 0.01,
    silenceTimeoutMs: 2000,
    maxQueryDurationMs: 30000,
    wakeWords: ['jarvis', 'athena'],
    enableClientWakeWord: false,  // Server-side by default
    enableVAD: true
};

// =============================================================================
// LiveKit Client Class
// =============================================================================

class LiveKitClient {
    constructor(config = {}) {
        this.config = { ...DEFAULT_CONFIG, ...config };
        this.room = null;
        this.localAudioTrack = null;
        this.remoteAudioTrack = null;

        // State
        this.isConnected = false;
        this.isListening = false;
        this.isProcessing = false;

        // Audio context
        this.audioContext = null;
        this.mediaStream = null;
        this.analyser = null;

        // VAD state
        this.silenceStart = null;
        this.isSpeaking = false;

        // Callbacks
        this.onStateChange = null;
        this.onTranscript = null;
        this.onResponse = null;
        this.onError = null;
        this.onWakeWord = null;

        // Room info
        this.roomName = null;
        this.token = null;
        this.livekitUrl = null;
    }

    // =========================================================================
    // Connection Management
    // =========================================================================

    /**
     * Check if LiveKit is available and configured.
     */
    async checkAvailability() {
        try {
            const response = await fetch(`${this.config.gatewayUrl}/livekit/config`);
            const config = await response.json();

            if (config.enabled) {
                this.livekitUrl = config.livekit_url;
                this.config.wakeWords = config.wake_words || this.config.wakeWords;
                this.config.silenceTimeoutMs = config.silence_timeout_ms || this.config.silenceTimeoutMs;
                return true;
            }
            return false;
        } catch (error) {
            console.error('[LiveKit] Availability check failed:', error);
            return false;
        }
    }

    /**
     * Connect to LiveKit and start streaming.
     */
    async connect() {
        if (this.isConnected) {
            console.log('[LiveKit] Already connected');
            return true;
        }

        try {
            // Check availability first
            const available = await this.checkAvailability();
            if (!available) {
                throw new Error('LiveKit not available');
            }

            // Request microphone permission
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: this.config.sampleRate,
                    channelCount: this.config.channels,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });

            // Create room on server
            const roomResponse = await fetch(`${this.config.gatewayUrl}/livekit/rooms`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    participant_name: 'User',
                    room_config: 'web_jarvis'
                })
            });

            if (!roomResponse.ok) {
                throw new Error(`Room creation failed: ${roomResponse.status}`);
            }

            const roomData = await roomResponse.json();
            this.roomName = roomData.room_name;
            this.token = roomData.token;
            this.livekitUrl = roomData.livekit_url;

            // Connect to LiveKit room
            await this._connectToRoom();

            // Have Athena join the room for server-side processing
            await fetch(`${this.config.gatewayUrl}/livekit/rooms/${this.roomName}/athena-join`, {
                method: 'POST'
            });

            this.isConnected = true;
            this._updateState('connected');

            console.log('[LiveKit] Connected to room:', this.roomName);
            return true;

        } catch (error) {
            console.error('[LiveKit] Connection failed:', error);
            this._handleError(error);
            return false;
        }
    }

    /**
     * Connect to LiveKit room using their SDK.
     */
    async _connectToRoom() {
        // Load LiveKit SDK dynamically if not present
        if (!window.LivekitClient) {
            await this._loadLiveKitSDK();
        }

        const { Room, RoomEvent, Track } = window.LivekitClient;

        this.room = new Room({
            adaptiveStream: true,
            dynacast: true,
            audioCaptureDefaults: {
                autoGainControl: true,
                echoCancellation: true,
                noiseSuppression: true
            }
        });

        // Set up event handlers
        this.room.on(RoomEvent.Connected, () => {
            console.log('[LiveKit] Room connected');
        });

        this.room.on(RoomEvent.Disconnected, () => {
            console.log('[LiveKit] Room disconnected');
            this.isConnected = false;
            this._updateState('disconnected');
        });

        this.room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
            if (track.kind === Track.Kind.Audio) {
                console.log('[LiveKit] Received audio track from:', participant.identity);
                this._handleRemoteAudio(track);
            }
        });

        this.room.on(RoomEvent.DataReceived, (data, participant) => {
            this._handleDataMessage(data, participant);
        });

        // Connect to room
        await this.room.connect(this.livekitUrl, this.token);

        // Publish local audio
        await this._publishLocalAudio();
    }

    /**
     * Load LiveKit SDK from CDN.
     */
    async _loadLiveKitSDK() {
        return new Promise((resolve, reject) => {
            if (window.LivekitClient) {
                resolve();
                return;
            }

            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js';
            script.onload = () => {
                console.log('[LiveKit] SDK loaded');
                resolve();
            };
            script.onerror = () => reject(new Error('Failed to load LiveKit SDK'));
            document.head.appendChild(script);
        });
    }

    /**
     * Publish local microphone audio to room.
     */
    async _publishLocalAudio() {
        if (!this.mediaStream) return;

        const audioTrack = this.mediaStream.getAudioTracks()[0];
        if (!audioTrack) return;

        this.localAudioTrack = await this.room.localParticipant.publishTrack(audioTrack, {
            name: 'user_microphone',
            source: window.LivekitClient.Track.Source.Microphone
        });

        console.log('[LiveKit] Local audio published');

        // Set up VAD
        if (this.config.enableVAD) {
            this._setupVAD();
        }
    }

    /**
     * Handle remote audio track (Athena's response).
     */
    _handleRemoteAudio(track) {
        // Attach to audio element for playback
        const audioElement = document.createElement('audio');
        audioElement.autoplay = true;
        audioElement.id = 'athena-audio-output';
        track.attach(audioElement);

        // Store reference
        this.remoteAudioTrack = track;

        // Remove old audio elements
        const oldAudio = document.getElementById('athena-audio-output');
        if (oldAudio && oldAudio !== audioElement) {
            oldAudio.remove();
        }
        document.body.appendChild(audioElement);
    }

    /**
     * Handle data messages from server.
     */
    _handleDataMessage(data, participant) {
        try {
            const message = JSON.parse(new TextDecoder().decode(data));

            switch (message.type) {
                case 'wake_word_detected':
                    console.log('[LiveKit] Wake word detected');
                    this.isListening = true;
                    this._updateState('listening');
                    if (this.onWakeWord) this.onWakeWord(message.wake_word);
                    break;

                case 'transcript':
                    console.log('[LiveKit] Transcript:', message.text);
                    if (this.onTranscript) this.onTranscript(message.text);
                    break;

                case 'response_start':
                    this.isProcessing = false;
                    this._updateState('responding');
                    break;

                case 'response_end':
                    this.isListening = false;
                    this._updateState('idle');
                    if (this.onResponse) this.onResponse(message.text);
                    break;

                case 'error':
                    console.error('[LiveKit] Server error:', message.error);
                    if (this.onError) this.onError(new Error(message.error));
                    break;

                case 'interrupted':
                    console.log('[LiveKit] Response was interrupted');
                    this._stopRemoteAudio();
                    this._updateState('listening');
                    if (this.onInterrupted) this.onInterrupted(message);
                    break;

                case 'tts_stopped':
                    console.log('[LiveKit] TTS stopped');
                    this._stopRemoteAudio();
                    break;

                case 'acknowledgment':
                    console.log('[LiveKit] Acknowledgment:', message.text);
                    // Brief ack, UI can show text briefly
                    break;
            }
        } catch (error) {
            console.error('[LiveKit] Failed to parse data message:', error);
        }
    }

    // =========================================================================
    // Voice Activity Detection
    // =========================================================================

    /**
     * Set up Voice Activity Detection on local audio.
     */
    _setupVAD() {
        if (!this.mediaStream) return;

        this.audioContext = new AudioContext({ sampleRate: this.config.sampleRate });
        const source = this.audioContext.createMediaStreamSource(this.mediaStream);
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 2048;

        source.connect(this.analyser);

        // Start VAD loop
        this._vadLoop();
    }

    /**
     * VAD processing loop.
     */
    _vadLoop() {
        if (!this.analyser || !this.isConnected) return;

        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        this.analyser.getByteFrequencyData(dataArray);

        // Calculate RMS energy
        const sum = dataArray.reduce((a, b) => a + b, 0);
        const average = sum / dataArray.length;
        const normalized = average / 255;

        const speaking = normalized > this.config.vadThreshold;

        if (speaking) {
            this.silenceStart = null;
            if (!this.isSpeaking) {
                this.isSpeaking = true;
                // console.log('[LiveKit] Speech started');
            }
        } else {
            if (this.isSpeaking) {
                if (!this.silenceStart) {
                    this.silenceStart = Date.now();
                } else if (Date.now() - this.silenceStart > this.config.silenceTimeoutMs) {
                    this.isSpeaking = false;
                    // console.log('[LiveKit] Speech ended');
                }
            }
        }

        requestAnimationFrame(() => this._vadLoop());
    }

    // =========================================================================
    // Manual Controls
    // =========================================================================

    /**
     * Manually trigger wake word (for button-press mode).
     */
    triggerWakeWord() {
        if (!this.isConnected) {
            console.warn('[LiveKit] Not connected');
            return;
        }

        // Send wake word trigger to server
        const encoder = new TextEncoder();
        const data = encoder.encode(JSON.stringify({ type: 'manual_wake' }));
        this.room.localParticipant.publishData(data, { reliable: true });

        this.isListening = true;
        this._updateState('listening');
    }

    /**
     * Interrupt current response.
     * Stops TTS playback immediately and optionally provides visual feedback.
     */
    interrupt() {
        if (!this.isConnected) return;

        const encoder = new TextEncoder();
        const data = encoder.encode(JSON.stringify({ type: 'interrupt' }));
        this.room.localParticipant.publishData(data, { reliable: true });

        // Stop any local audio playback immediately
        this._stopRemoteAudio();

        this._updateState('interrupted');

        // Show visual feedback
        this._showInterruptFeedback();
    }

    /**
     * Stop remote audio playback immediately.
     */
    _stopRemoteAudio() {
        const audioElement = document.getElementById('athena-audio-output');
        if (audioElement) {
            audioElement.pause();
            audioElement.currentTime = 0;
        }
    }

    /**
     * Show visual feedback for interruption.
     */
    _showInterruptFeedback() {
        const indicator = document.createElement('div');
        indicator.className = 'interrupt-indicator';
        indicator.innerHTML = '⏹️ Interrupted';
        document.body.appendChild(indicator);

        setTimeout(() => {
            indicator.classList.add('fade-out');
            setTimeout(() => indicator.remove(), 300);
        }, 500);
    }

    /**
     * Mute/unmute local audio.
     */
    setMuted(muted) {
        if (this.localAudioTrack) {
            this.localAudioTrack.mute(muted);
        }
    }

    // =========================================================================
    // Disconnect
    // =========================================================================

    /**
     * Disconnect from LiveKit and cleanup.
     */
    async disconnect() {
        if (this.room) {
            await this.room.disconnect();
            this.room = null;
        }

        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(track => track.stop());
            this.mediaStream = null;
        }

        if (this.audioContext) {
            await this.audioContext.close();
            this.audioContext = null;
        }

        // Close room on server
        if (this.roomName) {
            try {
                await fetch(`${this.config.gatewayUrl}/livekit/rooms/${this.roomName}`, {
                    method: 'DELETE'
                });
            } catch (e) {
                console.warn('[LiveKit] Failed to close room:', e);
            }
        }

        this.isConnected = false;
        this.isListening = false;
        this.roomName = null;
        this.token = null;

        this._updateState('disconnected');
        console.log('[LiveKit] Disconnected');
    }

    // =========================================================================
    // State Management
    // =========================================================================

    /**
     * Update state and notify listeners.
     */
    _updateState(state) {
        console.log('[LiveKit] State:', state);
        if (this.onStateChange) {
            this.onStateChange(state);
        }
    }

    /**
     * Handle errors.
     */
    _handleError(error) {
        console.error('[LiveKit] Error:', error);
        if (this.onError) {
            this.onError(error);
        }
    }

    /**
     * Get current state.
     */
    getState() {
        if (!this.isConnected) return 'disconnected';
        if (this.isProcessing) return 'processing';
        if (this.isListening) return 'listening';
        return 'idle';
    }
}

// =============================================================================
// Integration with Jarvis Web UI
// =============================================================================

/**
 * Initialize LiveKit for Jarvis Web Interface.
 *
 * Adds LiveKit as an alternative to push-to-talk mode.
 */
async function initLiveKitMode() {
    const client = new LiveKitClient();

    // Check if LiveKit is available
    const available = await client.checkAvailability();
    if (!available) {
        console.log('[LiveKit] Not available, using PTT mode');
        return null;
    }

    // Add mode toggle button
    const modeToggle = document.createElement('button');
    modeToggle.id = 'livekit-mode-toggle';
    modeToggle.className = 'mode-toggle-btn';
    modeToggle.textContent = '🎙️ Enable Always-On Mode';
    modeToggle.onclick = async () => {
        if (client.isConnected) {
            await client.disconnect();
            modeToggle.textContent = '🎙️ Enable Always-On Mode';
            modeToggle.classList.remove('active');
        } else {
            const connected = await client.connect();
            if (connected) {
                modeToggle.textContent = '🔴 Disable Always-On Mode';
                modeToggle.classList.add('active');
            }
        }
    };

    // Find and append to controls
    const controls = document.querySelector('.controls') || document.body;
    controls.appendChild(modeToggle);

    // Set up callbacks
    client.onStateChange = (state) => {
        updateStatusIndicator(state);
    };

    client.onTranscript = (text) => {
        // Update UI with transcript
        const transcriptEl = document.getElementById('transcript');
        if (transcriptEl) transcriptEl.textContent = text;
    };

    client.onResponse = (text) => {
        // Update UI with response
        const responseEl = document.getElementById('response');
        if (responseEl) responseEl.textContent = text;
    };

    client.onWakeWord = (word) => {
        showWakeWordIndicator(word);
    };

    return client;
}

/**
 * Update status indicator based on state.
 */
function updateStatusIndicator(state) {
    const indicator = document.getElementById('livekit-status');
    if (!indicator) return;

    const states = {
        'disconnected': { text: '⚪ Disconnected', class: 'status-disconnected' },
        'connected': { text: '🟢 Connected', class: 'status-connected' },
        'idle': { text: '🟢 Listening...', class: 'status-idle' },
        'listening': { text: '🔵 Hearing you...', class: 'status-listening' },
        'processing': { text: '🟡 Processing...', class: 'status-processing' },
        'responding': { text: '🟣 Speaking...', class: 'status-responding' },
        'interrupted': { text: '🔴 Interrupted', class: 'status-interrupted' }
    };

    const config = states[state] || states['disconnected'];
    indicator.textContent = config.text;
    indicator.className = `livekit-status ${config.class}`;
}

/**
 * Show wake word detection indicator.
 */
function showWakeWordIndicator(word) {
    const indicator = document.createElement('div');
    indicator.className = 'wake-word-indicator';
    indicator.textContent = `"${word}" detected`;

    document.body.appendChild(indicator);

    setTimeout(() => {
        indicator.classList.add('fade-out');
        setTimeout(() => indicator.remove(), 500);
    }, 1500);
}

// Export for use
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { LiveKitClient, initLiveKitMode };
}

// Global export
window.LiveKitClient = LiveKitClient;
window.initLiveKitMode = initLiveKitMode;
