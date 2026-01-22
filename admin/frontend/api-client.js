/**
 * Centralized API Client with Auth & Error Handling
 * All API calls should go through this client
 *
 * Part of Phase 0: Session & State Architecture
 */

(function() {
    // Dependency check
    if (!window.AppState) {
        console.error('[ApiClient] AppState not loaded. Check script order.');
        return;
    }

    /**
     * Custom API Error class
     */
    class ApiError extends Error {
        constructor(message, status, data = null) {
            super(message);
            this.name = 'ApiError';
            this.status = status;
            this.data = data;
        }
    }

    const ApiClient = {
        baseUrl: window.location.origin,

        /**
         * Make an authenticated request
         * Automatically handles: auth headers, 401 redirect, error normalization
         *
         * @param {string} endpoint - API endpoint
         * @param {Object} options - Fetch options plus:
         *   - skipAuth: boolean - Skip auth check/header for public endpoints
         */
        async request(endpoint, options = {}) {
            const { skipAuth = false, ...fetchOptions } = options;

            // Check auth state first (unless skipAuth is true)
            if (!skipAuth && !AppState.isAuthenticated()) {
                throw new ApiError('Not authenticated', 401);
            }

            const url = endpoint.startsWith('http') ? endpoint : `${this.baseUrl}${endpoint}`;

            // Create abort controller for request cancellation
            const abortController = new AbortController();
            const requestId = `${endpoint}-${Date.now()}`;
            AppState.registerRequest(requestId, abortController);

            // Build headers - include auth header only if authenticated and not skipped
            const headers = {
                'Content-Type': 'application/json',
                ...fetchOptions.headers
            };

            if (!skipAuth && AppState.isAuthenticated()) {
                headers['Authorization'] = `Bearer ${AppState.getToken()}`;
            }

            try {
                const response = await fetch(url, {
                    ...fetchOptions,
                    signal: abortController.signal,
                    headers
                });

                // Handle 401 - session expired (only matters for auth-required endpoints)
                if (response.status === 401 && !skipAuth) {
                    AppState.setAuthState('unauthenticated', null, null);
                    throw new ApiError('Session expired. Please login again.', 401);
                }

                // Handle other errors
                if (!response.ok) {
                    const errorData = await response.json().catch(() => ({}));
                    throw new ApiError(
                        errorData.detail || errorData.message || `Request failed: ${response.status}`,
                        response.status,
                        errorData
                    );
                }

                // Handle empty responses
                const contentType = response.headers.get('content-type');
                if (contentType && contentType.includes('application/json')) {
                    return await response.json();
                }
                return null;

            } catch (error) {
                if (error.name === 'AbortError') {
                    throw new ApiError('Request cancelled', 0);
                }
                if (error instanceof ApiError) {
                    throw error;
                }
                // Network error
                throw new ApiError(error.message || 'Network error', 0);
            } finally {
                AppState.unregisterRequest(requestId);
            }
        },

        /**
         * GET request
         * @param {string} endpoint - API endpoint
         * @param {Object} options - Optional: { skipAuth: true } for public endpoints
         */
        async get(endpoint, options = {}) {
            return this.request(endpoint, { method: 'GET', ...options });
        },

        /**
         * POST request
         */
        async post(endpoint, data, options = {}) {
            return this.request(endpoint, {
                method: 'POST',
                body: JSON.stringify(data),
                ...options
            });
        },

        /**
         * PUT request
         */
        async put(endpoint, data, options = {}) {
            return this.request(endpoint, {
                method: 'PUT',
                body: JSON.stringify(data),
                ...options
            });
        },

        /**
         * PATCH request
         */
        async patch(endpoint, data, options = {}) {
            return this.request(endpoint, {
                method: 'PATCH',
                body: JSON.stringify(data),
                ...options
            });
        },

        /**
         * DELETE request
         */
        async delete(endpoint, options = {}) {
            return this.request(endpoint, { method: 'DELETE', ...options });
        },

        /**
         * Make unauthenticated request (for public endpoints)
         * @deprecated Use get/post with { skipAuth: true } instead
         */
        async publicRequest(endpoint, options = {}) {
            const url = endpoint.startsWith('http') ? endpoint : `${this.baseUrl}${endpoint}`;
            const response = await fetch(url, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                }
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new ApiError(errorData.detail || 'Request failed', response.status, errorData);
            }

            const contentType = response.headers.get('content-type');
            if (contentType && contentType.includes('application/json')) {
                return response.json();
            }
            return null;
        }
    };

    // Expose on window
    window.ApiClient = ApiClient;
    window.ApiError = ApiError;
})();
