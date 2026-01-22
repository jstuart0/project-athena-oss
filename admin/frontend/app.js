/**
 * Project Athena - Admin Interface Frontend
 * Phase 2: Complete Management Interface
 *
 * NOTE: This file now integrates with Phase 0 modules (state.js, api-client.js, auth.js)
 * The global authToken and currentUser are kept for backward compatibility but sync with AppState
 */

// Configuration
const API_BASE = window.location.origin;

// Backward compatibility - these sync with AppState
let authToken = null;
let currentUser = null;

// Initialize app
document.addEventListener('DOMContentLoaded', async () => {
    // Initialize Phase 1-7 UI components
    if (typeof CommandPalette !== 'undefined') {
        CommandPalette.init();
    }

    // Initialize Lucide icons
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }

    // Subscribe to AppState auth changes (sync backward-compat vars)
    if (typeof AppState !== 'undefined') {
        AppState.subscribe((type, data) => {
            if (type === 'auth') {
                authToken = data.token;
                currentUser = data.user;
                updateAuthUI(data.status === 'authenticated');
            }
        });

        // Register page destroy callbacks for proper cleanup on navigation
        if (typeof destroyMetricsPage === 'function') {
            AppState.registerDestroyCallback('performance-metrics', destroyMetricsPage);
        }
        if (typeof destroyFeaturesPage === 'function') {
            AppState.registerDestroyCallback('features', destroyFeaturesPage);
        }
        if (typeof destroyAlertsPage === 'function') {
            AppState.registerDestroyCallback('alerts', destroyAlertsPage);
        }
        if (typeof stopAutoRefresh === 'function') {
            AppState.registerDestroyCallback('llm-components', stopAutoRefresh);
        }
        if (typeof destroyEmergingIntentsPage === 'function') {
            AppState.registerDestroyCallback('emerging-intents', destroyEmergingIntentsPage);
        }
        if (typeof destroyMusicConfigPage === 'function') {
            AppState.registerDestroyCallback('music-config', destroyMusicConfigPage);
        }
        if (typeof destroySystemConfigPage === 'function') {
            AppState.registerDestroyCallback('system-config', destroySystemConfigPage);
        }
        if (typeof destroyAdminJarvis === 'function') {
            AppState.registerDestroyCallback('admin-jarvis', destroyAdminJarvis);
        }
    }

    // Use new Auth module if available, otherwise fall back to legacy
    if (typeof Auth !== 'undefined') {
        await Auth.initialize();
    } else {
        checkAuthStatus();
    }

    initializeSidebarCategories();

    // Start status bar polling
    if (typeof StatusBar !== 'undefined') {
        StatusBar.start();
    }

    // Handle deep linking - check URL hash for initial tab
    const hash = window.location.hash.replace('#', '');
    const initialTab = hash || 'dashboard';
    showTab(initialTab);

    // Listen for hash changes (browser back/forward)
    window.addEventListener('hashchange', () => {
        const newHash = window.location.hash.replace('#', '');
        if (newHash) {
            showTab(newHash);
        }
    });
});

// ============================================================================
// Sidebar Category Collapse/Expand
// ============================================================================

function toggleCategory(categoryId) {
    const category = document.getElementById(`cat-${categoryId}`);
    if (category) {
        category.classList.toggle('collapsed');
        // Save state to localStorage
        const collapsed = category.classList.contains('collapsed');
        localStorage.setItem(`sidebar-cat-${categoryId}`, collapsed ? 'true' : 'false');
    }
}

function initializeSidebarCategories() {
    // Restore saved collapse states from localStorage
    // Default to collapsed unless explicitly set to expanded
    // IDs match the HTML: cat-config, cat-ai, cat-tools, cat-smarthome, cat-guest, cat-analytics, cat-infra, cat-system
    const categories = ['config', 'ai', 'tools', 'smarthome', 'guest', 'analytics', 'infra', 'system'];
    categories.forEach(catId => {
        const saved = localStorage.getItem(`sidebar-cat-${catId}`);
        const category = document.getElementById(`cat-${catId}`);
        if (category) {
            // Default to collapsed unless explicitly saved as 'false' (expanded)
            if (saved !== 'false') {
                category.classList.add('collapsed');
            }
        }
    });
}

// ============================================================================
// Authentication
// ============================================================================

async function checkAuthStatus() {
    // Check for token in URL (from auth callback)
    const urlParams = new URLSearchParams(window.location.search);
    const token = urlParams.get('token');

    if (token) {
        authToken = token;
        localStorage.setItem('auth_token', token);
        // Remove token from URL
        window.history.replaceState({}, document.title, window.location.pathname);
    } else {
        // Check localStorage
        authToken = localStorage.getItem('auth_token');
    }

    // If no token in localStorage, try to get from session
    if (!authToken) {
        try {
            const response = await fetch(`${API_BASE}/api/auth/session-token`, {
                credentials: 'include'  // Include cookies for session
            });
            if (response.ok) {
                const data = await response.json();
                if (data.token) {
                    authToken = data.token;
                    localStorage.setItem('auth_token', data.token);
                    console.log('[Auth] Token restored from session');
                }
            }
        } catch (e) {
            console.debug('[Auth] No session token available');
        }
    }

    if (authToken) {
        loadCurrentUser();
    } else {
        updateAuthUI(false);
    }
}

async function loadCurrentUser() {
    try {
        const response = await fetch(`${API_BASE}/api/auth/me`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });

        if (response.ok) {
            currentUser = await response.json();
            updateAuthUI(true);

            // Initialize guest context for multi-guest device identification
            if (typeof initializeGuestContext === 'function') {
                initializeGuestContext();
            }
        } else {
            // Token invalid
            authToken = null;
            localStorage.removeItem('auth_token');
            updateAuthUI(false);
        }
    } catch (error) {
        console.error('Failed to load user:', error);
        updateAuthUI(false);
    }
}

function updateAuthUI(authenticated) {
    const authSection = document.getElementById('auth-section');

    if (authenticated && currentUser) {
        authSection.innerHTML = `
            <div class="flex items-center gap-4">
                <div class="text-right">
                    <div class="text-sm font-medium text-white">${currentUser.full_name || currentUser.username}</div>
                    <div class="text-xs text-gray-400">Role: ${currentUser.role}</div>
                </div>
                <button onclick="handleAuth()"
                    class="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium transition-colors">
                    Logout
                </button>
            </div>
        `;
    } else {
        authSection.innerHTML = `
            <button onclick="handleAuth()" id="auth-button"
                class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                Login
            </button>
        `;
    }
}

function handleAuth() {
    if (authToken) {
        // Logout
        authToken = null;
        currentUser = null;
        localStorage.removeItem('auth_token');
        window.location.href = `${API_BASE}/api/auth/logout`;
    } else {
        // Login via Authentik OIDC
        window.location.href = `${API_BASE}/api/auth/login`;
    }
}

// ============================================================================
// OIDC Settings Management
// ============================================================================

async function loadOIDCSettings() {
    try {
        const response = await fetch(`${API_BASE}/api/settings/oidc`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });

        if (response.ok) {
            const settings = await response.json();

            // Populate form fields
            document.getElementById('oidc-provider-url').value = settings.provider_url || '';
            document.getElementById('oidc-client-id').value = settings.client_id || '';
            // Don't populate client secret for security
            document.getElementById('oidc-client-secret').value = '';
            document.getElementById('oidc-redirect-uri').value = settings.redirect_uri || `${window.location.origin}/api/auth/callback`;

            // Show status
            showOIDCStatus('Settings loaded successfully', 'success');
        } else {
            throw new Error('Failed to load OIDC settings');
        }
    } catch (error) {
        console.error('Failed to load OIDC settings:', error);
        showOIDCStatus('Failed to load settings', 'error');
    }
}

async function saveOIDCSettings() {
    const settings = {
        provider_url: document.getElementById('oidc-provider-url').value,
        client_id: document.getElementById('oidc-client-id').value,
        client_secret: document.getElementById('oidc-client-secret').value,
        redirect_uri: document.getElementById('oidc-redirect-uri').value
    };

    // Validate required fields
    if (!settings.provider_url || !settings.client_id) {
        showOIDCStatus('Provider URL and Client ID are required', 'error');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/settings/oidc`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(settings)
        });

        if (response.ok) {
            showOIDCStatus('Settings saved successfully! Backend will restart to apply changes.', 'success');
            // Clear the client secret field after save
            document.getElementById('oidc-client-secret').value = '';
        } else {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save settings');
        }
    } catch (error) {
        console.error('Failed to save OIDC settings:', error);
        showOIDCStatus(`Failed to save: ${error.message}`, 'error');
    }
}

async function testOIDCConnection() {
    showOIDCStatus('Testing connection...', 'info');

    try {
        const response = await fetch(`${API_BASE}/api/settings/oidc/test`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                provider_url: document.getElementById('oidc-provider-url').value,
                client_id: document.getElementById('oidc-client-id').value
            })
        });

        const result = await response.json();

        if (response.ok) {
            showOIDCStatus(`✅ Connection successful! Provider: ${result.provider_name || 'Unknown'}`, 'success');
        } else {
            throw new Error(result.detail || 'Connection test failed');
        }
    } catch (error) {
        console.error('OIDC connection test failed:', error);
        showOIDCStatus(`❌ Connection failed: ${error.message}`, 'error');
    }
}

function showOIDCStatus(message, type = 'info') {
    const statusDiv = document.getElementById('oidc-status');

    // Set color based on type
    let colorClass = 'text-gray-400';
    if (type === 'success') colorClass = 'text-green-400';
    else if (type === 'error') colorClass = 'text-red-400';
    else if (type === 'warning') colorClass = 'text-yellow-400';

    statusDiv.className = `text-sm ${colorClass}`;
    statusDiv.textContent = message;

    // Clear status after 5 seconds
    setTimeout(() => {
        statusDiv.textContent = '';
    }, 5000);
}

function getToken() {
    return authToken || localStorage.getItem('auth_token');
}

// escapeHtml is now provided by utils.js

// ============================================================================
// API Client
// ============================================================================

async function apiRequest(endpoint, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers
    };

    if (authToken) {
        headers['Authorization'] = `Bearer ${authToken}`;
    }

    const response = await fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers
    });

    if (response.status === 401) {
        // Unauthorized - redirect to login
        authToken = null;
        localStorage.removeItem('auth_token');
        showError('Session expired. Please login again.');
        updateAuthUI(false);
        throw new Error('Unauthorized');
    }

    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }

    return response.json();
}

// ============================================================================
// Tab Navigation
// ============================================================================

function showTab(tabName) {
    // Use AppState for lifecycle management (clears intervals, aborts requests)
    if (typeof AppState !== 'undefined') {
        AppState.setCurrentTab(tabName);
    }

    // Cleanup Athena pages when switching away
    if (window.Athena?.pages) {
        Object.values(Athena.pages).forEach(page => {
            if (page.destroy && typeof page.destroy === 'function') {
                try {
                    page.destroy();
                } catch (e) {
                    console.warn('[showTab] Error destroying page:', e);
                }
            }
        });
    }

    // Update URL hash for deep linking (without triggering hashchange)
    if (window.location.hash !== `#${tabName}`) {
        history.replaceState(null, '', `#${tabName}`);
    }

    // Update sidebar buttons - remove active state from all
    document.querySelectorAll('.sidebar-item').forEach(btn => {
        btn.classList.remove('sidebar-item-active', 'bg-gray-700', 'text-white');
        btn.classList.add('text-gray-400');
    });

    // Add active state to clicked button (find by data-route or onclick)
    document.querySelectorAll('.sidebar-item').forEach(btn => {
        const route = btn.getAttribute('data-route');
        const onclick = btn.getAttribute('onclick');
        if (route === tabName || (onclick && onclick.includes(`'${tabName}'`))) {
            btn.classList.add('sidebar-item-active', 'bg-gray-700', 'text-white');
            btn.classList.remove('text-gray-400');
        }
    });

    // Update tab content
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.add('hidden');
    });

    const tabElement = document.getElementById(`tab-${tabName}`);
    if (tabElement) {
        tabElement.classList.remove('hidden');
    }

    // Re-initialize Lucide icons for new content
    if (typeof lucide !== 'undefined') {
        setTimeout(() => lucide.createIcons(), 0);
    }

    // Load data for tab
    switch(tabName) {
        case 'dashboard':
            loadStatus();
            break;
        case 'policies':
            loadPolicies();
            break;
        case 'secrets':
            loadSecrets();
            break;
        case 'devices':
            loadDevices();
            break;
        case 'users':
            loadUsers();
            break;
        case 'audit':
            loadAuditLogs();
            break;
        case 'settings':
            loadSettings();
            break;
        case 'rag-connectors':
            loadConnectors();
            break;
        case 'site-scraper':
            if (typeof initSiteScraperTab === 'function') {
                initSiteScraperTab();
            }
            break;
        case 'voice-testing':
            loadTestHistory();
            break;
        case 'hallucination-checks':
            loadHallucinationChecks();
            break;
        case 'multi-intent':
            loadMultiIntentConfig();
            loadIntentChains();
            break;
        case 'validation-models':
            loadValidationModels();
            break;
        case 'external-api-keys':
            loadExternalApiKeys();
            break;
        case 'user-api-keys':
            loadUserApiKeys();
            break;
        case 'llm-backends':
            loadLLMBackends();
            break;
        case 'model-config':
            if (typeof initModelConfigPage === 'function') {
                initModelConfigPage();
            }
            break;
        case 'model-downloads':
            if (typeof initModelDownloadsPage === 'function') {
                initModelDownloadsPage();
            }
            break;
        case 'conversation-context':
            loadConversationSettings();
            break;
        case 'intent-routing':
            // Loaded by routing.js
            break;
        case 'performance-metrics':
            initMetricsPage();
            break;
        case 'guest-mode':
            loadGuestModeData();
            break;
        case 'sms':
            loadSMSData();
            break;
        case 'llm-components':
            loadLLMComponents();
            break;
        case 'service-control':
            loadServiceControl();
            break;
        case 'gateway-settings':
            loadGatewayConfig();
            break;
        case 'directions-settings':
            if (typeof initDirectionsSettingsPage === 'function') {
                initDirectionsSettingsPage();
            }
            break;
        case 'emerging-intents':
            if (typeof initEmergingIntentsPage === 'function') {
                initEmergingIntentsPage();
            }
            break;
        case 'music-config':
            if (typeof initMusicConfigPage === 'function') {
                initMusicConfigPage();
            }
            break;
        case 'room-audio':
            if (typeof initRoomAudioPage === 'function') {
                initRoomAudioPage();
            }
            break;
        case 'room-tv':
            if (typeof loadTVConfigs === 'function') {
                loadTVConfigs();
            }
            break;
        case 'follow-me':
            if (typeof loadFollowMeData === 'function') {
                loadFollowMeData();
            }
            break;
        case 'voice-config':
            if (typeof loadVoiceConfig === 'function') {
                loadVoiceConfig();
            }
            break;
        case 'features':
            if (typeof initFeaturesPage === 'function') {
                initFeaturesPage();
            }
            break;
        case 'system-config':
            if (typeof initSystemConfigPage === 'function') {
                initSystemConfigPage();
            }
            break;
        case 'presets':
            if (typeof initPresetsPage === 'function') {
                initPresetsPage();
            }
            break;
        case 'intent-analytics':
            if (typeof initAnalytics === 'function') {
                initAnalytics();
            }
            break;
        case 'calendar-sources':
            if (typeof initCalendarSources === 'function') {
                initCalendarSources();
            }
            break;
        case 'admin-jarvis':
            if (typeof initAdminJarvis === 'function') {
                initAdminJarvis();
            }
            break;
        case 'voice-automations':
            if (typeof loadVoiceAutomations === 'function') {
                loadVoiceAutomations();
            }
            break;
        case 'alerts':
            if (typeof loadAlertsPage === 'function') {
                loadAlertsPage();
            }
            break;
        case 'mission-control':
            if (window.Athena?.pages?.MissionControl?.init) {
                Athena.pages.MissionControl.init();
            }
            break;
        case 'voice-pipelines':
            if (window.Athena?.pages?.VoicePipelines?.init) {
                Athena.pages.VoicePipelines.init();
            }
            break;
        case 'memory-context':
            if (window.Athena?.pages?.MemoryContext?.init) {
                Athena.pages.MemoryContext.init();
            }
            break;
        case 'integrations':
            if (window.Athena?.pages?.Integrations?.init) {
                Athena.pages.Integrations.init();
            }
            break;
        case 'escalation':
            if (typeof initEscalationPage === 'function') {
                initEscalationPage();
            }
            break;
        case 'debug-logs':
            if (typeof initDebugLogs === 'function') {
                initDebugLogs();
            }
            break;
    }

    // Check for dynamically registered tab callbacks (from tool-calling.js, base-knowledge.js, etc.)
    if (window.tabChangeCallbacks && window.tabChangeCallbacks[tabName]) {
        window.tabChangeCallbacks[tabName]();
    }
}

