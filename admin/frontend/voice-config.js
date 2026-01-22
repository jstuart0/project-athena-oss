// Voice Configuration Management
// Manages STT (Whisper), TTS (Piper), and HA Pipeline settings

let sttModels = [];
let ttsVoices = [];
let voiceServices = [];
let voiceHealth = {};
let haPipelines = [];
let currentPipelineMode = 'full';
let haConnected = false;

// Running configuration from Mac mini containers
let runningConfig = {
    whisper_model: null,
    piper_voice: null,
    loaded: false
};

// Use the global getAuthToken from utils.js
function getToken() {
    return window.getAuthToken ? window.getAuthToken() : localStorage.getItem('auth_token');
}

// Fetch running configuration via admin backend proxy (avoids CORS/mixed-content issues)
async function loadRunningConfig() {
    try {
        const response = await fetch('/api/voice-config/running-config');
        if (response.ok) {
            const data = await response.json();
            if (data.error) {
                console.warn('Running config unavailable:', data.error);
                runningConfig.loaded = false;
                return false;
            }
            runningConfig = { ...data, loaded: true };
            return true;
        }
    } catch (error) {
        console.warn('Could not fetch running config:', error);
    }
    runningConfig.loaded = false;
    return false;
}

async function loadVoiceConfig() {
    try {
        const [sttRes, ttsRes, servicesRes] = await Promise.all([
            fetch('/api/voice-config/stt/models'),
            fetch('/api/voice-config/tts/voices'),
            fetch('/api/voice-config/services')
        ]);

        if (!sttRes.ok || !ttsRes.ok || !servicesRes.ok) {
            throw new Error('Failed to load voice configuration');
        }

        sttModels = await sttRes.json();
        ttsVoices = await ttsRes.json();
        voiceServices = await servicesRes.json();

        // Load running config from Mac mini (non-blocking)
        loadRunningConfig().then(() => {
            updateSyncStatus();
        });

        // Load HA pipeline config (non-blocking)
        loadHAPipelineConfig();

        // Check health in background
        checkVoiceHealth();

        renderVoiceConfig();
    } catch (error) {
        console.error('Failed to load voice config:', error);
        const container = document.getElementById('voice-config-content');
        if (container) {
            container.innerHTML = `
                <div class="error-message">
                    <i class="fas fa-exclamation-triangle"></i>
                    Failed to load voice configuration: ${error.message}
                </div>
            `;
        }
    }
}

// Check if admin settings match running containers
function updateSyncStatus() {
    const activeSTT = sttModels.find(m => m.is_active);
    const activeTTS = ttsVoices.find(v => v.is_active);

    const sttSyncEl = document.getElementById('stt-sync-status');
    const ttsSyncEl = document.getElementById('tts-sync-status');

    if (!runningConfig.loaded) {
        if (sttSyncEl) sttSyncEl.innerHTML = '<span class="sync-unknown" title="Cannot check - Mac mini unreachable"><i class="fas fa-question-circle"></i> Unknown</span>';
        if (ttsSyncEl) ttsSyncEl.innerHTML = '<span class="sync-unknown" title="Cannot check - Mac mini unreachable"><i class="fas fa-question-circle"></i> Unknown</span>';
        return;
    }

    // Check STT sync
    if (sttSyncEl && activeSTT) {
        const sttInSync = runningConfig.whisper_model === activeSTT.model_name;
        if (sttInSync) {
            sttSyncEl.innerHTML = '<span class="sync-ok" title="Running config matches setting"><i class="fas fa-check-circle"></i> In Sync</span>';
        } else {
            sttSyncEl.innerHTML = `<span class="sync-mismatch" title="Running: ${runningConfig.whisper_model || 'unknown'}"><i class="fas fa-exclamation-triangle"></i> Out of Sync (running: ${runningConfig.whisper_model || '?'})</span>`;
        }
    }

    // Check TTS sync
    if (ttsSyncEl && activeTTS) {
        const ttsInSync = runningConfig.piper_voice === activeTTS.voice_id;
        if (ttsInSync) {
            ttsSyncEl.innerHTML = '<span class="sync-ok" title="Running config matches setting"><i class="fas fa-check-circle"></i> In Sync</span>';
        } else {
            ttsSyncEl.innerHTML = `<span class="sync-mismatch" title="Running: ${runningConfig.piper_voice || 'unknown'}"><i class="fas fa-exclamation-triangle"></i> Out of Sync (running: ${runningConfig.piper_voice || '?'})</span>`;
        }
    }
}

