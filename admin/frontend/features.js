/**
 * Feature Flag Management UI - Redesigned
 *
 * Professional, visually appealing feature flag management with:
 * - Rich feature cards with icons and descriptions
 * - Pros and cons for each feature
 * - Latency impact indicators (High/Medium/Low/None)
 * - Visual toggle switches with status badges
 * - Grouped by category with summary stats
 *
 * Uses Phase 0 modules: ApiClient, RefreshManager, AppState
 */

let featuresData = [];
let impactData = [];
let whatIfScenarios = [];
let mlxApplicabilityData = null;

// Module lifecycle management
const FEATURES_REFRESH_INTERVAL = 30000; // 30 seconds

// Feature metadata with pros, cons, and latency impact
const featureMetadata = {
    // Processing Layer
    'intent_classification': {
        icon: '🧠',
        pros: ['Routes queries to appropriate services', 'Enables multi-service orchestration', 'Improves response accuracy'],
        cons: ['Adds ~50-100ms latency', 'Requires LLM inference'],
        latencyImpact: 'medium',
        latencyMs: '50-100ms'
    },
    'multi_intent': {
        icon: '🔀',
        pros: ['Handles complex multi-part queries', 'Better user experience for compound requests', 'Reduces follow-up questions'],
        cons: ['Increases processing time for simple queries', 'Higher compute cost'],
        latencyImpact: 'medium',
        latencyMs: '+100-200ms'
    },
    'conversation_context': {
        icon: '💬',
        pros: ['Maintains conversation continuity', 'Enables follow-up questions', 'More natural interactions'],
        cons: ['Requires session management', 'Memory overhead per session'],
        latencyImpact: 'low',
        latencyMs: '+10-30ms'
    },
    'streaming_responses': {
        icon: '⚡',
        pros: ['Faster perceived response time', 'Progressive content delivery', 'Better UX for long responses'],
        cons: ['More complex error handling', 'Client must support streaming'],
        latencyImpact: 'none',
        latencyMs: 'Reduces TTFB'
    },

    // RAG Layer
    'weather_rag': {
        icon: '🌤️',
        pros: ['Real-time weather data', 'Location-aware forecasts', 'Multi-day predictions'],
        cons: ['Requires API calls', 'Dependent on external service'],
        latencyImpact: 'medium',
        latencyMs: '+150-300ms'
    },
    'sports_rag': {
        icon: '🏈',
        pros: ['Live scores and schedules', 'Team standings', 'Player stats'],
        cons: ['API rate limits', 'Data freshness varies'],
        latencyImpact: 'medium',
        latencyMs: '+100-250ms'
    },
    'airports_rag': {
        icon: '✈️',
        pros: ['Flight status tracking', 'Delay information', 'Airport conditions'],
        cons: ['Requires flight API access', 'Limited to tracked flights'],
        latencyImpact: 'medium',
        latencyMs: '+200-400ms'
    },
    'dining_rag': {
        icon: '🍽️',
        pros: ['Restaurant recommendations', 'Reviews and ratings', 'Reservation info'],
        cons: ['Location data required', 'API costs'],
        latencyImpact: 'medium',
        latencyMs: '+150-350ms'
    },
    'news_rag': {
        icon: '📰',
        pros: ['Current events', 'Topic-based filtering', 'Source diversity'],
        cons: ['Content moderation needed', 'Freshness varies'],
        latencyImpact: 'low',
        latencyMs: '+100-200ms'
    },
    'stocks_rag': {
        icon: '📈',
        pros: ['Real-time quotes', 'Market trends', 'Portfolio tracking'],
        cons: ['Market hours limitations', 'Data delays possible'],
        latencyImpact: 'low',
        latencyMs: '+50-150ms'
    },

    // Optimization Layer
    'room_detection_cache': {
        icon: '🏠',
        pros: ['Faster room identification', 'Reduces HA API calls', 'Lower latency for repeated queries'],
        cons: ['Cache may become stale', 'Memory usage'],
        latencyImpact: 'high',
        latencyMs: '-100-200ms saved'
    },
    'session_warmup': {
        icon: '🔥',
        pros: ['Pre-fetches session on wake word', 'Reduces cold start latency', 'Smoother user experience'],
        cons: ['May pre-fetch unnecessarily', 'Slight resource overhead'],
        latencyImpact: 'high',
        latencyMs: '-150-300ms saved'
    },
    'precomputed_summaries': {
        icon: '📋',
        pros: ['Faster context loading', 'Reduced LLM token usage', 'Consistent summaries'],
        cons: ['Storage overhead', 'Summary may miss nuances'],
        latencyImpact: 'high',
        latencyMs: '-200-400ms saved'
    },
    'parallel_initialization': {
        icon: '⚡',
        pros: ['Concurrent service startup', 'Faster request initialization', 'Better resource utilization'],
        cons: ['More complex debugging', 'Potential race conditions'],
        latencyImpact: 'high',
        latencyMs: '-100-250ms saved'
    },
    'intent_prerouting': {
        icon: '🎯',
        pros: ['Fast routing for simple queries', 'Skips orchestrator for basic commands', 'Sub-second responses possible'],
        cons: ['May misclassify edge cases', 'Requires lightweight model'],
        latencyImpact: 'high',
        latencyMs: '-500-1000ms saved'
    },
    'simple_command_fastpath': {
        icon: '⏩',
        pros: ['Direct HA API calls for simple commands', 'Bypasses LLM for known patterns', 'Near-instant response'],
        cons: ['Limited to predefined patterns', 'Less flexible'],
        latencyImpact: 'high',
        latencyMs: '-800-1500ms saved'
    },
    'mlx_backend': {
        icon: '🖥️',
        pros: ['Apple Silicon optimized', 'Faster local inference', 'Lower power consumption'],
        cons: ['Mac-only', 'Limited model support'],
        latencyImpact: 'high',
        latencyMs: '-200-500ms saved'
    },
    'response_caching': {
        icon: '💾',
        pros: ['Instant repeated queries', 'Reduces compute costs', 'Lower API usage'],
        cons: ['Cache invalidation complexity', 'Stale data risk'],
        latencyImpact: 'high',
        latencyMs: '-1000ms+ saved'
    },

    // Integration Layer
    'home_assistant': {
        icon: '🏡',
        pros: ['Smart home control', 'Device state awareness', 'Automation integration'],
        cons: ['HA dependency', 'Network latency to HA'],
        latencyImpact: 'low',
        latencyMs: '+50-150ms'
    },
    'automation_system_mode': {
        icon: '🤖',
        pros: ['Pattern matching: Fast, predictable responses', 'Dynamic agent: Handles any automation request with LLM'],
        cons: ['Pattern matching: Limited to predefined patterns', 'Dynamic agent: Higher latency, uses LLM'],
        latencyImpact: 'medium',
        latencyMs: 'Varies by mode',
        hasModeSwitcher: true,
        modeDescriptions: {
            'pattern_matching': 'Fast keyword detection with predefined sequences. Best for simple, common commands.',
            'dynamic_agent': 'LLM-powered agent with tools. Handles complex automations, schedules, and triggers.'
        }
    },
    'clarification_prompts': {
        icon: '❓',
        pros: ['Handles ambiguous queries', 'Improves accuracy', 'Better user guidance'],
        cons: ['Extra interaction step', 'May feel verbose'],
        latencyImpact: 'none',
        latencyMs: 'N/A'
    },
    'voice_feedback': {
        icon: '🔊',
        pros: ['Audio confirmation', 'Hands-free interaction', 'Accessibility'],
        cons: ['TTS latency', 'Audio quality varies'],
        latencyImpact: 'medium',
        latencyMs: '+200-500ms'
    }
};