// ============================================================================
// Dashboard Tab (Existing Service Status)
// ============================================================================

async function loadStatus() {
    const errorContainer = document.getElementById('error-container');
    const statsContainer = document.getElementById('stats-container');
    const servicesContainer = document.getElementById('services-by-group-container');

    errorContainer.innerHTML = '';

    try {
        // Fetch services from the service registry
        const data = await apiRequest('/api/service-registry/services');

        // Update stats
        document.getElementById('stat-healthy').textContent = data.healthy_services;
        document.getElementById('stat-total').textContent = data.total_services;

        const healthStat = document.getElementById('stat-health');
        healthStat.textContent = data.overall_health.toUpperCase();
        healthStat.className = `text-3xl font-bold ${
            data.overall_health === 'healthy' ? 'text-green-400' :
            data.overall_health === 'degraded' ? 'text-yellow-400' : 'text-red-400'
        }`;

        statsContainer.style.display = 'grid';

        // Group services by type
        const servicesByGroup = {
            'RAG Services': [],
            'Core Services': [],
            'Database Services': []
        };

        data.services.forEach(service => {
            // Determine group based on service type or name
            if (service.service_type === 'rag' ||
                ['weather', 'sports', 'airports', 'flights', 'events', 'streaming',
                 'news', 'stocks', 'websearch', 'dining', 'recipes'].includes(service.name)) {
                servicesByGroup['RAG Services'].push(service);
            } else if (service.service_type === 'database' ||
                       service.name.includes('qdrant') || service.name.includes('redis')) {
                servicesByGroup['Database Services'].push(service);
            } else {
                servicesByGroup['Core Services'].push(service);
            }
        });

        // Render services by group
        servicesContainer.innerHTML = Object.entries(servicesByGroup).map(([group, services]) => {
            if (services.length === 0) return ''; // Skip empty groups

            return `
                <div class="mb-6">
                    <h3 class="text-lg font-semibold text-white mb-3">${group}</h3>
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        ${services.map(service => `
                            <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                                <div class="flex items-start justify-between mb-2">
                                    <div class="flex-1">
                                        <h4 class="font-medium text-white">${service.display_name || service.name}</h4>
                                        <p class="text-xs text-gray-400">
                                            ${service.host}:${service.port}
                                        </p>
                                    </div>
                                    <div class="flex items-center gap-2">
                                        <span class="w-2 h-2 rounded-full ${
                                            service.status === 'healthy' ? 'bg-green-500' :
                                            service.status === 'offline' ? 'bg-gray-500' :
                                            service.status === 'disabled' ? 'bg-gray-600' :
                                            'bg-red-500'
                                        }"></span>
                                        <span class="text-xs ${
                                            service.status === 'healthy' ? 'text-green-400' :
                                            service.status === 'offline' ? 'text-gray-400' :
                                            service.status === 'disabled' ? 'text-gray-500' :
                                            'text-red-400'
                                        }">${service.status}</span>
                                    </div>
                                </div>
                                ${service.enabled ?
                                    `<p class="text-xs text-gray-500 flex items-center gap-1">
                                        <span>Cache TTL: ${service.cache_ttl}s</span>${infoIcon('dashboard-cache-ttl')}
                                        <span class="mx-1">|</span>
                                        <span>Timeout: ${service.timeout}s</span>${infoIcon('dashboard-timeout')}
                                    </p>` :
                                    `<p class="text-xs text-yellow-500">Service is disabled</p>`
                                }
                                ${service.health_message ? `
                                    <p class="text-xs text-gray-400 mt-2">${service.health_message}</p>
                                ` : ''}
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }).filter(html => html !== '').join('');

    } catch (error) {
        errorContainer.innerHTML = `
            <div class="bg-red-900/20 border border-red-700/50 rounded-lg p-4 mb-4">
                <p class="text-red-200">Failed to load system status: ${error.message}</p>
            </div>
        `;
    }
}

// ============================================================================
// Policies Tab
// ============================================================================

async function loadPolicies() {
    if (!authToken) {
        document.getElementById('policies-container').innerHTML = `
            <div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4">
                <p class="text-yellow-200">Please login to manage policies</p>
            </div>
        `;
        return;
    }

    try {
        const policies = await apiRequest('/api/policies');

        const container = document.getElementById('policies-container');

        if (policies.length === 0) {
            container.innerHTML = `
                <div class="bg-dark-card border border-dark-border rounded-lg p-8 text-center">
                    <p class="text-gray-400">No policies configured yet</p>
                </div>
            `;
            return;
        }

        container.innerHTML = policies.map(policy => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex items-start justify-between mb-4">
                    <div class="flex-1">
                        <h3 class="text-lg font-semibold text-white">${policy.mode.charAt(0).toUpperCase() + policy.mode.slice(1)} Mode</h3>
                        <p class="text-sm text-gray-400 mt-1">${policy.description || 'No description'}</p>
                        <div class="flex items-center gap-4 mt-2 text-xs text-gray-500">
                            <span>Mode: <span class="text-blue-400">${policy.mode}</span></span>
                            <span>Version: ${policy.version}</span>
                            <span>Created: ${new Date(policy.created_at).toLocaleDateString()}</span>
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        ${policy.active ?
                            '<span class="px-2 py-1 bg-green-900/30 text-green-400 text-xs rounded">Active</span>' :
                            '<span class="px-2 py-1 bg-gray-700 text-gray-400 text-xs rounded">Inactive</span>'
                        }
                        <button onclick="viewPolicyVersions(${policy.id})"
                            class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white text-xs rounded transition-colors">
                            Versions
                        </button>
                        <button onclick="editPolicy(${policy.id})"
                            class="px-3 py-1 bg-gray-600 hover:bg-gray-700 text-white text-xs rounded transition-colors">
                            Edit
                        </button>
                        <button onclick="deletePolicy(${policy.id}, '${policy.mode.charAt(0).toUpperCase() + policy.mode.slice(1)} Mode')"
                            class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors">
                            Delete
                        </button>
                    </div>
                </div>
                <div class="bg-dark-bg rounded p-3 text-xs">
                    <pre class="text-gray-300 overflow-x-auto">${JSON.stringify(policy.config, null, 2)}</pre>
                </div>
            </div>
        `).join('');

    } catch (error) {
        showError(`Failed to load policies: ${error.message}`);
    }
}

function showCreatePolicyModal() {
    const modal = `
        <div id="policy-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='policy-modal') closeModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
                <h2 class="text-xl font-semibold text-white mb-4">Create Policy</h2>
                <form onsubmit="createPolicy(event)" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Name</label>
                        <input type="text" name="name" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Description</label>
                        <textarea name="description" rows="2"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Mode</label>
                        <select name="mode" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                            <option value="fast">Fast (Phi-3 mini)</option>
                            <option value="medium">Medium (Llama 3.1)</option>
                            <option value="custom">Custom Configuration</option>
                            <option value="rag">RAG with Knowledge Base</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Configuration (JSON)</label>
                        <textarea name="config" rows="8" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white font-mono text-sm focus:outline-none focus:border-blue-500"
                            placeholder='{"temperature": 0.7, "max_tokens": 500}'></textarea>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" name="active" id="policy-active" checked
                            class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                        <label for="policy-active" class="text-sm text-gray-300">Active</label>
                    </div>
                    <div class="flex gap-2 pt-4">
                        <button type="submit"
                            class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                            Create Policy
                        </button>
                        <button type="button" onclick="closeModal('policy-modal')"
                            class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                            Cancel
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.getElementById('modals-container').innerHTML = modal;
}

async function createPolicy(event) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);

    try {
        const config = JSON.parse(formData.get('config'));

        await apiRequest('/api/policies', {
            method: 'POST',
            body: JSON.stringify({
                name: formData.get('name'),
                description: formData.get('description'),
                mode: formData.get('mode'),
                config: config,
                active: formData.get('active') === 'on'
            })
        });

        closeModal();
        loadPolicies();
        showSuccess('Policy created successfully');
    } catch (error) {
        showError(`Failed to create policy: ${error.message}`);
    }
}

async function deletePolicy(id, name) {
    if (!confirm(`Delete policy "${name}"? This will soft-delete the policy (can be recovered).`)) {
        return;
    }

    try {
        await apiRequest(`/api/policies/${id}`, { method: 'DELETE' });
        loadPolicies();
        showSuccess('Policy deleted');
    } catch (error) {
        showError(`Failed to delete policy: ${error.message}`);
    }
}

// ============================================================================
// Secrets Tab
// ============================================================================

async function loadSecrets() {
    if (!authToken) {
        document.getElementById('secrets-container').innerHTML = `
            <div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4">
                <p class="text-yellow-200">Please login to manage secrets</p>
            </div>
        `;
        return;
    }

    try {
        const secrets = await apiRequest('/api/secrets');

        const container = document.getElementById('secrets-container');

        if (secrets.length === 0) {
            container.innerHTML = `
                <div class="bg-dark-card border border-dark-border rounded-lg p-8 text-center">
                    <p class="text-gray-400">No secrets stored yet</p>
                </div>
            `;
            return;
        }

        container.innerHTML = secrets.map(secret => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex items-start justify-between">
                    <div class="flex-1">
                        <h3 class="text-lg font-semibold text-white">${secret.name}</h3>
                        <p class="text-sm text-gray-400 mt-1">${secret.description || 'No description'}</p>
                        <div class="flex items-center gap-4 mt-2 text-xs text-gray-500">
                            <span>Type: <span class="text-blue-400">${secret.secret_type}</span></span>
                            <span>Updated: ${new Date(secret.updated_at).toLocaleDateString()}</span>
                            ${secret.last_rotated ? `<span>Rotated: ${new Date(secret.last_rotated).toLocaleDateString()}</span>` : ''}
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <button onclick="revealSecret(${secret.id}, '${secret.name}')"
                            class="px-3 py-1 bg-yellow-600 hover:bg-yellow-700 text-white text-xs rounded transition-colors">
                            Reveal
                        </button>
                        <button onclick="rotateSecret(${secret.id})"
                            class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white text-xs rounded transition-colors">
                            Rotate
                        </button>
                        <button onclick="deleteSecret(${secret.id}, '${secret.name}')"
                            class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors">
                            Delete
                        </button>
                    </div>
                </div>
            </div>
        `).join('');

    } catch (error) {
        showError(`Failed to load secrets: ${error.message}`);
    }
}

function showCreateSecretModal() {
    const modal = `
        <div id="secret-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='secret-modal') closeModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4">
                <h2 class="text-xl font-semibold text-white mb-4">Create Secret</h2>
                <form onsubmit="createSecret(event)" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Name</label>
                        <input type="text" name="name" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Type</label>
                        <select name="secret_type" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                            <option value="api_key">API Key</option>
                            <option value="token">Token</option>
                            <option value="password">Password</option>
                            <option value="certificate">Certificate</option>
                            <option value="other">Other</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Description</label>
                        <textarea name="description" rows="2"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Secret Value</label>
                        <textarea name="value" rows="4" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white font-mono text-sm focus:outline-none focus:border-blue-500"></textarea>
                        <p class="text-xs text-gray-500 mt-1">Value will be encrypted before storage</p>
                    </div>
                    <div class="flex gap-2 pt-4">
                        <button type="submit"
                            class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                            Create Secret
                        </button>
                        <button type="button" onclick="closeModal('secret-modal')"
                            class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                            Cancel
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.getElementById('modals-container').innerHTML = modal;
}

async function createSecret(event) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);

    try {
        await apiRequest('/api/secrets', {
            method: 'POST',
            body: JSON.stringify({
                name: formData.get('name'),
                secret_type: formData.get('secret_type'),
                description: formData.get('description'),
                value: formData.get('value')
            })
        });

        closeModal();
        loadSecrets();
        showSuccess('Secret created and encrypted successfully');
    } catch (error) {
        showError(`Failed to create secret: ${error.message}`);
    }
}

