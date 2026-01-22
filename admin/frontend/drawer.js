/**
 * Athena Drawer - Slide-out Panel Component
 *
 * Provides a slide-out drawer for editing, details, and forms.
 * Supports accessibility features including focus trapping and keyboard navigation.
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[AthenaDrawer] Athena namespace not found');
        return;
    }

    // Current drawer state
    let currentDrawer = null;
    let focusTrap = null;
    let lastFocusedElement = null;
    let backdropElement = null;

    /**
     * Open a drawer panel.
     * @param {Object} options - Drawer options
     * @param {string} options.title - Drawer title
     * @param {string|HTMLElement} options.content - Drawer content (HTML string or element)
     * @param {string} [options.width='480px'] - Drawer width
     * @param {string} [options.position='right'] - Position (right, left)
     * @param {Function} [options.onSave] - Save button callback
     * @param {Function} [options.onClose] - Close callback
     * @param {string} [options.saveText='Save'] - Save button text
     * @param {boolean} [options.showSave=true] - Whether to show save button
     * @param {string} [options.id] - Custom drawer ID
     * @returns {Object} Drawer control object
     */
    function open(options = {}) {
        // Close existing drawer first
        if (currentDrawer) {
            close(false); // Don't trigger onClose callback
        }

        const {
            title = 'Details',
            content = '',
            width = '480px',
            position = 'right',
            onSave = null,
            onClose = null,
            saveText = 'Save',
            showSave = true,
            id = 'athena-drawer-' + Date.now()
        } = options;

        // Store last focused element for restoration
        lastFocusedElement = document.activeElement;

        // Create backdrop
        backdropElement = document.createElement('div');
        backdropElement.className = `
            fixed inset-0 bg-black/50 backdrop-blur-sm
            z-[var(--z-modal-backdrop,90)]
            transition-opacity duration-300
            opacity-0
        `;
        backdropElement.addEventListener('click', () => close());
        document.body.appendChild(backdropElement);

        // Create drawer
        const drawer = document.createElement('div');
        drawer.id = id;
        drawer.className = `
            fixed top-0 ${position === 'right' ? 'right-0' : 'left-0'} bottom-0
            bg-dark-card border-${position === 'right' ? 'l' : 'r'} border-dark-border
            z-[var(--z-modal,100)]
            flex flex-col
            transform transition-transform duration-300 ease-out
            ${position === 'right' ? 'translate-x-full' : '-translate-x-full'}
        `;
        drawer.style.width = width;
        drawer.setAttribute('role', 'dialog');
        drawer.setAttribute('aria-modal', 'true');
        drawer.setAttribute('aria-labelledby', id + '-title');

        // Build drawer HTML
        drawer.innerHTML = `
            <!-- Header -->
            <div class="flex items-center justify-between px-6 py-4 border-b border-dark-border">
                <h2 id="${id}-title" class="text-lg font-semibold text-white">${escapeHtml(title)}</h2>
                <button class="drawer-close p-2 text-gray-400 hover:text-white hover:bg-dark-elevated rounded-lg transition-colors"
                        aria-label="Close drawer">
                    <i data-lucide="x" class="w-5 h-5"></i>
                </button>
            </div>

            <!-- Content -->
            <div class="drawer-content flex-1 overflow-y-auto p-6">
                ${typeof content === 'string' ? content : ''}
            </div>

            <!-- Footer -->
            <div class="flex items-center justify-end gap-3 px-6 py-4 border-t border-dark-border bg-dark-bg">
                <button class="drawer-cancel px-4 py-2 text-gray-400 hover:text-white transition-colors">
                    Cancel
                </button>
                ${showSave ? `
                    <button class="drawer-save px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors">
                        ${escapeHtml(saveText)}
                    </button>
                ` : ''}
            </div>
        `;

        // Add to DOM
        document.body.appendChild(drawer);

        // If content is an element, append it
        if (typeof content !== 'string' && content instanceof HTMLElement) {
            drawer.querySelector('.drawer-content').appendChild(content);
        }

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [drawer] });
        }

        // Store reference
        currentDrawer = {
            element: drawer,
            options,
            onClose
        };

        // Bind events
        drawer.querySelector('.drawer-close').addEventListener('click', () => close());
        drawer.querySelector('.drawer-cancel').addEventListener('click', () => close());

        const saveBtn = drawer.querySelector('.drawer-save');
        if (saveBtn && onSave) {
            saveBtn.addEventListener('click', async () => {
                try {
                    saveBtn.disabled = true;
                    saveBtn.innerHTML = '<i data-lucide="loader-2" class="w-4 h-4 animate-spin inline mr-2"></i>Saving...';
                    await onSave();
                    close();
                } catch (error) {
                    console.error('[AthenaDrawer] Save error:', error);
                    saveBtn.disabled = false;
                    saveBtn.innerHTML = saveText;
                    if (Athena.components.Toast) {
                        Athena.components.Toast.error('Failed to save: ' + error.message);
                    }
                }
            });
        }

        // Setup keyboard handling
        document.addEventListener('keydown', handleKeydown);

        // Setup focus trap
        setupFocusTrap(drawer);

        // Animate in
        requestAnimationFrame(() => {
            backdropElement.classList.remove('opacity-0');
            backdropElement.classList.add('opacity-100');
            drawer.classList.remove(position === 'right' ? 'translate-x-full' : '-translate-x-full');
            drawer.classList.add('translate-x-0');
        });

        // Focus first focusable element
        setTimeout(() => {
            const firstFocusable = drawer.querySelector('button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
            if (firstFocusable) firstFocusable.focus();
        }, 300);

        // Prevent body scroll
        document.body.style.overflow = 'hidden';

        // Emit event
        if (Athena.emit) Athena.emit('drawer:open', { id, title });

        return {
            close: () => close(),
            element: drawer,
            setContent: (newContent) => {
                const contentEl = drawer.querySelector('.drawer-content');
                if (contentEl) {
                    contentEl.innerHTML = typeof newContent === 'string' ? newContent : '';
                    if (typeof newContent !== 'string' && newContent instanceof HTMLElement) {
                        contentEl.appendChild(newContent);
                    }
                    if (window.lucide) lucide.createIcons({ nodes: [contentEl] });
                }
            },
            setTitle: (newTitle) => {
                const titleEl = drawer.querySelector('#' + id + '-title');
                if (titleEl) titleEl.textContent = newTitle;
            }
        };
    }

    /**
     * Close the current drawer.
     * @param {boolean} [triggerCallback=true] - Whether to trigger onClose callback
     */
    function close(triggerCallback = true) {
        if (!currentDrawer) return;

        const { element: drawer, options, onClose } = currentDrawer;
        const position = options.position || 'right';

        // Animate out
        drawer.classList.remove('translate-x-0');
        drawer.classList.add(position === 'right' ? 'translate-x-full' : '-translate-x-full');

        if (backdropElement) {
            backdropElement.classList.remove('opacity-100');
            backdropElement.classList.add('opacity-0');
        }

        // Cleanup after animation
        setTimeout(() => {
            drawer.remove();
            if (backdropElement) {
                backdropElement.remove();
                backdropElement = null;
            }
            currentDrawer = null;

            // Restore body scroll
            document.body.style.overflow = '';

            // Restore focus
            if (lastFocusedElement) {
                lastFocusedElement.focus();
                lastFocusedElement = null;
            }

            // Remove keyboard listener
            document.removeEventListener('keydown', handleKeydown);

            // Destroy focus trap
            if (focusTrap) {
                focusTrap.destroy();
                focusTrap = null;
            }

            // Trigger callback
            if (triggerCallback && onClose) {
                onClose();
            }

            // Emit event
            if (Athena.emit) Athena.emit('drawer:close', { id: drawer.id });

        }, 300);
    }

    /**
     * Handle keyboard events.
     */
    function handleKeydown(e) {
        if (e.key === 'Escape') {
            close();
        }
    }

    /**
     * Setup focus trap within drawer.
     */
    function setupFocusTrap(container) {
        const focusableElements = container.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        const firstFocusable = focusableElements[0];
        const lastFocusable = focusableElements[focusableElements.length - 1];

        function trapFocus(e) {
            if (e.key !== 'Tab') return;

            if (e.shiftKey && document.activeElement === firstFocusable) {
                e.preventDefault();
                lastFocusable.focus();
            } else if (!e.shiftKey && document.activeElement === lastFocusable) {
                e.preventDefault();
                firstFocusable.focus();
            }
        }

        container.addEventListener('keydown', trapFocus);

        focusTrap = {
            destroy: () => container.removeEventListener('keydown', trapFocus)
        };
    }

    /**
     * Check if a drawer is currently open.
     */
    function isOpen() {
        return currentDrawer !== null;
    }

    /**
     * Get the current drawer element.
     */
    function getElement() {
        return currentDrawer?.element || null;
    }

    /**
     * Escape HTML to prevent XSS.
     */
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Export to Athena namespace
    Athena.components.Drawer = {
        open,
        close,
        isOpen,
        getElement
    };

    // Also export as global for compatibility
    window.AthenaDrawer = Athena.components.Drawer;

})(window.Athena);