// Category configuration - maps all database categories to display config
const categoryConfig = {
    processing: {
        name: 'Processing Layer',
        icon: '⚙️',
        description: 'Core query processing and intent handling',
        color: 'blue'
    },
    rag: {
        name: 'RAG Services',
        icon: '🔍',
        description: 'Retrieval-Augmented Generation for real-time data',
        color: 'purple'
    },
    optimization: {
        name: 'Performance Optimizations',
        icon: '🚀',
        description: 'Latency reduction and caching strategies',
        color: 'green'
    },
    performance: {
        name: 'HA Voice Optimizations',
        icon: '⚡',
        description: 'Home Assistant voice latency optimizations',
        color: 'green'
    },
    integration: {
        name: 'Integrations',
        icon: '🔗',
        description: 'External service and smart home connections',
        color: 'orange'
    },
    integrations: {
        name: 'Integrations',
        icon: '🔗',
        description: 'External service and smart home connections',
        color: 'orange'
    },
    llm: {
        name: 'LLM Settings',
        icon: '🧠',
        description: 'Large Language Model configuration',
        color: 'indigo'
    },
    routing: {
        name: 'Routing',
        icon: '🎯',
        description: 'Query routing and classification settings',
        color: 'cyan'
    },
    voice: {
        name: 'Voice',
        icon: '🎤',
        description: 'Voice and audio processing features',
        color: 'pink'
    },
    experimental: {
        name: 'Experimental',
        icon: '🧪',
        description: 'Experimental features in development',
        color: 'yellow'
    }
};

