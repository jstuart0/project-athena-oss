/**
 * Smart Refresh Manager
 * Handles auto-refresh without destroying scroll position or form state
 *
 * Part of Phase 0: Session & State Architecture
 */

(function() {
    // Dependency check
    if (!window.AppState) {
        console.error('[RefreshManager] AppState not loaded. Check script order.');
        return;
    }

    const RefreshManager = {
        /**
         * Create a managed refresh interval for a tab
         * Auto-cleans up when tab changes
         *
         * @param {string} name - Unique name for this interval
         * @param {Function} callback - Function to call on each interval
         * @param {number} intervalMs - Interval in milliseconds
         * @returns {number} - The interval ID
         */
        createInterval(name, callback, intervalMs) {
            // Wrap callback to preserve scroll position
            const wrappedCallback = async () => {
                // Don't refresh if page is hidden
                if (document.hidden) return;

                const contentArea = document.querySelector('.content-area') ||
                                   document.querySelector('.main-content');
                const scrollTop = contentArea?.scrollTop || 0;

                try {
                    await callback();
                } catch (error) {
                    console.error(`[RefreshManager] Interval "${name}" error:`, error);
                }

                // Restore scroll position after DOM update
                requestAnimationFrame(() => {
                    if (contentArea) {
                        contentArea.scrollTop = scrollTop;
                    }
                });
            };

            const intervalId = setInterval(wrappedCallback, intervalMs);
            AppState.registerInterval(name, intervalId);

            return intervalId;
        },

        /**
         * Clear a specific interval by name
         */
        clearInterval(name) {
            AppState.clearInterval(name);
        },

        /**
         * Clear all intervals
         */
        clearAll() {
            AppState.clearAllIntervals();
        },

        /**
         * Smart update - only updates changed elements
         * Preserves scroll position and focus
         *
         * @param {string} containerId - ID of container to update
         * @param {string} newHtml - New HTML content
         * @param {Object} options - Options { fullReplace: boolean }
         */
        smartUpdate(containerId, newHtml, options = {}) {
            const container = document.getElementById(containerId);
            if (!container) return;

            // Save state before update
            const scrollTop = container.scrollTop;
            const activeElementId = document.activeElement?.id;
            const activeElementValue = document.activeElement?.value;
            const selectionStart = document.activeElement?.selectionStart;
            const selectionEnd = document.activeElement?.selectionEnd;

            // Perform update
            container.innerHTML = newHtml;

            // Restore state after update
            requestAnimationFrame(() => {
                // Restore scroll
                container.scrollTop = scrollTop;

                // Restore focus and selection
                if (activeElementId) {
                    const element = document.getElementById(activeElementId);
                    if (element) {
                        element.focus();
                        // Restore input value if it was being edited
                        if (activeElementValue !== undefined && element.tagName === 'INPUT') {
                            element.value = activeElementValue;
                        }
                        // Restore cursor position
                        if (selectionStart !== undefined && element.setSelectionRange) {
                            try {
                                element.setSelectionRange(selectionStart, selectionEnd);
                            } catch (e) {
                                // Some inputs don't support selection
                            }
                        }
                    }
                }
            });
        },

        /**
         * Update a specific table without affecting container scroll
         *
         * @param {string} tableId - ID of table to update
         * @param {string} rowsHtml - New HTML for tbody content
         */
        updateTable(tableId, rowsHtml) {
            const tbody = document.querySelector(`#${tableId} tbody`);
            if (!tbody) return;

            const scrollContainer = tbody.closest('.overflow-auto') ||
                                    tbody.closest('.main-content');
            const scrollTop = scrollContainer?.scrollTop || 0;

            tbody.innerHTML = rowsHtml;

            requestAnimationFrame(() => {
                if (scrollContainer) {
                    scrollContainer.scrollTop = scrollTop;
                }
            });
        },

        /**
         * Update a specific element by ID without full container refresh
         *
         * @param {string} elementId - ID of element to update
         * @param {string} newHtml - New HTML content
         */
        updateElement(elementId, newHtml) {
            const element = document.getElementById(elementId);
            if (!element) return;

            const parent = element.parentElement;
            const scrollTop = parent?.scrollTop || 0;

            element.innerHTML = newHtml;

            requestAnimationFrame(() => {
                if (parent) {
                    parent.scrollTop = scrollTop;
                }
            });
        },

        /**
         * Debounce a function
         *
         * @param {Function} fn - Function to debounce
         * @param {number} delay - Delay in milliseconds
         * @returns {Function} - Debounced function
         */
        debounce(fn, delay) {
            let timeoutId;
            return function(...args) {
                clearTimeout(timeoutId);
                timeoutId = setTimeout(() => fn.apply(this, args), delay);
            };
        },

        /**
         * Throttle a function
         *
         * @param {Function} fn - Function to throttle
         * @param {number} limit - Minimum time between calls in milliseconds
         * @returns {Function} - Throttled function
         */
        throttle(fn, limit) {
            let inThrottle;
            return function(...args) {
                if (!inThrottle) {
                    fn.apply(this, args);
                    inThrottle = true;
                    setTimeout(() => inThrottle = false, limit);
                }
            };
        }
    };

    // Expose on window
    window.RefreshManager = RefreshManager;
})();
