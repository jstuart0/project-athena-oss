/**
 * Command Palette for Athena Admin
 * Provides Cmd+K fuzzy search navigation
 *
 * Part of Phase 7: Command Palette
 */

(function() {
    // Dependency check
    if (!window.Router) {
        console.error('[CommandPalette] Router not loaded. Check script order.');
        return;
    }

    const CommandPalette = {
        isOpen: false,
        selectedIndex: 0,
        filteredCommands: [],
        triggerElement: null,

        /**
         * All available commands
         */
        commands: [
            // Dashboard
            { id: 'dashboard', label: 'Dashboard', description: 'Go to dashboard', route: 'dashboard', category: 'Navigation', icon: 'layout-dashboard' },

            // Configuration
            { id: 'policies', label: 'Policies', description: 'Manage policies', route: 'policies', category: 'Configuration', icon: 'file-text' },
            { id: 'secrets', label: 'Secrets', description: 'Manage secrets', route: 'secrets', category: 'Configuration', icon: 'lock' },
            { id: 'features', label: 'Feature Flags', description: 'Toggle feature flags', route: 'features', category: 'Configuration', icon: 'sliders-horizontal' },
            { id: 'external-api-keys', label: 'External API Keys', description: 'Manage external API keys', route: 'external-api-keys', category: 'Configuration', icon: 'key' },
            { id: 'user-api-keys', label: 'User API Keys', description: 'Manage user API keys', route: 'user-api-keys', category: 'Configuration', icon: 'key' },
            { id: 'base-knowledge', label: 'Base Knowledge', description: 'Configure base knowledge', route: 'base-knowledge', category: 'Configuration', icon: 'book-open' },
            { id: 'system-config', label: 'System Config', description: 'System configuration', route: 'system-config', category: 'Configuration', icon: 'settings-2' },
            { id: 'presets', label: 'Presets', description: 'Manage presets', route: 'presets', category: 'Configuration', icon: 'bookmark' },

            // AI & LLM
            { id: 'llm-backends', label: 'LLM Backends', description: 'Configure LLM backends', route: 'llm-backends', category: 'AI & LLM', icon: 'cpu' },
            { id: 'model-config', label: 'Model Config', description: 'Configure models', route: 'model-config', category: 'AI & LLM', icon: 'box' },
            { id: 'model-downloads', label: 'Model Downloads', description: 'Download models', route: 'model-downloads', category: 'AI & LLM', icon: 'download' },
            { id: 'cloud-providers', label: 'Cloud Providers', description: 'Manage cloud LLM providers', route: 'cloud-providers', category: 'AI & LLM', icon: 'cloud' },
            { id: 'service-bypass', label: 'Service Bypass', description: 'RAG bypass settings', route: 'service-bypass', category: 'AI & LLM', icon: 'git-branch' },

            // Voice
            { id: 'voice-config', label: 'Voice Config', description: 'Voice pipeline configuration', route: 'voice-config', category: 'Voice', icon: 'mic' },
            { id: 'voice-automations', label: 'Voice Automations', description: 'Manage voice automations', route: 'voice-automations', category: 'Voice', icon: 'play' },

            // Devices & Smart Home
            { id: 'devices', label: 'Devices', description: 'Manage devices', route: 'devices', category: 'Devices', icon: 'monitor' },
            { id: 'room-audio', label: 'Room Audio', description: 'Room audio settings', route: 'room-audio', category: 'Devices', icon: 'volume-2' },
            { id: 'room-tv', label: 'Room TV', description: 'Room TV settings', route: 'room-tv', category: 'Devices', icon: 'tv' },

            // Users & Security
            { id: 'users', label: 'Users', description: 'Manage users', route: 'users', category: 'Security', icon: 'users' },
            { id: 'audit', label: 'Audit Logs', description: 'View audit logs', route: 'audit', category: 'Security', icon: 'clipboard-list' },

            // Analytics
            { id: 'performance-metrics', label: 'Performance Metrics', description: 'View performance metrics', route: 'performance-metrics', category: 'Analytics', icon: 'activity' },

            // Alerts
            { id: 'alerts', label: 'Alerts', description: 'View and manage alerts', route: 'alerts', category: 'Alerts', icon: 'bell' },

            // System
            { id: 'service-control', label: 'Service Control', description: 'Control services', route: 'service-control', category: 'System', icon: 'server' },
            { id: 'settings', label: 'Settings', description: 'Application settings', route: 'settings', category: 'System', icon: 'settings' },

            // Actions
            { id: 'refresh', label: 'Refresh Page', description: 'Refresh current page', action: () => location.reload(), category: 'Actions', icon: 'refresh-cw' },
            { id: 'logout', label: 'Logout', description: 'Sign out of admin', action: () => Auth.logout(), category: 'Actions', icon: 'log-out' }
        ],

        /**
         * Open the command palette
         */
        open() {
            if (this.isOpen) return;

            this.triggerElement = document.activeElement;
            this.isOpen = true;
            this.selectedIndex = 0;
            this.filteredCommands = [...this.commands];

            this._render();
            this._show();

            // Focus input
            setTimeout(() => {
                const input = document.getElementById('command-palette-input');
                if (input) input.focus();
            }, 50);
        },

        /**
         * Close the command palette
         */
        close() {
            if (!this.isOpen) return;

            this.isOpen = false;
            this._hide();

            // Return focus to trigger element
            if (this.triggerElement) {
                this.triggerElement.focus();
            }
        },

        /**
         * Toggle the command palette
         */
        toggle() {
            if (this.isOpen) {
                this.close();
            } else {
                this.open();
            }
        },

        /**
         * Filter commands based on query
         */
        filter(query) {
            if (!query) {
                this.filteredCommands = [...this.commands];
            } else {
                const lowerQuery = query.toLowerCase();
                this.filteredCommands = this.commands.filter(cmd => {
                    return cmd.label.toLowerCase().includes(lowerQuery) ||
                           cmd.description.toLowerCase().includes(lowerQuery) ||
                           cmd.category.toLowerCase().includes(lowerQuery);
                });
            }

            this.selectedIndex = 0;
            this._updateResults();
        },

        /**
         * Execute selected command
         */
        execute(index = this.selectedIndex) {
            const command = this.filteredCommands[index];
            if (!command) return;

            this.close();

            if (command.action) {
                command.action();
            } else if (command.route) {
                Router.navigate(command.route);
            }
        },

        /**
         * Navigate selection up
         */
        selectPrevious() {
            if (this.filteredCommands.length === 0) return;

            this.selectedIndex = (this.selectedIndex - 1 + this.filteredCommands.length) % this.filteredCommands.length;
            this._updateSelection();
        },

        /**
         * Navigate selection down
         */
        selectNext() {
            if (this.filteredCommands.length === 0) return;

            this.selectedIndex = (this.selectedIndex + 1) % this.filteredCommands.length;
            this._updateSelection();
        },

        /**
         * Render the palette HTML
         */
        _render() {
            let container = document.getElementById('command-palette');

            if (!container) {
                container = document.createElement('div');
                container.id = 'command-palette';
                document.body.appendChild(container);
            }

            container.innerHTML = `
                <div class="command-palette-backdrop" onclick="CommandPalette.close()"></div>
                <div class="command-palette-dialog" role="dialog" aria-modal="true" aria-label="Command palette">
                    <div class="command-palette-header">
                        <i data-lucide="search" class="w-5 h-5 text-gray-400"></i>
                        <input
                            type="text"
                            id="command-palette-input"
                            class="command-palette-input"
                            placeholder="Search commands..."
                            role="combobox"
                            aria-expanded="true"
                            aria-controls="command-palette-results"
                            aria-autocomplete="list"
                            oninput="CommandPalette.filter(this.value)"
                            onkeydown="CommandPalette._handleKeyDown(event)"
                        >
                        <kbd class="command-palette-kbd">ESC</kbd>
                    </div>
                    <div id="command-palette-results" class="command-palette-results" role="listbox" aria-label="Search results">
                        ${this._renderResults()}
                    </div>
                    <div class="command-palette-footer">
                        <div class="command-palette-hint">
                            <kbd>↑↓</kbd> Navigate
                            <kbd>↵</kbd> Select
                            <kbd>ESC</kbd> Close
                        </div>
                    </div>
                </div>
            `;

            // Initialize icons
            if (typeof lucide !== 'undefined') {
                lucide.createIcons();
            }
        },

        /**
         * Render results list
         */
        _renderResults() {
            if (this.filteredCommands.length === 0) {
                return `
                    <div class="command-palette-empty">
                        <i data-lucide="search-x" class="w-8 h-8 text-gray-500 mb-2"></i>
                        <p>No commands found</p>
                    </div>
                `;
            }

            let currentCategory = '';
            let html = '';

            this.filteredCommands.forEach((cmd, index) => {
                if (cmd.category !== currentCategory) {
                    currentCategory = cmd.category;
                    html += `<div class="command-palette-category">${currentCategory}</div>`;
                }

                const isSelected = index === this.selectedIndex;
                html += `
                    <div
                        class="command-palette-item ${isSelected ? 'selected' : ''}"
                        role="option"
                        aria-selected="${isSelected}"
                        data-index="${index}"
                        onclick="CommandPalette.execute(${index})"
                        onmouseenter="CommandPalette._hoverItem(${index})"
                    >
                        <div class="command-palette-item-icon">
                            <i data-lucide="${cmd.icon || 'circle'}"></i>
                        </div>
                        <div class="command-palette-item-content">
                            <div class="command-palette-item-label">${cmd.label}</div>
                            <div class="command-palette-item-description">${cmd.description}</div>
                        </div>
                        ${isSelected ? '<i data-lucide="corner-down-left" class="w-4 h-4 text-gray-400"></i>' : ''}
                    </div>
                `;
            });

            return html;
        },

        /**
         * Update results after filtering
         */
        _updateResults() {
            const container = document.getElementById('command-palette-results');
            if (container) {
                container.innerHTML = this._renderResults();
                if (typeof lucide !== 'undefined') {
                    lucide.createIcons();
                }
            }
        },

        /**
         * Update selection highlighting
         */
        _updateSelection() {
            const items = document.querySelectorAll('.command-palette-item');
            items.forEach((item, index) => {
                const isSelected = index === this.selectedIndex;
                item.classList.toggle('selected', isSelected);
                item.setAttribute('aria-selected', isSelected);

                // Scroll into view if needed
                if (isSelected) {
                    item.scrollIntoView({ block: 'nearest' });
                }
            });
        },

        /**
         * Handle keyboard events
         */
        _handleKeyDown(event) {
            switch (event.key) {
                case 'ArrowDown':
                    event.preventDefault();
                    this.selectNext();
                    break;
                case 'ArrowUp':
                    event.preventDefault();
                    this.selectPrevious();
                    break;
                case 'Enter':
                    event.preventDefault();
                    this.execute();
                    break;
                case 'Escape':
                    event.preventDefault();
                    this.close();
                    break;
            }
        },

        /**
         * Handle mouse hover on item
         */
        _hoverItem(index) {
            this.selectedIndex = index;
            this._updateSelection();
        },

        /**
         * Show the palette
         */
        _show() {
            const container = document.getElementById('command-palette');
            if (container) {
                container.classList.add('open');
            }
        },

        /**
         * Hide the palette
         */
        _hide() {
            const container = document.getElementById('command-palette');
            if (container) {
                container.classList.remove('open');
            }
        },

        /**
         * Add a custom command
         */
        addCommand(command) {
            this.commands.push(command);
        },

        /**
         * Initialize keyboard shortcut
         */
        init() {
            document.addEventListener('keydown', (event) => {
                // Cmd+K or Ctrl+K
                if ((event.metaKey || event.ctrlKey) && event.key === 'k') {
                    event.preventDefault();
                    this.toggle();
                }

                // Escape to close
                if (event.key === 'Escape' && this.isOpen) {
                    event.preventDefault();
                    this.close();
                }
            });
        }
    };

    // Expose on window
    window.CommandPalette = CommandPalette;
    window.openCommandPalette = () => CommandPalette.open();
})();