/**
 * Get latency impact styling
 */
function getLatencyStyle(impact) {
    const styles = {
        high: { bg: 'bg-red-500/20', text: 'text-red-400', border: 'border-red-500/30', label: 'High Impact' },
        medium: { bg: 'bg-yellow-500/20', text: 'text-yellow-400', border: 'border-yellow-500/30', label: 'Medium Impact' },
        low: { bg: 'bg-blue-500/20', text: 'text-blue-400', border: 'border-blue-500/30', label: 'Low Impact' },
        none: { bg: 'bg-gray-500/20', text: 'text-gray-400', border: 'border-gray-500/30', label: 'No Impact' }
    };
    return styles[impact] || styles.none;
}

/**
 * Get optimization benefit styling (for features that reduce latency)
 */
function getOptimizationStyle(feature) {
    const meta = featureMetadata[feature.name] || featureMetadata[feature.flag_name];
    if (!meta) return null;

    // Check if this is an optimization feature (reduces latency)
    if (meta.latencyMs && meta.latencyMs.includes('saved')) {
        return { bg: 'bg-green-500/20', text: 'text-green-400', border: 'border-green-500/30', label: 'Saves Time' };
    }
    return null;
}

/**
 * Load MLX applicability data
 */
async function loadMLXApplicability() {
    try {
        const response = await fetch('/api/llm-backends/public/mlx-applicability', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            mlxApplicabilityData = await response.json();
        }
    } catch (error) {
        console.error('Failed to load MLX applicability:', error);
        mlxApplicabilityData = null;
    }
}

/**
 * Load all features from backend
 */
async function loadFeatures() {
    try {
        // Use ApiClient if available, fallback to fetch
        if (typeof ApiClient !== 'undefined') {
            featuresData = await ApiClient.get('/api/features');
        } else {
            const response = await fetch('/api/features', {
                headers: getAuthHeaders()
            });
            if (!response.ok) {
                throw new Error(`Failed to load features: ${response.statusText}`);
            }
            featuresData = await response.json();
        }
        console.log('Features loaded:', featuresData.length);

        // Load feature impact analysis, what-if scenarios, and MLX applicability in parallel
        await Promise.all([
            loadFeatureImpact(),
            loadWhatIfScenarios(),
            loadMLXApplicability()
        ]);

        renderFeatures();
    } catch (error) {
        console.error('Failed to load features:', error);
        safeShowToast('Failed to load features', 'error');
        showFeaturesError(error.message);
    }
}

/**
 * Load feature impact analysis
 */
async function loadFeatureImpact() {
    try {
        const response = await fetch('/api/features/impact/analysis', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            impactData = await response.json();
        }
    } catch (error) {
        console.error('Failed to load feature impact:', error);
    }
}

/**
 * Load what-if scenarios
 */
async function loadWhatIfScenarios() {
    try {
        const response = await fetch('/api/features/what-if/scenarios', {
            headers: getAuthHeaders()
        });

        if (response.ok) {
            whatIfScenarios = await response.json();
        }
    } catch (error) {
        console.error('Failed to load what-if scenarios:', error);
    }
}

