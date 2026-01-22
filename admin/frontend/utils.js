/**
 * Athena Admin - Shared Utilities Module
 *
 * This module provides shared utility functions used across the admin frontend.
 * All functions are exported for ES6 module usage and also exposed on window.AthenaUtils
 * for backward compatibility with files not yet converted to ES6 modules.
 *
 * @module utils
 */

// ============================================================================
// HTML & String Utilities
// ============================================================================

/**
 * Escape HTML to prevent XSS attacks
 * @param {string} text - Text to escape
 * @returns {string} Escaped HTML string
 */
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

/**
 * Format bytes to human-readable string
 * @param {number} bytes - Number of bytes
 * @param {number} [decimals=1] - Number of decimal places
 * @returns {string} Formatted string (e.g., "1.5 GB")
 */
function formatBytes(bytes, decimals = 1) {
    if (bytes === 0) return '0 B';
    if (!bytes || isNaN(bytes)) return '0 B';

    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.floor(Math.log(Math.abs(bytes)) / Math.log(k));
    const index = Math.min(i, sizes.length - 1);

    return parseFloat((bytes / Math.pow(k, index)).toFixed(decimals)) + ' ' + sizes[index];
}

/**
 * Format a date string or timestamp to locale string
 * @param {string|number|Date} date - Date to format
 * @param {Object} [options] - Intl.DateTimeFormat options
 * @returns {string} Formatted date string
 */
function formatDate(date, options = {}) {
    if (!date) return '';

    const defaultOptions = {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    };

    try {
        const d = date instanceof Date ? date : new Date(date);
        return d.toLocaleString(undefined, { ...defaultOptions, ...options });
    } catch (e) {
        return String(date);
    }
}

/**
 * Format duration in seconds to human-readable string
 * @param {number} seconds - Duration in seconds
 * @returns {string} Formatted duration (e.g., "2h 30m" or "45s")
 */
function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '0s';

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);

    if (hours > 0) {
        return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
    }
    if (minutes > 0) {
        return secs > 0 ? `${minutes}m ${secs}s` : `${minutes}m`;
    }
    return `${secs}s`;
}

// ============================================================================
// Notification System
// ============================================================================

/**
 * Show a toast notification
 * @param {string} message - Message to display
 * @param {string} [type='info'] - Type: 'success' | 'error' | 'warning' | 'info'
 * @param {number} [duration=3000] - Duration in milliseconds
 */
