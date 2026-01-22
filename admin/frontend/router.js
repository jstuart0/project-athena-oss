/**
 * Simple Hash-Based Router for Athena Admin
 * Enables deep linking and view management
 *
 * Part of Phase 1: Design Foundation
 */

(function() {
    // Dependency check
    if (!window.AppState) {
        console.error('[Router] AppState not loaded. Check script order.');
        return;
    }

    const Router = {
        routes: {},
        currentRoute: null,
        _initialized: false,

        /**
         * Register a route with its view loader and optional cleanup
         *
         * @param {string} path - Route path (e.g., 'dashboard', 'features')
         * @param {Function} loader - Function to call when route is activated
         * @param {Function} [cleanup] - Optional function to call when leaving route
         */
        register(path, loader, cleanup = null) {
            this.routes[path] = { loader, cleanup };
        },

        /**
         * Navigate to a route
         * @param {string} path - Route path
         */
        navigate(path) {
            if (!path) path = 'dashboard';
            window.location.hash = `#${path}`;
        },

        /**
         * Get current route from hash
         */
        getCurrentRoute() {
            const hash = window.location.hash;
            // Remove # and any leading /
            return hash ? hash.replace(/^#\/?/, '') : 'dashboard';
        },

        /**
         * Handle route change
         */
        async handleRouteChange() {
            const route = this.getCurrentRoute();

            // Skip if same route
            if (route === this.currentRoute) return;

            // Call cleanup on previous route
            const prevRouteConfig = this.routes[this.currentRoute];
            if (prevRouteConfig?.cleanup) {
                try {
                    prevRouteConfig.cleanup();
                } catch (e) {
                    console.error(`[Router] Cleanup error for ${this.currentRoute}:`, e);
                }
            }

            // Clear intervals from previous route
            AppState.clearAllIntervals();

            this.currentRoute = route;
            AppState.setCurrentTab(route);

            // Update nav active state
            this._updateNavState(route);

            // Load view content
            const routeConfig = this.routes[route];

            if (routeConfig?.loader) {
                try {
                    await routeConfig.loader();

                    // Initialize icons if Lucide is available
                    if (typeof lucide !== 'undefined') {
                        lucide.createIcons();
                    }
                } catch (error) {
                    console.error(`[Router] Failed to load route: ${route}`, error);
                    this._showError(route, error);
                }
            } else {
                // Route not found - try legacy showTab
                if (typeof legacyShowTab === 'function') {
                    legacyShowTab(route);
                } else {
                    this._show404(route);
                }
            }
        },

        /**
         * Update navigation active state
         */
        _updateNavState(route) {
            // Remove active from all nav items
            document.querySelectorAll('.nav-item, .sidebar-item').forEach(item => {
                item.classList.remove('nav-item-active', 'sidebar-item-active', 'bg-gray-700', 'text-white');
                item.classList.add('text-gray-400');
            });

            // Add active to current route
            document.querySelectorAll('.nav-item[data-route], .sidebar-item').forEach(item => {
                const itemRoute = item.dataset.route ||
                                  item.getAttribute('onclick')?.match(/showTab\(['"]([^'"]+)['"]\)/)?.[1];
                if (itemRoute === route) {
                    item.classList.add('nav-item-active', 'sidebar-item-active', 'bg-gray-700', 'text-white');
                    item.classList.remove('text-gray-400');
                }
            });
        },

        /**
         * Show error state
         */
        _showError(route, error) {
            const container = document.getElementById('view-container') ||
                              document.querySelector('.main-content');
            if (container) {
                container.innerHTML = `
                    <div class="flex flex-col items-center justify-center py-16 text-center">
                        <div class="w-16 h-16 rounded-full bg-red-500/10 flex items-center justify-center mb-4">
                            <i data-lucide="alert-circle" class="w-8 h-8 text-red-400"></i>
                        </div>
                        <h2 class="text-xl font-semibold text-white mb-2">Failed to load page</h2>
                        <p class="text-gray-400 mb-6 max-w-md">${error.message}</p>
                        <button onclick="Router.navigate('dashboard')"
                            class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                            Go to Dashboard
                        </button>
                    </div>
                `;
                if (typeof lucide !== 'undefined') {
                    lucide.createIcons();
                }
            }
        },

        /**
         * Show 404 state
         */
        _show404(route) {
            const container = document.getElementById('view-container') ||
                              document.querySelector('.main-content');
            if (container) {
                container.innerHTML = `
                    <div class="flex flex-col items-center justify-center py-16 text-center">
                        <div class="w-16 h-16 rounded-full bg-gray-500/10 flex items-center justify-center mb-4">
                            <i data-lucide="file-question" class="w-8 h-8 text-gray-400"></i>
                        </div>
                        <h2 class="text-xl font-semibold text-white mb-2">Page not found</h2>
                        <p class="text-gray-400 mb-6">The page "${route}" doesn't exist.</p>
                        <button onclick="Router.navigate('dashboard')"
                            class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                            Go to Dashboard
                        </button>
                    </div>
                `;
                if (typeof lucide !== 'undefined') {
                    lucide.createIcons();
                }
            }
        },

        /**
         * Initialize router
         */
        init() {
            if (this._initialized) return;

            // Listen for hash changes
            window.addEventListener('hashchange', () => this.handleRouteChange());

            // Handle initial route
            this.handleRouteChange();

            // Set up nav item click handlers
            document.querySelectorAll('.nav-item[data-route]').forEach(item => {
                item.addEventListener('click', (e) => {
                    e.preventDefault();
                    const route = item.dataset.route;
                    if (route) this.navigate(route);
                });
            });

            this._initialized = true;
        },

        /**
         * Check if router is initialized
         */
        isInitialized() {
            return this._initialized;
        }
    };

    // Navigation expansion/collapse for nav rail
    window.expandNav = function() {
        const nav = document.getElementById('nav-rail');
        if (nav) nav.classList.add('expanded');
    };

    window.collapseNav = function() {
        const nav = document.getElementById('nav-rail');
        if (nav && !nav.classList.contains('pinned')) {
            nav.classList.remove('expanded');
        }
    };

    // Pin nav open (toggle)
    let navPinned = false;
    window.toggleNavPin = function() {
        navPinned = !navPinned;
        const nav = document.getElementById('nav-rail');
        if (nav) {
            if (navPinned) {
                nav.classList.add('expanded', 'pinned');
            } else {
                nav.classList.remove('pinned');
            }
        }
    };

    // Expose on window
    window.Router = Router;
})();