/**
 * Toggle feature on/off
 */
async function toggleFeature(featureId) {
    try {
        const response = await fetch(`/api/features/${featureId}/toggle`, {
            method: 'PUT',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to toggle feature');
        }

        const updatedFeature = await response.json();

        // Update local data
        const index = featuresData.findIndex(f => f.id === featureId);
        if (index !== -1) {
            featuresData[index] = updatedFeature;
        }

        // Refresh displays
        await loadWhatIfScenarios();
        renderFeatures();

        const status = updatedFeature.enabled ? 'enabled' : 'disabled';
        safeShowToast(`${updatedFeature.display_name} ${status}`, 'success');

    } catch (error) {
        console.error('Failed to toggle feature:', error);
        safeShowToast(error.message, 'error');

        // Reload features to restore correct state
        await loadFeatures();
    }
}

/**
 * Calculate category statistics
 */
function getCategoryStats(features) {
    const enabled = features.filter(f => f.enabled).length;
    const total = features.length;
    const optimizations = features.filter(f => {
        const meta = featureMetadata[f.name] || featureMetadata[f.flag_name];
        return meta && meta.latencyMs && meta.latencyMs.includes('saved');
    });
    const activeOptimizations = optimizations.filter(f => f.enabled).length;

    return { enabled, total, activeOptimizations, totalOptimizations: optimizations.length };
}

/**
 * Render features grouped by category
 */
function renderFeatures() {
    const container = document.getElementById('features-container');
    if (!container) return;

    if (featuresData.length === 0) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-4xl mb-4">🎛️</div>
                <p class="text-gray-400">No features configured</p>
            </div>
        `;
        return;
    }

    // Group features by category dynamically
    // Exclude RAG services - they belong in a separate RAG management page
    const excludedCategories = ['rag', "'rag'"];
    const categories = {};

    featuresData.forEach(feature => {
        // Normalize category - strip any embedded quotes and lowercase
        let cat = (feature.category || 'processing').replace(/'/g, '').toLowerCase();

        if (excludedCategories.includes(cat) || cat === 'rag') {
            return; // Skip RAG services
        }
        if (!categories[cat]) {
            categories[cat] = [];
        }
        categories[cat].push(feature);
    });

    // Render summary cards
    let html = renderSummaryCards(categories);

    // Render each category
    html += '<div class="space-y-8 mt-8">';

    Object.keys(categories).forEach(categoryKey => {
        const features = categories[categoryKey];
        if (features.length === 0) return;

        // Sort by priority
        features.sort((a, b) => (a.priority || 0) - (b.priority || 0));

        // Get config with fallback for unknown categories
        const config = categoryConfig[categoryKey] || {
            name: categoryKey.charAt(0).toUpperCase() + categoryKey.slice(1),
            icon: '📦',
            description: `${categoryKey} features`,
            color: 'gray'
        };
        const stats = getCategoryStats(features);

        html += `
            <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
                <!-- Category Header -->
                <div class="px-6 py-4 border-b border-dark-border bg-gradient-to-r from-${config.color}-500/10 to-transparent">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-3">
                            <span class="text-2xl">${config.icon}</span>
                            <div>
                                <h3 class="text-lg font-semibold text-white">${config.name}</h3>
                                <p class="text-sm text-gray-400">${config.description}</p>
                            </div>
                        </div>
                        <div class="flex items-center gap-4">
                            <div class="text-right">
                                <div class="text-sm text-gray-400">Active</div>
                                <div class="text-lg font-semibold text-${config.color}-400">${stats.enabled}/${stats.total}</div>
                            </div>
                            ${stats.totalOptimizations > 0 ? `
                            <div class="text-right border-l border-dark-border pl-4">
                                <div class="text-sm text-gray-400">Optimizations</div>
                                <div class="text-lg font-semibold text-green-400">${stats.activeOptimizations}/${stats.totalOptimizations}</div>
                            </div>
                            ` : ''}
                        </div>
                    </div>
                </div>

                <!-- Features Grid -->
                <div class="p-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    ${features.map(feature => renderFeatureCard(feature)).join('')}
                </div>
            </div>
        `;
    });

    html += '</div>';

    // Add what-if analysis section
    html += renderWhatIfAnalysis();

    container.innerHTML = html;
}

/**
 * Render summary stat cards
 */
function renderSummaryCards(categories) {
    const allFeatures = Object.values(categories).flat();
    const totalEnabled = allFeatures.filter(f => f.enabled).length;
    const totalFeatures = allFeatures.length;

    // Count optimizations (performance category features)
    const perfFeatures = categories.performance || [];
    const activeOptimizations = perfFeatures.filter(f => f.enabled).length;
    const totalOptimizations = perfFeatures.length;

    // Calculate estimated latency impact from HA optimization features
    let estimatedSavings = 0;
    perfFeatures.forEach(f => {
        if (f.enabled) {
            const meta = featureMetadata[f.name] || featureMetadata[f.flag_name];
            if (meta && meta.latencyMs && meta.latencyMs.includes('saved')) {
                // Extract approximate savings (e.g., "-100-200ms saved" -> 150)
                const match = meta.latencyMs.match(/-(\d+)-(\d+)ms/);
                if (match) {
                    estimatedSavings += (parseInt(match[1]) + parseInt(match[2])) / 2;
                }
            }
        }
    });

    // Count processing features
    const processingFeatures = categories.processing || [];
    const activeProcessing = processingFeatures.filter(f => f.enabled).length;

    // MLX status card data
    let mlxStatusHtml = '';
    if (mlxApplicabilityData) {
        const { mlx_feature_enabled, summary, mlx_latency_impact_ms } = mlxApplicabilityData;
        const mlxModelsAvailable = summary.mlx_models_available;
        const componentsUsingMlx = summary.components_using_mlx;

        // Determine MLX status
        let mlxStatusColor, mlxStatusText, mlxIcon;
        if (!mlx_feature_enabled) {
            mlxStatusColor = 'gray';
            mlxStatusText = 'Disabled';
            mlxIcon = '⚪';
        } else if (componentsUsingMlx === 0) {
            mlxStatusColor = 'yellow';
            mlxStatusText = 'Unused';
            mlxIcon = '⚠️';
        } else {
            mlxStatusColor = 'green';
            mlxStatusText = 'Active';
            mlxIcon = '✅';
        }

        mlxStatusHtml = `
            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-purple-500/20">
                        <span class="text-xl">${mlxIcon}</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">MLX Backend</div>
                        <div class="text-2xl font-bold text-${mlxStatusColor}-400">${mlxStatusText}</div>
                        ${mlx_feature_enabled ? `<div class="text-xs text-gray-500">${componentsUsingMlx}/${summary.total_components} components</div>` : ''}
                    </div>
                </div>
            </div>
        `;
    }

    return `
        <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-blue-500/20">
                        <span class="text-xl">🎛️</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">Features Active</div>
                        <div class="text-2xl font-bold text-white">${totalEnabled}<span class="text-gray-500 text-lg">/${totalFeatures}</span></div>
                    </div>
                </div>
            </div>

            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-green-500/20">
                        <span class="text-xl">⚡</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">HA Optimizations</div>
                        <div class="text-2xl font-bold text-green-400">${activeOptimizations}<span class="text-gray-500 text-lg">/${totalOptimizations}</span></div>
                    </div>
                </div>
            </div>

            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-purple-500/20">
                        <span class="text-xl">⏱️</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">Est. Latency Saved</div>
                        <div class="text-2xl font-bold text-purple-400">${estimatedSavings.toFixed(0)}<span class="text-gray-500 text-lg">ms</span></div>
                    </div>
                </div>
            </div>

            <div class="bg-dark-card border border-dark-border rounded-xl p-4">
                <div class="flex items-center gap-3">
                    <div class="p-2 rounded-lg bg-blue-500/20">
                        <span class="text-xl">⚙️</span>
                    </div>
                    <div>
                        <div class="text-sm text-gray-400">Processing</div>
                        <div class="text-2xl font-bold text-blue-400">${activeProcessing}<span class="text-gray-500 text-lg">/${processingFeatures.length}</span></div>
                    </div>
                </div>
            </div>

            ${mlxStatusHtml}
        </div>
    `;
}

/**
 * Render individual feature card
 */
function renderFeatureCard(feature) {
    const isEnabled = feature.enabled;
    const isRequired = feature.required;
    const featureKey = feature.name || feature.flag_name;
    const meta = featureMetadata[featureKey] || {};

    // Check if this is a mode switcher feature
    if (meta.hasModeSwitcher && feature.config && feature.config.available_modes) {
        return renderModeSwitcherCard(feature, meta);
    }

    const icon = meta.icon || '⚙️';
    const pros = meta.pros || [];
    const cons = meta.cons || [];
    const latencyImpact = meta.latencyImpact || 'none';
    const latencyMs = meta.latencyMs || 'N/A';

    // Get styling based on whether this is an optimization or adds latency
    const isOptimization = latencyMs.includes('saved');
    const latencyStyle = isOptimization ? getOptimizationStyle(feature) : getLatencyStyle(latencyImpact);

    const statusClass = isEnabled ? 'border-green-500/30' : 'border-dark-border';
    const enabledBadge = isEnabled
        ? '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/20 text-green-400">Active</span>'
        : '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-500/20 text-gray-400">Inactive</span>';

    return `
        <div class="bg-dark-bg border ${statusClass} rounded-lg p-4 transition-all hover:border-blue-500/30">
            <!-- Header -->
            <div class="flex items-start justify-between mb-3">
                <div class="flex items-center gap-2">
                    <span class="text-xl">${icon}</span>
                    <div>
                        <div class="font-medium text-white flex items-center gap-2">
                            ${feature.display_name}
                            ${isRequired ? '<span class="text-yellow-500" title="Required">🔒</span>' : ''}
                        </div>
                        ${enabledBadge}
                    </div>
                </div>
                <label class="relative inline-flex items-center cursor-pointer">
                    <input type="checkbox"
                           class="sr-only peer"
                           ${isEnabled ? 'checked' : ''}
                           ${isRequired ? 'disabled' : ''}
                           onchange="toggleFeature(${feature.id})">
                    <div class="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer
                                peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full
                                peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px]
                                after:start-[2px] after:bg-white after:border-gray-300 after:border
                                after:rounded-full after:h-5 after:w-5 after:transition-all
                                peer-checked:bg-green-600 ${isRequired ? 'opacity-50 cursor-not-allowed' : ''}"></div>
                </label>
            </div>

            <!-- Description -->
            <p class="text-sm text-gray-400 mb-3">${feature.description || 'No description available'}</p>

            <!-- Latency Badge -->
            <div class="mb-3">
                <span class="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium ${latencyStyle?.bg || ''} ${latencyStyle?.text || ''} border ${latencyStyle?.border || ''}">
                    ${isOptimization ? '⚡' : '⏱️'} ${latencyMs}
                </span>
            </div>

            <!-- Pros & Cons (collapsible) -->
            ${(pros.length > 0 || cons.length > 0) ? `
            <details class="group">
                <summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-400 flex items-center gap-1">
                    <span class="group-open:rotate-90 transition-transform">▶</span>
                    View pros & cons
                </summary>
                <div class="mt-2 space-y-2">
                    ${pros.length > 0 ? `
                    <div>
                        <div class="text-xs text-green-400 font-medium mb-1">✓ Pros</div>
                        <ul class="text-xs text-gray-400 space-y-0.5 pl-3">
                            ${pros.map(p => `<li>• ${p}</li>`).join('')}
                        </ul>
                    </div>
                    ` : ''}
                    ${cons.length > 0 ? `
                    <div>
                        <div class="text-xs text-red-400 font-medium mb-1">✗ Cons</div>
                        <ul class="text-xs text-gray-400 space-y-0.5 pl-3">
                            ${cons.map(c => `<li>• ${c}</li>`).join('')}
                        </ul>
                    </div>
                    ` : ''}
                </div>
            </details>
            ` : ''}
        </div>
    `;
}

/**
 * Render mode switcher card for features with multiple modes
 */
function renderModeSwitcherCard(feature, meta) {
    const currentMode = feature.config?.mode || feature.config?.available_modes?.[0] || 'unknown';
    const availableModes = feature.config?.available_modes || [];
    const modeDescriptions = meta.modeDescriptions || {};
    const icon = meta.icon || '⚙️';

    const modeColors = {
        'pattern_matching': { bg: 'bg-blue-500/20', text: 'text-blue-400', border: 'border-blue-500/30' },
        'dynamic_agent': { bg: 'bg-purple-500/20', text: 'text-purple-400', border: 'border-purple-500/30' }
    };

    const currentColor = modeColors[currentMode] || { bg: 'bg-gray-500/20', text: 'text-gray-400', border: 'border-gray-500/30' };

    return `
        <div class="bg-dark-bg border ${currentColor.border} rounded-lg p-4 transition-all hover:border-blue-500/30 md:col-span-2">
            <!-- Header -->
            <div class="flex items-start justify-between mb-4">
                <div class="flex items-center gap-3">
                    <span class="text-2xl">${icon}</span>
                    <div>
                        <div class="font-medium text-white text-lg">${feature.display_name}</div>
                        <p class="text-sm text-gray-400 mt-1">${feature.description || 'Select automation system mode'}</p>
                    </div>
                </div>
                <span class="px-3 py-1 rounded-full text-sm font-medium ${currentColor.bg} ${currentColor.text}">
                    ${formatModeName(currentMode)}
                </span>
            </div>

            <!-- Mode Selector -->
            <div class="grid grid-cols-2 gap-3 mb-4">
                ${availableModes.map(mode => {
                    const isActive = mode === currentMode;
                    const color = modeColors[mode] || { bg: 'bg-gray-500/20', text: 'text-gray-400', border: 'border-gray-500/30' };
                    const desc = modeDescriptions[mode] || '';

                    return `
                        <button onclick="setAutomationMode('${mode}', ${feature.id})"
                                class="p-4 rounded-lg border-2 transition-all text-left
                                       ${isActive
                                           ? `${color.border} ${color.bg}`
                                           : 'border-dark-border hover:border-gray-500'}">
                            <div class="flex items-center gap-2 mb-2">
                                <div class="w-3 h-3 rounded-full ${isActive ? color.bg.replace('/20', '') : 'bg-gray-600'}"></div>
                                <span class="font-medium ${isActive ? color.text : 'text-gray-300'}">${formatModeName(mode)}</span>
                            </div>
                            <p class="text-xs text-gray-400">${desc}</p>
                        </button>
                    `;
                }).join('')}
            </div>

            <!-- Current Mode Info -->
            <div class="p-3 rounded-lg bg-dark-card border border-dark-border">
                <div class="flex items-center gap-2 text-sm">
                    <span class="${currentColor.text}">●</span>
                    <span class="text-gray-300">Currently using:</span>
                    <span class="font-medium ${currentColor.text}">${formatModeName(currentMode)}</span>
                </div>
            </div>
        </div>
    `;
}

/**
 * Format mode name for display
 */
function formatModeName(mode) {
    return mode.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}

/**
 * Set automation system mode
 */
async function setAutomationMode(mode, featureId) {
    try {
        const response = await fetch(`/api/features/${featureId}/config`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                mode: mode,
                available_modes: ['pattern_matching', 'dynamic_agent']
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update mode');
        }

        safeShowToast(`Automation mode set to ${formatModeName(mode)}`, 'success');

        // Reload features to reflect the change
        await loadFeatures();

    } catch (error) {
        console.error('Failed to set automation mode:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Render what-if analysis section
 */
function renderWhatIfAnalysis() {
    if (whatIfScenarios.length === 0) {
        return '';
    }

    const currentScenario = whatIfScenarios.find(s => s.scenario_name === 'Current Configuration');

    return `
        <div class="mt-8 bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <div class="px-6 py-4 border-b border-dark-border bg-gradient-to-r from-indigo-500/10 to-transparent">
                <div class="flex items-center gap-3">
                    <span class="text-2xl">📊</span>
                    <div>
                        <h3 class="text-lg font-semibold text-white">What-If Analysis</h3>
                        <p class="text-sm text-gray-400">Compare latency impact across different feature configurations</p>
                    </div>
                </div>
            </div>

            <div class="p-4 overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="text-left text-gray-400 border-b border-dark-border">
                            <th class="pb-3 pr-4 font-medium">Scenario</th>
                            <th class="pb-3 pr-4 font-medium">Description</th>
                            <th class="pb-3 pr-4 font-medium text-right">Total Latency</th>
                            <th class="pb-3 pr-4 font-medium text-right">Change</th>
                            <th class="pb-3 font-medium text-right">Impact</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${whatIfScenarios.map(scenario => {
                            const isCurrent = scenario.scenario_name === 'Current Configuration';
                            const isPositive = scenario.change_percent < 0;
                            const isNegative = scenario.change_percent > 0;

                            const changeClass = isPositive ? 'text-green-400' : isNegative ? 'text-red-400' : 'text-gray-400';
                            const changeSign = scenario.change_percent > 0 ? '+' : '';
                            const changeBg = isPositive ? 'bg-green-500/20' : isNegative ? 'bg-red-500/20' : 'bg-gray-500/20';

                            return `
                                <tr class="${isCurrent ? 'bg-blue-500/5' : ''} border-b border-dark-border/50">
                                    <td class="py-3 pr-4">
                                        <span class="font-medium text-white">${scenario.scenario_name}</span>
                                        ${isCurrent ? '<span class="ml-2 text-xs text-blue-400">⬅ Current</span>' : ''}
                                    </td>
                                    <td class="py-3 pr-4 text-gray-400">${scenario.description}</td>
                                    <td class="py-3 pr-4 text-right font-mono font-medium text-white">${scenario.total_latency_ms.toFixed(1)}ms</td>
                                    <td class="py-3 pr-4 text-right font-mono ${changeClass}">
                                        ${changeSign}${scenario.change_from_current_ms.toFixed(1)}ms
                                    </td>
                                    <td class="py-3 text-right">
                                        <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${changeBg} ${changeClass}">
                                            ${changeSign}${scenario.change_percent.toFixed(1)}%
                                        </span>
                                    </td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

/**
 * Show error state
 */
function showFeaturesError(message) {
    const container = document.getElementById('features-container');
    if (container) {
        container.innerHTML = `
            <div class="bg-red-500/10 border border-red-500/30 rounded-xl p-6 text-center">
                <div class="text-3xl mb-3">⚠️</div>
                <h3 class="text-lg font-semibold text-red-400 mb-2">Failed to Load Features</h3>
                <p class="text-gray-400">${message}</p>
                <button onclick="loadFeatures()" class="mt-4 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium transition-colors">
                    Try Again
                </button>
            </div>
        `;
    }
}

/**
 * Initialize features page
 */
function initFeaturesPage() {
    console.log('Initializing features page');

    // Load features data
    loadFeatures();

    // Set up auto-refresh using RefreshManager (prevents interval accumulation)
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('features-refresh', loadFeatures, FEATURES_REFRESH_INTERVAL);
    } else {
        // Fallback to setInterval (legacy behavior)
        if (typeof AppState !== 'undefined') {
            const intervalId = setInterval(() => loadFeatures(), FEATURES_REFRESH_INTERVAL);
            AppState.registerInterval('features-refresh', intervalId);
        } else {
            setInterval(() => loadFeatures(), FEATURES_REFRESH_INTERVAL);
        }
    }
}

/**
 * Cleanup features page (called when navigating away)
 */
function destroyFeaturesPage() {
    console.log('Cleaning up features page');
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('features-refresh');
    }
}

// Export for external use
if (typeof window !== 'undefined') {
    window.toggleFeature = toggleFeature;
    window.initFeaturesPage = initFeaturesPage;
    window.destroyFeaturesPage = destroyFeaturesPage;
}