function showNotification(message, type = 'info', duration = 3000) {
    // Color mapping for notification types
    const colors = {
        success: 'bg-green-600',
        error: 'bg-red-600',
        warning: 'bg-yellow-600',
        info: 'bg-blue-600'
    };

    // Create toast element
    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 ${colors[type] || colors.info} text-white px-6 py-3 rounded-lg shadow-lg z-50 transition-all duration-300 transform translate-y-0 opacity-100`;
    toast.textContent = message;
    toast.style.maxWidth = '400px';

    // Add to DOM
    document.body.appendChild(toast);

    // Animate out and remove
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(20px)';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

/**
 * Show success notification (convenience wrapper)
 * @param {string} message - Message to display
 */
function showSuccess(message) {
    showNotification(message, 'success');
}

/**
 * Show error notification (convenience wrapper)
 * @param {string} message - Message to display
 */
function showError(message) {
    showNotification(message, 'error');
}

/**
 * Show warning notification (convenience wrapper)
 * @param {string} message - Message to display
 */
function showWarning(message) {
    showNotification(message, 'warning');
}

/**
 * Show info notification (convenience wrapper)
 * @param {string} message - Message to display
 */
function showInfo(message) {
    showNotification(message, 'info');
}

/**
 * Safe wrapper for showNotification - checks if function exists
 * Used during migration period for backward compatibility
 * @param {string} message - Message to display
 * @param {string} [type='info'] - Notification type
 */
function safeShowToast(message, type = 'info') {
    // Try window.showToast first (legacy), then showNotification
    if (typeof window.showToast === 'function') {
        window.showToast(message, type);
    } else {
        showNotification(message, type);
    }
}

// ============================================================================
// Authentication & API
// ============================================================================

/**
 * Get the current authentication token
 * @returns {string|null} Auth token or null if not authenticated
 */
function getAuthToken() {
    // Try AppState first (authoritative source), then fallbacks
    if (window.AppState && typeof window.AppState.getToken === 'function') {
        const token = window.AppState.getToken();
        if (token) return token;
    }
    // Fallback to window.authToken or localStorage
    return window.authToken || localStorage.getItem('auth_token');
}

/**
 * Build authentication headers for API requests
 * @param {Object} [additionalHeaders={}] - Additional headers to include
 * @returns {Object} Headers object with auth
 */
function getAuthHeaders(additionalHeaders = {}) {
    const token = getAuthToken();
    return {
        'Content-Type': 'application/json',
        ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
        ...additionalHeaders
    };
}

/**
 * Make an authenticated API request
 * @param {string} url - API endpoint URL
 * @param {Object} [options={}] - Fetch options
 * @returns {Promise<Response>} Fetch response
 */
async function apiRequest(url, options = {}) {
    const defaultOptions = {
        headers: getAuthHeaders(options.headers || {}),
        credentials: 'same-origin'
    };

    const mergedOptions = {
        ...defaultOptions,
        ...options,
        headers: {
            ...defaultOptions.headers,
            ...(options.headers || {})
        }
    };

    const response = await fetch(url, mergedOptions);

    // Handle 401 Unauthorized - redirect to login
    if (response.status === 401) {
        // Clear stored token
        localStorage.removeItem('auth_token');
        window.authToken = null;

        // Trigger logout if function exists
        if (typeof window.logout === 'function') {
            window.logout();
        }
    }

    return response;
}

/**
 * Make a GET request with authentication
 * @param {string} url - API endpoint URL
 * @returns {Promise<any>} Parsed JSON response
 */
async function apiGet(url) {
    const response = await apiRequest(url);
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json();
}

/**
 * Make a POST request with authentication
 * @param {string} url - API endpoint URL
 * @param {any} data - Data to send (will be JSON stringified)
 * @returns {Promise<any>} Parsed JSON response
 */
async function apiPost(url, data) {
    const response = await apiRequest(url, {
        method: 'POST',
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json();
}

/**
 * Make a PUT request with authentication
 * @param {string} url - API endpoint URL
 * @param {any} data - Data to send (will be JSON stringified)
 * @returns {Promise<any>} Parsed JSON response
 */
async function apiPut(url, data) {
    const response = await apiRequest(url, {
        method: 'PUT',
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json();
}

/**
 * Make a DELETE request with authentication
 * @param {string} url - API endpoint URL
 * @returns {Promise<any>} Parsed JSON response
 */
async function apiDelete(url) {
    const response = await apiRequest(url, {
        method: 'DELETE'
    });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json();
}

// ============================================================================
// DOM Utilities
// ============================================================================

/**
 * Create an HTML element with attributes and content
 * @param {string} tag - HTML tag name
 * @param {Object} [attrs={}] - Attributes to set
 * @param {string|HTMLElement|Array} [content] - Inner content
 * @returns {HTMLElement} Created element
 */
function createElement(tag, attrs = {}, content = null) {
    const el = document.createElement(tag);

    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'className') {
            el.className = value;
        } else if (key === 'style' && typeof value === 'object') {
            Object.assign(el.style, value);
        } else if (key.startsWith('on') && typeof value === 'function') {
            el.addEventListener(key.slice(2).toLowerCase(), value);
        } else {
            el.setAttribute(key, value);
        }
    }

    if (content !== null) {
        if (typeof content === 'string') {
            el.textContent = content;
        } else if (content instanceof HTMLElement) {
            el.appendChild(content);
        } else if (Array.isArray(content)) {
            content.forEach(child => {
                if (typeof child === 'string') {
                    el.appendChild(document.createTextNode(child));
                } else if (child instanceof HTMLElement) {
                    el.appendChild(child);
                }
            });
        }
    }

    return el;
}

/**
 * Query selector shorthand
 * @param {string} selector - CSS selector
 * @param {HTMLElement} [parent=document] - Parent element
 * @returns {HTMLElement|null} Found element or null
 */
function $(selector, parent = document) {
    return parent.querySelector(selector);
}

/**
 * Query selector all shorthand
 * @param {string} selector - CSS selector
 * @param {HTMLElement} [parent=document] - Parent element
 * @returns {HTMLElement[]} Array of found elements
 */
function $$(selector, parent = document) {
    return Array.from(parent.querySelectorAll(selector));
}

// ============================================================================
// Debounce & Throttle
// ============================================================================

/**
 * Debounce a function
 * @param {Function} func - Function to debounce
 * @param {number} wait - Wait time in milliseconds
 * @returns {Function} Debounced function
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func.apply(this, args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Throttle a function
 * @param {Function} func - Function to throttle
 * @param {number} limit - Time limit in milliseconds
 * @returns {Function} Throttled function
 */
function throttle(func, limit) {
    let inThrottle;
    return function executedFunction(...args) {
        if (!inThrottle) {
            func.apply(this, args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

// ============================================================================
// Local Storage Utilities
// ============================================================================

/**
 * Get item from localStorage with JSON parsing
 * @param {string} key - Storage key
 * @param {any} [defaultValue=null] - Default value if key not found
 * @returns {any} Parsed value or default
 */
function getStorageItem(key, defaultValue = null) {
    try {
        const item = localStorage.getItem(key);
        return item ? JSON.parse(item) : defaultValue;
    } catch (e) {
        console.warn(`Failed to parse localStorage item "${key}":`, e);
        return defaultValue;
    }
}

/**
 * Set item in localStorage with JSON stringification
 * @param {string} key - Storage key
 * @param {any} value - Value to store
 */
function setStorageItem(key, value) {
    try {
        localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
        console.warn(`Failed to set localStorage item "${key}":`, e);
    }
}

/**
 * Remove item from localStorage
 * @param {string} key - Storage key
 */
function removeStorageItem(key) {
    localStorage.removeItem(key);
}

// ============================================================================
// URL & Navigation Utilities
// ============================================================================

/**
 * Get URL query parameters as object
 * @returns {Object} Query parameters
 */
function getQueryParams() {
    const params = {};
    const searchParams = new URLSearchParams(window.location.search);
    for (const [key, value] of searchParams) {
        params[key] = value;
    }
    return params;
}

/**
 * Update URL query parameter without page reload
 * @param {string} key - Parameter key
 * @param {string} value - Parameter value
 */
function setQueryParam(key, value) {
    const url = new URL(window.location.href);
    if (value === null || value === undefined) {
        url.searchParams.delete(key);
    } else {
        url.searchParams.set(key, value);
    }
    window.history.replaceState({}, '', url);
}

// ============================================================================
// Validation Utilities
// ============================================================================

/**
 * Check if a value is empty (null, undefined, empty string, empty array, empty object)
 * @param {any} value - Value to check
 * @returns {boolean} True if empty
 */
function isEmpty(value) {
    if (value === null || value === undefined) return true;
    if (typeof value === 'string') return value.trim() === '';
    if (Array.isArray(value)) return value.length === 0;
    if (typeof value === 'object') return Object.keys(value).length === 0;
    return false;
}

/**
 * Validate email format
 * @param {string} email - Email to validate
 * @returns {boolean} True if valid email format
 */
function isValidEmail(email) {
    const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return re.test(String(email).toLowerCase());
}

/**
 * Validate URL format
 * @param {string} url - URL to validate
 * @returns {boolean} True if valid URL format
 */
function isValidUrl(url) {
    try {
        new URL(url);
        return true;
    } catch {
        return false;
    }
}

// ============================================================================
// Expose on window for backward compatibility
// ============================================================================

// Create global AthenaUtils namespace
window.AthenaUtils = {
    // HTML & String
    escapeHtml,
    formatBytes,
    formatDate,
    formatDuration,

    // Notifications
    showNotification,
    showSuccess,
    showError,
    showWarning,
    showInfo,
    safeShowToast,

    // Auth & API
    getAuthToken,
    getAuthHeaders,
    apiRequest,
    apiGet,
    apiPost,
    apiPut,
    apiDelete,

    // DOM
    createElement,
    $,
    $$,

    // Timing
    debounce,
    throttle,

    // Storage
    getStorageItem,
    setStorageItem,
    removeStorageItem,

    // URL
    getQueryParams,
    setQueryParam,

    // Validation
    isEmpty,
    isValidEmail,
    isValidUrl
};

// Also expose common functions directly on window for maximum backward compatibility
// These can be removed once all files are migrated to use AthenaUtils or ES6 imports
window.escapeHtml = escapeHtml;
window.formatBytes = formatBytes;
window.showNotification = showNotification;
window.showSuccess = showSuccess;
window.showError = showError;
window.safeShowToast = safeShowToast;
window.getAuthToken = getAuthToken;
window.getAuthHeaders = getAuthHeaders;

console.log('Athena Utils module loaded');
