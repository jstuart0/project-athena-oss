/**
 * Music Player Widget for Jarvis Web
 *
 * Provides a UI for browser-based music playback with:
 * - HTML5 Audio element for direct streaming
 * - Now Playing display (track, artist, album art)
 * - Playback controls (play/pause, next, previous)
 * - Volume control
 * - Progress bar with seek support
 * - Queue display
 *
 * Integrates with MusicAssistantClient for MA control
 * and falls back to direct streaming when needed.
 */

class MusicPlayer {
    constructor(config = {}) {
        this.containerId = config.containerId || 'music-player-container';
        this.maClient = config.maClient || null; // MusicAssistantClient instance

        // Audio element for fallback playback (HTTP streams)
        this.audio = new Audio();
        this.audio.preload = 'metadata';

        // Sendspin client for real-time streaming
        this.sendspinClient = null;
        this.sendspinSource = null;
        this.useSendspin = config.useSendspin !== false; // Default to trying Sendspin
        this.sendspinConnected = false;

        // Player state
        this.currentTrack = null;
        this.queue = [];
        this.queueIndex = -1;
        this.isPlaying = false;
        this.volume = parseFloat(localStorage.getItem('jarvis_music_volume') || '0.8');
        this.isMuted = false;
        this.duration = 0;
        this.currentTime = 0;
        this.isExpanded = false;
        this.isVisible = false;

        // Playback mode: 'sendspin' or 'http'
        this.playbackMode = 'http';

        // Sendspin progress tracking
        this.sendspinStartTime = 0;
        this.sendspinProgressInterval = null;

        // UI elements
        this.container = null;
        this.elements = {};

        // Callbacks
        this.onTrackChange = null;
        this.onPlayStateChange = null;
        this.onError = null;

        // Initialize
        this._initAudio();
    }

    /**
     * Initialize and inject the music player into the page
     */
    async init() {
        this._createUI();
        this._bindEvents();
        this.audio.volume = this.volume;

        // Try to initialize Sendspin for real-time streaming
        if (this.useSendspin && window.SendspinClient) {
            await this._initSendspin();
        }

        console.log('[MusicPlayer] Initialized, playback mode:', this.playbackMode);
    }

    /**
     * Initialize Sendspin client for real-time audio streaming
     */
    async _initSendspin() {
        try {
            this.sendspinClient = new SendspinClient({
                volume: this.volume
            });

            // Set up event handlers
            this.sendspinClient.onStateChange = (state, oldState) => {
                console.log('[MusicPlayer] Sendspin state:', state);
                if (state === 'streaming') {
                    this.sendspinConnected = true;
                    this.playbackMode = 'sendspin';
                } else if (state === 'disconnected') {
                    this.sendspinConnected = false;
                    // Fall back to HTTP if disconnected
                    if (this.isPlaying && this.playbackMode === 'sendspin') {
                        console.log('[MusicPlayer] Sendspin disconnected, falling back to HTTP');
                        this.playbackMode = 'http';
                        this._playCurrentTrackHttp();
                    }
                }
            };

            this.sendspinClient.onStreamStart = (info) => {
                console.log('[MusicPlayer] Sendspin stream started:', info);
                this.isPlaying = true;
                this._updatePlayButton();
                if (this.onPlayStateChange) this.onPlayStateChange(true);
                // Start progress tracking for Sendspin, passing stream info for duration
                this._startSendspinProgress(info);
            };

            this.sendspinClient.onStreamEnd = (info) => {
                console.log('[MusicPlayer] Sendspin stream ended');
                // Stop progress tracking
                this._stopSendspinProgress();
                // Auto-advance to next track
                this.next();
            };

            this.sendspinClient.onError = (error) => {
                console.error('[MusicPlayer] Sendspin error:', error);
                // Fall back to HTTP on error
                this.sendspinConnected = false;
                this.playbackMode = 'http';
                if (this.onError) this.onError(error);
            };

            // Pre-initialize audio context (requires user interaction later)
            console.log('[MusicPlayer] Sendspin client ready');

        } catch (error) {
            console.warn('[MusicPlayer] Sendspin initialization failed:', error);
            this.useSendspin = false;
        }
    }

    /**
     * Connect Sendspin for a specific player
     */
    async connectSendspin(playerId) {
        if (!this.sendspinClient) {
            console.warn('[MusicPlayer] Sendspin not available');
            return false;
        }

        try {
            await this.sendspinClient.connect(playerId);
            this.sendspinConnected = true;
            this.playbackMode = 'sendspin';
            console.log('[MusicPlayer] Sendspin connected for player:', playerId);
            return true;
        } catch (error) {
            console.error('[MusicPlayer] Sendspin connection failed:', error);
            this.sendspinConnected = false;
            return false;
        }
    }

    /**
     * Play a track
     * @param {object} track - Track object with uri, name, artist, album_art
     * @param {boolean} addToQueue - Add to queue instead of replacing
     */
    async play(track, addToQueue = false) {
        if (!track) return;

        if (addToQueue) {
            this.queue.push(track);
            console.log('[MusicPlayer] Added to queue:', track.name);
        } else {
            // Replace queue
            this.queue = [track];
            this.queueIndex = 0;
            await this._playCurrentTrack();
        }

        this.show();
    }

    /**
     * Play multiple tracks
     * @param {object[]} tracks - Array of track objects
     * @param {number} startIndex - Index to start playing from
     */
    async playTracks(tracks, startIndex = 0) {
        if (!tracks || tracks.length === 0) return;

        this.queue = [...tracks];
        this.queueIndex = startIndex;
        await this._playCurrentTrack();
        this.show();
    }

    /**
     * Toggle play/pause
     */
    togglePlay() {
        if (this.isPlaying) {
            this.pause();
        } else {
            this.resume();
        }
    }

    /**
     * Pause playback
     */
    pause() {
        this.audio.pause();
        this.isPlaying = false;
        this._updatePlayButton();

        if (this.maClient?.playerId) {
            this.maClient.playerCommand('pause').catch(e => console.warn('[MusicPlayer] MA pause failed:', e));
        }
    }

