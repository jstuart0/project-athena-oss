/**
 * Centralized Application State Manager
 * Single source of truth for auth, loading, and navigation state
 *
 * Part of Phase 0: Session & State Architecture
 */

const AppState = {
    // Authentication state
    auth: {
        status: 'unknown',  // 'unknown' | 'checking' | 'authenticated' | 'unauthenticated'
        token: null,
        user: null
    },

    // Navigation state
    navigation: {
        currentTab: null,
        previousTab: null
    },

    // Active intervals/subscriptions for cleanup
    _activeIntervals: new Map(),
    _pendingRequests: new Map(),

    // Event listeners
    _listeners: [],

    // Page cleanup callbacks
    _destroyCallbacks: new Map(),

    /**
     * Update auth state and notify listeners
     */
    setAuthState(status, token = null, user = null) {
        this.auth.status = status;
        this.auth.token = token;
        this.auth.user = user;

        if (token) {
            localStorage.setItem('auth_token', token);
        } else {
            localStorage.removeItem('auth_token');
        }

        this._notify('auth', this.auth);
    },

    /**
     * Check if user is authenticated
     */
    isAuthenticated() {
        return this.auth.status === 'authenticated' && this.auth.token !== null;
    },

    /**
     * Get auth token (single source of truth)
     */
    getToken() {
        return this.auth.token;
    },

    /**
     * Get current user
     */
    getUser() {
        return this.auth.user;
    },

    /**
     * Register a managed interval (auto-cleanup on tab switch)
     */
    registerInterval(name, intervalId) {
        // Clear existing interval with same name
        if (this._activeIntervals.has(name)) {
            clearInterval(this._activeIntervals.get(name));
        }
        this._activeIntervals.set(name, intervalId);
    },

    /**
     * Clear a specific interval
     */
    clearInterval(name) {
        if (this._activeIntervals.has(name)) {
            clearInterval(this._activeIntervals.get(name));
            this._activeIntervals.delete(name);
        }
    },

    /**
     * Clear ALL intervals (called on tab switch)
     */
    clearAllIntervals() {
        for (const [name, intervalId] of this._activeIntervals) {
            clearInterval(intervalId);
        }
        this._activeIntervals.clear();
    },

    /**
     * Register a page destroy callback
     */
    registerDestroyCallback(tabName, callback) {
        this._destroyCallbacks.set(tabName, callback);
    },

    /**
     * Set current tab and cleanup previous
     */
    setCurrentTab(tabName) {
        const previous = this.navigation.currentTab;

        // Cleanup previous tab
        if (previous && previous !== tabName) {
            // Call page-specific destroy callback
            const destroyFn = this._destroyCallbacks.get(previous);
            if (destroyFn) {
                try {
                    destroyFn();
                } catch (error) {
                    console.error(`[AppState] Error destroying ${previous}:`, error);
                }
            }

            this.clearAllIntervals();
            this._abortPendingRequests();
        }

        this.navigation.previousTab = previous;
        this.navigation.currentTab = tabName;
        this._notify('navigation', this.navigation);
    },

    /**
     * Get current tab
     */
    getCurrentTab() {
        return this.navigation.currentTab;
    },

    /**
     * Register a pending request (for cancellation)
     */
    registerRequest(name, abortController) {
        this._pendingRequests.set(name, abortController);
    },

    /**
     * Unregister a pending request
     */
    unregisterRequest(name) {
        this._pendingRequests.delete(name);
    },

    /**
     * Abort all pending requests
     */
    _abortPendingRequests() {
        for (const [name, controller] of this._pendingRequests) {
            controller.abort();
        }
        this._pendingRequests.clear();
    },

    /**
     * Subscribe to state changes
     */
    subscribe(callback) {
        this._listeners.push(callback);
        return () => {
            this._listeners = this._listeners.filter(l => l !== callback);
        };
    },

    /**
     * Notify all listeners of state change
     */
    _notify(type, data) {
        this._listeners.forEach(callback => {
            try {
                callback(type, data);
            } catch (error) {
                console.error('[AppState] Listener error:', error);
            }
        });
    },

    /**
     * Reset state (for testing or logout)
     */
    reset() {
        this.clearAllIntervals();
        this._abortPendingRequests();
        this.auth = {
            status: 'unknown',
            token: null,
            user: null
        };
        this.navigation = {
            currentTab: null,
            previousTab: null
        };
    }
};

// Expose on window for other modules
window.AppState = AppState;
