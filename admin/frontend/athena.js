/**
 * Athena Admin UI Component Library
 *
 * Single namespace for all reusable UI components.
 * Avoids global namespace pollution and provides clear organization.
 *
 * Sub-namespaces:
 * - Athena.components: UI components (drawer, toolbar, toast, activity)
 * - Athena.charts: Chart.js utilities and sparklines
 * - Athena.pages: Page controllers (mission control, voice pipelines, etc.)
 * - Athena.utils: Utility functions
 */
(function(global) {
    'use strict';

    // Initialize main namespace
    const Athena = global.Athena || {};

    // Sub-namespaces for organization
    Athena.components = Athena.components || {};
    Athena.charts = Athena.charts || {};
    Athena.pages = Athena.pages || {};
    Athena.utils = Athena.utils || {};

    // Version info
    Athena.version = '1.0.0';

    /**
     * Initialize all Athena components.
     * Called once on DOMContentLoaded.
     */
    Athena.init = function() {
        console.log('[Athena] Initializing UI components v' + Athena.version);

        // Initialize core components if they exist
        if (Athena.components.Toast && typeof Athena.components.Toast.init === 'function') {
            Athena.components.Toast.init();
        }
        if (Athena.components.Activity && typeof Athena.components.Activity.init === 'function') {
            Athena.components.Activity.init();
        }
        if (Athena.components.Keyboard && typeof Athena.components.Keyboard.init === 'function') {
            Athena.components.Keyboard.init();
        }

        console.log('[Athena] UI components initialized');
    };

    /**
     * Destroy all page-specific state.
     * Called on page navigation to clean up.
     * @param {string} pageId - The ID of the page being left
     */
    Athena.destroyPage = function(pageId) {
        const page = Athena.pages[pageId];
        if (page && typeof page.destroy === 'function') {
            page.destroy();
            console.log('[Athena] Page destroyed:', pageId);
        }
    };

    /**
     * Initialize a specific page.
     * @param {string} pageId - The ID of the page to initialize
     */
    Athena.initPage = function(pageId) {
        const page = Athena.pages[pageId];
        if (page && typeof page.init === 'function') {
            page.init();
            console.log('[Athena] Page initialized:', pageId);
        }
    };

    // =========================================================================
    // UTILITY FUNCTIONS
    // =========================================================================

    /**
     * Format a timestamp for display.
     * @param {string|Date} timestamp - ISO timestamp or Date object
     * @param {boolean} includeTime - Whether to include time
     * @returns {string} Formatted date string
     */
    Athena.utils.formatDate = function(timestamp, includeTime = true) {
        if (!timestamp) return 'Never';

        const date = timestamp instanceof Date ? timestamp : new Date(timestamp);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        // Relative time for recent events
        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;

        // Absolute date for older events
        const options = includeTime
            ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
            : { month: 'short', day: 'numeric' };
        return date.toLocaleDateString('en-US', options);
    };

    /**
     * Format a number with appropriate suffix.
     * @param {number} num - Number to format
     * @returns {string} Formatted string (e.g., "1.2k", "3.5M")
     */
    Athena.utils.formatNumber = function(num) {
        if (num === null || num === undefined) return '0';
        if (num < 1000) return num.toString();
        if (num < 1000000) return (num / 1000).toFixed(1) + 'k';
        return (num / 1000000).toFixed(1) + 'M';
    };

    /**
     * Debounce a function call.
     * @param {Function} func - Function to debounce
     * @param {number} wait - Wait time in ms
     * @returns {Function} Debounced function
     */
    Athena.utils.debounce = function(func, wait = 250) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    };

    /**
     * Throttle a function call.
     * @param {Function} func - Function to throttle
     * @param {number} limit - Limit in ms
     * @returns {Function} Throttled function
     */
    Athena.utils.throttle = function(func, limit = 250) {
        let inThrottle;
        return function executedFunction(...args) {
            if (!inThrottle) {
                func(...args);
                inThrottle = true;
                setTimeout(() => inThrottle = false, limit);
            }
        };
    };

    /**
     * Deep clone an object.
     * @param {Object} obj - Object to clone
     * @returns {Object} Cloned object
     */
    Athena.utils.deepClone = function(obj) {
        return JSON.parse(JSON.stringify(obj));
    };

    /**
     * Generate a unique ID.
     * @param {string} prefix - Optional prefix
     * @returns {string} Unique ID
     */
    Athena.utils.uniqueId = function(prefix = 'athena') {
        return prefix + '_' + Math.random().toString(36).substr(2, 9);
    };

    /**
     * Escape HTML to prevent XSS.
     * @param {string} str - String to escape
     * @returns {string} Escaped string
     */
    Athena.utils.escapeHtml = function(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    };

    /**
     * Get severity color class.
     * @param {string} severity - Severity level (critical, warning, info, success)
     * @returns {string} Tailwind color class
     */
    Athena.utils.getSeverityColor = function(severity) {
        const colors = {
            critical: 'red',
            error: 'red',
            warning: 'yellow',
            info: 'blue',
            success: 'green',
            healthy: 'green',
            unhealthy: 'red',
            unknown: 'gray'
        };
        return colors[severity?.toLowerCase()] || 'gray';
    };

    /**
     * Get status indicator HTML.
     * @param {string} status - Status string
     * @returns {string} HTML for status indicator
     */
    Athena.utils.getStatusIndicator = function(status) {
        const color = Athena.utils.getSeverityColor(status);
        return `<span class="inline-block w-2 h-2 rounded-full bg-${color}-500"></span>`;
    };

    // =========================================================================
    // API SHORTCUTS
    // =========================================================================

    /**
     * Shortcut for API requests (wraps ApiClient if available).
     * @param {string} endpoint - API endpoint
     * @param {Object} options - Fetch options
     * @returns {Promise} API response
     */
    Athena.api = async function(endpoint, options = {}) {
        if (global.ApiClient && typeof global.ApiClient.request === 'function') {
            return global.ApiClient.request(endpoint, options);
        }
        // Fallback to fetch
        const response = await fetch(endpoint, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options
        });
        if (!response.ok) throw new Error(`API Error: ${response.status}`);
        return response.json();
    };

    // =========================================================================
    // EVENT BUS (Simple pub/sub for component communication)
    // =========================================================================

    const eventListeners = {};

    /**
     * Subscribe to an event.
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     * @returns {Function} Unsubscribe function
     */
    Athena.on = function(event, callback) {
        if (!eventListeners[event]) {
            eventListeners[event] = [];
        }
        eventListeners[event].push(callback);

        // Return unsubscribe function
        return () => {
            const idx = eventListeners[event].indexOf(callback);
            if (idx > -1) eventListeners[event].splice(idx, 1);
        };
    };

    /**
     * Emit an event.
     * @param {string} event - Event name
     * @param {*} data - Event data
     */
    Athena.emit = function(event, data) {
        if (eventListeners[event]) {
            eventListeners[event].forEach(callback => {
                try {
                    callback(data);
                } catch (e) {
                    console.error('[Athena] Event handler error:', e);
                }
            });
        }
    };

    /**
     * Subscribe to an event once.
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     */
    Athena.once = function(event, callback) {
        const unsubscribe = Athena.on(event, (data) => {
            unsubscribe();
            callback(data);
        });
    };

    // Export to global
    global.Athena = Athena;

    // Wait for authentication before initializing
    // Listen for auth state changes via AppState
    function waitForAuth() {
        // Check if already authenticated
        if (global.AppState && global.AppState.auth && global.AppState.auth.status === 'authenticated') {
            console.log('[Athena] Auth already complete, initializing');
            Athena.init();
            return;
        }

        // Subscribe to auth state changes
        if (global.AppState && global.AppState.subscribe) {
            const unsubscribe = global.AppState.subscribe((type, data) => {
                if (type === 'auth' && data.status === 'authenticated') {
                    console.log('[Athena] Auth complete, initializing');
                    unsubscribe();
                    // Small delay to let other auth handlers run first
                    setTimeout(Athena.init, 50);
                }
            });
        } else {
            // Fallback: wait a bit longer for auth to complete
            console.log('[Athena] AppState not available, using fallback timing');
            setTimeout(Athena.init, 500);
        }
    }

    // Start auth wait on DOMContentLoaded
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', waitForAuth);
    } else {
        // DOM already loaded, wait for auth
        waitForAuth();
    }

})(window);