async function loadHAPipelineConfig() {
    try {
        // Load pipelines and current mode in parallel
        const [pipelinesRes, preferredRes] = await Promise.all([
            fetch('/api/ha-pipelines/pipelines'),
            fetch('/api/ha-pipelines/pipelines/preferred')
        ]);

        if (pipelinesRes.ok) {
            const data = await pipelinesRes.json();
            haPipelines = data.pipelines || [];
            haConnected = true;
        } else {
            haConnected = false;
        }

        if (preferredRes.ok) {
            const data = await preferredRes.json();
            currentPipelineMode = data.current_mode || 'full';
        }

        // Re-render to show pipeline section
        renderPipelineModeSection();
    } catch (error) {
        console.error('Failed to load HA pipeline config:', error);
        haConnected = false;
        renderPipelineModeSection();
    }
}

function renderPipelineModeSection() {
    const container = document.getElementById('pipeline-mode-section');
    if (!container) return;

    if (!haConnected) {
        container.innerHTML = `
            <div class="pipeline-mode-card">
                <div class="config-card-header">
                    <div class="header-title">
                        <i class="fas fa-exchange-alt"></i>
                        <h3>Voice Pipeline Mode</h3>
                    </div>
                    <div class="header-status">
                        <span class="health-indicator health-unreachable" title="Home Assistant not reachable"></span>
                        <span class="service-status status-inactive">Disconnected</span>
                    </div>
                </div>
                <div class="config-card-body">
                    <div class="error-message" style="margin: 0;">
                        <i class="fas fa-exclamation-triangle"></i>
                        Unable to connect to Home Assistant. Check HA_TOKEN configuration.
                    </div>
                </div>
            </div>
        `;
        return;
    }

    container.innerHTML = `
        <div class="pipeline-mode-card">
            <div class="config-card-header">
                <div class="header-title">
                    <i class="fas fa-exchange-alt"></i>
                    <h3>Voice Pipeline Mode</h3>
                </div>
                <div class="header-status">
                    <span class="health-indicator health-healthy" title="Home Assistant connected"></span>
                    <span class="service-status status-active">Connected</span>
                </div>
            </div>
            <div class="config-card-body">
                <div class="mode-description">
                    <p>Choose your voice pipeline mode:</p>
                </div>
                <div class="mode-toggle-container three-modes">
                    <div class="mode-option ${currentPipelineMode === 'streaming_rag' ? 'active recommended' : ''}" onclick="setVoiceMode('streaming_rag')">
                        <div class="mode-icon">
                            <i class="fas fa-rocket"></i>
                        </div>
                        <div class="mode-info">
                            <span class="mode-name">Streaming + RAG</span>
                            <span class="mode-detail">True streaming with knowledge</span>
                        </div>
                        <div class="mode-badge">
                            <span class="badge-faster">Fast</span>
                            <span class="badge-smarter">Smart</span>
                            <span class="badge-recommended">Recommended</span>
                        </div>
                        ${currentPipelineMode === 'streaming_rag' ? '<span class="active-badge"><i class="fas fa-check"></i> Active</span>' : ''}
                    </div>
                    <div class="mode-option ${currentPipelineMode === 'full' ? 'active' : ''}" onclick="setVoiceMode('full')">
                        <div class="mode-icon">
                            <i class="fas fa-brain"></i>
                        </div>
                        <div class="mode-info">
                            <span class="mode-name">Full Pipeline</span>
                            <span class="mode-detail">RAG without streaming (stable)</span>
                        </div>
                        <div class="mode-badge">
                            <span class="badge-slower">Slower</span>
                            <span class="badge-smarter">Smart</span>
                        </div>
                        ${currentPipelineMode === 'full' ? '<span class="active-badge"><i class="fas fa-check"></i> Active</span>' : ''}
                    </div>
                    <div class="mode-option ${currentPipelineMode === 'simple' ? 'active' : ''}" onclick="setVoiceMode('simple')">
                        <div class="mode-icon">
                            <i class="fas fa-bolt"></i>
                        </div>
                        <div class="mode-info">
                            <span class="mode-name">Simple Mode</span>
                            <span class="mode-detail">Direct Ollama (no RAG)</span>
                        </div>
                        <div class="mode-badge">
                            <span class="badge-faster">Fastest</span>
                            <span class="badge-basic">Basic</span>
                        </div>
                        ${currentPipelineMode === 'simple' ? '<span class="active-badge"><i class="fas fa-check"></i> Active</span>' : ''}
                    </div>
                </div>
                <div class="pipeline-info">
                    <h4>Available Pipelines:</h4>
                    <div class="pipeline-list">
                        ${haPipelines.map(p => `
                            <div class="pipeline-item ${p.is_preferred ? 'preferred' : ''}">
                                <span class="pipeline-name">${escapeHtml(p.name)}</span>
                                <span class="pipeline-engine">${escapeHtml(p.conversation_engine?.split('.').pop() || 'unknown')}</span>
                                ${p.is_preferred ? '<span class="preferred-badge">Active</span>' : ''}
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>
        </div>
    `;
}

async function setVoiceMode(mode) {
    if (mode === currentPipelineMode) return;

    const modeNames = {
        'streaming_rag': 'Streaming + RAG',
        'full': 'Full Pipeline',
        'simple': 'Simple Mode'
    };

    try {
        showNotification(`Switching to ${modeNames[mode] || mode} mode...`, 'info');

        const response = await fetch('/api/ha-pipelines/mode/set', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ mode })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to set voice mode');
        }

        const result = await response.json();
        currentPipelineMode = mode;
        showNotification(result.message, 'success');

        // Refresh pipeline data
        await loadHAPipelineConfig();
    } catch (error) {
        console.error('Failed to set voice mode:', error);
        showNotification(`Failed to switch mode: ${error.message}`, 'error');
    }
}

async function checkVoiceHealth() {
    try {
        const response = await fetch('/api/voice-config/health');
        if (response.ok) {
            voiceHealth = await response.json();
            updateHealthIndicators();
        }
    } catch (error) {
        console.error('Health check failed:', error);
    }
}

function updateHealthIndicators() {
    // Update STT health
    const sttHealthEl = document.getElementById('stt-health');
    if (sttHealthEl && voiceHealth.stt) {
        const status = voiceHealth.stt.status;
        sttHealthEl.className = `health-indicator health-${status}`;
        sttHealthEl.title = status === 'healthy' ? 'Service is running' :
                           status === 'unreachable' ? 'Service not reachable' :
                           status === 'disabled' ? 'Service disabled' : 'Unknown status';
    }

    // Update TTS health
    const ttsHealthEl = document.getElementById('tts-health');
    if (ttsHealthEl && voiceHealth.tts) {
        const status = voiceHealth.tts.status;
        ttsHealthEl.className = `health-indicator health-${status}`;
        ttsHealthEl.title = status === 'healthy' ? 'Service is running' :
                           status === 'unreachable' ? 'Service not reachable' :
                           status === 'disabled' ? 'Service disabled' : 'Unknown status';
    }
}

function renderVoiceConfig() {
    const container = document.getElementById('voice-config-content');
    if (!container) return;

    const sttService = voiceServices.find(s => s.service_type === 'stt');
    const ttsService = voiceServices.find(s => s.service_type === 'tts');

    container.innerHTML = `
        <!-- HA Pipeline Mode Section -->
        <div id="pipeline-mode-section" class="pipeline-mode-section">
            <div class="pipeline-mode-card">
                <div class="config-card-header">
                    <div class="header-title">
                        <i class="fas fa-exchange-alt"></i>
                        <h3>Voice Pipeline Mode</h3>
                    </div>
                    <div class="header-status">
                        <span class="health-indicator health-unknown" title="Checking..."></span>
                        <span class="service-status">Loading...</span>
                    </div>
                </div>
                <div class="config-card-body">
                    <div class="text-center text-gray-400 py-4">
                        <div class="animate-pulse">Connecting to Home Assistant...</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="voice-config-grid">
            <!-- STT Configuration -->
            <div class="config-card">
                <div class="config-card-header">
                    <div class="header-title">
                        <i class="fas fa-microphone"></i>
                        <h3>Speech-to-Text (Whisper)</h3>
                    </div>
                    <div class="header-status">
                        <span id="stt-health" class="health-indicator health-unknown" title="Checking..."></span>
                        <span class="service-status ${sttService?.enabled ? 'status-active' : 'status-inactive'}">
                            ${sttService?.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                    </div>
                </div>
                <div class="config-card-body">
                    <div class="service-info">
                        <div class="info-row">
                            <span class="info-label">Host:</span>
                            <span class="info-value">${sttService?.host || 'Not configured'}</span>
                        </div>
                        <div class="info-row">
                            <span class="info-label">Status:</span>
                            <span id="stt-sync-status" class="info-value"><i class="fas fa-spinner fa-spin"></i> Checking...</span>
                        </div>
                    </div>
                    <h4>Select Model:</h4>
                    <p class="section-note"><i class="fas fa-info-circle"></i> Changes require service restart to take effect</p>
                    <div class="model-list">
                        ${sttModels.map(model => `
                            <div class="model-option ${model.is_active ? 'active' : ''}"
                                 onclick="setActiveSTT(${model.id}, '${escapeHtml(model.name)}')">
                                <div class="model-header">
                                    <span class="model-name">${escapeHtml(model.display_name)}</span>
                                    <span class="model-size">${model.size_mb} MB</span>
                                </div>
                                <p class="model-description">${escapeHtml(model.description || '')}</p>
                                ${model.is_active ? '<span class="active-badge"><i class="fas fa-check"></i> Active</span>' : ''}
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>

            <!-- TTS Configuration -->
            <div class="config-card">
                <div class="config-card-header">
                    <div class="header-title">
                        <i class="fas fa-volume-up"></i>
                        <h3>Text-to-Speech (Piper)</h3>
                    </div>
                    <div class="header-status">
                        <span id="tts-health" class="health-indicator health-unknown" title="Checking..."></span>
                        <span class="service-status ${ttsService?.enabled ? 'status-active' : 'status-inactive'}">
                            ${ttsService?.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                    </div>
                </div>
                <div class="config-card-body">
                    <div class="service-info">
                        <div class="info-row">
                            <span class="info-label">Host:</span>
                            <span class="info-value">${ttsService?.host || 'Not configured'}</span>
                        </div>
                        <div class="info-row">
                            <span class="info-label">Status:</span>
                            <span id="tts-sync-status" class="info-value"><i class="fas fa-spinner fa-spin"></i> Checking...</span>
                        </div>
                    </div>
                    <h4>Select Voice:</h4>
                    <p class="section-note"><i class="fas fa-info-circle"></i> TTS supports dynamic voice switching (no restart needed)</p>
                    <p class="section-note quality-legend">
                        <span class="quality-badge quality-low">low</span> 16kHz, fastest
                        <span class="quality-badge quality-medium">medium</span> 22kHz, balanced
                        <span class="quality-badge quality-high">high</span> 22kHz+, best quality
                    </p>

                    <!-- US English Voices -->
                    <h5 class="voice-region-header"><i class="fas fa-flag-usa"></i> US English</h5>
                    <div class="voice-list">
                        ${ttsVoices.filter(v => v.voice_id?.includes('en_US')).map(voice => `
                            <div class="model-option ${voice.is_active ? 'active' : ''}"
                                 onclick="setActiveTTS(${voice.id}, '${escapeHtml(voice.name)}')">
                                <div class="model-header">
                                    <span class="model-name">${escapeHtml(voice.display_name)}</span>
                                    <span class="model-quality quality-${voice.quality}" title="${getQualityTooltip(voice.quality)}">${voice.quality}</span>
                                </div>
                                <p class="model-description">${escapeHtml(voice.description || '')}</p>
                                ${voice.is_active ? '<span class="active-badge"><i class="fas fa-check"></i> Active</span>' : ''}
                            </div>
                        `).join('')}
                    </div>

                    <!-- UK English Voices -->
                    <h5 class="voice-region-header"><i class="fas fa-flag"></i> UK English</h5>
                    <div class="voice-list">
                        ${ttsVoices.filter(v => v.voice_id?.includes('en_GB')).map(voice => `
                            <div class="model-option ${voice.is_active ? 'active' : ''}"
                                 onclick="setActiveTTS(${voice.id}, '${escapeHtml(voice.name)}')">
                                <div class="model-header">
                                    <span class="model-name">${escapeHtml(voice.display_name)}</span>
                                    <span class="model-quality quality-${voice.quality}" title="${getQualityTooltip(voice.quality)}">${voice.quality}</span>
                                </div>
                                <p class="model-description">${escapeHtml(voice.description || '')}</p>
                                ${voice.is_active ? '<span class="active-badge"><i class="fas fa-check"></i> Active</span>' : ''}
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>
        </div>

        <div class="restart-notice" id="restart-notice" style="display: none;">
            <i class="fas fa-exclamation-triangle"></i>
            <span>STT model changed. Restart voice services to apply.</span>
            <button onclick="restartVoiceServices()" class="btn btn-warning">
                <i class="fas fa-sync"></i> Restart Services
            </button>
        </div>

        <div class="test-section">
            <h4><i class="fas fa-flask"></i> Test Voice Services</h4>
            <div class="test-controls">
                <div class="test-group">
                    <label>TTS Test:</label>
                    <input type="text" id="tts-test-input" placeholder="Enter text to synthesize" value="Hello, I am Athena, your voice assistant.">
                    <button onclick="testTTS()" class="btn btn-primary">
                        <i class="fas fa-play"></i> Test TTS
                    </button>
                </div>
            </div>
            <audio id="tts-test-audio" controls style="display: none; margin-top: 10px; width: 100%;"></audio>
        </div>
    `;

    // Update health indicators after render
    updateHealthIndicators();
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function getQualityTooltip(quality) {
    switch (quality) {
        case 'low': return '16kHz sample rate, smallest model, fastest synthesis (~50ms)';
        case 'medium': return '22kHz sample rate, balanced quality/speed (~100ms)';
        case 'high': return '22kHz+ sample rate, best quality, slower (~200ms)';
        default: return '';
    }
}

async function setActiveSTT(id, name) {
    try {
        // Ensure id is an integer for Pydantic validation
        const numericId = parseInt(id, 10);
        if (isNaN(numericId)) {
            throw new Error(`Invalid model ID: ${id}`);
        }

        const requestBody = { id: numericId };
        console.log('[VoiceConfig] Setting active STT:', requestBody);

        const response = await fetch('/api/voice-config/stt/set-active', {
            method: 'POST',
            headers: getAuthHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
            const error = await response.json();
            console.error('[VoiceConfig] STT set-active error:', error);
            // Handle FastAPI validation error format (detail is an array)
            const errorMsg = Array.isArray(error.detail)
                ? error.detail.map(e => e.msg || e.message || JSON.stringify(e)).join(', ')
                : (error.detail || error.message || 'Failed to set active STT model');
            throw new Error(errorMsg);
        }

        const result = await response.json();
        showNotification(`STT model set to ${result.model}. Restart required.`, 'success');
        document.getElementById('restart-notice').style.display = 'flex';
        await loadVoiceConfig();
    } catch (error) {
        showNotification(`Failed to update STT model: ${error.message}`, 'error');
    }
}

async function setActiveTTS(id, name) {
    try {
        // Ensure id is an integer for Pydantic validation
        const numericId = parseInt(id, 10);
        if (isNaN(numericId)) {
            throw new Error(`Invalid voice ID: ${id}`);
        }

        const requestBody = { id: numericId };
        console.log('[VoiceConfig] Setting active TTS:', requestBody);

        const response = await fetch('/api/voice-config/tts/set-active', {
            method: 'POST',
            headers: getAuthHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
            const error = await response.json();
            console.error('[VoiceConfig] TTS set-active error:', error);
            // Handle FastAPI validation error format (detail is an array)
            const errorMsg = Array.isArray(error.detail)
                ? error.detail.map(e => e.msg || e.message || JSON.stringify(e)).join(', ')
                : (error.detail || error.message || 'Failed to set active TTS voice');
            throw new Error(errorMsg);
        }

        const result = await response.json();
        showNotification(`TTS voice set to ${result.voice}. Restart required.`, 'success');
        document.getElementById('restart-notice').style.display = 'flex';
        await loadVoiceConfig();
    } catch (error) {
        showNotification(`Failed to update TTS voice: ${error.message}`, 'error');
    }
}

async function restartVoiceServices() {
    try {
        showNotification('Restarting voice services on Mac mini...', 'info');

        // Use admin backend proxy which handles config update and restart
        const response = await fetch('/api/voice-config/restart-services', {
            method: 'POST',
            headers: getAuthHeaders(),
            credentials: 'same-origin'
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to restart voice services');
        }

        const result = await response.json();
        console.log('Restart result:', result);

        showNotification('Voice services restarting... Please wait 30-60 seconds for models to load.', 'success');
        document.getElementById('restart-notice').style.display = 'none';

        // Re-check health and sync status after restart
        setTimeout(async () => {
            await checkVoiceHealth();
            await loadRunningConfig();
            updateSyncStatus();
        }, 10000);

    } catch (error) {
        console.error('Restart error:', error);
        showNotification(`Restart failed: ${error.message}. Check Mac mini connection.`, 'error');
        // Show manual restart instructions as fallback
        showNotification('Manual: Restart Wyoming services on your voice server (docker compose restart whisper piper)', 'info');
    }
}

async function testTTS() {
    const text = document.getElementById('tts-test-input').value.trim();
    if (!text) {
        showNotification('Please enter text to synthesize', 'warning');
        return;
    }

    const ttsService = voiceServices.find(s => s.service_type === 'tts');
    if (!ttsService || !ttsService.rest_port) {
        showNotification('TTS REST service not configured', 'error');
        return;
    }

    try {
        showNotification('Synthesizing speech...', 'info');

        // Fetch audio from TTS REST endpoint
        const url = `http://${ttsService.host}:${ttsService.rest_port}/synthesize?text=${encodeURIComponent(text)}`;
        const response = await fetch(url);

        if (!response.ok) {
            throw new Error(`TTS synthesis failed: ${response.statusText}`);
        }

        const audioBlob = await response.blob();
        const audioUrl = URL.createObjectURL(audioBlob);

        const audioEl = document.getElementById('tts-test-audio');
        audioEl.src = audioUrl;
        audioEl.style.display = 'block';
        audioEl.play();

        showNotification('TTS synthesis complete', 'success');

        // Clean up URL after playback
        audioEl.onended = () => URL.revokeObjectURL(audioUrl);
    } catch (error) {
        showNotification(`TTS test failed: ${error.message}`, 'error');
    }
}

// CSS styles for voice config (inject into page)
function injectVoiceConfigStyles() {
    if (document.getElementById('voice-config-styles')) return;

    const styles = document.createElement('style');
    styles.id = 'voice-config-styles';
    styles.textContent = `
        .voice-config-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }

        .config-card {
            background: var(--card-bg, #1e1e1e);
            border: 1px solid var(--border-color, #333);
            border-radius: 8px;
            overflow: hidden;
        }

        .config-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px 20px;
            background: var(--header-bg, #252525);
            border-bottom: 1px solid var(--border-color, #333);
        }

        .header-title {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .header-title i {
            font-size: 1.2em;
            color: var(--accent-color, #4a9eff);
        }

        .header-title h3 {
            margin: 0;
            font-size: 1.1em;
        }

        .header-status {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .health-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
        }

        .health-healthy { background: #4caf50; }
        .health-unhealthy { background: #f44336; }
        .health-unreachable { background: #ff9800; }
        .health-disabled { background: #9e9e9e; }
        .health-unknown { background: #757575; }

        .service-status {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.85em;
            font-weight: 500;
        }

        .status-active {
            background: rgba(76, 175, 80, 0.2);
            color: #4caf50;
        }

        .status-inactive {
            background: rgba(244, 67, 54, 0.2);
            color: #f44336;
        }

        .config-card-body {
            padding: 20px;
        }

        .service-info {
            background: var(--info-bg, #2a2a2a);
            padding: 12px 15px;
            border-radius: 6px;
            margin-bottom: 20px;
        }

        .info-row {
            display: flex;
            justify-content: space-between;
            padding: 4px 0;
        }

        .info-label {
            color: var(--text-muted, #888);
        }

        .info-value {
            font-family: monospace;
            color: var(--text-color, #fff);
        }

        .config-card-body h4 {
            margin: 0 0 15px 0;
            font-size: 0.95em;
            color: var(--text-muted, #888);
        }

        .model-list, .voice-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
            max-height: 350px;
            overflow-y: auto;
        }

        .model-option {
            padding: 12px 15px;
            background: var(--option-bg, #2a2a2a);
            border: 2px solid transparent;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
        }

        .model-option:hover {
            background: var(--option-hover-bg, #333);
            border-color: var(--accent-color, #4a9eff);
        }

        .model-option.active {
            border-color: #4caf50;
            background: rgba(76, 175, 80, 0.1);
        }

        .model-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 5px;
        }

        .model-name {
            font-weight: 600;
            color: var(--text-color, #fff);
        }

        .model-size {
            font-size: 0.85em;
            color: var(--text-muted, #888);
            background: var(--badge-bg, #333);
            padding: 2px 8px;
            border-radius: 4px;
        }

        .model-quality {
            font-size: 0.85em;
            padding: 2px 8px;
            border-radius: 4px;
            text-transform: capitalize;
        }

        .quality-low { background: rgba(255, 152, 0, 0.2); color: #ff9800; }
        .quality-medium { background: rgba(33, 150, 243, 0.2); color: #2196f3; }
        .quality-high { background: rgba(76, 175, 80, 0.2); color: #4caf50; }

        .model-description {
            margin: 0;
            font-size: 0.85em;
            color: var(--text-muted, #888);
            line-height: 1.4;
        }

        .active-badge {
            position: absolute;
            top: 10px;
            right: 10px;
            background: #4caf50;
            color: white;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: 600;
        }

        .restart-notice {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 15px 20px;
            background: rgba(255, 152, 0, 0.15);
            border: 1px solid rgba(255, 152, 0, 0.3);
            border-radius: 8px;
            margin-bottom: 20px;
            color: #ff9800;
        }

        .restart-notice i {
            font-size: 1.2em;
        }

        .restart-notice span {
            flex: 1;
        }

        .test-section {
            background: var(--card-bg, #1e1e1e);
            border: 1px solid var(--border-color, #333);
            border-radius: 8px;
            padding: 20px;
        }

        .test-section h4 {
            margin: 0 0 15px 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .test-controls {
            display: flex;
            flex-direction: column;
            gap: 15px;
        }

        .test-group {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .test-group label {
            min-width: 80px;
            color: var(--text-muted, #888);
        }

        .test-group input {
            flex: 1;
            padding: 8px 12px;
            background: var(--input-bg, #2a2a2a);
            border: 1px solid var(--border-color, #333);
            border-radius: 4px;
            color: var(--text-color, #fff);
        }

        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 500;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }

        .btn-primary {
            background: var(--accent-color, #4a9eff);
            color: white;
        }

        .btn-primary:hover {
            background: var(--accent-hover, #3a8eef);
        }

        .btn-warning {
            background: #ff9800;
            color: white;
        }

        .btn-warning:hover {
            background: #f57c00;
        }

        .error-message {
            padding: 20px;
            background: rgba(244, 67, 54, 0.1);
            border: 1px solid rgba(244, 67, 54, 0.3);
            border-radius: 8px;
            color: #f44336;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        /* Pipeline Mode Styles */
        .pipeline-mode-section {
            margin-bottom: 20px;
        }

        .pipeline-mode-card {
            background: var(--card-bg, #1e1e1e);
            border: 1px solid var(--border-color, #333);
            border-radius: 8px;
            overflow: hidden;
        }

        .mode-description {
            margin-bottom: 20px;
            color: var(--text-muted, #888);
        }

        .mode-description p {
            margin: 0;
        }

        .mode-toggle-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 20px;
        }

        .mode-toggle-container.three-modes {
            grid-template-columns: 1fr 1fr 1fr;
        }

        .mode-option {
            padding: 20px;
            background: var(--option-bg, #2a2a2a);
            border: 2px solid transparent;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .mode-option:hover {
            background: var(--option-hover-bg, #333);
            border-color: var(--accent-color, #4a9eff);
        }

        .mode-option.active {
            border-color: #4caf50;
            background: rgba(76, 175, 80, 0.1);
        }

        .mode-option.recommended {
            border-color: #ff9800;
        }

        .mode-option.active.recommended {
            border-color: #4caf50;
            box-shadow: 0 0 0 2px rgba(255, 152, 0, 0.3);
        }

        .mode-icon {
            font-size: 2em;
            color: var(--accent-color, #4a9eff);
        }

        .mode-option.active .mode-icon {
            color: #4caf50;
        }

        .mode-info {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .mode-name {
            font-weight: 600;
            font-size: 1.1em;
            color: var(--text-color, #fff);
        }

        .mode-detail {
            font-size: 0.85em;
            color: var(--text-muted, #888);
        }

        .mode-badge {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .mode-badge span {
            font-size: 0.75em;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 500;
        }

        .badge-slower {
            background: rgba(255, 152, 0, 0.2);
            color: #ff9800;
        }

        .badge-smarter {
            background: rgba(156, 39, 176, 0.2);
            color: #ce93d8;
        }

        .badge-faster {
            background: rgba(76, 175, 80, 0.2);
            color: #4caf50;
        }

        .badge-streaming {
            background: rgba(33, 150, 243, 0.2);
            color: #2196f3;
        }

        .badge-recommended {
            background: rgba(255, 152, 0, 0.2);
            color: #ff9800;
        }

        .badge-basic {
            background: rgba(158, 158, 158, 0.2);
            color: #bdbdbd;
        }

        .pipeline-info {
            background: var(--info-bg, #2a2a2a);
            padding: 15px;
            border-radius: 6px;
        }

        .pipeline-info h4 {
            margin: 0 0 10px 0;
            font-size: 0.9em;
            color: var(--text-muted, #888);
        }

        .pipeline-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .pipeline-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            background: var(--bg-darker, #1e1e1e);
            border-radius: 4px;
            font-size: 0.9em;
        }

        .pipeline-item.preferred {
            border-left: 3px solid #4caf50;
        }

        .pipeline-name {
            color: var(--text-color, #fff);
            font-weight: 500;
        }

        .pipeline-engine {
            font-family: monospace;
            color: var(--text-muted, #888);
            font-size: 0.85em;
        }

        .preferred-badge {
            background: rgba(76, 175, 80, 0.2);
            color: #4caf50;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: 600;
        }

        @media (max-width: 1024px) {
            .mode-toggle-container.three-modes {
                grid-template-columns: 1fr 1fr;
            }
        }

        @media (max-width: 768px) {
            .mode-toggle-container,
            .mode-toggle-container.three-modes {
                grid-template-columns: 1fr;
            }
        }
    `;
    document.head.appendChild(styles);
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    injectVoiceConfigStyles();
});

// Export for use in main app
window.loadVoiceConfig = loadVoiceConfig;