async function revealSecret(id, name) {
    const modal = `
        <div id="reveal-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='reveal-modal') closeModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4">
                <h2 class="text-xl font-semibold text-white mb-4">Reveal Secret: ${name}</h2>
                <div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4 mb-4">
                    <p class="text-yellow-200 text-sm">⚠️ This action will be logged in the audit trail</p>
                </div>
                <div id="secret-value-container" class="bg-dark-bg rounded p-4 mb-4">
                    <p class="text-gray-400 text-center">Loading...</p>
                </div>
                <button onclick="closeModal('reveal-modal')"
                    class="w-full px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                    Close
                </button>
            </div>
        </div>
    `;
    document.getElementById('modals-container').innerHTML = modal;

    try {
        const data = await apiRequest(`/api/secrets/${id}/reveal`);
        document.getElementById('secret-value-container').innerHTML = `
            <pre class="text-white font-mono text-sm overflow-x-auto">${data.value}</pre>
        `;
    } catch (error) {
        document.getElementById('secret-value-container').innerHTML = `
            <p class="text-red-400 text-center">${error.message}</p>
        `;
    }
}

async function deleteSecret(id, name) {
    if (!confirm(`Delete secret "${name}"? This action cannot be undone.`)) {
        return;
    }

    try {
        await apiRequest(`/api/secrets/${id}`, { method: 'DELETE' });
        loadSecrets();
        showSuccess('Secret deleted');
    } catch (error) {
        showError(`Failed to delete secret: ${error.message}`);
    }
}

// ============================================================================
// External API Keys Tab
// ============================================================================

