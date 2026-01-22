/**
 * Authentication Management
 * Handles login flow, session restoration, and auth guards
 *
 * Part of Phase 0: Session & State Architecture
 */

(function() {
    // Dependency check
    if (!window.AppState || !window.ApiClient) {
        console.error('[Auth] Dependencies not loaded. Ensure state.js and api-client.js load first.');
        return;
    }

    const Auth = {
        /**
         * Initialize authentication (called once on page load)
         * Returns a Promise that resolves when auth state is determined
         */
        async initialize() {
            AppState.setAuthState('checking');

            try {
                // 1. Check for token in URL (from OAuth callback)
                const urlParams = new URLSearchParams(window.location.search);
                const urlToken = urlParams.get('token');

                if (urlToken) {
                    // Remove token from URL for security
                    window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
                    return await this._validateAndSetToken(urlToken);
                }

                // 2. Check localStorage
                const storedToken = localStorage.getItem('auth_token');
                if (storedToken) {
                    return await this._validateAndSetToken(storedToken);
                }

                // 3. Try session cookie
                const sessionToken = await this._getSessionToken();
                if (sessionToken) {
                    return await this._validateAndSetToken(sessionToken);
                }

                // No valid auth found
                AppState.setAuthState('unauthenticated');
                return false;

            } catch (error) {
                console.error('[Auth] Initialization failed:', error);
                AppState.setAuthState('unauthenticated');
                return false;
            }
        },

        /**
         * Validate token and load user info
         */
        async _validateAndSetToken(token) {
            try {
                const response = await fetch(`${ApiClient.baseUrl}/api/auth/me`, {
                    headers: { 'Authorization': `Bearer ${token}` }
                });

                if (response.ok) {
                    const user = await response.json();
                    AppState.setAuthState('authenticated', token, user);
                    console.log('[Auth] Authenticated as:', user.username || user.email);
                    return true;
                } else {
                    // Token invalid
                    localStorage.removeItem('auth_token');
                    AppState.setAuthState('unauthenticated');
                    return false;
                }
            } catch (error) {
                console.error('[Auth] Token validation failed:', error);
                AppState.setAuthState('unauthenticated');
                return false;
            }
        },

        /**
         * Try to get token from session cookie
         */
        async _getSessionToken() {
            try {
                const response = await fetch(`${ApiClient.baseUrl}/api/auth/session-token`, {
                    credentials: 'include'
                });
                if (response.ok) {
                    const data = await response.json();
                    return data.token || null;
                }
            } catch (e) {
                // Session not available
            }
            return null;
        },

        /**
         * Redirect to login
         */
        login() {
            window.location.href = `${ApiClient.baseUrl}/api/auth/login`;
        },

        /**
         * Logout and redirect
         */
        logout() {
            AppState.setAuthState('unauthenticated');
            window.location.href = `${ApiClient.baseUrl}/api/auth/logout`;
        },

        /**
         * Auth guard - wraps a function to only run if authenticated
         * Shows login prompt if not authenticated
         */
        guard(fn) {
            return async function(...args) {
                if (!AppState.isAuthenticated()) {
                    Auth._showLoginRequired();
                    return null;
                }
                return fn.apply(this, args);
            };
        },

        /**
         * Show login required message in current container
         */
        _showLoginRequired() {
            const container = document.getElementById('view-container') ||
                              document.querySelector('.tab-content:not(.hidden)');

            if (container) {
                container.innerHTML = `
                    <div class="flex flex-col items-center justify-center py-16">
                        <div class="text-6xl mb-4">🔐</div>
                        <h2 class="text-xl font-semibold text-white mb-2">Authentication Required</h2>
                        <p class="text-gray-400 mb-6">Please login to access this page.</p>
                        <button onclick="Auth.login()"
                            class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                            Login with Authentik
                        </button>
                    </div>
                `;
            }
        },

        /**
         * Check if current user has required role
         */
        hasRole(role) {
            const user = AppState.getUser();
            if (!user) return false;
            return user.role === role || user.role === 'admin';
        },

        /**
         * Check if current user is admin
         */
        isAdmin() {
            return this.hasRole('admin');
        }
    };

    // Expose on window
    window.Auth = Auth;
})();