    /**
     * Resume playback
     */
    resume() {
        this.audio.play().catch(e => console.error('[MusicPlayer] Play failed:', e));
        this.isPlaying = true;
        this._updatePlayButton();

        if (this.maClient?.playerId) {
            this.maClient.playerCommand('play').catch(e => console.warn('[MusicPlayer] MA play failed:', e));
        }
    }

    /**
     * Play next track in queue
     */
    async next() {
        if (this.queueIndex < this.queue.length - 1) {
            this.queueIndex++;
            await this._playCurrentTrack();
        } else {
            console.log('[MusicPlayer] End of queue');
            this.pause();
        }

        if (this.maClient?.playerId) {
            this.maClient.playerCommand('next').catch(e => console.warn('[MusicPlayer] MA next failed:', e));
        }
    }

    /**
     * Play previous track in queue
     */
    async previous() {
        if (this.currentTime > 3 || this.queueIndex === 0) {
            // If more than 3 seconds in, restart current track
            this.audio.currentTime = 0;
        } else if (this.queueIndex > 0) {
            this.queueIndex--;
            await this._playCurrentTrack();
        }

        if (this.maClient?.playerId) {
            this.maClient.playerCommand('previous').catch(e => console.warn('[MusicPlayer] MA previous failed:', e));
        }
    }

    /**
     * Stop playback and clear queue
     */
    stop() {
        // Stop HTTP audio
        this.audio.pause();
        this.audio.currentTime = 0;

        // Stop Sendspin if connected
        if (this.sendspinClient && this.sendspinConnected) {
            // Sendspin will stop when MA stops
            this.sendspinConnected = false;
        }

        // Stop MA player
        if (this.maClient?.playerId) {
            this.maClient.playerCommand('stop').catch(e => console.warn('[MusicPlayer] MA stop failed:', e));
        }

        this.isPlaying = false;
        this.currentTrack = null;
        this.queue = [];
        this.queueIndex = -1;
        this.playbackMode = 'http';
        this._updatePlayButton();
        this._updateNowPlaying();
        this.hide();
    }

    /**
     * Set volume
     * @param {number} level - Volume level 0-1
     */
    setVolume(level) {
        this.volume = Math.max(0, Math.min(1, level));
        this.audio.volume = this.volume;
        this.isMuted = this.volume === 0;
        localStorage.setItem('jarvis_music_volume', this.volume.toString());
        this._updateVolumeUI();

        // Update Sendspin volume
        if (this.sendspinClient) {
            this.sendspinClient.setVolume(this.volume);
        }

        if (this.maClient?.playerId) {
            this.maClient.setVolume(Math.round(this.volume * 100)).catch(e => console.warn('[MusicPlayer] MA volume failed:', e));
        }
    }

    /**
     * Toggle mute
     */
    toggleMute() {
        if (this.isMuted) {
            this.audio.volume = this.volume;
            this.isMuted = false;
        } else {
            this.audio.volume = 0;
            this.isMuted = true;
        }
        this._updateVolumeUI();
    }

    /**
     * Seek to position
     * @param {number} time - Time in seconds
     */
    seek(time) {
        const seekTime = Math.max(0, Math.min(time, this.duration || time));
        console.log('[MusicPlayer] Seeking to:', seekTime, 'mode:', this.playbackMode);

        if (this.playbackMode === 'sendspin') {
            // For Sendspin streaming, update local tracking
            // Note: Actual seeking in the stream may not be supported
            this.currentTime = seekTime;
            if (this.sendspinStartTime) {
                // Adjust start time so progress tracking reflects the seek
                this.sendspinStartTime = Date.now() - (seekTime * 1000);
            }
            this._updateProgress();
            // TODO: If MA supports seeking, send seek command here
        } else if (this.audio.duration) {
            // HTTP mode - direct audio element seek
            this.audio.currentTime = seekTime;
        }
    }

    /**
     * Show the player (shows tab, optionally opens panel)
     */
    show() {
        if (this.container) {
            this.container.classList.add('visible');
            this.isVisible = true;
            // Auto-open panel when music starts
            this.openPanel();
        }
    }

    /**
     * Hide the player completely (stops music)
     */
    hide() {
        if (this.container) {
            this.container.classList.remove('visible');
            this.container.classList.remove('panel-open');
            this.isVisible = false;
            this.isPanelOpen = false;
        }
    }

    /**
     * Open the side panel
     */
    openPanel() {
        if (this.container) {
            this.container.classList.add('panel-open');
            this.isPanelOpen = true;
        }
    }

    /**
     * Close the side panel (minimize to tab)
     */
    closePanel() {
        if (this.container) {
            this.container.classList.remove('panel-open');
            this.isPanelOpen = false;
        }
    }

    /**
     * Toggle panel open/closed
     */
    togglePanel() {
        if (this.isPanelOpen) {
            this.closePanel();
        } else {
            this.openPanel();
        }
    }

    /**
     * Toggle expanded view (queue)
     */
    toggleExpanded() {
        this.isExpanded = !this.isExpanded;
        if (this.container) {
            this.container.classList.toggle('expanded', this.isExpanded);
        }
    }

    /**
     * Get current playback state
     */
    getState() {
        return {
            isPlaying: this.isPlaying,
            currentTrack: this.currentTrack,
            queue: this.queue,
            queueIndex: this.queueIndex,
            volume: this.volume,
            currentTime: this.currentTime,
            duration: this.duration
        };
    }

    // ==================== Private Methods ====================