async function loadExternalApiKeys() {
    const container = document.getElementById('external-api-keys-container');

    if (!authToken) {
        container.innerHTML = `<div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4 text-yellow-200">
            Please login to view API keys.
        </div>`;
        return;
    }

    container.innerHTML = `<div class="text-gray-400">Loading API keys...</div>`;

    try {
        const keys = await apiRequest('/api/external-api-keys');

        if (!keys.length) {
            container.innerHTML = `<div class="bg-dark-card border border-dark-border rounded-lg p-6 text-center text-gray-400">
                No external API keys configured yet.
            </div>`;
            return;
        }

        container.innerHTML = keys.map(key => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-4 flex items-center justify-between">
                <div>
                    <div class="text-white font-semibold">${key.service_name} <span class="text-xs text-gray-500">(${key.api_name})</span></div>
                    <div class="text-sm text-gray-400 mt-1">${key.endpoint_url}</div>
                    <div class="text-xs text-gray-500 mt-1">Key: ${key.api_key_masked}</div>
                    <div class="text-xs text-gray-500 mt-1">Status: ${key.enabled ? '<span class="text-green-400">Enabled</span>' : '<span class="text-red-400">Disabled</span>'}</div>
                </div>
                <div class="flex gap-2">
                    <button onclick="showCreateExternalApiKeyModal('${key.service_name}')"
                        class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white text-xs rounded transition-colors">Edit</button>
                    <button onclick="deleteExternalApiKey('${key.service_name}')"
                        class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors">Delete</button>
                </div>
            </div>
        `).join('');
    } catch (error) {
        container.innerHTML = `<div class="bg-red-900/30 border border-red-700/50 rounded-lg p-4 text-red-200">
            Failed to load API keys: ${error.message}
        </div>`;
    }
}

async function showCreateExternalApiKeyModal(serviceName = '') {
    const existing = serviceName ? await apiRequest(`/api/external-api-keys/${serviceName}`) : null;
    const payload = existing || {};

    const modal = `
        <div id="external-api-key-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='external-api-key-modal') closeModal('external-api-key-modal')">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4">
                <h2 class="text-xl font-semibold text-white mb-4">${existing ? 'Update' : 'Create'} External API Key</h2>
                <form onsubmit="saveExternalApiKey(event, '${existing ? existing.service_name : ''}')" class="space-y-4">
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Service Name</label>
                            <input type="text" name="service_name" value="${payload.service_name || ''}" ${existing ? 'readonly' : ''}
                                placeholder="api-football" required
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white">
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">API Name</label>
                            <input type="text" name="api_name" value="${payload.api_name || ''}" placeholder="API-Football.com" required
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white">
                        </div>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Endpoint URL</label>
                        <input type="text" name="endpoint_url" value="${payload.endpoint_url || ''}" placeholder="https://v3.football.api-sports.io" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">API Key</label>
                        <input type="text" name="api_key" value="" placeholder="${existing ? 'Leave blank to keep existing key' : 'Required'}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white">
                    </div>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Rate Limit (per minute)</label>
                            <input type="number" name="rate_limit_per_minute" value="${payload.rate_limit_per_minute || ''}" min="0"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white">
                        </div>
                        <div class="flex items-center gap-2 pt-6">
                            <input type="checkbox" name="enabled" ${payload.enabled !== false ? 'checked' : ''} class="w-4 h-4">
                            <label class="text-sm text-gray-300">Enabled</label>
                        </div>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Description</label>
                        <textarea name="description" rows="2" class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white">${payload.description || ''}</textarea>
                    </div>
                    <div class="flex gap-2 pt-4">
                        <button type="submit" class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded">Save</button>
                        <button type="button" onclick="closeModal('external-api-key-modal')" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `;

    document.getElementById('modals-container').innerHTML = modal;
}

async function saveExternalApiKey(event, existingServiceName = '') {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);

    const payload = {
        service_name: formData.get('service_name'),
        api_name: formData.get('api_name'),
        api_key: formData.get('api_key') || '',
        endpoint_url: formData.get('endpoint_url'),
        enabled: formData.get('enabled') === 'on',
        description: formData.get('description'),
        rate_limit_per_minute: formData.get('rate_limit_per_minute') || null
    };

    try {
        await apiRequest(`/api/external-api-keys${existingServiceName ? `/${existingServiceName}` : ''}`, {
            method: existingServiceName ? 'PUT' : 'POST',
            body: JSON.stringify(payload)
        });
        closeModal('external-api-key-modal');
        loadExternalApiKeys();
        showSuccess(`API key ${existingServiceName ? 'updated' : 'created'} successfully`);
    } catch (error) {
        showError(`Failed to save API key: ${error.message}`);
    }
}

async function deleteExternalApiKey(serviceName) {
    if (!confirm(`Delete API key "${serviceName}"?`)) return;

    try {
        await apiRequest(`/api/external-api-keys/${serviceName}`, { method: 'DELETE' });
        loadExternalApiKeys();
        showSuccess('API key deleted');
    } catch (error) {
        showError(`Failed to delete API key: ${error.message}`);
    }
}

// ============================================================================
// Devices Tab
// ============================================================================

async function loadDevices() {
    if (!authToken) {
        document.getElementById('devices-container').innerHTML = `
            <div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4">
                <p class="text-yellow-200">Please login to view devices</p>
            </div>
        `;
        return;
    }

    try {
        const devices = await apiRequest('/api/devices');

        const container = document.getElementById('devices-container');

        if (devices.length === 0) {
            container.innerHTML = `
                <div class="bg-dark-card border border-dark-border rounded-lg p-8 text-center">
                    <p class="text-gray-400">No devices registered yet</p>
                </div>
            `;
            return;
        }

        // Group by zone
        const devicesByZone = {};
        devices.forEach(device => {
            if (!devicesByZone[device.zone]) {
                devicesByZone[device.zone] = [];
            }
            devicesByZone[device.zone].push(device);
        });

        container.innerHTML = Object.entries(devicesByZone).map(([zone, zoneDevices]) => `
            <div class="mb-6">
                <h3 class="text-lg font-semibold text-white mb-3">${zone}</h3>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    ${zoneDevices.map(device => {
                        const statusColor = device.status === 'online' ? 'green' :
                                          device.status === 'offline' ? 'red' :
                                          device.status === 'degraded' ? 'yellow' : 'gray';

                        return `
                            <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                                <div class="flex items-start justify-between mb-3">
                                    <div class="flex-1">
                                        <h4 class="font-medium text-white">${device.name}</h4>
                                        <p class="text-xs text-gray-400">${device.device_type}</p>
                                    </div>
                                    <span class="px-2 py-1 bg-${statusColor}-900/30 text-${statusColor}-400 text-xs rounded">
                                        ${device.status}
                                    </span>
                                </div>
                                <div class="space-y-1 text-xs text-gray-500">
                                    <div>IP: ${device.ip_address}</div>
                                    ${device.last_seen ? `<div>Last seen: ${new Date(device.last_seen).toLocaleString()}</div>` : ''}
                                </div>
                                ${device.config && Object.keys(device.config).length > 0 ? `
                                    <details class="mt-3">
                                        <summary class="text-xs text-blue-400 cursor-pointer">Configuration</summary>
                                        <pre class="text-xs text-gray-400 mt-2 overflow-x-auto">${JSON.stringify(device.config, null, 2)}</pre>
                                    </details>
                                ` : ''}
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `).join('');

    } catch (error) {
        showError(`Failed to load devices: ${error.message}`);
    }
}

function showCreateDeviceModal() {
    const modal = `
        <div id="device-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='device-modal') closeModal()">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4">
                <h2 class="text-xl font-semibold text-white mb-4">Register Device</h2>
                <form onsubmit="createDevice(event)" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Name</label>
                        <input type="text" name="name" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Device Type</label>
                        <select name="device_type" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                            <option value="wyoming">Wyoming Voice Device</option>
                            <option value="jetson">Jetson Edge Device</option>
                            <option value="service">Service Container</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Zone</label>
                        <input type="text" name="zone" required placeholder="e.g., Office, Kitchen"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">IP Address</label>
                        <input type="text" name="ip_address" required placeholder="e.g., 192.168.1.x"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Configuration (JSON, optional)</label>
                        <textarea name="config" rows="4"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white font-mono text-sm focus:outline-none focus:border-blue-500"
                            placeholder='{"port": 10700, "protocol": "wyoming"}'></textarea>
                    </div>
                    <div class="flex gap-2 pt-4">
                        <button type="submit"
                            class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                            Register Device
                        </button>
                        <button type="button" onclick="closeModal('device-modal')"
                            class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                            Cancel
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.getElementById('modals-container').innerHTML = modal;
}

async function createDevice(event) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);

    try {
        const configStr = formData.get('config');
        const config = configStr ? JSON.parse(configStr) : {};

        await apiRequest('/api/devices', {
            method: 'POST',
            body: JSON.stringify({
                name: formData.get('name'),
                device_type: formData.get('device_type'),
                zone: formData.get('zone'),
                ip_address: formData.get('ip_address'),
                config: config
            })
        });

        closeModal();
        loadDevices();
        showSuccess('Device registered successfully');
    } catch (error) {
        showError(`Failed to register device: ${error.message}`);
    }
}

// ============================================================================
// Users Tab
// ============================================================================

async function loadUsers() {
    if (!authToken) {
        document.getElementById('users-container').innerHTML = `
            <div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4">
                <p class="text-yellow-200">Please login to view users</p>
            </div>
        `;
        return;
    }

    try {
        const users = await apiRequest('/api/users');

        const container = document.getElementById('users-container');

        container.innerHTML = `
            <div class="bg-dark-card border border-dark-border rounded-lg overflow-hidden">
                <table class="w-full">
                    <thead class="bg-dark-bg">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">User</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Role</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Last Login</th>
                            <th class="px-6 py-3 text-right text-xs font-medium text-gray-400 uppercase">Actions</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-dark-border">
                        ${users.map(user => `
                            <tr>
                                <td class="px-6 py-4">
                                    <div class="text-white font-medium">${user.full_name || user.username}</div>
                                    <div class="text-xs text-gray-400">${user.email}</div>
                                </td>
                                <td class="px-6 py-4">
                                    <span class="px-2 py-1 bg-blue-900/30 text-blue-400 text-xs rounded">${user.role}</span>
                                </td>
                                <td class="px-6 py-4">
                                    ${user.is_active ?
                                        '<span class="px-2 py-1 bg-green-900/30 text-green-400 text-xs rounded">Active</span>' :
                                        '<span class="px-2 py-1 bg-red-900/30 text-red-400 text-xs rounded">Inactive</span>'
                                    }
                                </td>
                                <td class="px-6 py-4 text-sm text-gray-400">
                                    ${user.last_login ? new Date(user.last_login).toLocaleString() : 'Never'}
                                </td>
                                <td class="px-6 py-4 text-right">
                                    ${currentUser && currentUser.id !== user.id ? `
                                        <button onclick="updateUserRole(${user.id}, '${user.username}')"
                                            class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white text-xs rounded transition-colors mr-2">
                                            Change Role
                                        </button>
                                        ${user.is_active ?
                                            `<button onclick="deactivateUser(${user.id}, '${user.username}')"
                                                class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors">
                                                Deactivate
                                            </button>` :
                                            `<button onclick="reactivateUser(${user.id}, '${user.username}')"
                                                class="px-3 py-1 bg-green-600 hover:bg-green-700 text-white text-xs rounded transition-colors">
                                                Reactivate
                                            </button>`
                                        }
                                    ` : '<span class="text-xs text-gray-500">Current User</span>'}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;

    } catch (error) {
        showError(`Failed to load users: ${error.message}`);
    }
}

async function updateUserRole(userId, username) {
    const newRole = prompt(`Enter new role for ${username}:\n\nAvailable roles:\n- owner (full access)\n- operator (read/write)\n- viewer (read only)\n- support (read + audit)`, 'viewer');

    if (!newRole) return;

    const validRoles = ['owner', 'operator', 'viewer', 'support'];
    if (!validRoles.includes(newRole)) {
        showError('Invalid role');
        return;
    }

    try {
        await apiRequest(`/api/users/${userId}`, {
            method: 'PUT',
            body: JSON.stringify({ role: newRole })
        });
        loadUsers();
        showSuccess('User role updated');
    } catch (error) {
        showError(`Failed to update user: ${error.message}`);
    }
}

async function deactivateUser(userId, username) {
    if (!confirm(`Deactivate user ${username}? They will not be able to login.`)) {
        return;
    }

    try {
        await apiRequest(`/api/users/${userId}`, { method: 'DELETE' });
        loadUsers();
        showSuccess('User deactivated');
    } catch (error) {
        showError(`Failed to deactivate user: ${error.message}`);
    }
}

async function reactivateUser(userId, username) {
    try {
        await apiRequest(`/api/users/${userId}/reactivate`, { method: 'POST' });
        loadUsers();
        showSuccess('User reactivated');
    } catch (error) {
        showError(`Failed to reactivate user: ${error.message}`);
    }
}

// ============================================================================
// Audit Logs Tab
// ============================================================================

async function loadAuditLogs() {
    if (!authToken) {
        document.getElementById('audit-container').innerHTML = `
            <div class="bg-yellow-900/20 border border-yellow-700/50 rounded-lg p-4">
                <p class="text-yellow-200">Please login to view audit logs</p>
            </div>
        `;
        return;
    }

    try {
        const logs = await apiRequest('/api/audit?limit=50');

        const container = document.getElementById('audit-container');

        if (logs.length === 0) {
            container.innerHTML = `
                <div class="bg-dark-card border border-dark-border rounded-lg p-8 text-center">
                    <p class="text-gray-400">No audit logs yet</p>
                </div>
            `;
            return;
        }

        container.innerHTML = `
            <div class="bg-dark-card border border-dark-border rounded-lg overflow-hidden">
                <table class="w-full">
                    <thead class="bg-dark-bg">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase"></th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Timestamp</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">User</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Action</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Resource</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-dark-border">
                        ${logs.map((log, index) => {
                            const actionColor = log.action === 'create' ? 'green' :
                                              log.action === 'delete' || log.action === 'revoke' ? 'red' :
                                              log.action === 'reveal' ? 'yellow' :
                                              'blue';
                            const statusColor = log.success ? 'green' : 'red';
                            const hasDetails = log.old_value || log.new_value || log.ip_address || log.user_agent || log.error_message;

                            return `
                                <tr class="cursor-pointer hover:bg-dark-bg/50 transition-colors" onclick="toggleAuditDetails(${index})">
                                    <td class="px-4 py-4 text-gray-500">
                                        <span id="audit-chevron-${index}" class="transform transition-transform duration-200">▶</span>
                                    </td>
                                    <td class="px-6 py-4 text-sm text-gray-400 whitespace-nowrap">
                                        ${new Date(log.timestamp).toLocaleString()}
                                    </td>
                                    <td class="px-6 py-4 text-sm text-white">
                                        ${log.username || log.user || 'System'}
                                    </td>
                                    <td class="px-6 py-4">
                                        <span class="px-2 py-1 bg-${actionColor}-900/30 text-${actionColor}-400 text-xs rounded">
                                            ${log.action}
                                        </span>
                                    </td>
                                    <td class="px-6 py-4 text-sm text-gray-300">
                                        ${log.resource_type}${log.resource_id ? ` #${log.resource_id}` : ''}
                                    </td>
                                    <td class="px-6 py-4">
                                        <span class="px-2 py-1 bg-${statusColor}-900/30 text-${statusColor}-400 text-xs rounded">
                                            ${log.success ? 'Success' : 'Failed'}
                                        </span>
                                    </td>
                                </tr>
                                <tr id="audit-details-${index}" class="hidden">
                                    <td colspan="6" class="px-6 py-4 bg-dark-bg/30">
                                        <div class="grid grid-cols-2 gap-4 text-sm">
                                            ${log.ip_address ? `
                                                <div>
                                                    <span class="text-gray-500">IP Address:</span>
                                                    <span class="text-gray-300 ml-2 font-mono">${log.ip_address}</span>
                                                </div>
                                            ` : ''}
                                            ${log.user_agent ? `
                                                <div class="col-span-2">
                                                    <span class="text-gray-500">User Agent:</span>
                                                    <span class="text-gray-400 ml-2 text-xs">${escapeHtml(log.user_agent)}</span>
                                                </div>
                                            ` : ''}
                                            ${log.error_message ? `
                                                <div class="col-span-2">
                                                    <span class="text-red-500">Error:</span>
                                                    <span class="text-red-400 ml-2">${escapeHtml(log.error_message)}</span>
                                                </div>
                                            ` : ''}
                                            ${log.old_value ? `
                                                <div class="col-span-2">
                                                    <div class="text-gray-500 mb-1">Previous Value:</div>
                                                    <pre class="bg-dark-bg border border-dark-border rounded p-2 text-xs text-gray-300 overflow-x-auto">${escapeHtml(JSON.stringify(log.old_value, null, 2))}</pre>
                                                </div>
                                            ` : ''}
                                            ${log.new_value ? `
                                                <div class="col-span-2">
                                                    <div class="text-gray-500 mb-1">New Value:</div>
                                                    <pre class="bg-dark-bg border border-dark-border rounded p-2 text-xs text-gray-300 overflow-x-auto">${escapeHtml(JSON.stringify(log.new_value, null, 2))}</pre>
                                                </div>
                                            ` : ''}
                                            ${!hasDetails ? `
                                                <div class="col-span-2 text-gray-500 italic">No additional details available</div>
                                            ` : ''}
                                        </div>
                                    </td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            </div>
        `;

    } catch (error) {
        showError(`Failed to load audit logs: ${error.message}`);
    }
}

/**
 * Toggle visibility of audit log detail row
 */
function toggleAuditDetails(index) {
    const detailsRow = document.getElementById(`audit-details-${index}`);
    const chevron = document.getElementById(`audit-chevron-${index}`);

    if (detailsRow && chevron) {
        const isHidden = detailsRow.classList.contains('hidden');

        if (isHidden) {
            detailsRow.classList.remove('hidden');
            chevron.textContent = '▼';
        } else {
            detailsRow.classList.add('hidden');
            chevron.textContent = '▶';
        }
    }
}

// ============================================================================
// Utility Functions
// ============================================================================

function closeModal() {
    document.getElementById('modals-container').innerHTML = '';
}

function showSuccess(message) {
    const toast = document.createElement('div');
    toast.className = 'fixed top-4 right-4 bg-green-600 text-white px-6 py-3 rounded-lg shadow-lg z-50';
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 3000);
}

function showError(message) {
    const toast = document.createElement('div');
    toast.className = 'fixed top-4 right-4 bg-red-600 text-white px-6 py-3 rounded-lg shadow-lg z-50';
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 5000);
}

// ============================================================================
// SETTINGS TAB - Server Configuration & Service Registry
// ============================================================================

async function loadSettings() {
    await Promise.all([loadServers(), loadServices()]);
}

async function loadServers() {
    try {
        const response = await fetch(`${API_BASE}/api/servers`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load servers');

        const data = await response.json();
        const container = document.getElementById('servers-container');

        if (data.servers.length === 0) {
            container.innerHTML = '<div class="col-span-full text-gray-400 text-center py-8">No servers configured</div>';
            return;
        }

        container.innerHTML = data.servers.map(server => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                <div class="flex justify-between items-start mb-3">
                    <div>
                        <h4 class="text-lg font-semibold text-white">${escapeHtml(server.name)}</h4>
                        <p class="text-sm text-gray-400">${escapeHtml(server.hostname || '')}</p>
                    </div>
                    <span class="px-2 py-1 rounded text-xs font-medium ${
                        server.status === 'online' ? 'bg-green-900/30 text-green-400' :
                        server.status === 'offline' ? 'bg-red-900/30 text-red-400' :
                        server.status === 'degraded' ? 'bg-yellow-900/30 text-yellow-400' :
                        'bg-gray-700 text-gray-300'
                    }">${server.status}</span>
                </div>

                <div class="space-y-2 text-sm mb-4">
                    <div class="flex justify-between">
                        <span class="text-gray-400">IP Address:</span>
                        <span class="text-gray-200 font-mono">${server.ip_address}</span>
                    </div>
                    <div class="flex justify-between">
                        <span class="text-gray-400">Role:</span>
                        <span class="text-gray-200">${server.role || 'N/A'}</span>
                    </div>
                    ${server.last_checked ? `
                        <div class="flex justify-between">
                            <span class="text-gray-400">Last Checked:</span>
                            <span class="text-gray-200">${new Date(server.last_checked).toLocaleString()}</span>
                        </div>
                    ` : ''}
                </div>

                <div class="flex gap-2">
                    <button onclick="checkServer(${server.id})"
                        class="flex-1 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm font-medium transition-colors">
                        Check Health
                    </button>
                    <button onclick="viewServerServices(${server.id})"
                        class="flex-1 px-3 py-1.5 bg-gray-600 hover:bg-gray-700 text-white rounded text-sm font-medium transition-colors">
                        View Services
                    </button>
                    <button onclick="deleteServer(${server.id})"
                        class="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white rounded text-sm font-medium transition-colors">
                        Delete
                    </button>
                </div>
            </div>
        `).join('');

    } catch (error) {
        console.error('Load servers error:', error);
        showError('Failed to load servers');
    }
}

async function loadServices() {
    try {
        const response = await fetch(`${API_BASE}/api/service-registry/services`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load services');

        const data = await response.json();
        const container = document.getElementById('services-container');

        if (data.services.length === 0) {
            container.innerHTML = '<div class="col-span-full text-gray-400 text-center py-8">No services registered</div>';
            return;
        }

        // Sort services by name
        data.services.sort((a, b) => a.name.localeCompare(b.name));

        container.innerHTML = data.services.map(service => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                <div class="flex justify-between items-start mb-3">
                    <div>
                        <h4 class="text-lg font-semibold text-white">${escapeHtml(service.display_name || service.name)}</h4>
                        <p class="text-sm text-gray-400">${service.host}:${service.port}</p>
                    </div>
                    <span class="px-2 py-1 rounded text-xs font-medium ${
                        service.status === 'healthy' ? 'bg-green-900/30 text-green-400' :
                        service.status === 'offline' ? 'bg-gray-700 text-gray-400' :
                        service.status === 'disabled' ? 'bg-yellow-900/30 text-yellow-400' :
                        service.status === 'error' || service.status === 'unhealthy' ? 'bg-red-900/30 text-red-400' :
                        'bg-gray-700 text-gray-300'
                    }">${service.status}</span>
                </div>

                <div class="space-y-2 text-sm mb-4">
                    <div class="flex justify-between">
                        <span class="text-gray-400">Type:</span>
                        <span class="text-gray-200">${service.service_type || 'rag'}</span>
                    </div>
                    <div class="flex justify-between">
                        <span class="text-gray-400">Cache TTL:</span>
                        <span class="text-gray-200">${service.cache_ttl}s</span>
                    </div>
                    <div class="flex justify-between">
                        <span class="text-gray-400">Timeout:</span>
                        <span class="text-gray-200">${service.timeout}s</span>
                    </div>
                    ${service.rate_limit ? `
                        <div class="flex justify-between">
                            <span class="text-gray-400">Rate Limit:</span>
                            <span class="text-gray-200">${service.rate_limit} req/min</span>
                        </div>
                    ` : ''}
                    ${service.health_message ? `
                        <div class="text-xs text-gray-400 mt-2">${escapeHtml(service.health_message)}</div>
                    ` : ''}
                </div>

                <div class="flex gap-2">
                    <button onclick="toggleService('${service.name}')"
                        class="flex-1 px-3 py-1.5 ${service.enabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded text-sm font-medium transition-colors">
                        ${service.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button onclick="refreshService('${service.name}')"
                        class="flex-1 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm font-medium transition-colors">
                        Refresh
                    </button>
                </div>
            </div>
        `).join('');

    } catch (error) {
        console.error('Load services error:', error);
        showError('Failed to load services');
    }
}

async function checkServer(serverId) {
    try {
        const response = await fetch(`${API_BASE}/api/servers/${serverId}/check`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Health check failed');

        showSuccess('Server health check completed');
        await loadServers();

    } catch (error) {
        console.error('Check server error:', error);
        showError('Server health check failed');
    }
}

async function toggleService(serviceName) {
    try {
        const response = await fetch(`${API_BASE}/api/service-registry/services/${serviceName}/toggle`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to toggle service');

        const result = await response.json();
        showSuccess(result.message);
        await loadServices();

    } catch (error) {
        console.error('Toggle service error:', error);
        showError('Failed to toggle service');
    }
}

async function refreshService(serviceName) {
    try {
        const response = await fetch(`${API_BASE}/api/service-registry/services/${serviceName}/refresh`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to refresh service');

        const result = await response.json();
        showSuccess(result.message);
        await loadServices();

    } catch (error) {
        console.error('Refresh service error:', error);
        showError('Failed to refresh service');
    }
}

async function checkService(serviceId) {
    try {
        const response = await fetch(`${API_BASE}/api/services/${serviceId}/check`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Health check failed');

        showSuccess('Service health check completed');
        await loadServices();

    } catch (error) {
        console.error('Check service error:', error);
        showError('Service health check failed');
    }
}

async function refreshAllServices() {
    try {
        const response = await fetch(`${API_BASE}/api/services/status/all`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to refresh services');

        const data = await response.json();
        showSuccess(`Checked ${data.checked} services: ${data.healthy} healthy, ${data.unhealthy} unhealthy`);
        await loadServices();

    } catch (error) {
        console.error('Refresh services error:', error);
        showError('Failed to refresh all services');
    }
}

function showCreateServerModal() {
    const modal = `
        <div class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" id="create-server-modal">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-md w-full mx-4">
                <h3 class="text-xl font-semibold text-white mb-4">Add Server</h3>

                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Server Name</label>
                        <input type="text" id="server-name" placeholder="mac-studio"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Hostname</label>
                        <input type="text" id="server-hostname" placeholder="Jays-Mac-Studio.local"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">IP Address</label>
                        <input type="text" id="server-ip" placeholder="e.g., 192.168.1.x" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                    </div>

                    <div>
                        <label class="block text-sm font-medium text-gray-300 mb-1">Role</label>
                        <select id="server-role"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                            <option value="compute">Compute</option>
                            <option value="storage">Storage</option>
                            <option value="integration">Integration</option>
                            <option value="other">Other</option>
                        </select>
                    </div>
                </div>

                <div class="flex gap-3 mt-6">
                    <button onclick="createServer()"
                        class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                        Create Server
                    </button>
                    <button onclick="closeModal('create-server-modal')"
                        class="flex-1 px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                        Cancel
                    </button>
                </div>
            </div>
        </div>
    `;

    document.getElementById('modals-container').innerHTML = modal;
}

async function createServer() {
    const name = document.getElementById('server-name').value.trim();
    const hostname = document.getElementById('server-hostname').value.trim();
    const ip = document.getElementById('server-ip').value.trim();
    const role = document.getElementById('server-role').value;

    if (!name || !ip) {
        showError('Name and IP address are required');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/servers`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ name, hostname, ip_address: ip, role })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create server');
        }

        showSuccess('Server created successfully');
        closeModal('create-server-modal');
        await loadServers();

    } catch (error) {
        console.error('Create server error:', error);
        showError(error.message);
    }
}

async function deleteServer(serverId) {
    if (!confirm('Are you sure you want to delete this server? All associated services will also be deleted.')) {
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/servers/${serverId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to delete server');

        showSuccess('Server deleted successfully');
        await loadServers();

    } catch (error) {
        console.error('Delete server error:', error);
        showError('Failed to delete server');
    }
}

async function viewServerServices(serverId) {
    try {
        const response = await fetch(`${API_BASE}/api/servers/${serverId}/services`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load server services');

        const data = await response.json();

        const modal = `
            <div class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" id="server-services-modal">
                <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-auto">
                    <h3 class="text-xl font-semibold text-white mb-4">Services on ${escapeHtml(data.server_name || 'Server')}</h3>

                    ${data.services.length === 0 ?
                        '<p class="text-gray-400">No services registered on this server</p>' :
                        `<div class="space-y-3">
                            ${data.services.map(service => `
                                <div class="bg-dark-bg border border-dark-border rounded p-3">
                                    <div class="flex justify-between items-start">
                                        <div>
                                            <h4 class="font-medium text-white">${escapeHtml(service.service_name)}</h4>
                                            <p class="text-sm text-gray-400">Port ${service.port} (${service.protocol || 'http'})</p>
                                        </div>
                                        <span class="px-2 py-1 rounded text-xs font-medium ${
                                            service.status === 'online' ? 'bg-green-900/30 text-green-400' :
                                            service.status === 'offline' ? 'bg-red-900/30 text-red-400' :
                                            service.status === 'degraded' ? 'bg-yellow-900/30 text-yellow-400' :
                                            'bg-gray-700 text-gray-300'
                                        }">${service.status}</span>
                                    </div>
                                </div>
                            `).join('')}
                        </div>`
                    }

                    <button onclick="closeModal('server-services-modal')"
                        class="w-full mt-4 px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                        Close
                    </button>
                </div>
            </div>
        `;

        document.getElementById('modals-container').innerHTML = modal;

    } catch (error) {
        console.error('View server services error:', error);
        showError('Failed to load server services');
    }
}

// ============================================================================
// RAG CONNECTORS TAB
// ============================================================================

async function loadConnectors() {
    try {
        const response = await fetch(`${API_BASE}/api/rag-connectors`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load connectors');

        const data = await response.json();
        const container = document.getElementById('connectors-container');

        if (data.connectors.length === 0) {
            container.innerHTML = '<div class="col-span-full text-gray-400 text-center py-8">No RAG connectors configured</div>';
            return;
        }

        container.innerHTML = data.connectors.map(connector => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                <div class="flex justify-between items-start mb-3">
                    <div>
                        <h4 class="text-lg font-semibold text-white">${escapeHtml(connector.name)}</h4>
                        <p class="text-sm text-gray-400">${connector.connector_type.replace('_', ' ')}</p>
                    </div>
                    <label class="relative inline-flex items-center cursor-pointer">
                        <input type="checkbox" ${connector.enabled ? 'checked' : ''}
                               onchange="toggleConnector(${connector.id}, this.checked)"
                               class="sr-only peer">
                        <div class="w-11 h-6 bg-gray-600 peer-focus:outline-none rounded-full peer
                                    peer-checked:after:translate-x-full peer-checked:after:border-white
                                    after:content-[''] after:absolute after:top-[2px] after:left-[2px]
                                    after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all
                                    peer-checked:bg-green-600"></div>
                    </label>
                </div>

                ${connector.service ? `
                    <div class="space-y-2 text-sm mb-4">
                        <div class="flex justify-between">
                            <span class="text-gray-400">Service:</span>
                            <span class="text-gray-200">${escapeHtml(connector.service.service_name)}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-gray-400">Server:</span>
                            <span class="text-gray-200">${escapeHtml(connector.service.server.name)}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-gray-400">Endpoint:</span>
                            <span class="text-gray-200 font-mono text-xs">${connector.service.server.ip_address}:${connector.service.port}</span>
                        </div>
                    </div>
                ` : ''}

                ${connector.last_test_at ? `
                    <div class="bg-dark-bg rounded p-2 mb-4 text-sm">
                        <div class="flex justify-between">
                            <span class="text-gray-400">Last Test:</span>
                            <span class="text-gray-200">${new Date(connector.last_test_at).toLocaleString()}</span>
                        </div>
                        ${connector.last_test_success !== null ? `
                            <div class="flex justify-between mt-1">
                                <span class="text-gray-400">Status:</span>
                                <span class="${connector.last_test_success ? 'text-green-400' : 'text-red-400'}">
                                    ${connector.last_test_success ? '✓ Success' : '✗ Failed'}
                                </span>
                            </div>
                        ` : ''}
                    </div>
                ` : ''}

                <div class="flex gap-2">
                    <button onclick="testConnector(${connector.id})"
                        class="flex-1 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm font-medium transition-colors">
                        Test
                    </button>
                    <button onclick="viewConnectorStats(${connector.id})"
                        class="flex-1 px-3 py-1.5 bg-gray-600 hover:bg-gray-700 text-white rounded text-sm font-medium transition-colors">
                        Stats
                    </button>
                    <button onclick="deleteConnector(${connector.id})"
                        class="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white rounded text-sm font-medium transition-colors">
                        Delete
                    </button>
                </div>
            </div>
        `).join('');

    } catch (error) {
        console.error('Load connectors error:', error);
        showError('Failed to load RAG connectors');
    }
}

async function testConnector(connectorId) {
    try {
        const response = await fetch(`${API_BASE}/api/rag-connectors/${connectorId}/test`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ test_query: 'test' })
        });

        if (!response.ok) throw new Error('Test failed');

        const data = await response.json();

        const resultHtml = `
            <div class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" id="test-result-modal">
                <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-auto">
                    <h3 class="text-xl font-semibold text-white mb-4">Connector Test Results</h3>

                    <div class="space-y-4">
                        <div class="flex items-center gap-2">
                            <span class="text-2xl">${data.test_result.success ? '✅' : '❌'}</span>
                            <span class="text-lg font-semibold ${data.test_result.success ? 'text-green-400' : 'text-red-400'}">
                                ${data.test_result.success ? 'Test Passed' : 'Test Failed'}
                            </span>
                        </div>

                        ${data.test_result.response_time ? `
                            <div class="bg-dark-bg rounded p-3">
                                <span class="text-gray-400">Response Time:</span>
                                <span class="text-white font-semibold ml-2">${data.test_result.response_time}ms</span>
                            </div>
                        ` : ''}

                        ${data.test_result.sample_data ? `
                            <div class="bg-dark-bg rounded p-3">
                                <div class="text-gray-400 mb-2">Sample Data:</div>
                                <pre class="text-xs text-gray-200 overflow-auto">${JSON.stringify(data.test_result.sample_data, null, 2)}</pre>
                            </div>
                        ` : ''}

                        ${data.test_result.error ? `
                            <div class="bg-red-900/20 border border-red-700 rounded p-3">
                                <div class="text-red-400 font-medium mb-1">Error:</div>
                                <div class="text-red-300 text-sm">${escapeHtml(data.test_result.error)}</div>
                            </div>
                        ` : ''}
                    </div>

                    <button onclick="closeModal('test-result-modal'); loadConnectors();"
                        class="w-full mt-4 px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                        Close
                    </button>
                </div>
            </div>
        `;

        document.getElementById('modals-container').innerHTML = resultHtml;

    } catch (error) {
        console.error('Test connector error:', error);
        showError('Connector test failed');
    }
}

async function toggleConnector(connectorId, enabled) {
    try {
        const response = await fetch(`${API_BASE}/api/rag-connectors/${connectorId}`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ enabled })
        });

        if (!response.ok) throw new Error('Failed to toggle connector');

        showSuccess(`Connector ${enabled ? 'enabled' : 'disabled'} successfully`);
        await loadConnectors();

    } catch (error) {
        console.error('Toggle connector error:', error);
        showError('Failed to toggle connector');
        await loadConnectors(); // Reload to reset checkbox
    }
}

async function deleteConnector(connectorId) {
    if (!confirm('Are you sure you want to delete this RAG connector?')) {
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/rag-connectors/${connectorId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to delete connector');

        showSuccess('Connector deleted successfully');
        await loadConnectors();

    } catch (error) {
        console.error('Delete connector error:', error);
        showError('Failed to delete connector');
    }
}

async function viewConnectorStats(connectorId) {
    try {
        const response = await fetch(`${API_BASE}/api/rag-connectors/${connectorId}/stats`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load stats');

        const data = await response.json();

        const modal = `
            <div class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" id="stats-modal">
                <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4">
                    <h3 class="text-xl font-semibold text-white mb-4">Connector Statistics</h3>

                    ${data.stats.length === 0 ?
                        '<p class="text-gray-400">No usage statistics available</p>' :
                        `<div class="space-y-3">
                            ${data.stats.map(stat => `
                                <div class="bg-dark-bg border border-dark-border rounded p-3">
                                    <div class="grid grid-cols-2 gap-3 text-sm">
                                        <div>
                                            <span class="text-gray-400">Date:</span>
                                            <span class="text-white ml-2">${new Date(stat.date).toLocaleDateString()}</span>
                                        </div>
                                        <div>
                                            <span class="text-gray-400">Queries:</span>
                                            <span class="text-white ml-2">${stat.query_count}</span>
                                        </div>
                                        <div>
                                            <span class="text-gray-400">Avg Response:</span>
                                            <span class="text-white ml-2">${stat.avg_response_time}ms</span>
                                        </div>
                                        <div>
                                            <span class="text-gray-400">Cache Hit Rate:</span>
                                            <span class="text-white ml-2">${stat.cache_hit_rate.toFixed(1)}%</span>
                                        </div>
                                    </div>
                                </div>
                            `).join('')}
                        </div>`
                    }

                    <button onclick="closeModal('stats-modal')"
                        class="w-full mt-4 px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                        Close
                    </button>
                </div>
            </div>
        `;

        document.getElementById('modals-container').innerHTML = modal;

    } catch (error) {
        console.error('View stats error:', error);
        showError('Failed to load connector statistics');
    }
}

function showCreateConnectorModal() {
    // First, we need to load available services
    fetch(`${API_BASE}/api/services`, {
        headers: { 'Authorization': `Bearer ${getToken()}` }
    })
    .then(response => response.json())
    .then(data => {
        const modal = `
            <div class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" id="create-connector-modal">
                <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-md w-full mx-4">
                    <h3 class="text-xl font-semibold text-white mb-4">Add RAG Connector</h3>

                    <div class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Connector Name</label>
                            <input type="text" id="connector-name" placeholder="weather"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                        </div>

                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Type</label>
                            <select id="connector-type"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                                <option value="external_api">External API</option>
                                <option value="vector_db">Vector Database</option>
                                <option value="cache">Cache</option>
                            </select>
                        </div>

                        <div>
                            <label class="block text-sm font-medium text-gray-300 mb-1">Service</label>
                            <select id="connector-service"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-gray-200">
                                <option value="">None</option>
                                ${data.services.map(service =>
                                    `<option value="${service.id}">${service.service_name} (${service.server_name || 'Unknown'})</option>`
                                ).join('')}
                            </select>
                        </div>

                        <div>
                            <label class="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" id="connector-enabled" checked
                                    class="w-4 h-4 bg-dark-bg border border-dark-border rounded">
                                <span class="text-sm text-gray-300">Enabled</span>
                            </label>
                        </div>
                    </div>

                    <div class="flex gap-3 mt-6">
                        <button onclick="createConnector()"
                            class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                            Create Connector
                        </button>
                        <button onclick="closeModal('create-connector-modal')"
                            class="flex-1 px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                            Cancel
                        </button>
                    </div>
                </div>
            </div>
        `;

        document.getElementById('modals-container').innerHTML = modal;
    })
    .catch(error => {
        console.error('Load services error:', error);
        showError('Failed to load services for connector creation');
    });
}

async function createConnector() {
    const name = document.getElementById('connector-name').value.trim();
    const type = document.getElementById('connector-type').value;
    const serviceId = document.getElementById('connector-service').value;
    const enabled = document.getElementById('connector-enabled').checked;

    if (!name || !type) {
        showError('Name and type are required');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/rag-connectors`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                name,
                connector_type: type,
                service_id: serviceId || null,
                enabled,
                config: {},
                cache_config: type === 'cache' ? { ttl: 3600 } : null
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create connector');
        }

        showSuccess('Connector created successfully');
        closeModal('create-connector-modal');
        await loadConnectors();

    } catch (error) {
        console.error('Create connector error:', error);
        showError(error.message);
    }
}

// ============================================================================
// VOICE TESTING TAB
// ============================================================================

async function testLLM() {
    const prompt = document.getElementById('llm-prompt').value.trim();
    const model = document.getElementById('llm-model').value;
    const resultsDiv = document.getElementById('llm-results');

    if (!prompt) {
        showError('Please enter a prompt');
        return;
    }

    resultsDiv.innerHTML = '<div class="text-gray-400 text-sm">Running test...</div>';

    try {
        const response = await fetch(`${API_BASE}/api/voice-tests/llm/test`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ text: prompt, model })
        });

        if (!response.ok) throw new Error('LLM test failed');

        const data = await response.json();

        resultsDiv.innerHTML = `
            <div class="bg-dark-bg rounded p-3 space-y-2">
                <div class="flex items-center gap-2">
                    <span class="text-2xl">${data.success ? '✅' : '❌'}</span>
                    <span class="font-semibold ${data.success ? 'text-green-400' : 'text-red-400'}">
                        ${data.success ? 'Test Passed' : 'Test Failed'}
                    </span>
                </div>

                <div class="grid grid-cols-2 gap-2 text-sm">
                    <div>
                        <span class="text-gray-400">Model:</span>
                        <span class="text-white ml-2">${data.model}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Response Time:</span>
                        <span class="text-white ml-2">${data.processing_time}ms</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Tokens:</span>
                        <span class="text-white ml-2">${data.tokens}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Tokens/sec:</span>
                        <span class="text-white ml-2">${data.tokens_per_second}</span>
                    </div>
                </div>

                <div class="mt-3">
                    <div class="text-gray-400 text-sm mb-1">Response:</div>
                    <div class="text-white text-sm bg-dark-card border border-dark-border rounded p-2 max-h-48 overflow-auto">
                        ${escapeHtml(data.response)}
                    </div>
                </div>
            </div>
        `;

        showSuccess('LLM test completed');

    } catch (error) {
        console.error('LLM test error:', error);
        resultsDiv.innerHTML = `<div class="text-red-400 text-sm">Error: ${error.message}</div>`;
        showError('LLM test failed');
    }
}

function updateRAGPlaceholder() {
    const connector = document.getElementById('rag-connector').value;
    const queryInput = document.getElementById('rag-query');

    const placeholders = {
        'weather': 'Enter city name (e.g., Seattle, Boston)...',
        'airports': 'Enter airport code (e.g., BOS, SEA, LAX)...',
        'flights': 'Enter flight number (e.g., AA100, DL123)...'
    };

    queryInput.placeholder = placeholders[connector] || 'Enter query...';
}

async function testRAG() {
    const query = document.getElementById('rag-query').value.trim();
    const connector = document.getElementById('rag-connector').value;
    const resultsDiv = document.getElementById('rag-results');

    if (!query) {
        showError('Please enter a query');
        return;
    }

    resultsDiv.innerHTML = '<div class="text-gray-400 text-sm">Running test...</div>';

    try {
        const response = await fetch(`${API_BASE}/api/voice-tests/rag/test`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ text: query, connector })
        });

        if (!response.ok) throw new Error('RAG test failed');

        const data = await response.json();

        resultsDiv.innerHTML = `
            <div class="bg-dark-bg rounded p-3 space-y-2">
                <div class="flex items-center gap-2">
                    <span class="text-2xl">${data.success ? '✅' : '❌'}</span>
                    <span class="font-semibold ${data.success ? 'text-green-400' : 'text-red-400'}">
                        ${data.success ? 'Test Passed' : 'Test Failed'}
                    </span>
                </div>

                <div class="grid grid-cols-2 gap-2 text-sm">
                    <div>
                        <span class="text-gray-400">Connector:</span>
                        <span class="text-white ml-2">${data.connector}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Response Time:</span>
                        <span class="text-white ml-2">${data.processing_time}ms</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Cached:</span>
                        <span class="text-white ml-2">${data.cached ? 'Yes' : 'No'}</span>
                    </div>
                </div>

                <div class="mt-3">
                    <div class="text-gray-400 text-sm mb-1">Response:</div>
                    <div class="text-white text-sm bg-dark-card border border-dark-border rounded p-2 max-h-48 overflow-auto">
                        <pre class="text-xs">${JSON.stringify(data.response, null, 2)}</pre>
                    </div>
                </div>
            </div>
        `;

        showSuccess('RAG test completed');

    } catch (error) {
        console.error('RAG test error:', error);
        resultsDiv.innerHTML = `<div class="text-red-400 text-sm">Error: ${error.message}</div>`;
        showError('RAG test failed');
    }
}

async function testFullPipeline() {
    const text = document.getElementById('pipeline-text').value.trim();
    const resultsDiv = document.getElementById('pipeline-results');
    const timingsDiv = document.getElementById('pipeline-timings');

    if (!text) {
        showError('Please enter a query');
        return;
    }

    resultsDiv.innerHTML = '<div class="text-gray-400 text-sm">Running full pipeline test...</div>';
    timingsDiv.innerHTML = '';

    try {
        const response = await fetch(`${API_BASE}/api/voice-tests/pipeline/test`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ text })
        });

        if (!response.ok) throw new Error('Pipeline test failed');

        const data = await response.json();

        // Store test ID for feedback
        lastTestId = data.test_id;

        resultsDiv.innerHTML = `
            <div class="bg-dark-bg rounded p-3 space-y-3">
                <div class="flex items-center gap-2">
                    <span class="text-2xl">${data.success ? '✅' : '❌'}</span>
                    <span class="font-semibold ${data.success ? 'text-green-400' : 'text-red-400'}">
                        ${data.success ? 'Test Passed' : 'Test Failed'}
                    </span>
                </div>

                <div class="grid grid-cols-2 gap-2 text-sm">
                    <div>
                        <span class="text-gray-400">Total Time:</span>
                        <span class="text-white ml-2">${data.total_time}ms</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Target Met:</span>
                        <span class="text-white ml-2">${data.target_met ? 'Yes (<5s)' : 'No (>5s)'}</span>
                    </div>
                </div>

                ${data.results && data.results.llm_response ? `
                    <div class="mt-3 p-3 bg-gray-900 rounded border border-gray-700">
                        <div class="flex items-center justify-between mb-2">
                            <div class="text-gray-400 text-sm font-semibold">LLM Response:</div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-500 text-xs">Was this response correct?</span>
                                <button onclick="markResponseFeedback(event, 'correct')"
                                    class="feedback-btn px-3 py-1 rounded text-sm bg-green-600 hover:bg-green-700 text-white transition-colors">
                                    ✓ Correct
                                </button>
                                <button onclick="markResponseFeedback(event, 'incorrect')"
                                    class="feedback-btn px-3 py-1 rounded text-sm bg-red-600 hover:bg-red-700 text-white transition-colors">
                                    ✗ Wrong
                                </button>
                            </div>
                        </div>
                        <div class="text-white text-sm whitespace-pre-wrap">${data.results.llm_response}</div>
                        <div class="feedback-message mt-2 text-sm"></div>
                    </div>
                ` : ''}

                ${data.note ? `
                    <div class="text-yellow-400 text-sm mt-2">
                        ℹ️ ${data.note}
                    </div>
                ` : ''}
            </div>
        `;

        // Display stage timings
        if (data.timings) {
            const stages = Object.entries(data.timings);
            timingsDiv.innerHTML = `
                <div class="bg-dark-bg rounded p-3">
                    <div class="text-gray-400 text-sm mb-2">Stage Timings:</div>
                    <div class="space-y-2">
                        ${stages.map(([stage, time]) => `
                            <div class="flex justify-between items-center">
                                <span class="text-gray-300 text-sm">${stage.toUpperCase()}:</span>
                                <div class="flex items-center gap-2">
                                    <div class="w-32 bg-gray-700 rounded-full h-2">
                                        <div class="bg-blue-500 h-2 rounded-full"
                                             style="width: ${Math.min(100, (time / data.total_time) * 100)}%"></div>
                                    </div>
                                    <span class="text-white text-sm font-mono w-16 text-right">${time}ms</span>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }

        showSuccess('Pipeline test completed');
        await loadTestHistory();

    } catch (error) {
        console.error('Pipeline test error:', error);
        resultsDiv.innerHTML = `<div class="text-red-400 text-sm">Error: ${error.message}</div>`;
        showError('Pipeline test failed');
    }
}

