/**
 * keyboard.js - Keyboard Navigation Support
 *
 * Provides comprehensive keyboard navigation for the Admin UI:
 * - Arrow key navigation between sidebar items
 * - Enter to activate sidebar items
 * - Escape to close modals/drawers
 * - Tab focus management
 * - Keyboard shortcuts for common actions
 */

(function() {
    'use strict';

    // Ensure Athena namespace exists
    window.Athena = window.Athena || {};
    window.Athena.keyboard = window.Athena.keyboard || {};

    // State
    let isInitialized = false;
    let currentFocusIndex = -1;
    let sidebarItems = [];

    // Keyboard shortcut registry
    const shortcuts = new Map();

    /**
     * Initialize keyboard navigation
     */
    function init() {
        if (isInitialized) return;

        // Collect sidebar items
        updateSidebarItems();

        // Add global keyboard listener
        document.addEventListener('keydown', handleGlobalKeydown);

        // Add focus tracking
        document.addEventListener('focusin', handleFocusIn);

        // Register default shortcuts
        registerDefaultShortcuts();

        isInitialized = true;
        console.log('[Keyboard] Navigation initialized');
    }

    /**
     * Destroy keyboard navigation
     */
    function destroy() {
        if (!isInitialized) return;

        document.removeEventListener('keydown', handleGlobalKeydown);
        document.removeEventListener('focusin', handleFocusIn);

        shortcuts.clear();
        sidebarItems = [];
        currentFocusIndex = -1;
        isInitialized = false;

        console.log('[Keyboard] Navigation destroyed');
    }

    /**
     * Update the list of sidebar items
     */
    function updateSidebarItems() {
        sidebarItems = Array.from(document.querySelectorAll('.sidebar-item:not([disabled])'));
    }

    /**
     * Register default keyboard shortcuts
     */
    function registerDefaultShortcuts() {
        // Navigation shortcuts (Ctrl/Cmd + number)
        register('1', () => navigateToTab('mission-control'), { ctrl: true, description: 'Go to Mission Control' });
        register('2', () => navigateToTab('voice-pipelines'), { ctrl: true, description: 'Go to Voice Pipelines' });
        register('3', () => navigateToTab('memory-context'), { ctrl: true, description: 'Go to Memory & Context' });
        register('4', () => navigateToTab('integrations'), { ctrl: true, description: 'Go to Integrations' });
        register('5', () => navigateToTab('devices'), { ctrl: true, description: 'Go to Devices' });
        register('6', () => navigateToTab('features'), { ctrl: true, description: 'Go to Features' });
        register('7', () => navigateToTab('service-control'), { ctrl: true, description: 'Go to Service Control' });

        // Action shortcuts
        register('/', () => focusSearch(), { description: 'Focus search' });
        register('?', () => showShortcutsHelp(), { shift: true, description: 'Show keyboard shortcuts' });
        register('Escape', () => closeOverlays(), { description: 'Close modals and drawers' });

        // Sidebar navigation
        register('ArrowDown', () => navigateSidebar(1), { description: 'Next sidebar item' });
        register('ArrowUp', () => navigateSidebar(-1), { description: 'Previous sidebar item' });
        register('Enter', () => activateFocusedSidebarItem(), { description: 'Activate sidebar item' });

        // Quick actions
        register('r', () => refreshCurrentPage(), { ctrl: true, description: 'Refresh current page' });
    }

    /**
     * Register a keyboard shortcut
     * @param {string} key - The key to listen for
     * @param {Function} callback - The function to call
     * @param {Object} options - Options (ctrl, shift, alt, meta, description)
     */
    function register(key, callback, options = {}) {
        const shortcutKey = buildShortcutKey(key, options);
        shortcuts.set(shortcutKey, {
            callback,
            description: options.description || '',
            key,
            ...options
        });
    }

    /**
     * Unregister a keyboard shortcut
     * @param {string} key - The key
     * @param {Object} options - Modifier options
     */
    function unregister(key, options = {}) {
        const shortcutKey = buildShortcutKey(key, options);
        shortcuts.delete(shortcutKey);
    }

    /**
     * Build a unique key for the shortcut map
     */
    function buildShortcutKey(key, options) {
        const parts = [];
        if (options.ctrl) parts.push('ctrl');
        if (options.shift) parts.push('shift');
        if (options.alt) parts.push('alt');
        if (options.meta) parts.push('meta');
        parts.push(key.toLowerCase());
        return parts.join('+');
    }

    /**
     * Handle global keydown events
     */
    function handleGlobalKeydown(event) {
        // Don't intercept if user is typing in an input
        if (isInputElement(event.target)) {
            // Still allow Escape to blur inputs
            if (event.key === 'Escape') {
                event.target.blur();
                return;
            }
            return;
        }

        const shortcutKey = buildShortcutKey(event.key, {
            ctrl: event.ctrlKey || event.metaKey,
            shift: event.shiftKey,
            alt: event.altKey,
            meta: event.metaKey
        });

        const shortcut = shortcuts.get(shortcutKey);
        if (shortcut) {
            event.preventDefault();
            event.stopPropagation();
            shortcut.callback(event);
        }
    }

    /**
     * Handle focus changes to track sidebar focus
     */
    function handleFocusIn(event) {
        const sidebarItem = event.target.closest('.sidebar-item');
        if (sidebarItem) {
            currentFocusIndex = sidebarItems.indexOf(sidebarItem);
        }
    }

    /**
     * Check if element is an input that should capture keyboard
     */
    function isInputElement(element) {
        const tagName = element.tagName.toLowerCase();
        return tagName === 'input' ||
               tagName === 'textarea' ||
               tagName === 'select' ||
               element.isContentEditable;
    }

    /**
     * Navigate to a specific tab
     */
    function navigateToTab(tabId) {
        if (typeof showTab === 'function') {
            showTab(tabId);
            // Focus the corresponding sidebar item
            const item = document.querySelector(`.sidebar-item[data-route="${tabId}"]`);
            if (item) {
                item.focus();
            }
        }
    }

    /**
     * Navigate through sidebar items
     */
    function navigateSidebar(direction) {
        updateSidebarItems();

        if (sidebarItems.length === 0) return;

        // If no item is focused, start at first/last based on direction
        if (currentFocusIndex < 0) {
            currentFocusIndex = direction > 0 ? 0 : sidebarItems.length - 1;
        } else {
            currentFocusIndex += direction;
        }

        // Wrap around
        if (currentFocusIndex >= sidebarItems.length) {
            currentFocusIndex = 0;
        } else if (currentFocusIndex < 0) {
            currentFocusIndex = sidebarItems.length - 1;
        }

        sidebarItems[currentFocusIndex]?.focus();
    }

    /**
     * Activate the currently focused sidebar item
     */
    function activateFocusedSidebarItem() {
        const focused = document.activeElement;
        if (focused?.classList.contains('sidebar-item')) {
            focused.click();
        }
    }

    /**
     * Focus the search input if available
     */
    function focusSearch() {
        const searchInput = document.querySelector('input[type="search"], input[placeholder*="earch"]');
        if (searchInput) {
            searchInput.focus();
            searchInput.select();
        }
    }

    /**
     * Close any open overlays (modals, drawers, dropdowns)
     */
    function closeOverlays() {
        // Close drawers
        if (window.Athena?.drawer?.close) {
            Athena.drawer.close();
        }

        // Close activity sidebar
        const activitySidebar = document.getElementById('activity-sidebar');
        if (activitySidebar && !activitySidebar.classList.contains('translate-x-full')) {
            if (window.Athena?.activitySidebar?.close) {
                Athena.activitySidebar.close();
            }
        }

        // Close any open dropdowns
        document.querySelectorAll('[data-dropdown-open="true"]').forEach(dropdown => {
            dropdown.setAttribute('data-dropdown-open', 'false');
            dropdown.classList.add('hidden');
        });

        // Close modals
        document.querySelectorAll('.modal-overlay, [role="dialog"]').forEach(modal => {
            if (modal.classList.contains('hidden')) return;
            modal.classList.add('hidden');
            // Restore body scroll
            document.body.style.overflow = '';
        });
    }

    /**
     * Refresh the current page
     */
    function refreshCurrentPage() {
        const activeTab = document.querySelector('.tab-content:not(.hidden)');
        if (!activeTab) return;

        const tabId = activeTab.id;

        // Map tab IDs to page objects
        const pageMap = {
            'mission-control': 'MissionControl',
            'voice-pipelines': 'VoicePipelines',
            'memory-context': 'MemoryContext',
            'integrations': 'Integrations'
        };

        const pageName = pageMap[tabId];
        if (pageName && window.Athena?.pages?.[pageName]?.init) {
            // Destroy and reinitialize
            if (window.Athena.pages[pageName].destroy) {
                window.Athena.pages[pageName].destroy();
            }
            window.Athena.pages[pageName].init();

            if (window.Athena?.toast?.show) {
                Athena.toast.show('Page refreshed', 'success');
            }
        }
    }

    /**
     * Show keyboard shortcuts help modal
     */
    function showShortcutsHelp() {
        // Group shortcuts by category
        const categories = {
            'Navigation': [],
            'Actions': [],
            'General': []
        };

        shortcuts.forEach((shortcut, key) => {
            const display = formatShortcutDisplay(key);
            const entry = { display, description: shortcut.description };

            if (key.includes('ctrl+') && /\d/.test(key)) {
                categories['Navigation'].push(entry);
            } else if (['arrowdown', 'arrowup', 'enter'].some(k => key.includes(k))) {
                categories['Navigation'].push(entry);
            } else if (['r', '/'].some(k => key.includes(k))) {
                categories['Actions'].push(entry);
            } else {
                categories['General'].push(entry);
            }
        });

        // Build modal content
        const content = document.createElement('div');
        content.className = 'p-6 max-w-lg';
        content.innerHTML = `
            <div class="flex items-center justify-between mb-6">
                <h2 class="text-xl font-semibold text-white">Keyboard Shortcuts</h2>
                <button onclick="this.closest('.modal-overlay').classList.add('hidden')" class="text-gray-400 hover:text-white">
                    <i data-lucide="x" class="w-5 h-5"></i>
                </button>
            </div>
            <div class="space-y-6">
                ${Object.entries(categories).map(([category, items]) => items.length > 0 ? `
                    <div>
                        <h3 class="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">${category}</h3>
                        <div class="space-y-2">
                            ${items.map(item => `
                                <div class="flex items-center justify-between py-1">
                                    <span class="text-gray-300">${item.description}</span>
                                    <kbd class="px-2 py-1 bg-gray-700 rounded text-xs font-mono text-gray-300">${item.display}</kbd>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                ` : '').join('')}
            </div>
        `;

        // Create modal overlay
        let modal = document.getElementById('keyboard-shortcuts-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'keyboard-shortcuts-modal';
            modal.className = 'modal-overlay fixed inset-0 bg-black/50 flex items-center justify-center z-50';
            modal.onclick = (e) => {
                if (e.target === modal) modal.classList.add('hidden');
            };
            document.body.appendChild(modal);
        }

        modal.innerHTML = '';
        const modalContent = document.createElement('div');
        modalContent.className = 'bg-gray-800 rounded-lg shadow-xl border border-gray-700';
        modalContent.appendChild(content);
        modal.appendChild(modalContent);
        modal.classList.remove('hidden');

        // Refresh Lucide icons
        if (window.lucide) {
            lucide.createIcons();
        }
    }

    /**
     * Format shortcut key for display
     */
    function formatShortcutDisplay(key) {
        const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;

        return key
            .replace('ctrl+', isMac ? '⌘ ' : 'Ctrl + ')
            .replace('shift+', isMac ? '⇧ ' : 'Shift + ')
            .replace('alt+', isMac ? '⌥ ' : 'Alt + ')
            .replace('meta+', isMac ? '⌘ ' : 'Win + ')
            .replace('arrowdown', '↓')
            .replace('arrowup', '↑')
            .replace('arrowleft', '←')
            .replace('arrowright', '→')
            .replace('enter', '↵ Enter')
            .replace('escape', 'Esc')
            .replace('/', '/')
            .toUpperCase();
    }

    /**
     * Trap focus within an element (for modals)
     */
    function trapFocus(element) {
        const focusableElements = element.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        const firstFocusable = focusableElements[0];
        const lastFocusable = focusableElements[focusableElements.length - 1];

        function handleTabKey(e) {
            if (e.key !== 'Tab') return;

            if (e.shiftKey) {
                if (document.activeElement === firstFocusable) {
                    e.preventDefault();
                    lastFocusable?.focus();
                }
            } else {
                if (document.activeElement === lastFocusable) {
                    e.preventDefault();
                    firstFocusable?.focus();
                }
            }
        }

        element.addEventListener('keydown', handleTabKey);

        // Focus first element
        firstFocusable?.focus();

        // Return cleanup function
        return () => {
            element.removeEventListener('keydown', handleTabKey);
        };
    }

    /**
     * Get all registered shortcuts
     */
    function getShortcuts() {
        return Array.from(shortcuts.entries()).map(([key, value]) => ({
            key,
            ...value
        }));
    }

    // Export to Athena namespace
    Athena.keyboard = {
        init,
        destroy,
        register,
        unregister,
        getShortcuts,
        trapFocus,
        closeOverlays,
        showShortcutsHelp
    };

    // Auto-initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
