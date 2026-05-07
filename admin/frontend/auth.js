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

    let authMethodsCache = null;

    const Auth = {
        async getMethods(force = false) {
            if (!force && authMethodsCache) {
                return authMethodsCache;
            }

            try {
                const response = await fetch(`${ApiClient.baseUrl}/api/auth/methods`);
                if (response.ok) {
                    authMethodsCache = await response.json();
                    return authMethodsCache;
                }
            } catch (error) {
                console.warn('[Auth] Failed to load auth methods:', error);
            }

            authMethodsCache = { oidc_enabled: true, local_enabled: true, demo_mode: false };
            return authMethodsCache;
        },

        /**
         * Initialize authentication (called once on page load)
         * Returns a Promise that resolves when auth state is determined
         */
        async initialize() {
            AppState.setAuthState('checking');

            try {
                // xander:4 / codex-H1: detect OIDC callback landing via ?logged_in=1.
                // On a fresh OIDC return the backend emits this hint instead of ?token=<jwt>.
                // Clear stale localStorage so a previous user's token cannot contaminate
                // the new session on a shared device (cross-user contamination fix).
                const urlParams = new URLSearchParams(window.location.search);
                if (urlParams.get('logged_in') === '1') {
                    localStorage.removeItem('auth_token');
                    // Strip the query param from the URL for cleanliness — it has served its purpose.
                    window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
                }
                // (URL ?token= reader removed in xander:4 — JWT no longer emitted in URL.)

                // 1. Check localStorage (cleared above if we just landed from OIDC callback)
                const storedToken = localStorage.getItem('auth_token');
                if (storedToken) {
                    return await this._validateAndSetToken(storedToken);
                }

                // 2. Try session cookie (primary path after OIDC callback)
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
        async login() {
            const methods = await this.getMethods();
            if (methods.local_enabled) {
                this.showLoginModal(methods);
                return;
            }
            window.location.href = `${ApiClient.baseUrl}/api/auth/login`;
        },

        /**
         * Logout and redirect
         */
        logout() {
            AppState.setAuthState('unauthenticated');
            window.location.href = `${ApiClient.baseUrl}/api/auth/logout`;
        },

        showLoginModal(methods = { oidc_enabled: true, local_enabled: true }) {
            const oidcButton = methods.oidc_enabled ? `
                <button type="button" onclick="Auth.loginWithOIDC()"
                    class="w-full px-4 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                    Continue with OIDC
                </button>
            ` : '';

            const localForm = methods.local_enabled ? `
                <form onsubmit="Auth.submitLocalLogin(event)" class="space-y-3">
                    <div>
                        <label for="local-login-username" class="block text-sm font-medium text-gray-400 mb-2">Username</label>
                        <input id="local-login-username" type="text" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div>
                        <label for="local-login-password" class="block text-sm font-medium text-gray-400 mb-2">Password</label>
                        <input id="local-login-password" type="password" required
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    </div>
                    <div id="local-login-error" class="hidden text-sm text-red-400"></div>
                    <button type="submit"
                        class="w-full px-4 py-3 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                        Sign in with local account
                    </button>
                </form>
            ` : '';

            const divider = methods.oidc_enabled && methods.local_enabled
                ? '<div class="text-xs text-gray-500 text-center uppercase tracking-wide">or</div>'
                : '';

            const modal = `
                <div id="login-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='login-modal') closeModal('login-modal')">
                    <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-md w-full mx-4">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-xl font-semibold text-white">Admin Login</h3>
                            <button type="button" onclick="closeModal('login-modal')" class="text-gray-400 hover:text-white text-2xl">&times;</button>
                        </div>
                        <div class="space-y-4">
                            ${oidcButton}
                            ${divider}
                            ${localForm}
                        </div>
                    </div>
                </div>
            `;

            document.getElementById('modals-container').innerHTML = modal;
        },

        loginWithOIDC() {
            window.location.href = `${ApiClient.baseUrl}/api/auth/login`;
        },

        async submitLocalLogin(event) {
            event.preventDefault();
            const username = document.getElementById('local-login-username').value.trim();
            const password = document.getElementById('local-login-password').value;
            const errorEl = document.getElementById('local-login-error');

            try {
                const response = await fetch(`${ApiClient.baseUrl}/api/auth/local-login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({ username, password })
                });

                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.detail || data.error || 'Login failed');
                }

                closeModal('login-modal');
                AppState.setAuthState('authenticated', data.token, data.user);
            } catch (error) {
                errorEl.textContent = error.message;
                errorEl.classList.remove('hidden');
            }
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
                        <i data-lucide="lock" class="w-12 h-12 mb-4 text-gray-400"></i>
                        <h2 class="text-xl font-semibold text-white mb-2">Authentication Required</h2>
                        <p class="text-gray-400 mb-6">Please login to access this page.</p>
                        <button onclick="Auth.login()"
                            class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                            Login
                        </button>
                    </div>
                `;
                if (typeof lucide !== 'undefined') lucide.createIcons();
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