// Store the last test ID for feedback
let lastTestId = null;

async function markResponseFeedback(event, feedback) {
    const btn = event.target;
    const feedbackMessageDiv = btn.closest('.bg-gray-900').querySelector('.feedback-message');
    const feedbackBtns = btn.closest('.flex.items-center.gap-2').querySelectorAll('.feedback-btn');

    // Disable all feedback buttons
    feedbackBtns.forEach(b => b.disabled = true);

    try {
        feedbackMessageDiv.innerHTML = '<span class="text-gray-400">Saving feedback...</span>';

        // Store feedback via API
        const response = await fetch(`${API_BASE}/api/voice-tests/feedback`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({
                test_id: lastTestId,
                feedback: feedback,
                query: document.getElementById('pipeline-text').value.trim()
            })
        });

        if (!response.ok) {
            throw new Error('Failed to save feedback');
        }

        const data = await response.json();

        // Show success message
        const icon = feedback === 'correct' ? '✓' : '✗';
        const color = feedback === 'correct' ? 'text-green-400' : 'text-red-400';
        feedbackMessageDiv.innerHTML = `<span class="${color}">${icon} Feedback recorded! The system will learn from this.</span>`;

        // Re-enable buttons after a delay
        setTimeout(() => {
            feedbackBtns.forEach(b => b.disabled = false);
            feedbackMessageDiv.innerHTML = '';
        }, 3000);

    } catch (error) {
        console.error('Feedback error:', error);
        feedbackMessageDiv.innerHTML = '<span class="text-red-400">Failed to save feedback</span>';
        feedbackBtns.forEach(b => b.disabled = false);
    }
}