    _initAudio() {
        this.audio.addEventListener('play', () => {
            this.isPlaying = true;
            this._updatePlayButton();
            this._updateMediaSessionPlaybackState('playing');
            if (this.onPlayStateChange) this.onPlayStateChange(true);
        });

        this.audio.addEventListener('pause', () => {
            this.isPlaying = false;
            this._updatePlayButton();
            this._updateMediaSessionPlaybackState('paused');
            if (this.onPlayStateChange) this.onPlayStateChange(false);
        });

        this.audio.addEventListener('ended', () => {
            this.next();
        });

        this.audio.addEventListener('timeupdate', () => {
            this.currentTime = this.audio.currentTime;
            this._updateProgress();
        });

        this.audio.addEventListener('durationchange', () => {
            this.duration = this.audio.duration || 0;
            this._updateProgress();
        });

        this.audio.addEventListener('error', (e) => {
            console.error('[MusicPlayer] Audio error:', e);
            if (this.onError) {
                this.onError(e);
            }
            // Try next track on error
            setTimeout(() => this.next(), 1000);
        });

        this.audio.addEventListener('loadedmetadata', () => {
            this.duration = this.audio.duration || 0;
            this._updateProgress();
        });

        // Initialize Media Session API for background playback on mobile
        this._initMediaSession();
    }

    /**
     * Initialize Media Session API for background playback and lock screen controls
     */
    _initMediaSession() {
        if (!('mediaSession' in navigator)) {
            console.log('[MusicPlayer] Media Session API not supported');
            return;
        }

        console.log('[MusicPlayer] Initializing Media Session API');

        // Set up action handlers
        navigator.mediaSession.setActionHandler('play', () => this.play());
        navigator.mediaSession.setActionHandler('pause', () => this.pause());
        navigator.mediaSession.setActionHandler('previoustrack', () => this.previous());
        navigator.mediaSession.setActionHandler('nexttrack', () => this.next());
        navigator.mediaSession.setActionHandler('seekbackward', (details) => {
            const skipTime = details.seekOffset || 10;
            this.seek(Math.max(0, this.currentTime - skipTime));
        });
        navigator.mediaSession.setActionHandler('seekforward', (details) => {
            const skipTime = details.seekOffset || 10;
            this.seek(Math.min(this.duration, this.currentTime + skipTime));
        });
        navigator.mediaSession.setActionHandler('seekto', (details) => {
            if (details.seekTime !== undefined) {
                this.seek(details.seekTime);
            }
        });
        navigator.mediaSession.setActionHandler('stop', () => this.stop());
    }

    /**
     * Update Media Session metadata for current track
     */
    _updateMediaSession() {
        if (!('mediaSession' in navigator) || !this.currentTrack) {
            return;
        }

        const artwork = [];
        if (this.currentTrack.album_art) {
            artwork.push({
                src: this.currentTrack.album_art,
                sizes: '512x512',
                type: 'image/jpeg'
            });
        }

        navigator.mediaSession.metadata = new MediaMetadata({
            title: this.currentTrack.name || 'Unknown Track',
            artist: this.currentTrack.artist || 'Unknown Artist',
            album: this.currentTrack.album || '',
            artwork: artwork
        });

        console.log('[MusicPlayer] Media Session metadata updated:', this.currentTrack.name);
    }

    /**
     * Update Media Session playback state
     */
    _updateMediaSessionPlaybackState(state) {
        if (!('mediaSession' in navigator)) {
            return;
        }
        navigator.mediaSession.playbackState = state;
    }

    async _playCurrentTrack() {
        if (this.queueIndex < 0 || this.queueIndex >= this.queue.length) {
            console.warn('[MusicPlayer] Invalid queue index');
            return;
        }

        this.currentTrack = this.queue[this.queueIndex];
        console.log('[MusicPlayer] Playing:', this.currentTrack?.name);

        // Update UI
        this._updateNowPlaying();

        // Try Sendspin for full track streaming (priority over preview URLs)
        console.log('[MusicPlayer] Checking Sendspin conditions:', {
            useSendspin: this.useSendspin,
            hasSendspinClient: !!this.sendspinClient,
            hasTrackUri: !!this.currentTrack?.uri,
            trackUri: this.currentTrack?.uri
        });
        if (this.useSendspin && this.sendspinClient && this.currentTrack?.uri) {
            console.log('[MusicPlayer] Attempting Sendspin playback...');
            const sendspinSuccess = await this._tryPlayViaSendspin();
            if (sendspinSuccess) {
                if (this.onTrackChange) this.onTrackChange(this.currentTrack);
                return;
            }
            console.log('[MusicPlayer] Sendspin failed, falling back to HTTP');
        } else {
            console.log('[MusicPlayer] Sendspin conditions not met, using HTTP');
        }

        // Fall back to HTTP streaming (preview URLs)
        console.log('[MusicPlayer] Falling back to HTTP playback');
        await this._playCurrentTrackHttp();

        if (this.onTrackChange) {
            this.onTrackChange(this.currentTrack);
        }
    }

    /**
     * Try to play via Sendspin for full track streaming
     * This connects to MA as a Sendspin player and receives audio directly
     */
    async _tryPlayViaSendspin() {
        try {
            // Step 1: Connect Sendspin if not already connected
            console.log('[MusicPlayer] _tryPlayViaSendspin called, sendspinConnected:', this.sendspinConnected);
            if (!this.sendspinConnected) {
                console.log('[MusicPlayer] Connecting Sendspin for full audio streaming...');

                // Initialize audio context (requires user interaction)
                console.log('[MusicPlayer] Initializing audio context...');
                await this.sendspinClient.initAudio();
                console.log('[MusicPlayer] Audio context initialized');

                // Connect to Sendspin proxy - this authenticates and sends client/hello
                console.log('[MusicPlayer] Connecting to Sendspin proxy...');
                await this.sendspinClient.connect();
                console.log('[MusicPlayer] Sendspin connect() returned, state:', this.sendspinClient.state);

                // Wait for server/hello to confirm player registration
                console.log('[MusicPlayer] Waiting for player registration...');
                const registered = await this._waitForSendspinRegistration(5000);
                console.log('[MusicPlayer] Registration result:', registered, 'activeRoles:', this.sendspinClient.activeRoles);
                if (!registered) {
                    console.warn('[MusicPlayer] Sendspin player registration timeout');
                    return false;
                }

                this.sendspinConnected = true;
                this.playbackMode = 'sendspin';
                console.log('[MusicPlayer] Sendspin player registered successfully');
            }

            // Step 2: Use MA API to play media to our Sendspin player
            // The Sendspin client_id is our player_id in MA
            const playerId = this.sendspinClient.clientId;
            console.log('[MusicPlayer] Playing to Sendspin player:', playerId);

            // Play via MA API - audio will stream to our Sendspin connection
            const response = await fetch(`${window.location.origin}/api/music/play`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    player_id: playerId,
                    uri: this.currentTrack.uri,
                    radio_mode: true
                })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                console.warn('[MusicPlayer] MA play request failed:', error);
                return false;
            }