async function loadTestHistory() {
    try {
        const response = await fetch(`${API_BASE}/api/voice-tests/tests/history?limit=10`, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load test history');

        const data = await response.json();
        const container = document.getElementById('test-history-container');

        if (data.tests.length === 0) {
            container.innerHTML = '<div class="text-gray-400 text-center py-8">No test history available</div>';
            return;
        }

        container.innerHTML = data.tests.map(test => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-4">
                <div class="flex justify-between items-start mb-2">
                    <div>
                        <h4 class="font-semibold text-white">${test.test_type.replace('_', ' ').toUpperCase()}</h4>
                        <p class="text-sm text-gray-400">${new Date(test.executed_at).toLocaleString()}</p>
                    </div>
                    <span class="px-2 py-1 rounded text-xs font-medium ${
                        test.success ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'
                    }">${test.success ? 'Success' : 'Failed'}</span>
                </div>

                <div class="text-sm text-gray-300 mb-2">
                    <span class="text-gray-400">Input:</span> ${escapeHtml(test.test_input.substring(0, 100))}${test.test_input.length > 100 ? '...' : ''}
                </div>

                ${test.result && test.result.processing_time ? `
                    <div class="text-sm text-gray-400">
                        Response Time: <span class="text-white">${test.result.processing_time}ms</span>
                    </div>
                ` : ''}

                ${!test.success && test.error_message ? `
                    <div class="text-sm text-red-400 mt-2">
                        Error: ${escapeHtml(test.error_message)}
                    </div>
                ` : ''}
            </div>
        `).join('');

    } catch (error) {
        console.error('Load test history error:', error);
        showError('Failed to load test history');
    }
}

// ============================================================================
// Hallucination Checks Tab
// ============================================================================

async function loadHallucinationChecks() {
    try {
        const response = await fetch(`${API_BASE}/api/hallucination-checks`, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        if (!response.ok) throw new Error('Failed to load hallucination checks');

        const data = await response.json();
        const container = document.getElementById('hallucination-checks-container');

        if (data.hallucination_checks.length === 0) {
            container.innerHTML = '<p class="text-gray-400">No hallucination checks configured yet.</p>';
            return;
        }

        container.innerHTML = data.hallucination_checks.map(check => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex justify-between items-start mb-4">
                    <div>
                        <h3 class="text-lg font-semibold text-white">${check.display_name}</h3>
                        <p class="text-sm text-gray-400 mt-1">${check.description || 'No description'}</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="px-3 py-1 rounded-full text-xs font-medium ${check.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-900/30 text-gray-400'}">
                            ${check.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                        <button onclick="deleteHallucinationCheck(${check.id}, '${check.display_name.replace(/'/g, "\\'")}')" class="text-red-400 hover:text-red-300 p-2">
                            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                            </svg>
                        </button>
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-4 text-sm">
                    <div>
                        <span class="text-gray-400">Type:</span>
                        <span class="text-white ml-2">${check.check_type}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Severity:</span>
                        <span class="text-white ml-2">${check.severity}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Priority:</span>
                        <span class="text-white ml-2">${check.priority}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Confidence:</span>
                        <span class="text-white ml-2">${check.confidence_threshold}</span>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load hallucination checks:', error);
        showError('Failed to load hallucination checks');
    }
}

function showCreateHallucinationCheckModal() {
    showError('Creating hallucination checks via UI is coming soon. Use the API for now.');
}

// ============================================================================
// Multi-Intent Config Tab
// ============================================================================

async function loadMultiIntentConfig() {
    try {
        const response = await fetch(`${API_BASE}/api/multi-intent/config`, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        if (!response.ok) throw new Error('Failed to load multi-intent config');

        const config = await response.json();
        const container = document.getElementById('multi-intent-config-container');

        container.innerHTML = `
            <div class="flex justify-between items-center mb-6">
                <h3 class="text-lg font-semibold text-white">Configuration</h3>
                <button onclick='showEditMultiIntentConfigModal(${JSON.stringify(config).replace(/'/g, "\\'")})'
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm">
                    Edit Configuration
                </button>
            </div>
            <div class="grid grid-cols-2 gap-6">
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Enabled</label>
                    <span class="px-3 py-1 rounded-full text-xs font-medium ${config.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-900/30 text-gray-400'}">
                        ${config.enabled ? 'Yes' : 'No'}
                    </span>
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Max Intents Per Query</label>
                    <span class="text-white">${config.max_intents_per_query}</span>
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Parallel Processing</label>
                    <span class="text-white">${config.parallel_processing ? 'Yes' : 'No'}</span>
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Combination Strategy</label>
                    <span class="text-white">${config.combination_strategy}</span>
                </div>
                <div class="col-span-2">
                    <label class="block text-sm font-medium text-gray-400 mb-2">Separators</label>
                    <div class="flex flex-wrap gap-2">
                        ${config.separators.map(sep => `
                            <span class="px-2 py-1 bg-dark-bg rounded text-xs text-gray-300">"${sep}"</span>
                        `).join('')}
                    </div>
                </div>
            </div>
        `;
    } catch (error) {
        console.error('Failed to load multi-intent config:', error);
        showError('Failed to load multi-intent configuration');
    }
}

async function loadIntentChains() {
    try {
        const response = await fetch(`${API_BASE}/api/multi-intent/chains`, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        if (!response.ok) throw new Error('Failed to load intent chains');

        const data = await response.json();
        const container = document.getElementById('intent-chains-container');

        if (data.intent_chains.length === 0) {
            container.innerHTML = '<p class="text-gray-400">No intent chain rules configured yet.</p>';
            return;
        }

        container.innerHTML = data.intent_chains.map(chain => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex justify-between items-start mb-4">
                    <div>
                        <h3 class="text-lg font-semibold text-white">${chain.name}</h3>
                        <p class="text-sm text-gray-400 mt-1">${chain.description || 'No description'}</p>
                    </div>
                    <span class="px-3 py-1 rounded-full text-xs font-medium ${chain.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-900/30 text-gray-400'}">
                        ${chain.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                </div>
                <div class="mb-3">
                    <span class="text-sm text-gray-400">Trigger Pattern:</span>
                    <code class="ml-2 text-xs bg-dark-bg px-2 py-1 rounded text-blue-400">${chain.trigger_pattern || 'N/A'}</code>
                </div>
                <div>
                    <span class="text-sm text-gray-400">Intent Sequence:</span>
                    <div class="flex flex-wrap gap-2 mt-2">
                        ${chain.intent_sequence.map((intent, idx) => `
                            <span class="px-2 py-1 bg-dark-bg rounded text-xs text-white">
                                ${idx + 1}. ${intent}
                            </span>
                        `).join('')}
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load intent chains:', error);
        showError('Failed to load intent chains');
    }
}

function showCreateIntentChainModal() {
    showError('Creating intent chains via UI is coming soon. Use the API for now.');
}

// ============================================================================
// Validation Models Tab
// ============================================================================

async function loadValidationModels() {
    try {
        const response = await fetch(`${API_BASE}/api/validation-models`, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        if (!response.ok) throw new Error('Failed to load validation models');

        const data = await response.json();
        const container = document.getElementById('validation-models-container');

        if (data.validation_models.length === 0) {
            container.innerHTML = '<p class="text-gray-400 col-span-3">No validation models configured yet.</p>';
            return;
        }

        container.innerHTML = data.validation_models.map(model => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex justify-between items-start mb-4">
                    <div>
                        <h3 class="text-lg font-semibold text-white">${model.name}</h3>
                        <p class="text-sm text-gray-400 mt-1">${model.model_id}</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="px-3 py-1 rounded-full text-xs font-medium ${model.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-900/30 text-gray-400'}">
                            ${model.enabled ? 'Active' : 'Inactive'}
                        </span>
                        <button onclick="deleteValidationModel(${model.id}, '${model.name.replace(/'/g, "\\'")}')" class="text-red-400 hover:text-red-300 p-2">
                            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                            </svg>
                        </button>
                    </div>
                </div>
                <div class="space-y-2 text-sm">
                    <div>
                        <span class="text-gray-400">Type:</span>
                        <span class="text-white ml-2 capitalize">${model.model_type}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Temperature:</span>
                        <span class="text-white ml-2">${model.temperature}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Max Tokens:</span>
                        <span class="text-white ml-2">${model.max_tokens}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Weight:</span>
                        <span class="text-white ml-2">${model.weight}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Min Confidence:</span>
                        <span class="text-white ml-2">${model.min_confidence_required}</span>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load validation models:', error);
        showError('Failed to load validation models');
    }
}

function showCreateValidationModelModal() {
    showError('Creating validation models via UI is coming soon. Use the API for now.');
}

// ============================================================================
// LLM BACKEND MANAGEMENT
// ============================================================================

async function loadLLMBackends() {
    try {
        const backends = await apiRequest('/api/llm-backends');

        // Calculate stats
        const totalBackends = backends.length;
        const enabledBackends = backends.filter(b => b.enabled).length;
        const avgTokensPerSec = backends.reduce((sum, b) => sum + (b.avg_tokens_per_sec || 0), 0) / (backends.filter(b => b.avg_tokens_per_sec).length || 1);
        const totalRequests = backends.reduce((sum, b) => sum + (b.total_requests || 0), 0);

        // Update stats
        document.getElementById('stat-total-backends').textContent = totalBackends;
        document.getElementById('stat-enabled-backends').textContent = enabledBackends;
        document.getElementById('stat-avg-tokens-per-sec').textContent = avgTokensPerSec.toFixed(1);
        document.getElementById('stat-total-requests').textContent = totalRequests.toLocaleString();

        // Render backends list
        const container = document.getElementById('llm-backends-container');
        if (backends.length === 0) {
            container.innerHTML = `
                <div class="bg-dark-card border border-dark-border rounded-lg p-8 text-center">
                    <div class="text-4xl mb-4">⚡</div>
                    <h3 class="text-lg font-semibold text-white mb-2">No Backends Configured</h3>
                    <p class="text-gray-400 mb-4">Get started by adding your first LLM backend</p>
                    <button onclick="showCreateBackendModal()" class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium transition-colors">
                        ➕ Add Backend
                    </button>
                </div>
            `;
            return;
        }

        container.innerHTML = backends.map(backend => `
            <div class="bg-dark-card border border-dark-border rounded-lg p-6">
                <div class="flex justify-between items-start mb-4">
                    <div class="flex-1">
                        <div class="flex items-center gap-3 mb-2">
                            <h3 class="text-lg font-semibold text-white">${backend.model_name}</h3>
                            <span class="px-3 py-1 rounded-full text-xs font-medium ${
                                backend.backend_type === 'ollama' ? 'bg-blue-900/30 text-blue-400' :
                                backend.backend_type === 'mlx' ? 'bg-purple-900/30 text-purple-400' :
                                'bg-orange-900/30 text-orange-400'
                            }">
                                ${backend.backend_type.toUpperCase()}
                            </span>
                            <span class="px-3 py-1 rounded-full text-xs font-medium ${
                                backend.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-900/30 text-gray-400'
                            }">
                                ${backend.enabled ? 'Enabled' : 'Disabled'}
                            </span>
                        </div>
                        <p class="text-sm text-gray-400">${backend.endpoint_url}</p>
                        ${backend.description ? `<p class="text-sm text-gray-500 mt-1">${backend.description}</p>` : ''}
                    </div>
                    <div class="flex gap-2">
                        <button onclick="toggleBackend(${backend.id})"
                            class="px-3 py-1 ${backend.enabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded text-sm transition-colors"
                            title="${backend.enabled ? 'Disable' : 'Enable'}">
                            ${backend.enabled ? '⏸️' : '▶️'}
                        </button>
                        <button onclick="showEditBackendModal(${backend.id})"
                            class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors"
                            title="Edit">
                            ✏️
                        </button>
                        <button onclick="deleteBackend(${backend.id}, '${backend.model_name}')"
                            class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors"
                            title="Delete">
                            🗑️
                        </button>
                    </div>
                </div>

                <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                    <div>
                        <div class="text-2xl font-bold text-yellow-400">${backend.avg_tokens_per_sec ? backend.avg_tokens_per_sec.toFixed(1) : '-'}</div>
                        <div class="text-xs text-gray-400">Avg Tokens/Sec</div>
                    </div>
                    <div>
                        <div class="text-2xl font-bold text-purple-400">${backend.avg_latency_ms ? backend.avg_latency_ms.toFixed(0) : '-'}</div>
                        <div class="text-xs text-gray-400">Avg Latency (ms)</div>
                    </div>
                    <div>
                        <div class="text-2xl font-bold text-blue-400">${backend.total_requests || 0}</div>
                        <div class="text-xs text-gray-400">Total Requests</div>
                    </div>
                    <div>
                        <div class="text-2xl font-bold ${backend.total_errors > 0 ? 'text-red-400' : 'text-green-400'}">${backend.total_errors || 0}</div>
                        <div class="text-xs text-gray-400">Errors</div>
                    </div>
                </div>

                <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                    <div>
                        <span class="text-gray-400">Priority:</span>
                        <span class="text-white ml-2">${backend.priority}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Max Tokens:</span>
                        <span class="text-white ml-2">${backend.max_tokens}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Temp Default:</span>
                        <span class="text-white ml-2">${backend.temperature_default}</span>
                    </div>
                    <div>
                        <span class="text-gray-400">Timeout:</span>
                        <span class="text-white ml-2">${backend.timeout_seconds}s</span>
                    </div>
                </div>
            </div>
        `).join('');

        // Also load LLM memory settings when loading backends
        await loadLLMMemorySettings();

    } catch (error) {
        console.error('Failed to load LLM backends:', error);
        showError('Failed to load LLM backends');
    }
}

// ============================================================================
// LLM MEMORY SETTINGS
// ============================================================================

async function loadLLMMemorySettings() {
    try {
        const settings = await apiRequest('/api/settings/llm-memory');

        // Update checkbox
        const keepLoadedCheckbox = document.getElementById('llm-keep-models-loaded');
        if (keepLoadedCheckbox) {
            keepLoadedCheckbox.checked = settings.keep_models_loaded;
        }

        // Update select
        const keepAliveSelect = document.getElementById('llm-keep-alive-seconds');
        if (keepAliveSelect) {
            keepAliveSelect.value = settings.default_keep_alive_seconds.toString();
        }

        // Update description
        const descriptionEl = document.getElementById('llm-keep-alive-description');
        if (descriptionEl) {
            descriptionEl.textContent = settings.keep_alive_description;
        }

        console.log('LLM memory settings loaded:', settings);
    } catch (error) {
        console.error('Failed to load LLM memory settings:', error);
        // Don't show error - settings might not be configured yet
    }
}

async function saveLLMMemorySettings() {
    try {
        const keepModelsLoaded = document.getElementById('llm-keep-models-loaded').checked;
        const keepAliveSeconds = parseInt(document.getElementById('llm-keep-alive-seconds').value);

        const response = await apiRequest('/api/settings/llm-memory', {
            method: 'POST',
            body: JSON.stringify({
                keep_models_loaded: keepModelsLoaded,
                default_keep_alive_seconds: keepAliveSeconds
            })
        });

        showSuccess('LLM memory settings saved successfully');

        // Update description based on selection
        updateKeepAliveDescription(keepAliveSeconds);

        console.log('LLM memory settings saved:', response);
    } catch (error) {
        console.error('Failed to save LLM memory settings:', error);
        showError('Failed to save LLM memory settings: ' + (error.message || 'Unknown error'));
    }
}

function updateKeepAliveDescription(seconds) {
    const descriptionEl = document.getElementById('llm-keep-alive-description');
    if (!descriptionEl) return;

    if (seconds === -1) {
        descriptionEl.textContent = 'Models stay loaded forever (until manually unloaded)';
    } else if (seconds === 0) {
        descriptionEl.textContent = 'Models unload immediately after each request';
    } else {
        const minutes = seconds / 60;
        if (minutes >= 1) {
            descriptionEl.textContent = `Models unload after ${minutes} minute(s) of inactivity`;
        } else {
            descriptionEl.textContent = `Models unload after ${seconds} second(s) of inactivity`;
        }
    }
}

// Add change listener for keep_alive select to update description
document.addEventListener('DOMContentLoaded', () => {
    const keepAliveSelect = document.getElementById('llm-keep-alive-seconds');
    if (keepAliveSelect) {
        keepAliveSelect.addEventListener('change', (e) => {
            updateKeepAliveDescription(parseInt(e.target.value));
        });
    }
});

function showCreateBackendModal() {
    const modal = document.createElement('div');
    modal.id = 'create-backend-modal';
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div class="flex justify-between items-center mb-6">
                <h2 class="text-xl font-semibold text-white">Add LLM Backend</h2>
                <button onclick="closeModal('create-backend-modal')" class="text-gray-400 hover:text-white">✕</button>
            </div>

            <form onsubmit="createBackend(event)" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Model Name *</label>
                        <input type="text" id="backend-model-name" required
                            placeholder="e.g., phi3:mini, llama3.1:8b"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Backend Type *</label>
                        <select id="backend-type" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                            <option value="ollama">Ollama</option>
                            <option value="mlx">MLX</option>
                            <option value="auto">Auto (MLX → Ollama fallback)</option>
                        </select>
                    </div>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Endpoint URL *</label>
                    <input type="text" id="backend-endpoint" required
                        placeholder="http://localhost:11434"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Description</label>
                    <input type="text" id="backend-description"
                        placeholder="Optional description"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Priority</label>
                        <input type="number" id="backend-priority" value="100"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                        <p class="text-xs text-gray-500 mt-1">Lower = higher priority for auto mode</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Timeout (seconds)</label>
                        <input type="number" id="backend-timeout" value="60"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Max Tokens</label>
                        <input type="number" id="backend-max-tokens" value="2048"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Default Temperature</label>
                        <input type="number" id="backend-temperature" value="0.7" step="0.1" min="0" max="2"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                </div>

                <div class="flex items-center gap-2">
                    <input type="checkbox" id="backend-enabled" checked
                        class="w-4 h-4 bg-dark-bg border-dark-border rounded">
                    <label for="backend-enabled" class="text-sm text-gray-400">Enable backend</label>
                </div>

                <div class="flex justify-end gap-3 pt-4">
                    <button type="button" onclick="closeModal('create-backend-modal')"
                        class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm transition-colors">
                        Cancel
                    </button>
                    <button type="submit"
                        class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm transition-colors">
                        Create Backend
                    </button>
                </div>
            </form>
        </div>
    `;
    document.body.appendChild(modal);
}

async function createBackend(event) {
    event.preventDefault();

    const data = {
        model_name: document.getElementById('backend-model-name').value,
        backend_type: document.getElementById('backend-type').value,
        endpoint_url: document.getElementById('backend-endpoint').value,
        description: document.getElementById('backend-description').value || null,
        priority: parseInt(document.getElementById('backend-priority').value),
        timeout_seconds: parseInt(document.getElementById('backend-timeout').value),
        max_tokens: parseInt(document.getElementById('backend-max-tokens').value),
        temperature_default: parseFloat(document.getElementById('backend-temperature').value),
        enabled: document.getElementById('backend-enabled').checked
    };

    try {
        await apiRequest('/api/llm-backends', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        closeModal('create-backend-modal');
        loadLLMBackends();
        showSuccess('LLM backend created successfully');
    } catch (error) {
        showError(`Failed to create backend: ${error.message}`);
    }
}

async function toggleBackend(backendId) {
    try {
        await apiRequest(`/api/llm-backends/${backendId}/toggle`, { method: 'POST' });
        loadLLMBackends();
        showSuccess('Backend status updated');
    } catch (error) {
        showError(`Failed to toggle backend: ${error.message}`);
    }
}

async function deleteBackend(backendId, modelName) {
    if (!confirm(`Are you sure you want to delete the backend for "${modelName}"?`)) {
        return;
    }

    try {
        await apiRequest(`/api/llm-backends/${backendId}`, { method: 'DELETE' });
        loadLLMBackends();
        showSuccess('Backend deleted successfully');
    } catch (error) {
        showError(`Failed to delete backend: ${error.message}`);
    }
}

function showEditBackendModal(backendId) {
    // For now, show a simple message - full edit modal can be added later
    showError('Edit modal coming soon. Delete and recreate for now.');
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.remove();
    }
}

// ============================================================================
// TOOLTIP SYSTEM
// ============================================================================