            this.isPlaying = true;
            this._updatePlayButton();

            // Dispatch event for full streaming (not preview)
            window.dispatchEvent(new CustomEvent('musicPlaybackFull', {
                detail: { track: this.currentTrack?.name, mode: 'sendspin' }
            }));

            return true;

        } catch (error) {
            console.error('[MusicPlayer] Sendspin playback failed:', error);
            this.sendspinConnected = false;
            this.playbackMode = 'http';
            return false;
        }
    }

    /**
     * Wait for Sendspin player registration (server/hello response)
     */
    _waitForSendspinRegistration(timeoutMs = 5000) {
        return new Promise((resolve) => {
            const startTime = Date.now();

            const checkRegistration = () => {
                // Check if we got server/hello (activeRoles will be populated)
                if (this.sendspinClient.activeRoles && this.sendspinClient.activeRoles.length > 0) {
                    resolve(true);
                    return;
                }

                // Check if we're in ready or streaming state
                if (this.sendspinClient.state === 'ready' ||
                    this.sendspinClient.state === 'streaming' ||
                    this.sendspinClient.state === 'syncing') {
                    resolve(true);
                    return;
                }

                // Timeout check
                if (Date.now() - startTime > timeoutMs) {
                    resolve(false);
                    return;
                }

                // Keep checking
                setTimeout(checkRegistration, 100);
            };

            checkRegistration();
        });
    }

    /**
     * Play current track using HTTP streaming (fallback mode)
     */
    async _playCurrentTrackHttp() {
        if (!this.currentTrack) return;

        // Get stream URL - prioritize preview URLs (DRM-free) over stream proxy
        let streamUrl = null;
        let isPreview = false;

        // 1. Check for Spotify preview URL (30-second DRM-free MP3)
        if (this.currentTrack.preview_url) {
            streamUrl = this.currentTrack.preview_url;
            isPreview = true;
            console.log('[MusicPlayer] Using preview URL (30s clip)');
        }
        // 2. Check metadata.preview (from MA search results)
        else if (this.currentTrack.metadata?.preview) {
            streamUrl = this.currentTrack.metadata.preview;
            isPreview = true;
            console.log('[MusicPlayer] Using metadata.preview URL (30s clip)');
        }
        // 3. Check direct stream_url
        else if (this.currentTrack.stream_url) {
            streamUrl = this.currentTrack.stream_url;
        }
        // 4. Try MA client or gateway proxy (may not work for DRM content)
        else if (this.currentTrack.uri) {
            if (this.maClient) {
                streamUrl = this.maClient.getStreamUrl(this.currentTrack.uri);
            } else {
                streamUrl = `${window.location.origin}/api/music/stream/${encodeURIComponent(this.currentTrack.uri)}`;
            }
        }

        console.log('[MusicPlayer] HTTP stream URL:', streamUrl, isPreview ? '(preview)' : '');

        if (streamUrl) {
            this.audio.src = streamUrl;
            this.audio.volume = this.volume;
            console.log('[MusicPlayer] Audio volume:', this.volume, 'muted:', this.audio.muted);
            try {
                const playPromise = this.audio.play();
                if (playPromise) {
                    await playPromise;
                }
                this.isPlaying = true;
                this.playbackMode = 'http';
                console.log('[MusicPlayer] HTTP playback started successfully');

                // Notify if this is a preview
                if (isPreview) {
                    window.dispatchEvent(new CustomEvent('musicPlaybackPreview', {
                        detail: { track: this.currentTrack?.name, duration: 30 }
                    }));
                }
            } catch (e) {
                console.error('[MusicPlayer] HTTP playback failed:', e.name, e.message);
                // Dispatch custom event for UI feedback
                window.dispatchEvent(new CustomEvent('musicPlaybackError', {
                    detail: { error: e.message, track: this.currentTrack?.name }
                }));
                // Auto-advance on error after delay
                setTimeout(() => this.next(), 2000);
            }
        } else {
            console.error('[MusicPlayer] No stream URL available for track');
            setTimeout(() => this.next(), 1000);
        }
    }

    _createUI() {
        // Check if container already exists
        let container = document.getElementById(this.containerId);
        if (!container) {
            container = document.createElement('div');
            container.id = this.containerId;
            document.body.appendChild(container);
        }

        this.container = container;
        this.container.className = 'music-player';

        this.container.innerHTML = `
            <!-- Minimized Tab (shows when player is hidden but music active) -->
            <div class="music-player-tab" id="music-tab">
                <div class="music-tab-art" id="music-tab-art">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M9 18V5l12-2v13"></path>
                        <circle cx="6" cy="18" r="3"></circle>
                        <circle cx="18" cy="16" r="3"></circle>
                    </svg>
                </div>
                <div class="music-tab-indicator" id="music-tab-indicator">
                    <div class="music-tab-bars">
                        <span></span><span></span><span></span>
                    </div>
                </div>
            </div>

            <!-- Main Player Panel -->
            <div class="music-player-panel" id="music-panel">
                <div class="music-player-inner">
                    <!-- Header with close/minimize -->
                    <div class="music-player-header">
                        <span class="music-player-header-title">Now Playing</span>
                        <button class="music-btn music-btn-minimize" id="music-minimize" title="Minimize">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="9 18 15 12 9 6"></polyline>
                            </svg>
                        </button>
                    </div>

                    <!-- Album Art (larger for side panel) -->
                    <div class="music-player-artwork" id="music-art">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                            <path d="M9 18V5l12-2v13"></path>
                            <circle cx="6" cy="18" r="3"></circle>
                            <circle cx="18" cy="16" r="3"></circle>
                        </svg>
                    </div>

                    <!-- Track Info -->
                    <div class="music-player-info">
                        <div class="music-player-title" id="music-title">No track playing</div>
                        <div class="music-player-artist" id="music-artist">-</div>
                    </div>

                    <!-- Progress Bar -->
                    <div class="music-player-progress">
                        <input type="range" id="music-progress" min="0" max="100" value="0" class="music-slider progress-slider">
                        <div class="music-time-row">
                            <span class="music-time" id="music-time-current">0:00</span>
                            <span class="music-time" id="music-time-duration">0:00</span>
                        </div>
                    </div>

                    <!-- Playback Controls -->
                    <div class="music-player-controls">
                        <button class="music-btn" id="music-prev" title="Previous">
                            <svg viewBox="0 0 24 24" fill="currentColor">
                                <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/>
                            </svg>
                        </button>
                        <button class="music-btn music-btn-play" id="music-play" title="Play/Pause">
                            <svg viewBox="0 0 24 24" fill="currentColor" class="play-icon">
                                <path d="M8 5v14l11-7z"/>
                            </svg>
                            <svg viewBox="0 0 24 24" fill="currentColor" class="pause-icon" style="display:none;">
                                <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
                            </svg>
                        </button>
                        <button class="music-btn" id="music-next" title="Next">
                            <svg viewBox="0 0 24 24" fill="currentColor">
                                <path d="M6 18l8.5-6L6 6v12zm2 0V6l8.5 6L8 18zm8 0h2V6h-2v12z"/>
                            </svg>
                        </button>
                    </div>

                    <!-- Volume Control -->
                    <div class="music-player-volume">
                        <button class="music-btn music-btn-volume" id="music-mute" title="Mute">
                            <svg viewBox="0 0 24 24" fill="currentColor" class="volume-icon">
                                <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/>
                            </svg>
                            <svg viewBox="0 0 24 24" fill="currentColor" class="muted-icon" style="display:none;">
                                <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/>
                            </svg>
                        </button>
                        <input type="range" id="music-volume" min="0" max="100" value="80" class="music-slider volume-slider">
                    </div>

                    <!-- Queue Toggle -->
                    <button class="music-btn music-btn-queue" id="music-expand" title="Show Queue">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="8" y1="6" x2="21" y2="6"></line>
                            <line x1="8" y1="12" x2="21" y2="12"></line>
                            <line x1="8" y1="18" x2="21" y2="18"></line>
                            <line x1="3" y1="6" x2="3.01" y2="6"></line>
                            <line x1="3" y1="12" x2="3.01" y2="12"></line>
                            <line x1="3" y1="18" x2="3.01" y2="18"></line>
                        </svg>
                        <span>Queue</span>
                    </button>

                    <!-- Expanded View (Queue) -->
                    <div class="music-player-expanded" id="music-expanded">
                        <div class="music-queue-header">
                            <span>Up Next</span>
                            <span id="music-queue-count">0 tracks</span>
                        </div>
                        <div class="music-queue-list" id="music-queue">
                            <!-- Queue items populated dynamically -->
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Store element references
        this.elements = {
            // Tab elements (minimized indicator)
            tab: document.getElementById('music-tab'),
            tabArt: document.getElementById('music-tab-art'),
            tabIndicator: document.getElementById('music-tab-indicator'),
            // Panel elements
            panel: document.getElementById('music-panel'),
            minimizeBtn: document.getElementById('music-minimize'),
            // Player elements
            art: document.getElementById('music-art'),
            title: document.getElementById('music-title'),
            artist: document.getElementById('music-artist'),
            playBtn: document.getElementById('music-play'),
            prevBtn: document.getElementById('music-prev'),
            nextBtn: document.getElementById('music-next'),
            muteBtn: document.getElementById('music-mute'),
            volumeSlider: document.getElementById('music-volume'),
            progressSlider: document.getElementById('music-progress'),
            timeCurrent: document.getElementById('music-time-current'),
            timeDuration: document.getElementById('music-time-duration'),
            expandBtn: document.getElementById('music-expand'),
            expanded: document.getElementById('music-expanded'),
            queue: document.getElementById('music-queue'),
            queueCount: document.getElementById('music-queue-count')
        };

        // Track panel state
        this.isPanelOpen = false;

        // Add CSS if not already present
        this._injectStyles();
    }

    _bindEvents() {
        // Tab click - open panel
        this.elements.tab.addEventListener('click', () => this.openPanel());

        // Minimize button - close panel
        this.elements.minimizeBtn.addEventListener('click', () => this.closePanel());

        // Play/Pause
        this.elements.playBtn.addEventListener('click', () => this.togglePlay());

        // Previous/Next
        this.elements.prevBtn.addEventListener('click', () => this.previous());
        this.elements.nextBtn.addEventListener('click', () => this.next());

        // Volume
        this.elements.muteBtn.addEventListener('click', () => this.toggleMute());
        this.elements.volumeSlider.addEventListener('input', (e) => {
            this.setVolume(e.target.value / 100);
        });

        // Progress
        this.elements.progressSlider.addEventListener('input', (e) => {
            const seekTime = (e.target.value / 100) * this.duration;
            this.seek(seekTime);
        });

        // Expand/Collapse queue
        this.elements.expandBtn.addEventListener('click', () => this.toggleExpanded());

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (!this.isVisible) return;
            if (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA') return;

            switch (e.key) {
                case ' ':
                    e.preventDefault();
                    this.togglePlay();
                    break;
                case 'ArrowLeft':
                    if (e.shiftKey) this.previous();
                    else this.seek(this.currentTime - 10);
                    break;
                case 'ArrowRight':
                    if (e.shiftKey) this.next();
                    else this.seek(this.currentTime + 10);
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this.setVolume(this.volume + 0.1);
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.setVolume(this.volume - 0.1);
                    break;
                case 'm':
                    this.toggleMute();
                    break;
            }
        });
    }

    _updateNowPlaying() {
        const defaultMusicIcon = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M9 18V5l12-2v13"></path>
                <circle cx="6" cy="18" r="3"></circle>
                <circle cx="18" cy="16" r="3"></circle>
            </svg>
        `;

        if (!this.currentTrack) {
            this.elements.title.textContent = 'No track playing';
            this.elements.artist.textContent = '-';
            this.elements.art.innerHTML = defaultMusicIcon;
            // Also update tab art
            if (this.elements.tabArt) {
                this.elements.tabArt.innerHTML = defaultMusicIcon;
            }
            return;
        }

        this.elements.title.textContent = this.currentTrack.name || 'Unknown Track';
        this.elements.artist.textContent = this.currentTrack.artist || this.currentTrack.artists?.[0]?.name || 'Unknown Artist';

        // Update album art (panel and tab)
        const imgUrl = this.currentTrack.album_art || this.currentTrack.image?.url;
        console.log('[MusicPlayer] Album art URL:', imgUrl);
        if (imgUrl) {
            this.elements.art.innerHTML = `<img src="${imgUrl}" alt="Album Art" onerror="this.style.display='none'">`;
            // Update tab art too
            if (this.elements.tabArt) {
                this.elements.tabArt.innerHTML = `<img src="${imgUrl}" alt="Album Art" onerror="this.parentElement.innerHTML='${defaultMusicIcon.replace(/'/g, "\\'")}'">`;
            }
        } else {
            // Keep default music icon if no album art
            this.elements.art.innerHTML = defaultMusicIcon;
            if (this.elements.tabArt) {
                this.elements.tabArt.innerHTML = defaultMusicIcon;
            }
        }

        // Update queue display
        this._updateQueueDisplay();

        // Update Media Session for lock screen / background playback
        this._updateMediaSession();
    }

    _updatePlayButton() {
        const playIcon = this.elements.playBtn.querySelector('.play-icon');
        const pauseIcon = this.elements.playBtn.querySelector('.pause-icon');

        if (this.isPlaying) {
            playIcon.style.display = 'none';
            pauseIcon.style.display = 'block';
            // Animate the tab indicator bars
            if (this.elements.tabIndicator) {
                this.elements.tabIndicator.classList.remove('paused');
            }
        } else {
            playIcon.style.display = 'block';
            pauseIcon.style.display = 'none';
            // Pause the tab indicator bars
            if (this.elements.tabIndicator) {
                this.elements.tabIndicator.classList.add('paused');
            }
        }
    }

    _updateProgress() {
        const percent = this.duration > 0 ? (this.currentTime / this.duration) * 100 : 0;

        // Log progress update periodically (every 5 seconds) for debugging
        if (!this._lastProgressLog || Date.now() - this._lastProgressLog > 5000) {
            this._lastProgressLog = Date.now();
            console.log('[MusicPlayer] Progress update:', {
                currentTime: this.currentTime?.toFixed(1),
                duration: this.duration?.toFixed(1),
                percent: percent?.toFixed(1),
                playbackMode: this.playbackMode
            });
        }

        if (this.elements.progressSlider) {
            this.elements.progressSlider.value = percent;
            // Also set the style for visual feedback on range inputs
            this.elements.progressSlider.style.setProperty('--progress', `${percent}%`);
        }
        if (this.elements.timeCurrent) {
            this.elements.timeCurrent.textContent = this._formatTime(this.currentTime);
        }
        if (this.elements.timeDuration) {
            this.elements.timeDuration.textContent = this._formatTime(this.duration);
        }
    }

    /**
     * Start progress tracking for Sendspin playback
     * @param {object} streamInfo - Stream info from Sendspin (may contain duration)
     */
    _startSendspinProgress(streamInfo = null) {
        this._stopSendspinProgress(); // Clear any existing interval

        this.sendspinStartTime = Date.now();
        this.currentTime = 0;

        // Get duration from multiple sources (priority order)
        console.log('[MusicPlayer] Track metadata for duration:', {
            track: this.currentTrack?.name,
            trackDuration: this.currentTrack?.duration,
            trackDurationMs: this.currentTrack?.duration_ms,
            metadataDuration: this.currentTrack?.metadata?.duration,
            streamInfoDuration: streamInfo?.duration,
            streamInfoMediaItem: streamInfo?.mediaItem
        });

        let foundDuration = false;

        // 1. Try stream info duration (most reliable when streaming)
        if (streamInfo?.duration && streamInfo.duration > 0) {
            // If duration > 1000, assume milliseconds
            this.duration = streamInfo.duration > 1000 ? streamInfo.duration / 1000 : streamInfo.duration;
            foundDuration = true;
            console.log('[MusicPlayer] Duration from streamInfo.duration:', this.duration, 'seconds');
        }
        // 2. Try stream info mediaItem (MA may nest it there)
        else if (streamInfo?.mediaItem?.duration && streamInfo.mediaItem.duration > 0) {
            this.duration = streamInfo.mediaItem.duration > 1000 ?
                streamInfo.mediaItem.duration / 1000 : streamInfo.mediaItem.duration;
            foundDuration = true;
            console.log('[MusicPlayer] Duration from streamInfo.mediaItem:', this.duration, 'seconds');
        }
        // 3. Try track metadata duration (from MA search results - usually in seconds already)
        else if (this.currentTrack?.duration && this.currentTrack.duration > 0) {
            // MA returns duration in seconds for search results
            this.duration = this.currentTrack.duration > 1000 ?
                this.currentTrack.duration / 1000 : this.currentTrack.duration;
            foundDuration = true;
            console.log('[MusicPlayer] Duration from track.duration:', this.duration, 'seconds');
        }
        // 4. Try track duration_ms (explicit milliseconds field)
        else if (this.currentTrack?.duration_ms && this.currentTrack.duration_ms > 0) {
            this.duration = this.currentTrack.duration_ms / 1000;
            foundDuration = true;
            console.log('[MusicPlayer] Duration from track.duration_ms:', this.duration, 'seconds');
        }
        // 5. Try metadata nested duration
        else if (this.currentTrack?.metadata?.duration && this.currentTrack.metadata.duration > 0) {
            const dur = this.currentTrack.metadata.duration;
            this.duration = dur > 1000 ? dur / 1000 : dur;
            foundDuration = true;
            console.log('[MusicPlayer] Duration from metadata.duration:', this.duration, 'seconds');
        }
        // 6. Default to 4 minutes if no duration available
        else {
            this.duration = 240;
            console.log('[MusicPlayer] No duration found, using default 4 minutes');
        }

        // Safety check - if duration is unreasonably large, cap it
        if (this.duration > 3600) {
            console.warn('[MusicPlayer] Duration too large, capping at 1 hour');
            this.duration = 3600;
        }

        console.log('[MusicPlayer] Starting Sendspin progress tracking, duration:', this.duration.toFixed(1), 'seconds');
        this._updateProgress();

        // Update progress every 250ms
        this.sendspinProgressInterval = setInterval(() => {
            if (this.isPlaying && this.playbackMode === 'sendspin') {
                this.currentTime = (Date.now() - this.sendspinStartTime) / 1000;
                this._updateProgress();
            }
        }, 250);
    }

    /**
     * Stop progress tracking for Sendspin playback
     */
    _stopSendspinProgress() {
        if (this.sendspinProgressInterval) {
            clearInterval(this.sendspinProgressInterval);
            this.sendspinProgressInterval = null;
        }
    }

    _updateVolumeUI() {
        const volumeIcon = this.elements.muteBtn.querySelector('.volume-icon');
        const mutedIcon = this.elements.muteBtn.querySelector('.muted-icon');

        if (this.isMuted) {
            volumeIcon.style.display = 'none';
            mutedIcon.style.display = 'block';
        } else {
            volumeIcon.style.display = 'block';
            mutedIcon.style.display = 'none';
        }

        this.elements.volumeSlider.value = this.isMuted ? 0 : this.volume * 100;
    }

    _updateQueueDisplay() {
        if (!this.elements.queue) return;

        this.elements.queueCount.textContent = `${this.queue.length} track${this.queue.length !== 1 ? 's' : ''}`;

        this.elements.queue.innerHTML = this.queue.map((track, index) => `
            <div class="music-queue-item ${index === this.queueIndex ? 'active' : ''}" data-index="${index}">
                <div class="queue-item-num">${index + 1}</div>
                <div class="queue-item-info">
                    <div class="queue-item-title">${track.name || 'Unknown Track'}</div>
                    <div class="queue-item-artist">${track.artist || track.artists?.[0]?.name || 'Unknown Artist'}</div>
                </div>
            </div>
        `).join('');

        // Add click handlers for queue items
        this.elements.queue.querySelectorAll('.music-queue-item').forEach(item => {
            item.addEventListener('click', () => {
                const index = parseInt(item.dataset.index, 10);
                if (index !== this.queueIndex) {
                    this.queueIndex = index;
                    this._playCurrentTrack();
                }
            });
        });
    }

    _formatTime(seconds) {
        if (!seconds || isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }

    _injectStyles() {
        if (document.getElementById('music-player-styles')) return;

        const style = document.createElement('style');
        style.id = 'music-player-styles';
        style.textContent = `
            /* ==================== Side-Sliding Music Player ==================== */

            .music-player {
                position: fixed;
                top: 0;
                right: 0;
                bottom: 100px; /* Leave space for voice input area */
                z-index: 1000;
                display: none;
                pointer-events: none;
            }

            .music-player.visible {
                display: flex;
                align-items: stretch;
                pointer-events: auto;
            }

            /* ==================== Minimized Tab ==================== */

            .music-player-tab {
                position: relative;
                width: 48px;
                background: var(--bg-secondary, #1e293b);
                border-radius: 12px 0 0 12px;
                cursor: pointer;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 8px;
                padding: 12px 8px;
                box-shadow: -4px 0 20px rgba(0, 0, 0, 0.3);
                transition: background 0.2s, width 0.2s;
                pointer-events: auto;
            }

            .music-player-tab:hover {
                background: var(--bg-tertiary, #334155);
                width: 52px;
            }

            .music-tab-art {
                width: 32px;
                height: 32px;
                border-radius: 6px;
                background: var(--bg-tertiary, #334155);
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
                flex-shrink: 0;
            }

            .music-tab-art img {
                width: 100%;
                height: 100%;
                object-fit: cover;
            }

            .music-tab-art svg {
                width: 18px;
                height: 18px;
                color: var(--text-muted, #64748b);
            }

            /* Animated bars indicator */
            .music-tab-indicator {
                display: flex;
                align-items: flex-end;
                justify-content: center;
                height: 16px;
                gap: 2px;
            }

            .music-tab-bars {
                display: flex;
                align-items: flex-end;
                gap: 2px;
                height: 100%;
            }

            .music-tab-bars span {
                width: 3px;
                background: var(--accent, #3b82f6);
                border-radius: 1px;
                animation: musicBar 0.8s ease-in-out infinite;
            }

            .music-tab-bars span:nth-child(1) {
                height: 8px;
                animation-delay: 0s;
            }

            .music-tab-bars span:nth-child(2) {
                height: 12px;
                animation-delay: 0.2s;
            }

            .music-tab-bars span:nth-child(3) {
                height: 6px;
                animation-delay: 0.4s;
            }

            @keyframes musicBar {
                0%, 100% { transform: scaleY(0.5); }
                50% { transform: scaleY(1); }
            }

            /* Pause animation when music is paused */
            .music-tab-indicator.paused .music-tab-bars span {
                animation-play-state: paused;
                opacity: 0.5;
            }

            /* Hide tab when panel is open */
            .music-player.panel-open .music-player-tab {
                display: none;
            }

            /* ==================== Main Panel ==================== */

            .music-player-panel {
                width: 320px;
                max-width: 90vw;
                background: var(--bg-secondary, #1e293b);
                box-shadow: -4px 0 30px rgba(0, 0, 0, 0.4);
                transform: translateX(100%);
                transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                overflow-y: auto;
                overflow-x: hidden;
                display: flex;
                flex-direction: column;
            }

            .music-player.panel-open .music-player-panel {
                transform: translateX(0);
            }

            .music-player-inner {
                padding: 16px;
                display: flex;
                flex-direction: column;
                gap: 16px;
                flex: 1;
            }

            /* ==================== Panel Header ==================== */

            .music-player-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
            }

            .music-player-header-title {
                font-size: 14px;
                font-weight: 600;
                color: var(--text-secondary, #94a3b8);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .music-btn-minimize {
                width: 32px;
                height: 32px;
            }

            .music-btn-minimize svg {
                width: 18px;
                height: 18px;
            }

            /* ==================== Album Artwork ==================== */

            .music-player-artwork {
                width: 100%;
                aspect-ratio: 1;
                max-width: 280px;
                margin: 0 auto;
                border-radius: 12px;
                background: var(--bg-tertiary, #334155);
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            }

            .music-player-artwork img {
                width: 100%;
                height: 100%;
                object-fit: cover;
            }

            .music-player-artwork svg {
                width: 64px;
                height: 64px;
                color: var(--text-muted, #64748b);
            }

            /* ==================== Track Info ==================== */

            .music-player-info {
                text-align: center;
            }

            .music-player-title {
                font-size: 18px;
                font-weight: 600;
                color: var(--text-primary, #f8fafc);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                margin-bottom: 4px;
            }

            .music-player-artist {
                font-size: 14px;
                color: var(--text-secondary, #94a3b8);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            /* ==================== Progress Bar ==================== */

            .music-player-progress {
                display: flex;
                flex-direction: column;
                gap: 4px;
            }

            .progress-slider {
                width: 100%;
                height: 6px;
                --progress: 0%;
                background: linear-gradient(to right,
                    var(--accent, #3b82f6) 0%,
                    var(--accent, #3b82f6) var(--progress),
                    var(--bg-tertiary, #334155) var(--progress),
                    var(--bg-tertiary, #334155) 100%);
                border-radius: 3px;
            }

            .music-time-row {
                display: flex;
                justify-content: space-between;
            }

            .music-time {
                font-size: 11px;
                color: var(--text-muted, #64748b);
            }

            /* ==================== Playback Controls ==================== */

            .music-player-controls {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 16px;
            }

            .music-btn {
                width: 44px;
                height: 44px;
                border: none;
                background: transparent;
                border-radius: 50%;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                color: var(--text-primary, #f8fafc);
                transition: background 0.2s, transform 0.1s;
            }

            .music-btn:hover {
                background: var(--bg-tertiary, #334155);
            }

            .music-btn:active {
                transform: scale(0.95);
            }

            .music-btn svg {
                width: 24px;
                height: 24px;
            }

            .music-btn-play {
                width: 56px;
                height: 56px;
                background: var(--accent, #3b82f6);
                color: white;
            }

            .music-btn-play:hover {
                background: var(--accent-hover, #2563eb);
            }

            .music-btn-play svg {
                width: 28px;
                height: 28px;
            }

            /* ==================== Volume Control ==================== */

            .music-player-volume {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 0 8px;
            }

            .music-btn-volume {
                width: 36px;
                height: 36px;
                flex-shrink: 0;
            }

            .music-btn-volume svg {
                width: 20px;
                height: 20px;
            }

            .music-slider {
                -webkit-appearance: none;
                height: 4px;
                border-radius: 2px;
                outline: none;
                background: var(--bg-tertiary, #334155);
            }

            .music-slider::-webkit-slider-thumb {
                -webkit-appearance: none;
                width: 14px;
                height: 14px;
                background: var(--accent, #3b82f6);
                border-radius: 50%;
                cursor: pointer;
                transition: transform 0.1s;
            }

            .music-slider::-webkit-slider-thumb:hover {
                transform: scale(1.2);
            }

            .volume-slider {
                flex: 1;
            }

            /* ==================== Queue Toggle ==================== */

            .music-btn-queue {
                width: 100%;
                height: auto;
                border-radius: 8px;
                padding: 10px;
                gap: 8px;
                font-size: 13px;
                background: var(--bg-tertiary, #334155);
                color: var(--text-secondary, #94a3b8);
            }

            .music-btn-queue svg {
                width: 18px;
                height: 18px;
            }

            .music-btn-queue:hover {
                background: var(--border, #475569);
                color: var(--text-primary, #f8fafc);
            }

            /* ==================== Queue (Expanded) ==================== */

            .music-player-expanded {
                max-height: 0;
                overflow: hidden;
                transition: max-height 0.3s ease;
            }

            .music-player.expanded .music-player-expanded {
                max-height: 300px;
            }

            .music-queue-header {
                display: flex;
                justify-content: space-between;
                padding: 12px 0 8px;
                font-size: 12px;
                color: var(--text-secondary, #94a3b8);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .music-queue-list {
                max-height: 240px;
                overflow-y: auto;
            }

            .music-queue-item {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 8px;
                border-radius: 8px;
                cursor: pointer;
                transition: background 0.2s;
            }

            .music-queue-item:hover {
                background: var(--bg-tertiary, #334155);
            }

            .music-queue-item.active {
                background: var(--accent-glow, rgba(59, 130, 246, 0.2));
            }

            .queue-item-num {
                width: 24px;
                text-align: center;
                font-size: 12px;
                color: var(--text-muted, #64748b);
            }

            .queue-item-info {
                flex: 1;
                min-width: 0;
            }

            .queue-item-title {
                font-size: 13px;
                color: var(--text-primary, #f8fafc);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .queue-item-artist {
                font-size: 11px;
                color: var(--text-secondary, #94a3b8);
            }

            /* ==================== Mobile Adjustments ==================== */

            @media (max-width: 480px) {
                .music-player {
                    bottom: 90px; /* Mobile voice input area */
                }

                .music-player-tab {
                    width: 44px;
                    padding: 10px 6px;
                }

                .music-player-tab:hover {
                    width: 48px;
                }

                .music-tab-art {
                    width: 28px;
                    height: 28px;
                }

                .music-player-panel {
                    width: 100vw;
                    max-width: none;
                }

                .music-player-artwork {
                    max-width: 240px;
                }

                .music-player-controls {
                    gap: 12px;
                }

                .music-btn-play {
                    width: 52px;
                    height: 52px;
                }
            }
        `;

        document.head.appendChild(style);
    }
}

// Export for use in Jarvis Web
window.MusicPlayer = MusicPlayer;