// Centralized tooltip content registry
const TOOLTIP_CONTENT = {
    // Dashboard
    'dashboard-health': 'Overall system health status based on the health of all registered services. Green = all healthy, Yellow = some degraded, Red = critical failures.',
    'dashboard-healthy': 'Number of services currently responding to health checks successfully.',
    'dashboard-total': 'Total number of services registered in the system.',
    'dashboard-cache-ttl': 'Time-to-live for cached responses. After this period, fresh data will be fetched.',
    'dashboard-timeout': 'Maximum time to wait for a service response before considering it failed.',

    // Policies
    'policies-mode': 'Operating mode determines access level and available features. Owner mode has full access, Guest mode has restricted capabilities.',
    'policies-description': 'A brief description of what this policy controls and when it applies.',

    // Secrets
    'secrets-name': 'Unique identifier for this secret. Used to reference this secret in configurations.',
    'secrets-type': 'The type of secret: API key, password, certificate, or other sensitive data.',

    // External API Keys
    'api-keys-service': 'The internal service name that will use this API key for external calls.',
    'api-keys-masked': 'API keys are masked for security. Only the last 4 characters are shown.',
    'api-keys-endpoint': 'The external API endpoint this key is used to authenticate with.',

    // Devices
    'devices-name': 'Friendly name for this device. Used for display purposes.',
    'devices-type': 'The type of device: speaker, display, light, sensor, etc.',
    'devices-zone': 'The physical zone or room where this device is located.',

    // LLM Configuration
    'llm-intent-model': 'The AI model used to classify user intents. Faster models reduce latency but may be less accurate.',
    'llm-validation-model': 'Model used to validate responses before sending to users. Helps prevent hallucinations.',
    'llm-command-model': 'Model used to generate the final response to the user\'s query.',
    'llm-backend': 'The LLM provider: OpenAI (cloud), Ollama (local), or MLX (Apple Silicon optimized).',

    // Intent Routing
    'routing-priority': 'Higher priority routes are evaluated first. Use lower numbers for more specific patterns.',
    'routing-rag': 'Enable RAG (Retrieval Augmented Generation) to fetch real-time data for this intent.',
    'routing-websearch': 'Enable web search fallback when RAG services don\'t have the answer.',
    'routing-pattern': 'Regex pattern to match user queries. Use (?i) for case-insensitive matching.',
    'routing-category': 'The intent category this pattern belongs to (e.g., weather, sports, general).',
    'routing-keyword': 'The keyword or phrase that triggers this intent classification.',

    // Tool Calling
    'tool-enabled': 'When enabled, this tool can be invoked by the LLM to perform actions.',
    'tool-confidence': 'Minimum confidence score (0-1) required for the LLM to execute this tool.',
    'tool-timeout': 'Maximum time to wait for tool execution before timing out.',
    'tool-parallel': 'Maximum number of tools that can execute simultaneously.',
    'tool-guest-allowed': 'Whether this tool can be used when the system is in guest mode.',
    'tool-category': 'Grouping category for organizational purposes.',
    'tool-temperature': 'Controls randomness in LLM responses. Lower = more deterministic.',

    // Guest Mode
    'guest-ical-url': 'URL to an iCal feed for automatic guest schedule synchronization.',
    'guest-checkin': 'The date and time when the current guest\'s stay begins.',
    'guest-checkout': 'The date and time when the current guest\'s stay ends.',
    'guest-airbnb-kb': 'Knowledge base of property-specific information for guest queries.',
    'guest-name': 'Name of the current guest for personalized responses.',
    'guest-contact': 'Contact information for the current guest.',

    // Conversation Context
    'conv-session-timeout': 'How long to maintain conversation context before starting fresh.',
    'conv-history-length': 'Number of previous exchanges to include for context.',
    'conv-clarification': 'When enabled, the system will ask clarifying questions for ambiguous queries.',
    'conv-sports-teams': 'Your favorite sports teams for personalized updates.',

    // Metrics
    'metrics-tps': 'Tokens per second - measures LLM processing speed. Higher is better.',
    'metrics-latency': 'Time from request to response. Lower is better.',
    'metrics-success-rate': 'Percentage of requests that completed successfully.',
    'metrics-model': 'The LLM model used for this request.',
    'metrics-backend': 'The backend provider that processed this request.',

    // Analytics
    'analytics-intent-coverage': 'Percentage of user intents that have configured routing rules.',
    'analytics-service-gaps': 'Intents that users frequently ask about but have no RAG service.',
    'analytics-classification': 'How user queries are being classified into intent categories.',

    // Base Knowledge
    'knowledge-category': 'Type of knowledge: property info, location data, user preferences, or temporal context.',
    'knowledge-priority': 'Higher priority knowledge is used when there are conflicts. Range: 0-100.',
    'knowledge-applies-to': 'Who this knowledge applies to: both modes, guest only, or owner only.',
    'knowledge-key': 'Unique identifier for this knowledge entry.',
    'knowledge-value': 'The actual content of this knowledge entry.',

    // Service Control
    'service-status': 'Current health status of the service based on the most recent health check.',
    'service-start': 'Start this RAG service. It will begin responding to queries.',
    'service-stop': 'Stop this RAG service. Queries will fall back to other services or web search.',
    'service-enabled': 'Whether this service is enabled and will receive requests.',

    // Features
    'feature-required': 'This feature is required for core functionality and cannot be disabled.',
    'feature-latency': 'Average time this feature adds to request processing.',
    'feature-hit-rate': 'Percentage of requests where this feature\'s cache was used.',
    'feature-enabled': 'Toggle this feature on or off. Some features may affect system performance.',

    // Hallucination Checks
    'hallucination-check': 'Validation rule to detect and prevent AI hallucinations in responses.',
    'hallucination-severity': 'Impact level if this check fails: low, medium, or high.',
    'hallucination-pattern': 'The pattern or rule used to detect potential hallucinations.',

    // Multi-Intent
    'multi-intent-enabled': 'Allow processing multiple intents in a single user query.',
    'multi-intent-max': 'Maximum number of intents to process in a single request.',
    'multi-intent-chain': 'Configuration for how intents should be chained together.',

    // Validation Models
    'validation-model-name': 'Identifier for this validation model configuration.',
    'validation-model-threshold': 'Minimum confidence score required to pass validation.',

    // RAG Connectors
    'rag-connector-url': 'The endpoint URL where this RAG service can be reached.',
    'rag-connector-enabled': 'Whether this connector is active and available for queries.',
    'rag-connector-type': 'The type of RAG service (weather, sports, news, etc.).',

    // Voice Testing
    'voice-test-llm': 'Test the LLM response quality without RAG or external services.',
    'voice-test-rag': 'Test RAG service responses for a specific query.',
    'voice-test-pipeline': 'Test the full voice assistant pipeline end-to-end.',

    // LLM Backends
    'backend-name': 'Display name for this LLM backend configuration.',
    'backend-type': 'Provider type: OpenAI API, Ollama (local), or MLX (Apple Silicon).',
    'backend-url': 'API endpoint for this backend. Leave empty for cloud providers.',
    'backend-default': 'Set as the default backend for all LLM operations.',
    'backend-model': 'The model to use with this backend.',

    // OIDC Settings
    'oidc-provider': 'The OpenID Connect provider URL (e.g., Authentik, Keycloak).',
    'oidc-client-id': 'Client ID from your OIDC provider application.',
    'oidc-client-secret': 'Client secret from your OIDC provider. Keep this confidential.',
    'oidc-redirect': 'URL where the OIDC provider should redirect after authentication.',

    // Users
    'users-role': 'User role determines access level: admin has full access, user has restricted access.',
    'users-active': 'Whether this user account is active and can log in.',

    // Audit
    'audit-action': 'The type of action that was performed.',
    'audit-user': 'The user who performed this action.',
    'audit-timestamp': 'When this action occurred.',

    // Guest Mode Extended
    'guest-current-name': 'Currently checked-in guest. The system is in guest mode and will use guest-appropriate responses.',
    'guest-upcoming-name': 'Guest with an upcoming reservation. The system will automatically switch to guest mode at check-in.',
    'guest-history-name': 'Historical guest entry. Manual entries can be edited or deleted. Calendar-synced entries are read-only.',

    // Knowledge Extended
    'knowledge-status': 'Whether this knowledge entry is active. Disabled entries won\'t be used in responses.',

    // Service Control Extended
    'service-name': 'The display name and internal identifier for this service.',
    'service-endpoint': 'The IP address and port where this service is running.',

    // Ollama Models
    'ollama-model-name': 'The Ollama model name. Click to load/unload the model from memory.',
    'ollama-model-size': 'Disk space used by this model. Loaded models also use VRAM/RAM.',
    'ollama-model-status': 'Whether the model is currently loaded in memory. Loaded models respond faster.',

    // Metrics Table Extended
    'metrics-timestamp': 'When this LLM request was processed.',
    'metrics-tokens-sec': 'Generation speed. Higher values mean faster responses.',
    'metrics-tokens': 'Total tokens generated in this response.',
    'metrics-source': 'What triggered this request (voice, API, scheduled, etc.).',
    'metrics-intent': 'The classified intent for this query.',
    'metrics-session-id': 'Unique identifier linking related requests in a conversation.',

    // Metrics Stats Extended
    'metrics-total-requests': 'Total number of LLM requests in the selected time period.',
    'metrics-avg-tokens': 'Average generation speed across all requests. Higher is better.',
    'metrics-avg-latency': 'Average time to complete requests. Lower is better.',
    'metrics-total-tokens': 'Total tokens generated. Useful for usage tracking and cost estimation.',

    // Analytics Extended
    'analytics-total-queries': 'Total queries processed in the selected time range.',
    'analytics-unique-intents': 'Number of distinct intent types identified across all queries.',
    'analytics-rag-coverage': 'Percentage of queries that were handled by dedicated RAG services.',
    'analytics-intent-name': 'The classified intent type for user queries.',
    'analytics-intent-count': 'How many times this intent was triggered.',
    'analytics-intent-percentage': 'This intent\'s share of total queries.',
    'analytics-rag-service': 'Whether a dedicated RAG service exists for this intent.',
    'analytics-system-mapping': 'The internal system route used to handle this intent.',

    // LLM Components
    'llm-component-name': 'The system component that uses LLM for processing.',
    'llm-component-model': 'The Ollama model assigned to this component. Change to optimize for speed or quality.',
    'llm-component-temperature': 'Controls response randomness (0-2). Lower = more consistent, Higher = more creative.',
    'llm-component-status': 'Whether this component is enabled. Disabled components use fallback behavior.',
};

// Currently visible tooltip element
let activeTooltip = null;
let activeIcon = null;

/**
 * Create tooltip container if it doesn't exist
 */
function ensureTooltipContainer() {
    let container = document.getElementById('tooltip-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'tooltip-container';
        document.body.appendChild(container);
    }
    return container;
}

/**
 * Generate info icon HTML for use in template literals
 * @param {string} tooltipKey - Key to look up in TOOLTIP_CONTENT
 * @param {string} [customText] - Optional custom tooltip text (overrides key lookup)
 * @returns {string} HTML string for the info icon
 */
function infoIcon(tooltipKey, customText = null) {
    const text = customText || TOOLTIP_CONTENT[tooltipKey] || tooltipKey;
    const escapedText = text.replace(/'/g, '&#39;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return `<span class="info-icon" tabindex="0" role="button" aria-label="More information" data-tooltip="${escapedText}" onmouseenter="showTooltip(event)" onmouseleave="hideTooltip()" onfocus="showTooltip(event)" onblur="hideTooltip()" onclick="toggleTooltip(event)">i</span>`;
}

/**
 * Show tooltip at the icon position
 */
function showTooltip(event) {
    const icon = event.currentTarget;
    const text = icon.getAttribute('data-tooltip');
    if (!text) return;

    // Don't show if already showing for this icon
    if (activeIcon === icon && activeTooltip) return;

    // Hide any existing tooltip
    hideTooltip();

    const container = ensureTooltipContainer();

    // Create tooltip element
    const tooltip = document.createElement('div');
    tooltip.className = 'tooltip';
    tooltip.innerHTML = `
        <div class="tooltip-arrow"></div>
        <div class="tooltip-content">${text}</div>
    `;

    container.appendChild(tooltip);

    // Position the tooltip
    positionTooltip(tooltip, icon);

    // Show with animation
    requestAnimationFrame(() => {
        tooltip.classList.add('visible');
    });

    activeTooltip = tooltip;
    activeIcon = icon;
}

/**
 * Position tooltip relative to icon, avoiding viewport edges
 */
function positionTooltip(tooltip, icon) {
    const iconRect = icon.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const arrow = tooltip.querySelector('.tooltip-arrow');

    const padding = 10;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    let top, left;
    let arrowPosition = 'top'; // Default: tooltip above, arrow points down

    // Try to position above first
    top = iconRect.top - tooltipRect.height - padding;
    left = iconRect.left + (iconRect.width / 2) - (tooltipRect.width / 2);

    // If too high, position below
    if (top < padding) {
        top = iconRect.bottom + padding;
        arrowPosition = 'bottom';
    }

    // If too far right, adjust left
    if (left + tooltipRect.width > viewportWidth - padding) {
        left = viewportWidth - tooltipRect.width - padding;
    }

    // If too far left, adjust
    if (left < padding) {
        left = padding;
    }

    tooltip.style.top = `${top}px`;
    tooltip.style.left = `${left}px`;

    // Update arrow position
    arrow.className = `tooltip-arrow ${arrowPosition}`;

    // Adjust arrow horizontal position to point at icon
    const arrowLeft = iconRect.left + (iconRect.width / 2) - left;
    arrow.style.left = `${Math.max(16, Math.min(arrowLeft, tooltipRect.width - 16))}px`;
    arrow.style.marginLeft = '0';
}

/**
 * Hide the active tooltip
 */
function hideTooltip() {
    if (activeTooltip) {
        activeTooltip.classList.remove('visible');
        setTimeout(() => {
            if (activeTooltip && activeTooltip.parentNode) {
                activeTooltip.parentNode.removeChild(activeTooltip);
            }
            activeTooltip = null;
            activeIcon = null;
        }, 150);
    }
}

/**
 * Toggle tooltip (for mobile tap)
 */
function toggleTooltip(event) {
    event.preventDefault();
    event.stopPropagation();

    const icon = event.currentTarget;

    if (activeIcon === icon && activeTooltip) {
        hideTooltip();
    } else {
        showTooltip(event);
    }
}

// Close tooltip on escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        hideTooltip();
    }
});

// Close tooltip when clicking outside
document.addEventListener('click', (e) => {
    if (activeTooltip && !e.target.closest('.info-icon') && !e.target.closest('.tooltip')) {
        hideTooltip();
    }
});

// Hide tooltip on scroll
document.addEventListener('scroll', hideTooltip, true);

// Make functions available globally
window.infoIcon = infoIcon;
window.showTooltip = showTooltip;
window.hideTooltip = hideTooltip;
window.toggleTooltip = toggleTooltip;
window.TOOLTIP_CONTENT = TOOLTIP_CONTENT;
